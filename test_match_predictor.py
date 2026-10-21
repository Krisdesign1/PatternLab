import unittest
from datetime import date,timedelta
from match_predictor import estimate, backtest, half_probabilities, group_scenarios, profile


def sample():
    return [dict(match_id=str(i+1),league='A',home='H'+str(i%4),away='A'+str(i%4),
                 timestamp=(date(2026,1,1)+timedelta(days=i//4)).isoformat()+'T12:00:00',
                 ht='1-0',ft='1-2',value='2nd') for i in range(64)]


class MatchTests(unittest.TestCase):
    def test_half_probabilities_symmetry_zero_and_normalization(self):
        p=half_probabilities(1,1)
        self.assertAlmostEqual(p['1st'],p['2nd'])
        self.assertEqual(half_probabilities(0,0),{'1st':0,'Égal':1,'2nd':0})
        for means in [(0,2),(2,0),(1.2,2.3),(10,12)]:
            p=half_probabilities(*means)
            self.assertAlmostEqual(sum(p.values()),1)
            self.assertGreaterEqual(min(p.values()),0)
        self.assertGreater(half_probabilities(1,3)['2nd'],half_probabilities(1,3)['1st'])

    def test_profiles_subtract_ht_from_ft_and_respect_venue(self):
        rows=sample()
        h=profile(rows,'H0','home')
        self.assertEqual(h,{'matches':16,'scored':[16,0],'conceded':[0,32]})
        self.assertEqual(profile(rows,'H0','away')['matches'],0)
        a=profile(rows,'A0','away')
        self.assertEqual(a['scored'],[0,32])

    def test_no_target_day_future_or_other_league_leakage(self):
        rows=sample();cutoff='2026-01-12'
        initial=estimate(rows,'A','H0','A0',cutoff)
        changed=[dict(r,ht='3-0',ft='3-0',value='1st') if r['timestamp'][:10]>=cutoff else r for r in rows]
        changed += [dict(r,league='B',match_id=str(1000+i)) for i,r in enumerate(rows)]
        self.assertEqual(initial,estimate(changed,'A','H0','A0',cutoff))
        self.assertEqual(initial['training'],44)
        self.assertTrue(initial['personalized'])
        self.assertAlmostEqual(sum(initial['probabilities'].values()),1)
        before=backtest(rows,'A');after=backtest(changed,'A')
        self.assertEqual([r for r in before if r['day']<cutoff],[r for r in after if r['day']<cutoff])

    def test_sparse_unknown_dedup_and_invalid(self):
        rows=sample()
        sparse=estimate(rows,'A','unknown','A0','2026-02-01')
        self.assertFalse(sparse['personalized'])
        self.assertEqual(sparse['probabilities'],sparse['baseline'])
        self.assertFalse(estimate(rows,'A','H0','A0','2026-01-02')['eligible'])
        self.assertEqual(estimate(rows,'A','H0','A0','2026-02-01'),estimate(rows+rows,'A','H0','A0','2026-02-01'))
        with self.assertRaises(ValueError):estimate(rows,'A','H0','H0','2026-02-01')
        with self.assertRaises(ValueError):estimate([dict(rows[0],ft='0-0')],'A','H0','A0','2026-02-01')

    def test_group_complete_normalized(self):
        e=estimate(sample(),'A','H0','A0','2026-02-01')
        groups=group_scenarios([e]*4)
        self.assertEqual(len(groups),81)
        self.assertAlmostEqual(sum(g['probability'] for g in groups),1)
        with self.assertRaises(ValueError):group_scenarios([e]*1)

    def test_interface_single_group_and_validation(self):
        from streamlit.testing.v1 import AppTest
        at=AppTest.from_string('from test_match_predictor import sample\n'
                              'from match_prediction_panel import render_match_prediction\n'
                              "render_match_prediction({'matches':sample()},'A')").run()
        at.checkbox(key='show_team_prediction').check().run()
        self.assertFalse(at.exception)
        at.button[0].click().run()
        self.assertTrue(at.warning)
        at.selectbox(key='team_home_A_1_0').select('H0')
        at.selectbox(key='team_away_A_1_0').select('A0')
        at.button[0].click().run()
        self.assertFalse(at.exception)
        self.assertTrue(any('Tendance :' in m.value for m in at.markdown))
        at.selectbox(key='team_group_size').select(4).run()
        for i in range(4):
            at.selectbox(key=f'team_home_A_4_{i}').select('H'+str(i))
            at.selectbox(key=f'team_away_A_4_{i}').select('A'+str(i))
        at.button[0].click().run()
        self.assertFalse(at.exception)
        self.assertTrue(any('Les trois scénarios de ton groupe' in m.value for m in at.markdown))
        at.selectbox(key='team_home_A_4_1').select('H0')
        at.selectbox(key='team_away_A_4_1').select('A0')
        at.button[0].click().run()
        self.assertTrue(any('une seule fois' in w.value for w in at.warning))
        self.assertFalse(any('Les trois scénarios de ton groupe' in m.value for m in at.markdown))
