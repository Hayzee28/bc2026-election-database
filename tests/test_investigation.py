import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import investigate_candidates as inv
import update_candidates as updater
import watch_local_sources as local


def row(name, jur='Test Town', office='Councillor', affiliation='', agent=None):
    return {'jurisdiction': jur, 'office': office, 'candidate': name,
            'affiliation': affiliation, 'financialAgent': agent or 'Agent', 'sourcePage': 1}


def meta(i, rows):
    return {'snapshotId': str(i), 'firstObservedAtUtc': f'2026-09-2{i}T12:00:00Z',
            'candidateCount': len(rows), 'currentSourceHash': str(i)}


def run(*snapshots):
    return inv.analyze([meta(i, rows) for i, rows in enumerate(snapshots)], list(snapshots))


class InvestigationTests(unittest.TestCase):
    def test_new_and_normal_removal(self):
        x = run([row('A')], [row('A'), row('B')], [row('B')])
        self.assertEqual([c['type'] for c in x['changes']], ['NEW', 'REMOVED'])

    def test_withdrawal_is_not_name_correction(self):
        x = run([row('A')], [row('A (Withdrawn)')])
        self.assertEqual([c['type'] for c in x['changes']], ['WITHDRAWN'])

    def test_boomerang_identical_and_duration(self):
        x = run([row('A')], [], [row('A')])
        restored = x['changes'][-1]
        self.assertEqual(restored['type'], 'RESTORED_AFTER_ABSENCE')
        self.assertEqual(restored['returnClassification'], 'IDENTICAL_RETURN')
        self.assertEqual(restored['absentHoursApprox'], 24)
        self.assertEqual(len(x['timelines']), 1)

    def test_return_with_status_affiliation_agent(self):
        x = run([row('A', affiliation='One', agent='Agent')], [],
                [row('A (Withdrawn)', affiliation='Two', agent='Other')])
        self.assertEqual(x['changes'][-1]['returnClassification'], 'MULTIPLE_FIELDS_CHANGED')
        self.assertEqual(set(x['changes'][-1]['fieldsChanged']), {'status', 'affiliation', 'financialAgent'})

    def test_return_status_only(self):
        x = run([row('A')], [], [row('A (Withdrawn)')])
        self.assertEqual(x['changes'][-1]['returnClassification'], 'RETURN_WITH_STATUS_CHANGE')

    def test_restoration_with_corrected_name(self):
        x = run([row('Martin Kendall', agent='Clerk')], [],
                [row('Martin Kendell', agent='Clerk')])
        self.assertEqual(x['changes'][-1]['type'], 'RESTORED_AFTER_ABSENCE')
        self.assertEqual(x['changes'][-1]['returnClassification'], 'RETURN_WITH_NAME_CHANGE')
        self.assertEqual(len(x['timelines']), 1)

    def test_capitalization_and_unique_name_correction(self):
        x = run([row('ruby Sidhu'), row('Martin Kendall', agent='Clerk')],
                [row('Ruby Sidhu'), row('Martin Kendell', agent='Clerk')])
        self.assertEqual([c['type'] for c in x['changes']], ['NAME_CORRECTION', 'NAME_CORRECTION'])
        self.assertEqual(len(x['timelines']), 2)
        self.assertTrue(any(c.get('identityLinkInferred') for c in x['changes']))
        self.assertTrue(any(c.get('identityLinkInferred') is False for c in x['changes']))

    def test_same_and_multi_jurisdiction_batches(self):
        same = run([row('A')], [row('A'), row('B'), row('C')])
        self.assertIn('SAME_JURISDICTION_BATCH', [b['type'] for b in same['batches']])
        multi = run([row('A')], [row('A'), row('B'), row('C', jur='Elsewhere')])
        self.assertIn('MULTI_JURISDICTION_BATCH', [b['type'] for b in multi['batches']])

    def test_local_present_ebc_absent(self):
        a = {'jurisdiction': 'Test Town', 'candidate': 'A', 'type': 'REMOVED',
             'atUtc': '2026-09-21T12:00:00Z'}
        state = {'Test Town': {'status': 'OK', 'checkedAtUtc': a['atUtc'], 'candidateNames': ['A']}}
        self.assertEqual(inv.local_match(a, state), 'LOCAL_PRESENT / EBC_ABSENT')
        state['Test Town']['checkedAtUtc'] = '2026-09-20T12:00:00Z'
        self.assertEqual(inv.local_match(a, state), 'UNKNOWN')

    def test_local_source_error_preserves_last_good(self):
        prior = {'sourceHash': 'old', 'candidateNames': ['A'], 'checkedAtUtc': 'before', 'status': 'OK'}
        config = {'url': 'https://town.example/candidates', 'officialHosts': ['town.example'], 'type': 'HTML'}
        def fail(*_): raise OSError('timeout')
        state = local.check_one(config, prior, 'now', fail)
        self.assertEqual(state['status'], 'ERROR')
        self.assertEqual(state['candidateNames'], ['A'])
        self.assertEqual(state['sourceHash'], 'old')

    def test_html_official_table(self):
        html = b'<h2>Candidates for Mayor</h2><table><tr><th>Candidate Name</th></tr><tr><td>A Person</td></tr></table>'
        config = {'url': 'https://town.example/candidates', 'officialHosts': ['town.example'],
                  'type': 'HTML', 'sections': [{'heading': 'Candidates for Mayor'}]}
        state = local.check_one(config, {}, 'now', lambda *_: (html, config['url']))
        self.assertEqual(state['candidateNames'], ['A Person'])
        self.assertFalse(local.official('https://evil.example/', config['officialHosts']))

    def test_wrapped_jurisdiction_office_and_status(self):
        current = {'office': 'Board of Education'}
        self.assertTrue(updater.is_wrapped_status_continuation(
            current, 'District', 'Trustee', '(Withdrawn)', '', ''))

    def test_source_hash_change_with_same_candidate_count_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'state.json'; history = Path(tmp) / 'history.json'
            state.write_text(json.dumps({'sourceSha256': 'before'}))
            history.write_text('[]')
            rows = [row('A')]
            with patch.object(updater, 'SOURCE_STATE', state), patch.object(updater, 'AUDIT_HISTORY', history):
                self.assertTrue(updater.record_change_audit(rows, rows, 'after',
                                  {'pageCount': 199, 'jurisdictionCount': 1}))
            event = json.loads(history.read_text())[0]
            self.assertEqual(event['candidateCount'], 1)
            self.assertTrue(event['sourceChanged'])
            self.assertEqual(event['previousSourceSha256'], 'before')

    def test_parser_failure_does_not_overwrite_public_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / 'data.json'; csv = Path(tmp) / 'data.csv'
            data.write_text(json.dumps([row('A')])); csv.write_text('known good')
            with patch.object(updater, 'DATA_JSON', data), patch.object(updater, 'DATA_CSV', csv), \
                 patch.object(updater, 'download_pdf', return_value=b'%PDF' + b'x' * 100000), \
                 patch.object(updater, 'parse_pdf_words', side_effect=RuntimeError('bad source')), \
                 patch.object(sys, 'argv', ['update_candidates.py']):
                with self.assertRaisesRegex(RuntimeError, 'bad source'):
                    updater.main()
            self.assertEqual(csv.read_text(), 'known good')
            self.assertEqual(len(json.loads(data.read_text())), 1)


if __name__ == '__main__':
    unittest.main()
