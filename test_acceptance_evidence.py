from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

import acceptance_evidence as evidence


def write_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value),encoding='utf-8')
    return sha256(path.read_bytes()).hexdigest()


def fixture(root,lot):
    """Portable synthetic receipts; no private backup or live journal needed."""
    audit=root/'audit';archive=audit/f'{lot}_sources';archive.mkdir(parents=True)
    sources={'README_MAC.md':'archived documentation','automation_worker.py':'archived worker'}
    source_hash={}
    for name,value in sources.items():
        p=archive/name;p.write_text(value);source_hash[name]=sha256(p.read_bytes()).hexdigest()
    log=archive/'tests.log';log.write_text('.....\nRan 5 tests in 0.123s\n\nOK\n')
    tests=dict(version=f'{lot}-tests-v1',checked_at='2026-11-02T17:40:00Z',passed=5,failures=0,ignored=0,
               py_compile_pass=True,diff_check_pass=True,log_path=log.relative_to(root).as_posix(),
               log_sha256=sha256(log.read_bytes()).hexdigest())
    receipt=dict(version=f'patternlab2-delivery-{lot}-v1',state=evidence.EXPECTED[lot],
                 received_at='2026-11-02T18:00:00Z',sources_archive=archive.relative_to(root).as_posix(),
                 sources_sha256=source_hash,tests=tests,evidence_sha256={})
    for key in ('full_program_complete','predictive_improvement_demonstrated','active_model_promoted',
                'candidate_weights_changed','old_forecasts_rewritten'):receipt[key]=False
    refs={f'PATTERNLAB2_TESTS_{lot.upper()}_2026-11-02.json':tests}
    if lot=='lot11':
        period=dict(start='2026-11-09T00:00:00Z',end_exclusive='2027-01-04T00:00:00Z',result_cutoff='2027-01-11T08:00:00Z')
        protocol=dict(id='a'*64,candidate_id='b'*64,candidate_files={'candidate.cbm':'c'*64,'report.json':'d'*64},
                      implementation={'automation_worker.py':source_hash['automation_worker.py']},
                      registered_at='2026-11-02T17:00:00Z',confirmation_start=period['start'],
                      confirmation_end=period['end_exclusive'],followup={'result_cutoff_utc':period['result_cutoff']})
        ack=dict(protocol_id=protocol['id'],kind='registration_ack',time_quality='local_registration_ack',observed_at_utc='2026-11-02T17:00:01Z')
        receipt.update(new_protocol_id='a'*64,old_protocol_id='e'*64,old_window_changed=False,old_missed_window_backfilled=False,
            old_protocol_implementation_compatible=False,new_protocol_implementation_compatible=True,
            scheduler_fix='civil-or-monotonic-refresh-v2',candidate_id='b'*64,
            candidate_files_unchanged=protocol['candidate_files'],new_registered_at=protocol['registered_at'],
            new_registration_ack_at=ack['observed_at_utc'],new_period=period,
            activation=dict(real_calendar_fetch_observed=True,new_cycle_id='f'*32,new_cycle_started_at='2026-11-02T17:01:00Z'))
        refs.update({
            'PATTERNLAB2_INSCRIPTION_LOT11_2026-11-02.json':dict(new_protocol=protocol,registration_ack=ack,
                old_payload_unchanged=True,candidate_unchanged=True,old_protocol_id='e'*64),
            'PATTERNLAB2_SERVICE_LOT11_2026-11-02.json':dict(real_calendar_fetch_observed=True,
                new_cycle=dict(id='f'*32,started_at='2026-11-02T17:01:00Z'),
                completed_operations=[['2026-11-02T17:02:00Z','2026-11-02','calendar']])})
    else:
        verified=dict(version='local-recovery-v1',state='verified',known_private_paths_excluded=True,
            embedded_secret_content_audited=False,files_verified=3,databases_restored=['data/api_football/fixtures.sqlite3'],verified_at='2026-11-02T17:03:00Z')
        domains=[dict(component=name,required=required,state='passed',items=1) for name,required in evidence.DOMAINS.items()]
        receipt.update(backup_files=3,databases_restored=1,application_rehearsal=domains,
            backup_is_global_atomic_snapshot=False,full_service_disaster_recovery_demonstrated=False,security=dict(backup_off_machine=False))
        refs.update({
            'PATTERNLAB2_SAUVEGARDE_LOT12_2026-11-02.json':dict(verification=verified,manifest_sha256='a'*64,
                atomic_across_databases=False,off_machine=False,started_at='2026-11-02T17:00:00Z',completed_at='2026-11-02T17:01:00Z'),
            'PATTERNLAB2_RESTAURATION_APPLICATION_LOT12_2026-11-02.json':dict(version='local-recovery-rehearsal-v1',state='passed',
                verification=verified,recovery_manifest_sha256='a'*64,production_restored=False,api_calls_allowed=False,
                credential_access_allowed=False,model_fit_requested=False,domain_checks=domains,checked_at='2026-11-02T17:04:00Z')})
    for name,value in refs.items():receipt['evidence_sha256']['audit/'+name]=write_json(audit/name,value)
    path=audit/f'PATTERNLAB2_RECEPTION_{lot.upper()}_2026-11-02.json';write_json(path,receipt)
    return path,receipt


class AcceptanceEvidenceTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)

    def test_missing_receipts_never_create_files_or_zero_tests(self):
        r=evidence.load(self.root)
        self.assertEqual([x['state'] for x in r['rows']],['missing','missing'])
        self.assertTrue(all(x['tests_passed'] is None for x in r['rows']))
        self.assertFalse(list(self.root.iterdir()))

    def test_both_valid_historical_receipts_do_not_depend_on_current_documentation(self):
        fixture(self.root,'lot11');fixture(self.root,'lot12')
        (self.root/'README_MAC.md').write_text('changed after delivery')
        r=evidence.load(self.root)
        self.assertEqual([x['state'] for x in r['rows']],['verified','verified'])
        self.assertEqual(r['rows'][1]['facts']['application_readers_passed'],6)
        self.assertFalse(r['backup_current_checked']);self.assertFalse(r['tests_rerun'])
        self.assertFalse(r['receipts_signed'])
        self.assertEqual(r['source_sha256'],evidence.load(self.root)['source_sha256'])

    def test_archived_source_tamper_is_invalid_and_no_untrusted_text_exported(self):
        fixture(self.root,'lot11');p=self.root/'audit/lot11_sources/README_MAC.md';p.write_text('private-token')
        r=evidence.load(self.root)
        self.assertEqual(r['rows'][0]['state'],'invalid')
        self.assertNotIn('private-token',json.dumps(r));self.assertIsNone(r['rows'][0]['tests_passed'])

    def test_evidence_and_log_tampering_are_rejected(self):
        path,receipt=fixture(self.root,'lot12')
        for rel in ['audit/lot12_sources/tests.log','audit/PATTERNLAB2_TESTS_LOT12_2026-11-02.json']:
            target=self.root/rel;old=target.read_bytes();target.write_text('corrupt')
            with self.subTest(rel=rel):self.assertEqual(evidence.load(self.root)['rows'][1]['state'],'invalid')
            target.write_bytes(old)

    def test_traversal_absolute_and_symlink_paths_are_rejected(self):
        path,r=fixture(self.root,'lot11')
        for value in ('../outside','/tmp/outside','audit/../lot11_sources','audit\\lot11_sources'):
            changed=deepcopy(r);changed['sources_archive']=value;write_json(path,changed)
            with self.subTest(value=value):self.assertEqual(evidence.load(self.root)['rows'][0]['state'],'invalid')
        write_json(path,r)
        source=self.root/'audit/lot11_sources/README_MAC.md';original=source.read_text();source.unlink()
        other=self.root/'other.md';other.write_text(original);source.symlink_to(other)
        self.assertEqual(evidence.load(self.root)['rows'][0]['state'],'invalid')

    def test_bool_test_count_and_inconsistent_verified_facts_are_not_accepted(self):
        path,r=fixture(self.root,'lot12')
        for mutate in (lambda x:x['tests'].update(passed=True),lambda x:x.update(databases_restored=7),
                       lambda x:x.update(predictive_improvement_demonstrated=True)):
            changed=deepcopy(r);mutate(changed);write_json(path,changed)
            self.assertEqual(evidence.load(self.root)['rows'][1]['state'],'invalid')

    def test_mismatched_log_count_even_with_updated_hash_is_invalid(self):
        path,r=fixture(self.root,'lot11');log=self.root/'audit/lot11_sources/tests.log'
        log.write_text('Ran 100 tests in 1.2s\n\nOK\n');r['tests']['log_sha256']=sha256(log.read_bytes()).hexdigest()
        r['evidence_sha256']['audit/PATTERNLAB2_TESTS_LOT11_2026-11-02.json']=write_json(self.root/'audit/PATTERNLAB2_TESTS_LOT11_2026-11-02.json',r['tests'])
        write_json(path,r);self.assertEqual(evidence.load(self.root)['rows'][0]['state'],'invalid')

    def test_duplicate_json_keys_and_oversized_receipt_fail_closed(self):
        path,_=fixture(self.root,'lot11');path.write_text('{"state":"x","state":"y"}')
        self.assertEqual(evidence.load(self.root)['rows'][0]['state'],'invalid')
        path.write_bytes(b' '* (evidence.MAX_FILE_BYTES+1))
        self.assertEqual(evidence.load(self.root)['rows'][0]['state'],'invalid')


if __name__=='__main__':unittest.main()
