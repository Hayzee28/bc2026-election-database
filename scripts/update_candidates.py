#!/usr/bin/env python3
"""Refresh the BC 2026 local-election candidate dataset from Elections BC.

Safety model:
- The published site stays untouched unless the new PDF parses cleanly.
- Layout/header drift aborts the run.
- Large row-count or identity changes abort the run.
- Duplicate/malformed records abort the run.
- Existing Result/Elected/Result Source/Notes values are preserved for matching candidates.
- Financial-agent service addresses are never written to public output.

Default source:
https://elections.bc.ca/docs/lecfa/Registered-Candidates-LEGE-2026-10-17.pdf
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
import sys
import tempfile
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pdfplumber

SOURCE_URL = (
    "https://elections.bc.ca/docs/lecfa/"
    "Registered-Candidates-LEGE-2026-10-17.pdf"
)

ROOT = Path(__file__).resolve().parents[1]
DATA_JSON = ROOT / "data.json"
DATA_CSV = ROOT / "bc-2026-candidates.csv"
AUDIT_HISTORY = ROOT / "candidate-change-history.json"
SOURCE_STATE = ROOT / "candidate-source-state.json"

# Column starts observed in Elections BC's 2026 register. We deliberately fail closed
# if these headers move materially rather than guessing at a redesigned PDF.
COLUMN_BOUNDS = [20.0, 132.5, 222.5, 362.5, 467.5, 607.5, 780.0]
EXPECTED_HEADER_X = {
    "JURISDICTION": 20.5,
    "OFFICE": 133.0,
    "CANDIDATE": 223.0,
    "AFFILIATION": 363.0,
    "FINANCIAL": 468.0,  # first FINANCIAL header = agent-name column
}
HEADER_X_TOLERANCE = 8.0
EXPECTED_PAGE_WIDTH = 792.0
EXPECTED_PAGE_HEIGHT = 612.0
PAGE_DIM_TOLERANCE = 8.0
LINE_Y_TOLERANCE = 0.9

PUBLIC_FIELDS = (
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
)
PRESERVE_FIELDS = ("result", "elected", "resultSource", "notes")

CSV_FIELDS = [
    "Jurisdiction",
    "Office",
    "Candidate Name",
    "Affiliation",
    "Financial Agent Name",
    "Source Page",
    "Result",
    "Elected",
    "Result Source",
    "Notes",
]


# Some source rows can wrap both the office and a status-only candidate suffix onto
# the next visual line (for example, ``Board of Education`` + ``Trustee`` and
# ``Nat Raedwulf Pogue`` + ``(Withdrawn)``). Without this guard, that continuation
# can look like a new candidate record. Keep this deliberately narrow/fail-closed.
STATUS_ONLY_CANDIDATE_RE = re.compile(r"^\((?:Withdrawn|Acclaimed)\)$", re.IGNORECASE)
OFFICE_WRAP_PREFIX = {
    "Trustee": "Board of Education",
    "Director": "Electoral Area",
    "Commissioner": "Local Community",
}


def is_wrapped_status_continuation(
    current: dict[str, Any] | None,
    jur: str,
    office: str,
    candidate: str,
    affiliation: str,
    agent: str,
) -> bool:
    if current is None or jur or affiliation or agent:
        return False
    prefix = OFFICE_WRAP_PREFIX.get(office)
    if not prefix or not STATUS_ONLY_CANDIDATE_RE.fullmatch(candidate):
        return False
    return norm(current.get("office")) == prefix


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def candidate_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (norm(row.get("jurisdiction")), norm(row.get("office")), norm(row.get("candidate")))


def download_pdf(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "BC-2026-Candidate-Database-Updater/1.0 (+GitHub Actions)"
        },
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        payload = response.read()
    if len(payload) < 100_000 or not payload.startswith(b"%PDF"):
        raise RuntimeError(
            f"Downloaded source does not look like the Elections BC PDF "
            f"({len(payload):,} bytes)."
        )
    return payload


def cluster_lines(page: Any) -> list[dict[str, Any]]:
    words = page.extract_words(
        x_tolerance=1,
        y_tolerance=1,
        keep_blank_chars=False,
    )
    lines: list[dict[str, Any]] = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines and abs(lines[-1]["top"] - word["top"]) <= LINE_Y_TOLERANCE:
            lines[-1]["words"].append(word)
        else:
            lines.append({"top": word["top"], "words": [word]})

    for line in lines:
        line["words"].sort(key=lambda w: w["x0"])
        buckets: list[list[str]] = [[] for _ in range(6)]
        for word in line["words"]:
            x0 = float(word["x0"])
            for idx in range(6):
                if COLUMN_BOUNDS[idx] <= x0 < COLUMN_BOUNDS[idx + 1]:
                    buckets[idx].append(word["text"])
                    break
        line["cells"] = [" ".join(parts) for parts in buckets]
        line["alltext"] = " ".join(w["text"] for w in line["words"])
    return lines


def validate_page_geometry(page: Any, page_no: int) -> None:
    if abs(float(page.width) - EXPECTED_PAGE_WIDTH) > PAGE_DIM_TOLERANCE:
        raise RuntimeError(
            f"Page {page_no}: unexpected width {page.width}; PDF layout may have changed."
        )
    if abs(float(page.height) - EXPECTED_PAGE_HEIGHT) > PAGE_DIM_TOLERANCE:
        raise RuntimeError(
            f"Page {page_no}: unexpected height {page.height}; PDF layout may have changed."
        )


def body_lines(page: Any, page_no: int) -> list[dict[str, Any]]:
    validate_page_geometry(page, page_no)
    lines = cluster_lines(page)

    header_idx = None
    for idx, line in enumerate(lines):
        text = line["alltext"]
        if "JURISDICTION" in text and "OFFICE" in text and "CANDIDATE" in text:
            header_idx = idx
            break
    if header_idx is None:
        raise RuntimeError(f"Page {page_no}: candidate-table header not found.")

    # Verify anchor positions. If Elections BC redesigns the PDF, stop instead of
    # silently assigning words to the wrong columns.
    header_words = lines[header_idx]["words"]
    by_text: dict[str, list[float]] = {}
    for word in header_words:
        by_text.setdefault(word["text"], []).append(float(word["x0"]))

    for label, expected_x in EXPECTED_HEADER_X.items():
        positions = by_text.get(label, [])
        if not positions:
            raise RuntimeError(f"Page {page_no}: missing expected header word {label!r}.")
        # FINANCIAL occurs twice. We only require one occurrence near the first agent column.
        if min(abs(x - expected_x) for x in positions) > HEADER_X_TOLERANCE:
            raise RuntimeError(
                f"Page {page_no}: header {label!r} moved from expected column; "
                "PDF layout may have changed."
            )

    out: list[dict[str, Any]] = []
    for line in lines[header_idx + 1 :]:
        text = line["alltext"]
        if "This information was collected" in text:
            break
        # Defensive fallback in case the legal footer wording changes slightly.
        if line["top"] > 570:
            break
        out.append(line)
    return out


def parse_pdf_words(pdf_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records: list[dict[str, Any]] = []
    current_jurisdiction = ""
    current: dict[str, Any] | None = None

    with pdfplumber.open(str(pdf_path)) as pdf:
        page_count = len(pdf.pages)
        if page_count < 150 or page_count > 260:
            raise RuntimeError(
                f"Unexpected page count {page_count}; refusing to guess after possible source redesign."
            )

        for page_no, page in enumerate(pdf.pages, start=1):
            for line in body_lines(page, page_no):
                jur, office, candidate, affiliation, agent, _address = [
                    norm(value) for value in line["cells"]
                ]
                is_status_continuation = is_wrapped_status_continuation(
                    current, jur, office, candidate, affiliation, agent
                )
                is_record_start = bool(office and candidate) and not is_status_continuation

                if is_record_start:
                    if current is not None:
                        records.append(current)

                    if jur:
                        current_jurisdiction = jur

                    current = {
                        "jurisdiction": current_jurisdiction,
                        "office": office,
                        "candidate": candidate,
                        "affiliation": affiliation,
                        "financialAgent": agent,
                        "sourcePage": page_no,
                        "result": "",
                        "elected": "",
                        "resultSource": "",
                        "notes": "",
                    }
                    continue

                if current is None:
                    # Blank spacer rows before the first record are harmless.
                    if any((jur, office, candidate, affiliation, agent)):
                        raise RuntimeError(
                            f"Page {page_no}: found candidate-table content before first record."
                        )
                    continue

                # Wrapped text belongs to the currently open row. Jurisdiction names can
                # also wrap; when they do, the continuation appears in the jurisdiction column.
                if jur:
                    current_jurisdiction = norm(f"{current_jurisdiction} {jur}")
                    current["jurisdiction"] = current_jurisdiction
                if office:
                    current["office"] = norm(f"{current['office']} {office}")
                if candidate:
                    current["candidate"] = norm(f"{current['candidate']} {candidate}")
                if affiliation:
                    current["affiliation"] = norm(f"{current['affiliation']} {affiliation}")
                if agent:
                    current["financialAgent"] = norm(f"{current['financialAgent']} {agent}")

    if current is not None:
        records.append(current)

    meta = {
        "pageCount": page_count,
        "candidateCount": len(records),
        "jurisdictionCount": len({r["jurisdiction"] for r in records}),
    }
    return records, meta



def parse_pdf_table(pdf_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Independent second extraction pass using pdfplumber's table engine.

    This intentionally uses a different reconstruction path from parse_pdf_words().
    Automatic publication is allowed only when both passes agree exactly on every
    public source field.
    """
    settings = {
        "vertical_strategy": "explicit",
        "explicit_vertical_lines": [20.0, 132.5, 222.5, 362.5, 467.5, 607.5],
        "horizontal_strategy": "text",
        "snap_tolerance": 3,
        "join_tolerance": 3,
        "intersection_tolerance": 5,
        "text_x_tolerance": 1,
        "text_y_tolerance": 1.5,
    }

    records: list[dict[str, Any]] = []
    current_jurisdiction = ""
    current: dict[str, Any] | None = None

    with pdfplumber.open(str(pdf_path)) as pdf:
        page_count = len(pdf.pages)
        for page_no, page in enumerate(pdf.pages, start=1):
            table = page.extract_table(settings)
            if not table:
                raise RuntimeError(f"Cross-check pass: no table found on page {page_no}.")

            header_idx = None
            for idx, row in enumerate(table):
                values = [norm(value) for value in row]
                if (
                    len(values) >= 3
                    and values[0] == "JURISDICTION"
                    and values[1] == "OFFICE"
                    and values[2].startswith("CANDIDATE")
                ):
                    header_idx = idx
                    break
            if header_idx is None:
                raise RuntimeError(
                    f"Cross-check pass: candidate-table header not found on page {page_no}."
                )

            for row in table[header_idx + 1 :]:
                values = [norm(value) for value in row[:5]]
                values += [""] * (5 - len(values))
                jur, office, candidate, affiliation, agent = values

                if jur.startswith("This information was collected"):
                    break
                if jur.startswith("Questions can be directed"):
                    break
                if jur.startswith("September ") or jur.startswith("Page "):
                    break

                is_status_continuation = is_wrapped_status_continuation(
                    current, jur, office, candidate, affiliation, agent
                )
                is_record_start = bool(office and candidate) and not is_status_continuation
                if is_record_start:
                    if current is not None:
                        records.append(current)
                    if jur:
                        current_jurisdiction = jur
                    current = {
                        "jurisdiction": current_jurisdiction,
                        "office": office,
                        "candidate": candidate,
                        "affiliation": affiliation,
                        "financialAgent": agent,
                        "sourcePage": page_no,
                        "result": "",
                        "elected": "",
                        "resultSource": "",
                        "notes": "",
                    }
                    continue

                if current is None:
                    continue
                if jur:
                    current_jurisdiction = norm(f"{current_jurisdiction} {jur}")
                    current["jurisdiction"] = current_jurisdiction
                if office:
                    current["office"] = norm(f"{current['office']} {office}")
                if candidate:
                    current["candidate"] = norm(f"{current['candidate']} {candidate}")
                if affiliation:
                    current["affiliation"] = norm(f"{current['affiliation']} {affiliation}")
                if agent:
                    current["financialAgent"] = norm(f"{current['financialAgent']} {agent}")

        if current is not None:
            records.append(current)

    meta = {
        "pageCount": page_count,
        "candidateCount": len(records),
        "jurisdictionCount": len({r["jurisdiction"] for r in records}),
    }
    return records, meta

def load_existing(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise RuntimeError("Existing data.json is not a JSON array.")
    return raw


def validate_records(
    new_rows: list[dict[str, Any]],
    old_rows: list[dict[str, Any]],
    meta: dict[str, Any],
) -> dict[str, Any]:
    if len(new_rows) < 2_500:
        raise RuntimeError(
            f"Parser produced only {len(new_rows):,} candidates; expected a full provincial register."
        )

    malformed: list[tuple[int, str]] = []
    seen: set[tuple[str, str, str]] = set()
    duplicates: list[tuple[str, str, str]] = []

    for idx, row in enumerate(new_rows, start=1):
        for field in ("jurisdiction", "office", "candidate", "financialAgent"):
            if not norm(row.get(field)):
                malformed.append((idx, field))
        page = row.get("sourcePage")
        if not isinstance(page, int) or page < 1 or page > meta["pageCount"]:
            malformed.append((idx, "sourcePage"))

        key = candidate_key(row)
        if key in seen:
            duplicates.append(key)
        seen.add(key)

        # Public-output privacy guard: fail if a service-address-style field ever sneaks in.
        forbidden_keys = {k.lower() for k in row if "address" in k.lower()}
        if forbidden_keys:
            raise RuntimeError(
                f"Privacy guard tripped: address field(s) present in parsed output: {sorted(forbidden_keys)}"
            )

    if malformed:
        preview = ", ".join(f"row {i}: {f}" for i, f in malformed[:10])
        raise RuntimeError(f"Malformed candidate records detected ({preview}).")
    if duplicates:
        preview = "; ".join(" / ".join(k) for k in duplicates[:5])
        raise RuntimeError(f"Duplicate candidate keys detected: {preview}")

    report: dict[str, Any] = {
        "pageCount": meta["pageCount"],
        "candidateCount": len(new_rows),
        "jurisdictionCount": meta["jurisdictionCount"],
        "duplicateCount": 0,
    }

    if old_rows:
        old_keys = {candidate_key(r) for r in old_rows}
        new_keys = {candidate_key(r) for r in new_rows}
        old_count = len(old_rows)
        ratio = len(new_rows) / old_count if old_count else math.inf
        overlap = len(old_keys & new_keys) / len(old_keys) if old_keys else 1.0

        old_jurisdictions = {norm(r.get("jurisdiction")) for r in old_rows if norm(r.get("jurisdiction"))}
        new_jurisdictions = {r["jurisdiction"] for r in new_rows}
        jur_overlap = (
            len(old_jurisdictions & new_jurisdictions) / len(old_jurisdictions)
            if old_jurisdictions
            else 1.0
        )

        # These are deliberately conservative. A legitimate unusually large change can be
        # reviewed and the threshold adjusted manually; a bad parser must never auto-publish.
        if not 0.85 <= ratio <= 1.15:
            raise RuntimeError(
                f"Candidate count changed too sharply: {old_count:,} -> {len(new_rows):,} "
                f"({(ratio - 1) * 100:+.1f}%). Manual review required."
            )
        if overlap < 0.80:
            raise RuntimeError(
                f"Only {overlap:.1%} of previous candidate identities remain. "
                "Possible PDF/parser break; manual review required."
            )
        if jur_overlap < 0.85:
            raise RuntimeError(
                f"Only {jur_overlap:.1%} of previous jurisdictions remain. "
                "Possible PDF/parser break; manual review required."
            )

        report.update(
            {
                "previousCandidateCount": old_count,
                "candidateDelta": len(new_rows) - old_count,
                "identityOverlap": round(overlap, 6),
                "jurisdictionOverlap": round(jur_overlap, 6),
            }
        )

    return report


def merge_preserved_fields(
    new_rows: list[dict[str, Any]], old_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    old_by_key = {candidate_key(row): row for row in old_rows}
    merged: list[dict[str, Any]] = []
    for row in new_rows:
        old = old_by_key.get(candidate_key(row), {})
        clean = {field: row.get(field, "") for field in PUBLIC_FIELDS}
        for field in PRESERVE_FIELDS:
            clean[field] = old.get(field, clean.get(field, ""))
        merged.append(clean)
    return merged


def write_json(rows: list[dict[str, Any]], path: Path) -> None:
    # Compact JSON keeps Cloudflare transfer/build artifacts small while retaining UTF-8 names.
    text = json.dumps(rows, ensure_ascii=False, separators=(",", ":")) + "\n"
    path.write_text(text, encoding="utf-8")


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    # utf-8-sig preserves the existing Excel-friendly BOM.
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_FIELDS)
        for row in rows:
            writer.writerow(
                [
                    row["jurisdiction"],
                    row["office"],
                    row["candidate"],
                    row["affiliation"],
                    row["financialAgent"],
                    row["sourcePage"],
                    row["result"],
                    row["elected"],
                    row["resultSource"],
                    row["notes"],
                ]
            )


def canonical_source_fields(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "jurisdiction": r["jurisdiction"],
            "office": r["office"],
            "candidate": r["candidate"],
            "affiliation": r["affiliation"],
            "financialAgent": r["financialAgent"],
            "sourcePage": r["sourcePage"],
        }
        for r in rows
    ]



STATUS_SUFFIX_RE = re.compile(r"\s+\((Withdrawn|Acclaimed)\)$", re.IGNORECASE)


def load_optional_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def audit_identity(row: dict[str, Any]) -> tuple[str, str, str]:
    candidate = norm(row.get("candidate"))
    base = STATUS_SUFFIX_RE.sub("", candidate)
    return (norm(row.get("jurisdiction")), norm(row.get("office")), base)


def candidate_status(row: dict[str, Any]) -> str:
    match = STATUS_SUFFIX_RE.search(norm(row.get("candidate")))
    return match.group(1).title() if match else ""


def compact_candidate(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "jurisdiction": norm(row.get("jurisdiction")),
        "office": norm(row.get("office")),
        "candidate": norm(row.get("candidate")),
        "affiliation": norm(row.get("affiliation")),
        "financialAgent": norm(row.get("financialAgent")),
        "sourcePage": row.get("sourcePage"),
    }


def summarize_candidate_changes(
    old_rows: list[dict[str, Any]], new_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    old_map = {audit_identity(row): row for row in old_rows}
    new_map = {audit_identity(row): row for row in new_rows}

    added = [
        compact_candidate(new_map[key])
        for key in sorted(new_map.keys() - old_map.keys())
    ]
    removed = [
        compact_candidate(old_map[key])
        for key in sorted(old_map.keys() - new_map.keys())
    ]

    status_changes: list[dict[str, Any]] = []
    modified: list[dict[str, Any]] = []
    for key in sorted(old_map.keys() & new_map.keys()):
        before = old_map[key]
        after = new_map[key]
        before_status = candidate_status(before)
        after_status = candidate_status(after)
        if before_status != after_status:
            status_changes.append(
                {
                    "jurisdiction": key[0],
                    "office": key[1],
                    "candidate": key[2],
                    "from": before_status or "Active",
                    "to": after_status or "Active",
                }
            )

        watched_fields = ("affiliation", "financialAgent")
        field_changes = {
            field: {"from": norm(before.get(field)), "to": norm(after.get(field))}
            for field in watched_fields
            if norm(before.get(field)) != norm(after.get(field))
        }
        if field_changes:
            modified.append(
                {
                    "jurisdiction": key[0],
                    "office": key[1],
                    "candidate": key[2],
                    "changes": field_changes,
                }
            )

    return {
        "previousCandidateCount": len(old_rows),
        "candidateCount": len(new_rows),
        "netCandidateDelta": len(new_rows) - len(old_rows),
        "added": added,
        "removed": removed,
        "statusChanges": status_changes,
        "modified": modified,
    }


def record_change_audit(
    old_rows: list[dict[str, Any]],
    new_rows: list[dict[str, Any]],
    source_sha256: str,
    meta: dict[str, Any],
) -> bool:
    previous_state = load_optional_json(SOURCE_STATE, {})
    previous_sha = previous_state.get("sourceSha256")
    changes = summarize_candidate_changes(old_rows, new_rows)

    data_changed = any(
        (
            changes["netCandidateDelta"],
            changes["added"],
            changes["removed"],
            changes["statusChanges"],
            changes["modified"],
        )
    )
    source_changed = previous_sha != source_sha256

    if not source_changed and not data_changed:
        return False

    observed_at = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

    history = load_optional_json(AUDIT_HISTORY, [])
    if not isinstance(history, list):
        raise RuntimeError("candidate-change-history.json is not a JSON array.")

    event = {
        "observedAtUtc": observed_at,
        "sourceChanged": source_changed,
        "sourceSha256": source_sha256,
        "previousSourceSha256": previous_sha,
        "pageCount": meta["pageCount"],
        "jurisdictionCount": meta["jurisdictionCount"],
        **changes,
    }
    history.append(event)

    AUDIT_HISTORY.write_text(
        json.dumps(history, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    SOURCE_STATE.write_text(
        json.dumps(
            {
                "observedAtUtc": observed_at,
                "sourceSha256": source_sha256,
                "pageCount": meta["pageCount"],
                "candidateCount": len(new_rows),
                "jurisdictionCount": meta["jurisdictionCount"],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(
        "AUDIT RECORDED | "
        f"source_changed={source_changed} | "
        f"count={len(old_rows):,}->{len(new_rows):,} | "
        f"added={len(changes['added'])} | removed={len(changes['removed'])} | "
        f"status_changes={len(changes['statusChanges'])} | "
        f"modified={len(changes['modified'])}"
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--pdf",
        type=Path,
        help="Parse a local PDF instead of downloading Elections BC's current register.",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Validate the source but do not rewrite data.json/CSV.",
    )
    parser.add_argument(
        "--expect-exact-current",
        action="store_true",
        help="Self-test: require parsed source fields to exactly match existing data.json.",
    )
    args = parser.parse_args()

    old_rows = load_existing(DATA_JSON)

    temp_path: Path | None = None
    try:
        if args.pdf:
            pdf_path = args.pdf.resolve()
            payload = pdf_path.read_bytes()
        else:
            payload = download_pdf(SOURCE_URL)
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(payload)
                temp_path = Path(tmp.name)
            pdf_path = temp_path

        sha256 = hashlib.sha256(payload).hexdigest()
        new_rows, meta = parse_pdf_words(pdf_path)
        cross_rows, cross_meta = parse_pdf_table(pdf_path)
        if meta != cross_meta or canonical_source_fields(new_rows) != canonical_source_fields(cross_rows):
            raise RuntimeError(
                "Independent extraction passes disagree. Automatic publication blocked for manual review."
            )
        report = validate_records(new_rows, old_rows, meta)

        if args.expect_exact_current:
            if canonical_source_fields(new_rows) != canonical_source_fields(old_rows):
                raise RuntimeError(
                    "Self-test failed: parsed source fields do not exactly match the known-good data.json."
                )
            print(
                f"SELF-TEST PASS (2 independent passes): {len(new_rows):,} records across {meta['pageCount']} pages; "
                "all source fields exactly match known-good data.json."
            )
            return 0

        merged = merge_preserved_fields(new_rows, old_rows)
        changed = canonical_source_fields(merged) != canonical_source_fields(old_rows)

        print(
            "VALIDATION PASS | "
            f"pages={report['pageCount']} | candidates={report['candidateCount']:,} | "
            f"jurisdictions={report['jurisdictionCount']} | sha256={sha256[:16]}…"
        )
        if "candidateDelta" in report:
            print(
                "CHANGE CHECK | "
                f"delta={report['candidateDelta']:+d} | "
                f"identity_overlap={report['identityOverlap']:.1%} | "
                f"jurisdiction_overlap={report['jurisdictionOverlap']:.1%}"
            )

        if args.check_only:
            print("CHECK-ONLY: no files written.")
            return 0

        if changed:
            write_json(merged, DATA_JSON)
            write_csv(merged, DATA_CSV)
            print(f"UPDATED: wrote {len(merged):,} validated candidate rows to data.json and CSV.")
        else:
            print("NO DATA CHANGE: source parsed cleanly; website files left untouched.")

        record_change_audit(old_rows, merged, sha256, meta)
        return 0

    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"UPDATE BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(1)
