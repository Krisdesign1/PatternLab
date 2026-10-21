"""Older explicit commands cannot replace any existing league campaign."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import model_campaign as campaign
from scripts import evaluate_context_neighbors as context_cli
from scripts import train_scenario_models as train_cli


class LegacyModelCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.api_root = patch.object(api, 'DATA_DIR', self.root)
        self.api_root.start();self.addCleanup(self.api_root.stop)

    def manifest(self, league, status):
        value = dict(version=campaign.VERSION, campaign=campaign.CAMPAIGN, source='API-Football',
                     league=league, status=status)
        path = campaign._paths(league)['manifest']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def run_cli(self, module, args):
        with patch('sys.argv', [module.__name__]+args), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return module.main()

    def test_all_existing_campaign_states_block_before_any_data_load_or_fit(self):
        for module in (train_cli, context_cli):
            for status in ('building','frozen','error'):
                with self.subTest(command=module.__name__, status=status), ExitStack() as stack:
                    path = self.manifest('39',status)
                    before = path.read_bytes()
                    train = stack.enter_context(patch.object(train_cli.ml,'train'))
                    run = stack.enter_context(patch.object(context_cli.contextual_validation,'run'))
                    save = stack.enter_context(patch.object(context_cli.contextual_validation,'save_report'))
                    matches = stack.enter_context(patch.object(api,'load_matches'))
                    source = stack.enter_context(patch.object(train_cli.sources,'load_matches'))
                    args = ['--league','39'] if module is train_cli else ['39']
                    with self.assertRaises(SystemExit) as raised:
                        self.run_cli(module,args)
                    self.assertEqual(raised.exception.code,2)
                    train.assert_not_called();run.assert_not_called();save.assert_not_called()
                    matches.assert_not_called();source.assert_not_called()
                    self.assertEqual(path.read_bytes(),before)
                    path.unlink()

    def test_preflight_checks_all_requested_leagues_before_partial_training(self):
        self.manifest('61','frozen')
        for module in (train_cli,context_cli):
            with self.subTest(command=module.__name__), patch.object(train_cli.ml,'train') as train, \
                 patch.object(context_cli.contextual_validation,'run') as run:
                args = ['--league','39','61'] if module is train_cli else ['39','61']
                with self.assertRaises(SystemExit): self.run_cli(module,args)
                train.assert_not_called();run.assert_not_called()

    def test_older_commands_still_work_for_a_league_without_a_campaign(self):
        rows=[dict(league='39')]
        native=dict(source='API-Football',league='39',split={},selection={},test={},deployment={})
        context=dict(league='39',aggregate_complete={},aggregate_partial={},deployment={})
        with patch.object(train_cli.sources,'load_matches',return_value=(rows,{})), \
             patch.object(train_cli.ml,'train',return_value=native) as train:
            self.run_cli(train_cli,['--league','39'])
            train.assert_called_once_with(rows,'39','API-Football')
        with patch.object(api,'load_matches',return_value=(rows,{'issues':[]})), \
             patch.object(api,'load_fixtures',return_value=[]), \
             patch.object(context_cli.contextual_validation,'run',return_value=context) as run, \
             patch.object(context_cli.contextual_validation,'save_report',return_value=self.root/'context.json') as save:
            self.run_cli(context_cli,['39','--sizes','2','4'])
            run.assert_called_once()
            self.assertEqual(run.call_args.args,(rows,'39'))
            self.assertEqual(run.call_args.kwargs['sizes'],(2,4))
            save.assert_called_once_with(context)
        self.assertFalse(list(self.root.rglob('CURRENT.json')))

    def test_campaign_appearing_after_preflight_is_rechecked_before_work(self):
        for module in (train_cli,context_cli):
            with self.subTest(command=module.__name__), \
                 patch.object(campaign,'load_manifest',side_effect=[None,dict(status='building')]), \
                 patch.object(train_cli.sources,'load_matches',return_value=([],{})), \
                 patch.object(api,'load_matches',return_value=([],{'issues':[]})), \
                 patch.object(api,'load_fixtures',return_value=[]), \
                 patch.object(train_cli.ml,'train') as train, \
                 patch.object(context_cli.contextual_validation,'run') as run, \
                 patch.object(context_cli.contextual_validation,'save_report') as save:
                args=['--league','39'] if module is train_cli else ['39']
                with self.assertRaises(SystemExit):self.run_cli(module,args)
                train.assert_not_called();run.assert_not_called();save.assert_not_called()


if __name__=='__main__':unittest.main()
