"""Read-local acceleration preserves every projection validation and exclusion."""
from copy import deepcopy
import json
import unittest

import club_trends
import competition_trends as trends


def projection_row():
    payload = dict(provider='api-football', match_id='1', league='39', season=2026,
        timestamp='2026-10-01T12:00:00', home_id='100', away_id='101',
        home='Home', away='Away', competition='Premier League', country='England',
        ht='0-0', ft='1-1', value='2nd')
    return dict(match_id='1', league='39', season=2026, kickoff_utc='2026-10-01T12:00:00',
                status='FT', issue=None, expected=1, has_projection=1,
                projection=json.dumps(payload), club_ids='["100","101"]')


class TrendReadContextTests(unittest.TestCase):
    def test_cached_and_uncached_validation_return_exactly_the_same_record(self):
        original = projection_row()
        context = trends._RecordContext()
        variants = [original]
        for field, values in {
            'provider': ('other', None, True), 'match_id': ('2', True, {}, None),
            'league': ('61', False, [], None), 'season': (True, '2026', 2025, None),
            'timestamp': ('2026-10-02T12:00:00', '', None, []),
            'home_id': ('101', True, {}, None, '00100'),
            'away_id': (False, [], None, '00101'),
            'home': ('', None, 4), 'away': (' ', None), 'competition': ('', None),
            'value': ('1st', None),
            'ht': (None, 'bad', '2-0', [0, 0], {'home': 0, 'away': 0}, [True, 0]),
            'ft': (None, 'bad', '0-0', [1, 1], {'home': 1, 'away': 1}, [1.0, 1]),
        }.items():
            for value in values:
                row = deepcopy(original)
                payload = json.loads(row['projection'])
                payload[field] = value
                row['projection'] = json.dumps(payload)
                variants.append(row)
        for status in ('NS', 'CANC', 'AET', 'PEN', None):
            variants.append(dict(original, status=status))
        for text in ('null', '[]', '42', '"text"', '{bad json'):
            variants.append(dict(original, projection=text))
        variants += [dict(original, has_projection=0, projection=json.dumps(value)) for value in (
            ['League', 'Country', 'Home', 'Away', {'home': 0, 'away': 0},
             {'home': 1, 'away': 1}, False, None],
            ['League', 'Country', 'Home', 'Away', None, None, True, 'Quarantaine'],
            None, [], 'bad',
        )]
        # Repeat with one reused context: stale successful evidence cannot make
        # a later invalid row pass. Failure reasons must remain identical too.
        for row in [*variants, *reversed(variants)]:
            with self.subTest(row=row):
                expected = trends._record(row)
                actual = trends._record(row, _context=context)
                self.assertEqual(actual, expected)
                self.assertEqual(context.outcome is not None, actual['included'])

    def test_club_current_import_issue_overrides_cached_successful_evidence(self):
        context = trends._RecordContext()
        row = projection_row()
        self.assertTrue(club_trends._record(row, context)['included'])
        invalid = club_trends._record(dict(row, issue='Import invalidé'), context)
        self.assertFalse(invalid['included'])
        self.assertTrue(invalid['excluded'])
        self.assertIsNone(invalid['value'])
        self.assertEqual(invalid['reason'], 'Import invalidé')
        appearances = list(club_trends._appearances(invalid, 'all', context.outcome))
        self.assertTrue(all(item['goals_for_h1'] is None for item in appearances))

    def test_caches_are_bounded_per_reader_and_do_not_retain_mutable_scores(self):
        context = trends._RecordContext()
        for number in range(1, 5001):
            context.identity(str(number))
        for number in range(300):
            context.score_pair(f'{number}-0')
        self.assertEqual(context._identity.cache_info().currsize, 4096)
        self.assertEqual(context._score_pair.cache_info().currsize, 256)
        mutable = {'home': 0, 'away': 0}
        self.assertEqual(context.score_pair(mutable), (0, 0))
        mutable['home'] = 1
        self.assertEqual(context.score_pair(mutable), (1, 0))
        other = trends._RecordContext()
        self.assertEqual(other._identity.cache_info().currsize, 0)
        self.assertEqual(other._score_pair.cache_info().currsize, 0)


if __name__ == '__main__':
    unittest.main()
