# -*- coding: utf-8 -*-
import json
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from urllib.parse import unquote

import xbmc
import xbmcgui
import xbmcvfs
from xbmcvfs import translatePath

VIDEO_EXTS = {
    '.m4v', '.3g2', '.3gp', '.nsv', '.tp', '.ts', '.ty', '.strm', '.pls', '.rm', '.rmvb', '.mpd', '.m3u', '.m3u8',
    '.ifo', '.mov', '.qt', '.divx', '.xvid', '.bivx', '.vob', '.nrg', '.img', '.iso', '.udf', '.pva', '.wmv', '.asf',
    '.asx', '.ogm', '.m2v', '.avi', '.bin', '.dat', '.mpg', '.mpeg', '.mp4', '.mkv', '.mk3d', '.avc', '.vp3', '.svq3',
    '.nuv', '.viv', '.dv', '.fli', '.flv', '.001', '.wpl', '.xspf', '.zip', '.vdr', '.dvr-ms', '.xsp', '.mts', '.m2t',
    '.m2ts', '.evo', '.ogv', '.sdp', '.avs', '.rec', '.url', '.pxml', '.vc1', '.h264', '.rcv', '.rss', '.mpls', '.mpl',
    '.webm', '.bdmv', '.bdm', '.wtv', '.trp', '.f4v'
}

PLAYLIST_EXTS = {'.m3u', '.m3u8', '.pls', '.strm', '.wpl', '.xspf'}

def log(message, level=xbmc.LOGINFO):
    xbmc.log(f"[TMDB Check] {message}", level)


def kodi_rpc(method, params=None):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params or {},
    }
    raw = xbmc.executeJSONRPC(json.dumps(payload, ensure_ascii=False))
    data = json.loads(raw)
    if "error" in data:
        raise RuntimeError(data["error"])
    return data.get("result", {})


def norm_path(path):
    return (path or "").replace('\\', '/')


def display_path(path):
    return unquote(norm_path(path or ''))


def path_compare_key(path):
    p = norm_path(path or '').rstrip('/')
    if not p:
        return ''

    # Normalize URI authority for robust matching between DB/VFS representations.
    m = re.match(r'(?i)^([a-z][a-z0-9+.-]*://)([^/]+)(/.*)?$', p)
    if m:
        scheme = m.group(1).lower()
        netloc = m.group(2)
        tail = m.group(3) or ''

        # DB path may include user:pass@host while scanned VFS path does not.
        if '@' in netloc:
            netloc = netloc.split('@', 1)[1]

        p = scheme + netloc.lower() + tail

    # DB may store decoded names while VFS listdir returns URL-encoded names.
    p = unquote(p)
    return p.rstrip('/')


def join_url(base, name):
    return base + name if base.endswith('/') else base + '/' + name


def is_video_file(name):
    ext = ('.' + name.rsplit('.', 1)[-1].lower()) if '.' in name else ''
    if ext not in VIDEO_EXTS:
        return False

    # Align with Kodi VideoInfoScanner: playlist files are skipped except .strm.
    if ext in PLAYLIST_EXTS and ext != '.strm':
        return False

    return True


def walk_vfs(root, on_progress=None):
    stack = [root]
    seen = set()
    results = []
    processed_dirs = 0
    checked_files = 0

    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)

        try:
            dirs, files = xbmcvfs.listdir(current)
        except Exception:
            continue

        processed_dirs += 1

        for d in dirs:
            stack.append(join_url(current, d))

        for f in files:
            checked_files += 1
            if not is_video_file(f):
                continue
            full_path = norm_path(join_url(current, f))
            results.append(full_path)

            if on_progress and checked_files % 200 == 0:
                keep_running = on_progress(
                    processed_dirs,
                    checked_files,
                    len(stack),
                    current,
                    len(results),
                )
                if keep_running is False:
                    raise InterruptedError("用户取消")

        if on_progress:
            keep_running = on_progress(
                processed_dirs,
                checked_files,
                len(stack),
                current,
                len(results),
            )
            if keep_running is False:
                raise InterruptedError("用户取消")

    return results


def _row_get(row, key, index=0):
    if row is None:
        return None

    if isinstance(row, dict):
        return row.get(key)

    try:
        return row[key]
    except Exception:
        pass

    try:
        return row[index]
    except Exception:
        return None


def _find_latest_videos_db_path():
    db_dir = translatePath('special://database/')
    try:
        _, files = xbmcvfs.listdir('special://database/')
    except Exception as exc:
        raise RuntimeError(f"读取 Kodi 数据库目录失败: {exc}")

    matcher = re.compile(r'^MyVideos(\d+)\.db$', re.IGNORECASE)
    latest_name = None
    latest_version = -1

    for name in files:
        matched = matcher.match(name)
        if not matched:
            continue
        version = int(matched.group(1))
        if version > latest_version:
            latest_version = version
            latest_name = name

    if not latest_name:
        raise RuntimeError("未找到 MyVideos*.db")

    return os.path.join(db_dir, latest_name)


def _parse_mysql_from_xml(xml_path):
    try:
        if not xbmcvfs.exists(xml_path):
            return None

        tree = ET.parse(xml_path)
        root = tree.getroot()
        vdb = root.find('videodatabase')
        if vdb is None:
            return None

        db_type = (vdb.findtext('type', '') or '').strip().lower()
        if db_type != 'mysql':
            return None

        host = (vdb.findtext('host', '') or '').strip()
        port = (vdb.findtext('port', '3306') or '3306').strip()
        user = (vdb.findtext('user', '') or '').strip()
        password = (vdb.findtext('pass', '') or '').strip()
        db_name = (vdb.findtext('name', '') or '').strip()

        if not host or not user:
            return None

        return {
            'host': host,
            'port': int(port),
            'user': user,
            'password': password,
            'database': db_name,
        }
    except Exception as exc:
        log(f"解析 MySQL 配置失败: {exc}", xbmc.LOGWARNING)
        return None


def _detect_mysql_database_name(cfg):
    base_name = cfg['database'] if cfg.get('database') else 'MyVideos'

    try:
        import pymysql

        conn = pymysql.connect(
            host=cfg['host'],
            port=int(cfg['port']),
            user=cfg['user'],
            password=cfg['password'],
            connect_timeout=5,
        )
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW DATABASES LIKE %s", (base_name + '%',))
                rows = cur.fetchall()
            dbs = [r[0] for r in rows]
        finally:
            conn.close()

        if dbs:
            pattern = re.escape(base_name) + r'(\d+)'

            def get_version(name):
                m = re.search(pattern, name)
                return int(m.group(1)) if m else 0

            best = max(dbs, key=get_version)
            if get_version(best) > 0:
                cfg['database'] = best
                return cfg
    except Exception as exc:
        log(f"自动探测 MySQL 视频库名失败: {exc}", xbmc.LOGWARNING)

    if base_name == 'MyVideos':
        try:
            _, files = xbmcvfs.listdir('special://database/')
            versions = []
            for name in files:
                m = re.search(r'MyVideos(\d+)\.db', name)
                if m:
                    versions.append(int(m.group(1)))
            if versions:
                cfg['database'] = f"MyVideos{max(versions)}"
                return cfg
        except Exception:
            pass

    cfg['database'] = base_name + '131'
    return cfg


def _get_mysql_config():
    result = None
    for special in ('special://userdata/advancedsettings.xml', 'special://profile/advancedsettings.xml'):
        xml_path = translatePath(special)
        cfg = _parse_mysql_from_xml(xml_path)
        if cfg:
            result = cfg

    if not result:
        return None

    return _detect_mysql_database_name(result)


class LibraryDbReader:
    def __init__(self):
        self.conn = None
        self.is_mysql = False

    def connect(self):
        mysql_cfg = _get_mysql_config()
        if mysql_cfg:
            try:
                import pymysql

                self.conn = pymysql.connect(
                    host=mysql_cfg['host'],
                    port=int(mysql_cfg['port']),
                    user=mysql_cfg['user'],
                    password=mysql_cfg['password'],
                    database=mysql_cfg['database'],
                    charset='utf8mb3',
                    collation='utf8mb3_general_ci',
                    cursorclass=pymysql.cursors.DictCursor,
                    autocommit=True,
                )
                self.is_mysql = True
                log(
                    f"使用 MySQL: {mysql_cfg['host']}:{mysql_cfg['port']}/{mysql_cfg['database']}",
                    xbmc.LOGINFO,
                )
                return
            except Exception as exc:
                raise RuntimeError(f"连接 MySQL 失败: {exc}")

        db_path = _find_latest_videos_db_path()
        uri = f"file:{db_path}?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True)
        self.conn.row_factory = sqlite3.Row
        self.is_mysql = False
        log(f"使用 SQLite: {db_path}", xbmc.LOGINFO)

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def fetchall(self, sql, params=()):
        cur = self.conn.cursor()
        try:
            if self.is_mysql:
                sql = sql.replace('?', '%s')
            cur.execute(sql, params)
            return cur.fetchall()
        finally:
            try:
                cur.close()
            except Exception:
                pass


def get_sources(reader, content_tag):
    rows = reader.fetchall("SELECT strPath FROM path WHERE strContent=?", (content_tag,))
    sources = []

    for row in rows:
        path = norm_path(_row_get(row, 'strPath', 0) or '')
        if not path:
            continue
        if not path.endswith('/'):
            path += '/'
        if path not in sources:
            sources.append(path)

    return sources


def get_library_fullpaths(reader, media_kind):
    if media_kind == 'movie':
        concat_expr = "CONCAT(p.strPath, f.strFilename)" if reader.is_mysql else "p.strPath || f.strFilename"

        # Include both canonical movie.idFile and merged version files in videoversion.
        sql_with_versions = (
            "SELECT DISTINCT {concat} AS full_path "
            "FROM files f "
            "JOIN path p ON f.idPath=p.idPath "
            "LEFT JOIN movie m ON m.idFile=f.idFile "
            "LEFT JOIN videoversion vv ON vv.idFile=f.idFile AND vv.media_type='movie' "
            "WHERE m.idFile IS NOT NULL OR vv.idFile IS NOT NULL"
        )

        # Compatibility fallback for old schemas without videoversion table.
        sql_fallback = (
            "SELECT {concat} AS full_path "
            "FROM movie AS m "
            "JOIN files f ON m.idFile=f.idFile "
            "JOIN path p ON f.idPath=p.idPath"
        )

        try:
            rows = reader.fetchall(sql_with_versions.format(concat=concat_expr))
        except Exception:
            rows = reader.fetchall(sql_fallback.format(concat=concat_expr))
    elif media_kind == 'episode':
        sql = (
            "SELECT {concat} AS full_path "
            "FROM episode AS e "
            "JOIN files f ON e.idFile=f.idFile "
            "JOIN path p ON f.idPath=p.idPath"
        )
        concat_expr = "CONCAT(p.strPath, f.strFilename)" if reader.is_mysql else "p.strPath || f.strFilename"
        rows = reader.fetchall(sql.format(concat=concat_expr))
    else:
        return set()

    output = set()
    for row in rows:
        full_path = norm_path(_row_get(row, 'full_path', 0) or '')
        if full_path:
            output.add(full_path)
    return output


def open_folder_of(full_path):
    folder = full_path.rsplit('/', 1)[0] if '/' in full_path else full_path
    xbmc.executebuiltin(f'ActivateWindow(Videos,{folder},return)')


def scan_folder(folder):
    try:
        kodi_rpc("VideoLibrary.Scan", {"directory": folder})
        xbmcgui.Dialog().notification(
            "检查未成功刮削电影/电视剧",
            "已触发该目录扫描",
            xbmcgui.NOTIFICATION_INFO,
            3000,
        )
    except Exception as exc:
        xbmcgui.Dialog().notification(
            "检查未成功刮削电影/电视剧",
            f"扫描失败：{exc}",
            xbmcgui.NOTIFICATION_ERROR,
            4000,
        )


def _new_tree_node():
    return {
        'dirs': {},
        'files': [],
        'count': 0,
    }


def _split_path_for_tree(full_path, root_hint=''):
    path = norm_path(full_path or '')
    root = norm_path(root_hint or '')

    if root and path.startswith(root):
        relative = path[len(root):].lstrip('/')
        parts = [part for part in relative.split('/') if part]
        if parts:
            return parts

    if '://' in path:
        scheme, rest = path.split('://', 1)
        if '/' in rest:
            host, tail = rest.split('/', 1)
            parts = [f"{scheme}://{host}"]
            parts.extend([part for part in tail.split('/') if part])
            return parts
        return [path]

    return [part for part in path.split('/') if part]


def _populate_tree_counts(node):
    total = len(node['files'])
    for child in node['dirs'].values():
        total += _populate_tree_counts(child)
    node['count'] = total
    return total


def _build_file_tree(files, root_hint=''):
    root = _new_tree_node()
    for full_path in sorted(set(files)):
        parts = _split_path_for_tree(full_path, root_hint=root_hint)
        if not parts:
            continue

        node = root
        for part in parts[:-1]:
            node = node['dirs'].setdefault(part, _new_tree_node())

        node['files'].append((parts[-1], full_path))

    _populate_tree_counts(root)
    return root


def _format_tree_title(base_title, root_hint, parts):
    if root_hint:
        display_root = unquote(root_hint.rstrip('/'))
        relative = '/'.join(unquote(part) for part in parts)
        if relative:
            return f"{base_title}\n{display_root}/{relative}"
        return f"{base_title}\n{display_root}"

    if parts:
        return f"{base_title}\n{'/'.join(unquote(part) for part in parts)}"

    return base_title


def _browse_tree_and_act(title, files, root_hint=''):
    dlg = xbmcgui.Dialog()
    tree = _build_file_tree(files, root_hint=root_hint)
    stack = [(tree, [])]

    while stack:
        node, parts = stack[-1]
        entries = []

        if parts:
            entries.append(('up', '.. 返回上级', None))

        for dir_name in sorted(node['dirs'].keys(), key=lambda item: item.lower()):
            child = node['dirs'][dir_name]
            entries.append(('dir', f"[目录] {unquote(dir_name)}（{child['count']}）", dir_name))

        for file_name, full_path in sorted(node['files'], key=lambda item: item[0].lower()):
            entries.append(('file', unquote(file_name), full_path))

        if not entries:
            if parts:
                stack.pop()
                continue
            dlg.ok("检查未成功刮削电影/电视剧", "没有可显示的结果。")
            return

        idx = dlg.select(
            _format_tree_title(title, root_hint, parts),
            [item[1] for item in entries],
        )
        if idx < 0:
            if parts:
                stack.pop()
                continue
            return

        action_type, _, payload = entries[idx]
        if action_type == 'up':
            stack.pop()
            continue
        if action_type == 'dir':
            stack.append((node['dirs'][payload], parts + [payload]))
            continue

        full_path = payload
        action = dlg.contextmenu(["打开所在目录", "扫描该目录（仅此路径）"])
        if action == 0:
            open_folder_of(full_path)
        elif action == 1:
            scan_folder(full_path.rsplit('/', 1)[0] if '/' in full_path else full_path)


def choose_and_act(title, files, root_hint=''):
    dlg = xbmcgui.Dialog()

    if not files:
        dlg.ok("检查未成功刮削电影/电视剧", "没有发现未成功刮削项。")
        return

    _browse_tree_and_act(title, files, root_hint=root_hint)


def show_main_ui(groups_movie, groups_tv):
    dlg = xbmcgui.Dialog()
    total_movie = sum(len(items) for items in groups_movie.values())
    total_tv = sum(len(items) for items in groups_tv.values())

    menu = [f"电影（{total_movie}）", f"电视剧（{total_tv}）"]
    kind_idx = dlg.select("检查未成功刮削电影/电视剧 - 选择类型", menu)
    if kind_idx < 0:
        return

    groups = groups_movie if kind_idx == 0 else groups_tv
    kind_label = "电影" if kind_idx == 0 else "电视剧"

    if not groups:
        dlg.ok("检查未成功刮削电影/电视剧", f"{kind_label}：没有未成功刮削项。")
        return

    sources = sorted(groups.keys())
    items = [f"全部（{sum(len(groups[s]) for s in sources)}）"]
    items.extend([f"{src}（{len(groups[src])}）" for src in sources])

    src_idx = dlg.select(f"{kind_label}未成功刮削 - 选择来源", items)
    if src_idx < 0:
        return

    if src_idx == 0:
        files = []
        for src in sources:
            files.extend(groups[src])
        choose_and_act(f"{kind_label} - 全部未成功刮削", files)
        return

    source = sources[src_idx - 1]
    choose_and_act(f"{kind_label} - {source}", groups[source], root_hint=source)


def run_check_unscraped_media():
    reader = LibraryDbReader()
    title = "检查未成功刮削电影/电视剧"
    progress = xbmcgui.DialogProgress()
    progress_closed = False

    def update_progress(percent, line1="", line2="", line3=""):
        safe_percent = max(0, min(100, int(percent)))
        if progress.iscanceled():
            raise InterruptedError("用户取消")
        try:
            progress.update(safe_percent, line1, line2, line3)
        except TypeError:
            # Fallback for Kodi builds with older DialogProgress.update signatures.
            message = line1
            if line2:
                message += "\n" + line2
            if line3:
                message += "\n" + line3
            progress.update(safe_percent, message)

    try:
        progress.create(title, "正在初始化...")
        update_progress(1, "正在初始化...", "准备检查未成功刮削项")

        reader.connect()
        update_progress(8, "数据库连接成功", "正在读取媒体源目录")

        movie_sources = get_sources(reader, 'movies')
        tv_sources = get_sources(reader, 'tvshows')

        if not movie_sources and not tv_sources:
            xbmcgui.Dialog().notification(
                title,
                "未发现设置为电影/电视剧的媒体源目录",
                xbmcgui.NOTIFICATION_INFO,
                4000,
            )
            return

        update_progress(
            15,
            "读取媒体源完成",
            f"电影源: {len(movie_sources)} 个",
            f"电视剧源: {len(tv_sources)} 个",
        )

        inlib_movies = get_library_fullpaths(reader, 'movie')
        inlib_episodes = get_library_fullpaths(reader, 'episode')
        inlib_movies_keys = {path_compare_key(item) for item in inlib_movies}
        inlib_episodes_keys = {path_compare_key(item) for item in inlib_episodes}
        update_progress(
            20,
            "已读取入库索引",
            f"已入库电影: {len(inlib_movies)}",
            f"已入库剧集: {len(inlib_episodes)}",
        )

        groups_movie = {}
        groups_tv = {}
        scanned_count = 0

        source_jobs = [
            ('电影', src, inlib_movies_keys, groups_movie)
            for src in movie_sources
        ] + [
            ('电视剧', src, inlib_episodes_keys, groups_tv)
            for src in tv_sources
        ]

        total_sources = len(source_jobs)
        scan_start = 20.0
        scan_end = 95.0

        for idx, (media_label, src, inlib_set, groups_target) in enumerate(source_jobs):
            source_start = scan_start + (scan_end - scan_start) * (float(idx) / max(total_sources, 1))
            source_end = scan_start + (scan_end - scan_start) * (float(idx + 1) / max(total_sources, 1))

            update_progress(
                source_start,
                f"正在扫描{media_label}源 ({idx + 1}/{total_sources})",
                f"源目录: {display_path(src)}",
                "正在递归遍历目录...",
            )

            def on_scan_progress(processed_dirs, checked_files, pending_dirs, current_dir, video_hits):
                denominator = processed_dirs + pending_dirs
                source_ratio = (float(processed_dirs) / denominator) if denominator > 0 else 1.0
                percent = source_start + (source_end - source_start) * source_ratio
                update_progress(
                    percent,
                    f"正在扫描{media_label}源 ({idx + 1}/{total_sources})",
                    f"当前目录: {display_path(current_dir)}",
                    f"目录: {processed_dirs} 文件: {checked_files} 命中视频: {video_hits}",
                )
                return not progress.iscanceled()

            files = walk_vfs(src, on_progress=on_scan_progress)
            scanned_count += len(files)
            missing = [f for f in files if path_compare_key(f) not in inlib_set]
            if missing:
                groups_target[src] = sorted(missing)

            update_progress(
                source_end,
                f"{media_label}源扫描完成 ({idx + 1}/{total_sources})",
                f"源目录: {display_path(src)}",
                f"扫描视频: {len(files)} 未成功刮削: {len(missing)}",
            )

        update_progress(97, "扫描完成", "正在导出结果...")

        export_data = {
            'movies': [
                {'source': src, 'file': f}
                for src, items in groups_movie.items()
                for f in items
            ],
            'episodes': [
                {'source': src, 'file': f}
                for src, items in groups_tv.items()
                for f in items
            ],
        }

        out_path = translatePath('special://profile/missing_videos_tmdb.json')
        with xbmcvfs.File(out_path, 'w') as fh:
            fh.write(bytearray(json.dumps(export_data, ensure_ascii=False, indent=2), 'utf-8'))

        total_movie = sum(len(items) for items in groups_movie.values())
        total_tv = sum(len(items) for items in groups_tv.values())

        update_progress(
            100,
            "完成",
            f"扫描文件: {scanned_count}",
            f"未成功刮削: 电影{total_movie} / 电视剧{total_tv}",
        )

        # Close progress dialog before opening result notifications/selection dialogs.
        try:
            progress.close()
            progress_closed = True
        except Exception:
            pass

        xbmcgui.Dialog().notification(
            title,
            f"扫描源: 电影{len(movie_sources)}个、电视剧{len(tv_sources)}个；"
            f"扫描文件: {scanned_count}；未成功刮削: 电影{total_movie}、电视剧{total_tv}",
            xbmcgui.NOTIFICATION_INFO,
            6000,
        )

        show_main_ui(groups_movie, groups_tv)

    except InterruptedError:
        xbmcgui.Dialog().notification(
            title,
            "已取消本次运行",
            xbmcgui.NOTIFICATION_INFO,
            3000,
        )

    except Exception as exc:
        xbmcgui.Dialog().notification(
            title,
            f"出错：{exc}",
            xbmcgui.NOTIFICATION_ERROR,
            6000,
        )
    finally:
        if not progress_closed:
            try:
                progress.close()
            except Exception:
                pass
        reader.close()
