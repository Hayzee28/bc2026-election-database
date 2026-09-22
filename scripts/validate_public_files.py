#!/usr/bin/env python3
"""Final consistency check for public site data files."""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JSON_PATH = ROOT / "data.json"
CSV_PATH = ROOT / "bc-2026-candidates.csv"


def main() -> None:
    rows = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise SystemExit("VALIDATION FAILED: data.json is empty or not an array.")

    required = {
        "jurisdiction",
        "office",
        "candidate",
        "affiliation",
        "financialAgent",
        "sourcePage",
        "result",
        "elected",
        "resultSource",
        "notes",
    }
    keys_seen = set()
    for i, row in enumerate(rows, start=1):
        if set(row) != required:
            raise SystemExit(f"VALIDATION FAILED: JSON schema mismatch at row {i}.")
        if any("address" in key.lower() for key in row):
            raise SystemExit("VALIDATION FAILED: address field present in public JSON.")
        if not row["jurisdiction"] or not row["office"] or not row["candidate"] or not row["financialAgent"]:
            raise SystemExit(f"VALIDATION FAILED: missing required value at JSON row {i}.")
        key = (row["jurisdiction"], row["office"], row["candidate"])
        if key in keys_seen:
            raise SystemExit(f"VALIDATION FAILED: duplicate candidate key {key!r}.")
        keys_seen.add(key)

    with CSV_PATH.open(encoding="utf-8-sig", newline="") as handle:
        csv_rows = list(csv.DictReader(handle))

    if len(csv_rows) != len(rows):
        raise SystemExit(
            f"VALIDATION FAILED: JSON has {len(rows)} rows but CSV has {len(csv_rows)}."
        )
    if csv_rows and any("address" in key.lower() for key in csv_rows[0]):
        raise SystemExit("VALIDATION FAILED: address field present in public CSV.")

    mapping = [
        ("Jurisdiction", "jurisdiction"),
        ("Office", "office"),
        ("Candidate Name", "candidate"),
        ("Affiliation", "affiliation"),
        ("Financial Agent Name", "financialAgent"),
        ("Source Page", "sourcePage"),
        ("Result", "result"),
        ("Elected", "elected"),
        ("Result Source", "resultSource"),
        ("Notes", "notes"),
    ]
    for i, (jrow, crow) in enumerate(zip(rows, csv_rows), start=1):
        for csv_key, json_key in mapping:
            left = str(crow.get(csv_key, ""))
            right = str(jrow.get(json_key, ""))
            if left != right:
                raise SystemExit(
                    f"VALIDATION FAILED: JSON/CSV mismatch at row {i}, field {json_key}."
                )

    print(f"PUBLIC FILE VALIDATION PASS: {len(rows):,} rows; JSON and CSV agree; no address fields.")


if __name__ == "__main__":
    main()
