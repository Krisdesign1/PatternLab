"""Selected dates never replace the real clock and survive navigation/reruns."""
from datetime import date, datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest
import analysis_date as dates


DAY = date(2026, 10, 28)


class AnalysisDateTests(unittest.TestCase):
    def test_kinshasa_midnight_is_previous_utc_evening(self):
        self.assertEqual(dates.local_today(datetime(2026, 10, 27, 23, tzinfo=timezone.utc)), DAY)
        with self.assertRaises(ValueError):
            dates.local_today(datetime(2026, 10, 28))

    def test_follow_today_and_explicit_date_use_separate_state(self):
        state = {}
        with patch.object(dates.st, 'session_state', state):
            self.assertEqual(dates.get_selected_day(DAY), DAY)
            dates.set_selected_day(DAY, follow_today=True)
            self.assertFalse(dates.day_refresh_needed(DAY))
            self.assertTrue(dates.day_refresh_needed(DAY + timedelta(days=1)))
            self.assertEqual(dates.get_selected_day(DAY + timedelta(days=1)), DAY + timedelta(days=1))
            dates.set_selected_day(DAY + timedelta(days=3))
            self.assertFalse(dates.day_refresh_needed(DAY + timedelta(days=1)))
            self.assertEqual(dates.get_selected_day(DAY + timedelta(days=1)), DAY + timedelta(days=3))
            with self.assertRaises(ValueError):
                dates.set_selected_day('invalid')
            with self.assertRaises(ValueError):
                dates.set_selected_day(date(2200, 1, 1))

    def test_picker_callbacks_and_midnight_do_not_reset_explicit_date(self):
        clock = [DAY]
        patcher = patch.object(dates, 'local_today', side_effect=lambda _now=None: clock[0])
        patcher.start()
        self.addCleanup(patcher.stop)
        at = AppTest.from_string('from analysis_date import render_date_selector\nrender_date_selector()').run()
        self.assertFalse(at.exception)
        at.button(key='analysis_tomorrow').click().run()
        self.assertFalse(at.exception)
        self.assertEqual(at.date_input[0].value, DAY + timedelta(days=1))
        clock[0] = DAY + timedelta(days=2)
        at.run()
        self.assertEqual(at.date_input[0].value, DAY + timedelta(days=1))
        at.button(key='analysis_today').click().run()
        self.assertEqual(at.date_input[0].value, DAY + timedelta(days=2))
        clock[0] = DAY + timedelta(days=3)
        at.run()
        self.assertFalse(at.exception)
        self.assertEqual(at.date_input[0].value, DAY + timedelta(days=3))
        at.date_input[0].set_value(DAY - timedelta(days=4)).run()
        self.assertEqual(at.session_state[dates.DAY_KEY], DAY - timedelta(days=4))

    def test_tomorrow_click_uses_the_clock_at_click_not_previous_render(self):
        clock = [DAY]
        with patch.object(dates, 'local_today', side_effect=lambda _now=None: clock[0]):
            at = AppTest.from_string('from analysis_date import render_date_selector\nrender_date_selector()').run()
            clock[0] += timedelta(days=1)
            at.button(key='analysis_tomorrow').click().run()
            self.assertFalse(at.exception)
            self.assertEqual(at.session_state[dates.DAY_KEY], DAY + timedelta(days=2))


if __name__ == '__main__':
    unittest.main()
