#!/usr/bin/env python3
"""Attach ICD diagnosis titles and discharge-note text to the selected dataset.

Rows are matched on both ``subject_id`` and ``hadm_id``. This is an inner
join: an input row is omitted when no matching discharge note exists or when
every matching note has an empty ``text`` field. If an admission has multiple
non-empty discharge notes, one output row is written for each matching note.

``long_title`` is looked up in ``d_icd_diagnoses.csv`` using both
``icd_code`` and ``icd_version``. An unmatched diagnosis is retained with an
empty ``long_title`` and included in the summary printed after processing.

Only Python's standard library is required. The discharge file is processed
as a stream so the full multi-gigabyte file is never loaded into memory.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import os
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import DefaultDict, Dict, Iterator, Optional, Sequence, TextIO, Tuple


KEY_COLUMNS = ("subject_id", "hadm_id")
ICD_KEY_COLUMNS = ("icd_code", "icd_version")
LONG_TITLE_COLUMN = "long_title"
DISCHARGE_TEXT_COLUMN = "text"

AdmissionKey = Tuple[str, str]
IcdKey = Tuple[str, str]
CsvRow = Dict[str, str]


def find_project_root(script_dir: Path) -> Path:
    """Find the nearest ancestor containing the expected input locations."""
    for candidate in (script_dir, *script_dir.parents):
        selected_path = (
            candidate
            / "data_output"
            / "first_time_seq1_dataset_icd_selected.csv"
        )
        note_dir = candidate / "mimic-iv-3.1" / "note"
        if selected_path.is_file() and (
            (note_dir / "discharge.csv").is_file()
            or (note_dir / "discharge.csv.gz").is_file()
        ):
            return candidate

    # Keep fallback paths useful if an input is missing.
    return script_dir.parent


def default_discharge_path(project_root: Path) -> Path:
    """Prefer the uncompressed discharge CSV and otherwise use CSV.GZ."""
    note_dir = project_root / "mimic-iv-3.1" / "note"
    csv_path = note_dir / "discharge.csv"
    if csv_path.is_file():
        return csv_path
    return note_dir / "discharge.csv.gz"


def parse_args() -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    project_root = find_project_root(script_dir)

    parser = argparse.ArgumentParser(
        description=(
            "Match the selected first-admission dataset to discharge notes "
            "by subject_id and hadm_id, excluding missing or empty text."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=(
            project_root
            / "data_output"
            / "first_time_seq1_dataset_icd_selected.csv"
        ),
        help="Selected first-admission CSV.",
    )
    parser.add_argument(
        "--discharge",
        type=Path,
        default=default_discharge_path(project_root),
        help="discharge.csv or discharge.csv.gz.",
    )
    parser.add_argument(
        "--diagnoses",
        type=Path,
        default=project_root / "mimic-iv-3.1" / "hosp" / "d_icd_diagnoses.csv",
        help="ICD diagnosis dictionary CSV containing long_title.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=(
            project_root
            / "data_output"
            / "first_time_seq1_dataset_icd_selected_with_discharge.csv"
        ),
        help=(
            "Joined CSV destination (default: <project_root>/data_output/"
            "first_time_seq1_dataset_icd_selected_with_discharge.csv)."
        ),
    )
    return parser.parse_args()


def set_max_csv_field_size() -> None:
    """Allow discharge notes larger than Python's default CSV field limit."""
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 10


def require_file(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"Required input file not found: {path}")


def require_columns(
    fieldnames: Optional[Sequence[str]],
    required: Sequence[str],
    source: Path,
) -> None:
    available = set(fieldnames or ())
    missing = [column for column in required if column not in available]
    if missing:
        raise ValueError(f"{source} is missing required columns: {', '.join(missing)}")


def admission_key(row: CsvRow) -> AdmissionKey:
    """Return a whitespace-normalized (subject_id, hadm_id) join key."""
    return row["subject_id"].strip(), row["hadm_id"].strip()


def icd_key(row: CsvRow) -> IcdKey:
    """Return a whitespace-normalized (icd_code, icd_version) lookup key."""
    return row["icd_code"].strip(), row["icd_version"].strip()


def open_csv(path: Path) -> TextIO:
    """Open a plain or gzip-compressed CSV as UTF-8 text."""
    if path.suffix.lower() == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", newline="")
    return path.open("r", encoding="utf-8", newline="")


def load_icd_titles(diagnoses_path: Path) -> Dict[IcdKey, str]:
    """Load long diagnosis titles keyed by both ICD code and version."""
    titles: Dict[IcdKey, str] = {}

    with open_csv(diagnoses_path) as handle:
        reader = csv.DictReader(handle)
        require_columns(
            reader.fieldnames,
            (*ICD_KEY_COLUMNS, LONG_TITLE_COLUMN),
            diagnoses_path,
        )

        for row_number, row in enumerate(reader, start=2):
            key = icd_key(row)
            if not all(key):
                raise ValueError(
                    f"{diagnoses_path} has a blank icd_code or icd_version "
                    f"at CSV record {row_number}"
                )

            title = row[LONG_TITLE_COLUMN]
            title = title.strip() if title is not None else ""
            existing = titles.get(key)
            if existing is not None and existing != title:
                raise ValueError(
                    f"{diagnoses_path} contains conflicting long_title values "
                    f"for icd_code={key[0]!r}, icd_version={key[1]!r}"
                )
            titles[key] = title

    return titles


def load_input_rows(
    input_path: Path,
    icd_titles: Dict[IcdKey, str],
) -> tuple[list[str], DefaultDict[AdmissionKey, list[CsvRow]], int, int]:
    """Load the comparatively small selected dataset, grouped by join key."""
    rows_by_key: DefaultDict[AdmissionKey, list[CsvRow]] = defaultdict(list)
    input_rows = 0
    unmatched_icd_rows = 0

    with open_csv(input_path) as handle:
        reader = csv.DictReader(handle)
        require_columns(
            reader.fieldnames,
            (*KEY_COLUMNS, *ICD_KEY_COLUMNS),
            input_path,
        )
        fieldnames = list(reader.fieldnames or ())
        generated_columns = [
            column
            for column in (LONG_TITLE_COLUMN, DISCHARGE_TEXT_COLUMN)
            if column in fieldnames
        ]
        if generated_columns:
            raise ValueError(
                f"{input_path} already contains generated column(s): "
                f"{', '.join(generated_columns)}"
            )

        for row_number, row in enumerate(reader, start=2):
            key = admission_key(row)
            if not all(key):
                raise ValueError(
                    f"{input_path} has a blank subject_id or hadm_id "
                    f"at CSV record {row_number}"
                )
            diagnosis_key = icd_key(row)
            if not all(diagnosis_key):
                raise ValueError(
                    f"{input_path} has a blank icd_code or icd_version "
                    f"at CSV record {row_number}"
                )
            long_title = icd_titles.get(diagnosis_key)
            if long_title is None:
                long_title = ""
                unmatched_icd_rows += 1
            row[LONG_TITLE_COLUMN] = long_title
            rows_by_key[key].append(row)
            input_rows += 1

    return (
        [*fieldnames, LONG_TITLE_COLUMN],
        rows_by_key,
        input_rows,
        unmatched_icd_rows,
    )


def iter_joined_rows(
    discharge_path: Path,
    rows_by_key: DefaultDict[AdmissionKey, list[CsvRow]],
    matched_keys: set[AdmissionKey],
    counters: Dict[str, int],
) -> Iterator[CsvRow]:
    """Stream discharge notes and yield every non-empty inner-join result."""
    with open_csv(discharge_path) as handle:
        reader = csv.DictReader(handle)
        require_columns(
            reader.fieldnames,
            (*KEY_COLUMNS, DISCHARGE_TEXT_COLUMN),
            discharge_path,
        )

        for discharge_row in reader:
            counters["discharge_rows_scanned"] += 1
            key = admission_key(discharge_row)
            input_matches = rows_by_key.get(key)
            if not input_matches:
                continue

            text = discharge_row[DISCHARGE_TEXT_COLUMN]
            if text is None or not text.strip():
                counters["matching_notes_with_empty_text"] += 1
                continue

            matched_keys.add(key)
            counters["nonempty_matching_notes"] += 1
            for input_row in input_matches:
                yield {**input_row, DISCHARGE_TEXT_COLUMN: text}


def write_joined_dataset(
    output_path: Path,
    input_fieldnames: list[str],
    rows_by_key: DefaultDict[AdmissionKey, list[CsvRow]],
    discharge_path: Path,
) -> tuple[int, set[AdmissionKey], Dict[str, int]]:
    """Write the joined CSV atomically and return processing statistics."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_fieldnames = [*input_fieldnames, DISCHARGE_TEXT_COLUMN]
    matched_keys: set[AdmissionKey] = set()
    counters = {
        "discharge_rows_scanned": 0,
        "matching_notes_with_empty_text": 0,
        "nonempty_matching_notes": 0,
    }
    output_rows = 0

    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            dir=output_path.parent,
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=output_fieldnames)
            writer.writeheader()
            for row in iter_joined_rows(
                discharge_path,
                rows_by_key,
                matched_keys,
                counters,
            ):
                writer.writerow(row)
                output_rows += 1

        os.replace(temp_path, output_path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

    return output_rows, matched_keys, counters


def main() -> int:
    args = parse_args()
    input_path = args.input.resolve()
    discharge_path = args.discharge.resolve()
    diagnoses_path = args.diagnoses.resolve()
    output_path = args.output.resolve()

    require_file(input_path)
    require_file(discharge_path)
    require_file(diagnoses_path)
    if output_path in {input_path, discharge_path, diagnoses_path}:
        raise ValueError("Output path must differ from all input paths")

    set_max_csv_field_size()
    icd_titles = load_icd_titles(diagnoses_path)
    (
        input_fieldnames,
        rows_by_key,
        input_rows,
        unmatched_icd_rows,
    ) = load_input_rows(input_path, icd_titles)
    output_rows, matched_keys, counters = write_joined_dataset(
        output_path,
        input_fieldnames,
        rows_by_key,
        discharge_path,
    )

    matched_input_rows = sum(len(rows_by_key[key]) for key in matched_keys)
    excluded_input_rows = input_rows - matched_input_rows

    print(f"Output: {output_path}")
    print(f"Input rows: {input_rows:,}")
    print(f"ICD dictionary entries loaded: {len(icd_titles):,}")
    print(f"Input rows without a matching long_title: {unmatched_icd_rows:,}")
    print(f"Discharge rows scanned: {counters['discharge_rows_scanned']:,}")
    print(f"Input rows with non-empty discharge text: {matched_input_rows:,}")
    print(f"Input rows excluded: {excluded_input_rows:,}")
    print(
        "Matching discharge notes skipped for empty text: "
        f"{counters['matching_notes_with_empty_text']:,}"
    )
    print(f"Output rows written: {output_rows:,}")
    if output_rows > matched_input_rows:
        print(
            "Additional rows from admissions with multiple discharge notes: "
            f"{output_rows - matched_input_rows:,}"
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeError, ValueError, csv.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
