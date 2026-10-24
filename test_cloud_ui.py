"""Cloud failures suspend a page before its controls can mutate data."""
from contextlib import contextmanager
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import cloud_storage
import cloud_ui


def sample_app():
    import streamlit as st
    from cloud_ui import guarded
    @guarded
    def render():
        st.session_state['page_executions'] = st.session_state.get('page_executions', 0) + 1
        st.success('Page ready')
    render()


def busy_notice_app():
    from cloud_ui import _busy_notice
    _busy_notice(100, 'notice_retry')


def status_app():
    from cloud_ui import render_storage_status
    render_storage_status()


class CloudUiTests(unittest.TestCase):
    def setUp(self):
        configured = patch.object(cloud_storage, 'configured', return_value=True)
        configured.start()
        self.addCleanup(configured.stop)
        # Busy behavior tests never contact the configured real storage.
        preview = patch.object(cloud_ui, '_render_snapshot_preview', return_value=None)
        preview.start()
        self.addCleanup(preview.stop)

    def test_retry_reuses_saved_files_until_the_committed_revision_changes(self):
        cloud_ui._snapshot_preview.clear()
        self.addCleanup(cloud_ui._snapshot_preview.clear)
        reads = []
        @contextmanager
        def snapshot(**kwargs):
            reads.append(kwargs)
            yield object()
        with patch.object(cloud_storage, 'committed_snapshot', snapshot), \
                patch.object(cloud_ui, '_read_preview', return_value={'calendar': []}):
            for revision in (1, 1, 2, 2):
                self.assertEqual(cloud_ui._snapshot_preview('isolated-cache-test', '2026-10-24', revision),
                                 {'calendar': []})
        self.assertEqual(len(reads), 2)

    def test_required_storage_failure_does_not_execute_page(self):
        @contextmanager
        def failed():
            raise cloud_storage.CloudStorageError('Connexion absente.')
            yield
        with patch.object(cloud_storage, 'transaction', failed):
            app = AppTest.from_function(sample_app).run()
        self.assertFalse(app.exception)
        self.assertTrue(app.error)
        self.assertFalse(app.success)
        self.assertFalse(app.button)
        self.assertNotIn('page_executions', app.session_state)

    def test_busy_page_shows_retry_without_exception(self):
        @contextmanager
        def busy():
            raise cloud_storage.CloudStorageBusy('Busy')
            yield
        with patch.object(cloud_storage, 'transaction', busy):
            app = AppTest.from_function(sample_app).run()
        self.assertFalse(app.exception)
        self.assertTrue(app.info)
        self.assertFalse(app.success)
        self.assertEqual([button.label for button in app.button], ['Réessayer maintenant'])
        self.assertNotIn('page_executions', app.session_state)

    def test_retry_renders_page_when_shared_update_has_finished(self):
        attempts = []

        @contextmanager
        def first_busy_then_ready():
            attempts.append(None)
            if len(attempts) == 1:
                raise cloud_storage.CloudStorageBusy('Busy')
            yield

        with patch.object(cloud_storage, 'transaction', first_busy_then_ready):
            app = AppTest.from_function(sample_app).run()
            self.assertFalse(app.success)
            self.assertNotIn('page_executions', app.session_state)
            app.button[0].click().run()

        self.assertFalse(app.exception)
        self.assertEqual([message.value for message in app.success], ['Page ready'])
        self.assertEqual(app.session_state['page_executions'], 1)
        self.assertEqual(len(attempts), 2)
        self.assertFalse(app.button)
        self.assertFalse(app.info)

    def test_notice_waits_until_deadline_without_accessing_shared_data(self):
        with patch.object(cloud_ui, 'monotonic', return_value=99), \
                patch.object(cloud_ui.st, 'rerun') as rerun, \
                patch.object(cloud_storage, 'transaction', side_effect=AssertionError('No storage polling')), \
                patch.object(cloud_storage, 'status', side_effect=AssertionError('No database polling')), \
                patch('api_football.ApiFootballClient', side_effect=AssertionError('No API requests')):
            app = AppTest.from_function(busy_notice_app).run()

        self.assertFalse(app.exception)
        self.assertTrue(app.info)
        self.assertEqual([button.label for button in app.button], ['Réessayer maintenant'])
        self.assertTrue(any('10 secondes' in caption.value for caption in app.caption))
        rerun.assert_not_called()

    def test_manual_retry_recovers_without_the_automatic_fragment(self):
        attempts = []

        @contextmanager
        def first_busy_then_ready():
            attempts.append(None)
            if len(attempts) == 1:
                raise cloud_storage.CloudStorageBusy('Busy')
            yield

        with patch.object(cloud_storage, 'transaction', first_busy_then_ready), \
                patch.object(cloud_ui, '_retry_timer', return_value=None):
            app = AppTest.from_function(sample_app).run()
            self.assertFalse(app.exception)
            self.assertEqual([button.label for button in app.button], ['Réessayer maintenant'])
            self.assertNotIn('page_executions', app.session_state)
            app.button[0].click().run()

        self.assertFalse(app.exception)
        self.assertEqual([message.value for message in app.success], ['Page ready'])
        self.assertEqual(app.session_state['page_executions'], 1)
        self.assertEqual(len(attempts), 2)

    def test_notice_requests_one_full_app_rerun_at_deadline(self):
        with patch.object(cloud_ui, 'monotonic', return_value=100), \
                patch.object(cloud_ui.st, 'rerun') as rerun, \
                patch.object(cloud_storage, 'transaction', side_effect=AssertionError('No storage polling')), \
                patch.object(cloud_storage, 'status', side_effect=AssertionError('No database polling')), \
                patch('api_football.ApiFootballClient', side_effect=AssertionError('No API requests')):
            app = AppTest.from_function(busy_notice_app).run()

        self.assertFalse(app.exception)
        rerun.assert_called_once_with(scope='app')

    def test_notice_manual_retry_does_not_wait_for_deadline(self):
        with patch.object(cloud_ui, 'monotonic', return_value=99), \
                patch.object(cloud_ui.st, 'rerun') as rerun:
            app = AppTest.from_function(busy_notice_app).run()
            rerun.assert_not_called()
            app.button[0].click().run()

        self.assertFalse(app.exception)
        rerun.assert_called_once_with(scope='app')

    def test_manual_retry_while_still_busy_finishes_without_hot_loop(self):
        attempts = []

        @contextmanager
        def still_busy():
            attempts.append(None)
            raise cloud_storage.CloudStorageBusy('Busy')
            yield

        with patch.object(cloud_storage, 'transaction', still_busy), \
                patch.object(cloud_ui, 'monotonic', return_value=100):
            app = AppTest.from_function(sample_app).run(timeout=3)
            self.assertEqual(len(attempts), 1)
            app.button[0].click().run(timeout=3)

        self.assertFalse(app.exception)
        # AppTest starts a full run for the click. The retry button then requests
        # one more full run, where its trigger has reset and the new timer waits.
        self.assertEqual(len(attempts), 3)
        self.assertTrue(app.info)
        self.assertEqual([button.label for button in app.button], ['Réessayer maintenant'])
        self.assertFalse(app.success)
        self.assertNotIn('page_executions', app.session_state)

    def test_each_failed_attempt_creates_a_fresh_future_retry_deadline(self):
        executions = []

        @contextmanager
        def busy():
            raise cloud_storage.CloudStorageBusy('Busy')
            yield

        @cloud_ui.guarded
        def render():
            executions.append(None)

        with patch.object(cloud_storage, 'transaction', busy), \
                patch.object(cloud_ui, 'monotonic', side_effect=[100, 200]), \
                patch.object(cloud_ui, '_busy_notice') as notice, \
                patch.object(cloud_ui, '_arm_retry') as arm:
            render()
            render()

        self.assertEqual(executions, [])
        self.assertEqual([call.args[0] for call in notice.call_args_list], [None, None])
        self.assertEqual([call.args[0] for call in arm.call_args_list], [110, 210])
        self.assertEqual(notice.call_args_list[0].args[1], notice.call_args_list[1].args[1])

    def test_unconfigured_status_clearly_reports_local_only(self):
        with patch.object(cloud_storage, 'configured', return_value=False), \
                patch.object(cloud_storage, 'status', side_effect=AssertionError('No remote network')):
            app = AppTest.from_function(status_app).run()
        self.assertFalse(app.exception)
        self.assertIn('Base locale active', app.success[0].value)
        self.assertFalse(app.warning)

    def test_local_mode_does_not_install_cloud_polling(self):
        def local_app():
            import streamlit as st
            import cloud_ui
            st.session_state['_cloud_retry_due'] = 0
            st.session_state['_cloud_retry_watchdog'] = True
            cloud_ui.install_retry_watchdog()
            st.success('Local ready')
        with patch.object(cloud_storage, 'configured', return_value=False), \
                patch.object(cloud_ui, '_retry_watchdog', side_effect=AssertionError('No cloud polling')):
            app = AppTest.from_function(local_app).run()
        self.assertFalse(app.exception)
        self.assertNotIn('_cloud_retry_due', app.session_state)
        self.assertNotIn('_cloud_retry_watchdog', app.session_state)

    def test_installed_watchdog_avoids_conditional_fragment_and_clears_on_success(self):
        with patch.object(cloud_ui, '_retry_timer') as conditional, \
                patch.object(cloud_ui, 'monotonic', return_value=99):
            def app_body():
                import streamlit as st
                import cloud_ui
                cloud_ui.install_retry_watchdog()
                cloud_ui._busy_notice(100, 'watchdog_retry')
            app = AppTest.from_function(app_body).run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state['_cloud_retry_due'],100)
        conditional.assert_not_called()

    def test_busy_preview_does_not_replay_page_actions(self):
        @contextmanager
        def busy():
            raise cloud_storage.CloudStorageBusy('Busy')
            yield
        with patch.object(cloud_storage, 'transaction', busy), \
                patch.object(cloud_ui, '_render_snapshot_preview') as preview:
            app = AppTest.from_function(sample_app).run()
        self.assertFalse(app.exception)
        self.assertNotIn('page_executions',app.session_state)
        preview.assert_called_once_with()

    def test_watchdog_waits_for_slow_snapshot_then_arms_a_fresh_deadline(self):
        clock = [100]
        observed = []

        @contextmanager
        def busy():
            raise cloud_storage.CloudStorageBusy('Busy')
            yield

        def slow_preview():
            import streamlit as st
            # Simulate a pending deadline from an earlier attempt and a read
            # that takes 25s: the watchdog may tick while that read is active.
            self.assertNotIn('_cloud_retry_due', st.session_state)
            clock[0] = 125
            cloud_ui._retry_watchdog.__wrapped__()
            observed.append('preview_completed')

        def watchdog_app():
            import streamlit as st
            import cloud_ui
            st.session_state['_cloud_retry_due'] = 110
            cloud_ui.install_retry_watchdog()
            cloud_ui.guarded(lambda:st.success('Must not run'))()
            st.session_state['armed_after_preview'] = st.session_state['_cloud_retry_due']

        with patch.object(cloud_storage, 'transaction', busy), \
                patch.object(cloud_ui, 'monotonic', side_effect=lambda:clock[0]), \
                patch.object(cloud_ui, '_render_snapshot_preview', side_effect=slow_preview), \
                patch.object(cloud_ui.st, 'rerun') as rerun, \
                patch.object(cloud_ui, '_retry_timer') as standalone_timer:
            app = AppTest.from_function(watchdog_app).run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state['armed_after_preview'], 135)
            self.assertEqual(observed, ['preview_completed'])
            rerun.assert_not_called()
            standalone_timer.assert_not_called()
            # Exercise later watchdog ticks in the same session without
            # invoking storage or replaying the guarded page body.
            def tick_app():
                import cloud_ui
                cloud_ui._retry_watchdog.__wrapped__()
            clock[0] = 134
            with patch.object(cloud_storage, 'transaction', side_effect=AssertionError('No storage polling')):
                tick = AppTest.from_function(tick_app)
                tick.session_state['_cloud_retry_due'] = app.session_state['armed_after_preview']
                tick.run()
                self.assertFalse(tick.exception)
                rerun.assert_not_called()
                clock[0] = 135
                tick.run()
            self.assertFalse(tick.exception)
            rerun.assert_called_once_with(scope='app')

    def test_standalone_timer_is_not_registered_until_slow_preview_finishes(self):
        clock, events = [100], []

        @contextmanager
        def busy():
            raise cloud_storage.CloudStorageBusy('Busy')
            yield

        def preview():
            events.append('preview')
            clock[0] = 125

        with patch.object(cloud_storage, 'transaction', busy), \
                patch.object(cloud_ui, 'monotonic', side_effect=lambda:clock[0]), \
                patch.object(cloud_ui, '_render_snapshot_preview', side_effect=preview), \
                patch.object(cloud_ui, '_retry_timer', side_effect=lambda due:events.append(('timer',due))):
            app = AppTest.from_function(sample_app).run()
        self.assertFalse(app.exception)
        self.assertEqual(events, ['preview', ('timer',135)])
        self.assertEqual(app.session_state['_cloud_retry_due'],135)


class SnapshotKickoffCacheTests(unittest.TestCase):
    def test_elapsed_match_disappears_when_same_cached_snapshot_crosses_kickoff(self):
        class Clock(datetime):
            current = datetime(2026, 10, 24, 11, 59, 59, tzinfo=timezone.utc)

            @classmethod
            def now(cls, tz=None):
                return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)

        def row(identifier, kickoff):
            return dict(fixture=dict(match_id=str(identifier), timestamp=kickoff, league='39',
                            competition='Premier League', home=f'Home {identifier}', away=f'Away {identifier}'),
                        probabilities={'1st': .6, 'Égal': .2, '2nd': .2}, max_probability=.6)

        rows = [row(1, '2026-10-24T12:00:00'), row(2, '2026-10-24T13:00:00Z')]
        saved = dict(revision=7, updated_at='2026-10-24T11:58:00+00:00', day='2026-10-24',
                     calendar=[], entries=[], probabilities=dict(rows=rows))
        downloads = []

        @contextmanager
        def snapshot(**kwargs):
            downloads.append(kwargs)
            yield object()

        def preview_app():
            import cloud_ui
            cloud_ui._render_snapshot_preview()

        cloud_ui._snapshot_preview.clear()
        self.addCleanup(cloud_ui._snapshot_preview.clear)
        with patch.object(cloud_ui, 'datetime', Clock), \
                patch.object(cloud_storage, 'snapshot_identity', return_value=('offline-kickoff-test', 'isolated')), \
                patch.object(cloud_storage, 'committed_revision', return_value=7), \
                patch.object(cloud_storage, 'committed_snapshot', snapshot), \
                patch.object(cloud_ui, '_read_preview', return_value=saved), \
                patch.object(cloud_storage, '_backend', side_effect=AssertionError('No remote backend')), \
                patch('api_football.ApiFootballClient', side_effect=AssertionError('No API client')):
            app = AppTest.from_function(preview_app).run()
            app.segmented_control(key='cloud_snapshot_view').set_value('Fortes probabilités').run()
            self.assertFalse(app.exception)
            self.assertEqual(app.dataframe[0].value['Domicile'].tolist(), ['Home 1', 'Home 2'])
            Clock.current = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
            app.run()
            self.assertFalse(app.exception)
            self.assertEqual(app.dataframe[0].value['Domicile'].tolist(), ['Home 2'])
            self.assertEqual(app.dataframe[0].value['1st'].tolist(), [60.0])
        self.assertEqual(len(downloads), 1)
        self.assertEqual(len(saved['probabilities']['rows']), 2)


if __name__ == '__main__':
    unittest.main()
