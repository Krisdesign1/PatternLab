import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from test_api_football import fixture
from api_football import normalize_fixture

SPEC = importlib.util.spec_from_file_location('verify_halftime_contract',
                                             Path(__file__).parent/'scripts/verify_halftime_contract.py')
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


class ContractReceiptTests(unittest.TestCase):
    def test_checks_original_raw_projection_and_label_without_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder)/'fixtures.sqlite3'
            raw = fixture()
            with sqlite3.connect(database) as con:
                con.execute('CREATE TABLE fixtures(match_id TEXT,normalized_json TEXT,raw_json TEXT)')
                con.execute('INSERT INTO fixtures VALUES(?,?,?)',
                            ('123',json.dumps(normalize_fixture(raw)),json.dumps(raw)))
            first = CLI.verify(database)
            self.assertTrue(first['passed'])
            self.assertEqual(first['counts']['2nd'],1)
            changed = normalize_fixture(raw); changed['value'] = '1st'
            with sqlite3.connect(database) as con:
                con.execute('UPDATE fixtures SET normalized_json=?',(json.dumps(changed),))
            second = CLI.verify(database)
            self.assertFalse(second['passed'])
            self.assertEqual(second['failures'][0]['match_id'],'123')
            self.assertTrue(second['database_unchanged'])


if __name__=='__main__':
    unittest.main()
