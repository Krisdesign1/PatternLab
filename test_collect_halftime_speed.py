"""Independent scheduler regressions: active speed must not create idle spin."""
import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch


SPEC=importlib.util.spec_from_file_location('speed_cli',Path(__file__).parent/'scripts/collect_halftime_statistics.py')
CLI=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(CLI)


class FakeStore:
    def __init__(self,*args,**kwargs):pass
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def get_meta(self,key,default=None):
        return time.time() if key=='last_discovery_cycle' else default


class SchedulerSpeedTests(unittest.TestCase):
    def test_future_provider_pause_overrides_completed_active_batch(self):
        result=dict(status='completed',requests=25,queue=dict(provider_pause_until=1001))
        self.assertEqual(CLI.watch_delay('run',result,0,60,now=1000),60)

    def test_expired_provider_pause_does_not_add_artificial_delay(self):
        result=dict(status='completed',requests=25,queue=dict(provider_pause_until=999))
        self.assertEqual(CLI.watch_delay('run',result,0,60,now=1000),0)

    def test_explicit_active_interval_preserved_only_for_active_work(self):
        self.assertEqual(CLI.watch_delay('run',dict(status='completed',requests=25),5,60,now=1000),5)
        self.assertEqual(CLI.watch_delay('run',dict(status='idle',requests=0),5,60,now=1000),60)

    def run_watch(self,responses,*,action='run',options=()):
        sleeps=[]
        target=('halftime_statistics_collector.collect_once' if action=='run'
                else 'halftime_statistics_collector.discover')
        with tempfile.TemporaryDirectory() as temp:
            arguments=[action,'--watch','--database',str(Path(temp)/'half.sqlite3'),
                       '--interval-seconds','0',*options]
            with patch('halftime_statistics_store.Store',FakeStore):
                with patch('halftime_statistics_client.ApiClient'):
                    with patch(target,side_effect=[*responses,KeyboardInterrupt()]) as worker:
                        with patch.object(CLI.time,'sleep',side_effect=sleeps.append):
                            with contextlib.redirect_stdout(io.StringIO()):
                                self.assertEqual(CLI.main(arguments),0)
        return sleeps,worker.call_count

    def test_zero_active_interval_allowed_but_idle_still_waits(self):
        sleeps,calls=self.run_watch([
            dict(status='completed',requests=25,queue=dict(provider_pause_until=0)),
            dict(status='idle',requests=0,queue=dict(provider_pause_until=0))])
        self.assertEqual(calls,3)
        self.assertTrue(sleeps)
        self.assertGreaterEqual(sleeps[-1],60)
        self.assertTrue(all(value==0 for value in sleeps[:-1]))

    def test_completed_without_network_never_busy_spins(self):
        sleeps,_=self.run_watch([dict(status='completed',requests=0,queue={})])
        self.assertEqual(len(sleeps),1)
        self.assertGreaterEqual(sleeps[0],60)

    def test_paused_after_work_must_wait(self):
        sleeps,_=self.run_watch([dict(status='paused',requests=25,queue={})])
        self.assertEqual(len(sleeps),1)
        self.assertGreaterEqual(sleeps[0],60)

    def test_cached_discovery_never_busy_spins(self):
        sleeps,_=self.run_watch([dict(status='completed',cached_seasons=21,requests=0)],action='discover')
        self.assertEqual(len(sleeps),1)
        self.assertGreaterEqual(sleeps[0],60)

    def test_negative_interval_rejected_before_storage(self):
        with patch('halftime_statistics_store.Store',side_effect=AssertionError('must not create store')):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    CLI.main(['run','--interval-seconds','-1'])

    def test_zero_idle_interval_rejected_before_storage(self):
        with patch('halftime_statistics_store.Store',side_effect=AssertionError('must not create store')):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    CLI.main(['run','--idle-interval-seconds','0'])


if __name__=='__main__':unittest.main()
