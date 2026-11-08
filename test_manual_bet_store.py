"""Explicit selections stay empty until saved and keep their own FC stakes."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import manual_bet_store as store


NOW=datetime(2026,10,28,10,tzinfo=timezone.utc)


def ticket(identifier='a',status='pending',day='2026-10-28'):
    return dict(id=identifier,entry_id='forecast-'+identifier,entry_ids=['forecast-'+identifier],
        size=2,matches='1. A — B · 2. C — D',match_ids=['100','101'],sequence=['2nd','2nd'],
        methods=['Clubs · Choix 1'],probability_by_method='Clubs : 36,00 %',
        day=day,captured_at='2026-10-28T08:00:00Z',kickoff_at='2026-10-28T12:00:00Z',
        odds=4.41,stake=1,status=status,return_amount=None,pnl=None,
        actual=['2nd','2nd'] if status=='won' else None,reason='Saved result')


class ManualBetTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.path=Path(folder.name)/'separate'/'manual.sqlite3'

    def add(self,value=None,stake=300,now=NOW):
        return store.add(value or ticket(),stake,path=self.path,now=now)

    def test_reading_and_empty_projection_never_create_or_auto_import_bets(self):
        self.assertEqual(store.list_bets(self.path),[])
        self.assertFalse(self.path.exists());self.assertFalse(self.path.parent.exists())
        report=store.build([ticket('one'),ticket('two')],[])
        self.assertEqual(report['tickets'],[])
        self.assertEqual(report['summary']['bets'],0)
        self.assertEqual(report['summary']['stake_total'],0)

    def test_add_persists_only_one_explicit_selection(self):
        one=self.add();self.assertEqual(one['stake'],300)
        self.assertEqual(one['timing'],'before_kickoff')
        saved=store.list_bets(self.path)
        report=store.build([ticket('a'),ticket('b')],saved)
        self.assertEqual([row['id'] for row in report['tickets']],['a'])
        self.assertEqual(report['summary']['stake_total'],300)
        self.assertEqual(report['summary']['stake_open'],300)
        self.assertIsNone(report['tickets'][0]['pnl'])

    def test_duplicate_add_never_replaces_original_stake_or_selection_time(self):
        first=self.add(stake=100)
        second=self.add(stake=900,now=NOW+timedelta(hours=4))
        self.assertEqual(first,second)
        self.assertEqual(len(store.list_bets(self.path)),1)

    def test_batch_keeps_existing_stakes_and_deduplicates_requested_scenarios(self):
        original=self.add(ticket('a'),100)
        result=store.add_many([ticket('a'),ticket('b'),ticket('b'),ticket('c')],300,
            path=self.path,now=NOW+timedelta(minutes=1))
        self.assertEqual([row['id'] for row in result],['a','b','c'])
        self.assertEqual(result[0],original)
        self.assertEqual({row['id']:row['stake'] for row in store.list_bets(self.path)},
                         {'a':100,'b':300,'c':300})

    def test_invalid_batch_creates_nothing_and_does_not_partially_update_an_existing_portfolio(self):
        bad=ticket('invalid');bad['sequence']=['unrecognized']
        with self.assertRaises(store.ManualBetError):
            store.add_many([ticket('valid'),bad],300,path=self.path,now=NOW)
        self.assertFalse(self.path.exists())
        original=self.add(ticket('a'),100)
        with self.assertRaises(store.ManualBetError):
            store.add_many([ticket('b'),bad],300,path=self.path,now=NOW)
        self.assertEqual(store.list_bets(self.path),[original])

    def test_empty_batch_does_not_create_storage(self):
        self.assertEqual(store.add_many([],300,path=self.path,now=NOW),[])
        self.assertFalse(self.path.exists())

    def test_batch_database_failure_rolls_back_every_new_selection(self):
        original=self.add(ticket('a'),100)
        with sqlite3.connect(self.path) as con:
            con.execute('''CREATE TRIGGER refuse_one BEFORE INSERT ON manual_bets
                WHEN NEW.id='c' BEGIN SELECT RAISE(ABORT,'test failure'); END''')
        with self.assertRaises(store.ManualBetError):
            store.add_many([ticket('b'),ticket('c')],300,path=self.path,now=NOW)
        self.assertEqual(store.list_bets(self.path),[original])

    def test_individual_stakes_and_cent_accounting(self):
        self.add(ticket('a'),100)
        self.add(ticket('b'),200)
        catalog=[ticket('a','won'),ticket('b','lost')]
        report=store.build(catalog,store.list_bets(self.path))
        self.assertEqual(report['summary']['bets'],2)
        self.assertEqual(report['summary']['stake_total'],300)
        self.assertEqual(report['summary']['returned'],441)
        self.assertEqual(report['summary']['pnl'],141)
        self.assertEqual(report['summary']['roi'],47)

    def test_missing_catalog_does_not_reuse_an_old_win(self):
        self.add(ticket(status='won'))
        result=store.build([],store.list_bets(self.path))
        self.assertEqual(result['tickets'][0]['status'],'unresolved')
        self.assertIsNone(result['tickets'][0]['return_amount'])
        self.assertEqual(result['summary']['pnl'],0)
        self.assertEqual(result['summary']['stake_open'],300)

    def test_odds_remain_fixed_after_add_and_no_intermediate_rounding(self):
        self.add(stake=.1)
        with patch.dict(store.DEFAULT_ODDS,{'2nd':99.0}):
            saved=store.list_bets(self.path)
            result=store.build([dict(ticket(status='won'),odds=99**2)],saved)
        self.assertEqual(result['tickets'][0]['odds'],4.41)
        self.assertEqual(result['tickets'][0]['return_amount'],.44)
        self.assertEqual(result['tickets'][0]['pnl'],.34)

    def test_twenty_legs_decimal_product_and_final_money_rounding(self):
        value=ticket();value.update(size=20,sequence=['2nd']*20,match_ids=[str(i) for i in range(100,120)])
        self.add(value,1)
        catalog=[dict(value,status='won')]
        result=store.build(catalog,store.list_bets(self.path))
        self.assertEqual(result['tickets'][0]['return_amount'],float((Decimal('2.1')**20).quantize(Decimal('.01'))))

    def test_after_kickoff_add_is_explicitly_retrospective(self):
        result=self.add(now=NOW+timedelta(hours=3))
        self.assertEqual(result['timing'],'retrospective')
        result=store.build([ticket(status='won')],store.list_bets(self.path))
        self.assertEqual(result['tickets'][0]['timing'],'retrospective')

    def test_missing_kickoff_is_conservatively_retrospective(self):
        value=ticket();value.pop('kickoff_at')
        self.assertEqual(self.add(value)['timing'],'retrospective')

    def test_earlier_corrected_kickoff_requalifies_projection_without_changing_record(self):
        self.add()
        saved=store.list_bets(self.path);before=deepcopy(saved)
        advanced=dict(ticket(),kickoff_at='2026-10-28T09:30:00Z')
        result=store.build([advanced],saved)
        self.assertEqual(result['tickets'][0]['timing'],'retrospective')
        self.assertEqual(saved,before)
        self.assertEqual(store.list_bets(self.path)[0]['timing'],'before_kickoff')

    def test_stake_edit_is_individual_and_after_kickoff_becomes_retrospective(self):
        self.add(ticket('a'),100);self.add(ticket('b'),200)
        changed=store.update_stake('a',500,path=self.path,now=NOW+timedelta(minutes=1))
        self.assertEqual((changed['stake'],changed['timing']),(500,'before_kickoff'))
        changed=store.update_stake('a',600,path=self.path,now=NOW+timedelta(hours=3))
        self.assertEqual((changed['stake'],changed['timing']),(600,'retrospective'))
        self.assertEqual(next(row['stake'] for row in store.list_bets(self.path) if row['id']=='b'),200)

    def test_remove_and_absent_edit_do_not_touch_unrelated_selections(self):
        self.assertFalse(store.remove('missing',path=self.path));self.assertFalse(self.path.exists())
        with self.assertRaises(store.ManualBetError):store.update_stake('x',100,path=self.path,now=NOW)
        self.assertFalse(self.path.exists())
        self.add(ticket('a'));self.add(ticket('b'))
        self.assertTrue(store.remove('a',path=self.path))
        self.assertEqual([row['id'] for row in store.list_bets(self.path)],['b'])

    def test_snapshot_is_immutable_and_corruption_rejected(self):
        self.add()
        with sqlite3.connect(self.path) as con:
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("UPDATE manual_bets SET snapshot='{}'")
            con.execute("UPDATE manual_bets SET stake='nan'")
        with self.assertRaises(store.ManualBetError):store.list_bets(self.path)

    def test_refunds_are_neutral_and_open_bets_never_become_losses(self):
        self.add(ticket('refund'),100);self.add(ticket('open'),200)
        result=store.build([ticket('refund','refunded'),ticket('open','unresolved')],store.list_bets(self.path))
        self.assertEqual(result['summary']['returned'],100)
        self.assertEqual(result['summary']['pnl'],0)
        self.assertEqual(result['summary']['stake_open'],200)
        self.assertEqual(result['summary']['stake_settled'],0)
        self.assertIsNone(result['summary']['roi'])

    def test_input_catalog_and_saved_choices_remain_unchanged(self):
        self.add();saved=store.list_bets(self.path);catalog=[ticket()]
        before=deepcopy((catalog,saved))
        store.build(catalog,saved)
        self.assertEqual((catalog,saved),before)

    def test_catalog_result_reorders_to_the_selected_proposal_order(self):
        original=ticket();original['sequence']=['1st','2nd']
        self.add(original,100)
        changed=dict(original,entry_id='other-forecast',match_ids=['101','100'],
                     sequence=['2nd','1st'],matches='1. C — D · 2. A — B',
                     status='won',actual=['2nd','1st'])
        result=store.build([changed],store.list_bets(self.path))
        selected=result['tickets'][0]
        self.assertEqual(selected['status'],'won')
        self.assertEqual(selected['sequence'],['1st','2nd'])
        self.assertEqual(selected['actual'],['1st','2nd'])
        self.assertEqual(selected['match_ids'],['100','101'])

    def test_incompatible_or_unverifiable_result_order_remains_unresolved(self):
        self.add()
        for changed in (dict(ticket(status='won'),match_ids=['100','999']),
                        dict(ticket(status='won'),match_ids=None,entry_id='other')):
            result=store.build([changed],store.list_bets(self.path))
            self.assertEqual(result['tickets'][0]['status'],'unresolved')
            self.assertIsNone(result['tickets'][0]['return_amount'])

    def test_invalid_stakes_future_capture_and_naive_clock_are_rejected(self):
        for stake in (0,-1,True,float('nan'),float('inf'),.001):
            with self.subTest(stake=stake),self.assertRaises(store.ManualBetError):self.add(stake=stake)
        with self.assertRaises(store.ManualBetError):self.add(now=datetime(2026,10,28))
        with self.assertRaises(store.ManualBetError):self.add(now=NOW-timedelta(hours=3))
        self.assertFalse(self.path.exists())


if __name__=='__main__':
    unittest.main()
