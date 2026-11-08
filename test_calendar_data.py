from datetime import date
from copy import deepcopy
import unittest

import calendar_data as calendar


def raw(match_id=1, status="NS", stamp="2026-10-18T12:00:00Z", league=39):
    return {"fixture": {"id": match_id, "date": stamp, "status": {"short": status, "elapsed": 67, "extra": None}},
            "league": {"id": league, "name": "Premier League", "country": "England"},
            "teams": {"home": {"id": 1, "name": "Home FC"}, "away": {"id": 2, "name": "Away FC"}},
            "goals": {"home": 2, "away": 1},
            "score": {"halftime": {"home": 1, "away": 0}, "fulltime": {"home": 1, "away": 1}}}


class CalendarTests(unittest.TestCase):
    def test_final_verdict_compares_both_halves_including_goalless_draw(self):
        for ht, ft, expected in (((2, 1), (3, 1), '1st'), ((1, 0), (2, 0), 'Égal'),
                                 ((0, 0), (1, 1), '2nd'), ((0, 0), (0, 0), 'Égal')):
            with self.subTest(halftime=ht, fulltime=ft):
                item = raw(status='FT')
                item['score'] = {'halftime': dict(zip(('home', 'away'), ht)),
                                 'fulltime': dict(zip(('home', 'away'), ft))}
                before = deepcopy(item)
                row = calendar.from_raw(item)
                self.assertEqual(row['final_verdict'], expected)
                self.assertEqual(item, before)

    def test_live_and_future_matches_never_get_final_verdict_from_provisional_scores(self):
        for status in ('1H', 'HT', '2H', 'ET', 'BT', 'P', 'LIVE', 'NS', 'TBD'):
            with self.subTest(status=status):
                expected = 'En attente' if status in ('NS', 'TBD') else 'En cours'
                self.assertEqual(calendar.from_raw(raw(status=status))['final_verdict'], expected)

    def test_overtime_and_penalties_do_not_change_the_regulation_verdict(self):
        for status in ('AET', 'PEN'):
            with self.subTest(status=status):
                item = raw(status=status)
                item['score'].update(extratime={'home': 6, 'away': 1}, penalty={'home': 5, 'away': 4})
                item['goals'] = {'home': 6, 'away': 1}
                # HT has one goal, regulation FT two goals: one in each half.
                self.assertEqual(calendar.from_raw(item)['final_verdict'], 'Égal')
                del item['score']['fulltime']
                self.assertEqual(calendar.from_raw(item)['final_verdict'], 'Non évaluable')

    def test_missing_or_invalid_final_scores_are_not_evaluated(self):
        for period in ('halftime', 'fulltime'):
            for invalid in (None, -1, True, 1.5, '1'):
                with self.subTest(period=period, value=invalid):
                    item = raw(status='FT')
                    item['score'][period]['home'] = invalid
                    row = calendar.from_raw(item)
                    self.assertEqual(row['final_verdict'], 'Non évaluable')
                    self.assertIn('absent ou invalide', row['final_verdict_reason'])

    def test_per_team_score_regression_requires_review_even_with_consistent_totals(self):
        item = raw(status='FT')
        item['score']['halftime'] = {'home': 2, 'away': 0}
        item['score']['fulltime'] = {'home': 1, 'away': 3}
        row = calendar.from_raw(item)
        self.assertEqual(row['final_verdict'], 'À vérifier')
        self.assertIn('incohérents', row['final_verdict_reason'])

    def test_administrative_statuses_and_unknown_state_never_get_sporting_verdict(self):
        for status in ('CANC', 'ABD', 'AWD', 'WO', 'PST', 'SUSP', 'INT', 'UNKNOWN'):
            with self.subTest(status=status):
                expected = 'À vérifier' if status == 'UNKNOWN' else 'Non évaluable'
                self.assertEqual(calendar.from_raw(raw(status=status))['final_verdict'], expected)

    def test_quarantined_complete_result_is_not_published_as_final(self):
        item = raw(status='FT')
        item['_patternlab_import'] = {'quarantined': True, 'reason': 'identity_changed'}
        row = calendar.from_raw(item)
        self.assertEqual(row['final_verdict'], 'À vérifier')
        self.assertIn('Quarantaine', row['final_verdict_reason'])

    def test_wrong_provider_or_unconfirmed_identity_blocks_final_verdict(self):
        for kind in ('provider', 'missing_team', 'same_team'):
            with self.subTest(kind=kind):
                item = raw(status='FT')
                if kind == 'provider':
                    item['provider'] = 'other-provider'
                elif kind == 'missing_team':
                    item['teams']['home']['id'] = None
                else:
                    item['teams']['away']['id'] = item['teams']['home']['id']
                self.assertEqual(calendar.from_raw(item)['final_verdict'], 'À vérifier')

    def test_live_rows_have_score_halftime_phase_and_minute_without_final_result(self):
        item = raw(status="2H")
        item["fixture"]["status"].update(elapsed=90, extra=4)
        row = calendar.from_raw(item)
        self.assertEqual((row["status_group"], row["status"], row["score"], row["ht"]), ("live", "IN_PLAY", "2–1", "1–0"))
        self.assertEqual(row["minute"], "90+4′")
        self.assertEqual(row["phase"], "regulation")
        self.assertEqual(row["regulation_score"], "—")
        self.assertNotIn("value", row)
        self.assertFalse(row["selectable"])

    def test_overtime_live_goals_and_regulation_final_are_separate(self):
        for status in ("AET", "PEN"):
            with self.subTest(status=status):
                row = calendar.from_raw(raw(status=status))
                self.assertEqual(row["score"], "2–1")
                self.assertEqual(row["regulation_score"], "1–1")
                self.assertEqual(row["status"], "FINISHED")
        self.assertEqual(calendar.from_raw(raw(status="ET"))["phase"], "extra_time")
        self.assertEqual(calendar.from_raw(raw(status="P"))["phase"], "penalties")

    def test_missing_or_negative_sentinels_never_become_zero_scores(self):
        item = raw()
        item["goals"] = {"home": None, "away": -1}
        item["score"]["halftime"] = {"home": True, "away": None}
        row = calendar.from_raw(item)
        self.assertEqual((row["score"], row["ht"]), ("—", "—"))
        self.assertIsNone(row["home_goals"])
        self.assertIsNone(row["away_goals"])

    def test_unknown_teams_are_displayed_but_not_selectable(self):
        item = raw()
        item["teams"]["home"] = {"id": None, "name": None}
        row = calendar.from_raw(item)
        self.assertEqual(row["home"], "Club à confirmer")
        self.assertFalse(row["teams_confirmed"])
        self.assertFalse(row["selectable"])
        self.assertEqual(len(calendar.day_rows([item], date(2026, 10, 18))), 1)

    def test_default_day_contains_all_competitions_and_statuses_at_utc_boundaries(self):
        items = [raw(1, "NS", "2026-10-17T22:59:59Z"), raw(2, "HT", "2026-10-17T23:00:00Z"),
                 raw(3, "FT", "2026-10-18T22:59:59Z", league=61), raw(4, "NS", "2026-10-18T23:00:00Z")]
        rows = calendar.day_rows(items, date(2026, 10, 18))
        self.assertEqual([row["match_id"] for row in rows], ["2", "3"])
        self.assertEqual(len(calendar.filter_rows(rows)), 2)
        self.assertEqual(len(calendar.filter_rows(rows, status="En cours")), 1)
        self.assertEqual(len(calendar.filter_rows(rows, status="Terminés")), 1)

    def test_filters_use_exact_competition_ids_and_case_insensitive_club_names(self):
        rows = [calendar.from_raw(raw(1, league=39)), calendar.from_raw(raw(2, league=390)),
                calendar.from_raw(raw(3, status="HT", league=61)), calendar.from_raw(raw(4, status="PST"))]
        self.assertEqual([r["match_id"] for r in calendar.filter_rows(rows, league="39", status="À venir", query="home")], ["1"])
        self.assertEqual([r["match_id"] for r in calendar.filter_rows(rows, status="PAUSED")], ["3"])
        self.assertEqual([r["match_id"] for r in calendar.filter_rows(rows, status="Autres")], ["4"])
        self.assertEqual(calendar.filter_rows(rows, query="not a club"), [])

    def test_invalid_dates_ids_or_filters_have_explicit_errors(self):
        for item in (raw(stamp="2026-10-18T12:00:00"), raw(match_id=None)):
            with self.assertRaises(ValueError):
                calendar.from_raw(item)
        with self.assertRaises(ValueError):
            calendar.filter_rows([], status="impossible")
        with self.assertRaises(ValueError):
            calendar.day_rows([], "2026-10-18")


if __name__ == "__main__":
    unittest.main()
