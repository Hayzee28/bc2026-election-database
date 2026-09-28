import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from build_candidate_event import build_event


class CandidateEventTests(unittest.TestCase):
    def test_ignores_non_actionable_source_or_field_changes(self):
        previous = [{"observedAtUtc": "2026-09-27T00:00:00Z"}]
        current = previous + [{
            "observedAtUtc": "2026-09-28T00:00:00Z",
            "added": [],
            "removed": [],
            "statusChanges": [],
            "modified": [{"changes": {"affiliation": {"from": "A", "to": "B"}}}],
        }]
        self.assertIsNone(build_event(previous, current))

    def test_summarizes_only_safe_actionable_counts(self):
        previous = []
        current = [{
            "observedAtUtc": "2026-09-28T01:02:03Z",
            "added": [{"candidate": "Not copied to event"}],
            "removed": [{"candidate": "Not copied to event"}],
            "statusChanges": [{"candidate": "Not copied to event"}],
        }]
        event = build_event(previous, current)
        self.assertEqual(event["actionableCount"], 3)
        self.assertEqual(event["addedCount"], 1)
        self.assertNotIn("candidate", event)
        self.assertNotIn("name", event)


if __name__ == "__main__":
    unittest.main()
