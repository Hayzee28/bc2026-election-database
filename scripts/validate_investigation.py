#!/usr/bin/env python3
"""Fail publication when historical or public investigation output is inconsistent."""
import gzip
import json
from pathlib import Path
from update_candidates import summarize_candidate_changes

ROOT = Path(__file__).resolve().parents[1]
history = json.loads((ROOT / 'source-history.json').read_text())
audit = json.loads((ROOT / 'candidate-change-history.json').read_text())
current = json.loads((ROOT / 'data.json').read_text())
timelines = json.loads((ROOT / 'candidate-timelines.json').read_text())
assert history and len(history) == len(audit)
assert len(current) == history[-1]['candidateCount']
assert len({t['candidateId'] for t in timelines}) == len(timelines)
snapshots = []
for e in history:
    path = ROOT / 'source-snapshots' / (e['snapshotId'] + '.json.gz')
    rows = json.loads(gzip.decompress(path.read_bytes()))
    assert len(rows) == e['candidateCount']
    snapshots.append(rows)
    assert all(set(row) == {'jurisdiction', 'office', 'candidate', 'affiliation', 'financialAgent', 'sourcePage'} for row in rows)
assert [{k: r[k] for k in ('jurisdiction', 'office', 'candidate', 'affiliation', 'financialAgent', 'sourcePage')} for r in current] == snapshots[-1]
for i in range(1, len(history)):
    delta = summarize_candidate_changes(snapshots[i - 1], snapshots[i])
    assert (len(delta['added']), len(delta['removed']), len(delta['statusChanges']), len(delta['modified'])) == (history[i]['additions'], history[i]['removals'], history[i]['statusChanges'], history[i]['fieldChanges'])
for name in ('candidate-timelines.json', 'jurisdiction-churn.json', 'candidate-anomalies.json',
             'candidate-anomaly-report.json', 'source-history.json', 'local-source-state.json', 'local-ebc-mismatches.json'):
    path = ROOT / name
    if path.exists():
        payload = path.read_text(encoding='utf-8').lower()
        assert 'serviceaddress' not in payload and 'service address' not in payload, name
print(f'INVESTIGATION VALIDATION PASS: {len(history)} snapshots, {len(timelines)} timelines')
