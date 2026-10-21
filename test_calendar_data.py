from datetime import date
import unittest

import calendar_data as calendar


def raw(match_id=1, status="NS", stamp="2026-10-18T12:00:00Z", league=39):
    return {"fixture": {"id": match_id, "date": stamp, "status": {"short": status, "elapsed": 67, "extra": None}},
            "league": {"id": league, "name": "Premier League", "country": "England"},
            "teams": {"home": {"id": 1, "name": "Home FC"}, "away": {"id": 2, "name": "Away FC"}},
            "goals": {"home": 2, "away": 1},
            "score": {"halftime": {"home": 1, "away": 0}, "fulltime": {"home": 1, "away": 1}}}


class CalendarTests(unittest.TestCase):
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
