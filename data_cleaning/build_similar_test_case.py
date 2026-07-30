#!/usr/bin/env python3
"""Build deterministic MIMIC similar/test case CSV files.

This is the executable implementation of ``docs/similar_test_case_design.md``.
It performs the ICD3-stratified 80:20 split, extracts audited discharge-note
sections for the similar set, truncates test notes before the resolved
``Discharge Medications`` section, and writes deterministic QC/manifest JSON.

Only Python's standard library is required.  All destinations are first
written to sibling temporary files and are replaced only after every runtime
and current-baseline validation succeeds.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence, TextIO


ALGORITHM_VERSION = "mimic2-similar-test-v1"
TEXT_PARSER_VERSION = "mimic2-discharge-section-parser-v2"

EXPECTED_SOURCE_SHA256 = (
    "c887c2c96b3c2416f1f512bcbf8f39cff34524b283a1bb640847808aa21229b4"
)
EXPECTED_SOURCE_SIZE = 72_116_486
EXPECTED_SOURCE_ROWS = 6_777
EXPECTED_GROUPS = 109
EXPECTED_TEST_ROWS = 1_355
EXPECTED_SIMILAR_ROWS = 5_422
EXPECTED_INFEASIBLE_GROUPS = 27
EXPECTED_QUOTA_SHA256 = (
    "884f35885444b35ef852048434ef7ad02aa6d79bf5600fd615dfd0ba066fe531"
)

SOURCE_FIELDS = (
    "subject_id",
    "hadm_id",
    "admittime",
    "seq_num",
    "icd_code",
    "icd_version",
    "text",
)
IDENTIFIER_FIELDS = ("subject_id", "hadm_id")

SECTION_FIELDS = (
    "chief_complaint",
    "major_surgical_or_invasive_procedure",
    "history_of_present_illness",
    "past_medical_history",
    "social_history",
    "family_history",
    "physical_exam",
    "pertinent_results",
    "brief_hospital_course",
    "medications_on_admission",
    "discharge_medications",
    "discharge_disposition",
    "discharge_diagnosis",
    "discharge_condition",
    "discharge_instructions",
)

SECTION_TITLES = {
    "chief_complaint": "Chief Complaint",
    "major_surgical_or_invasive_procedure": (
        "Major Surgical or Invasive Procedure"
    ),
    "history_of_present_illness": "History of Present Illness",
    "past_medical_history": "Past Medical History",
    "social_history": "Social History",
    "family_history": "Family History",
    "physical_exam": "Physical Exam",
    "pertinent_results": "Pertinent Results",
    "brief_hospital_course": "Brief Hospital Course",
    "medications_on_admission": "Medications on Admission",
    "discharge_medications": "Discharge Medications",
    "discharge_disposition": "Discharge Disposition",
    "discharge_diagnosis": "Discharge Diagnosis",
    "discharge_condition": "Discharge Condition",
    "discharge_instructions": "Discharge Instructions",
}
SECTION_INDEX = {field: index for index, field in enumerate(SECTION_FIELDS)}

SIMILAR_FIELDS = (
    "subject_id",
    "hadm_id",
    "admittime",
    "seq_num",
    "icd_code",
    "icd_version",
    *SECTION_FIELDS,
)
TEST_FIELDS = (
    "subject_id",
    "hadm_id",
    "seq_num",
    "icd_code",
    "icd_version",
    "discharge_text_before_disposition",
)

ICD_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{2,6}$")
REDACTION = r"_{3,}"

# (standard, special-added, resolved, missing) from design section 8.1.1.
EXPECTED_TEXT_AUDIT = {
    "chief_complaint": (6_372, 240, 6_612, 165),
    "major_surgical_or_invasive_procedure": (6_759, 15, 6_774, 3),
    "history_of_present_illness": (6_569, 18, 6_587, 190),
    "past_medical_history": (6_556, 6, 6_562, 215),
    "social_history": (6_466, 20, 6_486, 291),
    "family_history": (6_405, 25, 6_430, 347),
    "physical_exam": (6_360, 195, 6_555, 222),
    "pertinent_results": (6_627, 5, 6_632, 145),
    "brief_hospital_course": (5_949, 36, 5_985, 792),
    "medications_on_admission": (6_342, 246, 6_588, 189),
    "discharge_medications": (6_610, 2, 6_612, 165),
    "discharge_disposition": (6_597, 58, 6_655, 122),
    "discharge_diagnosis": (6_640, 110, 6_750, 27),
    "discharge_condition": (6_775, 2, 6_777, 0),
    "discharge_instructions": (6_733, 0, 6_733, 44),
}

RecordKey = tuple[str, str]


@dataclass(frozen=True)
class SourceRecord:
    subject_id: str
    hadm_id: str
    admittime: str
    seq_num: str
    icd_code: str
    icd_version: str
    text: str
    icd3: str
    score: str
    source_row_number: int

    @property
    def key(self) -> RecordKey:
        return self.subject_id, self.hadm_id


@dataclass(frozen=True)
class GroupQuota:
    icd3: str
    total: int
    lower: int
    upper: int
    test: int
    integer_infeasible: bool

    @property
    def similar(self) -> int:
        return self.total - self.test


@dataclass(frozen=True)
class LineSpan:
    start: int
    end: int
    content: str


@dataclass(frozen=True)
class HeadingCandidate:
    field: str
    start: int
    end: int
    priority: int
    label: str
    kind: str = "normal"

    @property
    def identity(self) -> tuple[str, int, int, int, str, str]:
        return (
            self.field,
            self.start,
            self.end,
            self.priority,
            self.label,
            self.kind,
        )


@dataclass
class ParseResult:
    sections: dict[str, str]
    resolved: dict[str, list[HeadingCandidate]]
    standard_fields: set[str]
    rejected: list[tuple[HeadingCandidate, str]]
    discharge_medications_start: int | None
    followup_boundaries: list[HeadingCandidate]


@dataclass(frozen=True)
class AliasRule:
    field: str
    label: str
    priority: int
    pattern: re.Pattern[str]
    kind: str = "normal"


def find_project_root(script_dir: Path) -> Path:
    """Find the nearest ancestor containing the designed source CSV."""
    relative = (
        Path("data_output")
        / "first_time_seq1_dataset_icd_selected_with_discharge.csv"
    )
    for candidate in (script_dir, *script_dir.parents):
        if (candidate / relative).is_file():
            return candidate
    return script_dir.parent


def parse_args() -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    project_root = find_project_root(script_dir)
    output_dir = project_root / "data_output"
    parser = argparse.ArgumentParser(
        description=(
            "Build deterministic mimic_similar.csv and mimic_test.csv "
            "according to docs/similar_test_case_design.md."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=(
            output_dir
            / "first_time_seq1_dataset_icd_selected_with_discharge.csv"
        ),
        help="Source CSV containing the seven designed fields.",
    )
    parser.add_argument(
        "--similar-output",
        type=Path,
        default=output_dir / "mimic_similar.csv",
        help="Structured similar-case CSV destination.",
    )
    parser.add_argument(
        "--test-output",
        type=Path,
        default=output_dir / "mimic_test.csv",
        help="Truncated test-case CSV destination.",
    )
    parser.add_argument(
        "--quality-report",
        type=Path,
        default=output_dir / "mimic_similar_test_quality_report.json",
        help="Detailed deterministic quality-report JSON destination.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=output_dir / "mimic_similar_test_manifest.json",
        help="Deterministic split/output manifest JSON destination.",
    )
    parser.add_argument(
        "--allow-baseline-mismatch",
        action="store_true",
        help=(
            "Allow a source SHA other than the documented baseline. Quotas "
            "are recomputed, but the report is marked as needing a design "
            "baseline update."
        ),
    )
    return parser.parse_args()


def configure_csv_field_limit() -> None:
    """Allow CSV cells as large as the interpreter supports."""
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 10


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_icd(value: str) -> str:
    return value.strip().upper().replace(".", "")


def stable_score(subject_id: str, hadm_id: str) -> str:
    payload = f"{ALGORITHM_VERSION}|{subject_id}|{hadm_id}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def require_distinct_paths(input_path: Path, destinations: Sequence[Path]) -> None:
    resolved_input = input_path.resolve()
    resolved_destinations = [path.resolve() for path in destinations]
    if resolved_input in resolved_destinations:
        raise ValueError("An output path must not overwrite the source CSV")
    if len(set(resolved_destinations)) != len(resolved_destinations):
        raise ValueError("All output/report/manifest paths must be distinct")


def load_source(path: Path) -> tuple[list[SourceRecord], dict[str, Any]]:
    """Load and strictly validate the documented admission-level source."""
    if not path.is_file():
        raise FileNotFoundError(f"Required source CSV not found: {path}")

    records: list[SourceRecord] = []
    subject_rows: dict[str, int] = {}
    hadm_rows: dict[str, int] = {}
    pair_rows: dict[RecordKey, int] = {}
    text_digest_rows: dict[str, int] = {}
    icd_versions: Counter[str] = Counter()
    seq_numbers: Counter[str] = Counter()

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = tuple(reader.fieldnames or ())
        if len(fieldnames) != len(set(fieldnames)):
            raise ValueError(f"Source CSV has duplicate column names: {path}")
        if fieldnames != SOURCE_FIELDS:
            raise ValueError(
                "Source CSV header must exactly equal "
                f"{SOURCE_FIELDS!r}; found {fieldnames!r}"
            )

        for row_number, row in enumerate(reader, start=2):
            if None in row:
                raise ValueError(
                    f"CSV record {row_number} has more values than its header"
                )
            if any(row.get(field) is None for field in SOURCE_FIELDS):
                raise ValueError(f"CSV record {row_number} is missing a value")

            subject_id = row["subject_id"]
            hadm_id = row["hadm_id"]
            if not subject_id.strip() or not hadm_id.strip():
                raise ValueError(f"Blank identifier at CSV record {row_number}")
            if subject_id != subject_id.strip() or hadm_id != hadm_id.strip():
                raise ValueError(
                    f"Identifier has surrounding whitespace at record {row_number}"
                )
            if subject_id in subject_rows:
                raise ValueError(
                    "subject_id is not an indivisible unique record: "
                    f"{subject_id!r} at records {subject_rows[subject_id]} and "
                    f"{row_number}; patient-level allocation is required"
                )
            if hadm_id in hadm_rows:
                raise ValueError(
                    f"Duplicate hadm_id {hadm_id!r} at records "
                    f"{hadm_rows[hadm_id]} and {row_number}"
                )
            key = (subject_id, hadm_id)
            if key in pair_rows:
                raise ValueError(f"Duplicate admission key {key!r}")

            code = normalize_icd(row["icd_code"])
            if len(code) < 3 or ICD_PATTERN.fullmatch(code) is None:
                raise ValueError(
                    f"Invalid normalized ICD code {code!r} at record {row_number}"
                )
            text = row["text"]
            if not text.strip():
                raise ValueError(f"Blank text at CSV record {row_number}")
            text_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if text_digest in text_digest_rows:
                raise ValueError(
                    "Duplicate discharge text at records "
                    f"{text_digest_rows[text_digest]} and {row_number}"
                )

            subject_rows[subject_id] = row_number
            hadm_rows[hadm_id] = row_number
            pair_rows[key] = row_number
            text_digest_rows[text_digest] = row_number
            icd_versions[row["icd_version"]] += 1
            seq_numbers[row["seq_num"]] += 1
            records.append(
                SourceRecord(
                    subject_id=subject_id,
                    hadm_id=hadm_id,
                    admittime=row["admittime"],
                    seq_num=row["seq_num"],
                    icd_code=row["icd_code"],
                    icd_version=row["icd_version"],
                    text=text,
                    icd3=code[:3],
                    score=stable_score(subject_id, hadm_id),
                    source_row_number=row_number,
                )
            )

    if not records:
        raise ValueError(f"Source CSV contains no data records: {path}")

    metadata = {
        "rows": len(records),
        "unique_subject_ids": len(subject_rows),
        "unique_hadm_ids": len(hadm_rows),
        "unique_texts": len(text_digest_rows),
        "icd_versions": dict(sorted(icd_versions.items())),
        "seq_numbers": dict(sorted(seq_numbers.items())),
    }
    return records, metadata


def nearest_infeasible_test_count(total: int) -> int:
    """Return the closest integer allocation to 20%, ties toward test=smaller."""
    return min(range(total + 1), key=lambda value: (abs(5 * value - total), value))


def compute_quotas(records: Sequence[SourceRecord]) -> dict[str, GroupQuota]:
    group_sizes = Counter(record.icd3 for record in records)
    working: dict[str, dict[str, int | bool]] = {}
    for icd3, total in sorted(group_sizes.items()):
        lower = (15 * total + 99) // 100
        upper = (25 * total) // 100
        infeasible = lower > upper
        test = nearest_infeasible_test_count(total) if infeasible else lower
        working[icd3] = {
            "total": total,
            "lower": lower,
            "upper": upper,
            "test": test,
            "infeasible": infeasible,
        }

    target = (2 * len(records) + 5) // 10
    minimum = sum(int(values["test"]) for values in working.values())
    maximum = sum(
        int(values["test"] if values["infeasible"] else values["upper"])
        for values in working.values()
    )
    if not minimum <= target <= maximum:
        raise ValueError(
            f"Overall test target {target} is outside reachable range "
            f"[{minimum}, {maximum}]"
        )

    current = minimum
    while current < target:
        candidates: list[tuple[Fraction, str]] = []
        for icd3, values in working.items():
            if values["infeasible"] or int(values["test"]) >= int(values["upper"]):
                continue
            total = int(values["total"])
            test = int(values["test"])
            before = 5 * test - total
            after = 5 * (test + 1) - total
            delta = Fraction(after * after - before * before, 25 * total * total)
            candidates.append((delta, icd3))
        if not candidates:
            raise RuntimeError("Quota allocation exhausted before reaching target")
        _, selected = min(candidates, key=lambda item: (item[0], item[1]))
        working[selected]["test"] = int(working[selected]["test"]) + 1
        current += 1

    return {
        icd3: GroupQuota(
            icd3=icd3,
            total=int(values["total"]),
            lower=int(values["lower"]),
            upper=int(values["upper"]),
            test=int(values["test"]),
            integer_infeasible=bool(values["infeasible"]),
        )
        for icd3, values in working.items()
    }


def assign_split(
    records: Sequence[SourceRecord], quotas: Mapping[str, GroupQuota]
) -> tuple[set[RecordKey], set[RecordKey]]:
    grouped: dict[str, list[SourceRecord]] = defaultdict(list)
    for record in records:
        grouped[record.icd3].append(record)

    test_keys: set[RecordKey] = set()
    similar_keys: set[RecordKey] = set()
    for icd3, group_records in grouped.items():
        ordered = sorted(
            group_records,
            key=lambda record: (record.score, record.subject_id, record.hadm_id),
        )
        quota = quotas[icd3].test
        test_keys.update(record.key for record in ordered[:quota])
        similar_keys.update(record.key for record in ordered[quota:])
    return similar_keys, test_keys


def flexible_title(title: str) -> str:
    return r"[ \t]+".join(re.escape(part) for part in title.split())


STANDARD_PATTERNS = {
    field: re.compile(
        rf"^[ \t]*{flexible_title(title)}[ \t]*:[ \t]*$", re.IGNORECASE
    )
    for field, title in SECTION_TITLES.items()
}
INLINE_PATTERNS = {
    field: re.compile(
        rf"^[ \t]*{flexible_title(title)}[ \t]*:[ \t]*"
        rf"(?P<content>\S.*)$",
        re.IGNORECASE,
    )
    for field, title in SECTION_TITLES.items()
}
NO_COLON_PATTERNS = {
    field: re.compile(
        rf"^[ \t]*{flexible_title(title)}[ \t]*$", re.IGNORECASE
    )
    for field, title in SECTION_TITLES.items()
}


def inline_alias(body: str) -> re.Pattern[str]:
    return re.compile(
        rf"^[ \t]*{body}[ \t]*:[ \t]*(?P<content>\S.*)?$",
        re.IGNORECASE,
    )


def standalone_alias(body: str, punctuation: str = ":") -> re.Pattern[str]:
    return re.compile(
        rf"^[ \t]*{body}[ \t]*{punctuation}[ \t]*$", re.IGNORECASE
    )


ALIAS_RULES = (
    AliasRule(
        "chief_complaint",
        "redacted_chief_complaint",
        2,
        standalone_alias(rf"{REDACTION}[ \t]+Complaint", r"[.:]"),
    ),
    AliasRule(
        "major_surgical_or_invasive_procedure",
        "redacted_major_middle",
        2,
        standalone_alias(
            rf"Major[ \t]+{REDACTION}[ \t]+or[ \t]+Invasive[ \t]+Procedure"
        ),
    ),
    AliasRule(
        "major_surgical_or_invasive_procedure",
        "redacted_major_first",
        2,
        standalone_alias(
            rf"{REDACTION}[ \t]+Surgical[ \t]+or[ \t]+Invasive[ \t]+Procedure"
        ),
    ),
    AliasRule(
        "history_of_present_illness",
        "redacted_history_of_present_illness",
        2,
        standalone_alias(
            rf"{REDACTION}[ \t]+of[ \t]+Present[ \t]+Illness"
        ),
    ),
    AliasRule(
        "past_medical_history",
        "redacted_past_medical_history",
        2,
        standalone_alias(rf"{REDACTION}[ \t]+Medical[ \t]+History"),
    ),
    AliasRule(
        "past_medical_history",
        "pmh_abbreviation",
        3,
        inline_alias(r"PMH(?:[ \t]*/[ \t]*PSH)?"),
        "contextual",
    ),
    AliasRule(
        "social_history",
        "sh_abbreviation",
        3,
        inline_alias(r"SH"),
        "contextual",
    ),
    AliasRule(
        "family_history",
        "fh_abbreviation",
        3,
        inline_alias(r"FH"),
        "contextual",
    ),
    AliasRule(
        "social_history",
        "redacted_history_as_social",
        3,
        standalone_alias(rf"{REDACTION}[ \t]+History"),
        "history_social",
    ),
    AliasRule(
        "family_history",
        "redacted_history_as_family",
        3,
        standalone_alias(rf"{REDACTION}[ \t]+History"),
        "history_family",
    ),
    AliasRule(
        "physical_exam",
        "redacted_physical_second",
        2,
        re.compile(
            rf"^[ \t]*Physical[ \t]+{REDACTION}[ \t]*:?[ \t]*$",
            re.IGNORECASE,
        ),
        "physical_exam",
    ),
    AliasRule(
        "physical_exam",
        "redacted_physical_first",
        2,
        standalone_alias(rf"{REDACTION}[ \t]+Exam"),
        "physical_exam",
    ),
    AliasRule(
        "physical_exam",
        "admission_physical_exam_fallback",
        4,
        re.compile(
            r"^[ \t]*(?:(?:Admission[ \t]+)?Physical[ \t]+"
            r"Exam(?:ination)?(?:[ \t]+on[ \t]+Admission)?|"
            r"Admission[ \t]+Exam)[ \t]*:?[ \t]*$",
            re.IGNORECASE,
        ),
        "physical_exam",
    ),
    AliasRule(
        "pertinent_results",
        "redacted_pertinent_results",
        3,
        standalone_alias(rf"{REDACTION}[ \t]+Results"),
        "pertinent_results",
    ),
    AliasRule(
        "brief_hospital_course",
        "redacted_hospital_course",
        2,
        standalone_alias(rf"{REDACTION}[ \t]+Hospital[ \t]+Course"),
    ),
    AliasRule(
        "brief_hospital_course",
        "hospital_course_fallback",
        3,
        re.compile(
            r"^[ \t]*Hospital[ \t]+Course"
            r"(?:[ \t]+by[ \t]+Problems?)?[ \t]*:?[ \t]*$",
            re.IGNORECASE,
        ),
        "course_fallback",
    ),
    AliasRule(
        "medications_on_admission",
        "redacted_medications_on_admission",
        2,
        standalone_alias(rf"{REDACTION}[ \t]+on[ \t]+Admission"),
    ),
    AliasRule(
        "discharge_medications",
        "redacted_discharge_medications",
        2,
        standalone_alias(rf"{REDACTION}[ \t]+Medications"),
    ),
    AliasRule(
        "discharge_medications",
        "ambiguous_discharge_as_medications",
        3,
        standalone_alias(rf"Discharge[ \t]+{REDACTION}"),
        "discharge_medications",
    ),
    AliasRule(
        "discharge_disposition",
        "redacted_discharge_disposition",
        2,
        standalone_alias(rf"{REDACTION}[ \t]+Disposition"),
    ),
    AliasRule(
        "discharge_disposition",
        "ambiguous_discharge_as_disposition",
        3,
        standalone_alias(rf"Discharge[ \t]+{REDACTION}"),
        "discharge_disposition",
    ),
    AliasRule(
        "discharge_diagnosis",
        "redacted_discharge_diagnosis",
        3,
        standalone_alias(rf"{REDACTION}[ \t]+Diagnosis"),
        "discharge_diagnosis",
    ),
    AliasRule(
        "discharge_condition",
        "redacted_discharge_condition",
        3,
        standalone_alias(rf"{REDACTION}[ \t]+Condition"),
        "discharge_condition",
    ),
)

FOLLOWUP_STANDARD = re.compile(
    r"^[ \t]*Followup[ \t]+Instructions[ \t]*:[ \t]*(?P<content>\S.*)?$",
    re.IGNORECASE,
)
FOLLOWUP_NO_COLON = re.compile(
    r"^[ \t]*Followup[ \t]+Instructions[ \t]*$", re.IGNORECASE
)
REDACTED_INSTRUCTIONS = standalone_alias(
    rf"{REDACTION}[ \t]+Instructions"
)
MANGLED_BRIEF = re.compile(
    rf"{REDACTION}(?P<title>rief[ \t]+Hospital[ \t]+Course"
    rf"(?:[ \t]+Template)?[ \t]*[.:]?)",
    re.IGNORECASE,
)
MANGLED_DISPOSITION = re.compile(
    rf"{REDACTION}(?P<title>ischarge[ \t]+Disposition[ \t]*:)",
    re.IGNORECASE,
)
MANGLED_DIAGNOSIS = re.compile(
    rf"{REDACTION}(?P<title>ischarge[ \t]+Diagnosis[ \t]*:)",
    re.IGNORECASE,
)


def iter_lines(text: str) -> Iterable[LineSpan]:
    offset = 0
    for raw in text.splitlines(keepends=True):
        content = raw.rstrip("\r\n")
        yield LineSpan(offset, offset + len(content), content)
        offset += len(raw)
    if not text or (text[-1] not in "\r\n"):
        return


def line_candidate(
    line: LineSpan,
    match: re.Match[str],
    field: str,
    priority: int,
    label: str,
    kind: str = "normal",
) -> HeadingCandidate:
    content_start: int | None = None
    if "content" in match.re.groupindex and match.group("content") is not None:
        content_start = line.start + match.start("content")
    return HeadingCandidate(
        field=field,
        start=line.start,
        end=content_start if content_start is not None else line.end,
        priority=priority,
        label=label,
        kind=kind,
    )


def collect_candidates(
    text: str,
) -> tuple[
    dict[str, list[HeadingCandidate]],
    list[HeadingCandidate],
    list[HeadingCandidate],
]:
    by_field: dict[str, list[HeadingCandidate]] = defaultdict(list)
    followup: list[HeadingCandidate] = []
    redacted_followup: list[HeadingCandidate] = []

    for line in iter_lines(text):
        for field in SECTION_FIELDS:
            standard = STANDARD_PATTERNS[field].fullmatch(line.content)
            if standard is not None:
                by_field[field].append(
                    line_candidate(line, standard, field, 0, "standard")
                )
                continue
            inline = INLINE_PATTERNS[field].fullmatch(line.content)
            if inline is not None:
                by_field[field].append(
                    line_candidate(
                        line, inline, field, 1, "canonical_inline", "contextual"
                    )
                )
            no_colon = NO_COLON_PATTERNS[field].fullmatch(line.content)
            if no_colon is not None:
                by_field[field].append(
                    line_candidate(
                        line,
                        no_colon,
                        field,
                        1,
                        "canonical_no_colon",
                        "contextual",
                    )
                )

        for rule in ALIAS_RULES:
            match = rule.pattern.fullmatch(line.content)
            if match is not None:
                by_field[rule.field].append(
                    line_candidate(
                        line,
                        match,
                        rule.field,
                        rule.priority,
                        rule.label,
                        rule.kind,
                    )
                )

        match = FOLLOWUP_STANDARD.fullmatch(line.content)
        if match is not None:
            followup.append(
                line_candidate(line, match, "__followup__", 0, "followup_standard")
            )
        else:
            match = FOLLOWUP_NO_COLON.fullmatch(line.content)
            if match is not None:
                followup.append(
                    line_candidate(
                        line, match, "__followup__", 1, "followup_no_colon"
                    )
                )
        match = REDACTED_INSTRUCTIONS.fullmatch(line.content)
        if match is not None:
            redacted_followup.append(
                line_candidate(
                    line,
                    match,
                    "__followup__",
                    2,
                    "redacted_followup_instructions",
                )
            )

    for match in MANGLED_BRIEF.finditer(text):
        by_field["brief_hospital_course"].append(
            HeadingCandidate(
                "brief_hospital_course",
                match.start("title"),
                match.end("title"),
                2,
                "mangled_brief_hospital_course",
                "mangled_brief",
            )
        )
    for match in MANGLED_DISPOSITION.finditer(text):
        by_field["discharge_disposition"].append(
            HeadingCandidate(
                "discharge_disposition",
                match.start("title"),
                match.end("title"),
                3,
                "mangled_discharge_disposition",
                "discharge_disposition",
            )
        )
    for match in MANGLED_DIAGNOSIS.finditer(text):
        by_field["discharge_diagnosis"].append(
            HeadingCandidate(
                "discharge_diagnosis",
                match.start("title"),
                match.end("title"),
                3,
                "mangled_discharge_diagnosis",
                "discharge_diagnosis",
            )
        )

    for field in SECTION_FIELDS:
        unique = {candidate.identity: candidate for candidate in by_field[field]}
        by_field[field] = sorted(
            unique.values(), key=lambda item: (item.priority, item.start, item.end)
        )
    return by_field, followup, redacted_followup


def all_resolved(
    resolved: Mapping[str, Sequence[HeadingCandidate]],
) -> Iterable[tuple[str, HeadingCandidate]]:
    for field, candidates in resolved.items():
        for candidate in candidates:
            yield field, candidate


def nearest_before(
    position: int,
    resolved: Mapping[str, Sequence[HeadingCandidate]],
) -> tuple[str, HeadingCandidate] | None:
    matches = [
        (field, candidate)
        for field, candidate in all_resolved(resolved)
        if candidate.start < position
    ]
    return max(matches, key=lambda item: item[1].start) if matches else None


def nearest_after(
    position: int,
    resolved: Mapping[str, Sequence[HeadingCandidate]],
) -> tuple[str, HeadingCandidate] | None:
    matches = [
        (field, candidate)
        for field, candidate in all_resolved(resolved)
        if candidate.start > position
    ]
    return min(matches, key=lambda item: item[1].start) if matches else None


def has_before(
    fields: Iterable[str],
    position: int,
    resolved: Mapping[str, Sequence[HeadingCandidate]],
) -> bool:
    wanted = set(fields)
    return any(
        field in wanted and candidate.start < position
        for field, candidate in all_resolved(resolved)
    )


def has_after(
    fields: Iterable[str],
    position: int,
    resolved: Mapping[str, Sequence[HeadingCandidate]],
) -> bool:
    wanted = set(fields)
    return any(
        field in wanted and candidate.start > position
        for field, candidate in all_resolved(resolved)
    )


def basic_order_valid(
    candidate: HeadingCandidate,
    resolved: Mapping[str, Sequence[HeadingCandidate]],
) -> bool:
    index = SECTION_INDEX[candidate.field]
    lower = max(
        (
            match.start
            for field, match in all_resolved(resolved)
            if SECTION_INDEX[field] < index
        ),
        default=-1,
    )
    upper = min(
        (
            match.start
            for field, match in all_resolved(resolved)
            if SECTION_INDEX[field] > index
        ),
        default=sys.maxsize,
    )
    return lower < candidate.start < upper


def looks_like_medications_block(value: str) -> bool:
    first = next((line.strip() for line in value.splitlines() if line.strip()), "")
    return bool(
        re.match(r"^(?:\d+\.|RX\b|None\b|No\b.*medications?\b)", first, re.I)
    )


def validate_candidate(
    candidate: HeadingCandidate,
    resolved: Mapping[str, Sequence[HeadingCandidate]],
    text: str,
) -> tuple[bool, str]:
    kind = candidate.kind
    if kind == "contextual":
        # These are explicit complete titles or unambiguous PMH/SH/FH
        # abbreviations.  MIMIC notes occasionally swap Social/Family order or
        # place PMH inside a problem-oriented course, so the closed spelling is
        # safer than imposing the usual global template order here.
        return True, "accepted"
    if kind == "history_social":
        valid = has_before(
            ("past_medical_history",), candidate.start, resolved
        ) and has_after(("family_history",), candidate.start, resolved)
        return (valid, "accepted" if valid else "ambiguous_history_context")
    if kind == "history_family":
        valid = has_before(("social_history",), candidate.start, resolved) and has_after(
            ("physical_exam", "pertinent_results"), candidate.start, resolved
        )
        return (valid, "accepted" if valid else "ambiguous_history_context")
    if kind == "pertinent_results":
        valid = has_before(
            ("physical_exam", "family_history"), candidate.start, resolved
        ) and has_after(
            ("brief_hospital_course", "medications_on_admission"),
            candidate.start,
            resolved,
        )
        return (valid, "accepted" if valid else "pertinent_results_context")
    if kind == "discharge_diagnosis":
        valid = has_before(
            (
                "brief_hospital_course",
                "medications_on_admission",
                "discharge_medications",
                "discharge_disposition",
            ),
            candidate.start,
            resolved,
        ) and has_after(("discharge_condition",), candidate.start, resolved)
        return (valid, "accepted" if valid else "discharge_diagnosis_context")
    if kind == "discharge_condition":
        valid = has_before(
            ("discharge_diagnosis",), candidate.start, resolved
        ) and has_after(("discharge_instructions",), candidate.start, resolved)
        return (valid, "accepted" if valid else "discharge_condition_context")
    if kind == "discharge_medications":
        following = nearest_after(candidate.start, resolved)
        if following is None or following[0] != "discharge_disposition":
            return False, "ambiguous_discharge_next_heading"
        if not has_before(
            ("medications_on_admission", "brief_hospital_course"),
            candidate.start,
            resolved,
        ):
            return False, "ambiguous_discharge_previous_heading"
        block = text[candidate.end : following[1].start]
        if not looks_like_medications_block(block):
            return False, "ambiguous_discharge_not_medications"
        return True, "accepted"
    if kind == "discharge_disposition":
        following = nearest_after(candidate.start, resolved)
        valid = (
            has_before(
                ("brief_hospital_course", "discharge_medications"),
                candidate.start,
                resolved,
            )
            and following is not None
            and following[0] == "discharge_diagnosis"
        )
        return (valid, "accepted" if valid else "discharge_disposition_context")
    if kind == "physical_exam":
        valid = has_before(
            (
                "history_of_present_illness",
                "past_medical_history",
                "social_history",
                "family_history",
            ),
            candidate.start,
            resolved,
        ) and has_after(
            (
                "pertinent_results",
                "brief_hospital_course",
                "medications_on_admission",
            ),
            candidate.start,
            resolved,
        )
        return (valid, "accepted" if valid else "physical_exam_context")
    if not basic_order_valid(candidate, resolved):
        return False, "canonical_order_violation"
    return True, "accepted"


def parse_sections(text: str) -> ParseResult:
    candidates, followup, redacted_followup = collect_candidates(text)
    resolved: dict[str, list[HeadingCandidate]] = {
        field: [candidate for candidate in candidates[field] if candidate.priority == 0]
        for field in SECTION_FIELDS
    }
    standard_fields = {field for field in SECTION_FIELDS if resolved[field]}

    for priority in (1, 2, 3, 4):
        changed = True
        while changed:
            changed = False
            for field in SECTION_FIELDS:
                if resolved[field]:
                    continue
                valid: list[HeadingCandidate] = []
                for candidate in candidates[field]:
                    if candidate.priority != priority:
                        continue
                    accepted, _ = validate_candidate(candidate, resolved, text)
                    if accepted:
                        valid.append(candidate)
                if valid:
                    resolved[field] = sorted(valid, key=lambda item: item.start)
                    changed = True

    accepted_ids = {
        candidate.identity
        for field in SECTION_FIELDS
        for candidate in resolved[field]
    }
    rejected: list[tuple[HeadingCandidate, str]] = []
    for field in SECTION_FIELDS:
        for candidate in candidates[field]:
            if candidate.identity in accepted_ids:
                continue
            if resolved[field] and min(item.priority for item in resolved[field]) < candidate.priority:
                reason = "higher_priority_heading_present"
            elif field in standard_fields:
                reason = "standard_heading_present"
            else:
                accepted, reason = validate_candidate(candidate, resolved, text)
                if accepted:
                    reason = "same_priority_candidate_not_selected"
            rejected.append((candidate, reason))

    discharge_instruction_positions = [
        candidate.start for candidate in resolved["discharge_instructions"]
    ]
    if discharge_instruction_positions:
        last_instruction = max(discharge_instruction_positions)
        followup.extend(
            candidate
            for candidate in redacted_followup
            if candidate.start > last_instruction
        )
    followup = sorted(
        {candidate.identity: candidate for candidate in followup}.values(),
        key=lambda item: item.start,
    )

    boundaries = sorted(
        [
            candidate
            for field in SECTION_FIELDS
            for candidate in resolved[field]
        ]
        + followup,
        key=lambda item: (item.start, item.end),
    )
    sections = {field: "" for field in SECTION_FIELDS}
    for field in SECTION_FIELDS:
        pieces: list[str] = []
        for candidate in resolved[field]:
            next_start = min(
                (
                    boundary.start
                    for boundary in boundaries
                    if boundary.start > candidate.start
                ),
                default=len(text),
            )
            value = text[candidate.end : next_start].strip()
            if value:
                pieces.append(value)
        sections[field] = "\n\n".join(pieces)

    discharge_starts = [
        candidate.start for candidate in resolved["discharge_medications"]
    ]
    return ParseResult(
        sections=sections,
        resolved=resolved,
        standard_fields=standard_fields,
        rejected=rejected,
        discharge_medications_start=min(discharge_starts) if discharge_starts else None,
        followup_boundaries=followup,
    )


def record_key_json(key: RecordKey) -> dict[str, str]:
    return {"subject_id": key[0], "hadm_id": key[1]}


class TextAudit:
    def __init__(self) -> None:
        self.all_records: set[RecordKey] = set()
        self.standard: dict[str, set[RecordKey]] = {
            field: set() for field in SECTION_FIELDS
        }
        self.resolved: dict[str, set[RecordKey]] = {
            field: set() for field in SECTION_FIELDS
        }
        self.empty_output: dict[str, set[RecordKey]] = {
            field: set() for field in SECTION_FIELDS
        }
        self.similar_empty_output: dict[str, set[RecordKey]] = {
            field: set() for field in SECTION_FIELDS
        }
        self.accepted_by_label: dict[
            str, dict[str, set[RecordKey]]
        ] = {
            field: defaultdict(set) for field in SECTION_FIELDS
        }
        self.rejected: dict[str, dict[str, set[RecordKey]]] = defaultdict(
            lambda: defaultdict(set)
        )
        self.mangled_brief: set[RecordKey] = set()
        self.mangled_brief_failed: set[RecordKey] = set()
        self.test_missing_discharge_medications: set[RecordKey] = set()
        self.test_truncated_by_label: dict[str, set[RecordKey]] = defaultdict(set)

    def add(
        self,
        record: SourceRecord,
        parsed: ParseResult,
        is_similar: bool,
        is_test: bool,
    ) -> None:
        key = record.key
        self.all_records.add(key)
        for field in SECTION_FIELDS:
            candidates = parsed.resolved[field]
            if field in parsed.standard_fields:
                self.standard[field].add(key)
            if candidates:
                self.resolved[field].add(key)
            for candidate in candidates:
                self.accepted_by_label[field][candidate.label].add(key)
                if candidate.label == "mangled_brief_hospital_course":
                    self.mangled_brief.add(key)
            if not parsed.sections[field]:
                self.empty_output[field].add(key)
                if is_similar:
                    self.similar_empty_output[field].add(key)
        for candidate, reason in parsed.rejected:
            self.rejected[candidate.label][reason].add(key)
            if candidate.label == "mangled_brief_hospital_course":
                self.mangled_brief.add(key)
        if key in self.mangled_brief and (
            not parsed.resolved["brief_hospital_course"]
            or not parsed.sections["brief_hospital_course"]
        ):
            self.mangled_brief_failed.add(key)
        if is_test:
            if parsed.discharge_medications_start is None:
                self.test_missing_discharge_medications.add(key)
            else:
                labels = {
                    candidate.label
                    for candidate in parsed.resolved["discharge_medications"]
                }
                for label in labels:
                    self.test_truncated_by_label[label].add(key)

    @staticmethod
    def _record_set(values: set[RecordKey]) -> dict[str, Any]:
        ordered = sorted(values)
        return {
            "count": len(ordered),
            "records": [record_key_json(key) for key in ordered],
        }

    def as_json(self, total_rows: int) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        for field in SECTION_FIELDS:
            standard = self.standard[field]
            resolved = self.resolved[field]
            special = resolved - standard
            missing = self.all_records - resolved
            accepted_labels = {
                label: self._record_set(values)
                for label, values in sorted(self.accepted_by_label[field].items())
            }
            fields[field] = {
                "standard_title": self._record_set(standard),
                "special_rule_added": self._record_set(special),
                "resolved_heading": self._record_set(resolved),
                "missing_heading": self._record_set(missing),
                "empty_output": self._record_set(self.empty_output[field]),
                "similar_output_empty": self._record_set(
                    self.similar_empty_output[field]
                ),
                "accepted_by_label": accepted_labels,
            }
        rejected = {
            label: {
                reason: self._record_set(values)
                for reason, values in sorted(reasons.items())
            }
            for label, reasons in sorted(self.rejected.items())
        }
        return {
            "parser_version": TEXT_PARSER_VERSION,
            "fields": fields,
            "rejected_candidates": rejected,
            "mangled_brief_hospital_course": self._record_set(
                self.mangled_brief
            ),
            "mangled_brief_hospital_course_failed": self._record_set(
                self.mangled_brief_failed
            ),
            "test_truncation": {
                "missing_discharge_medications_heading": self._record_set(
                    self.test_missing_discharge_medications
                ),
                "truncated_by_label": {
                    label: self._record_set(values)
                    for label, values in sorted(
                        self.test_truncated_by_label.items()
                    )
                },
            },
        }


def validate_text_audit(
    audit: TextAudit, total_rows: int, baseline_match: bool
) -> list[dict[str, Any]]:
    actual: dict[str, tuple[int, int, int, int]] = {}
    for field in SECTION_FIELDS:
        standard = len(audit.standard[field])
        resolved = len(audit.resolved[field])
        actual[field] = (
            standard,
            resolved - standard,
            resolved,
            total_rows - resolved,
        )

    matches = actual == EXPECTED_TEXT_AUDIT if baseline_match else True
    checks = [
        {
            "name": "documented_text_audit_baseline",
            "passed": matches,
            "expected": EXPECTED_TEXT_AUDIT if baseline_match else None,
            "actual": actual,
        },
        {
            "name": "redacted_history_resolution",
            "passed": (
                len(
                    audit.accepted_by_label["social_history"].get(
                        "redacted_history_as_social", set()
                    )
                )
                == (10 if baseline_match else len(
                    audit.accepted_by_label["social_history"].get(
                        "redacted_history_as_social", set()
                    )
                ))
                and len(
                    audit.accepted_by_label["family_history"].get(
                        "redacted_history_as_family", set()
                    )
                )
                == (16 if baseline_match else len(
                    audit.accepted_by_label["family_history"].get(
                        "redacted_history_as_family", set()
                    )
                ))
            ),
        },
        {
            "name": "mangled_brief_hospital_course_resolution",
            "passed": (
                len(audit.mangled_brief)
                == (26 if baseline_match else len(audit.mangled_brief))
                and not audit.mangled_brief_failed
            ),
            "actual": len(audit.mangled_brief),
            "failed_records": len(audit.mangled_brief_failed),
        },
        {
            "name": "redacted_instructions_not_discharge_instructions",
            "passed": not audit.accepted_by_label["discharge_instructions"].get(
                "redacted_followup_instructions", set()
            ),
        },
    ]
    return checks


def quota_rows(quotas: Mapping[str, GroupQuota]) -> list[dict[str, Any]]:
    return [
        {
            "icd3": quota.icd3,
            "total": quota.total,
            "similar": quota.similar,
            "test": quota.test,
            "test_ratio": quota.test / quota.total,
            "lower": quota.lower,
            "upper": quota.upper,
            "integer_infeasible": quota.integer_infeasible,
        }
        for quota in sorted(quotas.values(), key=lambda item: item.icd3)
    ]


def quota_digest(quotas: Mapping[str, GroupQuota]) -> str:
    payload = [
        {
            "icd3": quota.icd3,
            "total": quota.total,
            "similar": quota.similar,
            "test": quota.test,
        }
        for quota in sorted(quotas.values(), key=lambda item: item.icd3)
    ]
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def create_temp_text(path: Path) -> tuple[Path, TextIO]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    )
    return Path(handle.name), handle


def write_json_temp(path: Path, value: Any) -> Path:
    temp_path, handle = create_temp_text(path)
    try:
        with handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        return temp_path
    except Exception:
        handle.close()
        temp_path.unlink(missing_ok=True)
        raise


def build_outputs(
    records: Sequence[SourceRecord],
    similar_keys: set[RecordKey],
    test_keys: set[RecordKey],
    similar_path: Path,
    test_path: Path,
) -> tuple[Path, Path, TextAudit, dict[str, int]]:
    similar_temp, similar_handle = create_temp_text(similar_path)
    test_temp, test_handle = create_temp_text(test_path)
    audit = TextAudit()
    counts = {"similar": 0, "test": 0}
    try:
        with similar_handle, test_handle:
            similar_writer = csv.DictWriter(
                similar_handle, fieldnames=SIMILAR_FIELDS, lineterminator="\n"
            )
            test_writer = csv.DictWriter(
                test_handle, fieldnames=TEST_FIELDS, lineterminator="\n"
            )
            similar_writer.writeheader()
            test_writer.writeheader()

            ordered = sorted(
                records,
                key=lambda record: (
                    record.icd3,
                    record.score,
                    record.subject_id,
                    record.hadm_id,
                ),
            )
            for record in ordered:
                is_similar = record.key in similar_keys
                is_test = record.key in test_keys
                if is_similar == is_test:
                    raise AssertionError(f"Invalid split membership for {record.key!r}")
                parsed = parse_sections(record.text)
                audit.add(record, parsed, is_similar, is_test)
                if is_similar:
                    row = {
                        "subject_id": record.subject_id,
                        "hadm_id": record.hadm_id,
                        "admittime": record.admittime,
                        "seq_num": record.seq_num,
                        "icd_code": record.icd_code,
                        "icd_version": record.icd_version,
                        **parsed.sections,
                    }
                    similar_writer.writerow(row)
                    counts["similar"] += 1
                else:
                    cutoff = parsed.discharge_medications_start
                    prefix = record.text if cutoff is None else record.text[:cutoff]
                    test_writer.writerow(
                        {
                            "subject_id": record.subject_id,
                            "hadm_id": record.hadm_id,
                            "seq_num": record.seq_num,
                            "icd_code": record.icd_code,
                            "icd_version": record.icd_version,
                            "discharge_text_before_disposition": prefix,
                        }
                    )
                    counts["test"] += 1
        return similar_temp, test_temp, audit, counts
    except Exception:
        similar_handle.close()
        test_handle.close()
        similar_temp.unlink(missing_ok=True)
        test_temp.unlink(missing_ok=True)
        raise


def split_validations(
    records: Sequence[SourceRecord],
    quotas: Mapping[str, GroupQuota],
    similar_keys: set[RecordKey],
    test_keys: set[RecordKey],
    baseline_match: bool,
) -> list[dict[str, Any]]:
    input_keys = {record.key for record in records}
    feasible_ok = all(
        quota.integer_infeasible
        or (15 * quota.total <= 100 * quota.test <= 25 * quota.total)
        for quota in quotas.values()
    )
    infeasible_ok = all(
        not quota.integer_infeasible
        or quota.test == nearest_infeasible_test_count(quota.total)
        for quota in quotas.values()
    )
    digest = quota_digest(quotas)
    return [
        {
            "name": "split_counts",
            "passed": len(similar_keys) + len(test_keys) == len(records),
            "similar": len(similar_keys),
            "test": len(test_keys),
        },
        {
            "name": "split_key_partition",
            "passed": not (similar_keys & test_keys)
            and (similar_keys | test_keys) == input_keys,
        },
        {"name": "feasible_group_ratios", "passed": feasible_ok},
        {"name": "infeasible_group_nearest_integer", "passed": infeasible_ok},
        {
            "name": "documented_quota_baseline",
            "passed": not baseline_match or digest == EXPECTED_QUOTA_SHA256,
            "expected_sha256": EXPECTED_QUOTA_SHA256 if baseline_match else None,
            "actual_sha256": digest,
        },
    ]


def build_quality_report(
    input_path: Path,
    source_sha256: str,
    source_metadata: Mapping[str, Any],
    quotas: Mapping[str, GroupQuota],
    output_counts: Mapping[str, int],
    audit: TextAudit,
    checks: Sequence[Mapping[str, Any]],
    baseline_match: bool,
) -> dict[str, Any]:
    infeasible = sum(quota.integer_infeasible for quota in quotas.values())
    return {
        "report_version": 1,
        "algorithm_version": ALGORITHM_VERSION,
        "input": {
            "file_name": input_path.name,
            "size_bytes": input_path.stat().st_size,
            "sha256": source_sha256,
            "documented_baseline_match": baseline_match,
            **source_metadata,
        },
        "split": {
            "similar_rows": output_counts["similar"],
            "test_rows": output_counts["test"],
            "group_count": len(quotas),
            "integer_feasible_groups": len(quotas) - infeasible,
            "integer_infeasible_groups": infeasible,
            "quotas": quota_rows(quotas),
        },
        "text_audit": audit.as_json(int(source_metadata["rows"])),
        "validation": {
            "all_runtime_checks_passed": all(bool(check["passed"]) for check in checks),
            "documented_baseline_approved": baseline_match,
            "checks": list(checks),
        },
    }


def build_manifest(
    source_sha256: str,
    records: Sequence[SourceRecord],
    quotas: Mapping[str, GroupQuota],
    similar_keys: set[RecordKey],
    test_keys: set[RecordKey],
    similar_path: Path,
    test_path: Path,
    similar_sha256: str,
    test_sha256: str,
) -> dict[str, Any]:
    score_by_key = {record.key: record.score for record in records}
    membership_key = lambda key: (score_by_key[key], key[0], key[1])
    return {
        "manifest_version": 1,
        "algorithm_version": ALGORITHM_VERSION,
        "text_parser_version": TEXT_PARSER_VERSION,
        "source": {
            "sha256": source_sha256,
            "rows": len(records),
        },
        "outputs": {
            "similar": {
                "file_name": similar_path.name,
                "rows": len(similar_keys),
                "sha256": similar_sha256,
                "fields": list(SIMILAR_FIELDS),
            },
            "test": {
                "file_name": test_path.name,
                "rows": len(test_keys),
                "sha256": test_sha256,
                "fields": list(TEST_FIELDS),
            },
        },
        "quota_sha256": quota_digest(quotas),
        "group_quotas": quota_rows(quotas),
        "membership": {
            "similar": [
                record_key_json(key)
                for key in sorted(similar_keys, key=membership_key)
            ],
            "test": [
                record_key_json(key)
                for key in sorted(test_keys, key=membership_key)
            ],
        },
    }


def main() -> int:
    args = parse_args()
    input_path = args.input.resolve()
    similar_path = args.similar_output.resolve()
    test_path = args.test_output.resolve()
    quality_path = args.quality_report.resolve()
    manifest_path = args.manifest.resolve()
    destinations = (similar_path, test_path, quality_path, manifest_path)
    require_distinct_paths(input_path, destinations)
    configure_csv_field_limit()

    source_sha256 = sha256_path(input_path)
    baseline_match = source_sha256 == EXPECTED_SOURCE_SHA256
    if not baseline_match and not args.allow_baseline_mismatch:
        raise ValueError(
            "Source SHA-256 does not match the documented baseline. "
            f"Expected {EXPECTED_SOURCE_SHA256}, found {source_sha256}. "
            "Review/update the design baseline or rerun with "
            "--allow-baseline-mismatch for an explicitly unapproved build."
        )

    records, source_metadata = load_source(input_path)
    if baseline_match:
        baseline_source_ok = (
            input_path.stat().st_size == EXPECTED_SOURCE_SIZE
            and len(records) == EXPECTED_SOURCE_ROWS
        )
        if not baseline_source_ok:
            raise AssertionError("Documented source size/row baseline mismatch")

    quotas = compute_quotas(records)
    similar_keys, test_keys = assign_split(records, quotas)
    checks = split_validations(
        records, quotas, similar_keys, test_keys, baseline_match
    )
    if baseline_match:
        checks.extend(
            [
                {
                    "name": "documented_overall_counts",
                    "passed": len(similar_keys) == EXPECTED_SIMILAR_ROWS
                    and len(test_keys) == EXPECTED_TEST_ROWS,
                },
                {
                    "name": "documented_group_counts",
                    "passed": len(quotas) == EXPECTED_GROUPS
                    and sum(
                        quota.integer_infeasible for quota in quotas.values()
                    )
                    == EXPECTED_INFEASIBLE_GROUPS,
                },
            ]
        )

    csv_temps: list[Path] = []
    json_temps: list[Path] = []
    try:
        similar_temp, test_temp, audit, output_counts = build_outputs(
            records, similar_keys, test_keys, similar_path, test_path
        )
        csv_temps.extend((similar_temp, test_temp))
        checks.extend(validate_text_audit(audit, len(records), baseline_match))
        checks.append(
            {
                "name": "written_output_counts",
                "passed": output_counts
                == {"similar": len(similar_keys), "test": len(test_keys)},
                "actual": output_counts,
            }
        )
        failed = [check["name"] for check in checks if not check["passed"]]
        if failed:
            raise AssertionError(
                "Validation failed before output commit: " + ", ".join(failed)
            )

        similar_sha256 = sha256_path(similar_temp)
        test_sha256 = sha256_path(test_temp)
        quality_report = build_quality_report(
            input_path,
            source_sha256,
            source_metadata,
            quotas,
            output_counts,
            audit,
            checks,
            baseline_match,
        )
        manifest = build_manifest(
            source_sha256,
            records,
            quotas,
            similar_keys,
            test_keys,
            similar_path,
            test_path,
            similar_sha256,
            test_sha256,
        )
        quality_temp = write_json_temp(quality_path, quality_report)
        manifest_temp = write_json_temp(manifest_path, manifest)
        json_temps.extend((quality_temp, manifest_temp))

        for temporary, destination in (
            (similar_temp, similar_path),
            (test_temp, test_path),
            (quality_temp, quality_path),
            (manifest_temp, manifest_path),
        ):
            os.replace(temporary, destination)
        csv_temps.clear()
        json_temps.clear()
    finally:
        for temporary in (*csv_temps, *json_temps):
            temporary.unlink(missing_ok=True)

    print(f"input_rows={len(records)} input_sha256={source_sha256}")
    print(
        f"similar_rows={len(similar_keys)} similar_sha256={similar_sha256} "
        f"output={similar_path}"
    )
    print(
        f"test_rows={len(test_keys)} test_sha256={test_sha256} "
        f"output={test_path}"
    )
    print(f"quality_report={quality_path}")
    print(f"manifest={manifest_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, AssertionError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
