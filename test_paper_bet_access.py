"""Paper-bet ownership stays separate from shared administration rights."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import admin_access
import paper_bet_access as access


AUTH = dict(redirect_uri='https://example.test/oauth2callback', cookie_secret='private-cookie',
            client_id='private-client', client_secret='private-secret',
            server_metadata_url='https://issuer.test/.well-known/openid-configuration')
CLAIMS = dict(is_logged_in=True, iss='https://issuer.test', sub='private-owner-subject', exp=2000,
              email='owner@example.test', email_verified=True)
NOTICE = 'from paper_bet_access import render_notice\nrender_notice()'
MUTATION = '''import streamlit as st
import paper_bet_access as access
st.session_state.setdefault('mutations', 0)
if st.button('Ajouter', disabled=not access.can_edit()):
    access.require_edit()
    st.session_state['mutations'] += 1
'''


class PaperBetAccessTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(auth=deepcopy(AUTH),
                           access=dict(admin_emails=['owner@example.test'], shared_writes=False))
        self.claims = deepcopy(CLAIMS)
        self.enterContext(patch.object(admin_access, '_secrets', side_effect=lambda: self.config))
        self.enterContext(patch.object(admin_access, '_claims', side_effect=lambda: self.claims))
        self.enterContext(patch.object(admin_access.st, 'get_option', return_value='0.0.0.0'))
        self.enterContext(patch.object(admin_access, 'time', return_value=1000))

    def test_locked_owner_can_edit_without_shared_administration(self):
        self.assertTrue(access.can_edit())
        self.assertFalse(admin_access.can_admin())
        at = AppTest.from_string(MUTATION).run()
        self.assertFalse(at.exception)
        self.assertFalse(at.button[0].disabled)
        at.button[0].click().run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state['mutations'], 1)
        self.assertFalse(admin_access.can_admin())

    def test_missing_unlisted_unverified_and_expired_identity_cannot_edit(self):
        for claims in ({}, dict(CLAIMS, is_logged_in=False), dict(CLAIMS, email='visitor@example.test'),
                       dict(CLAIMS, email_verified=False), dict(CLAIMS, exp=999)):
            with self.subTest(claims=claims):
                self.claims = claims
                self.assertFalse(access.can_edit())
        self.claims = deepcopy(CLAIMS)
        self.config = {}
        self.assertFalse(access.can_edit())

    def test_forged_event_cannot_mutate_for_a_visitor(self):
        self.claims = {}
        with patch.object(access.st, 'button', return_value=True):
            at = AppTest.from_string(MUTATION)
            at.session_state['admin'] = True
            at.session_state['owner'] = True
            at.query_params['admin'] = 'true'
            at.run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state['mutations'], 0)
        self.assertEqual(at.warning[0].value, access.DENIED)

    def test_owner_revoked_after_render_is_stopped_before_mutation(self):
        def revoke(*args, **kwargs):
            self.config['access']['admin_emails'] = []
            return True

        with patch.object(access.st, 'button', side_effect=revoke):
            at = AppTest.from_string(MUTATION).run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state['mutations'], 0)
        self.assertEqual(at.warning[0].value, access.DENIED)

    def test_owner_notice_explains_portfolio_access_and_preserves_lock(self):
        at = AppTest.from_string(NOTICE).run()
        self.assertFalse(at.exception)
        text = ' '.join(value.value for value in at.caption)
        self.assertIn('tu peux ajouter tes paris simulés', text)
        self.assertIn('restent verrouillés', text)
        self.assertFalse(at.button)
        self.assertFalse(admin_access.can_admin())

    def test_signed_out_visitor_can_start_native_login_without_secrets_in_notice(self):
        self.claims = {}
        with patch.object(access.st, 'login') as login:
            at = AppTest.from_string(NOTICE).run()
            self.assertFalse(at.exception)
            self.assertIn('propriétaire', at.caption[0].value)
            at.button(key='paper_bet_login').click().run()
            self.assertFalse(at.exception)
            login.assert_called_once_with(None)
        text = ' '.join(value.value for value in at.caption)
        for private in (*AUTH.values(), CLAIMS['email'], CLAIMS['sub']):
            self.assertNotIn(private, text)

    def test_named_provider_is_forwarded_to_native_login(self):
        self.claims = {}
        self.config['access']['provider'] = 'google'
        self.config['auth'] = dict(redirect_uri=AUTH['redirect_uri'], cookie_secret=AUTH['cookie_secret'],
                                   google={key: AUTH[key] for key in
                                           ('client_id', 'client_secret', 'server_metadata_url')})
        with patch.object(access.st, 'login') as login:
            at = AppTest.from_string(NOTICE).run()
            at.button(key='paper_bet_login').click().run()
            self.assertFalse(at.exception)
            login.assert_called_once_with('google')

    def test_logged_in_unlisted_or_expired_session_is_read_only_with_renewal_guidance(self):
        for claims in (dict(CLAIMS, email='visitor@example.test'), dict(CLAIMS, exp=999)):
            with self.subTest(claims=claims):
                self.claims = claims
                at = AppTest.from_string(NOTICE).run()
                self.assertFalse(at.exception)
                self.assertTrue(any('session a expiré' in value.value for value in at.caption))
                self.assertFalse(at.button)

    def test_no_login_is_offered_without_complete_server_configuration(self):
        self.claims = {}
        del self.config['auth']['client_secret']
        at = AppTest.from_string(NOTICE).run()
        self.assertFalse(at.exception)
        self.assertFalse(at.button)


if __name__ == '__main__':
    unittest.main()
