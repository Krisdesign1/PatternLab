from datetime import timedelta
import fcntl
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import automation_worker as automation
import model_campaign_worker as worker
from test_model_campaign import NOW


class CampaignWorkerTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        self.config=dict(automation.DEFAULT_CONFIG)
        self.jobs=[dict(league='777',status='ready'),dict(league='39',status='frozen'),dict(league='888',status='ready')]
        for target,name,kw in (
            (worker.campaign,'plan',dict(side_effect=lambda *a,**kw:self.jobs)),
            (worker.api,'load_matches',dict(return_value=([],{'issues':[]}))),
            (worker.api,'load_fixtures',dict(return_value=[])),
            (worker.campaign,'run_one',dict(side_effect=lambda rows,league,**kw:dict(league=league,status='frozen'))),
            (api,'ApiFootballClient',dict(side_effect=AssertionError('No API for model preparation')))):
            p=patch.object(target,name,**kw);obj=p.start();self.addCleanup(p.stop)
            if name=='run_one':self.run_one=obj

    def run_cycle(self, **kwargs):
        return worker.run_cycle(root=self.root,config=self.config,now=NOW,**kwargs)

    def test_only_one_ready_league_runs_and_frozen_entries_are_skipped(self):
        report=self.run_cycle()
        self.assertEqual((report['status'],report['prepared'],report['league']),('completed',1,'777'))
        self.run_one.assert_called_once_with([], '777', sizes=self.config['sizes'], root=self.root, now=NOW)
        self.jobs[0]['status']='frozen'
        second=self.run_cycle()
        self.assertEqual(second['league'],'888')
        self.jobs[2]['status']='frozen'
        self.assertEqual(self.run_cycle()['status'],'idle')
        self.assertEqual(self.run_one.call_count,2)

    def test_disabled_and_dry_run_do_not_fit_or_write(self):
        self.assertEqual(self.run_cycle(dry_run=True)['status'],'dry_run')
        self.config['models_enabled']=False
        self.assertEqual(self.run_cycle()['status'],'disabled')
        self.assertEqual(list(self.root.iterdir()),[])
        self.run_one.assert_not_called()

    def test_worker_lock_blocks_concurrent_fits_without_clobbering_status(self):
        status=self.root/'model_campaign_status.json';status.write_text('{"status":"building"}')
        with (self.root/'model_campaign_worker.lock').open('a') as handle:
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertEqual(self.run_cycle()['status'],'busy')
        self.assertEqual(status.read_text(),'{"status":"building"}')
        self.run_one.assert_not_called()

    def test_errors_retry_same_league_only_after_daily_backoff_while_others_advance(self):
        self.run_one.side_effect=worker.campaign.CampaignError('native failed secret')
        report=self.run_cycle()
        self.assertEqual(report['status'],'error')
        self.assertNotIn('secret',str(report))
        self.run_one.side_effect=lambda rows,league,**kw:dict(league=league,status='frozen')
        second=self.run_cycle()
        self.assertEqual(second['league'],'888')
        self.assertEqual(self.run_cycle()['status'],'idle')
        report=worker.run_cycle(root=self.root,config=self.config,now=NOW+timedelta(days=1))
        self.assertEqual(report['league'],'777')


if __name__=='__main__':unittest.main()
