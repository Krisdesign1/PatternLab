from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import causal_candidate
import scenario_group_audit as audit
from test_scenario_group_contract import entry


class GroupAuditTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.path=Path(temporary.name)/'journal.sqlite3'

    def save(self, value, identity='group'):
        value=deepcopy(value);created=value.pop('created_at');value.pop('id',None)
        encoded=json.dumps(value,ensure_ascii=False,sort_keys=True)
        with sqlite3.connect(self.path) as con:
            con.execute('CREATE TABLE IF NOT EXISTS predictions(id TEXT PRIMARY KEY,created_at TEXT,payload TEXT,digest TEXT)')
            con.execute('INSERT INTO predictions VALUES(?,?,?,?)',(identity,created,encoded,sha256(encoded.encode()).hexdigest()))

    def test_missing_path_does_not_create_database_or_results(self):
        value=audit.load_report(self.path)
        self.assertEqual(value['counts']['total'],0)
        self.assertFalse(self.path.exists())

    def test_valid_legacy_and_contradictory_forecasts_are_reported_separately(self):
        self.save(entry(2),'valid')
        legacy=entry(3)
        for payload in legacy['methods'].values():
            for field in ('decision_at','ordered_match_ids','fixture_fingerprint'):payload.pop(field,None)
        self.save(legacy,'legacy')
        invalid=entry(4);invalid['methods']['clubs']['training_before']='2026-11-02'
        self.save(invalid,'invalid')
        value=audit.load_report(self.path)
        self.assertEqual({key:value['counts'][key] for key in ('total','valid','unverified','invalid')},
                         {'total':3,'valid':1,'unverified':1,'invalid':1})
        self.assertTrue(value['counts']['issue_codes'])
        self.assertEqual({r['id'] for r in value['rows']},{'valid','legacy','invalid'})
        self.assertNotIn('distribution',json.dumps(value))

    def test_read_only_stable_fingerprint_ignores_result_revisions(self):
        self.save(entry())
        before=self.path.read_bytes()
        with patch.object(api.ApiFootballClient,'get',side_effect=AssertionError('No API')), \
             patch.object(causal_candidate,'fit_candidate',side_effect=AssertionError('No fit')):
            one=audit.load_report(self.path)
        self.assertEqual(self.path.read_bytes(),before)
        with sqlite3.connect(self.path) as con:
            con.execute('CREATE TABLE results(payload TEXT)')
            con.execute('INSERT INTO results VALUES(?)',('new result',))
        two=audit.load_report(self.path)
        self.assertEqual(one['source_sha256'],two['source_sha256'])
        self.assertEqual(one['rows'],two['rows'])

    def test_tampered_payload_never_becomes_partial_success(self):
        self.save(entry())
        with sqlite3.connect(self.path) as con:
            con.execute("UPDATE predictions SET payload='{}'")
        with self.assertRaisesRegex(ValueError,'altérée'):audit.load_report(self.path)

    def test_bad_schema_and_index_contradiction_fail_closed(self):
        with sqlite3.connect(self.path) as con:con.execute('CREATE TABLE wrong(x TEXT)')
        with self.assertRaises(ValueError):audit.load_report(self.path)
        self.path.unlink()
        value=entry();value['id']='not-group'
        self.save(value)
        with sqlite3.connect(self.path) as con:
            raw=con.execute('SELECT payload FROM predictions').fetchone()[0]
            obj=json.loads(raw);obj['id']='different'
            raw=json.dumps(obj)
            con.execute('UPDATE predictions SET payload=?,digest=?',(raw,sha256(raw.encode()).hexdigest()))
        with self.assertRaisesRegex(ValueError,'index'):audit.load_report(self.path)


if __name__=='__main__':unittest.main()
