import copy
import json
import math
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import database
from league_model import (VALUES, load_matches, score, outcome, probabilities,
                          chronological_evaluation, coupon_scenarios, league_summary)


def row(day, value='2nd', league='A'):
    return dict(day=day, value=value, league=league)


class LeagueTests(unittest.TestCase):
    def test_score_conversion_compares_period_goals(self):
        self.assertEqual(outcome(score('(1 - 0)'), score('1 - 1')), 'Égal')
        self.assertEqual(outcome((2, 0), (2, 1)), '1st')
        self.assertEqual(outcome((0, 0), (1, 0)), '2nd')
        with self.assertRaises(ValueError):
            outcome((2, 0), (1, 1))
        with self.assertRaises(ValueError):
            score('pending')

    def test_sparse_fallback_and_league_isolation(self):
        data = [row(f'2026-09-{18+i%3}', '1st') for i in range(30)]
        data += [row('2026-09-18', '2nd', 'B') for _ in range(100)]
        p = probabilities(data, 'A')
        self.assertEqual(p['method'], 'Compétition lissée')
        self.assertGreater(p['probabilities']['1st'], probabilities(data)['probabilities']['1st'])
        self.assertEqual(probabilities(data, 'B')['method'], 'Référence globale')
        self.assertEqual(probabilities(data, 'unknown')['probabilities'], probabilities(data)['probabilities'])
        self.assertAlmostEqual(sum(p['probabilities'].values()), 1)

    def test_evaluation_excludes_whole_target_day(self):
        data = [row('2026-09-18', '1st') for _ in range(100)]
        data += [row('2026-09-19', '2nd') for _ in range(200)]
        records = chronological_evaluation(data)
        self.assertEqual(len(records), 400)
        self.assertTrue(all(x['correct'] == 0 for x in records))
        self.assertTrue(all(abs(x['loss'] - math.log(103)) < 1e-10 for x in records))
        self.assertEqual(chronological_evaluation(data[::-1]), records)

    def test_scenarios_cover_all_four_match_combinations(self):
        p = probabilities([row('2026-09-18')])
        scenarios = coupon_scenarios([p] * 4)
        self.assertEqual(len(scenarios), 81)
        self.assertAlmostEqual(sum(s['Probabilité indicative (%)'] for s in scenarios), 100)
        self.assertEqual(scenarios[0]['Scénario'], '2nd → 2nd → 2nd → 2nd')

    def test_provenance_and_score_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            detail = dict(id='1', ht='(1-0)', ft='1-1', date='19/09/2026 18:00', home='A', away='B',
                          league_info="getstag(this,1,'France','Ligue 1','fr/ligue1','fr')")
            (folder/'day_details.json').write_text(json.dumps({'matches': [detail]}))
            imported = dict(coupon_id=1, matches=[dict(match_id='1', ht='1-0', ft='1-1', value='Égal')])
            (folder/'day_import.json').write_text(json.dumps({'source': 'forebet', 'imported': [imported]}))
            coupons = [dict(id=1, note='Forebet | match_ids=1', sequence=['Égal'])]
            self.assertEqual(len(load_matches(coupons, folder)[0]), 1)
            self.assertEqual(load_matches([], folder)[0], [])
            other = copy.deepcopy(coupons); other[0]['note'] = 'New coupon'
            self.assertEqual(load_matches(other, folder)[0], [])
            detail['ft'] = '3-0'
            (folder/'day_details.json').write_text(json.dumps({'matches': [detail]}))
            rows, audit = load_matches(coupons, folder)
            self.assertEqual(rows, [])
            self.assertEqual(len(audit['issues']), 1)

    def test_api_explorer_groups_without_using_coupons_or_other_providers(self):
        from streamlit.testing.v1 import AppTest
        import league_pattern_panel as panel
        from test_scenario_workspace_panel import history_rows
        rows=history_rows()
        with patch.object(panel.match_sources,'data_signature',return_value=('synthetic',1,1)), \
             patch.object(panel.match_sources,'load_matches',return_value=(rows,dict(expected=len(rows),matched=len(rows),issues=[]))) as load:
            at=AppTest.from_string("from league_pattern_panel import render_leagues\nrender_leagues([], expert=False)",default_timeout=15).run()
            self.assertFalse(at.exception)
            self.assertFalse(any(s.key=='league_source' for s in at.selectbox))
            self.assertEqual(set(at.selectbox(key='league_detail').options),{'England — Premier League','France — Ligue 1'})
            at.selectbox(key='league_group_size').set_value(20).run()
            self.assertFalse(at.exception)
            self.assertTrue(any('groupes complets de 20 matchs' in s.value for s in at.caption))
            self.assertTrue(all(call.args==([], 'API-Football') for call in load.call_args_list))


if __name__ == '__main__':
    unittest.main()
