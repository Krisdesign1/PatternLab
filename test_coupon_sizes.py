import csv
import io
import math
import random
import shutil
import tempfile
import unittest
from itertools import product
from pathlib import Path
from unittest.mock import patch
import database
from coupon_sizes import SIZES, top_independent
from coupon_io import parse_coupons
from league_model import VALUES
from league_patterns import repeated_scenarios, scenario_completions
from scenario_predictor import fit_sequences, probability, best_sequences, predict_league
from ml_model import fit_model
from test_scenario_predictor import sample


class SizeTests(unittest.TestCase):
    def test_storage_and_csv_boundaries_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(database, 'DB_PATH', Path(tmp)/'test.db'):
            sequences = [[VALUES[n%3]]*n for n in SIZES]
            self.assertEqual(database.import_coupons(sequences), 19)
            before=database.load_coupons()
            for n in (0,1,21):
                with self.assertRaises(ValueError):database.add_coupon(['1st']*n)
            with self.assertRaises(ValueError):database.import_coupons([['1st']*2,['1st']*21])
            self.assertEqual(before,database.load_coupons())
            output=io.StringIO();csv.writer(output).writerows(sequences)
            parsed,bad=parse_coupons(output.getvalue().encode('utf-8-sig'))
            self.assertEqual(parsed,sequences);self.assertFalse(bad)
            valid,bad=parse_coupons(('1st\n1st;2nd\n'+','.join(['2nd']*21)+'\ninvalid,2nd').encode())
            self.assertEqual(valid,[['1st','2nd']]);self.assertEqual(len(bad),3)
            self.assertEqual(fit_model(sequences)['transitions'],sum(n-1 for n in SIZES))

    def test_group_lengths_and_completion_no_overlap(self):
        for size in SIZES:
            rows=sample(2*size+1)
            result=repeated_scenarios(rows,size)['A']
            self.assertEqual([len(b['sequence']) for b in result['blocks']],[size,size])
            self.assertEqual(len(result['pending']),1)
            found=scenario_completions(result,result['blocks'][0]['sequence'][:-1])
            self.assertGreaterEqual(found['support'],1)
            self.assertTrue(all(len(r['suffix'])==1 for r in found['ranking']))
            with self.assertRaises(ValueError):scenario_completions(result,['1st']*size)

    def test_top_k_matches_exhaustive_including_ties(self):
        rng=random.Random(17)
        for size in range(2,7):
            for uniform in (False, True):
                distributions=[]
                for _ in range(size):
                    weights=[1,1,1] if uniform else [rng.random()+.05 for _ in VALUES]
                    distributions.append(dict(zip(VALUES,[w/sum(weights) for w in weights])))
                exhaustive=sorted([{'sequence':s,'probability':math.prod(d[v] for d,v in zip(distributions,s))}
                                   for s in product(VALUES,repeat=size)],key=lambda r:(-r['probability'],r['sequence']))
                self.assertEqual(top_independent(distributions,3),exhaustive[:3])
                sequences=[tuple(rng.choice(VALUES) for _ in range(size)) for _ in range(30)]
                fit=fit_sequences(sequences,size)
                expected=sorted(product(VALUES,repeat=size),key=lambda s:(-probability(fit,s),s))[:3]
                self.assertEqual(best_sequences(fit),expected)

    def test_twenty_match_predictions_are_bounded_and_not_renormalized(self):
        dist=dict(zip(VALUES,[.2,.3,.5]))
        top=top_independent([dist]*20,3)
        self.assertEqual(len(top),3)
        self.assertEqual(top[0]['sequence'],('2nd',)*20)
        self.assertAlmostEqual(top[0]['probability'],.5**20)
        self.assertLess(sum(x['probability'] for x in top),1)
        result=repeated_scenarios(sample(480),20)['A']
        forecast=predict_league(result)
        self.assertEqual(len(forecast['ranking']),3)
        self.assertTrue(all(len(r['sequence'])==20 for r in forecast['ranking']))
        self.assertTrue(forecast['trials'])

    def test_retired_manual_workspace_cannot_recreate_coupons(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as tmp, patch.object(database, 'DB_PATH', Path(tmp)/'coupons.db'):
            at=AppTest.from_file('legacy_workspace.py', default_timeout=15).run()
            self.assertFalse(at.exception)
            self.assertFalse(at.button)
            self.assertFalse(database.DB_PATH.exists())
