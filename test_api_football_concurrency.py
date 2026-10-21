"""Provider counters and read/merge/replace locking, on isolated files only."""
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event, Lock
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import api_football as api
from test_api_football import fixture


class ConcurrentStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)/'nested'/'fixtures.json'

    def test_concurrent_threads_preserve_all_fixtures_and_season_manifests(self):
        def write(index):
            season = 2020+index
            rows = [fixture(index*100+i+1, season=season) for i in range(4)]
            marker = dict(competition_code='61', season=season,
                          match_ids=[row['fixture']['id'] for row in rows])
            return api.save_fixtures(rows, self.path, season_imports=[marker])
        with ThreadPoolExecutor(max_workers=8) as pool:
            counts = list(pool.map(write, range(8)))
        self.assertEqual(len(api.load_fixtures(self.path)), 32)
        self.assertEqual(len(api.load_season_imports(self.path)), 8)
        self.assertEqual(sorted(counts), list(range(4, 33, 4)))

    def test_other_writer_cannot_read_an_old_snapshot_while_first_merge_holds_lock(self):
        entered, release, second_read = Event(), Event(), Event()
        count, guard = [0], Lock()
        original = api._load_store
        def load(path):
            with guard:
                count[0] += 1
                index = count[0]
            if index == 1:
                entered.set()
                if not release.wait(3):
                    raise AssertionError('First writer was not released')
            else:
                second_read.set()
            return original(path)
        with patch.object(api, '_load_store', side_effect=load), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(api.save_fixtures, [fixture(1)], self.path)
            self.assertTrue(entered.wait(2))
            second = pool.submit(api.save_fixtures, [fixture(2)], self.path)
            try:
                self.assertFalse(second_read.wait(.1))
            finally:
                release.set()
            first.result(timeout=3)
            second.result(timeout=3)
        self.assertEqual({r['fixture']['id'] for r in api.load_fixtures(self.path)}, {1,2})

    def test_independent_processes_share_the_same_stable_lock(self):
        script = ('import sys; import api_football as api; from test_api_football import fixture; '
                  'api.save_fixtures([fixture(int(sys.argv[2]))], sys.argv[1])')
        processes = [subprocess.Popen([sys.executable, '-c', script, str(self.path), str(i+1)],
                        cwd=Path(__file__).resolve().parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                     for i in range(6)]
        for process in processes:
            _, errors = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, errors.decode())
        self.assertEqual({r['fixture']['id'] for r in api.load_fixtures(self.path)}, set(range(1,7)))

    def test_failed_merge_releases_lock_and_keeps_previous_file(self):
        api.save_fixtures([fixture(1)], self.path)
        original = self.path.read_bytes()
        with patch.object(api.os, 'replace', side_effect=OSError('write failure')):
            with self.assertRaises(api.ApiFootballError):
                api.save_fixtures([fixture(2)], self.path)
        self.assertEqual(self.path.read_bytes(), original)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(api.save_fixtures, [fixture(3)], self.path).result(timeout=3)
        self.assertEqual({r['fixture']['id'] for r in api.load_fixtures(self.path)}, {1,3})
        self.assertTrue(self.path.with_name('.fixtures.json.lock').exists())
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])


class QuotaHeaderTests(unittest.TestCase):
    def setUp(self):
        self.client = api.ApiFootballClient('synthetic-api-key')

    def response(self, headers=None):
        response = io.BytesIO(json.dumps(dict(errors=[], response=[])).encode())
        if headers is not None:
            response.headers = headers
        return response

    def test_success_captures_only_safe_case_insensitive_nonnegative_counters(self):
        response = self.response({'X-RateLimit-Requests-Limit':'7500', 'x-ratelimit-requests-remaining':' 7498 ',
            'X-RATELIMIT-REMAINING':'298', 'Retry-After':'12', 'x-private-token':'synthetic-api-key'})
        with patch.object(api, 'urlopen', return_value=response):
            self.client.fixtures(date='2026-10-18')
        self.assertEqual(self.client.last_quota, dict(requests_limit_day=7500, requests_remaining_day=7498,
            requests_available_minute=298, reset_seconds=12))
        self.assertNotIn('synthetic-api-key', str(self.client.last_quota))

    def test_http_error_still_records_zero_remaining_without_leaking_headers(self):
        error = HTTPError('https://example.test',429,'synthetic-api-key',
            {'x-ratelimit-requests-remaining':'0','x-ratelimit-remaining':'0','retry-after':'60'},None)
        with patch.object(api, 'urlopen', side_effect=error):
            with self.assertRaises(api.ApiFootballError) as caught:
                self.client.fixtures()
        self.assertEqual(self.client.last_quota, dict(requests_remaining_day=0,requests_available_minute=0,reset_seconds=60))
        self.assertNotIn('synthetic-api-key', str(caught.exception))

    def test_missing_malformed_and_network_error_headers_clear_previous_counters(self):
        self.client.last_quota = {'requests_remaining_day':10}
        with patch.object(api, 'urlopen', return_value=self.response()):
            self.client.fixtures()
        self.assertEqual(self.client.last_quota,{})
        with patch.object(api, 'urlopen', return_value=self.response({
            'x-ratelimit-requests-limit':'-1','x-ratelimit-requests-remaining':True,
            'x-ratelimit-remaining':'3.5','retry-after':'Wed, 21 Oct 2026 07:28:00 GMT'})):
            self.client.fixtures()
        self.assertEqual(self.client.last_quota,{})
        self.client.last_quota = {'requests_remaining_day':10}
        with patch.object(api, 'urlopen', side_effect=URLError('offline')):
            with self.assertRaises(api.ApiFootballError): self.client.fixtures()
        self.assertEqual(self.client.last_quota,{})


if __name__ == '__main__':
    unittest.main()
