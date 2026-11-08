"""Differential checks: scoped candidate reads preserve the original journal."""
import copy
from datetime import timedelta
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import candidate_tracking as original
import candidate_tracking_io as scoped
import cloud_storage
import fixture_store
import prediction_journal as journal
from test_candidate_tracking import CandidateTrackingTests as FixtureHelpers, NOW
from test_prediction_journal import raw_fixtures


class CandidateScopedIOTests(unittest.TestCase):
    def setUp(self):
        # Reuse only the isolated fixture setup/helpers; fake artifact creation
        # writes deterministic test files and performs no ML training.
        self.helpers = FixtureHelpers('test_read_only_empty_views_create_nothing')
        self.helpers.setUp()
        self.addCleanup(self.helpers.doCleanups)
        self.root = self.helpers.root
        self.twin = Path(self.enterContext(tempfile.TemporaryDirectory()))/'scoped'
        history = []
        for row in self.helpers.rows:
            item = raw_fixtures([row], status='FT')[0]
            item['score'] = {period:dict(zip(('home', 'away'), map(int, row[key].split('-'))))
                for period,key in (('halftime','ht'),('fulltime','ft'))}
            history.append(item)
        other = copy.deepcopy(self.helpers.future[0])
        other['fixture']['id'] = 99990
        other['league']['id'] = 999
        api.save_fixtures(history+self.helpers.future+[other], self.root/'fixtures.json')
        # The helper's reader stub is useful for enrollment; tests explicitly
        # use the real SQLite authority for the scoped history reads below.
        fixture_store.migrate_json(self.root/'fixtures.json')
        self.study = self.helpers.enroll()
        shutil.copytree(self.root, self.twin)
        self.expected_implementation = original._implementation()

    def methods(self, rows, fixtures, study, root):
        return self.helpers.methods(rows, fixtures, study, root)

    def history_reader(self, path, **filters):
        return fixture_store.matches(fixture_store.database_path(path), **filters)

    def run_pair(self, *, refresh=None):
        calls = [[], []]
        reports = []
        for index,(engine,root) in enumerate(((original,self.root),(scoped,self.twin))):
            def fresh(_root=root, _index=index, **params):
                calls[_index].append(params)
                if refresh:
                    return refresh(_root, **params)
                return api.load_fixtures(_root/'fixtures.json', ids=params['ids'].split('-'))
            with patch.object(api,'load_matches',side_effect=self.history_reader), \
                    patch.object(original,'_methods',side_effect=self.methods):
                reports.append(engine.run_cycle(root, clock=lambda:NOW, refresh=fresh))
        self.assertEqual(reports[0], reports[1])
        self.assertEqual(calls[0],calls[1])
        self.assertEqual(journal.list_entries(original.database_path(self.root)),
                         journal.list_entries(original.database_path(self.twin)))
        self.assertEqual(original._implementation(),self.expected_implementation)
        return reports

    def test_real_scoped_sqlite_reads_preserve_forecasts_ids_methods_and_idempotence(self):
        self.assertEqual(self.run_pair()[0]['created'],3)
        self.assertEqual(self.run_pair()[0]['existing'],3)
        self.assertEqual(original.studies(self.root),original.studies(self.twin))

    def test_day_bounds_and_unrelated_leagues_do_not_change_anchors_or_forecasts(self):
        for root in (self.root,self.twin):
            additions=[]
            for mid,stamp in ((99001,'2025-03-19T23:30:00Z'),
                              (99002,'2025-03-20T22:59:00Z'),
                              (99003,'2025-03-20T23:00:00Z')):
                item=copy.deepcopy(self.helpers.future[0]);item['fixture'].update(id=mid,date=stamp)
                additions.append(item)
            api.save_fixtures(additions,root/'fixtures.json')
        self.run_pair()
        first=original._anchors(self.study, [], NOW.date().isoformat(), self.root)
        second=original._anchors(self.study, [], NOW.date().isoformat(), self.twin)
        self.assertEqual(first,second)
        self.assertNotIn('99003',{f['match_id'] for block in second for f in block['fixtures']})

    def test_saved_anchor_is_not_rebuilt_when_a_fixture_moves_to_another_day(self):
        for root in (self.root,self.twin):
            original._anchors(self.study,api.load_fixtures(root/'fixtures.json'),NOW.date().isoformat(),root)
            shifted=copy.deepcopy(self.helpers.future[0]);shifted['fixture']['date']='2025-03-21T12:00:00Z'
            api.save_fixtures([shifted],root/'fixtures.json')
        self.run_pair()

    def test_empty_studies_do_not_read_any_fixtures_or_history(self):
        root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        with patch.object(api,'load_matches',side_effect=AssertionError('No history')) as history, \
                patch.object(api,'load_fixtures',side_effect=AssertionError('No raw')) as raw:
            self.assertEqual(scoped.run_cycle(root,clock=lambda:NOW)['status'],'idle')
            self.assertEqual(scoped.reconcile(root,now=NOW)['entries'],0)
        history.assert_not_called();raw.assert_not_called()

    def test_reconciliation_uses_only_journal_ids_and_matches_status_revisions(self):
        self.run_pair()
        for root in (self.root,self.twin):
            raw=copy.deepcopy(self.helpers.future)
            for item in raw:
                item['fixture']['status']['short']='FT'
            raw[0]['fixture']['status']['short']='CANC'
            raw[1]['fixture']['status']['short']='PST'
            api.save_fixtures(raw,root/'fixtures.json')
        expected=original.reconcile(self.root,now=NOW+timedelta(days=1))
        with patch.object(api,'load_fixtures',wraps=api.load_fixtures) as reader:
            actual=scoped.reconcile(self.twin,now=NOW+timedelta(days=1))
        self.assertEqual(actual,expected)
        self.assertEqual(reader.call_count,1)
        self.assertEqual(set(reader.call_args.kwargs['ids']),{str(f['fixture']['id']) for f in self.helpers.future})
        self.assertEqual(journal.list_entries(original.database_path(self.root)),
                         journal.list_entries(original.database_path(self.twin)))

    def test_scope_is_explicit_and_scheduler_pause_propagates(self):
        import automation_worker
        with patch.object(api,'load_matches',side_effect=self.history_reader) as history, \
                patch.object(api,'load_fixtures',wraps=api.load_fixtures) as raw, \
                patch.object(original,'_methods',side_effect=self.methods), \
                self.assertRaises(automation_worker._Pause):
            scoped.run_cycle(self.twin,clock=lambda:NOW,
                check_time=lambda:(_ for _ in ()).throw(automation_worker._Pause('Checkpoint')))
        self.assertEqual(history.call_args.kwargs,{'leagues':('39',)})
        self.assertEqual(raw.call_count,1)
        self.assertEqual(raw.call_args.kwargs['leagues'],('39',))
        self.assertEqual(raw.call_args.kwargs['start'].isoformat(),'2025-03-19T23:00:00+00:00')
        self.assertEqual(raw.call_args.kwargs['end'].isoformat(),'2025-03-20T23:00:00+00:00')


if __name__=='__main__':
    unittest.main()
