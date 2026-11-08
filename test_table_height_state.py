"""Height preferences are optional, compatible, and presentation-only."""
from copy import deepcopy
import unittest

import pandas as pd
from pandas.testing import assert_frame_equal

from table_view_state import apply_dataframe_view, normalize_view


class TableHeightStateTests(unittest.TestCase):
    def test_legacy_view_retains_columns_widths_and_sort_without_a_height(self):
        old = dict(version=1, columns=['B', 'A'], hidden=['A'], widths={'B': 180},
                   sort=[dict(column='B', direction='desc')])
        view = normalize_view(['A', 'B'], supplied=old)
        self.assertIsNone(view['height'])
        self.assertEqual({key: view[key] for key in old}, old)

    def test_fixed_height_content_and_reset_are_preserved(self):
        defaults = dict(version=1, height=800)
        for height in (200, 1000, 5000, 'content', None):
            with self.subTest(height=height):
                result = normalize_view(['A'], defaults, dict(version=1, height=height))
                self.assertEqual(result['height'], height)

    def test_invalid_heights_fall_back_without_affecting_other_settings(self):
        defaults = dict(version=1, height=900)
        invalid = (True, False, 0, 199, 5001, -50, 800.0, float('nan'), float('inf'),
                   '900', 'auto', 'stretch', '100vh', [], {}, {'css': 'height:100vh'})
        for height in invalid:
            with self.subTest(height=height):
                view = normalize_view(['A'], defaults, dict(version=1, height=height, widths={'A': 150}))
                self.assertEqual(view['height'], 900)
                self.assertEqual(view['widths'], {'A': 150})

    def test_missing_or_unknown_version_height_uses_caller_defaults(self):
        defaults = dict(version=1, height=750)
        for saved in (None, {}, {'version': 88, 'height': 1000}, {'version': 1}):
            self.assertEqual(normalize_view(['A'], defaults, saved)['height'], 750)
        for defaults in (None, {}, {'version': 1, 'height': -1}, {'version': 88, 'height': 1200}):
            self.assertIsNone(normalize_view(['A'], defaults)['height'])

    def test_schema_changes_preserve_height_and_never_mutate_preferences(self):
        saved = dict(version=1, columns=['B', 'removed', 'A'], hidden=['A'], widths={'B': 150},
                     sort=[dict(column='B', direction='desc')], height=1600)
        before = deepcopy(saved)
        result = normalize_view(['A', 'B', 'new'], supplied=saved)
        self.assertEqual(result['height'], 1600)
        self.assertEqual(result['columns'], ['B', 'A', 'new'])
        self.assertEqual(saved, before)

    def test_height_never_filters_or_rewrites_rows(self):
        frame = pd.DataFrame({'id': list(range(100)), 'p': [index / 100 for index in range(100)]})
        original = frame.copy(deep=True)
        for height in (200, 1600, 5000, 'content', None):
            view = normalize_view(list(frame), supplied=dict(version=1, height=height))
            assert_frame_equal(apply_dataframe_view(frame, view), original)
        assert_frame_equal(frame, original)


if __name__ == '__main__':
    unittest.main()
