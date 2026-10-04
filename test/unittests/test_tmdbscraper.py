# pylint: disable=invalid-name,protected-access,too-many-lines
import unittest
import sys
import types
from unittest.mock import patch


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
from python.lib.tmdbscraper import fanarttv as fanart_module
from python.lib.tmdbscraper_direct import fanarttv as fanart_direct_module
from python import scraper_config, scraper_datahelper
from test.unittests.test_image_resize import default_settings, query, PREFIX


class TestImageResizePipelines(unittest.TestCase):
    def test_search_previews_respect_widths_in_both_scrapers(self):
        for module in (tmdb_module, tmdb_direct_module):
            with self.subTest(module=module.__name__):
                settings = default_settings(enable_image_resize=True)
                scraper = module.TMDBMovieScraper(settings, 'zh-CN', 'us')
                scraper._urls = {'original': 'https://image.tmdb.org/t/p/original',
                                 'preview': 'https://image.tmdb.org/t/p/w780'}
                response = {'results': [{'id': 1, 'title': 'Example',
                                         'poster_path': '/poster.jpg',
                                         'backdrop_path': '/backdrop.jpg'}], 'total_pages': 1}
                with patch.object(module.tmdbapi, 'search_movie', return_value=response):
                    result = scraper.search('Example')[0]
                self.assertEqual(query(result['poster_path'])['w'], ['720'])
                self.assertEqual(query(result['backdrop_path'])['w'], ['1280'])

    def test_tmdb_and_fanart_movie_and_set_art_are_resized_after_merging(self):
        for tmdb, fanart in ((tmdb_module, fanart_module),
                             (tmdb_direct_module, fanart_direct_module)):
            for enabled in (False, True):
                with self.subTest(module=tmdb.__name__, enabled=enabled):
                    settings = default_settings(enable_image_resize=enabled,
                                                image_max_width_landscape='1000')
                    urls = {'original': 'https://image.tmdb.org/t/p/original',
                            'preview': 'https://image.tmdb.org/t/p/w780'}
                    images = {'posters': [{'file_path': '/other.jpg', 'iso_639_1': 'zh'},
                                          {'file_path': '/default.jpg', 'iso_639_1': 'zh'}],
                              'backdrops': [{'file_path': '/landscape.jpg', 'iso_639_1': 'zh'},
                                            {'file_path': '/fanart.jpg', 'iso_639_1': None}],
                              'logos': [{'file_path': '/logo.png', 'iso_639_1': 'zh'}]}
                    movie = {'images': images, 'poster_path': '/default.jpg'}
                    art = tmdb._parse_artwork(movie, {'images': images}, urls, 'zh-CN', PREFIX)
                    self.assertTrue(art['poster'][0]['url'].endswith('/default.jpg'))
                    details = {'available_art': art, 'info': {'studio': [], 'tag': []}, 'ratings': {}}
                    fanart_data = {
                        'movieposter': [{'url': 'https://assets.fanart.tv/fanart/poster.jpg', 'lang': 'zh'},
                                        {'url': 'https://assets.fanart.tv/fanart/keyart.jpg', 'lang': '00'}],
                        'moviebanner': [{'url': 'https://assets.fanart.tv/fanart/banner.jpg', 'lang': 'zh'}],
                        'hdmovielogo': [{'url': 'https://assets.fanart.tv/fanart/logo.png', 'lang': 'zh'}],
                    }
                    extra_art = fanart._parse_data(fanart_data, 'zh', settings=settings)
                    extra_art.update({'set.' + key: value.copy() for key, value in list(extra_art.items())})
                    scraper_datahelper.combine_scraped_details_available_artwork(
                        details, {'available_art': extra_art}, 'zh-CN', settings)
                    scraper_config.configure_tmdb_artwork(details, settings)
                    result = scraper_config.configure_scraped_details(details, settings)
                    for art_type, items in result['available_art'].items():
                        for image in items:
                            for key in ('url', 'preview'):
                                params = query(image[key])
                                if not enabled or art_type.endswith('clearlogo'):
                                    self.assertNotIn('w', params)
                                else:
                                    expected = ('720' if art_type.endswith(('poster', 'keyart')) else
                                                '1000' if art_type.endswith('landscape') else '1280')
                                    self.assertEqual(params['w'], [expected])
                                    self.assertIn('we', params)

    def test_landscape_reclassified_as_fanart_uses_final_type_width(self):
        settings = default_settings(enable_image_resize=True, landscape=False,
                                    image_max_width_landscape='1000', image_max_width_fanart='1200')
        details = {'info': {'studio': [], 'tag': []}, 'ratings': {},
                   'available_art': {'landscape': [{'url': PREFIX + 'https://example.com/a.jpg'}]}}
        scraper_config.configure_tmdb_artwork(details, settings)
        result = scraper_config.configure_scraped_details(details, settings)
        self.assertNotIn('landscape', result['available_art'])
        self.assertEqual(query(result['available_art']['fanart'][0]['url'])['w'], ['1200'])

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
