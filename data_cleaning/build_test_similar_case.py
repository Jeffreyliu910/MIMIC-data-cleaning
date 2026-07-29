#!/usr/bin/env python3
"""Split similar cases into patient-disjoint retrieval and test CSV files.

The implementation follows ``test_similar_design.md``:

* ``subject_id`` is the indivisible allocation unit;
* the requested 80:20 ratio is measured in admission records;
* sampling is stratified by the first three normalized ICD characters, then by
  age band, sex, and admission type when the secondary stratum is large enough;
* rare full ICD codes remain in the similar-case set by default;
* every full ICD code present in the test set is guaranteed to remain
  represented in the similar-case set (closed-set evaluation);
* ICU use, in-hospital mortality, and note length are reported as balance
  checks, but are not added to either output CSV.

Only the Python standard library is required. The source CSV is read twice when
outputs are created, so long discharge notes do not all have to be held in
memory.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import statistics
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence


ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "gastro/similar_case.csv"
DEFAULT_SIMILAR_OUTPUT = ROOT / "gastro/similar_case_set.csv"
DEFAULT_TEST_OUTPUT = ROOT / "gastro/test_case.csv"
DEFAULT_REPORT = ROOT / "gastro/test_similar_case_quality_report.json"
DEFAULT_PATIENTS = ROOT / "mimic-iv-3.1/hosp/patients.csv"
DEFAULT_ADMISSIONS = ROOT / "mimic-iv-3.1/hosp/admissions.csv"
DEFAULT_ICUSTAYS = ROOT / "mimic-iv-3.1/icu/icustays.csv"

SOURCE_FIELDS = (
    "subject_id",
    "hadm_id",
    "icd_code",
    "icd_version",
    "long_title",
    "discharge_text",
    "discharge_text_before_disposition",
)
SIMILAR_FIELDS = (
    "subject_id",
    "hadm_id",
    "icd_code",
    "icd_version",
    "long_title",
    "discharge_text",
)
TEST_FIELDS = (
    "subject_id",
    "hadm_id",
    "icd_code",
    "icd_version",
    "long_title",
    "discharge_text_before_disposition",
)

UNKNOWN = "UNKNOWN"


@dataclass
class Admission:
    """Lightweight admission metadata; long note text is intentionally omitted."""

    subject_id: str
    hadm_id: str
    icd_prefix: str
    code_key: str
    text_length: int
    source_row_number: int
    age_band: str = UNKNOWN
    gender: str = UNKNOWN
    admission_type: str = UNKNOWN
    has_icu_stay: bool | None = None
    hospital_expired: bool | None = None


@dataclass
class PatientGroup:
    subject_id: str
    admissions: list[Admission]
    primary_prefix: str
    age_band: str
    gender: str
    admission_type: str
    sampling_stratum: tuple[str, str, str, str] | None = None

    @property
    def admission_count(self) -> int:
        return len(self.admissions)

    @property
    def code_counts(self) -> Counter[str]:
        return Counter(admission.code_key for admission in self.admissions)


def configure_csv_field_limit() -> None:
    """Set the largest CSV cell limit accepted by the current interpreter."""
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 10


def normalized_code(value: str) -> str:
    return "".join(str(value).strip().upper().split()).replace(".", "")


def icd_prefix(value: str) -> str:
    code = normalized_code(value)
    return code[:3] if code else UNKNOWN


def full_code_key(version: str, code: str) -> str:
    normalized_version = str(version).strip() or UNKNOWN
    normalized = normalized_code(code) or UNKNOWN
    return f"{normalized_version}:{normalized}"


def stable_token(seed: int, *values: str) -> str:
    payload = "\x1f".join((str(seed), *values)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def row_fingerprint(row: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for field in SOURCE_FIELDS:
        digest.update(field.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(row[field].encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def require_columns(
    reader: csv.DictReader, required: Iterable[str], path: Path
) -> None:
    if reader.fieldnames is None:
        raise ValueError(f"CSV has no header: {path}")
    missing = [field for field in required if field not in reader.fieldnames]
    if missing:
        raise ValueError(
            f"CSV is missing required column(s) {', '.join(missing)}: {path}"
        )


def validate_source_row(
    row: dict[str, str], row_number: int, path: Path
) -> None:
    if None in row:
        raise ValueError(
            f"Row {row_number} has more fields than the header: {path}"
        )
    missing_values = [field for field in SOURCE_FIELDS if row.get(field) is None]
    if missing_values:
        raise ValueError(
            f"Row {row_number} lacks value(s) for "
            f"{', '.join(missing_values)}: {path}"
        )
    empty_ids = [
        field for field in ("subject_id", "hadm_id") if not row[field].strip()
    ]
    if empty_ids:
        raise ValueError(
            f"Row {row_number} has empty identifier(s) "
            f"{', '.join(empty_ids)}: {path}"
        )


def load_source(
    path: Path,
) -> tuple[list[Admission], dict[str, list[Admission]], dict[str, Any]]:
    """Read source metadata and validate admission-level uniqueness."""
    admissions: list[Admission] = []
    by_subject: dict[str, list[Admission]] = defaultdict(list)
    first_fingerprint: dict[tuple[str, str], str] = {}
    first_row_number: dict[tuple[str, str], int] = {}
    duplicate_rows = 0

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        require_columns(reader, SOURCE_FIELDS, path)
        for row_number, row in enumerate(reader, start=2):
            validate_source_row(row, row_number, path)
            key = (row["subject_id"].strip(), row["hadm_id"].strip())
            fingerprint = row_fingerprint(row)
            if key in first_fingerprint:
                duplicate_rows += 1
                if first_fingerprint[key] != fingerprint:
                    raise ValueError(
                        "Conflicting duplicate admission at row "
                        f"{row_number}; first seen at row "
                        f"{first_row_number[key]} for "
                        f"subject_id={key[0]}, hadm_id={key[1]}"
                    )
                continue

            first_fingerprint[key] = fingerprint
            first_row_number[key] = row_number
            admission = Admission(
                subject_id=key[0],
                hadm_id=key[1],
                icd_prefix=icd_prefix(row["icd_code"]),
                code_key=full_code_key(row["icd_version"], row["icd_code"]),
                text_length=len(row["discharge_text"]),
                source_row_number=row_number,
            )
            admissions.append(admission)
            by_subject[admission.subject_id].append(admission)

    if not admissions:
        raise ValueError(f"Source CSV contains no data admissions: {path}")

    source_metrics = {
        "data_rows": len(admissions) + duplicate_rows,
        "unique_admissions": len(admissions),
        "duplicate_identical_rows_skipped": duplicate_rows,
        "subject_count": len(by_subject),
    }
    return admissions, by_subject, source_metrics


def parse_int(value: str, field: str, row_number: int, path: Path) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(
            f"Invalid integer in {field} at row {row_number}: {path}"
        ) from exc


def age_band(age: int | None) -> str:
    if age is None:
        return UNKNOWN
    if age < 18:
        return "00-17"
    if age < 40:
        return "18-39"
    if age < 65:
        return "40-64"
    if age < 80:
        return "65-79"
    return "80+"


def load_patient_metadata(
    path: Path, wanted_subjects: set[str]
) -> dict[str, tuple[str, int, int]]:
    result: dict[str, tuple[str, int, int]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        require_columns(
            reader, ("subject_id", "gender", "anchor_age", "anchor_year"), path
        )
        for row_number, row in enumerate(reader, start=2):
            subject_id = (row["subject_id"] or "").strip()
            if subject_id not in wanted_subjects:
                continue
            if subject_id in result:
                raise ValueError(f"Duplicate subject_id={subject_id} in {path}")
            result[subject_id] = (
                (row["gender"] or "").strip().upper() or UNKNOWN,
                parse_int(row["anchor_age"], "anchor_age", row_number, path),
                parse_int(row["anchor_year"], "anchor_year", row_number, path),
            )
    return result


def load_admission_metadata(
    path: Path, wanted_hadm_ids: set[str]
) -> dict[str, tuple[str, str, int, bool]]:
    result: dict[str, tuple[str, str, int, bool]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        require_columns(
            reader,
            (
                "subject_id",
                "hadm_id",
                "admittime",
                "admission_type",
                "hospital_expire_flag",
            ),
            path,
        )
        for row_number, row in enumerate(reader, start=2):
            hadm_id = (row["hadm_id"] or "").strip()
            if hadm_id not in wanted_hadm_ids:
                continue
            if hadm_id in result:
                raise ValueError(f"Duplicate hadm_id={hadm_id} in {path}")
            admittime = (row["admittime"] or "").strip()
            try:
                admission_year = datetime.fromisoformat(admittime).year
            except ValueError as exc:
                raise ValueError(
                    f"Invalid admittime at row {row_number}: {path}"
                ) from exc
            expire_flag = (row["hospital_expire_flag"] or "").strip()
            if expire_flag not in {"0", "1"}:
                raise ValueError(
                    f"Invalid hospital_expire_flag at row {row_number}: {path}"
                )
            result[hadm_id] = (
                (row["subject_id"] or "").strip(),
                (row["admission_type"] or "").strip().upper() or UNKNOWN,
                admission_year,
                expire_flag == "1",
            )
    return result


def load_icu_hadm_ids(path: Path, wanted_hadm_ids: set[str]) -> set[str]:
    result: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        require_columns(reader, ("hadm_id",), path)
        for row in reader:
            hadm_id = (row["hadm_id"] or "").strip()
            if hadm_id in wanted_hadm_ids:
                result.add(hadm_id)
    return result


def attach_metadata(
    admissions: Sequence[Admission],
    patients_path: Path,
    admissions_path: Path,
    icustays_path: Path,
) -> dict[str, Any]:
    wanted_subjects = {admission.subject_id for admission in admissions}
    wanted_hadm_ids = {admission.hadm_id for admission in admissions}
    patients = load_patient_metadata(patients_path, wanted_subjects)
    admission_metadata = load_admission_metadata(admissions_path, wanted_hadm_ids)
    icu_hadm_ids = load_icu_hadm_ids(icustays_path, wanted_hadm_ids)

    matched_patients = 0
    matched_admissions = 0
    for admission in admissions:
        patient = patients.get(admission.subject_id)
        metadata = admission_metadata.get(admission.hadm_id)
        if patient is not None:
            matched_patients += 1
            admission.gender = patient[0]
        if metadata is None:
            continue
        metadata_subject, admission_type, admission_year, expired = metadata
        if metadata_subject != admission.subject_id:
            raise ValueError(
                "subject_id mismatch for hadm_id="
                f"{admission.hadm_id}: source={admission.subject_id}, "
                f"admissions.csv={metadata_subject}"
            )
        matched_admissions += 1
        admission.admission_type = admission_type
        admission.hospital_expired = expired
        admission.has_icu_stay = admission.hadm_id in icu_hadm_ids
        if patient is not None:
            calculated_age = patient[1] + admission_year - patient[2]
            admission.age_band = age_band(max(0, calculated_age))

    return {
        "patient_metadata_matched_admissions": matched_patients,
        "admission_metadata_matched_admissions": matched_admissions,
        "icu_positive_admissions": sum(
            admission.has_icu_stay is True for admission in admissions
        ),
        "missing_patient_metadata_admissions": len(admissions) - matched_patients,
        "missing_admission_metadata_admissions": len(admissions)
        - matched_admissions,
    }


def deterministic_mode(values: Iterable[str]) -> str:
    counts = Counter(values)
    if not counts:
        return UNKNOWN
    return min(counts, key=lambda value: (-counts[value], value))


def build_patient_groups(
    by_subject: dict[str, list[Admission]]
) -> dict[str, PatientGroup]:
    return {
        subject_id: PatientGroup(
            subject_id=subject_id,
            admissions=subject_admissions,
            primary_prefix=deterministic_mode(
                admission.icd_prefix for admission in subject_admissions
            ),
            age_band=deterministic_mode(
                admission.age_band for admission in subject_admissions
            ),
            gender=deterministic_mode(
                admission.gender for admission in subject_admissions
            ),
            admission_type=deterministic_mode(
                admission.admission_type for admission in subject_admissions
            ),
        )
        for subject_id, subject_admissions in by_subject.items()
    }


def assign_sampling_strata(
    groups: dict[str, PatientGroup], min_secondary_admissions: int
) -> None:
    """Use full secondary strata when stable, otherwise fall back to ICD prefix."""
    full_counts: Counter[tuple[str, str, str, str]] = Counter()
    full_subjects: Counter[tuple[str, str, str, str]] = Counter()
    for group in groups.values():
        full_key = (
            group.primary_prefix,
            group.age_band,
            group.gender,
            group.admission_type,
        )
        full_counts[full_key] += group.admission_count
        full_subjects[full_key] += 1

    for group in groups.values():
        full_key = (
            group.primary_prefix,
            group.age_band,
            group.gender,
            group.admission_type,
        )
        if (
            full_counts[full_key] >= min_secondary_admissions
            and full_subjects[full_key] >= 2
        ):
            group.sampling_stratum = full_key
        else:
            group.sampling_stratum = (
                group.primary_prefix,
                "*",
                "*",
                "*",
            )


def identify_pinned_subjects(
    groups: dict[str, PatientGroup], rare_code_min_admissions: int
) -> tuple[set[str], set[str], Counter[str], dict[str, set[str]]]:
    code_counts: Counter[str] = Counter()
    code_subjects: dict[str, set[str]] = defaultdict(set)
    for group in groups.values():
        for admission in group.admissions:
            code_counts[admission.code_key] += 1
            code_subjects[admission.code_key].add(group.subject_id)

    rare_codes = {
        code
        for code, count in code_counts.items()
        if count < rare_code_min_admissions or len(code_subjects[code]) < 2
    }
    pinned_subjects = {
        subject_id
        for code in rare_codes
        for subject_id in code_subjects[code]
    }
    return pinned_subjects, rare_codes, code_counts, code_subjects


def can_move_to_test(
    group: PatientGroup, similar_code_counts: Counter[str]
) -> bool:
    return all(
        similar_code_counts[code] - count > 0
        for code, count in group.code_counts.items()
    )


def move_to_test(
    group: PatientGroup,
    test_subjects: set[str],
    similar_code_counts: Counter[str],
) -> None:
    test_subjects.add(group.subject_id)
    similar_code_counts.subtract(group.code_counts)


def move_to_similar(
    group: PatientGroup,
    test_subjects: set[str],
    similar_code_counts: Counter[str],
) -> None:
    test_subjects.remove(group.subject_id)
    similar_code_counts.update(group.code_counts)


def stratified_patient_split(
    groups: dict[str, PatientGroup],
    test_ratio: float,
    seed: int,
    pinned_subjects: set[str],
    code_counts: Counter[str],
) -> tuple[set[str], dict[str, Any]]:
    """Greedily approximate row-level stratum targets with whole patients."""
    by_stratum: dict[tuple[str, str, str, str], list[PatientGroup]] = defaultdict(
        list
    )
    for group in groups.values():
        assert group.sampling_stratum is not None
        by_stratum[group.sampling_stratum].append(group)

    test_subjects: set[str] = set()
    similar_code_counts = code_counts.copy()
    stratum_targets: dict[tuple[str, str, str, str], int] = {}
    stratum_test_counts: Counter[tuple[str, str, str, str]] = Counter()

    for stratum in sorted(by_stratum):
        stratum_groups = by_stratum[stratum]
        admission_total = sum(group.admission_count for group in stratum_groups)
        target = round(admission_total * test_ratio)
        stratum_targets[stratum] = target
        candidates = [
            group
            for group in stratum_groups
            if group.subject_id not in pinned_subjects
        ]
        candidates.sort(
            key=lambda group: stable_token(seed, *stratum, group.subject_id)
        )

        current = 0
        for group in candidates:
            before = abs(target - current)
            after = abs(target - (current + group.admission_count))
            if after > before or not can_move_to_test(group, similar_code_counts):
                continue
            move_to_test(group, test_subjects, similar_code_counts)
            current += group.admission_count
            stratum_test_counts[stratum] += group.admission_count

    total_admissions = sum(group.admission_count for group in groups.values())
    target_total = round(total_admissions * test_ratio)
    current_total = sum(groups[subject].admission_count for subject in test_subjects)

    # Rounding each stratum independently can miss the global admission target.
    # Rebalance only when a whole-patient move strictly improves that target.
    while current_total != target_total:
        need_more = current_total < target_total
        candidates = [
            group
            for group in groups.values()
            if (
                group.subject_id not in pinned_subjects
                and (
                    (need_more and group.subject_id not in test_subjects)
                    or (not need_more and group.subject_id in test_subjects)
                )
                and (
                    not need_more
                    or can_move_to_test(group, similar_code_counts)
                )
            )
        ]
        improving = []
        for group in candidates:
            proposed = (
                current_total + group.admission_count
                if need_more
                else current_total - group.admission_count
            )
            if abs(target_total - proposed) >= abs(target_total - current_total):
                continue
            assert group.sampling_stratum is not None
            stratum = group.sampling_stratum
            current_stratum = stratum_test_counts[stratum]
            proposed_stratum = (
                current_stratum + group.admission_count
                if need_more
                else current_stratum - group.admission_count
            )
            stratum_cost = abs(stratum_targets[stratum] - proposed_stratum)
            improving.append(
                (
                    abs(target_total - proposed),
                    stratum_cost,
                    stable_token(seed, "rebalance", group.subject_id),
                    group,
                    proposed,
                    proposed_stratum,
                )
            )
        if not improving:
            break
        _, _, _, selected, proposed_total, proposed_stratum_count = min(
            improving, key=lambda item: item[:3]
        )
        assert selected.sampling_stratum is not None
        if need_more:
            move_to_test(selected, test_subjects, similar_code_counts)
        else:
            move_to_similar(selected, test_subjects, similar_code_counts)
        current_total = proposed_total
        stratum_test_counts[selected.sampling_stratum] = proposed_stratum_count

    split_metrics = {
        "target_test_admissions": target_total,
        "actual_test_admissions": current_total,
        "target_deviation_admissions": current_total - target_total,
        "sampling_stratum_count": len(by_stratum),
        "sampling_strata": {
            "|".join(stratum): {
                "all_admissions": sum(
                    group.admission_count for group in by_stratum[stratum]
                ),
                "target_test_admissions": stratum_targets[stratum],
                "actual_test_admissions": stratum_test_counts[stratum],
            }
            for stratum in sorted(by_stratum)
        },
    }
    return test_subjects, split_metrics


def rate_summary(values: Sequence[bool | None]) -> dict[str, Any]:
    known = [value for value in values if value is not None]
    positive = sum(value is True for value in known)
    return {
        "known_count": len(known),
        "unknown_count": len(values) - len(known),
        "positive_count": positive,
        "rate": None if not known else positive / len(known),
    }


def length_summary(values: Sequence[int]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "mean": None, "median": None, "min": None, "max": None}
    return {
        "count": len(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
    }


def prefix_distribution(admissions: Sequence[Admission]) -> dict[str, Any]:
    counts = Counter(admission.icd_prefix for admission in admissions)
    total = len(admissions)
    return {
        prefix: {
            "count": counts[prefix],
            "proportion": counts[prefix] / total if total else None,
        }
        for prefix in sorted(counts)
    }


def cohort_balance(admissions: Sequence[Admission]) -> dict[str, Any]:
    return {
        "admission_count": len(admissions),
        "icu": rate_summary(
            [admission.has_icu_stay for admission in admissions]
        ),
        "in_hospital_mortality": rate_summary(
            [admission.hospital_expired for admission in admissions]
        ),
        "discharge_text_length_characters": length_summary(
            [admission.text_length for admission in admissions]
        ),
        "icd_prefix_distribution": prefix_distribution(admissions),
    }


def absolute_rate_difference(
    left: dict[str, Any], right: dict[str, Any]
) -> float | None:
    if left["rate"] is None or right["rate"] is None:
        return None
    return abs(left["rate"] - right["rate"])


def build_report(
    input_path: Path,
    similar_output: Path,
    test_output: Path,
    test_ratio: float,
    seed: int,
    rare_code_min_admissions: int,
    min_secondary_admissions: int,
    admissions: list[Admission],
    groups: dict[str, PatientGroup],
    test_subjects: set[str],
    pinned_subjects: set[str],
    rare_codes: set[str],
    source_metrics: dict[str, Any],
    metadata_metrics: dict[str, Any],
    split_metrics: dict[str, Any],
) -> dict[str, Any]:
    test_admissions = [
        admission
        for admission in admissions
        if admission.subject_id in test_subjects
    ]
    similar_admissions = [
        admission
        for admission in admissions
        if admission.subject_id not in test_subjects
    ]
    similar_codes = {admission.code_key for admission in similar_admissions}
    test_codes = {admission.code_key for admission in test_admissions}
    missing_retrieval_codes = sorted(test_codes - similar_codes)

    all_balance = cohort_balance(admissions)
    similar_balance = cohort_balance(similar_admissions)
    test_balance = cohort_balance(test_admissions)
    actual_test_ratio = len(test_admissions) / len(admissions)
    similar_subjects = set(groups) - test_subjects
    checks = {
        "all_admissions_allocated_once": (
            len(similar_admissions) + len(test_admissions) == len(admissions)
        ),
        "subject_sets_are_disjoint": not (similar_subjects & test_subjects),
        "all_subjects_allocated_once": (
            similar_subjects | test_subjects == set(groups)
        ),
        "closed_set_full_icd_codes": not missing_retrieval_codes,
        "rare_code_subjects_kept_in_similar_set": not (
            pinned_subjects & test_subjects
        ),
        "similar_output_headers_exact": True,
        "test_output_headers_exact": True,
    }
    if not all(checks.values()):
        raise RuntimeError(f"Internal split validation failed: {checks}")

    return {
        "generator": Path(__file__).name,
        "input": str(input_path.resolve()),
        "outputs": {
            "similar_case_set": str(similar_output.resolve()),
            "test_case": str(test_output.resolve()),
        },
        "configuration": {
            "requested_test_ratio": test_ratio,
            "seed": seed,
            "rare_code_min_admissions": rare_code_min_admissions,
            "min_secondary_stratum_admissions": min_secondary_admissions,
            "closed_set_key": "icd_version + normalized full icd_code",
            "patient_is_indivisible": True,
        },
        "source": source_metrics,
        "metadata": metadata_metrics,
        "allocation": {
            "similar_subjects": len(similar_subjects),
            "test_subjects": len(test_subjects),
            "similar_admissions": len(similar_admissions),
            "test_admissions": len(test_admissions),
            "actual_similar_ratio": len(similar_admissions) / len(admissions),
            "actual_test_ratio": actual_test_ratio,
            "test_ratio_difference": actual_test_ratio - test_ratio,
            "rare_full_icd_code_count": len(rare_codes),
            "rare_full_icd_codes": sorted(rare_codes),
            "pinned_similar_subject_count": len(pinned_subjects),
            "pinned_similar_admission_count": sum(
                groups[subject_id].admission_count
                for subject_id in pinned_subjects
            ),
            **split_metrics,
        },
        "closed_set": {
            "similar_full_icd_code_count": len(similar_codes),
            "test_full_icd_code_count": len(test_codes),
            "test_codes_missing_from_similar": missing_retrieval_codes,
        },
        "balance": {
            "all": all_balance,
            "similar": similar_balance,
            "test": test_balance,
            "similar_vs_test_absolute_rate_difference": {
                "icu": absolute_rate_difference(
                    similar_balance["icu"], test_balance["icu"]
                ),
                "in_hospital_mortality": absolute_rate_difference(
                    similar_balance["in_hospital_mortality"],
                    test_balance["in_hospital_mortality"],
                ),
            },
        },
        "checks": checks,
    }


def ensure_distinct_paths(paths: Sequence[Path]) -> None:
    resolved = [path.resolve() for path in paths]
    if len(set(resolved)) != len(resolved):
        raise ValueError("Input, outputs, and report paths must all be different")


def temporary_csv(path: Path) -> tuple[Any, Path]:
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
    return handle, Path(handle.name)


def write_outputs(
    input_path: Path,
    similar_output: Path,
    test_output: Path,
    test_subjects: set[str],
    expected_unique_admissions: int,
) -> tuple[int, int]:
    """Re-read source and atomically replace both output CSV files."""
    similar_handle, similar_temp = temporary_csv(similar_output)
    test_handle, test_temp = temporary_csv(test_output)
    seen: set[tuple[str, str]] = set()
    similar_count = 0
    test_count = 0
    try:
        with (
            input_path.open("r", encoding="utf-8-sig", newline="") as source,
            similar_handle,
            test_handle,
        ):
            reader = csv.DictReader(source)
            require_columns(reader, SOURCE_FIELDS, input_path)
            similar_writer = csv.DictWriter(
                similar_handle,
                fieldnames=SIMILAR_FIELDS,
                extrasaction="ignore",
                lineterminator="\n",
            )
            test_writer = csv.DictWriter(
                test_handle,
                fieldnames=TEST_FIELDS,
                extrasaction="ignore",
                lineterminator="\n",
            )
            similar_writer.writeheader()
            test_writer.writeheader()

            for row_number, row in enumerate(reader, start=2):
                validate_source_row(row, row_number, input_path)
                key = (row["subject_id"].strip(), row["hadm_id"].strip())
                if key in seen:
                    continue
                seen.add(key)
                if key[0] in test_subjects:
                    test_writer.writerow({field: row[field] for field in TEST_FIELDS})
                    test_count += 1
                else:
                    similar_writer.writerow(
                        {field: row[field] for field in SIMILAR_FIELDS}
                    )
                    similar_count += 1

        if len(seen) != expected_unique_admissions:
            raise RuntimeError(
                "Source changed between validation and output passes: "
                f"expected {expected_unique_admissions} unique admissions, "
                f"found {len(seen)}"
            )
        os.replace(similar_temp, similar_output)
        os.replace(test_temp, test_output)
    except BaseException:
        similar_handle.close()
        test_handle.close()
        similar_temp.unlink(missing_ok=True)
        test_temp.unlink(missing_ok=True)
        raise
    return similar_count, test_count


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary_path = Path(handle.name)
    os.replace(temporary_path, path)


def ratio(value: str) -> float:
    try:
        result = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not 0.0 < result < 1.0:
        raise argparse.ArgumentTypeError("must be greater than 0 and less than 1")
    return result


def positive_integer(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if result < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument(
        "--similar-output", type=Path, default=DEFAULT_SIMILAR_OUTPUT
    )
    parser.add_argument("--test-output", type=Path, default=DEFAULT_TEST_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--test-ratio",
        type=ratio,
        default=0.20,
        help="Target fraction of admission records in test_case.csv (default: 0.20)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=20260720,
        help="Seed for deterministic within-stratum ordering (default: 20260720)",
    )
    parser.add_argument(
        "--rare-code-min-admissions",
        type=positive_integer,
        default=5,
        help=(
            "Full ICD codes with fewer admissions than this are kept in the "
            "similar set; codes from only one patient are always kept (default: 5)"
        ),
    )
    parser.add_argument(
        "--min-secondary-stratum-admissions",
        type=positive_integer,
        default=10,
        help=(
            "Minimum admission count for age/sex/admission-type subdivision; "
            "smaller groups fall back to ICD-prefix strata (default: 10)"
        ),
    )
    parser.add_argument("--patients", type=Path, default=DEFAULT_PATIENTS)
    parser.add_argument("--admissions", type=Path, default=DEFAULT_ADMISSIONS)
    parser.add_argument("--icustays", type=Path, default=DEFAULT_ICUSTAYS)
    parser.add_argument(
        "--skip-metadata",
        action="store_true",
        help=(
            "Run without MIMIC patients/admissions/icustays metadata; secondary "
            "strata and ICU/mortality checks will be UNKNOWN"
        ),
    )
    parser.add_argument(
        "--force", action="store_true", help="Allow replacement of output files"
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Build and validate the allocation without writing any files",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_csv_field_limit()
    try:
        if not args.input.is_file():
            raise FileNotFoundError(f"Input file does not exist: {args.input}")
        ensure_distinct_paths(
            (args.input, args.similar_output, args.test_output, args.report)
        )
        if not args.validate_only and not args.force:
            existing = [
                str(path)
                for path in (args.similar_output, args.test_output, args.report)
                if path.exists()
            ]
            if existing:
                raise FileExistsError(
                    "Refusing to replace existing output(s); use --force: "
                    + ", ".join(existing)
                )

        admissions, by_subject, source_metrics = load_source(args.input)
        if args.skip_metadata:
            metadata_metrics = {
                "metadata_skipped": True,
                "patient_metadata_matched_admissions": 0,
                "admission_metadata_matched_admissions": 0,
                "icu_positive_admissions": 0,
                "missing_patient_metadata_admissions": len(admissions),
                "missing_admission_metadata_admissions": len(admissions),
            }
        else:
            for path in (args.patients, args.admissions, args.icustays):
                if not path.is_file():
                    raise FileNotFoundError(f"Metadata file does not exist: {path}")
            metadata_metrics = {
                "metadata_skipped": False,
                **attach_metadata(
                    admissions, args.patients, args.admissions, args.icustays
                ),
            }

        groups = build_patient_groups(by_subject)
        assign_sampling_strata(groups, args.min_secondary_stratum_admissions)
        pinned, rare_codes, code_counts, _ = identify_pinned_subjects(
            groups, args.rare_code_min_admissions
        )
        test_subjects, split_metrics = stratified_patient_split(
            groups,
            args.test_ratio,
            args.seed,
            pinned,
            code_counts,
        )
        report = build_report(
            args.input,
            args.similar_output,
            args.test_output,
            args.test_ratio,
            args.seed,
            args.rare_code_min_admissions,
            args.min_secondary_stratum_admissions,
            admissions,
            groups,
            test_subjects,
            pinned,
            rare_codes,
            source_metrics,
            metadata_metrics,
            split_metrics,
        )

        if args.validate_only:
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
            return 0

        similar_count, test_count = write_outputs(
            args.input,
            args.similar_output,
            args.test_output,
            test_subjects,
            len(admissions),
        )
        if (
            similar_count != report["allocation"]["similar_admissions"]
            or test_count != report["allocation"]["test_admissions"]
        ):
            raise RuntimeError("Written output counts differ from validated allocation")
        write_json_atomic(args.report, report)
    except (
        OSError,
        UnicodeError,
        csv.Error,
        ValueError,
        RuntimeError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(
        f"Wrote {similar_count:,} admissions to {args.similar_output} "
        f"and {test_count:,} admissions to {args.test_output}."
    )
    print(
        "Actual split: "
        f"{report['allocation']['actual_similar_ratio']:.2%} similar / "
        f"{report['allocation']['actual_test_ratio']:.2%} test; "
        f"{len(test_subjects):,} test subjects; no subject leakage."
    )
    print(
        "Closed-set validation passed; quality report: "
        f"{args.report}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
