# pylint: disable=invalid-name,protected-access,too-many-lines
import unittest
import sys
import types


if 'xbmc' not in sys.modules:
    xbmc_stub = types.ModuleType('xbmc')
    xbmc_stub.LOGDEBUG = 0
    xbmc_stub.LOGINFO = 1
    xbmc_stub.LOGWARNING = 2
    xbmc_stub.LOGERROR = 4
    xbmc_stub.log = lambda *args, **kwargs: None
    xbmc_stub.executebuiltin = lambda *args, **kwargs: None
    sys.modules['xbmc'] = xbmc_stub

if 'xbmcgui' not in sys.modules:
    xbmcgui_stub = types.ModuleType('xbmcgui')

    class _WindowStub(object):
        def __init__(self, *_args, **_kwargs):
            pass

        def getProperty(self, *_args, **_kwargs):
            return ''

        def clearProperty(self, *_args, **_kwargs):
            return None

    xbmcgui_stub.Window = _WindowStub
    sys.modules['xbmcgui'] = xbmcgui_stub

from python.lib.tmdbscraper import get_imdb_id
from python.lib.tmdbscraper import tmdb as tmdb_module
from python.lib.tmdbscraper_direct import tmdb as tmdb_direct_module

class TestScraperTMDBScraper(unittest.TestCase):
    def test_get_imdbid_from_uniqueids(self):
        input_model = {'imdb': 'tt1234', 'tmdb': '4321'}
        expected_output = 'tt1234'

        actual_output = get_imdb_id(input_model)

        self.assertEqual(expected_output, actual_output)

    def test_get_imdbid_from_uniqueids__no_imdb(self):
        input_model = {'tmdb': '4321'}

        actual_output = get_imdb_id(input_model)

        self.assertIsNone(actual_output)

    def test_get_imdbid_from_uniqueids__malformed_imdb(self):
        input_model = {'imdb': '4321'}

        actual_output = get_imdb_id(input_model)

        self.assertIsNone(actual_output)

    def test_score_search_match_with_nested_alternative_titles(self):
        item = {
            'title': '平成狸合战',
            'original_title': '平成狸合戦ぽんぽこ',
            'alternative_titles': {
                'titles': [
                    {'iso_3166_1': 'CN', 'title': '百变狸猫'},
                ]
            },
            'release_date': '1994-07-16',
        }

        meta = tmdb_module._score_search_match(item, '百变狸猫', 1994)

        self.assertTrue(meta['exact'])
        self.assertTrue(meta['contains'])
        self.assertTrue(meta['year_ok'])

    def test_score_search_match_with_nested_alternative_titles_direct(self):
        item = {
            'title': '平成狸合战',
            'original_title': '平成狸合戦ぽんぽこ',
            'alternative_titles': {
                'results': [
                    {'iso_3166_1': 'CN', 'title': '百变狸猫'},
                ]
            },
            'release_date': '1994-07-16',
        }

        meta = tmdb_direct_module._score_search_match(item, '百变狸猫', 1994)

        self.assertTrue(meta['exact'])
        self.assertTrue(meta['contains'])
        self.assertTrue(meta['year_ok'])
