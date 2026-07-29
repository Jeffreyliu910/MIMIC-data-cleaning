#!/usr/bin/env python3
"""Filter the first-admission dataset by the selected ICD-10 code workbook.

The script reads ``ICD编码（原始）`` from the ``ICD案例统计`` worksheet,
normalizes both source and candidate codes, and preserves every matching CSV
row exactly at the field level and in its original relative order.

Only Python's standard library is required. The XLSX workbook is read as an
Open XML ZIP package; neither source file is modified.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import posixpath
import re
import sys
import tempfile
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Mapping, Sequence
from xml.etree import ElementTree


WORKSHEET_NAME = "ICD案例统计"
CODE_HEADER = "ICD编码（原始）"
VERSION_HEADER = "ICD版本"
EXPECTED_ICD_VERSION = "10"
ICD_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{2,6}$")

SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_REL_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"main": SPREADSHEET_NS, "rel": PACKAGE_REL_NS}


def find_project_root(script_dir: Path) -> Path:
    """Find the nearest ancestor containing both required source files."""
    for candidate in (script_dir, *script_dir.parents):
        if (
            (candidate / "mimic-iv-3.1" / "ICD_code_selected.xlsx").is_file()
            and (candidate / "data_output" / "first_time_seq1_dataset.csv").is_file()
        ):
            return candidate

    return script_dir.parent


def parse_args() -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    project_root = find_project_root(script_dir)
    default_output = (
        project_root
        / "data_output"
        / "first_time_seq1_dataset_icd_selected.csv"
    )

    parser = argparse.ArgumentParser(
        description=(
            "Filter first_time_seq1_dataset.csv by the ICD-10 codes selected "
            "in ICD_code_selected.xlsx."
        )
    )
    parser.add_argument(
        "--icd-xlsx",
        type=Path,
        default=project_root / "mimic-iv-3.1" / "ICD_code_selected.xlsx",
        help="Selected ICD code workbook.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=project_root / "data_output" / "first_time_seq1_dataset.csv",
        help="Input first-admission CSV.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=default_output,
        help=(
            "Filtered CSV destination (default: "
            "<project_root>/data_output/"
            "first_time_seq1_dataset_icd_selected.csv)."
        ),
    )
    parser.add_argument(
        "--qc-output",
        type=Path,
        default=default_output.with_name(
            "first_time_seq1_dataset_icd_selected_qc.json"
        ),
        help="Quality-control JSON destination.",
    )
    return parser.parse_args()


def normalize_icd(value: object) -> str:
    """Normalize case, surrounding whitespace, and ICD display dots."""
    return str(value).strip().upper().replace(".", "")


def require_file(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"Required input file not found: {path}")


def require_columns(
    fieldnames: Sequence[str] | None,
    required: Sequence[str],
    source: Path,
) -> None:
    available = set(fieldnames or ())
    missing = [column for column in required if column not in available]
    if missing:
        raise ValueError(f"{source} is missing required columns: {', '.join(missing)}")


def column_index(cell_reference: str) -> int:
    """Convert an A1 cell reference to a zero-based column index."""
    match = re.match(r"^([A-Z]+)", cell_reference.upper())
    if match is None:
        raise ValueError(f"Invalid XLSX cell reference: {cell_reference!r}")

    result = 0
    for character in match.group(1):
        result = result * 26 + (ord(character) - ord("A") + 1)
    return result - 1


def read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    """Read the optional XLSX shared-string table."""
    try:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []

    strings: list[str] = []
    for item in root.findall("main:si", NS):
        strings.append(
            "".join(
                text.text or ""
                for text in item.findall(".//main:t", NS)
            )
        )
    return strings


def resolve_worksheet_path(
    archive: zipfile.ZipFile,
    worksheet_name: str,
) -> str:
    """Resolve a worksheet name to its XML member within the XLSX archive."""
    try:
        workbook_root = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        relationships_root = ElementTree.fromstring(
            archive.read("xl/_rels/workbook.xml.rels")
        )
    except KeyError as exc:
        raise ValueError("The XLSX package is missing workbook metadata") from exc

    relationship_id: str | None = None
    for sheet in workbook_root.findall("main:sheets/main:sheet", NS):
        if sheet.get("name") == worksheet_name:
            relationship_id = sheet.get(f"{{{OFFICE_REL_NS}}}id")
            break

    if not relationship_id:
        raise ValueError(f"Worksheet not found in workbook: {worksheet_name}")

    target: str | None = None
    for relationship in relationships_root.findall("rel:Relationship", NS):
        if relationship.get("Id") == relationship_id:
            target = relationship.get("Target")
            break

    if not target:
        raise ValueError(
            f"Worksheet relationship is missing for: {worksheet_name}"
        )

    if target.startswith("/"):
        member_path = posixpath.normpath(target.lstrip("/"))
    else:
        member_path = posixpath.normpath(posixpath.join("xl", target))

    if not member_path.startswith("xl/") or member_path not in archive.namelist():
        raise ValueError(f"Invalid worksheet target in XLSX: {target}")
    return member_path


def read_cell_value(
    cell: ElementTree.Element,
    shared_strings: Sequence[str],
) -> str:
    """Return an XLSX cell's stored value as text."""
    cell_type = cell.get("t")
    if cell_type == "inlineStr":
        return "".join(
            text.text or ""
            for text in cell.findall(".//main:is//main:t", NS)
        )

    value_node = cell.find("main:v", NS)
    if value_node is None or value_node.text is None:
        return ""
    stored_value = value_node.text

    if cell_type == "s":
        try:
            return shared_strings[int(stored_value)]
        except (ValueError, IndexError) as exc:
            raise ValueError(
                f"Invalid shared-string index in XLSX: {stored_value}"
            ) from exc
    return stored_value


def iter_worksheet_rows(
    archive: zipfile.ZipFile,
    member_path: str,
    shared_strings: Sequence[str],
) -> Iterable[tuple[int, Dict[int, str]]]:
    """Yield worksheet rows as row number and column-indexed string values."""
    try:
        root = ElementTree.fromstring(archive.read(member_path))
    except KeyError as exc:
        raise ValueError(f"Worksheet XML not found in XLSX: {member_path}") from exc

    for fallback_row_number, row in enumerate(
        root.findall("main:sheetData/main:row", NS),
        start=1,
    ):
        row_number_text = row.get("r")
        row_number = (
            int(row_number_text) if row_number_text else fallback_row_number
        )
        values: Dict[int, str] = {}
        for cell in row.findall("main:c", NS):
            reference = cell.get("r")
            if not reference:
                raise ValueError(
                    f"Cell without a reference in worksheet row {row_number}"
                )
            values[column_index(reference)] = read_cell_value(
                cell,
                shared_strings,
            )
        yield row_number, values


def load_selected_codes(xlsx_path: Path) -> set[str]:
    """Load, normalize, and validate selected ICD-10 codes from the workbook."""
    if not zipfile.is_zipfile(xlsx_path):
        raise ValueError(f"Not a valid XLSX ZIP package: {xlsx_path}")

    with zipfile.ZipFile(xlsx_path) as archive:
        shared_strings = read_shared_strings(archive)
        worksheet_path = resolve_worksheet_path(archive, WORKSHEET_NAME)
        rows = list(iter_worksheet_rows(archive, worksheet_path, shared_strings))

    header_row_number: int | None = None
    code_column: int | None = None
    version_column: int | None = None
    for row_number, values in rows:
        headers = {value.strip(): index for index, value in values.items()}
        if CODE_HEADER in headers:
            header_row_number = row_number
            code_column = headers[CODE_HEADER]
            version_column = headers.get(VERSION_HEADER)
            break

    if header_row_number is None or code_column is None:
        raise ValueError(
            f"Column {CODE_HEADER!r} not found in worksheet {WORKSHEET_NAME!r}"
        )
    if version_column is None:
        raise ValueError(
            f"Column {VERSION_HEADER!r} not found in worksheet {WORKSHEET_NAME!r}"
        )

    selected_codes: dict[str, tuple[str, int]] = {}
    for row_number, values in rows:
        if row_number <= header_row_number:
            continue
        if not any(value.strip() for value in values.values()):
            continue

        raw_code = values.get(code_column, "")
        code = normalize_icd(raw_code)
        if not code:
            raise ValueError(
                f"Blank selected ICD code in {WORKSHEET_NAME!r}, row {row_number}"
            )
        if not ICD_CODE_PATTERN.fullmatch(code):
            raise ValueError(
                f"Invalid selected ICD code {raw_code!r} in row {row_number}"
            )

        version = values.get(version_column, "").strip()
        if version != EXPECTED_ICD_VERSION:
            raise ValueError(
                f"Unexpected ICD version {version!r} in workbook row {row_number}; "
                f"expected {EXPECTED_ICD_VERSION!r}"
            )

        previous = selected_codes.get(code)
        if previous is not None:
            previous_raw, previous_row = previous
            raise ValueError(
                "Duplicate selected ICD code after normalization: "
                f"{previous_raw!r} (row {previous_row}) and "
                f"{raw_code!r} (row {row_number})"
            )
        selected_codes[code] = (raw_code, row_number)

    if not selected_codes:
        raise ValueError(f"No ICD codes found in worksheet {WORKSHEET_NAME!r}")
    return set(selected_codes)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_snapshot(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def create_temp_path(destination: Path, suffix: str) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=suffix,
        dir=destination.parent,
    )
    os.close(descriptor)
    return Path(name)


def filter_csv(
    input_path: Path,
    output_path: Path,
    selected_codes: set[str],
) -> dict[str, object]:
    """Stream-filter the input CSV and atomically replace the destination."""
    temp_path = create_temp_path(output_path, ".tmp")
    input_rows = 0
    matched_rows = 0
    unmatched_rows = 0
    blank_code_rows = 0
    invalid_code_rows = 0
    exact_match_rows = 0
    normalization_only_match_rows = 0
    matched_codes: set[str] = set()
    version_counts: Counter[str] = Counter()
    invalid_code_samples: list[dict[str, object]] = []

    try:
        with (
            input_path.open("r", encoding="utf-8-sig", newline="") as source,
            temp_path.open("w", encoding="utf-8", newline="") as destination,
        ):
            reader = csv.DictReader(source)
            require_columns(
                reader.fieldnames,
                ("icd_code", "icd_version"),
                input_path,
            )
            fieldnames = list(reader.fieldnames or ())
            writer = csv.DictWriter(destination, fieldnames=fieldnames)
            writer.writeheader()

            for line_number, row in enumerate(reader, start=2):
                input_rows += 1
                if None in row or any(row.get(column) is None for column in fieldnames):
                    raise ValueError(
                        f"{input_path} has a field-count mismatch at line "
                        f"{line_number}"
                    )

                version = row["icd_version"].strip()
                version_counts[version] += 1
                if version != EXPECTED_ICD_VERSION:
                    raise ValueError(
                        f"Unexpected ICD version {version!r} at "
                        f"{input_path}:{line_number}; expected "
                        f"{EXPECTED_ICD_VERSION!r}"
                    )

                raw_code = row["icd_code"]
                code = normalize_icd(raw_code)
                if not code:
                    blank_code_rows += 1
                    continue
                if not ICD_CODE_PATTERN.fullmatch(code):
                    invalid_code_rows += 1
                    if len(invalid_code_samples) < 20:
                        invalid_code_samples.append(
                            {"line": line_number, "icd_code": raw_code}
                        )
                    continue

                if code in selected_codes:
                    writer.writerow(row)
                    matched_rows += 1
                    matched_codes.add(code)
                    if raw_code in selected_codes:
                        exact_match_rows += 1
                    else:
                        normalization_only_match_rows += 1
                else:
                    unmatched_rows += 1

        os.replace(temp_path, output_path)
    finally:
        temp_path.unlink(missing_ok=True)

    accounted_rows = (
        matched_rows + unmatched_rows + blank_code_rows + invalid_code_rows
    )
    if accounted_rows != input_rows:
        raise RuntimeError(
            f"Row reconciliation failed: {accounted_rows} != {input_rows}"
        )

    return {
        "input_rows": input_rows,
        "matched_rows": matched_rows,
        "unmatched_rows": unmatched_rows,
        "blank_icd_code_rows": blank_code_rows,
        "invalid_icd_code_rows": invalid_code_rows,
        "invalid_icd_code_samples": invalid_code_samples,
        "exact_match_rows": exact_match_rows,
        "normalization_only_match_rows": normalization_only_match_rows,
        "matched_unique_codes": len(matched_codes),
        "unmatched_selected_codes": sorted(selected_codes - matched_codes),
        "icd_version_counts": dict(sorted(version_counts.items())),
    }


def file_metadata(path: Path) -> dict[str, object]:
    stat = path.stat()
    return {
        "path": str(path),
        "size_bytes": stat.st_size,
        "modified_at_utc": datetime.fromtimestamp(
            stat.st_mtime,
            timezone.utc,
        ).isoformat(),
        "sha256": sha256_file(path),
    }


def write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    temp_path = create_temp_path(path, ".tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def ensure_distinct_paths(paths: Mapping[str, Path]) -> None:
    resolved: dict[Path, str] = {}
    for label, path in paths.items():
        canonical = path.resolve()
        previous_label = resolved.get(canonical)
        if previous_label is not None:
            raise ValueError(
                f"{label} and {previous_label} resolve to the same path: {canonical}"
            )
        resolved[canonical] = label


def main() -> int:
    args = parse_args()
    xlsx_path = args.icd_xlsx.resolve()
    input_path = args.input.resolve()
    output_path = args.output.resolve()
    qc_output_path = args.qc_output.resolve()

    require_file(xlsx_path)
    require_file(input_path)
    ensure_distinct_paths(
        {
            "--icd-xlsx": xlsx_path,
            "--input": input_path,
            "--output": output_path,
            "--qc-output": qc_output_path,
        }
    )

    source_snapshots = {
        xlsx_path: source_snapshot(xlsx_path),
        input_path: source_snapshot(input_path),
    }
    selected_codes = load_selected_codes(xlsx_path)
    metrics = filter_csv(input_path, output_path, selected_codes)

    for source, snapshot in source_snapshots.items():
        if source_snapshot(source) != snapshot:
            raise RuntimeError(f"Source file changed during processing: {source}")

    matched_rows = int(metrics["matched_rows"])
    input_rows = int(metrics["input_rows"])
    matched_unique_codes = int(metrics["matched_unique_codes"])
    review_required = bool(
        metrics["blank_icd_code_rows"] or metrics["invalid_icd_code_rows"]
    )
    qc_report: dict[str, object] = {
        "status": "completed_needs_review" if review_required else "completed",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "matching_rule": (
            "upper(trim(string(icd_code))).replace('.', '') exact set membership"
        ),
        "worksheet": WORKSHEET_NAME,
        "code_column": CODE_HEADER,
        "expected_icd_version": EXPECTED_ICD_VERSION,
        "sources": {
            "icd_xlsx": file_metadata(xlsx_path),
            "input_csv": file_metadata(input_path),
        },
        "output": file_metadata(output_path),
        "metrics": {
            **metrics,
            "selected_unique_codes": len(selected_codes),
            "unmatched_selected_code_count": len(
                metrics["unmatched_selected_codes"]
            ),
            "record_match_rate": matched_rows / input_rows if input_rows else 0.0,
            "selected_code_coverage": (
                matched_unique_codes / len(selected_codes)
                if selected_codes
                else 0.0
            ),
        },
    }
    write_json_atomic(qc_output_path, qc_report)

    print(f"Output: {output_path}")
    print(f"QC report: {qc_output_path}")
    print(f"Selected ICD codes: {len(selected_codes):,}")
    print(f"Input rows: {input_rows:,}")
    print(f"Matched rows: {matched_rows:,}")
    print(f"Unmatched rows: {int(metrics['unmatched_rows']):,}")
    print(f"Matched unique ICD codes: {matched_unique_codes:,}")
    print(
        "Selected ICD codes absent from input: "
        f"{len(metrics['unmatched_selected_codes']):,}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
