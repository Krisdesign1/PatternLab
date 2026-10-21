import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import database
from analytics import complete_patterns, recent_vs_global
from global_scenarios import global_groups


class GlobalScenarioTests(unittest.TestCase):
    def test_crosses_leagues_chronologically_without_overlap(self):
        rows=[{'match_id':str(i),'timestamp':f'2026-09-01T{(i-1)//2:02}:00:00',
               'league':'A' if i%2 else 'B','value':'2nd' if i%2 else '1st'} for i in range(1,42)]
        for size in range(2,21):
            result=global_groups(rows[::-1]+rows[:1],size)
            flattened=[r['match_id'] for g in result['groups'] for r in g['matches']]
            self.assertEqual(flattened,[str(i) for i in range(1,(41//size)*size+1)])
            self.assertEqual(len(result['remaining']),41%size)
            self.assertEqual(sum(complete_patterns([g['sequence'] for g in result['groups']],size).values()),41//size)
        self.assertEqual(global_groups(rows,2)['groups'][0]['sequence'],('2nd','1st'))
        with self.assertRaises(ValueError):global_groups(rows+[dict(rows[0],value='Égal')],4)

    def test_twenty_match_repetition_and_consistent_trends(self):
        rows=[{'match_id':str(i),'timestamp':'2026-09-01T12:00:00','league':'A','value':'2nd'} for i in range(1,42)]
        result=global_groups(rows,20)
        self.assertEqual(complete_patterns([g['sequence'] for g in result['groups']],20)[('2nd',)*20],2)
        trends=recent_vs_global([('1st',)*4,('2nd',)*2],2,recent_n=1,complete=True)
        self.assertEqual(len(trends),1)
        self.assertEqual(trends[0]['scénario'],'2nd → 2nd')

    def test_synthetic_twenty_match_groups_do_not_read_external_data(self):
        rows=[dict(match_id=str(i), timestamp='2026-09-01T12:00:00', league='39', value='2nd') for i in range(1,61)]
        groups=global_groups(rows,20)['groups']
        self.assertEqual(len(groups),3)
        self.assertTrue(all(group['sequence']==('2nd',)*20 for group in groups))
