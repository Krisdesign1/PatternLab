"""Saved views must preserve values, types, rows, and independent table settings."""
from copy import deepcopy
import unittest

import pandas as pd
from pandas.testing import assert_frame_equal

from table_view_state import apply_dataframe_view, normalize_view


class TableViewStateTests(unittest.TestCase):
    def test_schema_migration_retains_known_order_and_adds_new_columns(self):
        saved = dict(version=1, columns=['B', 'removed', 'A'], hidden=['A', 'removed'],
                     widths={'A': 110, 'removed': 200}, sort=[dict(column='removed', direction='asc')])
        view = normalize_view(['A', 'B', 'new'], supplied=saved)
        self.assertEqual(view['columns'], ['B', 'A', 'new'])
        self.assertEqual(view['hidden'], ['A'])
        self.assertEqual(view['widths'], {'A': 110})
        self.assertEqual(view['sort'], [])

    def test_invalid_payloads_keep_defaults(self):
        defaults = dict(version=1, columns=['B', 'A'], hidden=['B'], widths={'A': 125}, sort=[], height=None)
        for saved in (None, 'bad', [], {'version': 88}, {'version': 1, 'columns': 'bad', 'hidden': 3}):
            self.assertEqual(normalize_view(['A', 'B'], defaults, saved), defaults)

    def test_invalid_settings_and_prototype_names_cannot_introduce_columns(self):
        saved = dict(version=1, columns=['A', 'A', '__proto__'], hidden=['unknown'],
                     widths={'A': float('inf'), 'B': True, 'C': -10, 'D': 41.5, 'E': 3000},
                     sort=[{'column': 'A', 'direction': 'delete'}, {'column': 'unknown', 'direction': 'asc'}])
        view = normalize_view(list('ABCDE'), supplied=saved)
        self.assertEqual(view['columns'], list('ABCDE'))
        self.assertEqual(view['widths'], {})
        self.assertEqual(view['hidden'], [])
        self.assertEqual(view['sort'], [])

    def test_all_columns_can_be_hidden_and_recovered(self):
        saved = dict(version=1, columns=['A', 'B'], hidden=['A', 'B'], widths={}, sort=[])
        self.assertEqual(normalize_view(['A', 'B'], supplied=saved)['hidden'], ['A', 'B'])
        self.assertEqual(normalize_view(['A', 'B'])['hidden'], [])

    def test_sort_numeric_chronological_stable_and_null_last(self):
        original = pd.DataFrame({'points': [2.0, 10.0, 2.0, None, 2.0],
                                 'date': pd.to_datetime(['2026-11-08', '2026-11-01', '2026-10-31',
                                                         '2026-11-01', '2026-10-31']),
                                 'id': ['a', 'b', 'c', 'd', 'e']}, index=[7, 3, 9, 4, 5])
        before = original.copy(deep=True)
        saved = dict(version=1, columns=['id', 'date', 'points'], hidden=['points'], widths={},
                     sort=[dict(column='points', direction='desc'), dict(column='date', direction='asc')])
        result = apply_dataframe_view(original, saved)
        self.assertEqual(list(result['id']), ['b', 'c', 'e', 'a', 'd'])
        self.assertEqual(list(result.index), [3, 9, 5, 7, 4])
        self.assertEqual(list(result.columns), list(original.columns))
        assert_frame_equal(original, before)
        self.assertEqual(result.dtypes.to_dict(), original.dtypes.to_dict())

    def test_empty_or_unsortable_frame_stays_intact(self):
        frame = pd.DataFrame({'A': [object(), {'x': 1}]})
        view = dict(version=1, columns=['A'], hidden=[], widths={}, sort=[dict(column='A', direction='asc')])
        self.assertEqual(list(apply_dataframe_view(frame, view)['A']), list(frame['A']))
        empty = pd.DataFrame({'A': pd.Series(dtype='int64')})
        assert_frame_equal(apply_dataframe_view(empty, view), empty)

    def test_normalization_never_mutates_saved_preferences(self):
        saved = dict(version=1, columns=['B', 'A'], hidden=['B'], widths={'A': 125},
                     sort=[dict(column='A', direction='asc')])
        before = deepcopy(saved)
        normalize_view(['A', 'B', 'C'], supplied=saved)
        self.assertEqual(saved, before)


if __name__ == '__main__':
    unittest.main()
