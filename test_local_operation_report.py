from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import io
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import automation_health as health
import local_operation_report as observation
from scripts import observe_local_operation as cli
from test_probability_reliability import entry


NOW = datetime(2026,10,24,8,tzinfo=timezone.utc)


class LocalOperationReportTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def report(self, **kwargs):
        return observation.build(now=NOW,days=7,**kwargs)

    def test_empty_read_creates_nothing_and_never_claims_uptime_or_complete_monitoring(self):
        with patch('api_football.ApiFootballClient',side_effect=AssertionError('No API')), \
                patch('journal_protocol.register_protocol',side_effect=AssertionError('No registration')):
            report = observation.collect(self.root,now=NOW)
        self.assertEqual(list(self.root.iterdir()),[])
        self.assertEqual(report['component_states'],dict(automation='missing',journal='missing',fixtures='missing',protocol='missing',day_service='missing',refresh_events='missing'))
        self.assertEqual(report['daily'][-1]['day'],'2026-10-24')
        self.assertTrue(report['daily'][-2]['closed_calendar_day'])
        self.assertFalse(report['daily'][-1]['closed_calendar_day'])
        self.assertFalse(any(day['monitoring_began_before_day'] for day in report['daily']))
        self.assertNotIn('uptime',report['automation'])
        self.assertTrue(report['limitations'])

    def test_overlapping_groups_and_repeated_inputs_never_inflate_unique_matches(self):
        first = entry(identity='first',status='pending'); first['observed']=[]
        later = entry(identity='later',created='2026-10-20T11:00:00Z')
        before = deepcopy([later,first,first])
        report = self.report(entries=[later,first,first])
        self.assertEqual(report['journal']['entries'],2)
        self.assertEqual(report['journal']['duplicate_entry_inputs'],1)
        self.assertEqual(report['journal']['unique_matches'],2)
        self.assertEqual(report['journal']['match_appearances'],4)
        self.assertEqual(report['journal']['repeated_match_appearances'],2)
        day = next(row for row in report['daily'] if row['day']=='2026-10-20')
        self.assertEqual(day['forecast_unique_matches'],2)
        self.assertEqual(day['result_states'],{'pending':2})
        self.assertEqual(day['forecasts_before_kickoff'],2)
        self.assertEqual(day['forecast_lead_seconds_min'],7200)
        self.assertEqual([later,first,first],before)
        self.assertNotIn('failed',str(day))

    def test_naive_utc_fixture_dates_are_grouped_in_requested_local_day(self):
        original=entry(status='unresolved'); original['observed']=[]
        original['fixtures'][0]['timestamp']='2026-10-19T23:30:00'
        original['fixtures'][1]['timestamp']='2026-10-20T00:30:00'
        original['created_at']='2026-10-19T23:45:00Z'
        report=self.report(entries=[original])
        day=next(row for row in report['daily'] if row['day']=='2026-10-20')
        self.assertEqual((day['forecast_unique_matches'],day['forecasts_before_kickoff'],day['forecasts_not_before_kickoff']),(2,1,1))
        self.assertEqual(day['result_states'],{'excluded':2})
        self.assertEqual(day['result_exclusions'],{'missing_or_unresolved_result':2})

    def test_future_forecasts_and_results_are_not_reported_as_observed(self):
        future=entry(identity='future',created='2026-10-25T00:00:00Z')
        current=entry(); current['checked_at']='2026-10-25T00:00:00Z'
        report=self.report(entries=[current,future])
        self.assertEqual(report['journal']['future_entry_inputs'],1)
        day=next(row for row in report['daily'] if row['day']=='2026-10-20')
        self.assertEqual(day['result_exclusions'],{'observation_after_report':2})

    def test_multiple_days_stale_signal_is_retained_as_unknown_activity_not_an_inferred_failure(self):
        start=NOW-timedelta(days=2)
        report=dict(started_at=start.isoformat(),status='completed',requests=3,issues=[])
        with health.monitor(self.root,report,clock=lambda:start,interval_minutes=15,origin='service') as pulse:
            pulse.progress('history')
            output=observation.collect(self.root,now=NOW,days=4,include_evidence=False)
            running=output['automation']['running'][0]
            self.assertEqual((running['phase'],running['signal_state']),('history','stale'))
            self.assertEqual(running['counters'],{'requests':3})
            self.assertEqual(output['daily'][1]['cycle_states'],{'running':1})
            self.assertEqual(output['daily'][2]['cycles_started'],0)
            self.assertTrue(output['daily'][2]['monitoring_began_before_day'])
            self.assertNotIn('downtime',output['automation'])

    def test_calendar_age_is_last_snapshot_age_not_a_score_freshness_claim(self):
        fixtures=dict(state='available',revision=3,updated_at=NOW.isoformat(),
            rows=[dict(match_id='1',kickoff_utc='2026-10-24T07:00:00',status='FT',usable=1),
                  dict(match_id='2',kickoff_utc='2026-10-24T12:00:00',status='NS',usable=0)],
            calendar_updates={('2026-10-24','Africa/Kinshasa'):(NOW-timedelta(minutes=11)).isoformat()})
        before=deepcopy(fixtures)
        with patch('builtins.open',side_effect=AssertionError('Pure builder performs no I/O')), \
                patch('sqlite3.connect',side_effect=AssertionError('Pure builder performs no I/O')):
            report=self.report(fixture_snapshot=fixtures)
        day=report['daily'][-1]
        self.assertEqual((day['calendar_matches'],day['calendar_usable_results']),(2,1))
        self.assertEqual(day['calendar_refresh_age_seconds'],660)
        self.assertEqual(fixtures,before)
        self.assertNotIn('scores_fresh',day)

    def seed_journal(self,values):
        with sqlite3.connect(self.root/'predictions.sqlite3') as con:
            con.executescript('''CREATE TABLE predictions (id TEXT PRIMARY KEY,created_at,payload,digest);
                CREATE TABLE results (prediction_id,revision,checked_at,payload,PRIMARY KEY(prediction_id,revision));''')
            for value in values:
                payload=json.dumps({key:item for key,item in value.items()
                                    if key not in ('id','created_at','checked_at','status','observed')})
                con.execute('INSERT INTO predictions VALUES (?,?,?,?)',
                            (value['id'],value['created_at'],payload,sha256(payload.encode()).hexdigest()))
                for revision,status in ((1,'pending'),(2,value['status'])):
                    result=json.dumps(dict(status=status,observed=value['observed'] if revision==2 else []))
                    con.execute('INSERT INTO results VALUES (?,?,?,?)',(value['id'],revision,value['checked_at'],result))

    def test_real_local_read_is_readonly_validates_digests_and_reads_latest_revision(self):
        self.seed_journal([entry()])
        target=self.root/'predictions.sqlite3'; before=target.read_bytes()
        output=observation.collect(self.root,now=NOW,include_evidence=False)
        self.assertEqual(output['journal']['group_states'],{'settled':1})
        self.assertEqual(next(row for row in output['daily'] if row['day']=='2026-10-20')['result_states'],{'evaluated':2})
        self.assertEqual(target.read_bytes(),before)
        self.assertEqual(set(path.name for path in self.root.iterdir()),{'predictions.sqlite3'})
        with sqlite3.connect(target) as con:
            con.execute("UPDATE predictions SET digest='invalid'")
        before=target.read_bytes()
        output=observation.collect(self.root,now=NOW,include_evidence=False)
        self.assertEqual(output['component_states']['journal'],'unavailable_or_invalid')
        self.assertEqual(target.read_bytes(),before)

    def test_fixture_projection_does_not_read_raw_payload_or_retained_json(self):
        from test_api_football import fixture
        import api_football as api
        import fixture_store as store
        target=self.root/'fixtures.json'
        api.save_fixtures([fixture()],target); store.migrate_json(target)
        database=store.database_path(target); before=database.read_bytes()
        with patch('fixture_store.raw_fixtures',side_effect=AssertionError('No raw payloads')), \
                patch('api_football._load_store',side_effect=AssertionError('No giant JSON parse')):
            output=observation.collect(self.root,now=NOW,include_evidence=False)
        self.assertEqual(output['component_states']['fixtures'],'available')
        self.assertEqual(database.read_bytes(),before)

    def test_evidence_uses_existing_registered_protocol_and_preserves_coverage_without_pooling(self):
        from test_saved_model_comparison import entry as comparable
        import journal_protocol
        with patch('cloud_storage.configured',return_value=False):
            protocol=journal_protocol.register_protocol(self.root/'prospective_protocol.json',
                now=datetime(2026,10,20,9,tzinfo=timezone.utc))
        self.seed_journal([comparable()])
        before=(self.root/'prospective_protocol.json').read_bytes()
        with patch('api_football.ApiFootballClient',side_effect=AssertionError('No API')), \
                patch('catboost_scenarios.forecast',side_effect=AssertionError('No inference')):
            output=observation.collect(self.root,now=NOW)
        evidence=output['predictive_evidence']
        self.assertEqual(evidence['state'],'available')
        self.assertEqual(evidence['registered_at'],protocol['registered_at'])
        self.assertEqual(evidence['coverage']['unique_matches'],2)
        self.assertEqual(evidence['coverage']['mature_cohorts'],0)
        self.assertEqual(len(evidence['cohorts']),2)
        self.assertEqual((self.root/'prospective_protocol.json').read_bytes(),before)
        self.assertNotIn('pooled_accuracy',evidence)

    def test_cli_output_is_explicit_exclusive_and_invalid_values_are_sanitized(self):
        destination=self.root/'report.json'
        with patch('sys.stdout',new_callable=io.StringIO):
            self.assertEqual(cli.main(['--data-dir',str(self.root),'--skip-evidence','--output',str(destination)]),0)
        before=destination.read_bytes()
        with patch('sys.stderr',new_callable=io.StringIO) as errors:
            self.assertEqual(cli.main(['--data-dir',str(self.root),'--skip-evidence','--output',str(destination)]),1)
            self.assertNotIn(str(self.root),errors.getvalue())
        self.assertEqual(destination.read_bytes(),before)
        for kwargs in (dict(days=0),dict(days=True),dict(timezone_name='invalid-secret-token')):
            with self.assertRaises(observation.ObservationError):
                observation.build(now=NOW,**kwargs)

    def test_only_latest_day_service_fields_are_exported_and_old_day_times_are_not_relabelled(self):
        payload=dict(secret='DO_NOT_EXPORT',day_service=dict(day='2026-10-24',
            calendar_at=(NOW-timedelta(minutes=2)).isoformat(),
            results_at=(NOW-timedelta(days=1)).isoformat(),forecasts_at=(NOW+timedelta(minutes=1)).isoformat(),
            private='DO_NOT_EXPORT'))
        target=self.root/'automation_state.json'; target.write_text(json.dumps(payload))
        before=target.read_bytes()
        output=observation.collect(self.root,now=NOW,include_evidence=False)
        day=output['daily'][-1]
        self.assertEqual(day['latest_day_service_checkpoints'],{'calendar_at':(NOW-timedelta(minutes=2)).isoformat()})
        self.assertEqual(day['latest_day_service_age_seconds'],{'calendar_at':120})
        self.assertEqual(output['daily'][-2]['latest_day_service_checkpoints'],{})
        self.assertNotIn('DO_NOT_EXPORT',json.dumps(output))
        self.assertEqual(target.read_bytes(),before)

    def seed_refreshes(self,events):
        with sqlite3.connect(health.path(self.root)) as con:
            con.execute('CREATE TABLE refresh_events (id TEXT PRIMARY KEY,observed_at,cycle_started_at,day,phase,counters)')
            con.execute('CREATE INDEX refresh_events_observed ON refresh_events(observed_at)')
            for index,(stamp,phase) in enumerate(events):
                con.execute('INSERT INTO refresh_events VALUES (?,?,?,?,?,?)',
                    (f'{index:032x}',stamp.isoformat(),stamp.isoformat(),
                     stamp.astimezone(observation.ZoneInfo('Africa/Kinshasa')).date().isoformat(),phase,'{"requests":2}'))

    def test_readonly_refresh_events_gaps_across_midnight_and_duplicate_ids_are_not_uptime(self):
        self.seed_refreshes([(NOW-timedelta(days=1,minutes=10),'calendar'),(NOW-timedelta(minutes=5),'calendar'),
                             (NOW-timedelta(minutes=4),'results'),(NOW+timedelta(minutes=1),'calendar')])
        target=health.path(self.root); before=target.read_bytes()
        events=observation._refresh_events(self.root,NOW,NOW-timedelta(days=7))
        self.assertEqual(len(events['rows']),3)
        events['rows'].append(deepcopy(events['rows'][0]))
        output=self.report(refresh_events=events)
        self.assertEqual(output['refresh_observations']['duplicate_input_events'],1)
        self.assertEqual(output['refresh_observations']['phases']['calendar']['count'],2)
        today=output['daily'][-1]['completed_refreshes']
        self.assertEqual(today['calendar']['count'],1)
        self.assertEqual(today['calendar']['longest_observed_interval_seconds'],86700)
        self.assertEqual(output['refresh_observations']['phases']['results']['age_since_last_seconds'],240)
        self.assertNotIn('failure',str(output['refresh_observations']))
        self.assertEqual(target.read_bytes(),before)

    def test_event_reader_bounds_rows_days_and_does_not_migrate_old_ledger(self):
        self.seed_refreshes([(NOW-timedelta(days=32),'calendar'),(NOW-timedelta(days=2),'calendar'),
                             (NOW-timedelta(days=1),'calendar'),(NOW,'calendar')])
        with patch.object(observation,'REFRESH_EVENT_LIMIT',2):
            events=observation._refresh_events(self.root,NOW,NOW-timedelta(days=365))
        self.assertTrue(events['truncated'])
        self.assertEqual(len(events['rows']),2)
        self.assertEqual(events['window_start'],(NOW-timedelta(days=31)).isoformat())
        with sqlite3.connect(health.path(self.root)) as con:
            con.execute('DROP TABLE refresh_events')
        before=health.path(self.root).read_bytes()
        self.assertEqual(observation._refresh_events(self.root,NOW,NOW-timedelta(days=7))['state'],'not_yet_recorded')
        self.assertEqual(health.path(self.root).read_bytes(),before)

    def test_event_query_normalizes_local_midnight_to_utc_before_sql_comparison(self):
        inside=datetime(2026,10,23,23,30,tzinfo=timezone.utc)
        outside=datetime(2026,10,23,22,59,tzinfo=timezone.utc)
        self.seed_refreshes([(inside,'calendar'),(outside,'calendar')])
        midnight=datetime(2026,10,24,tzinfo=observation.ZoneInfo('Africa/Kinshasa'))
        events=observation._refresh_events(self.root,NOW,midnight)
        self.assertEqual(len(events['rows']),1)
        self.assertEqual(events['rows'][0]['observed_at'],inside.isoformat())
        self.assertEqual(events['window_start'],'2026-10-23T23:00:00+00:00')


if __name__=='__main__':
    unittest.main()
