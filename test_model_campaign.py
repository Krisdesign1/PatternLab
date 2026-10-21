"""One frozen league campaign, isolated artifacts, no retrospective retuning."""
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import catboost_scenarios as ml
import contextual_validation as contextual
import model_campaign as campaign
import scenario_features as features
from test_catboost_scenarios import sample as chronological_sample
from test_seasonal_validation import sample as seasonal_sample


NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


class NativeStub:
    def save_model(self, path, format='cbm'):
        Path(path).write_bytes(b'isolated-fake-native-artifact')


def metric(n=4):
    return dict(tested=n, top1=0, top3=1, top1_rate=0, top3_rate=1/n, loss=4.0)


def fake_train(rows, league, source, directory=None, **kwargs):
    cutoff = kwargs.get('before', NOW.date().isoformat())
    report = dict(version=ml.VERSION, feature_version=features.FEATURE_VERSION,
        source=source, provider='api-football', league=league,
        feature_columns=list(features.FEATURE_COLUMNS), categorical_columns=list(features.CATEGORICAL_COLUMNS),
        labels=list(ml.VALUES), selection=dict(alpha=0.0, params=dict(depth=2, iterations=5)),
        validation={name:metric() for name in ml.METHODS}, test={name:metric() for name in ml.METHODS},
        targets=dict(validation=[],test=[dict(day='2025-01-01',match_ids=['1','2','3','4'])]),
        deployment=dict(fingerprint=ml._fingerprint(ml._rows(rows,league,source,cutoff)),
                        cutoff_exclusive=cutoff, trained_through=rows[-1]['timestamp'][:10], rows=len(rows)))
    ml._publish(NativeStub(),report,source,league,directory)
    return report


def fake_context(rows, league, source, sizes=(2,4,6,8)):
    outputs = {str(size): dict(selection=dict(params=dict(k=20, half_life_days=None, mix=0.0))) for size in sizes}
    folds = [dict(test_season=2026, split=dict(validation=dict(end='2025-12-31')), sizes=outputs)]
    aggregate = {str(size): {name:metric() for name in contextual.METHODS} for size in sizes}
    fingerprint = ml._fingerprint(rows)
    return dict(version=contextual.VERSION, source=source, league=league, exploratory=True,
                sizes=list(sizes), folds=folds, fingerprint=fingerprint,
                aggregate=aggregate, aggregate_complete=aggregate, aggregate_partial=aggregate,
                deployment=contextual._configuration(folds,rows,league,fingerprint))


class CampaignTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.root=Path(folder.name)
        self.network=patch.object(api,'ApiFootballClient',side_effect=AssertionError('Campaign never uses network'))
        self.network.start();self.addCleanup(self.network.stop)

    def make_campaign(self, rows=None, league='39', **kwargs):
        with patch.object(ml,'train',side_effect=fake_train) as trained, \
             patch.object(contextual,'run',side_effect=fake_context) as evaluated:
            result=campaign.run_one(rows or seasonal_sample(),league,root=self.root,now=NOW,**kwargs)
        return result,trained,evaluated

    def test_plan_covers_every_api_competition_and_prioritizes_upcoming_without_writing(self):
        rows=seasonal_sample()
        rows += [dict(r,league='222') for r in chronological_sample()]
        rows += [dict(r,league='999',provider='football-data.org') for r in chronological_sample()]
        raw=[dict(league=dict(id=222,name='Upcoming League'),
                  fixture=dict(id=999,date='2026-10-19T12:00:00Z',status=dict(short='NS'))),
             dict(league=dict(id=500,name='Empty League'),
                  fixture=dict(id=1000,date='2026-10-20T12:00:00Z',status=dict(short='NS')))]
        jobs=campaign.plan(rows,raw=raw,root=self.root,now=NOW)
        self.assertEqual([r['league'] for r in jobs],['222','500','39'])
        self.assertTrue(jobs[0]['chronological_ready'])
        self.assertFalse(jobs[0]['contextual_ready'])
        self.assertEqual(jobs[1]['status'],'insufficient')
        self.assertTrue(jobs[2]['contextual_ready'])
        self.assertEqual(list(self.root.iterdir()),[])

    def test_insufficient_history_is_not_frozen_and_can_become_ready_later(self):
        with patch.object(ml,'train',side_effect=AssertionError('No insufficient fit')):
            first=campaign.run_one(chronological_sample(40),'39',root=self.root,now=NOW)
        self.assertEqual(first['status'],'insufficient')
        paths=campaign._paths('39',self.root)
        self.assertFalse(paths['input'].exists())
        self.assertIsNone(campaign.load_manifest('39',self.root))
        result,trained,_=self.make_campaign(chronological_sample())
        self.assertEqual(result['status'],'frozen')
        self.assertEqual(trained.call_count,1)

    def test_configured_historical_imports_complete_before_first_input_freezes(self):
        (self.root/'automation_config.json').write_text(json.dumps(dict(history_seasons=[2022,2023,2024,2025,2026])))
        rows=seasonal_sample()
        job=campaign.plan(rows,root=self.root,now=NOW)[0]
        self.assertEqual(job['status'],'insufficient')
        self.assertTrue(job['chronological_ready'])
        self.assertEqual(job['missing_seasons'],[2022,2023,2024,2025,2026])
        with patch.object(ml,'train',side_effect=AssertionError('Wait for imports')):
            result=campaign.run_one(rows,'39',root=self.root,now=NOW)
        self.assertEqual(result['status'],'insufficient')
        self.assertFalse(campaign._paths('39',self.root)['input'].exists())
        # Complete API manifests, including empty responses, are the import
        # boundary. Usable-result requirements remain a separate check.
        api.save_fixtures([],self.root/'fixtures.json',season_imports=[dict(
            competition_code='39',season=year,match_ids=[]) for year in range(2022,2027)])
        self.assertEqual(campaign.plan(rows,root=self.root,now=NOW)[0]['status'],'ready')
        result,_,_=self.make_campaign(rows)
        self.assertEqual(result['status'],'frozen')

    def test_first_input_and_parameters_are_frozen_after_publishing_once(self):
        rows=seasonal_sample()
        result,trained,evaluated=self.make_campaign(rows)
        self.assertEqual(result['status'],'frozen')
        self.assertEqual(trained.call_count,1)
        self.assertEqual(evaluated.call_count,1)
        self.assertEqual(result['components']['catboost']['origin'],'trained_new_on_frozen_input')
        self.assertEqual(result['components']['context_neighbors']['origin'],'validated_new_on_frozen_input')
        paths=campaign._paths('39',self.root)
        native=ml.load_report('API-Football','39',paths['production_native'])
        self.assertEqual(native['deployment']['generation'],result['components']['catboost']['generation'])
        deployed=contextual._validate_report(json.loads(paths['production_context'].read_text()),'39')
        self.assertEqual(deployed['deployment']['generation'],result['components']['context_neighbors']['generation'])
        before={p:p.read_bytes() for p in (paths['input'],paths['manifest'],paths['production_context'])}
        changed=copy.deepcopy(rows)
        changed[0].update(ht='0-0',ft='0-0',value='Égal')
        with patch.object(ml,'train',side_effect=AssertionError('Frozen campaign never retrains')), \
             patch.object(contextual,'run',side_effect=AssertionError('Frozen campaign never retunes')):
            again=campaign.run_one(changed,'39',sizes=(2,4),root=self.root,now=NOW+timedelta(days=3))
        self.assertEqual(again,result)
        self.assertEqual({p:p.read_bytes() for p in before},before)
        self.assertEqual(campaign.plan(changed,root=self.root,now=NOW)[0]['status'],'frozen')

    def test_missing_real_seasons_are_explicit_not_a_fake_five_method_evaluation(self):
        result,trained,evaluated=self.make_campaign(chronological_sample())
        self.assertEqual(result['status'],'frozen')
        self.assertEqual(trained.call_count,1)
        evaluated.assert_not_called()
        self.assertEqual(result['components']['context_neighbors']['status'],'unavailable')
        self.assertEqual(result['comparisons']['status'],'partial')
        self.assertEqual(result['comparisons']['sizes']['4']['methods'],list(ml.METHODS))
        for size in ('2','6','8'):
            self.assertEqual(result['comparisons']['sizes'][size]['status'],'unavailable')
        self.assertFalse(campaign._paths('39',self.root)['production_context'].exists())

    def test_existing_compatible_exploratory_artifacts_are_adopted_without_new_fits(self):
        rows=seasonal_sample()
        paths=campaign._paths('39',self.root)
        original=fake_train(rows,'39','API-Football',paths['production_native'])
        campaign._write(paths['production_context'],fake_context(rows,'39','API-Football'))
        with patch.object(ml,'train',side_effect=AssertionError('Existing model should be adopted')), \
             patch.object(contextual,'run',side_effect=AssertionError('Existing context should be adopted')):
            result=campaign.run_one(rows,'39',root=self.root,now=NOW)
        self.assertEqual(result['components']['catboost']['origin'],'adopted_existing_exploratory')
        self.assertEqual(result['components']['context_neighbors']['origin'],'adopted_existing_exploratory')
        self.assertEqual(result['components']['catboost']['generation'],original['deployment']['generation'])
        self.assertIn('not a new independent test',result['interpretation'])

    def test_failed_campaign_retries_same_frozen_input_and_completed_components_only(self):
        rows=seasonal_sample()
        with patch.object(ml,'train',side_effect=fake_train) as trained, \
             patch.object(contextual,'run',side_effect=OSError('Interrupted validation')):
            with self.assertRaises(campaign.CampaignError):
                campaign.run_one(rows,'39',root=self.root,now=NOW)
        self.assertEqual(trained.call_count,1)
        self.assertEqual(campaign.load_manifest('39',self.root)['status'],'error')
        paths=campaign._paths('39',self.root)
        input_before=paths['input'].read_bytes()
        changed=copy.deepcopy(rows)
        changed[0].update(ht='0-0',ft='0-0',value='Égal')
        with patch.object(ml,'train',side_effect=AssertionError('Completed native model must stay frozen')), \
             patch.object(contextual,'run',side_effect=fake_context) as evaluated:
            result=campaign.run_one(changed,'39',root=self.root,now=NOW+timedelta(days=1))
        self.assertEqual(result['status'],'frozen')
        self.assertEqual(evaluated.call_args.args[0],rows)
        self.assertEqual(paths['input'].read_bytes(),input_before)

    def test_foreign_sources_future_labels_and_other_leagues_do_not_change_frozen_input(self):
        rows=seasonal_sample()
        extras=[dict(rows[0],league='61',ht=None,value='invalid'),
                dict(rows[0],provider='other',ht=None,value='invalid'),
                dict(rows[0],match_id='99999999',timestamp='2099-01-01T12:00:00Z',ht=None,value='invalid')]
        result,_,_=self.make_campaign(rows+extras)
        frozen=campaign._read(campaign._paths('39',self.root)['input'])
        self.assertEqual(frozen['rows'],rows)
        self.assertEqual(result['input_fingerprint'],ml._fingerprint(rows))

    def test_corrupted_snapshot_or_locked_league_cannot_restart_with_new_inputs(self):
        rows=seasonal_sample()
        paths=campaign._paths('39',self.root)
        with campaign._lock(paths):
            with self.assertRaisesRegex(campaign.CampaignError,'en cours'):
                campaign.run_one(rows,'39',root=self.root,now=NOW)
        frozen=campaign._input(rows,'39',campaign.DEFAULT_SIZES,paths,NOW)
        frozen['fingerprint']='f'*64
        campaign._write(paths['input'],frozen)
        with self.assertRaises(campaign.CampaignError): self.make_campaign(rows)

    def test_interrupted_publication_reuses_frozen_artifacts_and_does_not_republish_native(self):
        rows=seasonal_sample()
        paths=campaign._paths('39',self.root)
        original=campaign._write
        def fail_context_publication(path,value):
            if path == paths['production_context']:
                raise OSError('Interrupted final publication')
            return original(path,value)
        with patch.object(ml,'train',side_effect=fake_train), \
             patch.object(contextual,'run',side_effect=fake_context), \
             patch.object(campaign,'_write',side_effect=fail_context_publication):
            with self.assertRaises(campaign.CampaignError):
                campaign.run_one(rows,'39',root=self.root,now=NOW)
        manifest=campaign.load_manifest('39',self.root)
        self.assertIn('catboost',manifest['publications'])
        self.assertNotIn('context_neighbors',manifest['publications'])
        with patch.object(ml,'train',side_effect=AssertionError('No repeat fitting')), \
             patch.object(contextual,'run',side_effect=AssertionError('No repeat selection')), \
             patch.object(campaign,'_copy_native',side_effect=AssertionError('Native model already published')):
            result=campaign.run_one(rows,'39',root=self.root,now=NOW+timedelta(days=1))
        self.assertEqual(result['status'],'frozen')
        self.assertIn('context_neighbors',result['publications'])

    def test_retry_cannot_change_the_first_search_protocol(self):
        paths=campaign._paths('39',self.root)
        campaign._input(seasonal_sample(),'39',campaign.DEFAULT_SIZES,paths,NOW)
        with patch.object(ml,'ALPHAS',(0.0,)), \
             patch.object(ml,'train',side_effect=AssertionError('Protocol change must not trigger fitting')):
            with self.assertRaisesRegex(campaign.CampaignError,'protocole'):
                campaign.run_one(seasonal_sample(),'39',root=self.root,now=NOW)

    def test_retry_training_keeps_the_first_input_day_cutoff(self):
        rows=chronological_sample()
        with patch.object(ml,'train',side_effect=OSError('Fit interrupted')):
            with self.assertRaises(campaign.CampaignError):
                campaign.run_one(rows,'39',root=self.root,now=NOW)
        with patch.object(ml,'train',side_effect=fake_train) as trained:
            result=campaign.run_one(rows,'39',root=self.root,now=NOW+timedelta(days=2))
        self.assertEqual(result['status'],'frozen')
        self.assertEqual(trained.call_args.kwargs['before'],'2026-10-18')
        self.assertEqual(ml.load_report('API-Football','39',self.root/'models')['deployment']['cutoff_exclusive'],'2026-10-18')

    def test_plan_distinguishes_later_results_from_backfill_or_correction_without_retuning(self):
        rows=seasonal_sample()
        self.make_campaign(rows)
        self.assertEqual(campaign.plan(rows,root=self.root,now=NOW)[0]['history_state'],'unchanged')
        later=dict(rows[-1],match_id='88888888',timestamp='2026-10-19T12:00:00')
        planned=campaign.plan(rows+[later],root=self.root,now=NOW+timedelta(days=2))[0]
        self.assertEqual(planned['history_state'],'new_results')
        self.assertFalse(planned['changed_history'])
        backfill=dict(rows[0],match_id='77777777')
        self.assertTrue(campaign.plan(rows+[backfill],root=self.root,now=NOW)[0]['changed_history'])
        changed=copy.deepcopy(rows)
        changed[0].update(ht='0-0',ft='0-0',value='Égal')
        planned=campaign.plan(changed,root=self.root,now=NOW)[0]
        self.assertEqual(planned['status'],'frozen')
        self.assertEqual(planned['history_state'],'changed_history')

    def test_stale_existing_native_and_context_are_not_silently_adopted(self):
        rows=seasonal_sample()
        paths=campaign._paths('39',self.root)
        fake_train(rows,'39','API-Football',paths['production_native'])
        campaign._write(paths['production_context'],fake_context(rows,'39','API-Football'))
        changed=copy.deepcopy(rows)
        changed[0].update(ht='0-0',ft='0-0',value='Égal')
        result,trained,evaluated=self.make_campaign(changed)
        self.assertEqual(trained.call_count,1)
        self.assertEqual(evaluated.call_count,1)
        self.assertEqual(result['components']['catboost']['origin'],'trained_new_on_frozen_input')
        self.assertEqual(result['components']['context_neighbors']['origin'],'validated_new_on_frozen_input')

    def test_competing_manual_deployment_is_preserved_instead_of_overwritten(self):
        rows=seasonal_sample()
        paths=campaign._paths('39',self.root)
        manual={'generation':'different-manual-model','report_sha256':'manual-test-pointer'}
        def selected_then_manual(*args,**kwargs):
            result=fake_context(*args,**kwargs)
            campaign._write(ml._location('API-Football','39',paths['production_native'])/'CURRENT.json',manual)
            return result
        with patch.object(ml,'train',side_effect=fake_train), \
             patch.object(contextual,'run',side_effect=selected_then_manual):
            with self.assertRaisesRegex(campaign.CampaignError,'autre entraînement'):
                campaign.run_one(rows,'39',root=self.root,now=NOW)
        self.assertEqual(campaign._read(ml._location('API-Football','39',paths['production_native'])/'CURRENT.json'),manual)
        self.assertEqual(campaign.load_manifest('39',self.root)['status'],'error')


@unittest.skipUnless(importlib.util.find_spec('catboost'),'CatBoost runtime unavailable')
class NativeCampaignTests(unittest.TestCase):
    def test_real_native_fit_isolated_and_publish_roundtrip_without_journal_or_api(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(ml,'CANDIDATES',({'depth':2,'iterations':5,'l2_leaf_reg':5},)), \
             patch.object(ml,'ALPHAS',(0.0,)), \
             patch.object(api,'ApiFootballClient',side_effect=AssertionError('No API')):
            root=Path(folder)
            result=campaign.run_one([dict(row,league='345') for row in chronological_sample()],
                                    '345',root=root,now=NOW)
            self.assertEqual(result['status'],'frozen')
            deployed=ml.load_report('API-Football','345',root/'models')
            self.assertEqual(deployed['deployment']['rows'],240)
            self.assertEqual(result['components']['catboost']['generation'],deployed['deployment']['generation'])
            self.assertFalse(list(root.rglob('*.sqlite3')))


if __name__=='__main__':
    unittest.main()
