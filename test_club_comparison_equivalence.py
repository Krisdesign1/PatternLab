"""The optimized comparison must equal two independently bounded histories."""
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import club_trends as trends
import fixture_store
from test_club_trends import club_match


class ClubComparisonEquivalenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'fixtures.json'
        self.start, self.end = date(2026, 10, 1), date(2026, 10, 12)
        self.now = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
        rows = []
        for index in range(48):
            stamp = datetime(2026, 10, 1, 22, 59, tzinfo=timezone.utc) + timedelta(hours=6 * index)
            item = club_match(index + 1, ('1st', 'Égal', '2nd')[index % 3],
                team=100 + index % 4, away=bool(index % 2), league=(39, 61)[index % 2],
                name='Club ' + str(index % 4), stamp=stamp.isoformat(),
                season=(2025, 2026)[index % 2])
            if index % 11 == 0:
                item['score']['halftime']['home'] = None
            if index % 13 == 0:
                item['_patternlab_import'] = {'quarantined': True, 'reason': 'Identité à vérifier'}
            rows.append(item)
        self.path.write_text(json.dumps({'version': 1, 'fixtures': rows, 'season_imports': []}))
        fixture_store.migrate_json(self.path)
        # A stale accepted projection with a later import issue must stay out.
        with sqlite3.connect(fixture_store.database_path(self.path)) as connection:
            connection.execute("UPDATE fixtures SET issue='Import invalidé' WHERE match_id='3'")

    def test_comparison_equals_independent_histories_for_every_boundary_shape(self):
        for role in ('all', 'home', 'away'):
            for recent in (1, 5):
                for first, last in ((date(2026, 9, 29), date(2026, 9, 30)),
                                    (date(2026, 10, 1), date(2026, 10, 1)),
                                    (date(2026, 10, 6), date(2026, 10, 8)),
                                    (date(2026, 10, 14), date(2026, 10, 14))):
                    for seasons in (None, [2026]):
                        with self.subTest(role=role, recent=recent, first=first, last=last, seasons=seasons):
                            options = dict(path=self.path, now=self.now, role=role,
                                           recent=recent, seasons=seasons)
                            comparison = trends.build_comparison(self.start, self.end,
                                comparison_start=first, comparison_end=last, **options)
                            for phase, cutoff in (('before', first), ('after', last + timedelta(days=1))):
                                expected = trends.build(self.start, self.end, before_day=cutoff, **options)
                                self.assertEqual(comparison[phase], expected)

    def test_finished_reports_have_independent_mutable_profile_fields(self):
        # The compared day lies after the last stored match: the reports share
        # all observations, yet changing one view must never mutate the other.
        result = trends.build_comparison(self.start, self.end,
            comparison_start=date(2026, 10, 14), path=self.path, now=self.now)
        before, after = result['before']['rows'][0], result['after']['rows'][0]
        snapshot = json.loads(json.dumps(after))
        before['counts']['1st'] = 999
        before['seasons'].append(2099)
        before['recent_results'].append('1st')
        before['recent_home_ids'].append('999')
        before['home_counts']['2nd'] = 999
        self.assertEqual(after, snapshot)


if __name__ == '__main__':
    unittest.main()
