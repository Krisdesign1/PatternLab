import os
from datetime import datetime,timedelta,timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import types
import unittest
from unittest.mock import Mock,patch

import daily_learning_worker as worker


class DailyLearningWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.now=datetime(2026,10,29,9,tzinfo=timezone.utc)
        self.models=types.SimpleNamespace(prepare_protocol=Mock(),
            plan=Mock(return_value=dict(ready=True,changed=True)),
            train=Mock(side_effect=lambda rows,code,**kwargs:dict(candidate_id='id'+code,
                trained_through='2026-10-28',cutoff_exclusive='2026-10-29')))
        self.conf=dict(enabled=True,models_enabled=True,model_jobs_per_cycle=1)
        self.patches=[patch.dict(os.environ,{'PATTERNLAB_STORAGE_MODE':'local'}),
            patch.dict('sys.modules',{'daily_learning_models':self.models}),
            patch.object(worker,'_inventory',return_value=({'1':40,'2':70,'3':12},
                {'2':'2026-10-29T10:00:00','1':'2026-10-30T12:00:00'},{'1':'hash1','2':'hash2'})),
            patch.object(worker.api,'load_matches',return_value=([{'match_id':'1'}],{'issues':[]}))]
        for p in self.patches:p.start();self.addCleanup(p.stop)
        worker.enable(self.root)

    def run_job(self,**kwargs):
        return worker.run_training(self.root,config=self.conf,now=self.now,**kwargs)

    def test_queue_prioritizes_upcoming_then_visits_each_league_once(self):
        self.assertEqual(self.run_job()['league'],'2')
        self.assertEqual(self.run_job()['league'],'1')
        self.assertEqual(self.run_job()['status'],'idle')
        self.assertEqual(self.models.train.call_count,2)

    def test_unchanged_inputs_are_not_retrained(self):
        self.models.plan.return_value=dict(ready=True,changed=False,candidate_id='same')
        report=self.run_job()
        self.assertEqual(report['candidates_unchanged'],2)
        self.models.train.assert_not_called()

    def test_error_does_not_starve_another_league(self):
        self.models.train.side_effect=[ValueError('private failure'),
            dict(candidate_id='ok',trained_through='2026-10-28',cutoff_exclusive='2026-10-29')]
        first=self.run_job();second=self.run_job()
        self.assertEqual(first['status'],'error')
        self.assertNotIn('private failure',str(first))
        self.assertEqual(second['league'],'1')

    def test_next_utc_day_rechecks_changed_data(self):
        self.run_job();self.now+=timedelta(days=1)
        self.run_job()
        self.assertEqual(self.models.train.call_count,2)
        self.assertEqual(self.models.train.call_args.args[1],'1')

    def test_same_day_import_rechecks_changed_input_without_midnight(self):
        self.run_job();self.run_job()
        worker._inventory.return_value=({'1':40,'2':70},
            {'2':'2026-10-29T10:00:00'},{'1':'hash1','2':'corrected'})
        report=self.run_job()
        self.assertEqual(report['league'],'2')
        self.assertEqual(self.models.train.call_count,3)

    def test_midnight_preserves_unvisited_background_priority(self):
        self.run_job()
        self.now+=timedelta(days=1)
        worker._inventory.return_value=({'1':40,'2':70},
            {'2':'2026-10-30T10:00:00'},{'1':'hash1','2':'new-day'})
        self.assertEqual(self.run_job()['league'],'1')

    def test_disabled_and_dry_run_never_prepare_or_train(self):
        self.conf['models_enabled']=False
        self.assertEqual(self.run_job()['status'],'disabled')
        self.assertFalse((self.root/'daily_learning/status.json').exists())
        self.conf['models_enabled']=True
        self.assertEqual(self.run_job(dry_run=True)['status'],'dry_run')
        self.models.prepare_protocol.assert_not_called();self.models.train.assert_not_called()

    def test_reading_status_does_not_start_worker(self):
        self.assertEqual(worker.status(self.root)['status'],'not_started')
        self.assertFalse((self.root/'daily_learning/status.json').exists())
        self.models.prepare_protocol.assert_not_called()

    def test_new_install_requires_explicit_activation(self):
        worker.enable(self.root,enabled=False)
        self.assertEqual(self.run_job()['status'],'disabled')
        self.models.prepare_protocol.assert_not_called()

    def test_only_known_valid_competitions_remain_queued(self):
        self.models.plan.return_value=dict(ready=False,reason='All classes absent')
        report=self.run_job()
        self.assertEqual(report['remaining_today'],0)
        self.assertEqual(report['checked'],2)
        self.models.train.assert_not_called()

    def test_existing_provider_worker_prepares_between_tasks(self):
        import automation_worker as automation
        gate=Mock();report={}
        with patch.object(worker,'run_training',return_value=dict(status='completed',prepared=1)) as prepare:
            automation._prepare_daily_learning(self.root,self.conf,gate,report)
        prepare.assert_called_once_with(self.root,config=self.conf)
        self.assertEqual(report['daily_learning_training']['prepared'],1)
        self.assertEqual(gate.check_time.call_count,2)


if __name__=='__main__':unittest.main()
