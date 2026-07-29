#!/usr/bin/env python3
"""Build the first-admission, sequence-1 ICD-10 diagnosis dataset.

This script implements the following SQL semantics:

1. Rank admissions within each subject by ``admittime, hadm_id``.
2. Keep the first admission for each subject.
3. INNER JOIN diagnoses where ``seq_num = 1`` and ``icd_version = 10``.

Patients whose first admission has only an ICD-9 primary diagnosis or no
diagnosis record are excluded from the output.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import os
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Sequence, Tuple


OUTPUT_COLUMNS = (
    "subject_id",
    "hadm_id",
    "admittime",
    "seq_num",
    "icd_code",
    "icd_version",
)

Admission = Tuple[str, int, str]
Diagnosis = Tuple[str, str, str]


def find_default_data_dir(script_dir: Path) -> Path:
    """Find mimic-iv-3.1/hosp in the script directory or an ancestor."""
    for base_dir in (script_dir, *script_dir.parents):
        candidate = base_dir / "mimic-iv-3.1" / "hosp"
        if (
            (candidate / "admissions.csv.gz").is_file()
            and (candidate / "diagnoses_icd.csv.gz").is_file()
        ):
            return candidate

    # Preserve a useful expected path for the later missing-file error message.
    return script_dir / "mimic-iv-3.1" / "hosp"


def parse_args() -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    default_data_dir = find_default_data_dir(script_dir)
    default_output_path = (
        default_data_dir.parent.parent
        / "data_output"
        / "first_time_seq1_dataset.csv"
    )
    parser = argparse.ArgumentParser(
        description=(
            "Extract each patient's first hospital admission and its seq_num=1 "
            "ICD-10 diagnosis from MIMIC-IV."
        )
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=default_data_dir,
        help="Directory containing admissions.csv.gz and diagnoses_icd.csv.gz.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=default_output_path,
        help=(
            "Destination CSV path (default: "
            "<project_root>/data_output/first_time_seq1_dataset.csv)."
        ),
    )
    return parser.parse_args()


def require_columns(
    fieldnames: Sequence[str] | None,
    required: Sequence[str],
    source: Path,
) -> None:
    available = set(fieldnames or ())
    missing = [column for column in required if column not in available]
    if missing:
        raise ValueError(f"{source} is missing required columns: {', '.join(missing)}")


def load_first_admissions(admissions_path: Path) -> Dict[str, Admission]:
    """Return subject_id -> (admittime, numeric hadm_id, original hadm_id)."""
    first_admissions: Dict[str, Admission] = {}

    with gzip.open(admissions_path, "rt", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        require_columns(
            reader.fieldnames,
            ("subject_id", "hadm_id", "admittime"),
            admissions_path,
        )

        for row in reader:
            subject_id = row["subject_id"]
            hadm_id = row["hadm_id"]
            admittime = row["admittime"]
            if not subject_id or not hadm_id or not admittime:
                raise ValueError(
                    f"{admissions_path} contains an admission with a blank "
                    "subject_id, hadm_id, or admittime"
                )

            candidate: Admission = (admittime, int(hadm_id), hadm_id)
            current = first_admissions.get(subject_id)
            if current is None or candidate[:2] < current[:2]:
                first_admissions[subject_id] = candidate

    return first_admissions


def load_matching_diagnoses(
    diagnoses_path: Path,
    first_admission_keys: set[Tuple[str, str]],
) -> Dict[Tuple[str, str], List[Diagnosis]]:
    """Return matches keyed by (subject_id, first-admission hadm_id)."""
    diagnoses: Dict[Tuple[str, str], List[Diagnosis]] = {}

    with gzip.open(diagnoses_path, "rt", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        require_columns(
            reader.fieldnames,
            ("subject_id", "hadm_id", "seq_num", "icd_code", "icd_version"),
            diagnoses_path,
        )

        for row in reader:
            admission_key = (row["subject_id"], row["hadm_id"])
            if (
                admission_key in first_admission_keys
                and row["seq_num"] == "1"
                and row["icd_version"] == "10"
                and row["icd_code"].strip()
            ):
                diagnoses.setdefault(admission_key, []).append(
                    (row["seq_num"], row["icd_code"], row["icd_version"])
                )

    return diagnoses


def write_dataset(
    output_path: Path,
    first_admissions: Dict[str, Admission],
    diagnoses: Dict[Tuple[str, str], List[Diagnosis]],
) -> Tuple[int, int, int]:
    """Write matched rows and return row/matched/excluded patient counts."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_rows = 0
    matched_patients = 0
    excluded_patients = 0

    temp_path: Path | None = None
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
            writer = csv.writer(handle)
            writer.writerow(OUTPUT_COLUMNS)

            for subject_id in sorted(first_admissions, key=int):
                admittime, _, hadm_id = first_admissions[subject_id]
                matches = diagnoses.get((subject_id, hadm_id))
                if matches:
                    matched_patients += 1
                    for seq_num, icd_code, icd_version in matches:
                        writer.writerow(
                            (
                                subject_id,
                                hadm_id,
                                admittime,
                                seq_num,
                                icd_code,
                                icd_version,
                            )
                        )
                        output_rows += 1
                else:
                    excluded_patients += 1

        os.replace(temp_path, output_path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

    return output_rows, matched_patients, excluded_patients


def main() -> int:
    args = parse_args()
    data_dir = args.data_dir.resolve()
    output_path = args.output.resolve()
    admissions_path = data_dir / "admissions.csv.gz"
    diagnoses_path = data_dir / "diagnoses_icd.csv.gz"

    for source in (admissions_path, diagnoses_path):
        if not source.is_file():
            raise FileNotFoundError(f"Required input file not found: {source}")

    first_admissions = load_first_admissions(admissions_path)
    first_admission_keys = {
        (subject_id, admission[2])
        for subject_id, admission in first_admissions.items()
    }
    diagnoses = load_matching_diagnoses(diagnoses_path, first_admission_keys)
    output_rows, matched_patients, excluded_patients = write_dataset(
        output_path,
        first_admissions,
        diagnoses,
    )

    print(f"Output: {output_path}")
    print(f"Patients with admissions: {len(first_admissions):,}")
    print(f"Patients with an ICD-10 seq_num=1 match: {matched_patients:,}")
    print(f"Patients excluded without an ICD-10 seq_num=1 match: {excluded_patients:,}")
    print(f"Data rows written: {output_rows:,}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
