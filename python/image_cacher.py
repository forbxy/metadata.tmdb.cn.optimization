import os
import re
import json
import sqlite3
import urllib.parse
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor, as_completed

import xbmc
import xbmcgui
import xbmcaddon
import xbmcvfs

ADDON = xbmcaddon.Addon('metadata.tmdb.cn.optimization')


def get_image_url(url):
    """Wrap a source URL once, preserving native image transforms/options."""
    if not url:
        return None
    if not url.startswith("image://"):
        scheme = urllib.parse.urlsplit(url).scheme.lower()
        if scheme not in ("http", "https", "dav", "davs", "webdav", "webdavs"):
            return None
        # Kodi's VFS uses dav/davs for WebDAV.
        if scheme in ("webdav", "webdavs"):
            url = ("davs" if scheme == "webdavs" else "dav") + url[len(scheme):]
        url = "image://" + urllib.parse.quote(url, safe="") + "/"

    # CURL serializes percent escapes in the image host in lower case.
    # Change only the outer encoding; the source URL and options stay intact.
    host = urllib.parse.urlsplit(url).netloc
    canonical_host = re.sub(r"%[0-9a-fA-F]{2}", lambda match: match.group().lower(), host)
    return "image://" + canonical_host + url[8 + len(host):]


def get_texture_key(image_url):
    """Match Kodi's CTextureUtils::UnwrapImageURL cache identity."""
    parts = urllib.parse.urlsplit(image_url)
    suffix = image_url[8 + len(parts.netloc):]
    if "@" not in parts.netloc and "?" not in suffix:
        return urllib.parse.unquote(parts.netloc)
    # Typed images and images with transform options use the whole wrapper.
    return image_url


class ImageCacher:
    def __init__(self):
        self.thread_count = max(1, int(ADDON.getSetting('thread_count') or 8))
        self.db_path = xbmcvfs.translatePath("special://database/Textures13.db")

    def get_video_items(self):
        movies = []
        tvshows = []
        episodes = []
        movie_sets = []
        try:
            req = {"jsonrpc": "2.0", "method": "VideoLibrary.GetMovies", "params": {"properties": ["art"]}, "id": 1}
            res = json.loads(xbmc.executeJSONRPC(json.dumps(req)))
            movies = res.get("result", {}).get("movies", [])

            req = {"jsonrpc": "2.0", "method": "VideoLibrary.GetTVShows", "params": {"properties": ["art"]}, "id": 2}
            res = json.loads(xbmc.executeJSONRPC(json.dumps(req)))
            tvshows = res.get("result", {}).get("tvshows", [])

            req = {"jsonrpc": "2.0", "method": "VideoLibrary.GetEpisodes", "params": {"properties": ["art"]}, "id": 3}
            res = json.loads(xbmc.executeJSONRPC(json.dumps(req)))
            episodes = res.get("result", {}).get("episodes", [])

            req = {"jsonrpc": "2.0", "method": "VideoLibrary.GetMovieSets", "params": {"properties": ["art"]}, "id": 4}
            res = json.loads(xbmc.executeJSONRPC(json.dumps(req)))
            movie_sets = res.get("result", {}).get("sets", [])
        except:
            pass

        return movies + tvshows + episodes + movie_sets

    def _get_cached_texture(self, texture_key):
        # Read only: Kodi owns all texture filenames, metadata and DB writes.
        if not os.path.isfile(self.db_path):
            return None
        try:
            db_uri = "file:" + urllib.parse.quote(os.path.abspath(self.db_path), safe="/") + "?mode=ro"
            with closing(sqlite3.connect(db_uri, uri=True, timeout=5.0)) as conn:
                row = conn.execute(
                    "SELECT id, cachedurl FROM texture WHERE url=?", (texture_key,)
                ).fetchone()
            if row:
                return row[0], xbmcvfs.translatePath("special://thumbnails/" + row[1])
        except Exception as exc:
            xbmc.log(f"ImageCacher: Cache query failed for {texture_key}: {exc}", xbmc.LOGERROR)
        return None

    def download_image(self, kodi_url):
        image_url = get_image_url(kodi_url)
        if not image_url:
            return "skipped"

        try:
            cached = self._get_cached_texture(get_texture_key(image_url))
            if cached:
                texture_id, physical_path = cached
                try:
                    if os.path.getsize(physical_path) > 0:
                        return "skipped"
                except OSError:
                    pass
                # A stale row can prevent image:// from regenerating the file.
                # Remove only this row via Kodi, rather than editing SQLite.
                request = {
                    "jsonrpc": "2.0", "method": "Textures.RemoveTexture",
                    "params": {"textureid": texture_id}, "id": 1,
                }
                response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
                if response.get("result") != "OK":
                    raise RuntimeError(f"Cannot remove stale texture: {response}")

            # Opening image:// synchronously triggers Kodi's decoder, resize,
            # format conversion and texture DB registration. A single byte is
            # enough to verify that it produced a readable cache file.
            image_file = xbmcvfs.File(image_url)
            try:
                if image_file.readBytes(1):
                    return "success"
            finally:
                image_file.close()
            xbmc.log(f"ImageCacher: Native cache returned no data for {image_url}", xbmc.LOGERROR)
        except Exception as exc:
            xbmc.log(f"ImageCacher: Native cache failed for {image_url}: {exc}", xbmc.LOGERROR)
        return "failed"

    def start(self):
        dp_main = xbmcgui.DialogProgress()
        dp_main.create("正在缓存图片...", "正在读取所有的影视库信息，请稍候...")

        items = self.get_video_items()
        if not items:
            dp_main.close()
            xbmcgui.Dialog().ok("全量缓存图片", "媒体库中没有找到视频")
            return

        urls_to_cache = set()
        for m in items:
            if dp_main.iscanceled():
                dp_main.close()
                return
            xbmc.log(f"{json.dumps(m, indent=2)}", xbmc.LOGDEBUG)
            art = m.get("art", {})
            for k in ["poster", "fanart", "set.poster", "set.fanart"]:
                v = art.get(k)
                if isinstance(v, str):
                    image_url = get_image_url(v)
                    if image_url:
                        urls_to_cache.add(image_url)

        urls_to_cache = list(urls_to_cache)
        total = len(urls_to_cache)
        if total == 0:
            dp_main.close()
            xbmcgui.Dialog().ok("全量缓存图片", "无需缓存或数据库里没有海报")
            return

        dp_main.update(0, "初始化完成，正在通过 Kodi 原生入口缓存图片...")

        success_count = 0
        skip_count = 0
        fail_count = 0

        xbmc.log(f"ImageCacher: Thread count: {self.thread_count}, Total urls {total}", xbmc.LOGINFO)

        canceled = False
        with ThreadPoolExecutor(max_workers=self.thread_count) as executor:
            future_to_url = {executor.submit(self.download_image, url): url for url in urls_to_cache}
            for i, future in enumerate(as_completed(future_to_url)):
                if dp_main.iscanceled():
                    xbmc.log("ImageCacher: Process canceled by user.", xbmc.LOGINFO)
                    for f in future_to_url:
                        f.cancel()
                    canceled = True
                    break
                url = future_to_url[future]
                try:
                    res = future.result()
                    if res == "success":
                        success_count += 1
                    elif res == "skipped":
                        skip_count += 1
                    else:
                        fail_count += 1
                except Exception as exc:
                    xbmc.log(f"ImageCacher: Thread Execution Exception on {url}: {exc}", xbmc.LOGERROR)
                    fail_count += 1

                msg = f"总计:{total} 成功缓存:{success_count} 跳过已缓存:{skip_count} 失败:{fail_count}"
                dp_main.update(int((i+1)*100/total), f"正使用 {self.thread_count} 线程缓存海报...\n{msg}")

        dp_main.close()
        if canceled:
            xbmcgui.Dialog().ok("缓存已取消", f"任务已中止。\n成功缓存: {success_count}\n跳过已缓存: {skip_count}\n失败: {fail_count}")
        else:
            xbmcgui.Dialog().ok("缓存完成", f"总计处理图片: {total}\n成功缓存: {success_count}\n跳过已缓存: {skip_count}\n失败: {fail_count}")

if __name__ == "__main__":
    cacher = ImageCacher()
    cacher.start()
