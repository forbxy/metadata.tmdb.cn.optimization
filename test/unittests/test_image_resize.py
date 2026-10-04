import copy
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlsplit
import xml.etree.ElementTree as ET

from python.lib.image_resize import ImageResizer
from python.scraper_config import PathSpecificSettings, configure_scraped_details


ROOT = Path(__file__).resolve().parents[2]
PREFIX = 'https://wsrv.nl/?url='
SOURCE = 'https://image.tmdb.org/t/p/original/poster.jpg'


def default_settings(**overrides):
    data = {}
    for setting in ET.parse(ROOT / 'resources/settings.xml').iter('setting'):
        value = setting.findtext('default', '')
        if setting.get('type') == 'boolean':
            value = value == 'true'
        data[setting.get('id')] = value
    data.update(overrides)
    return PathSpecificSettings(data, lambda _message: None)


def query(url):
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


class TestImageResize(unittest.TestCase):
    def test_disabled_by_default_preserves_urls_exactly(self):
        original = PREFIX + SOURCE
        self.assertEqual(ImageResizer(default_settings()).resize(original, 'poster'), original)

    def test_default_widths_and_movie_sets(self):
        resizer = ImageResizer(default_settings(enable_image_resize=True))
        for art_type, width in [('poster', 720), ('keyart', 720), ('thumb', 720),
                                ('discart', 720), ('fanart', 1280),
                                ('landscape', 1280), ('banner', 1280),
                                ('set.poster', 720), ('set.fanart', 1280)]:
            with self.subTest(art_type=art_type):
                params = query(resizer.resize(PREFIX + SOURCE, art_type))
                self.assertEqual(params['w'], [str(width)])
                self.assertEqual(params['url'], [SOURCE])
                self.assertIn('we', params)

    def test_clear_art_and_logo_default_to_original_size(self):
        resizer = ImageResizer(default_settings(enable_image_resize=True))
        for art_type in ('clearart', 'clearlogo', 'set.clearlogo', 'unknown'):
            self.assertEqual(resizer.resize(PREFIX + SOURCE, art_type), PREFIX + SOURCE)

    def test_empty_invalid_and_non_positive_widths_leave_original(self):
        for value in ('', '0', '-1', '12.5', 'invalid', '720&h=1'):
            with self.subTest(value=value):
                resizer = ImageResizer(default_settings(enable_image_resize=True,
                                                       image_max_width_poster=value))
                self.assertEqual(resizer.resize(PREFIX + SOURCE, 'poster'), PREFIX + SOURCE)

    def test_custom_width_can_enable_logo_resizing(self):
        resizer = ImageResizer(default_settings(enable_image_resize=True,
                                               image_max_width_clearlogo=' 500 '))
        self.assertEqual(query(resizer.resize(PREFIX + SOURCE, 'clearlogo'))['w'], ['500'])

    def test_only_wsrv_proxy_is_supported(self):
        for prefix in ('https://proxy.example/?url=', 'https://wsrv.nl.example/?url=',
                       'https://example.com/wsrv.nl/?url=', 'file://wsrv.nl/?url='):
            with self.subTest(prefix=prefix):
                resizer = ImageResizer(default_settings(enable_image_resize=True,
                                                       image_proxy_prefix=prefix))
                self.assertEqual(resizer.resize(prefix + SOURCE, 'poster'), prefix + SOURCE)
                self.assertEqual(resizer.resize(PREFIX + SOURCE, 'poster'), PREFIX + SOURCE)

    def test_http_wsrv_and_empty_proxy_fallback(self):
        for prefix in ('http://wsrv.nl/?url=', ''):
            with self.subTest(prefix=prefix):
                resizer = ImageResizer(default_settings(enable_image_resize=True,
                                                       image_proxy_prefix=prefix))
                self.assertEqual(query(resizer.resize((prefix or PREFIX) + SOURCE, 'poster'))['w'], ['720'])

    def test_direct_and_local_images_are_preserved(self):
        resizer = ImageResizer(default_settings(enable_image_resize=True))
        for url in (SOURCE, '/storage/poster.jpg', 'image://encoded/', '',
                    'https://wsrv.nl/?output=png'):
            self.assertEqual(resizer.resize(url, 'poster'), url)

    def test_source_query_percent_encoding_and_fragment_survive(self):
        source = 'https://example.com/海报%20test.jpg?a=1&b=a+b%26c#part'
        resizer = ImageResizer(default_settings(enable_image_resize=True))
        params = query(resizer.resize(PREFIX + source, 'poster'))
        self.assertEqual(params['url'], [source])
        self.assertEqual(params['w'], ['720'])
        self.assertNotIn('b', params)

    def test_encoded_urls_can_be_resized_repeatedly(self):
        resizer = ImageResizer(default_settings(enable_image_resize=True))
        first = resizer.resize(PREFIX + SOURCE, 'poster')
        self.assertEqual(resizer.resize(first, 'poster'), first)
        changed = ImageResizer(default_settings(enable_image_resize=True,
                                               image_max_width_poster='600')).resize(first, 'poster')
        self.assertEqual(query(changed)['w'], ['600'])
        self.assertEqual(query(changed)['url'], [SOURCE])

    def test_existing_size_parameters_are_replaced_and_other_options_preserved(self):
        prefix = 'https://wsrv.nl/?output=webp&w=2000&h=2000&fit=fill&dpr=2&url='
        resizer = ImageResizer(default_settings(enable_image_resize=True, image_proxy_prefix=prefix))
        params = query(resizer.resize(prefix + SOURCE, 'poster'))
        self.assertEqual(params['url'], [SOURCE])
        self.assertEqual(params['w'], ['720'])
        self.assertEqual(params['output'], ['webp'])
        for key in ('h', 'fit', 'dpr'):
            self.assertNotIn(key, params)

    def test_final_configuration_resizes_art_previews_cast_and_crew(self):
        details = {
            'info': {'title': 'Example', 'studio': [], 'director': [
                {'name': 'Director', 'thumbnail': PREFIX + SOURCE}],
                'credits': [{'name': 'Writer', 'thumbnail': PREFIX + SOURCE}, 'Legacy name'],
                'tag': []},
            'ratings': {},
            'cast': [{'name': 'Actor', 'thumbnail': PREFIX + SOURCE}],
            'available_art': {'poster': [{'url': PREFIX + SOURCE, 'preview': PREFIX + SOURCE}],
                              'set.landscape': [{'url': PREFIX + SOURCE, 'preview': PREFIX + SOURCE}],
                              'clearlogo': [{'url': PREFIX + SOURCE}]},
        }
        settings = default_settings(enable_image_resize=True, image_max_width_landscape='1000',
                                    image_max_width_thumb='360')
        result = configure_scraped_details(copy.deepcopy(details), settings)
        self.assertEqual(query(result['available_art']['poster'][0]['url'])['w'], ['720'])
        self.assertEqual(query(result['available_art']['poster'][0]['preview'])['w'], ['720'])
        self.assertEqual(query(result['available_art']['set.landscape'][0]['url'])['w'], ['1000'])
        self.assertEqual(result['available_art']['clearlogo'], details['available_art']['clearlogo'])
        for person in [result['cast'][0], result['info']['director'][0], result['info']['credits'][0]]:
            self.assertEqual(query(person['thumbnail'])['w'], ['360'])

    def test_settings_category_order_and_width_visibility(self):
        root = ET.parse(ROOT / 'resources/settings.xml')
        categories = [category.get('id') for category in root.iter('category')]
        self.assertEqual(categories.index('image_resize'), categories.index('proxy_custom') + 1)
        self.assertEqual(categories.index('dns_custom'), categories.index('image_resize') + 1)
        widths = [setting for setting in root.iter('setting')
                  if setting.get('id', '').startswith('image_max_width_')]
        self.assertEqual(len(widths), 9)
        for setting in widths:
            self.assertEqual(setting.findtext('constraints/allowempty'), 'true')
            self.assertIsNotNone(setting.find("dependencies/dependency[@type='visible']/condition[@setting='enable_image_resize']"))


if __name__ == '__main__':
    unittest.main()
