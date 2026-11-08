"""Exercise replay trust, isolation and registration guards using fake data."""
from contextlib import redirect_stdout
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from deployment.linux import frozen_replay as replay


PROJECT = Path(__file__).resolve().parents[2]


class DisposableCopyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.marker = self.root / replay.MARKER
        self.marker.write_text(json.dumps(dict(version=replay.MARKER_VERSION,
            project_root=str(self.root), disposable=True, production=False)))
        environment = patch.dict(os.environ, {"PATTERNLAB_STORAGE_MODE": "local"}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_only_marked_separate_absolute_copy_is_accepted(self):
        self.assertEqual(replay.validate_copy(self.root), self.root)
        for root in (Path("relative"), PROJECT, replay.LIVE_ROOT, self.root.parent):
            with self.subTest(root=root), self.assertRaises(replay.ReplayError):
                replay.validate_copy(root)
        self.marker.unlink()
        with self.assertRaisesRegex(replay.ReplayError, "marker_required"):
            replay.validate_copy(self.root)

    def test_live_or_ambiguous_marker_and_inherited_credentials_fail(self):
        self.marker.write_text(json.dumps(dict(version=replay.MARKER_VERSION,
            project_root=str(self.root), disposable=True, production=True)))
        with self.assertRaisesRegex(replay.ReplayError, "marker_invalid"):
            replay.validate_copy(self.root)
        self.marker.write_text(json.dumps(dict(version=replay.MARKER_VERSION,
            project_root=str(self.root), disposable=True, production=False)))
        with patch.dict(os.environ, {"API_FOOTBALL_KEY": "TEST-ONLY-PRIVATE"}):
            with self.assertRaisesRegex(replay.ReplayError, "private_environment_forbidden") as error:
                replay.validate_copy(self.root)
        self.assertNotIn("TEST-ONLY", str(error.exception))

    def test_symlinks_and_hardlinks_cannot_reach_live_files(self):
        link = self.root / "linked.py"
        link.symlink_to(PROJECT / "causal_candidate.py")
        with self.assertRaisesRegex(replay.ReplayError, "links_forbidden"):
            replay.validate_copy(self.root)
        link.unlink()
        original = self.root / "fixture.py"
        original.write_text("synthetic fixture\n")
        os.link(original, link)
        with self.assertRaisesRegex(replay.ReplayError, "hardlinks_forbidden"):
            replay.validate_copy(self.root)

    def test_private_profiles_and_copied_environment_are_rejected_without_reading(self):
        for relative in (".venv", ".aws", ".env.local", ".streamlit/secrets.toml"):
            with self.subTest(relative=relative):
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("TEST-ONLY-PRIVATE")
                with self.assertRaisesRegex(replay.ReplayError, "private_or_environment"):
                    replay.validate_copy(self.root)
                path.unlink()

    def test_uncheckpointed_sqlite_sidecars_are_refused(self):
        for name in ("fixture.sqlite3-wal", "fixture.sqlite3-shm", "fixture.sqlite3-journal"):
            path = self.root / name
            path.write_bytes(b"synthetic uncheckpointed state")
            with self.assertRaisesRegex(replay.ReplayError, "sqlite_snapshot_sidecars_forbidden"):
                replay.validate_copy(self.root)
            path.unlink()

    def test_audit_evidence_is_pinned_and_frozen_inventory_contains_exactly_22(self):
        saved = replay.reference(PROJECT)
        self.assertEqual(len(saved["sources"]), 22)
        self.assertEqual(set(saved["sources"]), replay.FROZEN_FILES)
        replay.verify_sources(PROJECT, saved)
        for name in (replay.PRESERVATION, replay.RECORDED_REPLAY):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(PROJECT / name, target)
        target = self.root / replay.PRESERVATION
        target.write_bytes(target.read_bytes() + b" ")
        with self.assertRaisesRegex(replay.ReplayError, "evidence_hash_mismatch"):
            replay.reference(self.root)

    def test_one_changed_frozen_source_fails_before_artifact_loading(self):
        sources = {}
        for name in replay.FROZEN_FILES:
            value = (name + "\n").encode()
            (self.root / name).write_bytes(value)
            sources[name] = sha256(value).hexdigest()
        saved = dict(sources=sources, candidate=dict(implementation_sha256={}))
        replay.verify_sources(self.root, saved)
        (self.root / "automation_worker.py").write_text("changed synthetic fixture\n")
        with self.assertRaisesRegex(replay.ReplayError, "frozen_source_hash_mismatch"):
            replay.verify_sources(self.root, saved)
        saved["sources"].pop("automation_worker.py")
        with self.assertRaisesRegex(replay.ReplayError, "22_inventory"):
            replay.verify_sources(self.root, saved)


class RegisteredReplayTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("/disposable-fixture")
        self.forecast_id = "1" * 64
        self.candidate_id = "2" * 64
        self.package_id = "3" * 64
        self.protocol_id = "4" * 64
        self.hashes = {"candidate.cbm": "5" * 64, "report.json": "6" * 64, "COMPLETE.json": "7" * 64}
        self.saved = dict(replay=dict(forecast_id=self.forecast_id), candidate=dict(
            candidate_id=self.candidate_id, package_id=self.package_id, protocol_id="8" * 64,
            artifact_sha256=self.hashes))
        self.expected = {"1st": 0.2, "Égal": 0.3, "2nd": 0.5}
        self.payload = dict(id=self.forecast_id, protocol_id=self.protocol_id, candidate_id=self.candidate_id,
            snapshot=dict(features={"synthetic_feature": 1}), methods=dict(candidate=self.expected))
        self.protocol = dict(id=self.protocol_id, candidate_id=self.candidate_id, package_id=self.package_id,
            candidate_files={name: self.hashes[name] for name in ("candidate.cbm", "report.json")})
        self.connection = Mock()
        self.connection.execute.return_value.fetchone.return_value = {"synthetic": "row"}
        self.store = SimpleNamespace(_connect=Mock(return_value=self.connection),
            _forecast=Mock(side_effect=lambda *_: {"payload": self.payload}),
            _protocol=Mock(side_effect=lambda *_: self.protocol))
        self.candidates = SimpleNamespace(load_candidate=Mock(return_value={"synthetic": "loaded"}),
            predict_candidate=Mock(return_value=[dict(self.expected)]))
        self.integrity = SimpleNamespace(inspect=Mock(return_value=dict(status="valid",
            candidate_id=self.candidate_id, package_id=self.package_id, artifact_sha256=self.hashes)))

    def execute(self):
        return replay.replay(self.root, self.saved, self.store, self.candidates, self.integrity)

    def test_exact_saved_row_and_its_original_registration_are_replayed(self):
        result = self.execute()
        self.connection.execute.assert_called_once_with("SELECT * FROM forecasts WHERE id=?", (self.forecast_id,))
        self.assertEqual(result["protocol_id"], self.protocol_id)
        self.assertNotEqual(result["protocol_id"], self.saved["candidate"]["protocol_id"])
        self.assertEqual(result["max_absolute_probability_difference"], 0)
        self.candidates.predict_candidate.assert_called_once_with({"synthetic": "loaded"}, [{"synthetic_feature": 1}])
        self.connection.close.assert_called_once()

    def test_missing_row_or_wrong_registration_fails_before_model_load(self):
        self.connection.execute.return_value.fetchone.return_value = None
        with self.assertRaisesRegex(replay.ReplayError, "forecast_missing"):
            self.execute()
        self.candidates.load_candidate.assert_not_called()
        self.connection.execute.return_value.fetchone.return_value = {"synthetic": "row"}
        self.protocol["package_id"] = "9" * 64
        with self.assertRaisesRegex(replay.ReplayError, "registration_mismatch"):
            self.execute()
        self.candidates.load_candidate.assert_not_called()

    def test_corrupt_registered_artifact_proof_fails_before_model_load(self):
        self.integrity.inspect.return_value["artifact_sha256"] = {**self.hashes, "candidate.cbm": "0" * 64}
        with self.assertRaisesRegex(replay.ReplayError, "integrity_failed"):
            self.execute()
        self.candidates.load_candidate.assert_not_called()

    def test_tolerance_nan_class_and_invalid_distributions_fail_closed(self):
        close = {"1st": 0.2 + 5e-13, "Égal": 0.3 - 5e-13, "2nd": 0.5}
        self.assertLessEqual(replay.compare_probabilities(close, self.expected), 1e-12)
        for actual in ({"1st": 0.2 + 2e-12, "Égal": 0.3 - 2e-12, "2nd": 0.5},
                       {"1st": float("nan"), "Égal": 0.3, "2nd": 0.5},
                       {"1st": True, "Égal": 0, "2nd": 0}, {"other": 1},
                       {"1st": 0.9, "Égal": 0.3, "2nd": 0.5}):
            with self.subTest(actual=actual), self.assertRaises(replay.ReplayError):
                replay.compare_probabilities(actual, self.expected)


class ProcessGuardTests(unittest.TestCase):
    def child(self, code, *arguments):
        # The permanent audit hook must stay inside an isolated test process.
        prefix = "import importlib.util,sys\nfrom pathlib import Path\np=Path(sys.argv[1])\ns=importlib.util.spec_from_file_location('replay_under_test',p)\nr=importlib.util.module_from_spec(s);s.loader.exec_module(r)\n"
        result = subprocess.run([sys.executable, "-I", "-B", "-c", prefix + code,
            str(PROJECT / "deployment/linux/frozen_replay.py"), *map(str, arguments)],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_audit_hook_blocks_writes_key_reads_network_and_process_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            private = root / "secrets.toml"
            private.write_text("TEST-ONLY-PRIVATE")
            output = self.child("""
import json,os,socket,subprocess
root=Path(sys.argv[2]);g=r.ReadOnlyGuard(root/'fixture.sqlite3');sys.addaudithook(g.audit)
operations=[lambda:(root/'forbidden.txt').write_text('bad'),lambda:os.mkdir(root/'new'),
            lambda:(root/'secrets.toml').read_text(),lambda:socket.socket(),
            lambda:socket.getaddrinfo('example.invalid',443),lambda:subprocess.run(['/bin/true'])]
for operation in operations:
    try:operation()
    except r.ReplayError:pass
    else:raise AssertionError('guard bypassed')
print(json.dumps(g.denied))
""", root)
            self.assertEqual(output, dict(network=2, credential=1, training=0, write=2, process=1))
            self.assertFalse((root / "forbidden.txt").exists())
            self.assertFalse((root / "new").exists())

    def test_sqlite_uses_immutable_readonly_uri_and_cannot_publish(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            database = root / "fixture.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE saved(value INTEGER)")
                connection.execute("INSERT INTO saved VALUES(7)")
            before = replay.fingerprint(database)
            output = self.child("""
import json,sqlite3
db=Path(sys.argv[2]);g=r.ReadOnlyGuard(db);original=sqlite3.connect;sys.addaudithook(g.audit)
con=g.sqlite_connect(original,db.as_uri()+'?mode=ro',uri=True)
assert con.execute('SELECT value FROM saved').fetchone()[0]==7
try:con.execute('INSERT INTO saved VALUES(8)')
except sqlite3.OperationalError:pass
else:raise AssertionError('writable database')
con.close()
try:original(db)
except r.ReplayError:pass
else:raise AssertionError('writable connection accepted')
print(json.dumps({'read_passed':True,'denied_writable_connections':g.denied['write']}))
""", database)
            self.assertEqual(output, dict(read_passed=True, denied_writable_connections=1))
            self.assertEqual(replay.fingerprint(database), before)
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["fixture.sqlite3"])

    def test_complete_guarded_flow_blocks_key_fit_and_model_publication(self):
        # Fake app modules exercise the actual run() guard installation. The
        # subprocess neither imports application source nor loads a real model.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "data/api_football").mkdir(parents=True)
            (root / "data/api_football/causal_confirmation.sqlite3").write_bytes(b"synthetic database")
            output = self.child("""
import json,types
from unittest.mock import Mock,patch
root=Path(sys.argv[2]);installed=[]
api=types.SimpleNamespace(load_key=lambda:None,ApiFootballClient=types.SimpleNamespace(get=lambda:None))
candidates=types.SimpleNamespace(fit_candidate=lambda:None,write_candidate=lambda:None)
class Base:
    def fit(self):pass
class Classifier(Base):pass
class Regressor(Base):pass
catboost=types.ModuleType('catboost');catboost.CatBoost=Base;catboost.CatBoostClassifier=Classifier
catboost.CatBoostRegressor=Regressor;catboost.train=lambda:None;catboost.cv=lambda:None
sys.modules['catboost']=catboost
modules={'api_football':api,'causal_candidate':candidates,
         'causal_confirmation_store':object(),'active_candidate_integrity':object()}
def runtime():
    assert installed, 'audit hook must precede native/runtime imports'
def attempt(*_args):
    calls=[api.load_key,candidates.fit_candidate,candidates.write_candidate,
           lambda:catboost.CatBoostClassifier().fit(),catboost.train,catboost.cv]
    denied=0
    for call in calls:
        try:call()
        except r.ReplayError:denied+=1
        else:raise AssertionError('unsafe operation permitted')
    assert denied==len(calls)
    return {}
with patch.object(r.sys,'addaudithook',side_effect=lambda hook:installed.append(hook)), \\
     patch.object(r,'runtime_check',side_effect=runtime),patch.object(r,'_module',side_effect=lambda name,_:modules[name]), \\
     patch.object(r,'verify_sources'),patch.object(r,'verify_artifacts'),patch.object(r,'replay',side_effect=attempt):
    try:r.run(root,{})
    except r.ReplayError as error:assert str(error)=='forbidden_operation_attempted'
    else:raise AssertionError('swallowed forbidden attempts reported success')
print(json.dumps({'key_fit_and_publication_blocked':True,'runtime_after_audit_guard':True}))
""", root)
            self.assertEqual(output, dict(key_fit_and_publication_blocked=True, runtime_after_audit_guard=True))

    def test_successful_guarded_flow_rechecks_snapshot_and_keeps_patches_scoped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "data/api_football").mkdir(parents=True)
            (root / "data/api_football/causal_confirmation.sqlite3").write_bytes(b"synthetic database")
            for name in ("api_football", "causal_candidate", "causal_confirmation_store", "active_candidate_integrity"):
                (root / (name + ".py")).write_text("# synthetic module origin\n")
            before = replay.snapshot_metadata(root)
            output = self.child("""
import json,types
from unittest.mock import Mock,patch
root=Path(sys.argv[2]);candidate='2'*64;package='3'*64;forecast='1'*64;protocol_id='4'*64
hashes={'candidate.cbm':'5'*64,'report.json':'6'*64,'COMPLETE.json':'7'*64}
saved={'candidate':{'candidate_id':candidate,'package_id':package,'artifact_sha256':hashes},
       'replay':{'forecast_id':forecast}}
expected={'1st':.2,'Égal':.3,'2nd':.5}
payload={'id':forecast,'candidate_id':candidate,'protocol_id':protocol_id,
         'snapshot':{'features':{'synthetic':1}},'methods':{'candidate':expected}}
protocol={'id':protocol_id,'candidate_id':candidate,'package_id':package,
          'candidate_files':{k:hashes[k] for k in ('candidate.cbm','report.json')}}
def module(name):
    value=types.ModuleType(name);value.__file__=str(root/(name+'.py'));sys.modules[name]=value;return value
api=module('api_football');original_key=lambda:None;api.load_key=original_key
api.ApiFootballClient=types.SimpleNamespace(get=lambda:None)
candidates=module('causal_candidate');original_fit=lambda:None;candidates.fit_candidate=original_fit
candidates.write_candidate=lambda:None;candidates.load_candidate=Mock(return_value={'synthetic':'model'})
candidates.predict_candidate=Mock(return_value=[expected])
store=module('causal_confirmation_store');con=Mock();con.execute.return_value.fetchone.return_value={'synthetic':'row'}
store._connect=Mock(return_value=con);store._forecast=lambda *_:{'payload':payload};store._protocol=lambda *_:protocol
integrity=module('active_candidate_integrity')
integrity.inspect=lambda *_:{'status':'valid','candidate_id':candidate,'package_id':package,'artifact_sha256':hashes}
catboost=types.ModuleType('catboost')
class Base:
    def fit(self):pass
class Classifier(Base):pass
class Regressor(Base):pass
catboost.CatBoost=Base;catboost.CatBoostClassifier=Classifier;catboost.CatBoostRegressor=Regressor
catboost.train=lambda:None;catboost.cv=lambda:None;sys.modules['catboost']=catboost
with patch.object(r,'runtime_check') as runtime,patch.object(r,'verify_sources') as sources,patch.object(r,'verify_artifacts') as artifacts:
    result=r.run(root,saved)
    assert sources.call_count==2 and artifacts.call_count==2
    runtime.assert_called_once()
assert api.load_key is original_key and candidates.fit_candidate is original_fit
assert candidates.load_candidate.call_count==1 and candidates.predict_candidate.call_count==1
assert not any(result['denied_operations'].values())
print(json.dumps(result))
""", root)
            self.assertEqual(output["state"], "passed")
            self.assertEqual(output["frozen_source_files_verified"], 22)
            self.assertEqual(output["max_absolute_probability_difference"], 0)
            self.assertFalse(output["new_forecast_published"])
            self.assertTrue(output["database_sha256_unchanged"])
            self.assertEqual(replay.snapshot_metadata(root), before)

    def test_missing_isolation_is_rejected_before_copy_or_application_reads(self):
        output = io.StringIO()
        with patch.object(replay.sys, "flags", SimpleNamespace(isolated=0)), \
                patch.object(replay, "validate_copy") as validate, redirect_stdout(output):
            self.assertEqual(replay.main(["--copy-root", "/unused"]), 1)
        validate.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["reason"], "isolated_python_and_no_bytecode_required")

    def test_arbitrary_failure_is_redacted_and_not_marked_qualified(self):
        output = io.StringIO()
        with patch.object(replay.sys, "flags", SimpleNamespace(isolated=1)), \
                patch.object(replay.sys, "dont_write_bytecode", True), patch.object(replay.os, "geteuid", return_value=1000), \
                patch.object(replay, "validate_copy", return_value=Path("/synthetic-copy")), \
                patch.object(replay, "reference", return_value={}), patch.object(replay, "verify_sources"), \
                patch.object(replay, "verify_artifacts"), \
                patch.object(replay, "run", side_effect=ValueError("TEST-ONLY-PRIVATE")), redirect_stdout(output):
            self.assertEqual(replay.main(["--copy-root", "/synthetic-copy"]), 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result["state"], "failed")
        self.assertNotIn("TEST-ONLY", output.getvalue())
        self.assertNotIn("linux_host_qualification", result)


if __name__ == "__main__":
    unittest.main()
