from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import urllib.parse
from unittest.mock import MagicMock, patch


# Load with isolated Kodi stubs; do not affect other scraper tests' imports.
KODI_MODULES = {name: MagicMock() for name in ('xbmc', 'xbmcgui', 'xbmcaddon', 'xbmcvfs')}
SPEC = importlib.util.spec_from_file_location(
    'image_cacher_test_module', Path(__file__).resolve().parents[2] / 'python/image_cacher.py'
)
image_cacher = importlib.util.module_from_spec(SPEC)
with patch.dict(sys.modules, KODI_MODULES):
    SPEC.loader.exec_module(image_cacher)


class TestImageURLs(unittest.TestCase):
    def test_source_url_round_trip_preserves_encoding_and_headers(self):
        source = 'https://wsrv.nl/?url=example.org/电影%2F海报.png&w=720|User-Agent=Test%20Agent'
        wrapped = image_cacher.get_image_url(source)
        self.assertTrue(wrapped.startswith('image://'))
        self.assertEqual(source, image_cacher.get_texture_key(wrapped))
        self.assertEqual(wrapped, image_cacher.get_image_url(wrapped))

    def test_wrapped_transform_keeps_options_and_separate_cache_identity(self):
        source = 'https://example.org/poster.png'
        wrapped = 'image://' + urllib.parse.quote(source, safe='') + '/?width=720&height=1080'
        result = image_cacher.get_image_url(wrapped)
        self.assertTrue(result.endswith('/?width=720&height=1080'))
        self.assertEqual(source, urllib.parse.unquote(urllib.parse.urlsplit(result).netloc))
        self.assertEqual(result, image_cacher.get_texture_key(result))

    def test_typed_native_image_is_preserved(self):
        wrapped = 'image://video@%2Fstorage%2Fmovie.mkv/'
        result = image_cacher.get_image_url(wrapped)
        self.assertEqual('image://video@%2fstorage%2fmovie.mkv/', result)
        self.assertEqual(result, image_cacher.get_texture_key(result))

    def test_webdav_aliases_use_native_protocols(self):
        for scheme, native in (('webdav', 'dav'), ('webdavs', 'davs'), ('dav', 'dav'), ('davs', 'davs')):
            with self.subTest(scheme=scheme):
                self.assertEqual(
                    native + '://example.org/poster.jpg',
                    image_cacher.get_texture_key(image_cacher.get_image_url(scheme + '://example.org/poster.jpg'))
                )

    def test_unsupported_or_empty_source_is_skipped(self):
        for source in (None, '', '/storage/poster.jpg', 'plugin://example/item'):
            with self.subTest(source=source):
                self.assertIsNone(image_cacher.get_image_url(source))


class TestImageCacher(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='native-cache-test-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.db = self.root / 'Textures13.db'
        self.thumbs = self.root / 'Thumbnails'
        self.thumbs.mkdir()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('CREATE TABLE texture (id INTEGER PRIMARY KEY, url TEXT, cachedurl TEXT)')
        for module in KODI_MODULES.values():
            module.reset_mock(return_value=True, side_effect=True)
        image_cacher.ADDON.getSetting.return_value = '8'
        KODI_MODULES['xbmcvfs'].translatePath.side_effect = self.translate_path
        KODI_MODULES['xbmc'].executeJSONRPC.return_value = json.dumps({'result': 'OK'})
        self.file = MagicMock()
        self.file.readBytes.return_value = bytearray(b'\xff')
        KODI_MODULES['xbmcvfs'].File.return_value = self.file
        self.cacher = image_cacher.ImageCacher()
        self.source = 'https://example.org/poster.png?width=720'

    def translate_path(self, path):
        if path == 'special://database/Textures13.db':
            return str(self.db)
        return str(self.thumbs / path[len('special://thumbnails/'):])

    def cached_row(self, key=None, content=b'cached image'):
        cachedurl = 'a/native.jpg'
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('INSERT INTO texture VALUES (42, ?, ?)', (key or self.source, cachedurl))
        physical = self.thumbs / cachedurl
        physical.parent.mkdir(exist_ok=True)
        if content is not None:
            physical.write_bytes(content)
        return physical

    def test_native_entry_creates_cache_without_manual_db_write(self):
        self.assertEqual('success', self.cacher.download_image(self.source))
        KODI_MODULES['xbmcvfs'].File.assert_called_once_with(image_cacher.get_image_url(self.source))
        self.file.readBytes.assert_called_once_with(1)
        self.file.close.assert_called_once()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual([], conn.execute('SELECT * FROM texture').fetchall())

    def test_existing_cache_skips_native_open(self):
        self.cached_row()
        self.assertEqual('skipped', self.cacher.download_image(self.source))
        KODI_MODULES['xbmcvfs'].File.assert_not_called()
        KODI_MODULES['xbmc'].executeJSONRPC.assert_not_called()

    def test_wrapped_plain_source_also_skips_existing_cache(self):
        self.cached_row()
        self.assertEqual('skipped', self.cacher.download_image(image_cacher.get_image_url(self.source)))
        KODI_MODULES['xbmcvfs'].File.assert_not_called()

    def test_transform_does_not_reuse_untransformed_cache(self):
        self.cached_row()
        wrapped = image_cacher.get_image_url(self.source) + '?size=thumb'
        self.assertEqual('success', self.cacher.download_image(wrapped))
        KODI_MODULES['xbmcvfs'].File.assert_called_once_with(wrapped)

    def test_existing_transformed_cache_is_skipped(self):
        wrapped = image_cacher.get_image_url(self.source) + '?size=thumb'
        self.cached_row(key=wrapped)
        self.assertEqual('skipped', self.cacher.download_image(wrapped))
        KODI_MODULES['xbmcvfs'].File.assert_not_called()

    def test_missing_file_is_removed_through_kodi_before_regenerating(self):
        self.cached_row(content=None)
        self.assertEqual('success', self.cacher.download_image(self.source))
        request = json.loads(KODI_MODULES['xbmc'].executeJSONRPC.call_args.args[0])
        self.assertEqual('Textures.RemoveTexture', request['method'])
        self.assertEqual({'textureid': 42}, request['params'])
        # The Python tool does not delete or rewrite the row itself.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual(1, conn.execute('SELECT count(*) FROM texture').fetchone()[0])

    def test_empty_file_is_regenerated(self):
        self.cached_row(content=b'')
        self.assertEqual('success', self.cacher.download_image(self.source))
        KODI_MODULES['xbmc'].executeJSONRPC.assert_called_once()

    def test_stale_removal_failure_is_reported(self):
        self.cached_row(content=None)
        KODI_MODULES['xbmc'].executeJSONRPC.return_value = json.dumps({'error': {'code': -1}})
        self.assertEqual('failed', self.cacher.download_image(self.source))
        KODI_MODULES['xbmcvfs'].File.assert_not_called()

    def test_absent_db_is_not_created_by_cache_query(self):
        self.db.unlink()
        self.assertEqual('success', self.cacher.download_image(self.source))
        self.assertFalse(self.db.exists())

    def test_empty_native_read_is_failure_and_closes_file(self):
        self.file.readBytes.return_value = bytearray()
        self.assertEqual('failed', self.cacher.download_image(self.source))
        self.file.close.assert_called_once()

    def test_native_read_exception_is_failure_and_closes_file(self):
        self.file.readBytes.side_effect = OSError('native decoder failure')
        self.assertEqual('failed', self.cacher.download_image(self.source))
        self.file.close.assert_called_once()

    def test_native_open_exception_is_failure(self):
        KODI_MODULES['xbmcvfs'].File.side_effect = OSError('open failure')
        self.assertEqual('failed', self.cacher.download_image(self.source))

    def test_raw_and_wrapped_library_art_are_deduplicated(self):
        self.cacher.get_video_items = MagicMock(return_value=[
            {'art': {'poster': self.source, 'fanart': image_cacher.get_image_url(self.source)}},
            {'art': {'set.poster': self.source, 'poster': '/storage/ignored.jpg'}},
        ])
        self.cacher.download_image = MagicMock(return_value='success')
        progress = KODI_MODULES['xbmcgui'].DialogProgress.return_value
        progress.iscanceled.return_value = False
        self.cacher.start()
        self.cacher.download_image.assert_called_once_with(image_cacher.get_image_url(self.source))
        progress.close.assert_called_once()
        self.assertIn('成功缓存: 1', KODI_MODULES['xbmcgui'].Dialog.return_value.ok.call_args.args[1])


if __name__ == '__main__':
    unittest.main()
