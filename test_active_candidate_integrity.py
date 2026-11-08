from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import active_candidate_integrity as integrity
import causal_candidate as candidates
import test_causal_candidate as candidate_helpers


def sealed(value):
    value=dict(value);value['id']=candidates.digest(value);return value


class ActiveCandidateIntegrityTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.report=candidate_helpers.CausalCandidateTests().fitted_unit()[2]
        self.cid=self.report['id'];self.directory=self.root/'causal_confirmation/candidates'/self.cid
        self.directory.mkdir(parents=True);(self.directory/'candidate.cbm').write_bytes(b'opaque-test-binary-not-loaded')
        (self.directory/'report.json').write_text(json.dumps(self.report))
        files={name:sha256((self.directory/name).read_bytes()).hexdigest() for name in integrity.FILES}
        self.package=sealed(dict(version='causal-candidate-bundle-v1',state='complete',candidate_id=self.cid,files=files))
        (self.directory/'COMPLETE.json').write_text(json.dumps(self.package))
        completed=datetime.fromisoformat(self.report['completed_at'])
        self.protocol=dict(id='a'*64,candidate_id=self.cid,package_id=self.package['id'],candidate_files=files,
            candidate_completed_at=self.report['completed_at'],registered_at=(completed+timedelta(seconds=1)).isoformat())

    def test_valid_bytes_and_metadata_without_model_load_fit_or_api(self):
        with patch.object(candidates.ml,'_classifier',side_effect=AssertionError('No CatBoost load')), \
             patch.object(candidates,'fit_candidate',side_effect=AssertionError('No fit')):
            r=integrity.inspect(self.root,self.protocol)
        self.assertEqual(r['status'],'valid');self.assertFalse(r['model_loaded']);self.assertFalse(r['model_inference_checked'])
        self.assertEqual(r['source_sha256'],integrity.inspect(self.root,self.protocol)['source_sha256'])

    def test_missing_binary_cannot_be_valid(self):
        (self.directory/'candidate.cbm').unlink()
        self.assertEqual(integrity.inspect(self.root,self.protocol)['status'],'missing')

    def test_corrupt_binary_is_invalid_with_safe_error(self):
        (self.directory/'candidate.cbm').write_bytes(b'private-untrusted-text')
        r=integrity.inspect(self.root,self.protocol)
        self.assertEqual(r['status'],'invalid');self.assertIn('candidate_artifact_hash_mismatch',r['issues'])
        self.assertNotIn('private-untrusted-text',json.dumps(r))

    def test_wrong_package_id_or_candidate_field_paths_are_rejected(self):
        for change in (dict(package_id='f'*64),dict(candidate_id='../outside'),dict(candidate_files={'../candidate.cbm':'a'*64,'report.json':'b'*64})):
            value=dict(self.protocol,**change)
            self.assertEqual(integrity.inspect(self.root,value)['status'],'invalid')

    def test_symlink_artifact_rejected_even_with_same_bytes(self):
        file=self.directory/'candidate.cbm';other=self.root/'same.bin';file.rename(other);file.symlink_to(other)
        self.assertEqual(integrity.inspect(self.root,self.protocol)['status'],'invalid')

    def test_resealed_package_cannot_change_protocol_bound_hashes(self):
        package=deepcopy(self.package);package.pop('id');package['files']['candidate.cbm']='0'*64
        (self.directory/'COMPLETE.json').write_text(json.dumps(sealed(package)))
        self.assertEqual(integrity.inspect(self.root,self.protocol)['status'],'invalid')


if __name__=='__main__':unittest.main()
