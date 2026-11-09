"""The online patch may replace only the four reviewed trend sources."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('club_period_install',
    Path(__file__).parent / 'deployment/linux/install_club_trends_period.py')
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)


class ClubPeriodInstallTests(unittest.TestCase):
    def test_exact_four_sources_required_and_live_baselines_checked(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(install.base, 'ROOT', Path(directory)):
            root = Path(directory)
            payload = {name: b'# new reviewed source\n' for name in install.SOURCES}
            previous = {}
            for name in payload:
                (root / name).write_bytes(b'# reviewed baseline\n')
                previous[name] = install.base.digest(root / name)
            def bundle(values):
                manifest = {'sha256': {name: hashlib.sha256(data).hexdigest() for name, data in values.items()},
                            'previous_sha256': {name: previous.get(name) for name in values}}
                path = root / 'patch.tar.gz'
                with tarfile.open(path, 'w:gz') as archive:
                    for name, data in {**values, 'manifest.json': json.dumps(manifest).encode()}.items():
                        info = tarfile.TarInfo(name)
                        info.size = len(data)
                        archive.addfile(info, io.BytesIO(data))
                return path, install.base.digest(path)
            path, sha = bundle(payload)
            self.assertEqual(install.load_bundle(path, sha)[1], payload)
            self.assertEqual(set(install.SOURCES), {'club_trends.py', 'club_trends_day.py',
                                                   'club_trends_panel.py', 'club_recent_sequences.py'})
            for extra in ('app.py', 'prediction_journal.py', '../outside.py', '.streamlit/secrets.toml'):
                with self.subTest(extra=extra), self.assertRaisesRegex(RuntimeError, 'source scope'):
                    install.load_bundle(*bundle({**payload, extra: b'# forbidden\n'}))
            path, sha = bundle(payload)
            (root / 'club_trends.py').write_bytes(b'# independent edit\n')
            with self.assertRaisesRegex(RuntimeError, 'reviewed baseline'):
                install.load_bundle(path, sha)


if __name__ == '__main__':
    unittest.main()
