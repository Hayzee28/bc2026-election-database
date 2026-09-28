#!/usr/bin/env python3
"""Build one privacy-safe event only when new candidate changes need review."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any


def build_event(previous: list[dict[str, Any]], current: list[dict[str, Any]]):
    if len(current) <= len(previous):
        return None

    new_events = current[len(previous):]
    counts = {
        "addedCount": sum(len(event.get("added", [])) for event in new_events),
        "removedCount": sum(len(event.get("removed", [])) for event in new_events),
        "statusChangeCount": sum(len(event.get("statusChanges", [])) for event in new_events),
    }
    actionable_count = sum(counts.values())
    if actionable_count == 0:
        return None

    latest = new_events[-1]
    return {
        "eventType": "candidate_attention_needed",
        "observedAt": latest.get("observedAtUtc", ""),
        **counts,
        "actionableCount": actionable_count,
        "safeUrl": "https://bc2026-election-database.hayzee28.workers.dev/changelog.html",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--previous", required=True, type=Path)
    parser.add_argument("--current", required=True, type=Path)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    previous = json.loads(args.previous.read_text(encoding="utf-8"))
    current = json.loads(args.current.read_text(encoding="utf-8"))
    event = build_event(previous, current)
    output_path = args.github_output or (
        Path(os.environ["GITHUB_OUTPUT"]) if os.environ.get("GITHUB_OUTPUT") else None
    )

    lines = [
        f"dispatch={'true' if event else 'false'}",
        "payload=" + json.dumps(event or {}, separators=(",", ":")),
    ]
    if output_path:
        with output_path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    else:
        print("\n".join(lines))


if __name__ == "__main__":
    main()
