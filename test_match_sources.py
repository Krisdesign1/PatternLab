"""Legacy sources are rejected before any read, including old coupon inputs."""
from unittest import TestCase
from unittest.mock import patch
import api_football
import match_sources


class MatchSourceTests(TestCase):
    def test_only_api_results_are_loaded_and_coupons_are_ignored(self):
        row={'match_id':'1','provider':'api-football'}
        with patch.object(api_football,'load_matches',return_value=([row],{})) as load:
            self.assertEqual(match_sources.load_matches([{'sequence':['1st','2nd']}]),([row],{}))
            load.assert_called_once_with(api_football.STORE_PATH)
        self.assertEqual(match_sources.SOURCES,('API-Football',))

    def test_retired_sources_fail_before_any_api_access(self):
        with patch.object(api_football,'load_matches') as load, patch.object(api_football,'load_fixtures') as fixtures:
            for source in ('Forebet','football-data.org','Unknown'):
                for operation,args in ((match_sources.load_matches,([],source)),(match_sources.data_signature,(source,)),
                                       (match_sources.scheduled_fixtures,(source,'39'))):
                    with self.subTest(source=source,operation=operation.__name__),self.assertRaises(ValueError):operation(*args)
            load.assert_not_called();fixtures.assert_not_called()
