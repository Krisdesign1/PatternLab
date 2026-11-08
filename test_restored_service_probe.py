from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import restored_service_probe as probe


class SandboxValidationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name).resolve()
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(probe,'SCRIPT_ROOT',self.root))
        self.stack.enter_context(patch.object(Path,'cwd',return_value=self.root))
        self.stack.enter_context(patch.object(probe.sys,'prefix',str(self.root/'.venv')))
        self.stack.enter_context(patch.dict(os.environ,{'PATTERNLAB_STORAGE_MODE':'local'},clear=True))
        self.marker=dict(version=probe.MARKER_VERSION,project_root=str(self.root),offline=True,production=False)
        (self.root/'.restoration_sandbox.json').write_text(json.dumps(self.marker))
        (self.root/'.streamlit').mkdir()
        (self.root/'.streamlit/secrets.toml').write_text('PATTERNLAB_STORAGE_MODE="local"\n[access]\nlocal_owner=true\n')

    def test_complete_synthetic_profile_and_independent_environment_are_required(self):
        self.assertEqual(probe.validate_sandbox(self.root),self.root)
        with patch.object(probe.sys,'prefix','/outside/python'),self.assertRaisesRegex(probe.ProbeError,'independent_environment'):
            probe.validate_sandbox(self.root)

    def test_marker_cannot_be_missing_or_allow_production(self):
        path=self.root/'.restoration_sandbox.json';path.unlink()
        with self.assertRaisesRegex(probe.ProbeError,'marker_missing'):probe.validate_sandbox(self.root)
        self.marker['production']=True;path.write_text(json.dumps(self.marker))
        with self.assertRaisesRegex(probe.ProbeError,'marker_invalid'):probe.validate_sandbox(self.root)

    def test_location_mismatch_fails_before_marker_or_profile_reads(self):
        with patch.object(Path,'cwd',return_value=Path('/outside')),self.assertRaisesRegex(probe.ProbeError,'location_mismatch'):
            probe.validate_sandbox(self.root)

    def test_existing_real_access_values_are_refused_without_echo(self):
        with patch.dict(os.environ,{'API_FOOTBALL_KEY':'PRIVATE'}),self.assertRaisesRegex(probe.ProbeError,'private_environment') as raised:
            probe.validate_sandbox(self.root)
        self.assertNotIn('PRIVATE',str(raised.exception))
        (self.root/'.streamlit/secrets.toml').write_text('PATTERNLAB_STORAGE_MODE="local"\nAPI_FOOTBALL_KEY="PRIVATE"\n[access]\nlocal_owner=true\n')
        with self.assertRaisesRegex(probe.ProbeError,'synthetic_profile_required'):probe.validate_sandbox(self.root)

    def test_symlink_marker_or_synthetic_profile_is_not_followed(self):
        path=self.root/'.restoration_sandbox.json';outside=self.root/'marker.json';path.rename(outside);path.symlink_to(outside)
        with self.assertRaisesRegex(probe.ProbeError,'path_invalid'):probe.validate_sandbox(self.root)

    def test_main_refuses_existing_output_before_probe(self):
        out=self.root/'receipt.json';out.write_text('original')
        with patch.object(probe,'probe') as run,redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            probe.main(['--project-root',str(self.root),'--output','receipt.json'])
        run.assert_not_called();self.assertEqual(out.read_text(),'original')

    def test_main_never_accepts_an_output_outside_sandbox(self):
        with patch.object(probe,'probe') as run,redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            probe.main(['--project-root',str(self.root),'--output','../receipt.json'])
        run.assert_not_called()


class ProbeGuardTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name).resolve()
        import api_football as api,cloud_storage,causal_candidate as candidate,recovery_rehearsal
        self.api,self.candidate,self.recovery=api,candidate,recovery_rehearsal
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(probe,'validate_sandbox',return_value=self.root))
        self.stack.enter_context(patch.object(probe,'_origin'))
        self.stack.enter_context(patch.object(api,'DATA_DIR',self.root/'data/api_football'))
        self.stack.enter_context(patch.object(cloud_storage,'configured',return_value=False))
        self.stack.enter_context(patch.object(probe,'_candidate_replay',return_value=dict(state='passed',model_binary_loaded=True)))
        self.stack.enter_context(patch.object(probe,'_offline_cycle',return_value=dict(state='passed',calendar_completed=True)))

    def test_success_is_explicitly_offline_and_not_complete_disaster_recovery(self):
        with patch.object(self.recovery,'_domain',return_value={'items':1}):r=probe.probe(self.root)
        self.assertEqual(r['state'],'passed');self.assertEqual(len(r['checks']),9)
        self.assertFalse(r['complete_service_recovery_demonstrated']);self.assertFalse(r['private_profile_restored'])
        self.assertFalse(r['live_provider_tested']);self.assertFalse(r['launchd_registered'])
        self.assertTrue(r['synthetic_profile_used'])

    def test_attempted_network_becomes_failed_check_and_controlled_counter(self):
        def forbidden(*args):
            with socket.socket() as sock:sock.connect(('example.invalid',443))
        with patch.object(self.recovery,'_domain',side_effect=forbidden):r=probe.probe(self.root)
        self.assertEqual(r['state'],'failed');self.assertEqual(r['network_attempts'],6)
        self.assertNotIn('example.invalid',json.dumps(r))

    def test_attempted_api_key_read_is_blocked_and_reported_without_value(self):
        with patch.object(self.recovery,'_domain',side_effect=lambda *args:self.api.load_key()):r=probe.probe(self.root)
        self.assertEqual(r['state'],'failed');self.assertEqual(r['credential_attempts'],6)

    def test_domain_failure_is_not_reported_as_empty_success(self):
        with patch.object(self.recovery,'_domain',side_effect=ValueError('PRIVATE')):r=probe.probe(self.root)
        self.assertEqual(r['state'],'failed');self.assertNotIn('PRIVATE',json.dumps(r))
        rows=[c for c in r['checks'] if c['component'] in self.recovery.STORES]
        self.assertTrue(all(c['state']=='failed' and 'items' not in c for c in rows))


class OfflineCycleTests(unittest.TestCase):
    def test_replay_cycle_clears_only_disposable_gate_state_and_pauses_before_capture(self):
        import api_football as api
        import api_match_workspace as calendar
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);root=Path(temp.name).resolve()
        data=root/'data/api_football';data.mkdir(parents=True)
        state=dict(quota={'requests_remaining_day':0},cooldown_until=9999999999,plans={'keep':True},result_checks={'12':1})
        path=data/'automation_state.json';path.write_text(json.dumps(state))
        original=path.read_bytes()
        class Pause(Exception):pass
        def write(p,value):p.write_text(json.dumps(value))
        def execute(**kwargs):
            client=kwargs['client'];self.assertEqual(kwargs['root'],data)
            self.assertTrue(kwargs['config']['enabled']);self.assertFalse(kwargs['config']['models_enabled'])
            result=client.fixtures(date=calendar.datetime.now(calendar.timezone.utc).astimezone(calendar.ZoneInfo('Africa/Kinshasa')).date().isoformat(),timezone='Africa/Kinshasa')
            self.assertEqual(result['results'],0)
            with self.assertRaises(Pause):client.fixtures(ids='12')
            return dict(status='paused',requests=2,day_service={'calendar_at':'2026-11-02T00:00:00Z'},causal_confirmation_results={})
        worker=SimpleNamespace(DEFAULT_CONFIG={},TIMEZONE='Africa/Kinshasa',_Pause=Pause,
            _read=lambda p,default:json.loads(p.read_text()),_write=write,run_cycle=execute,
            _track_causal_confirmation=None,_forecast=None,_track_candidates=None,_prepare_daily_learning=None,
            _track_daily_learning=None,_track_argumented_selection=None)
        service=SimpleNamespace(_run_once=lambda proxy,r,**kwargs:proxy.run_cycle(root=r/'data/api_football',config=kwargs['config']))
        with patch.object(calendar,'local_day_matches',return_value=[]):r=probe._offline_cycle(root,api,worker,service)
        self.assertEqual(r['state'],'passed');self.assertTrue(r['seeded_offline_gate'])
        self.assertEqual(r['original_restored_state_sha256'],probe.sha256(original).hexdigest())
        self.assertEqual(json.loads(path.read_text()),dict(plans={'keep':True},result_checks={'12':1}))
        self.assertFalse(r['full_forecast_queue_executed']);self.assertFalse(r['production_service_registered'])


if __name__=='__main__':unittest.main()
