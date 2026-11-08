"""Integration contracts for presentation-only browser preferences."""
import ast
from copy import deepcopy
import json
from pathlib import Path
import unittest

import pandas as pd
from streamlit.testing.v1 import AppTest

import table_views as views
from table_view_state import normalize_view


class SavedTableViewsTests(unittest.TestCase):
    def test_column_formats_badges_and_pinned_settings_survive_width_override(self):
        frame = pd.DataFrame({'Match': ['A'], 'p': [.7], 'Résultat': [['2nd']]})
        config = {'Match': {'width': 200, 'pinned': True},
                  'p': {'label': 'Probabilité', 'type_config': {'type': 'number', 'format': '%.2f'}},
                  'Résultat': {'type_config': {'type': 'multiselect', 'options': ['1st', 'Égal', '2nd']}}}
        before = deepcopy(config)
        view = normalize_view(list(frame), supplied=dict(version=1, columns=['p', 'Match', 'Résultat'],
                              hidden=['Résultat'], widths={'p': 130}, sort=[]))
        applied = views._view_kwargs(frame, view, {'hide_index': True, 'key': 'existing', 'column_config': config})
        self.assertEqual(applied['column_order'], ['p', 'Match'])
        self.assertEqual(applied['key'], 'existing')
        self.assertEqual(applied['column_config']['p']['width'], 130)
        self.assertEqual(applied['column_config']['p']['type_config'], config['p']['type_config'])
        self.assertEqual(applied['column_config']['Résultat'], config['Résultat'])
        self.assertTrue(applied['column_config']['Match']['pinned'])
        self.assertEqual(config, before)

    def test_defaults_capture_hidden_order_and_sizes_and_allow_reshowing(self):
        frame = pd.DataFrame({'A': [1], 'B': [2], 'C': [3]})
        options = {'column_order': ['C', 'A'], 'column_config': {'A': None, 'C': {'width': 140}}}
        defaults = views._defaults(frame, options)
        self.assertEqual(defaults['columns'], ['C', 'A', 'B'])
        self.assertEqual(defaults['hidden'], ['A', 'B'])
        shown = normalize_view(list(frame), defaults, dict(version=1, columns=list(frame), hidden=[], widths={}, sort=[]))
        applied = views._view_kwargs(frame, shown, options)
        self.assertEqual(applied['column_order'], ['A', 'B', 'C'])
        self.assertNotIn('A', applied['column_config'])

    def test_preferences_have_distinct_stable_keys_and_no_row_data(self):
        key = views.storage_key('calendar:matches')
        self.assertEqual(key, views.storage_key('calendar:matches'))
        self.assertNotEqual(key, views.storage_key('clubs:matches'))
        self.assertNotIn('calendar', key)
        with self.assertRaises(ValueError): views.storage_key('')

    def test_multiple_identical_schema_tables_and_reruns_do_not_collide(self):
        source = ('import table_views as v\nv.begin_render()\n'
                  'for i in range(3):\n v.dataframe([{"A":i}],table_id="loop")\n')
        for _ in range(2):
            at = AppTest.from_string(source).run()
            self.assertFalse(at.exception)
            self.assertEqual(len(at.dataframe), 3)
            keys = [item.proto.id for item in at.get('bidi_component')]
            self.assertEqual(len(set(keys)), 3)
            at.run(); self.assertFalse(at.exception)

    def test_editor_transmits_schema_only_not_table_rows(self):
        at = AppTest.from_string('from table_views import dataframe\n'
            'dataframe([{"Match":"test-only-private-value","p":.7}],table_id="safe")').run()
        self.assertFalse(at.exception)
        payload = json.loads(at.get('bidi_component')[0].proto.json)
        self.assertNotIn('test-only-private-value', json.dumps(payload))
        self.assertEqual(payload['columns'], [{'id': 'Match', 'label': 'Match'}, {'id': 'p', 'label': 'p'}])

    def test_every_production_dataframe_uses_explicit_stable_view_id(self):
        routed, raw = [], []
        for path in Path(__file__).parent.glob('*.py'):
            if path.name.startswith('test_') or path.name == 'table_views.py': continue
            for node in ast.walk(ast.parse(path.read_bytes())):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)): continue
                owner, name = node.func.value.id, node.func.attr
                if owner == 'st' and name in ('dataframe', 'table'): raw.append((path.name, node.lineno))
                if owner == 'table_views' and name in ('dataframe', 'table', 'html_table'):
                    identity = next((kw.value for kw in node.keywords if kw.arg == 'table_id'), None)
                    self.assertIsInstance(identity, ast.Constant, (path.name, node.lineno))
                    routed.append(identity.value)
        self.assertFalse(raw)
        self.assertEqual(len(routed), 192)
        self.assertEqual(len(set(routed)), 192)


if __name__ == '__main__': unittest.main()
