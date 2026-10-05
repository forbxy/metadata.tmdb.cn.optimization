from concurrent.futures import Future
from contextlib import closing
import importlib.util
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch


KODI_MODULES = {name: MagicMock() for name in (
    'xbmc', 'xbmcgui', 'xbmcaddon', 'xbmcvfs', 'scraper_direct',
    'lib', 'lib.tmdbscraper_direct',
)}
SPEC = importlib.util.spec_from_file_location(
    'kodi_scraper_database_test_module',
    Path(__file__).resolve().parents[2] / 'python/kodi_scraper_thread.py',
)
scraper = importlib.util.module_from_spec(SPEC)
with patch.dict(sys.modules, KODI_MODULES), patch.object(sys, 'path', sys.path.copy()):
    SPEC.loader.exec_module(scraper)

SCHEMA = (Path(__file__).parent / 'fixtures/kodi22_video_schema.sql').read_text()
KODI21_VIEW = (Path(__file__).parent / 'fixtures/kodi21_movie_view.sql').read_text()
DETAILS = {'info': {'title': 'Example', 'premiered': '2023-01-01'},
           'uniqueids': {'tmdb': '123'}, 'ratings': {'themoviedb': {'rating': 8, 'votes': 10}}}


class TestKodiDatabase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db_path = str(Path(self.directory.name) / 'MyVideos149.db')
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.executescript(SCHEMA)
        self.db = scraper.KodiDatabase(self.db_path)
        self.db.connect()
        self.addCleanup(self.db.close)

    def save(self, filename='example.mkv', merge=False, directory='/movies/'):
        id_path = self.db.get_or_create_path(directory)
        id_file = self.db.get_or_create_file(directory + filename, id_path)
        return self.db.save_movie(id_file, DETAILS, directory + filename, merge)

    def counts(self):
        return tuple(self.db.conn.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                     for table in ('movie', 'movie_view'))

    def bind_source(self, path='/movies/', addon='metadata.tmdb.cn.optimization'):
        self.db.get_or_create_path(path)
        self.db.conn.execute("UPDATE path SET strContent='movies', strScraper=? WHERE strPath=?", (addon, path))
        self.db.conn.commit()

    def use_kodi21_schema(self):
        self.db.conn.execute('DROP VIEW movie_view')
        self.db.conn.execute('ALTER TABLE movie DROP COLUMN originalLanguage')
        self.db.conn.execute('ALTER TABLE sets DROP COLUMN strOriginalSet')
        self.db.conn.execute('UPDATE videoversiontype SET itemType=0')
        self.db.conn.executescript(KODI21_VIEW)
        self.db._load_video_version_item_type()

    def add_extra(self, movie_id):
        extra_type = self.db.video_version_item_type + 1
        id_path = self.db.get_or_create_path('/movies/extras/')
        id_file = self.db.get_or_create_file('/movies/extras/making-of.mkv', id_path)
        self.db.conn.execute("INSERT INTO videoversiontype VALUES (40850, 'Making of', 2, ?)", (extra_type,))
        self.db.conn.execute("INSERT INTO videoversion VALUES (?, ?, 'movie', ?, 40850)", (id_file, movie_id, extra_type))
        self.db.conn.commit()
        return id_file

    def test_new_movie_is_visible_in_kodi22_view(self):
        movie_id = self.save()
        self.assertEqual((1, 1), self.counts())
        row = self.db.conn.execute('SELECT idMovie, isDefaultVersion FROM movie_view').fetchone()
        self.assertEqual((movie_id, 1), tuple(row))

    def test_merge_keeps_default_and_additional_versions_visible(self):
        movie_id = self.save()
        self.assertEqual(movie_id, self.save('extended.mkv', merge=True))
        self.assertEqual((1, 2), self.counts())
        self.assertEqual(1, self.db.conn.execute('SELECT count(*) FROM movie_view WHERE isDefaultVersion=1').fetchone()[0])
        self.assertEqual({'example.mkv', 'extended.mkv'},
                         {row[0] for row in self.db.conn.execute('SELECT videoVersionTypeName FROM movie_view')})

    def test_version_names_do_not_reuse_extra_types(self):
        self.db.conn.execute("INSERT INTO videoversiontype VALUES (40801, 'extended.mkv', 2, 2)")
        type_id = self.db.get_video_version_type_id('extended.mkv')
        self.assertNotEqual(40801, type_id)
        self.assertEqual(1, self.db.conn.execute('SELECT itemType FROM videoversiontype WHERE id=?', (type_id,)).fetchone()[0])

    def test_kodi21_version_zero_remains_supported(self):
        self.use_kodi21_schema()
        self.save()
        self.save('extended.mkv', merge=True)
        self.assertEqual((1, 2), self.counts())
        self.assertEqual({0}, {row[0] for row in self.db.conn.execute('SELECT itemType FROM videoversion')})

    def test_kodi21_cache_includes_versions_and_extras(self):
        self.use_kodi21_schema()
        movie_id = self.save()
        self.save('extended.mkv', merge=True)
        self.add_extra(movie_id)
        self.assertEqual((1, 2), self.counts())  # Kodi 21's view excludes extras.
        sim = scraper.KodiScraperSimulation()
        sim.db = self.db
        sim.load_scraped_files()
        self.assertEqual({'/movies/example.mkv', '/movies/extended.mkv', '/movies/extras/making-of.mkv'},
                         sim.scraped_files)

    def test_kodi21_repair_leaves_valid_version_zero_untouched(self):
        self.use_kodi21_schema()
        self.bind_source()
        self.save()
        before = [tuple(row) for row in self.db.conn.execute('SELECT * FROM videoversion')]
        self.assertEqual(0, self.db.repair_legacy_video_versions(self.db.get_all_paths()))
        self.assertEqual(before, [tuple(row) for row in self.db.conn.execute('SELECT * FROM videoversion')])

    def test_kodi22_cache_includes_extras_and_excludes_invalid_versions(self):
        movie_id = self.save()
        self.add_extra(movie_id)
        sim = scraper.KodiScraperSimulation()
        sim.db = self.db
        sim.load_scraped_files()
        self.assertEqual({'/movies/example.mkv', '/movies/extras/making-of.mkv'}, sim.scraped_files)
        self.db.conn.execute("UPDATE videoversion SET itemType=0 WHERE itemType=1")
        sim.scraped_files.clear()
        sim.load_scraped_files()
        self.assertEqual(set(), sim.scraped_files)

    def test_kodi20_without_version_tables_saves_movie(self):
        self.db.conn.executescript('''DROP VIEW movie_view;
            DROP TABLE videoversion; DROP TABLE videoversiontype;
            CREATE VIEW movie_view AS SELECT m.*, f.strFilename, p.strPath
                FROM movie m JOIN files f ON f.idFile=m.idFile JOIN path p ON p.idPath=f.idPath;''')
        self.db._load_video_version_item_type()
        self.save(merge=True)
        self.assertEqual((1, 1), self.counts())

    def test_repair_restores_legacy_default_and_custom_versions_once(self):
        self.bind_source()
        self.save()
        self.save('extended.mkv', merge=True)
        self.db.conn.execute('UPDATE videoversion SET itemType=0')
        self.db.conn.execute('UPDATE videoversiontype SET itemType=0 WHERE owner=2')
        self.assertEqual(0, self.db.conn.execute('SELECT count(*) FROM movie_view WHERE isDefaultVersion=1').fetchone()[0])
        self.assertEqual(2, self.db.repair_legacy_video_versions(self.db.get_all_paths()))
        self.assertEqual((1, 2), self.counts())
        self.assertEqual(0, self.db.repair_legacy_video_versions(self.db.get_all_paths()))

    def test_repair_respects_nested_source_scraper(self):
        self.bind_source()
        self.bind_source('/movies/other/', 'another.scraper')
        self.save(directory='/movies/other/')
        self.db.conn.execute('UPDATE videoversion SET itemType=0')
        self.assertEqual(0, self.db.repair_legacy_video_versions(self.db.get_all_paths()))
        self.assertEqual((1, 0), self.counts())

    def test_failed_metadata_update_rolls_back_movie_and_file(self):
        self.db.conn.execute('''CREATE TRIGGER fail_movie BEFORE UPDATE ON movie
            BEGIN SELECT RAISE(ABORT, 'write failure'); END''')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'write failure'):
            self.save()
        self.assertEqual((0, 0), self.counts())
        self.assertEqual(0, self.db.conn.execute('SELECT count(*) FROM files').fetchone()[0])
        self.db.conn.execute('DROP TRIGGER fail_movie')
        self.save()
        self.assertEqual((1, 1), self.counts())

    def test_failed_version_insert_rolls_back_movie(self):
        self.db.conn.execute('''CREATE TRIGGER fail_version BEFORE INSERT ON videoversion
            BEGIN SELECT RAISE(ABORT, 'version failure'); END''')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'version failure'):
            self.save()
        self.assertEqual((0, 0), self.counts())

    def test_failed_merge_rolls_back_changes_to_existing_version(self):
        self.save()
        self.db.conn.execute('''CREATE TRIGGER fail_version BEFORE INSERT ON videoversion
            BEGIN SELECT RAISE(ABORT, 'merge failure'); END''')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'merge failure'):
            self.save('extended.mkv', merge=True)
        self.assertEqual((1, 1), self.counts())
        self.assertEqual(40400, self.db.conn.execute('SELECT idType FROM videoversion').fetchone()[0])

    def test_retry_incomplete_legacy_movie_repairs_its_version_link(self):
        self.save()
        self.db.conn.execute("UPDATE movie SET c00=''")
        self.db.conn.execute('UPDATE videoversion SET itemType=0')
        self.db.conn.commit()
        self.save()
        self.assertEqual((1, 1), self.counts())

    def test_scraped_cache_excludes_hidden_and_incomplete_rows(self):
        self.save()
        self.db.conn.execute('UPDATE videoversion SET itemType=0')
        sim = scraper.KodiScraperSimulation()
        sim.db = self.db
        sim.load_scraped_files()
        self.assertEqual(set(), sim.scraped_files)
        self.db.conn.execute('UPDATE videoversion SET itemType=1')
        self.db.conn.execute("UPDATE movie SET c00='' ")
        sim.load_scraped_files()
        self.assertEqual(set(), sim.scraped_files)

    def test_database_connection_failure_is_propagated(self):
        broken = scraper.KodiDatabase(str(Path(self.directory.name) / 'missing' / 'video.db'))
        with self.assertRaises(sqlite3.OperationalError):
            broken.connect()
        self.assertIsNone(broken.conn)

    def test_mysql_dict_cursor_reads_standard_version_type(self):
        db = scraper.KodiDatabase(mysql_config={})
        db.conn = MagicMock()
        cursor = db.conn.cursor.return_value
        cursor.fetchone.side_effect = [{'table': 'videoversiontype'}, {'itemType': 1}]
        db._load_video_version_item_type()
        self.assertEqual(1, db.video_version_item_type)
        cursor.execute.assert_called_with('SELECT itemType FROM videoversiontype WHERE id=%s', (40400,))


class TestScraperSaveStatus(unittest.TestCase):
    def run_completed(self, db):
        sim = scraper.KodiScraperSimulation()
        sim.db = db
        future = Future()
        future.set_result(DETAILS)
        sim.future_map[future] = ('/movies/example.mkv', None, 1, False)
        sim.running_futures.add(future)
        sim.handle_finished_futures({future})
        return sim

    def test_save_error_counts_as_failure_and_keeps_reason(self):
        db = MagicMock()
        db.save_movie.side_effect = sqlite3.OperationalError('database is locked')
        sim = self.run_completed(db)
        self.assertEqual((1, 0, 1), (sim.stats_processed, sim.stats_success, sim.stats_failed))
        self.assertIn('database is locked', sim.failed_items[0]['history'][0])
        db.conn.rollback.assert_called_once()

    def test_no_database_counts_as_failure(self):
        sim = self.run_completed(None)
        self.assertEqual((0, 1), (sim.stats_success, sim.stats_failed))

    def test_success_count_requires_saved_movie_id(self):
        db = MagicMock()
        db.save_movie.return_value = 42
        sim = self.run_completed(db)
        self.assertEqual((1, 0), (sim.stats_success, sim.stats_failed))
        self.assertEqual([], sim.failed_items)

    def test_kodi22_uses_active_database_name(self):
        with patch.object(scraper.xbmc, 'getDatabaseName', return_value='MyVideos149'), \
                patch.object(scraper, 'translatePath', return_value='/database'):
            self.assertEqual('/database/MyVideos149.db', scraper.KodiScraperSimulation().get_latest_db_path())

    def test_kodi21_without_database_name_api_uses_existing_fallback(self):
        with patch.object(scraper, 'xbmc', types.SimpleNamespace()), \
                patch.object(scraper, 'translatePath', return_value='/database'), \
                patch.object(scraper.xbmcvfs, 'listdir', return_value=([], ['MyVideos121.db', 'MyVideos131.db'])):
            self.assertEqual('/database/MyVideos131.db', scraper.KodiScraperSimulation().get_latest_db_path())

    def test_missing_active_database_does_not_fall_back_to_old_copy(self):
        with patch.object(scraper.xbmc, 'getDatabaseName', return_value='MyVideos149'), \
                patch.object(scraper.xbmcvfs, 'exists', return_value=False):
            self.assertIsNone(scraper.KodiScraperSimulation().get_latest_db_path())

    def test_mysql_uses_kodi22_active_custom_database_name(self):
        sim = scraper.KodiScraperSimulation()
        with patch.object(sim, '_parse_mysql_from_xml', return_value={'database': 'Videos'}), \
                patch.object(scraper.xbmc, 'getDatabaseName', return_value='Videos149'):
            self.assertEqual('Videos149', sim.get_mysql_config()['database'])
