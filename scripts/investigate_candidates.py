#!/usr/bin/env python3
"""Reconstruct and analyze validated Elections BC candidate snapshots.

Never interprets a missing row as a reason for its absence. Historical snapshots
come only from committed data.json versions, and future snapshots from the updater.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
from difflib import SequenceMatcher
import gzip
import hashlib
import json
from pathlib import Path
import re
import subprocess

from update_candidates import SOURCE_URL, audit_identity, candidate_status, STATUS_SUFFIX_RE

ROOT = Path(__file__).resolve().parents[1]
SNAP_DIR = ROOT / 'source-snapshots'
HISTORY = ROOT / 'source-history.json'
FIELDS = ('jurisdiction', 'office', 'candidate', 'affiliation', 'financialAgent', 'sourcePage')
COMPARE = ('candidate', 'office', 'jurisdiction', 'affiliation', 'financialAgent', 'status')
MILESTONES = [
    ('2026-09-01', 'Nomination period begins'),
    ('2026-09-11', 'Close of nominations'),
    ('2026-09-18', 'Campaign financing arrangement deadline'),
    ('2026-09-19', 'Campaign period begins'),
    ('2026-10-17', 'General Voting Day'),
]
CALENDAR_URL = 'https://elections.bc.ca/local-elections/2026-general-local-elections/'


def read(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def dump(path, value, compact=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':') if compact else None,
                               indent=None if compact else 2) + '\n', encoding='utf-8')


def source_row(row):
    return {field: row.get(field, '') for field in FIELDS}


def snapshot_bytes(rows):
    return (json.dumps([source_row(row) for row in rows], ensure_ascii=False,
                       separators=(',', ':')) + '\n').encode('utf-8')


def save_snapshot(identifier, rows):
    SNAP_DIR.mkdir(exist_ok=True)
    path = SNAP_DIR / (identifier + '.json.gz')
    raw = snapshot_bytes(rows)
    if path.exists():
        if gzip.decompress(path.read_bytes()) != raw:
            raise RuntimeError(f'Immutable snapshot differs: {path}')
        return
    with path.open('wb') as handle:
        with gzip.GzipFile(filename='', mode='wb', fileobj=handle, mtime=0) as out:
            out.write(raw)


def load_snapshot(entry):
    with gzip.open(SNAP_DIR / (entry['snapshotId'] + '.json.gz'), 'rt', encoding='utf-8') as handle:
        rows = json.load(handle)
    if len(rows) != entry['candidateCount']:
        raise RuntimeError('Snapshot count mismatch: ' + entry['snapshotId'])
    if len({audit_identity(r) for r in rows}) != len(rows):
        raise RuntimeError('Duplicate candidate identity in snapshot: ' + entry['snapshotId'])
    return rows


def git_data_versions():
    commits = subprocess.check_output(['git', 'log', '--reverse', '--format=%H', '--', 'data.json'],
                                      cwd=ROOT, text=True).splitlines()
    versions = []
    for sha in commits:
        rows = json.loads(subprocess.check_output(['git', 'show', f'{sha}:data.json'], cwd=ROOT))
        if not versions or snapshot_bytes(versions[-1][1]) != snapshot_bytes(rows):
            versions.append((sha, rows))
    return versions


def history_entry(audit, index, identifier, rows, provenance):
    return {
        'snapshotId': identifier,
        'firstObservedAtUtc': audit['observedAtUtc'],
        'sourceUrl': audit.get('sourceUrl', SOURCE_URL),
        'sourceSha256': audit.get('sourceSha256'),
        'sourceSha256Prefix': audit.get('sourceSha256Prefix'),
        'previousSourceSha256': audit.get('previousSourceSha256'),
        'currentSourceHash': audit.get('sourceSha256') or audit.get('sourceSha256Prefix'),
        'previousSourceHash': audit.get('previousSourceSha256') or None,
        'pageCount': audit.get('pageCount'),
        'candidateCount': len(rows),
        'jurisdictionCount': len({r['jurisdiction'] for r in rows}),
        'additions': len(audit.get('added', [])),
        'removals': len(audit.get('removed', [])),
        'statusChanges': len(audit.get('statusChanges', [])),
        'fieldChanges': len(audit.get('modified', [])),
        'reconstructed': provenance == 'git_backfill',
        'provenance': provenance,
        'auditIndex': index,
    }


def sync_archive(backfill=False):
    audit = read(ROOT / 'candidate-change-history.json', [])
    if not isinstance(audit, list) or not audit:
        raise RuntimeError('Candidate audit history missing')
    registry = read(HISTORY, [])
    if backfill and not registry:
        versions = git_data_versions()
        if len(versions) < len(audit):
            raise RuntimeError('Git has fewer distinct data versions than audit revisions')
        # Match each audited count in chronological order; never fabricate a snapshot.
        position = 0
        for i, event in enumerate(audit):
            matches = [(j, sha, rows) for j, (sha, rows) in enumerate(versions)
                       if j >= position and len(rows) == event['candidateCount']]
            if not matches:
                raise RuntimeError(f'No committed snapshot matches audit event {i}')
            j, sha, rows = matches[0]
            position = j + 1
            identifier = sha[:16]
            save_snapshot(identifier, rows)
            entry = history_entry(event, i, identifier, rows, 'git_backfill')
            if registry:
                entry['previousSourceHash'] = registry[-1]['currentSourceHash']
            registry.append(entry)
    if not registry:
        raise RuntimeError('Run --backfill to establish the committed baseline')
    if len(registry) > len(audit):
        raise RuntimeError('Audit history was truncated')
    for i, event in enumerate(audit[:len(registry)]):
        if registry[i]['firstObservedAtUtc'] != event['observedAtUtc']:
            raise RuntimeError('Append-only audit diverged at event ' + str(i))
    for i in range(len(registry), len(audit)):
        event = audit[i]
        if i != len(audit) - 1:
            raise RuntimeError('Missing intermediate PDF snapshot; refusing to invent it')
        rows = read(ROOT / 'data.json', [])
        if len(rows) != event['candidateCount']:
            raise RuntimeError('Current data does not match latest audit count')
        identifier = event.get('sourceSha256') or hashlib.sha256(snapshot_bytes(rows)).hexdigest()
        save_snapshot(identifier, rows)
        entry = history_entry(event, i, identifier, rows, 'validated_updater')
        entry['previousSourceHash'] = registry[-1]['currentSourceHash']
        registry.append(entry)
    dump(HISTORY, registry)
    return registry


def key(row):
    return tuple(p.casefold() for p in audit_identity(row))


def record(row):
    return {**source_row(row), 'status': candidate_status(row) or 'Active'}


def find_name_corrections(removed, added):
    """Link only unique, similar names with the same nonblank agent and place."""
    possibilities = defaultdict(list)
    reverse = defaultdict(list)
    for old_key, before in removed.items():
        for new_key, after in added.items():
            if old_key[:2] != new_key[:2]:
                continue
            if not before['financialAgent'] or before['financialAgent'].casefold() != after['financialAgent'].casefold():
                continue
            a, b = old_key[2], new_key[2]
            score = SequenceMatcher(None, a, b).ratio()
            if score >= .52:
                possibilities[old_key].append(new_key)
                reverse[new_key].append(old_key)
    return {old: options[0] for old, options in possibilities.items()
            if len(options) == 1 and len(reverse[options[0]]) == 1}


def base_name(value):
    return STATUS_SUFFIX_RE.sub('', value)


def classify_return(before, after):
    changed = {f: {'from': before[f], 'to': after[f]} for f in COMPARE if before[f] != after[f]
               and (f != 'candidate' or base_name(before[f]) != base_name(after[f]))}
    if not changed:
        label = 'IDENTICAL_RETURN'
    elif len(changed) > 1:
        label = 'MULTIPLE_FIELDS_CHANGED'
    else:
        label = {'status': 'RETURN_WITH_STATUS_CHANGE', 'candidate': 'RETURN_WITH_NAME_CHANGE',
                 'affiliation': 'RETURN_WITH_AFFILIATION_CHANGE',
                 'financialAgent': 'RETURN_WITH_AGENT_CHANGE'}.get(next(iter(changed)), 'MULTIPLE_FIELDS_CHANGED')
    return label, changed


def duration(start, end):
    seconds = (datetime.fromisoformat(end.replace('Z', '+00:00')) -
               datetime.fromisoformat(start.replace('Z', '+00:00'))).total_seconds()
    return round(seconds / 3600, 2) if seconds >= 0 else None


def analyze(registry, snapshots=None, local=None):
    snapshots = snapshots or [load_snapshot(e) for e in registry]
    if len(registry) != len(snapshots):
        raise ValueError('Snapshot mismatch')
    entities, aliases, changes, batches, revisions = {}, {}, [], [], []
    prev = {}
    for i, (meta, rows) in enumerate(zip(registry, snapshots)):
        now = meta['firstObservedAtUtc']
        current = {key(r): record(r) for r in rows}
        if len(current) != len(rows):
            raise ValueError('Duplicate candidate identity')
        if i == 0:
            for k, r in current.items():
                entity_id = '|'.join(k)
                aliases[k] = entity_id
                entities[entity_id] = {'candidateId': entity_id, 'candidate': r['candidate'],
                                       'jurisdiction': r['jurisdiction'], 'office': r['office'],
                                       'firstPresentAtUtc': now, 'events': [
                                           {'atUtc': now, 'type': 'BASELINE_PRESENT', 'record': r}],
                                       'state': 'PRESENT', '_last': r, '_absent': None}
            prev = current
            revisions.append({'atUtc': now, 'sourceHash': meta['currentSourceHash'],
                              'candidateChanges': 0, 'jurisdictionsAffected': 0,
                              'baseline': True})
            continue
        removed = {k: prev[k] for k in prev.keys() - current.keys()}
        added = {k: current[k] for k in current.keys() - prev.keys()}
        corrections = find_name_corrections(removed, added)
        for old, new in corrections.items():
            aliases[new] = aliases[old]
            del removed[old]
            del added[new]
            before, after = prev[old], current[new]
            ent = entities[aliases[new]]
            ent['candidate'] = after['candidate']
            ent['_last'] = after
            changes.append({'atUtc': now, 'revision': i, 'candidateId': aliases[new],
                            'candidate': after['candidate'], 'jurisdiction': after['jurisdiction'],
                            'office': after['office'], 'type': 'NAME_CORRECTION',
                            'from': before['candidate'], 'to': after['candidate'], 'category': 'FIELD_CORRECTION',
                            'identityLinkInferred': True,
                            'linkBasis': 'Unique similar name and identical agent in same jurisdiction/office'})
            ent['events'].append(changes[-1])
        for k in sorted(removed):
            ent = entities[aliases[k]]
            ent['state'] = 'ABSENT'
            ent['_absent'] = now
            c = {'atUtc': now, 'revision': i, 'candidateId': aliases[k],
                 'candidate': prev[k]['candidate'], 'jurisdiction': prev[k]['jurisdiction'],
                 'office': prev[k]['office'], 'type': 'REMOVED'}
            changes.append(c); ent['events'].append(c)
        # A returning candidate may have a corrected name. Link only an unambiguous
        # previously absent record with the same jurisdiction, office, and agent.
        absent_records = {tuple(ent['candidateId'].split('|')): ent['_last']
                          for ent in entities.values() if ent['state'] == 'ABSENT'
                          and ent['_last'] is not None}
        for old_key, new_key in find_name_corrections(absent_records, added).items():
            if new_key not in aliases:
                aliases[new_key] = entities['|'.join(old_key)]['candidateId']
        for k in sorted(added):
            after = current[k]
            if k in aliases:
                ent = entities[aliases[k]]
                before = ent['_last']
                label, fields = classify_return(before, after)
                absent_at = ent['_absent']
                ctype = 'RESTORED_AFTER_ABSENCE'
                c = {'atUtc': now, 'revision': i, 'candidateId': aliases[k],
                     'candidate': after['candidate'], 'jurisdiction': after['jurisdiction'],
                     'office': after['office'], 'type': ctype, 'returnClassification': label,
                     'disappearedAtUtc': absent_at, 'absentHoursApprox': duration(absent_at, now),
                     'firstPresentAtUtc': ent['firstPresentAtUtc'], 'fieldsChanged': fields,
                     'identityLinkInferred': k != tuple(ent['candidateId'].split('|'))}
                ent['candidate'] = after['candidate']
            else:
                entity_id = '|'.join(k)
                aliases[k] = entity_id
                ent = entities[entity_id] = {'candidateId': entity_id, 'candidate': after['candidate'],
                        'jurisdiction': after['jurisdiction'], 'office': after['office'],
                        'firstPresentAtUtc': now, 'events': [], 'state': 'PRESENT',
                        '_last': after, '_absent': None}
                c = {'atUtc': now, 'revision': i, 'candidateId': entity_id,
                     'candidate': after['candidate'], 'jurisdiction': after['jurisdiction'],
                     'office': after['office'], 'type': 'NEW'}
            changes.append(c); ent['events'].append(c)
            ent['state'] = 'PRESENT'; ent['_last'] = after; ent['_absent'] = None
        for k in sorted(prev.keys() & current.keys()):
            before, after = prev[k], current[k]
            ent = entities[aliases[k]]
            ent['_last'] = after
            if base_name(before['candidate']) != base_name(after['candidate']):
                c = {'atUtc': now, 'revision': i, 'candidateId': aliases[k],
                     'candidate': after['candidate'], 'jurisdiction': after['jurisdiction'],
                     'office': after['office'], 'type': 'NAME_CORRECTION',
                     'from': before['candidate'], 'to': after['candidate'], 'category': 'FIELD_CORRECTION',
                     'identityLinkInferred': False}
                changes.append(c); ent['events'].append(c); ent['candidate'] = after['candidate']
            for field, label in [('status', 'WITHDRAWN' if after['status'] == 'Withdrawn' else 'STATUS_CHANGE'),
                                 ('affiliation', 'AFFILIATION_CHANGE'),
                                 ('financialAgent', 'FINANCIAL_AGENT_CHANGE')]:
                if before[field] != after[field]:
                    c = {'atUtc': now, 'revision': i, 'candidateId': aliases[k],
                         'candidate': after['candidate'], 'jurisdiction': after['jurisdiction'],
                         'office': after['office'], 'type': label, 'field': field,
                         'from': before[field], 'to': after[field],
                         **({'category': 'FIELD_CORRECTION'} if field != 'status' else {})}
                    changes.append(c); ent['events'].append(c)
            # Page moves are source layout changes, not candidate corrections.
        revision_changes = [c for c in changes if c['revision'] == i]
        affected = sorted({c['jurisdiction'] for c in revision_changes})
        revisions.append({'atUtc': now, 'sourceHash': meta['currentSourceHash'],
                          'candidateChanges': len(revision_changes),
                          'jurisdictionsAffected': len(affected),
                          'jurisdictions': affected})
        if len(revision_changes) >= 2:
            counts = Counter(c['jurisdiction'] for c in revision_changes)
            kind = ('SAME_JURISDICTION_BATCH' if len(counts) == 1 else
                    'CENTRAL_WIDE_UPDATE' if len(counts) >= 5 and len(revision_changes) >= 10 else
                    'MULTI_JURISDICTION_BATCH')
            batches.append({'atUtc': now, 'revision': i, 'type': kind,
                            'candidateChanges': len(revision_changes), 'jurisdictions': affected,
                            'candidates': [{'candidate': c['candidate'], 'jurisdiction': c['jurisdiction'],
                                            'type': c['type']} for c in revision_changes]})
            # Nested local groups expose five changes in one municipality even in a wider revision.
            for jur, count in sorted(counts.items()):
                if count >= 2 and len(counts) > 1:
                    batches.append({'atUtc': now, 'revision': i, 'type': 'SAME_JURISDICTION_BATCH',
                                    'candidateChanges': count, 'jurisdictions': [jur],
                                    'candidates': [{'candidate': c['candidate'], 'jurisdiction': jur,
                                                    'type': c['type']} for c in revision_changes if c['jurisdiction'] == jur]})
        prev = current
    timeline = []
    for e in entities.values():
        timeline.append({k: v for k, v in e.items() if not k.startswith('_')})
    timeline.sort(key=lambda e: (e['jurisdiction'], e['office'], e['candidate']))
    local = local or {}
    anomalies = []
    for c in changes:
        if c['type'] not in ('REMOVED', 'RESTORED_AFTER_ABSENCE', 'WITHDRAWN', 'NAME_CORRECTION'):
            continue
        local_state = local_match(c, local)
        related = [b['type'] for b in batches if b['revision'] == c['revision'] and
                   any(x['candidate'] == c['candidate'] and x['jurisdiction'] == c['jurisdiction']
                       for x in b['candidates'])]
        facts = [f"Elections BC revision at {c['atUtc']}: {c['type']}."]
        clues = []
        if c.get('identityLinkInferred'):
            facts = [f"At {c['atUtc']}, the source shows the earlier name absent and the later name present."]
            clues.append({'indicator': 'Same candidate identity', 'direction': 'SUPPORTS',
                          'basis': 'Unique similar name and identical agent in the same jurisdiction and office; identity link remains inferred.'})
        if c['type'] == 'RESTORED_AFTER_ABSENCE':
            facts += [f"Absent approximately {c['absentHoursApprox']} hours between observed revisions.",
                      f"Return comparison: {c['returnClassification']}."]
            if c['returnClassification'] == 'IDENTICAL_RETURN':
                clues.append({'indicator': 'Temporary record/publishing issue', 'direction': 'SUPPORTS',
                              'basis': 'Returned source fields were identical.'})
        if local_state == 'LOCAL_PRESENT / EBC_ABSENT':
            facts.append('Temporary absence observed only in Elections BC source.')
            clues.append({'indicator': 'Elections BC-side publishing/sync issue', 'direction': 'SUPPORTS',
                          'basis': 'Official local list contained the candidate during the observed EBC absence.'})
            clues.append({'indicator': 'Local-list removal', 'direction': 'WEAKENS',
                          'basis': 'Official local list still contained the exact candidate name.'})
        if local_state == 'LOCAL_ABSENT / EBC_ABSENT':
            clues.append({'indicator': 'Local status change', 'direction': 'SUPPORTS',
                          'basis': 'Both observed source lists omitted the exact candidate name.'})
            clues.append({'indicator': 'EBC-only absence', 'direction': 'WEAKENS',
                          'basis': 'Official local list also omitted the exact candidate name.'})
        if c['type'] == 'WITHDRAWN':
            clues.append({'indicator': 'Status update', 'direction': 'SUPPORTS',
                          'basis': 'Elections BC marked the record Withdrawn.'})
        if related:
            clues.append({'indicator': 'Batch update', 'direction': 'SUPPORTS',
                          'basis': ', '.join(sorted(set(related)))})
        if not clues:
            clues.append({'indicator': 'Cause of change', 'direction': 'UNKNOWN',
                          'basis': 'Observed source revisions do not explain why.'})
        anomalies.append({**c, 'pattern': 'PRESENT → ABSENT → PRESENT' if c['type'] == 'RESTORED_AFTER_ABSENCE' else c['type'],
                          'localSourceDuringAbsence': local_state,
                          'publicExplanation': 'NO PUBLIC EXPLANATION FOUND',
                          'batchTypes': sorted(set(related)), 'facts': facts, 'clues': clues,
                          'unresolved': ['Reason for source change is not established.']})
    counts = defaultdict(Counter)
    updates = defaultdict(set)
    people = defaultdict(set)
    for c in changes:
        jur = c['jurisdiction']; counts[jur][c['type']] += 1
        updates[jur].add(c['revision']); people[jur].add(c['candidateId'])
    churn = []
    for jur, tally in counts.items():
        total = sum(tally.values())
        labels = []
        if total >= 5: labels.append('HIGH CHANGE COUNT')
        if tally['RESTORED_AFTER_ABSENCE'] >= 2: labels.append('MULTIPLE RESTORATIONS')
        if tally['WITHDRAWN'] + tally['STATUS_CHANGE'] >= 2: labels.append('MULTIPLE STATUS CHANGES')
        if len(updates[jur]) >= 2: labels.append('REPEATED UPDATE ACTIVITY')
        churn.append({'jurisdiction': jur, 'changeCount': total, 'additions': tally['NEW'],
                      'removals': tally['REMOVED'], 'restorations': tally['RESTORED_AFTER_ABSENCE'],
                      'withdrawals': tally['WITHDRAWN'], 'nameCorrections': tally['NAME_CORRECTION'],
                      'affiliationChanges': tally['AFFILIATION_CHANGE'],
                      'financialAgentChanges': tally['FINANCIAL_AGENT_CHANGE'],
                      'statusChanges': tally['STATUS_CHANGE'], 'updateCount': len(updates[jur]),
                      'uniqueCandidatesAffected': len(people[jur]), 'labels': labels})
    churn.sort(key=lambda r: (-r['changeCount'], r['jurisdiction']))
    hours = Counter(c['atUtc'][:13] for c in changes)
    days = Counter(c['atUtc'][:10] for c in changes)
    timing = {'changesPerHourUtc': dict(sorted(hours.items())),
              'changesPerDayUtc': dict(sorted(days.items())),
              'revisions': revisions,
              'milestones': [{'date': date, 'name': name, 'sourceUrl': CALENDAR_URL}
                             for date, name in MILESTONES]}
    return {'timelines': timeline, 'changes': changes, 'batches': batches, 'anomalies': anomalies,
            'churn': churn, 'timing': timing}


def local_match(change, local):
    jur = change['jurisdiction']
    s = local.get(jur, {})
    # A later/current check cannot establish what a local page said at an earlier disappearance.
    if s.get('status') != 'OK' or not s.get('checkedAtUtc') or s['checkedAtUtc'][:13] != change['atUtc'][:13]:
        return 'UNKNOWN'
    name = change['candidate'].casefold()
    present = any(n.casefold() == name for n in s.get('candidateNames', []))
    ebc_present = change['type'] not in ('REMOVED',)
    return f"LOCAL_{'PRESENT' if present else 'ABSENT'} / EBC_{'PRESENT' if ebc_present else 'ABSENT'}"



def compare_current_local(registry, local, timelines):
    """Literal name presence in configured official lists, scoped by jurisdiction."""
    current = load_snapshot(registry[-1])
    ebc_by_jur = defaultdict(dict)
    for row in current:
        ebc_by_jur[row['jurisdiction']][base_name(row['candidate']).casefold()] = row['candidate']
    historical = defaultdict(dict)
    for entity in timelines:
        historical[entity['jurisdiction']][base_name(entity['candidate']).casefold()] = entity['candidate']
    results = []
    for jur, state in sorted(local.items()):
        if state.get('status') != 'OK':
            continue
        names = {name.casefold(): name for name in state.get('candidateNames', [])}
        ebc = ebc_by_jur[jur]
        universe = set(names) | set(ebc) | set(historical[jur])
        for name in sorted(universe):
            local_yes, ebc_yes = name in names, name in ebc
            results.append({'jurisdiction': jur, 'candidate': names.get(name) or ebc.get(name) or historical[jur][name],
                            'state': f"LOCAL_{'PRESENT' if local_yes else 'ABSENT'} / EBC_{'PRESENT' if ebc_yes else 'ABSENT'}",
                            'localCheckedAtUtc': state['checkedAtUtc'],
                            'ebcObservedAtUtc': registry[-1]['firstObservedAtUtc'],
                            'localSourceUrl': state['sourceUrl'],
                            'ebcSourceUrl': registry[-1]['sourceUrl'],
                            'comparisonMethod': 'case-insensitive exact name in configured jurisdiction'})
    return results


def render_report(result, registry):
    lines = ['# Candidate change observations', '',
             f"Generated from {len(registry)} observed/reconstructed source revisions. Times are UTC.",
             'Historical Git backfill is marked reconstructed; missing earlier source states are unknown.',
             '', '## Snapshot history', '',
             '| First observed UTC | Candidates | Jurisdictions | + | − | Status | Fields | Source hash |',
             '|---|---:|---:|---:|---:|---:|---:|---|']
    for e in registry:
        lines.append(f"| {e['firstObservedAtUtc']}{' (reconstructed)' if e['reconstructed'] else ''} | "
                     f"{e['candidateCount']} | {e['jurisdictionCount']} | {e['additions']} | "
                     f"{e['removals']} | {e['statusChanges']} | {e['fieldChanges']} | "
                     f"{e['currentSourceHash'] or 'unknown'} |")
    lines += ['', '## Candidate observations', '']
    for a in result['anomalies']:
        lines += [f"### {a['candidate']} — {a['jurisdiction']} ({a['office']})", '',
                  f"Pattern: {a['pattern']}; observed {a['atUtc']}", '', '**FACTS**', '']
        lines += ['- ' + f for f in a['facts']]
        lines += ['', f"Local source comparison: {a['localSourceDuringAbsence']}",
                  f"Public explanation: {a['publicExplanation']}", '', '**PROCESS-OF-ELIMINATION CLUES (INFERENCE)**', '']
        lines += [f"- {c['direction']}: {c['indicator']} — {c['basis']}" for c in a['clues']]
        lines += ['', '**UNRESOLVED**', '- Reason for source change is not established.', '']
    lines += ['## Jurisdiction changes (raw counts)', '',
              '| Jurisdiction | Changes | Updates | Candidates affected |',
              '|---|---:|---:|---:|']
    for c in result['churn']:
        lines.append(f"| {c['jurisdiction']} | {c['changeCount']} | {c['updateCount']} | {c['uniqueCandidatesAffected']} |")
    return '\n'.join(lines) + '\n'


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--backfill', action='store_true')
    args = p.parse_args()
    registry = sync_archive(args.backfill)
    local = read(ROOT / 'local-source-state.json', {})
    result = analyze(registry, local=local)
    explanations = read(ROOT / 'official-explanations.json', {})
    for anomaly in result['anomalies']:
        if anomaly['candidateId'] in explanations:
            anomaly['publicExplanation'] = explanations[anomaly['candidateId']]
            anomaly['unresolved'] = ['Official text match found; whether it explains this revision is not established.']
    outputs = {'local-ebc-mismatches.json': compare_current_local(registry, local, result['timelines']),
               'candidate-timelines.json': result['timelines'],
               'jurisdiction-churn.json': result['churn'],
               'candidate-anomalies.json': result['anomalies'],
               'candidate-anomaly-report.json': {'anomalies': result['anomalies'],
                                                   'batches': result['batches'], 'timing': result['timing']},
               'source-history.json': registry}
    for name, value in outputs.items():
        dump(ROOT / name, value, compact=name != 'candidate-anomaly-report.json' and name != 'source-history.json')
    path = ROOT / 'reports/latest-candidate-anomalies.md'
    path.parent.mkdir(exist_ok=True)
    path.write_text(render_report(result, registry), encoding='utf-8')
    print(f"ANALYSIS PASS: {len(registry)} snapshots, {len(result['timelines'])} candidate timelines, "
          f"{len(result['anomalies'])} anomaly events")


if __name__ == '__main__':
    main()
