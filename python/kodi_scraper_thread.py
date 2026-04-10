import re
import os
import urllib.parse
import json
import time
import xbmc
import xbmcvfs
import xbmcgui
import xbmcaddon
import sys
import sqlite3
import traceback
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from xbmcvfs import translatePath

ADDON_SETTINGS = xbmcaddon.Addon()

# Ensure we can import from the same directory
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.append(script_dir)


from scraper_direct import ScraperRunner
from lib.tmdbscraper_direct import dns_override


def log(message, level=xbmc.LOGDEBUG):
    xbmc.log(f"[TMDB Thread] {message}", level)


class _RowProxy:
    """Allows both dict-like (row['col']) and index (row[0]) access for MySQL DictCursor rows."""
    __slots__ = ('_dict', '_values')
    def __init__(self, row_dict):
        self._dict = row_dict
        self._values = list(row_dict.values())
    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return self._dict[key]
    def __contains__(self, key):
        return key in self._dict
    def __bool__(self):
        return True


class _MySQLCursorWrapper:
    """Wraps a pymysql DictCursor to auto-convert ? placeholders to %s and wrap rows for index access."""
    def __init__(self, cursor):
        self._cursor = cursor
    
    def execute(self, sql, params=None):
        sql = sql.replace('?', '%s')
        if params is not None:
            return self._cursor.execute(sql, params)
        return self._cursor.execute(sql)
    
    def fetchone(self):
        row = self._cursor.fetchone()
        if row is not None:
            return _RowProxy(row)
        return None
    
    def fetchall(self):
        return [_RowProxy(r) for r in self._cursor.fetchall()]
    
    @property
    def lastrowid(self):
        return self._cursor.lastrowid



class SettingsProxy:
    def __init__(self, base_settings, overrides):
        self.base_settings = base_settings
        self.overrides = overrides
        
    def getSetting(self, key):
        if key in self.overrides:
            return str(self.overrides[key])
        return self.base_settings.getSetting(key)

    def getSettingString(self, key):
        if key in self.overrides:
            return str(self.overrides[key])
        return self.base_settings.getSettingString(key)
        
    def getSettingBool(self, key):
        if key in self.overrides:
            val = self.overrides[key]
            # Handle XML boolean text usually being "true"/"false"
            return str(val).lower() == 'true'
        return self.base_settings.getSettingBool(key)
        
    def getSettingInt(self, key):
        if key in self.overrides:
            try: return int(self.overrides[key])
            except: return 0
        return self.base_settings.getSettingInt(key)

    def setSetting(self, key, value):
        return self.base_settings.setSetting(key, value)

class KodiDatabase:
    def __init__(self, db_path=None, mysql_config=None):
        """
        db_path: SQLite file path (used when mysql_config is None)
        mysql_config: dict with keys: host, port, user, password, database
        """
        self.db_path = db_path
        self.mysql_config = mysql_config
        self.is_mysql = mysql_config is not None
        self.conn = None
        
    def connect(self):
        try:
            if self.is_mysql:
                import pymysql
                cfg = self.mysql_config
                self.conn = pymysql.connect(
                    host=cfg['host'],
                    port=int(cfg['port']),
                    user=cfg['user'],
                    password=cfg['password'],
                    database=cfg['database'],
                    charset='utf8mb3',
                    collation='utf8mb3_general_ci',
                    cursorclass=pymysql.cursors.DictCursor,
                    autocommit=False
                )
                log(f"Connected to MySQL: {cfg['host']}:{cfg['port']}/{cfg['database']}", xbmc.LOGINFO)
            else:
                self.conn = sqlite3.connect(self.db_path)
                self.conn.row_factory = sqlite3.Row
        except Exception as e:
            log(f"DB Connect Error: {e}", xbmc.LOGERROR)

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def cursor(self):
        """Returns a cursor. For MySQL, wraps it to auto-convert ? to %s and support index-based row access."""
        raw = self.conn.cursor()
        if self.is_mysql:
            return _MySQLCursorWrapper(raw)
        return raw

    def _prepare_string_array(self, items, separator=" / "):
        if isinstance(items, list):
            return separator.join(items)
        return str(items) if items else ""

    def get_or_create_path(self, path):
        # Ensure path ends with /
        start_path = path.replace("\\", "/")
        if not start_path.endswith("/"):
            start_path += "/"
            
        cur = self.cursor()
        cur.execute("SELECT idPath FROM path WHERE strPath=?", (start_path,))
        row = cur.fetchone()
        if row: return row[0]
        
        # 2. 构建完整的父子目录关系 / Recursive parent creation
        # Use string manipulation instead of os.path to safely handle URLs
        path_no_slash = start_path.rstrip("/")
        last_slash = path_no_slash.rfind("/")
        
        if last_slash > 0:
            parent_path = path_no_slash[:last_slash] + "/"
            # Avoid infinite recursion (e.g. smb:// -> smb:/ -> smb://)
            if parent_path != start_path and "://" in start_path: 
                # For URLs ensure we don't break protocol
                if not parent_path.endswith(":///") and parent_path.count("/") >= 2:
                     self.get_or_create_path(parent_path)
            elif parent_path != start_path:
                 # Local paths
                 self.get_or_create_path(parent_path)
            
        # 1. path不需要写类型和刮削器 (Empty strings)
        # 3. strhash，strSettings需要写入空字符串
        cur.execute("INSERT INTO path (strPath, strContent, strScraper, strHash, strSettings, scanRecursive) VALUES (?, ?, ?, ?, ?, ?)", 
                   (start_path, "", "", "", "", 0))
        return cur.lastrowid

    def get_or_create_file(self, file_path, id_path):
        filename = os.path.basename(file_path)
        cur = self.cursor()
        cur.execute("SELECT idFile FROM files WHERE idPath=? AND strFilename=?", (id_path, filename))
        row = cur.fetchone()
        if row: return row[0]
        
        cur.execute("INSERT INTO files (idPath, strFilename, dateAdded) VALUES (?, ?, ?)", 
                   (id_path, filename, time.strftime("%Y-%m-%d %H:%M:%S")))
        return cur.lastrowid
        
    def get_or_create_set(self, set_name, set_overview=""):
        if not set_name: return None
        cur = self.cursor()
        cur.execute("SELECT idSet FROM sets WHERE strSet=?", (set_name,))
        row = cur.fetchone()
        if row: return row[0]
        
        cur.execute("INSERT INTO sets (strSet, strOverview) VALUES (?, ?)", (set_name, set_overview))
        return cur.lastrowid

    def get_all_paths(self):
        """
        Retrieves all path configurations from the database.
        Returns a dictionary: { normalized_path_str: { 'settings': xml, 'scraper': str, 'content': str, 'noUpdate': bool, 'exclude': bool } }
        """
        paths_map = {}
        if not self.conn:
            return paths_map
            
        try:
            cur = self.cursor()
            # Select necessary columns. Note: columns might vary by Kodi version, but these are standard enough.
            # Use dictionary cursor or access by index
            cur.execute("SELECT strPath, strSettings, strScraper, strContent, noUpdate, exclude FROM path")
            rows = cur.fetchall()
            
            for row in rows:
                p_str = row['strPath']
                # Normalize path ending with slash
                p_str = p_str.replace("\\", "/")
                if not p_str.endswith("/"):
                    p_str += "/"
                    
                paths_map[p_str] = {
                    'settings': row['strSettings'],
                    'scraper': row['strScraper'],
                    'content': row['strContent'],
                    'noUpdate': bool(row['noUpdate']),
                    'exclude': bool(row['exclude'])
                }
        except Exception as e:
            log(f"Error fetching all paths: {e}", xbmc.LOGERROR)
            
        return paths_map

    def add_link(self, table, name, media_id, media_type):
        if not name: return
        cur = self.cursor()
        id_col = table + "_id"
        cur.execute(f"SELECT {id_col} FROM {table} WHERE name=?", (name,))
        row = cur.fetchone()
        if row: item_id = row[0]
        else:
            cur.execute(f"INSERT INTO {table} (name) VALUES (?)", (name,))
            item_id = cur.lastrowid
        
        try: cur.execute(f"INSERT INTO {table}_link ({id_col}, media_id, media_type) VALUES (?, ?, ?)", 
                   (item_id, media_id, media_type))
        except: pass 

    def _handle_movie_version_merge(self, id_movie, id_file, file_path):
        """
        Logic for 'Merge same movie as versions'
        1. Check existing versions of id_movie.
        2. If any version has default/empty name (or 'Default'), update it to its filename.
        3. Determine name for the NEW file (file_path).
        4. Insert new version link.
        """
        try:
            cur = self.cursor()

            # Helper to get decoded buffer filename
            def get_decoded_name(path):
                f_name = os.path.basename(path)
                return urllib.parse.unquote(f_name)
            
            # 1. Get all existing versions for this movie
            # List of {idFile, name, idType}
            # Note: We use idType to know if it is a system default
            # Join with videoversiontype on id (PK of type table) = idType (FK in version table)
            cur.execute("SELECT vv.idFile, vvt.name, vv.idType FROM videoversion vv LEFT JOIN videoversiontype vvt ON vv.idType = vvt.id WHERE vv.idMedia=? AND vv.media_type='movie'", (id_movie,))
            rows = cur.fetchall()
            existing_versions = []
            used_names = set()
            
            for r in rows:
                existing_versions.append({'idFile': r[0], 'name': r[1], 'idType': r[2]})
                if r[1]: used_names.add(r[1])

            # 2. Fix existing versions if they have generic names
            for ver in existing_versions:
                 v_id_file = ver['idFile']
                 v_name = ver['name']
                 v_id_type = ver['idType']
                 
                 # Logic: If name is 'Default' or empty or it is a system type (< 40800), rename it to its filename
                 if not v_name or v_id_type < 40800:
                     cur.execute("SELECT strFilename FROM files WHERE idFile=?", (v_id_file,))
                     f_row = cur.fetchone()
                     if f_row and f_row[0]:
                         v_filename = f_row[0]
                         try: v_decoded_name = urllib.parse.unquote(v_filename)
                         except: v_decoded_name = v_filename

                         if v_decoded_name:
                             new_type_id = self.get_video_version_type_id(v_decoded_name)
                             # Update DB
                             # videoversion PK is idFile!
                             cur.execute("UPDATE videoversion SET idType=? WHERE idFile=? AND idMedia=?", (new_type_id, v_id_file, id_movie))
                             used_names.add(v_decoded_name)
            
            # 3. Add link for NEW file
            # Check if this file is already linked?
            cur.execute("SELECT idType FROM videoversion WHERE idFile=? AND idMedia=? AND media_type='movie'", (id_file, id_movie))
            if cur.fetchone():
                 log(f"Version link already exists for {file_path}", xbmc.LOGINFO)
                 return
            
            # Determine unique name for new file
            new_name = get_decoded_name(file_path)
            base_name = new_name
            counter = 2
            final_name = base_name
            
            # Simple collision resolution
            while final_name in used_names:
                final_name = f"{base_name} (v{counter})"
                counter += 1
            
            log(f"Merging merged Version: {final_name} -> ID {id_movie}", xbmc.LOGINFO)
            type_id = self.get_video_version_type_id(final_name)
            
            # Insert
            # itemType=0 (Version)
            # idType is the FK to videoversiontype
            cur.execute("INSERT INTO videoversion (idFile, idMedia, media_type, itemType, idType) VALUES (?, ?, 'movie', 0, ?)", 
                       (id_file, id_movie, type_id))
            self.conn.commit()

        except Exception as e:
            log(f"Error _handle_movie_version_merge: {e}", xbmc.LOGERROR)

    def get_video_version_type_id(self, version_name):
        if not version_name: 
            return 40400 # Default
        
        try:
            cur = self.cursor()
            # Check if type exists
            cur.execute("SELECT id FROM videoversiontype WHERE name=?", (version_name,))
            row = cur.fetchone()
            if row: return row[0]
            
            # Create new type (owner=2 usually means user created/addon)
            cur.execute("INSERT INTO videoversiontype (name, owner, itemType) VALUES (?, 2, 0)", (version_name,))
            return cur.lastrowid
        except Exception as e:
            # Fallback for older Kodi versions without this table
            log(f"Error get_video_version_type_id: {e}", xbmc.LOGERROR)
            return 40400


    def save_movie(self, id_file, details, file_path="", merge_versions=False):
        if not self.conn: return None
        cur = self.cursor()
        info = details.get('info', {})
        available_art = details.get('available_art', {})
        
        # --- MERGE VERSION LOGIC ---
        if merge_versions:
            tmdb_id = None
            if 'tmdb' in details.get('uniqueids', {}):
                tmdb_id = details['uniqueids']['tmdb']
            elif 'id' in details: # sometimes in root
                tmdb_id = details['id']
            
            if tmdb_id:
                # Check for EXISTING movie with this TMDB ID
                cur.execute("SELECT media_id FROM uniqueid WHERE media_type='movie' AND type='tmdb' AND value=?", (str(tmdb_id),))
                u_row = cur.fetchone()
                if u_row:
                    existing_id_movie = u_row[0]
                    # Ensure it exists in movie table
                    cur.execute("SELECT idMovie FROM movie WHERE idMovie=?", (existing_id_movie,))
                    if cur.fetchone():
                         # Perform merge
                         self._handle_movie_version_merge(existing_id_movie, id_file, file_path)
                         return existing_id_movie
        # ---------------------------

        cur.execute("SELECT idMovie FROM movie WHERE idFile=?", (id_file,))
        row = cur.fetchone()
        
        if row: id_movie = row[0]
        else:
            cur.execute("INSERT INTO movie (idFile) VALUES (?)", (id_file,))
            id_movie = cur.lastrowid
            
            # Kodi 19+ Video Versions (Asset management)
            # VideoAssetType::VERSION = 0
            # VIDEO_VERSION_ID_DEFAULT = 40400
            try:
                cur.execute("INSERT INTO videoversion (idFile, idMedia, media_type, itemType, idType) VALUES (?, ?, ?, ?, ?)",
                            (id_file, id_movie, 'movie', 0, 40400))
            except: 
                # Older Kodi versions might not have this table
                pass

        c00 = info.get('title', '')
        c01 = info.get('plot', '')
        c02 = info.get('plotoutline', '')
        c03 = info.get('tagline', '')
        c06 = self._prepare_string_array(info.get('credits', []))
        
        premiered = info.get('premiered', '')

        # c08: Thumb (XML Collection)
        c08 = self._build_image_xml(available_art, movie_title=c00)
        
        # Fallback: If c08 empty, try info['thumb']
        if not c08 and info.get('thumb'):
             val = info.get('thumb')
             c08 = f'<thumb spoof="" cache="" aspect="poster" preview="">{self._xml_escape(val)}</thumb>'

        c10 = info.get('sorttitle', '')
        c11 = info.get('duration', 0)
        c12 = info.get('mpaa', '')
        c13 = info.get('top250', 0)
        c14 = self._prepare_string_array(info.get('genre', []))
        c15 = self._prepare_string_array(info.get('director', []))
        c16 = info.get('originaltitle', '')
        c18 = self._prepare_string_array(info.get('studio', []))
        c19 = info.get('trailer', '')
        
        # c20: Fanart (XML Collection)
        c20 = self._build_fanart_xml(available_art)
        
        # Fallback: If c20 empty, try info['fanart']
        if not c20 and info.get('fanart'):
             val = info.get('fanart')
             c20 = f'<fanart><thumb colors="" preview="">{self._xml_escape(val)}</thumb></fanart>'

        c21 = self._prepare_string_array(info.get('country', []))
        
        # c22: File Path (Absolute)
        c22 = file_path
        
        c23 = None
        if id_file:
             cur.execute("SELECT idPath FROM files WHERE idFile=?", (id_file,))
             r = cur.fetchone()
             if r: c23 = r[0]

        id_set = None
        if info.get('set'):
             id_set = self.get_or_create_set(info.get('set'), info.get('setoverview', ''))

        # Kodi Source: Check if we missed any columns.
        # c04 is 'votes' string in older versions, but now typically unused or just string representation.
        # c05 is idRating.
        # c09 is idUniqueId.
        # c07 is Year.
        
        sql = """UPDATE movie SET 
            c00=?, c01=?, c02=?, c03=?, c06=?, c08=?, c10=?, c11=?, c12=?, c13=?, c14=?, c15=?, c16=?, c18=?, c19=?, c20=?, c21=?, c22=?, c23=?, premiered=?, idSet=?
            WHERE idMovie=?"""
        
        try:
            cur.execute(sql, (
                c00, c01, c02, c03, c06, c08, c10, c11, c12, c13, c14, c15, c16, c18, c19, c20, c21, c22, c23, premiered, id_set,
                id_movie
            ))
        except Exception as e:
            log(f"DB Error updating movie: {e}", xbmc.LOGERROR)
        
        cur.execute("DELETE FROM genre_link WHERE media_id=? AND media_type='movie'", (id_movie,))
        for g in info.get('genre', []): self.add_link('genre', g, id_movie, 'movie')
            
        cur.execute("DELETE FROM studio_link WHERE media_id=? AND media_type='movie'", (id_movie,))
        for s in info.get('studio', []): self.add_link('studio', s, id_movie, 'movie')
            
        cur.execute("DELETE FROM country_link WHERE media_id=? AND media_type='movie'", (id_movie,))
        for c in info.get('country', []): self.add_link('country', c, id_movie, 'movie')

        cur.execute("DELETE FROM tag_link WHERE media_id=? AND media_type='movie'", (id_movie,))
        for t in info.get('tag', []): self.add_link('tag', t, id_movie, 'movie')
            
        cur.execute("DELETE FROM director_link WHERE media_id=? AND media_type='movie'", (id_movie,))
        for d in info.get('director', []): self._add_person_link(d, 'director', id_movie, 'movie')

        cur.execute("DELETE FROM writer_link WHERE media_id=? AND media_type='movie'", (id_movie,))
        for w in info.get('credits', []): self._add_person_link(w, 'writer', id_movie, 'movie')

        cur.execute("DELETE FROM actor_link WHERE media_id=? AND media_type='movie'", (id_movie,))
        for actor in details.get('cast', []): self._add_actor(actor, id_movie, 'movie')

        cur.execute("DELETE FROM rating WHERE media_id=? AND media_type='movie'", (id_movie,))
        default_rating_id = None
        for r_type, r_val in details.get('ratings', {}).items():
            cur.execute("INSERT INTO rating (media_id, media_type, rating_type, rating, votes) VALUES (?, ?, ?, ?, ?)",
                       (id_movie, 'movie', r_type, r_val.get('rating', 0), r_val.get('votes', 0)))
            rid = cur.lastrowid
            if r_val.get('default', False) or default_rating_id is None:
                default_rating_id = rid
        if default_rating_id:
            cur.execute("UPDATE movie SET c05=? WHERE idMovie=?", (default_rating_id, id_movie))

        cur.execute("DELETE FROM uniqueid WHERE media_id=? AND media_type='movie'", (id_movie,))
        default_unique_id = None
        for u_type, u_val in details.get('uniqueids', {}).items():
             cur.execute("INSERT INTO uniqueid (media_id, media_type, value, type) VALUES (?, ?, ?, ?)",
                        (id_movie, 'movie', u_val, u_type))
             uid = cur.lastrowid
             if u_type == 'tmdb': default_unique_id = uid
        if default_unique_id:
             try: cur.execute("UPDATE movie SET c09=? WHERE idMovie=?", (default_unique_id, id_movie))
             except: pass

        cur.execute("DELETE FROM art WHERE media_id=? AND media_type='movie'", (id_movie,))
        
        for art_type, art_list in details.get('available_art', {}).items():
            if not art_list: continue
            
            # Kodi Logic: Only one active art per type is stored in 'art' table.
            # Usually the first one from the valid list.
            img = art_list[0]
            url = img if isinstance(img, str) else img.get('url', '')
            if not url: continue

            if art_type.startswith('set.') and id_set:
                # Handle Set Art (stored with media_type='set')
                real_type = art_type[4:] # remove 'set.'
                # Ensure we update set art. Delete specific type to avoid duplicates.
                cur.execute("DELETE FROM art WHERE media_id=? AND media_type='set' AND type=?", (id_set, real_type))
                cur.execute("INSERT INTO art (media_id, media_type, type, url) VALUES (?, ?, ?, ?)",
                           (id_set, 'set', real_type, url))
            elif not art_type.startswith('set.'):
                # Handle Movie Art
                cur.execute("INSERT INTO art (media_id, media_type, type, url) VALUES (?, ?, ?, ?)",
                           (id_movie, 'movie', art_type, url))
        
        self.conn.commit()
        return id_movie

    def _xml_escape(self, s):
        if not s: return ""
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    # MySQL TEXT column limit: 65,535 bytes. Limit art entries per aspect to stay within.
    _MAX_ART_PER_ASPECT = 20

    def _build_image_xml(self, available_art, movie_title=""):
        """
        Builds Kodi-compatible XML string for c08 (thumbnails/posters/logos etc).
        Format: <thumb aspect="..." preview="...">url</thumb>
        Exclude 'fanart' (singular) as it belongs in c20.
        """
        if not available_art:
            return ""
            
        xml_parts = []
        aspect_counts = {}
        for aspect, items in available_art.items():
            # 'fanart' key usually mapped to c20. 
            # Note: 'set.fanart' MUST be included in c08 as per user requirement.
            if aspect == 'fanart':
                continue
                
            if not isinstance(items, list):
                items = [items]
            
            aspect_counts[aspect] = len(items)
            for item in items[:self._MAX_ART_PER_ASPECT]:
                url = item.get('url','') if isinstance(item, dict) else item
                preview = item.get('preview','') if isinstance(item, dict) else ''
                
                if url:
                    xml_parts.append(f'<thumb spoof="" cache="" aspect="{aspect}" preview="{self._xml_escape(preview)}">{self._xml_escape(url)}</thumb>')
        
        result = "".join(xml_parts)
        result_bytes = len(result.encode('utf-8'))
        # log(f"[c08] {movie_title} art counts: {aspect_counts}, total_aspects={len(aspect_counts)}, xml_bytes={result_bytes}", xbmc.LOGINFO)
        if result_bytes > 60000:
            log(f"[c08] WARNING: {movie_title} xml_bytes={result_bytes} approaching TEXT limit (65535)!", xbmc.LOGWARNING)
        return result

    def _build_fanart_xml(self, available_art):
        """
        Builds Kodi-compatible XML string for c20 (fanart).
        Format: <fanart><thumb colors="" preview="...">url</thumb>...</fanart>
        """
        fanart_items = available_art.get('fanart', [])
        if not fanart_items:
            return ""
            
        if not isinstance(fanart_items, list):
            fanart_items = [fanart_items]
            
        xml_parts = ["<fanart>"]
        for item in fanart_items:
            url = item.get('url', '') if isinstance(item, dict) else item
            preview = item.get('preview', '') if isinstance(item, dict) else ''
            
            if url:
                xml_parts.append(f'<thumb colors="" preview="{self._xml_escape(preview)}">{self._xml_escape(url)}</thumb>')
        
        xml_parts.append("</fanart>")
        return "".join(xml_parts)

    def _sanitize_utf8mb3(self, text):
        """Remove characters outside BMP (> 3 bytes in UTF-8) for MySQL utf8mb3 columns."""
        if not text or not self.is_mysql:
            return text
        return ''.join(c for c in text if ord(c) <= 0xFFFF)

    def _add_person_link(self, name, role, media_id, media_type):
        if not name: return
        name = self._sanitize_utf8mb3(name)
        cur = self.cursor()
        cur.execute("SELECT actor_id FROM actor WHERE name=?", (name,))
        row = cur.fetchone()
        if row: actor_id = row[0]
        else:
            cur.execute("INSERT INTO actor (name) VALUES (?)", (name,))
            actor_id = cur.lastrowid
        try: cur.execute(f"INSERT INTO {role}_link (actor_id, media_id, media_type) VALUES (?, ?, ?)", 
                   (actor_id, media_id, media_type))
        except: pass

    def _add_actor(self, actor, media_id, media_type):
        name = actor.get('name')
        if not name: return
        name = self._sanitize_utf8mb3(name)
        cur = self.cursor()
        cur.execute("SELECT actor_id FROM actor WHERE name=?", (name,))
        row = cur.fetchone()
        
        thumb = actor.get('thumbnail', '')
        
        if row: actor_id = row[0]
        else:
            cur.execute("INSERT INTO actor (name, art_urls) VALUES (?, ?)", (name, thumb))
            actor_id = cur.lastrowid
            
        # Update Art Table for Actor (One 'thumb' per actor)
        if thumb:
             cur.execute("DELETE FROM art WHERE media_id=? AND media_type='actor' AND type='thumb'", (actor_id,))
             cur.execute("INSERT INTO art (media_id, media_type, type, url) VALUES (?, ?, ?, ?)", 
                        (actor_id, 'actor', 'thumb', thumb))

        try: cur.execute("INSERT INTO actor_link (actor_id, media_id, media_type, role, cast_order) VALUES (?, ?, ?, ?, ?)", 
                   (actor_id, media_id, media_type, actor.get('role', ''), actor.get('order', 0)))
        except: pass

class KodiScraperSimulation:
    def __init__(self):
        # Default Advanced Settings from Kodi source
        # Fixed regex logic: Kodi C++ R-string delimiter "()" confused Python regex. Actual regex has NO outer parens.
        # NOTE: Using re.ASCII to mimic Kodi C++ regex behavior (which doesn't match Unicode in \w by default).
        # This prevents misdetection of Chinese/Unicode strings in brackets like [宾虚_Ben-Hur_1959] as IDs.
        self.video_filename_identifier_regexp = re.compile(r'[\{\[](\w+?)(?:id)?[-=](\w+)[\}\]]', re.IGNORECASE | re.ASCII)
        self.video_clean_datetime_regexp = re.compile(r'(.*[^ _\,\.\(\)\[\]\-])[ _\.\(\)\[\]\-]+(19[0-9][0-9]|20[0-9][0-9])([ _\,\.\(\)\[\]\-]|[^0-9]$)?', re.IGNORECASE)
        
        self.video_clean_string_regexps = [
            re.compile(r'[ _\,\.\(\)\[\]\-](10bit|480p|480i|576p|576i|720p|720i|1080p|1080i|2160p|3d|aac|ac3|aka|atmos|avi|bd5|bdrip|bdremux|bluray|brrip|cam|cd[1-9]|custom|dc|ddp|divx|divx5|dolbydigital|dolbyvision|dsr|dsrip|dts|dts-hdma|dts-hra|dts-x|dv|dvd|dvd5|dvd9|dvdivx|dvdrip|dvdscr|dvdscreener|extended|fragment|fs|h264|h265|hdr|hdr10|hevc|hddvd|hdrip|hdtv|hdtvrip|hrhd|hrhdtv|internal|limited|multisubs|nfofix|ntsc|ogg|ogm|pal|pdtv|proper|r3|r5|read.nfo|remastered|remux|repack|rerip|retail|screener|se|svcd|tc|telecine|telesync|truehd|ts|uhd|unrated|ws|x264|x265|xvid|xvidvd|xxx|web-dl|webrip|www.www|\[.*\])([ _\,\.\(\)\[\]\-]|$)', re.IGNORECASE),
            re.compile(r'(\[.*\])', re.IGNORECASE)
        ]
        
        self.video_extensions = ['.m4v', '.3g2', '.3gp', '.nsv', '.tp', '.ts', '.ty', '.strm', '.pls', '.rm', '.rmvb', '.mpd', '.m3u', '.m3u8', '.ifo', '.mov', '.qt', '.divx', '.xvid', '.bivx', '.vob', '.nrg', '.img', '.iso', '.udf', '.pva', '.wmv', '.asf', '.asx', '.ogm', '.m2v', '.avi', '.bin', '.dat', '.mpg', '.mpeg', '.mp4', '.mkv', '.mk3d', '.avc', '.vp3', '.svq3', '.nuv', '.viv', '.dv', '.fli', '.flv', '.001', '.wpl', '.xspf', '.zip', '.vdr', '.dvr-ms', '.xsp', '.mts', '.m2t', '.m2ts', '.evo', '.ogv', '.sdp', '.avs', '.rec', '.url', '.pxml', '.vc1', '.h264', '.rcv', '.rss', '.mpls', '.mpl', '.webm', '.bdmv', '.bdm', '.wtv', '.trp', '.f4v']
        
        self.image_extensions = ['.png', '.jpg', '.jpeg', '.tbn', '.webp']
        self.art_types = ['poster', 'fanart', 'banner', 'landscape', 'clearlogo', 'clearart', 'discart', 'disc', 'keyart', 'logo', 'thumb']

        # Cache for scraped files (loaded via JSONRPC)
        self.scraped_files = set()
        self.loaded_scraped_status = False
        
        # Whole Path Cache: { normalized_path: { ...attributes... } }
        self.path_cache = {} 
        
        self.pDialog = None
        self.deal_process = 0
        self.stop_scan = False
        try:
            self.MAX_WORKERS = ADDON_SETTINGS.getSettingInt('thread_count')
            if self.MAX_WORKERS < 1: self.MAX_WORKERS = 8
        except:
            self.MAX_WORKERS = 8
        self.executor = None
        
        self.stats_processed = 0
        self.stats_success = 0
        self.stats_failed = 0
        self.running_futures = set()
        self.future_map = {}
        self.failed_items = []
        self.db = None

        

    # --- Helper: Normalize Paths ---
    def normalize_path(self, path):
        # Standardize separators and decoding
        # try:
        #     p = urllib.parse.unquote(path)
        # except:
        #     p = path
        p = path.replace("\\", "/")
        return p.rstrip("/")

    # --- JSONRPC Methods ---
    def execute_jsonrpc(self, method, params=None):
        req = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": 1
        }
        json_str = xbmc.executeJSONRPC(json.dumps(req))
        return json.loads(json_str)

    def load_scraped_files(self):
        """
        Loads all scraped file paths from the 'files' table in the database.
        This includes main movie files AND all versions/assets.
        """
        log("Loading scraped files from DB (direct SQL)...", xbmc.LOGINFO)
        if not self.db or not self.db.conn:
            log("DB not connected, falling back to JSON-RPC...", xbmc.LOGWARNING)
            self._load_scraped_files_jsonrpc()
            return

        try:
            cur = self.db.cursor()
            # Only load files that actually have a movie entry in the movie table.
            # Kodi clears the movie table when library is reset, but leaves files table intact.
            # Using just files table would cause false positives (thinking files are already scraped).
            query = "SELECT p.strPath, f.strFilename FROM files f JOIN path p ON f.idPath = p.idPath JOIN movie m ON m.idFile = f.idFile"
            cur.execute(query)
            rows = cur.fetchall()
            
            count = 0
            for r in rows:
                p_str = r[0]
                f_str = r[1]
                full_path = p_str + f_str
                self.scraped_files.add(self.normalize_path(full_path))
                count += 1
                
            log(f"Loaded {count} files from DB.", xbmc.LOGINFO)
            self.loaded_scraped_status = True
            
        except Exception as e:
            log(f"Error loading files from DB: {e}", xbmc.LOGERROR)
            # Fallback
            self._load_scraped_files_jsonrpc()

    def _load_scraped_files_jsonrpc(self):
        """
        Loads all scraped movie file paths into memory for fast lookup (JSON-RPC Fallback).
        """
        log("Loading scraped movies from library (JSON-RPC)...", xbmc.LOGINFO)
        result = self.execute_jsonrpc("VideoLibrary.GetMovies", {"properties": ["file"]})
        
        if "result" in result and "movies" in result["result"]:
            for movie in result["result"]["movies"]:
                f = movie.get("file", "")
                if f:
                    self.scraped_files.add(self.normalize_path(f))
        
        log(f"Loaded {len(self.scraped_files)} scraped movies via JSON-RPC.", xbmc.LOGINFO)
        self.loaded_scraped_status = True

    def load_path_cache(self):
        """
        Loads all path configs from DB into memory.
        """
        if self.db:
            self.path_cache = self.db.get_all_paths()
            log(f"Loaded {len(self.path_cache)} path configurations.", xbmc.LOGINFO)

    def get_scraper_roots(self):
        """
        Identify paths that are explicitly set to use this scraper.
        Returns: List of path strings.
        """
        roots = []
        target_scraper = "metadata.tmdb.cn.optimization"
        
        for p, data in self.path_cache.items():
            if data['scraper'] == target_scraper and data['content'] == 'movies':
                roots.append(p)
        return roots

    def _get_start_path_and_parents(self, path):
        """
        Generator yielding paths from current up to root.
        """
        curr = self.normalize_path(path)
        if not curr.endswith("/"):
            curr += "/"
            
        # Avoid infinite loops
        max_depth = 50
        count = 0
        
        while count < max_depth:
            yield curr
            
            # Find parent
            # "smb://server/share/folder/" -> "smb://server/share/"
            # "c:/folder/" -> "c:/"
            
            stripped = curr.rstrip("/")
            last_slash = stripped.rfind("/")
            if last_slash <= 0:
                break
                
            parent = stripped[:last_slash] + "/"
            
            # Protocol check (don't go beyond smb://)
            if "://" in curr and not parent.endswith("://") and parent.count("/") < 3:
                 break
                 
            if parent == curr:
                break
            curr = parent
            count += 1

    def resolve_path_attributes(self, dir_path):
        """
        Walks up the directory tree to find effective settings and flags (exclude/noUpdate).
        Returns: (overrides_dict, is_excluded, is_no_update)
        """
        overrides = {}
        is_excluded = False
        is_no_update = False
        
        found_settings = False
        
        # Traverse up
        for p in self._get_start_path_and_parents(dir_path):
            if p in self.path_cache:
                data = self.path_cache[p]
                
                # Flags are inherited "True" if ANY parent has them set? 
                # Kodi logic: VideoInfoScanner::GetPathDetails checks recursive/exclude per path.
                # If a parent path is marked 'exclude', usually the scanner wouldn't have entered it.
                # So yes, if any parent in the traversal is excluded, we are excluded.
                if data['exclude']: is_excluded = True
                if data['noUpdate']: is_no_update = True
                
                # Settings: Found at nearest level? 
                # Usually settings are attached to the Content Root. 
                # We take the FIRST non-empty settings we find walking up.
                if not found_settings and data['settings']:
                    overrides = self._parse_settings_xml(data['settings'])
                    found_settings = True
                    
        return overrides, is_excluded, is_no_update

    def is_video_scraped(self, file_path):
        if not self.loaded_scraped_status:
            self.load_scraped_files()
        
        # Check normalized path
        return self.normalize_path(file_path) in self.scraped_files

    # --- Clean String Logic (Same as before) ---
    def get_filename_identifier(self, filename):
        """
        Parses filename for valid unique IDs (e.g. {tmdb=123})
        Returns (success, type, id, match_string)
        """
        match = self.video_filename_identifier_regexp.search(filename)
        if match:
            # match.group(0) is the full match string e.g. [tmdb=123]
            # match.group(1) is the type e.g. tmdb
            # match.group(2) is the id e.g. 123
            return True, match.group(1).lower(), match.group(2), match.group(0)
        return False, "", "", ""

    def clean_string(self, filename):
        """
        Simulates CUtil::CleanString
        """
        str_title_and_year = filename
        str_year = ""
        
        if str_title_and_year == "..":
            return "", "", ""

        # 1. Check for identifier
        has_id, id_type, id_val, id_match = self.get_filename_identifier(str_title_and_year)
        if has_id:
            str_title_and_year = str_title_and_year.replace(id_match, "")

        # 2. Check for year (and extract title part)
        # C++: if (reYear.RegFind(strTitleAndYear.c_str()) >= 0)
        # Matches: 1 -> Title, 2 -> Year
        year_match = self.video_clean_datetime_regexp.search(str_title_and_year)
        if year_match:
            str_title_and_year = year_match.group(1)
            str_year = year_match.group(2)

        # 3. Remove extension
        # If year was found, str_title_and_year is just the title part (captured before year).
        # If year NOT found, we might still have the extension.
        if not year_match:
             # Python splitext handles removing extension
             root, ext_found = os.path.splitext(str_title_and_year)
             if ext_found: 
                 str_title_and_year = root

        # 4. Apply CleanString regexps (clutter removal)
        for regex in self.video_clean_string_regexps:
            match = regex.search(str_title_and_year)
            if match:
                # RegFind returns start index. We resize string to that index.
                # In Python, we slice.
                str_title_and_year = str_title_and_year[:match.start()]
        
        # 5. Clean Chars
        # Replace _ with space.
        # If no spaces, replace . with space (skip initial dots)
        cleaned = list(str_title_and_year)
        initial_dots = True
        already_contains_space = ' ' in str_title_and_year
        
        for i, c in enumerate(cleaned):
            if c != '.':
                initial_dots = False
            
            if c == '_':
                cleaned[i] = ' '
            elif (not already_contains_space) and (not initial_dots) and (c == '.'):
                cleaned[i] = ' '
        
        str_title_and_year = "".join(cleaned).strip()
        str_title = str_title_and_year
        
        return str_title, str_year, (id_type, id_val) if has_id else None

    def get_latest_db_path(self):
        """
        Finds the latest or active MyVideos database in Kodi userdata.
        """
        db_dir = translatePath("special://database")
        try:
            files = xbmcvfs.listdir(db_dir)[1] # returns (dirs, files)
        except:
            return None
        
        # Filter MyVideos*.db
        video_dbs = [f for f in files if f.startswith("MyVideos") and f.endswith(".db")]
        
        if not video_dbs:
            return None
            
        # Parse version numbers and find max
        def get_version(name):
            try:
                match = re.search(r'MyVideos(\d+)\.db', name)
                return int(match.group(1)) if match else 0
            except:
                return 0
                
        latest_db = max(video_dbs, key=get_version)
        return os.path.join(db_dir, latest_db)

    def _parse_mysql_from_xml(self, xml_path):
        """Parse MySQL videodatabase config from a single advancedsettings.xml file. Returns dict or None."""
        try:
            if not xbmcvfs.exists(xml_path):
                return None
            tree = ET.parse(xml_path)
            root = tree.getroot()
            vdb = root.find("videodatabase")
            if vdb is None:
                return None
            db_type = vdb.findtext("type", "").strip().lower()
            if db_type != "mysql":
                return None

            host = vdb.findtext("host", "").strip()
            port = vdb.findtext("port", "3306").strip()
            user = vdb.findtext("user", "").strip()
            password = vdb.findtext("pass", "").strip()
            db_name = vdb.findtext("name", "").strip()

            if not host or not user:
                log(f"MySQL config incomplete in {xml_path}", xbmc.LOGWARNING)
                return None

            return {
                'host': host,
                'port': int(port),
                'user': user,
                'password': password,
                'database': db_name
            }
        except Exception as e:
            log(f"Error parsing {xml_path} for MySQL: {e}", xbmc.LOGWARNING)
            return None

    def get_mysql_config(self):
        """
        Parses advancedsettings.xml to check if MySQL is configured for videodatabase.
        Returns dict {host, port, user, password, database} or None.
        Checks special://userdata/ first, then special://profile/ can override (consistent with Kodi).
        """
        result = None
        for special_path in ("special://userdata/advancedsettings.xml",
                             "special://profile/advancedsettings.xml"):
            xml_path = translatePath(special_path)
            cfg = self._parse_mysql_from_xml(xml_path)
            if cfg:
                log(f"MySQL config found in {special_path}", xbmc.LOGINFO)
                result = cfg

        if result and not result['database']:
            db_dir = translatePath("special://database")
            try:
                files = xbmcvfs.listdir(db_dir)[1]
                video_dbs = [f for f in files if f.startswith("MyVideos") and f.endswith(".db")]
                if video_dbs:
                    def get_ver(n):
                        m = re.search(r'MyVideos(\d+)\.db', n)
                        return int(m.group(1)) if m else 0
                    version = max(get_ver(f) for f in video_dbs)
                    result['database'] = f"MyVideos{version}"
                else:
                    result['database'] = "MyVideos131"
            except:
                result['database'] = "MyVideos131"

        if result:
            log(f"MySQL config detected: {result['host']}:{result['port']}/{result['database']}", xbmc.LOGINFO)
        return result

    def scan_local_art(self, file_path, details, video_files_in_dir=1, files_map=None):
        """
        Scans for local artwork and injects it into details['available_art']
        """
        if not details: return
        
        dir_path = os.path.dirname(file_path)
        file_name = os.path.basename(file_path)
        file_base, _ = os.path.splitext(file_name)
        
        if files_map is None:
            try:
                _, files = xbmcvfs.listdir(dir_path)
                # Make set for O(1) lookups, case insensitive-ish
                files_map = {f.lower(): f for f in files}
            except:
                return

        available_art = details.setdefault('available_art', {})
        
        # Helper: add art if exists
        def add_art(art_type, art_filename_lower):
            if art_filename_lower in files_map:
                real_name = files_map[art_filename_lower]
                full_art_path = os.path.join(dir_path, real_name)
                art_entry = {'url': full_art_path, 'preview': full_art_path}
                
                if art_type not in available_art:
                    available_art[art_type] = []
                
                exists = False
                for existing in available_art[art_type]:
                     if isinstance(existing, dict) and existing.get('url') == full_art_path: exists = True
                     elif existing == full_art_path: exists = True
                
                if not exists:
                    # Insert at beginning
                    available_art[art_type].insert(0, art_entry)

        # 1. Check <movie>-<arttype>.ext
        for art_type in self.art_types:
            for ext in self.image_extensions:
                # e.g. avatar-poster.jpg
                candidate = f"{file_base}-{art_type}{ext}".lower()
                add_art(art_type, candidate)
                
                # 2. Check <arttype>.ext (e.g. poster.jpg)
                # Only if this is the only video in dir, OR specific setting (which we simplify to "Single Video Mode")
                # This matches Kodi's behavior for avoiding ambiguous matches in flat folders.
                if video_files_in_dir == 1:
                    candidate_generic = f"{art_type}{ext}".lower()
                    add_art(art_type, candidate_generic)
                
                # 3. Check <movie>.<ext> (thumb)
                if art_type == 'thumb':
                    candidate_thumb = f"{file_base}{ext}".lower() # avatar.jpg
                    add_art('thumb', candidate_thumb)

    def get_movie_by_tmdb_id(self, tmdb_id):
        if not tmdb_id: return None
        try:
            cur = self.conn.cursor()
            # uniqueid table: media_id, media_type, value, type
            cur.execute("SELECT media_id FROM uniqueid WHERE media_type='movie' AND type='tmdb' AND value=?", (str(tmdb_id),))
            row = cur.fetchone()
            if row: return row[0]
        except Exception as e:
            log(f"DB Error get_movie_by_tmdb_id: {e}", xbmc.LOGERROR)
        return None

    def get_video_version_type_id(self, version_name):
        if not version_name: 
            return 40400 # Default
        
        try:
            cur = self.conn.cursor()
            # Check if type exists
            cur.execute("SELECT idType FROM videoversiontype WHERE name=?", (version_name,))
            row = cur.fetchone()
            if row: return row[0]
            
            # Create new type (owner=2 usually means user created/addon)
            cur.execute("INSERT INTO videoversiontype (name, owner) VALUES (?, 2)", (version_name,))
            return cur.lastrowid
        except Exception as e:
            # Fallback for older Kodi versions without this table
            return 40400

    def get_existing_versions(self, id_movie):
        """Returns list of (idFile, versionName) for a movie"""
        versions = []
        try:
            cur = self.conn.cursor()
            sql = """
                SELECT vv.idFile, vvt.name 
                FROM videoversion vv 
                LEFT JOIN videoversiontype vvt ON vv.idType = vvt.idType 
                WHERE vv.idMedia=? AND vv.media_type='movie'
            """
            cur.execute(sql, (id_movie,))
            input_rows = cur.fetchall()
            for r in input_rows:
                versions.append({'idFile': r[0], 'name': r[1]})
        except:
             pass
        return versions

    def update_video_version_type(self, id_file, id_movie, version_name):
        """Updates or Inserts video version link"""
        try:
            type_id = self.get_video_version_type_id(version_name)
            cur = self.conn.cursor()
            
            # Check if link exists
            cur.execute("SELECT idVersion FROM videoversion WHERE idFile=? AND idMedia=? AND media_type='movie'", (id_file, id_movie))
            row = cur.fetchone()
            
            if row:
                cur.execute("UPDATE videoversion SET idType=? WHERE idVersion=?", (type_id, row[0]))
            else:
                 cur.execute("INSERT INTO videoversion (idFile, idMedia, media_type, itemType, idType) VALUES (?, ?, ?, ?, ?)",
                            (id_file, id_movie, 'movie', 0, type_id))
        except Exception as e:
            log(f"Error update_video_version_type: {e}", xbmc.LOGERROR)

    def _get_filename_from_idfile(self, id_file):
        try:
            cur = self.conn.cursor()
            cur.execute("SELECT strFilename FROM files WHERE idFile=?", (id_file,))
            row = cur.fetchone()
            if row: return row[0]
        except: pass
        return ""

    def _parse_xml_nfo(self, nfo_content):
        """
        Parses XML content into details dict.
        """
        try:
            # Clean possible BOM or garbage
            content = nfo_content.strip()
            root = ET.fromstring(content)
            if root.tag not in ['movie', 'video']:
                return None
        except:
            return None
            
        details = {'info': {}, 'cast': [], 'available_art': {}, 'uniqueids': {}, 'ratings': {}}
        info = details['info']
        
        def txt(elem): return elem.text if elem is not None and elem.text else ""
        
        info['title'] = txt(root.find('title'))
        info['originaltitle'] = txt(root.find('originaltitle'))
        info['sorttitle'] = txt(root.find('sorttitle'))
        info['plot'] = txt(root.find('plot'))
        info['outline'] = txt(root.find('outline'))
        info['tagline'] = txt(root.find('tagline'))
        info['year'] = txt(root.find('year'))
        info['premiered'] = txt(root.find('premiered')) or txt(root.find('releasedate'))
        info['duration'] = txt(root.find('runtime'))
        info['mpaa'] = txt(root.find('mpaa'))
        info['trailer'] = txt(root.find('trailer'))
        
        # Lists
        info['genre'] = [g.text for g in root.findall('genre') if g.text]
        info['country'] = [c.text for c in root.findall('country') if c.text]
        info['studio'] = [s.text for s in root.findall('studio') if s.text]
        info['tag'] = [t.text for t in root.findall('tag') if t.text]
        info['credits'] = [c.text for c in root.findall('credits') if c.text] # writers
        info['director'] = [d.text for d in root.findall('director') if d.text]
        
        # Set
        set_node = root.find('set')
        if set_node is not None:
            if set_node.find('name') is not None:
                info['set'] = txt(set_node.find('name'))
                info['setoverview'] = txt(set_node.find('overview'))
            else:
                 info['set'] = set_node.text
        
        # Cast
        for actor in root.findall('actor'):
            name = txt(actor.find('name'))
            if name:
                details['cast'].append({
                    'name': name,
                    'role': txt(actor.find('role')),
                    'thumbnail': txt(actor.find('thumb')),
                    'order': txt(actor.find('order')) or 0
                })
                
        # UniqueIDs
        default_id = txt(root.find('id'))
        if default_id:
            if default_id.startswith('tt'): details['uniqueids']['imdb'] = default_id
            else: details['uniqueids']['default'] = default_id
            
        for uid in root.findall('uniqueid'):
            val = uid.text
            t = uid.get('type', 'unknown')
            if val:
                details['uniqueids'][t] = val
        
        # Ratings
        for r in root.findall('rating'):
            name = r.get('name', 'default')
            val = txt(r.find('value'))
            votes = txt(r.find('votes'))
            try:
                details['ratings'][name] = {'rating': float(val), 'votes': int(votes) if votes else 0, 'default': r.get('default') == 'true'}
            except: pass
            
        # Thumb/Fanart from NFO (URL)
        thumbs = [t.text for t in root.findall('thumb') if t.text]
        if thumbs: details['available_art']['poster'] = [{'url': t} for t in thumbs]
        
        fanarts = root.findall('fanart')
        for f in fanarts:
            ft = [t.text for t in f.findall('thumb') if t.text]
            if ft:
                if 'fanart' not in details['available_art']: details['available_art']['fanart'] = []
                details['available_art']['fanart'].extend([{'url': t} for t in ft])
        
        return details

    def scan_local_nfo(self, file_path, video_files_in_dir=1, files_map=None):
        """
        Scans for NFO file. 
        """
        dir_path = os.path.dirname(file_path)
        file_name = os.path.basename(file_path)
        base_name, _ = os.path.splitext(file_name)

        if files_map is None:
            try:
                _, files = xbmcvfs.listdir(dir_path)
                files_map = {f.lower(): f for f in files}
            except:
                return None, None
        
        candidates = [f"{base_name}.nfo".lower()]
        
        # Only check movie.nfo if single video in dir (matches Kodi ambiguity logic)
        if video_files_in_dir == 1:
            candidates.append("movie.nfo")
        
        found_nfo = None
        for c in candidates:
            if c in files_map:
                found_nfo = os.path.join(dir_path, files_map[c])
                break
        
        if not found_nfo:
            return None, None
            
        f = xbmcvfs.File(found_nfo)
        content = f.read()
        f.close()
        
        details = self._parse_xml_nfo(content)
        if details and details['info'].get('title'):
            return details, None
            
        ids = {}
        imdb_match = re.search(r'(tt\d{7,})', content)
        if imdb_match: ids['imdb'] = imdb_match.group(1)
        tmdb_url_match = re.search(r'themoviedb.org/movie/(\d+)', content)
        if tmdb_url_match: ids['tmdb'] = tmdb_url_match.group(1)
        
        if ids:
            return None, ids
            
        return None, None

    def deepseek_pre_clean_name(self, raw_name):
        # Pre-clean filename for DeepSeek: Remove known Kodi ID patterns
        # Matches variations: [tmdb=123], [tmdbid=123], [tmdb-id=123], [tmdb_id=123] etc.
        ds_filename = re.sub(r'[\{\[](?:tmdb|imdb)(?:[._-]?id)?\s*[-=]\s*(\w+)[\}\]]', ' ', raw_name, flags=re.IGNORECASE | re.ASCII)
        ds_filename = re.sub(r'\s+', ' ', ds_filename).strip()
        return ds_filename

    def extract_info_via_deepseek(self, raw_name, deepseek_extractor):
        if not deepseek_extractor:
            return None, None, None

        title = None
        year = None
        english_title = None

        try:
            ds_filename = self.deepseek_pre_clean_name(raw_name)

            # Use cleaned filename for context
            ds_data = deepseek_extractor.extract(ds_filename)
            if ds_data:
                # Support multiple key variations for robustness
                t_cn = ds_data.get('cn') or ds_data.get('chinese') or ds_data.get('zh')
                if t_cn == "中文名": t_cn = None # Handle case where model fails and returns prompt keys
                
                t_en = ds_data.get('en') or ds_data.get('english') or ds_data.get('englist')
                if t_en == "英文名": t_en = None # Handle case where model fails and returns prompt keys
                
                # Replace symbols with spaces in titles
                if t_cn: t_cn = re.sub(r'[._\-\[\]]', ' ', t_cn).strip()
                if t_cn: t_cn = re.sub(r'\s+', ' ', t_cn).strip()
                if t_en: t_en = re.sub(r'[._\-\[\]]', ' ', t_en).strip()
                if t_en: t_en = re.sub(r'\s+', ' ', t_en).strip()
                
                y_ds = ds_data.get('year') or ds_data.get('yr')
                if y_ds == "年份": y_ds = None # Handle case where model fails and returns prompt keys
                
                new_title = t_cn if t_cn else t_en
                if new_title:
                    title = new_title
                    english_title = t_en
                if y_ds:
                    try:
                        int(y_ds)
                    except Exception as e:
                        year = None
                log(f"DeepSeek Extracted: '{raw_name}' -> zh: '{title}', year: '{year}', en: '{english_title}' ", xbmc.LOGINFO)
        except Exception as e:
            log(f"DeepSeek Process Error: {e}", xbmc.LOGERROR)
            
        return title, year, english_title

    def process_file(self, file_path, settings, video_files_in_dir=1, deepseek_extractor=None, files_map=None):
        search_history = []
        try:
            if files_map is None:
                files_map = {}
                try:
                    dir_path = os.path.dirname(file_path)
                    _, files = xbmcvfs.listdir(dir_path)
                    files_map = {f.lower(): f for f in files}
                except: pass

            normalized_path = file_path.replace("\\", "/")
            raw_name = urllib.parse.unquote(normalized_path.split("/")[-1])
            title, year, unique_id = self.clean_string(raw_name)
            if not year:
                year = None
            
            if unique_id:
                id_type, id_val = unique_id
                search_history.append(f"唯一ID: {id_type}={id_val}")
            
            log(f"Processing: {file_path} | Title: {title} | Year: {year} | ID: {unique_id}", xbmc.LOGINFO)
            runner = ScraperRunner(settings)
            details = None
            ds_title, ds_year, ds_english = None, None, None
            english_title_from_deepseek = None # For compatibility in failure logging

            ignore_local = settings.getSettingBool('ignore_local_nfo_art')

            # 1. NFO Check
            if not ignore_local:
                nfo_details, nfo_ids = self.scan_local_nfo(file_path, video_files_in_dir, files_map)
                if nfo_details:
                    log(f"Found Full NFO for {file_path}", xbmc.LOGINFO)
                    details = nfo_details
                elif nfo_ids:
                    log(f"Found NFO IDs: {nfo_ids}", xbmc.LOGINFO)
                    search_history.append(f"NFO IDs: {nfo_ids}")
                    try:
                        details = runner.get_details(nfo_ids)
                    except Exception as e:
                        log(f"GetDetails(NFO) Error: {e}", xbmc.LOGERROR)

            # unique_id = None
            # 2. Filename ID
            if not details and unique_id:
                id_type, id_val = unique_id
                log(f"ID found in filename: {id_type}={id_val}. Attempting direct details lookup.", xbmc.LOGINFO)
                try:
                    details = runner.get_details({id_type: id_val})
                except Exception as e:
                    log(f"GetDetails(Direct) Error: {e}", xbmc.LOGERROR)

            # 3. Search
            if not details:
                try:
                    results = []
                    ds_title, ds_year, ds_english = None, None, None
                    
                    only_on_failure = settings.getSettingBool('deepseek_only_on_failure')

                    # 3.1 Direct Search (Traditional)
                    if not deepseek_extractor or only_on_failure:
                        # If deepseek is off, OR it's enabled but we only use it on failure
                        search_history.append(f"搜索(传统): {title} ({year})")
                        results = runner.search(title, year)
                        
                        if results:
                            # Traditional search success
                            match = results[0]
                            log(f"Match found (Traditional): {match.get('title')} (ID: {match.get('id')})", xbmc.LOGINFO)
                            unique_ids = {'tmdb': str(match.get('id'))}
                            details = runner.get_details(unique_ids)

                    # 3.2 DeepSeek Search (If needed)
                    # Condition: DeepSeek is enabled AND (it's not 'only_on_failure' OR previous search failed)
                    should_use_deepseek = deepseek_extractor and (not details)
                    
                    if should_use_deepseek:
                        if only_on_failure:
                            log("Traditional search failed. Trying DeepSeek...", xbmc.LOGINFO)
                            
                        ds_title, ds_year, ds_english = self.extract_info_via_deepseek(raw_name, deepseek_extractor)
                        
                        # Use DeepSeek info if available
                        search_title = ds_title
                        search_year = ds_year
                        if ds_title:
                            search_history.append(f"搜索(DeepSeek): {search_title} ({search_year})")
                            results = runner.search(search_title, search_year)
                        
                        # Fallback to English title if primary search failed
                        if not results and ds_english and ds_english != search_title:
                            log(f"No results for DeepSeek Chinese title. Trying DeepSeek English title: {ds_english}", xbmc.LOGINFO)
                            search_history.append(f"搜索(DeepSeek英文): {ds_english} ({search_year})")
                            results = runner.search(ds_english, search_year)
                            
                        if results:
                            match = results[0]
                            log(f"Match found (DeepSeek): {match.get('title')} (ID: {match.get('id')})", xbmc.LOGINFO)
                            unique_ids = {'tmdb': str(match.get('id'))}
                            details = runner.get_details(unique_ids)
                        else:
                            log(f"No results found via DeepSeek for {search_title}", xbmc.LOGWARNING)

                except Exception as e:
                    log(f"Search Error: {e}", xbmc.LOGERROR)

            if not details or "error" in details:
                # log(f"Failed to get details for {title} {year} {unique_id} {english_title_from_deepseek} {file_path}", xbmc.LOGERROR)
                return {'is_failed': True, 'history': search_history}
            
            # 4. Local Artwork Overlay
            if not ignore_local:
                self.scan_local_art(file_path, details, video_files_in_dir, files_map)

            return details
        except Exception:
            log(f"Fatal Error in process_file for {file_path}: {traceback.format_exc()}", xbmc.LOGERROR)
            return {'is_failed': True, 'history': search_history}

    def check_should_stop(self):
        if self.stop_scan: return True
        if self.pDialog and self.pDialog.iscanceled():
            self.stop_scan = True
            return True
        return False


    def handle_finished_futures(self, done_futures):
        for f in done_futures:
            if f not in self.future_map: continue
                
            f_path, _, weight, merge_vers = self.future_map.pop(f)
            self.running_futures.remove(f)
            
            # process_file returns 'details' or {'is_failed': True, 'history': []} or None
            details = None
            try: details = f.result()
            except: details = None
            
            self.stats_processed += 1
            if weight:
                self.deal_process += weight
            scraped_title = None
            
            # Check for failure marker
            is_failed = False
            failure_history = []
            if details and isinstance(details, dict) and details.get('is_failed'):
                is_failed = True
                failure_history = details.get('history', [])
                details = None # Clear details to trigger failure block below

            if details and not is_failed:
                self.stats_success += 1
                if self.db:
                    try:
                        # Ensure thread safety for DB writes (Main Thread)
                        f_dir = os.path.dirname(f_path)
                        id_path = self.db.get_or_create_path(f_dir)
                        id_file = self.db.get_or_create_file(f_path, id_path)
                        self.db.save_movie(id_file, details, f_path, merge_versions=merge_vers)
                        info_obj = details.get('info', {})
                        year = info_obj.get('year', '')
                        if not year and info_obj.get('premiered'):
                            try: year = str(info_obj.get('premiered'))[:4]
                            except: pass
                        scraped_title = f"{info_obj.get('title', 'Unknown')}({year})"
                        log(f"Saved to DB: {scraped_title}", xbmc.LOGINFO)
                    except Exception as e:
                        log(f"DB Save Error for {f_path}: {e}", xbmc.LOGERROR)
            else:
                self.stats_failed += 1
                log(f"Task Failed or Returned None for {f_path}", xbmc.LOGWARNING)
                # Store object with failure info
                self.failed_items.append({
                    'path': f_path,
                    'history': failure_history
                })
            f_dir = urllib.parse.unquote(os.path.dirname(f_path))
            f_name = urllib.parse.unquote(os.path.basename(f_path).split(".")[0])
            message = f"目录: {f_dir}\n {f_name}-> {scraped_title}\n 总计(成功: {self.stats_success}, 失败: {self.stats_failed})"
            if self.pDialog:
                self.pDialog.update(int(self.deal_process*100), message)

    def scan_path(self, path, path_total_process, deepseek_extractor=None):
        """
        Scans a path using xbmcvfs (supports dav://, smb://, etc.)
        """
        if self.check_should_stop(): return
        # Ensure trailing slash
        if not path.endswith("/") and not path.endswith("\\"):
            path += "/"
            
        if self.pDialog:
            display_name = urllib.parse.unquote(os.path.basename(path.rstrip("/\\")))
            message = f"扫描目录: {display_name}\n总计(成功: {self.stats_success}, 失败: {self.stats_failed})"
            self.pDialog.update(int(self.deal_process * 100), message)
                
        # 1. Resolve effective scraper settings/flags for this directory ONCE
        overrides, is_excluded, is_no_update = self.resolve_path_attributes(path)
        settings = SettingsProxy(ADDON_SETTINGS, overrides)
        
        if is_excluded:
            log(f"SKIPPING Directory {path}: Path Excluded", xbmc.LOGINFO)
            return
        if is_no_update:
            log(f"SKIPPING Directory {path}: Path noUpdate", xbmc.LOGINFO)
            return

        # Check if this path has been explicitly set to a different content type or scraper
        # e.g. subdirectory changed to "tvshows" with a TV show scraper
        normalized_check = path.replace("\\", "/")
        if not normalized_check.endswith("/"):
            normalized_check += "/"
        if normalized_check in self.path_cache:
            path_data = self.path_cache[normalized_check]
            p_content = path_data.get('content', '')
            p_scraper = path_data.get('scraper', '')
            # If this path has explicit content set to something other than 'movies', skip it
            if p_content and p_content != 'movies':
                log(f"SKIPPING Directory {path}: Content type is '{p_content}', not 'movies'", xbmc.LOGINFO)
                self.deal_process += path_total_process
                return
            # If this path has an explicit scraper that is not ours, skip it
            if p_scraper and p_scraper != 'metadata.tmdb.cn.optimization':
                log(f"SKIPPING Directory {path}: Scraper is '{p_scraper}', not ours", xbmc.LOGINFO)
                self.deal_process += path_total_process
                return

        try:
            dirs, files = xbmcvfs.listdir(path)
            files_map = {f.lower(): f for f in files}
        except Exception:
            log(f"Error listing dir: {path}", xbmc.LOGERROR)
            return

        # NEW: Check merge setting
        merge_vers = settings.getSettingBool('merge_same_movie_version')
            
        # Kodi Logic: Check for .nomedia file
        # If present, recursively skip this folder and all subfolders (Kodi behavior)
        if ".nomedia" in files:
            log(f"SKIPPING Directory {path}: .nomedia found", xbmc.LOGINFO)
            self.deal_process += path_total_process
            return

        # New Logic: Skip BDMV folder if setting enabled
        if settings.getSettingBool('skip_bdmv_folder'):
            # Case-insensitive check for BDMV folder
            if any(d.upper() == 'BDMV' for d in dirs):
                log(f"SKIPPING Directory {path}: BDMV folder found", xbmc.LOGINFO)
                self.deal_process += path_total_process
                return

        l = len(files) + len(dirs)
        if l == 0:
            self.deal_process += path_total_process
            return
        item_weight_process = (path_total_process / l) if l > 0 else 0
        
        # Count actual video files for ambiguity checks
        video_files_in_dir = 0
        for f in files:
            _, ext = os.path.splitext(f)
            if ext.lower() in self.video_extensions:
                video_files_in_dir += 1
        
        # Process Files using the directory's runner settings
        for file in files:
            if self.check_should_stop(): break

            _, ext = os.path.splitext(file)
            if ext.lower() in self.video_extensions:
                full_path = path + file
                
                # Check scraped
                if self.is_video_scraped(full_path):
                    log(f"Already scraped, skipping: {full_path}", xbmc.LOGINFO)
                    self.deal_process += item_weight_process
                    if self.pDialog:
                        self.pDialog.update(int(self.deal_process * 100))
                    continue

                # Check for cancellation or pool fullness
                while len(self.running_futures) >= self.MAX_WORKERS:
                    if self.check_should_stop(): break
                    
                    # BLOCKING WAIT: Wait for at least one future to complete
                    done, _ = wait(self.running_futures, return_when=FIRST_COMPLETED)
                    self.handle_finished_futures(done)

                if self.check_should_stop(): break

                # Submit new task
                future = self.executor.submit(self.process_file, full_path, settings, video_files_in_dir, deepseek_extractor=deepseek_extractor, files_map=files_map)
                self.running_futures.add(future)
                self.future_map[future] = (full_path, settings, item_weight_process, merge_vers)
        
        # Process Directories
        for d in dirs:
            self.scan_path(path + d + "/", item_weight_process, deepseek_extractor)



    def trigger_library_refresh(self):
        """
        Trigger a library refresh using the method observed in plugin.video.emby.vfs.
        This forces Kodi to re-read the database and update widgets/views.
        """
        log("Triggering Library Refresh...", xbmc.LOGINFO)
        
        # 1. VideoLibrary.Scan with a dummy directory.
        # This helps update global states/widgets as seen in Emby Next Gen plugin
        json_cmd = json.dumps({
            "jsonrpc": "2.0",
            "method": "VideoLibrary.Scan",
            "params": {
                "showdialogs": False,
                "directory": "TMDB_CN_REFRESH_TRIGGER"
            },
            "id": 1
        })
        try:
            xbmc.executeJSONRPC(json_cmd)
        except:
            pass
        
        # 2. Container.Refresh
        # This updates the currently visible list/view
        xbmc.executebuiltin("Container.Refresh")

    def _parse_settings_xml(self, xml_str):
        overrides = {}
        if not xml_str:
            return overrides
        try:
            # Wrap in root if multiple settings (though snippet shows root is <settings>)
            # XML provided: <settings version="2"><setting ...>...</settings>
            if not xml_str.strip().startswith("<"):
                return overrides
                
            root = ET.fromstring(xml_str)
            if root.tag == 'settings':
                for setting in root.findall('setting'):
                    key = setting.get('id')
                    val = setting.text
                    if key:
                        overrides[key] = val if val is not None else ""
        except Exception as e:
            log(f"Error parsing path settings XML: {e}", xbmc.LOGWARNING)
        return overrides

    def _apply_dns_settings(self, settings):
        if not dns_override:
            return

        def get_domain(url):
            if '://' in url:
                return urllib.parse.urlparse(url).netloc
            return url.split('/')[0]

        # Use defaults same as scraper settings.xml defaults
        tmdb_domain = settings.getSetting('tmdb_api_base_url') or 'api.tmdb.org'
        fanart_domain = settings.getSetting('fanart_base_url') or 'webservice.fanart.tv'
        trakt_domain = settings.getSetting('trakt_base_url') or 'trakt.tv'
        imdb_domain = settings.getSetting('imdb_base_url') or 'www.imdb.com'
        
        target_map = {
            'dns_tmdb_api': get_domain(tmdb_domain),
            'dns_fanart_tv': get_domain(fanart_domain),
            'dns_imdb_www': get_domain(imdb_domain),
            'dns_trakt_tv': get_domain(trakt_domain)
        }
        
        custom_ips = {}
        for key, domain in target_map.items():
            ip = settings.getSetting(key).strip()
            # Pass empty string to clear previous overrides (if any)
            custom_ips[domain] = ip
        
        dns_override.set_custom_hosts(custom_ips)

    def scan_and_process(self):
        """
        Main entry point.
        """
        log("Starting Scan...", xbmc.LOGINFO)
        icon_path = ADDON_SETTINGS.getAddonInfo('icon')
        
        try:
            req = {"jsonrpc": "2.0", "method": "Settings.GetSettingValue", "params": {"setting": "myvideos.extractflags"}, "id": 1}
            res = json.loads(xbmc.executeJSONRPC(json.dumps(req)))
            if res.get("result", {}).get("value") is True:
                extend_cfg_path = xbmcvfs.translatePath("special://profile/addon_data/metadata.tmdb.cn.optimization/settings_extend.json")
                dont_ask = False
                if os.path.exists(extend_cfg_path):
                    try:
                        with open(extend_cfg_path, "r", encoding="utf-8") as f:
                            cfg = json.loads(f.read())
                            dont_ask = cfg.get("dont_ask_extractflags", False)
                    except:
                        pass
                
                if not dont_ask:
                    ret = xbmcgui.Dialog().yesnocustom(
                        "建议关闭设置项",
                        "检测到【设置-媒体-视频-从文件中提取视频信息】处于[COLOR yellow]开启[/COLOR]状态。\n此设置在远端网盘或者高延迟环境下，会导致打开视频媒体库列表时严重卡顿。\n\n是否立即为您关闭该设置？\n",
                        customlabel="不再提示",
                        nolabel="否(不关闭)",
                        yeslabel="是(立即关闭)"
                    )
                    if ret == 1:
                        set_req = {"jsonrpc": "2.0", "method": "Settings.SetSettingValue", "params": {"setting": "myvideos.extractflags", "value": False}, "id": 2}
                        xbmc.executeJSONRPC(json.dumps(set_req))
                        log("Disabled myvideos.extractflags by user prompt.", xbmc.LOGINFO)
                    elif ret == 2:
                        try:
                            cfg = {}
                            if os.path.exists(extend_cfg_path):
                                with open(extend_cfg_path, "r", encoding="utf-8") as f:
                                    try: cfg = json.loads(f.read())
                                    except: pass
                            if "ignored_tips" not in cfg or not isinstance(cfg["ignored_tips"], dict):
                                cfg["ignored_tips"] = {}
                            cfg["ignored_tips"]["myvideos.extractflags"] = True
                            os.makedirs(os.path.dirname(extend_cfg_path), exist_ok=True)
                            with open(extend_cfg_path, "w", encoding="utf-8") as f:
                                f.write(json.dumps(cfg, indent=4))
                            log("User selected ignored_tips myvideos.extractflags, saved.", xbmc.LOGINFO)
                        except Exception as ex:
                            log(f"Write settings_extend.json Error: {ex}", xbmc.LOGWARNING)
        except Exception as e:
            log(f"Check extractflags Error: {e}", xbmc.LOGWARNING)

        self.pDialog = xbmcgui.DialogProgress()
        heading = f"TMDB CN Optimization - {self.MAX_WORKERS}线程扫描中..."
        self.pDialog.create(heading, "初始化中...")
        
        try:
            # Initialize Thread Pool
            self.executor = ThreadPoolExecutor(max_workers=self.MAX_WORKERS)
            log(f"Initialized ThreadPoolExecutor with {self.MAX_WORKERS} workers.", xbmc.LOGINFO)

            # Initialize DB - try MySQL first, fall back to SQLite
            mysql_config = self.get_mysql_config()
            if mysql_config:
                try:
                    self.db = KodiDatabase(mysql_config=mysql_config)
                    self.db.connect()
                    log("Using MySQL database.", xbmc.LOGINFO)
                except Exception as e:
                    log(f"MySQL connection failed: {e}, falling back to SQLite.", xbmc.LOGWARNING)
                    self.db = None
            
            if not self.db:
                db_path = self.get_latest_db_path()
                if db_path:
                    self.db = KodiDatabase(db_path=db_path)
                    self.db.connect()
                    log("Using SQLite database.", xbmc.LOGINFO)
                else:
                    self.db = None
                    log("No Kodi Database found. Simulation only.", xbmc.LOGWARNING)
            
            if self.db:
                # Load path cache dynamically
                self.load_path_cache()
            
            self.load_scraped_files()
            
            # Get start points
            paths = self.get_scraper_roots()
            if not paths:
                log("No sources found bound to metadata.tmdb.cn.optimization", xbmc.LOGINFO)
                if self.pDialog:
                    self.pDialog.update(100, "没有源绑定到 metadata.tmdb.cn.optimization.")
                    time.sleep(1)
            else:
                path_total_process = 1/len(paths)
                for path in paths:
                    if self.check_should_stop(): raise KeyboardInterrupt()
                        
                    log(f"Processing Root Path: {path}", xbmc.LOGINFO)
                    
                    # Apply DNS overrides based on root path settings
                    overrides, _, _ = self.resolve_path_attributes(path)
                    path_settings = SettingsProxy(ADDON_SETTINGS, overrides)
                    self._apply_dns_settings(path_settings)
                    
                    # Initialize DeepSeek Extractor for this path
                    deepseek_extractor = None
                    if path_settings.getSettingBool('enable_deepseek'):
                        try:
                            from lib.deepseek_extractor import DeepSeekExtractor
                            key_file = path_settings.getSettingString('deepseek_key_file')
                            if key_file and xbmcvfs.exists(key_file):
                                with xbmcvfs.File(key_file) as f:
                                    ds_key = f.read().strip()
                                if ds_key:
                                    prompt_template = 'Parse filename to JSON: {"cn":"中文名","en":"英文名","year":"年份"}'
                                    deepseek_extractor = DeepSeekExtractor(
                                        ds_key,
                                        'https://api.deepseek.com',
                                        path_settings.getSettingString('deepseek_model'),
                                        prompt_template
                                    )
                                    log(f"DeepSeek initialized for path: {path}", xbmc.LOGINFO)
                            else:
                                log(f"DeepSeek key file not found or empty: {key_file}", xbmc.LOGWARNING)
                                xbmcgui.Dialog().notification("TMDB CN Optimization", f"DeepSeek 密钥文件未找到或为空: {key_file}", icon_path, 3000)
                        except Exception as e:
                            log(f"DeepSeek Init Error: {e}", xbmc.LOGERROR)
                            xbmcgui.Dialog().notification("TMDB CN Optimization", f"DeepSeek 初始化失败: {e}", icon_path, 3000)
                            break

                    self.scan_path(path, path_total_process, deepseek_extractor)

                # Flush final remaining futures
                while self.running_futures and not self.check_should_stop():
                     done, _ = wait(self.running_futures, return_when=FIRST_COMPLETED)
                     self.handle_finished_futures(done)
                    
        except KeyboardInterrupt:
            log("Scan cancelled by user.", xbmc.LOGINFO)
        except Exception as e:
            log(f"Scan Process Error: {traceback.format_exc()}", xbmc.LOGERROR)
            xbmcgui.Dialog().notification("TMDB CN Optimization", f"扫描出错: {e}", icon_path, 4000)
        finally:
            if self.executor:
                self.executor.shutdown(wait=False)
                self.executor = None

            if self.pDialog:
                self.pDialog.close()
                self.pDialog = None
                
            if self.db:
                self.db.close()
            # Refresh UI/Library
            self.trigger_library_refresh()
            msg = f"多线程刮削: {self.stats_processed} | 成功: {self.stats_success} | 失败: {self.stats_failed}"
            xbmcgui.Dialog().notification("TMDB CN Optimization", msg, icon_path, 5000)
            log(f"[SUMMARY] {msg.replace(chr(10), ' ')}", xbmc.LOGINFO)

            if self.failed_items:
                failed_map = {}
                for item in self.failed_items:
                    # Backward compatibility safely just in case
                    if isinstance(item, str):
                        f_path = item
                        history = []
                    else:
                        f_path = item.get('path')
                        history = item.get('history', [])

                    try: decoded_path = urllib.parse.unquote(f_path)
                    except: decoded_path = f_path
                    
                    parent_dir = os.path.dirname(decoded_path)
                    file_name = os.path.basename(decoded_path)
                    
                    if parent_dir not in failed_map:
                        failed_map[parent_dir] = []
                    failed_map[parent_dir].append((file_name, history))
                
                lines = []
                for d in sorted(failed_map.keys()):
                    lines.append(f"[COLOR yellow] {d}[/COLOR]")
                    # Sort by filename
                    for f_name, hist in sorted(failed_map[d], key=lambda x: x[0]):
                        lines.append(f"   [COLOR red]{f_name}[/COLOR]")
                        if hist:
                            for h in hist:
                                lines.append(f"      [COLOR grey]- {h}[/COLOR]")
                    lines.append("")
                        
                failed_msg = "\n".join(lines)
                
                # Also log the failed list
                log("[FAILED LIST] 刮削失败列表:", xbmc.LOGINFO)
                for d in sorted(failed_map.keys()):
                    log(f"  目录: {d}", xbmc.LOGINFO)
                    for f_name, hist in sorted(failed_map[d], key=lambda x: x[0]):
                        hist_str = " | ".join(hist) if hist else ""
                        log(f"    失败: {f_name}  {hist_str}", xbmc.LOGINFO)

                xbmcgui.Dialog().textviewer("刮削失败列表 (按目录)", failed_msg)

if __name__ == '__main__':
    if len(sys.argv) > 1 and "action=cache_images" in sys.argv[1]:
        from image_cacher import ImageCacher
        cacher = ImageCacher()
        cacher.start()
    else:
        sim = KodiScraperSimulation()
        sim.scan_and_process()
