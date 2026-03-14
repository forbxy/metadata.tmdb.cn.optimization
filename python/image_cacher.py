import os
import xbmc
import xbmcgui
import xbmcaddon
import xbmcvfs
import json
import urllib.parse
import struct
import requests
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

ADDON = xbmcaddon.Addon('metadata.tmdb.cn.optimization')

def get_kodi_texture_hash(url):
    crc = 0xffffffff
    for val in url.lower().encode("utf-8"):
        crc ^= val << 24
        for _ in range(8):
            crc = crc << 1 if (crc & 0x80000000) == 0 else (crc << 1) ^ 0x104c11db7
    return hex(crc & 0xFFFFFFFF).replace("0", "", 1) if hex(crc & 0xFFFFFFFF).startswith('0x') else hex(crc & 0xFFFFFFFF)[2:].zfill(8)

def get_image_metadata(img_data):
    height = 0
    width = 0
    size = len(img_data)
    if size < 24:
        return width, height
    
    try:
        # PNG
        if img_data.startswith(b'\x89PNG\r\n\x1a\n'):
            width, height = struct.unpack('>ii', img_data[16:24])
        # GIF
        elif img_data.startswith(b'GIF87a') or img_data.startswith(b'GIF89a'):
            width, height = struct.unpack('<HH', img_data[6:10])
        # BMP
        elif img_data.startswith(b'BM'):
            width, height = struct.unpack('<ii', img_data[18:26])
        # WebP
        elif img_data.startswith(b'RIFF') and img_data[8:12] == b'WEBP':
            vp8_sig = img_data[12:16]
            if vp8_sig == b'VP8 ':
                width, height = struct.unpack('<HH', img_data[26:30])
                width = width & 0x3fff
                height = height & 0x3fff
            elif vp8_sig == b'VP8L':
                b = struct.unpack('<I', img_data[21:25])[0]
                width = (b & 0x3FFF) + 1
                height = ((b >> 14) & 0x3FFF) + 1
            elif vp8_sig == b'VP8X':
                width = (img_data[24] | (img_data[25] << 8) | (img_data[26] << 16)) + 1
                height = (img_data[27] | (img_data[28] << 8) | (img_data[29] << 16)) + 1
        # JPG
        elif img_data.startswith(b'\xff\xd8'):
            i = 2
            while i < size - 2:
                # 寻找下一个 Marker 标记
                while i < size and img_data[i] != 0xFF:
                    i += 1
                while i < size and img_data[i] == 0xFF:
                    i += 1
                if i >= size:
                    break
                marker = img_data[i]
                i += 1
                if marker == 0xD9 or marker == 0xDA: # EOI or SOS
                    break
                
                # 读取区块长度
                if i + 1 >= size:
                    break
                block_length = struct.unpack('>H', img_data[i:i+2])[0]
                
                # 符合 Start of Frame (包含宽高信息) 的 Marker (0xC0 - 0xCF, 排除 0xC4, 0xC8, 0xCC)
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    if i + 5 < size:
                        height, width = struct.unpack('>HH', img_data[i+3:i+7])
                    break
                i += block_length
    except Exception:
        pass
        
    return width, height

def get_kodi_texture_path_info(url):
    crc = 0xffffffff
    for val in url.lower().encode("utf-8"):
        crc ^= val << 24
        for _ in range(8):
            crc = crc << 1 if (crc & 0x80000000) == 0 else (crc << 1) ^ 0x104c11db7
    crc_hex = format(crc & 0xFFFFFFFF, '08x')
    folder = crc_hex[0]
    
    # Kodi 原生逻辑：通过 URL 后缀判断扩展名，只允许 .png 和 .gif，其余全部兜底为 .jpg
    clean_url = url.split("?")[0].lower()
    if clean_url.endswith(".png"):
        ext = ".png"
    elif clean_url.endswith(".gif"):
        ext = ".gif"
    else:
        ext = ".jpg"
        
    cached_url = f"{folder}/{crc_hex}{ext}"
    base_vfs_path = f"special://thumbnails/{cached_url}"
    physical_path = xbmcvfs.translatePath(base_vfs_path)
    
    return physical_path, cached_url

class ImageCacher:
    def __init__(self):
        self.thread_count = int(ADDON.getSetting('thread_count') or 8)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
        })
        self.db_path = xbmcvfs.translatePath("special://database/Textures13.db")
        # 增加多线程 SQLite 写入保护锁（防止 database is locked 报错）
        self.db_lock = threading.Lock()
        
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
        
    def check_exists_in_db(self, kodi_url):
        try:
            with self.db_lock:
                conn = sqlite3.connect(self.db_path, timeout=5.0)
                c = conn.cursor()
                c.execute("SELECT cachedurl FROM texture WHERE url=?", (kodi_url,))
                row = c.fetchone()
                conn.close()
                # 如果数据库宣称缓存过，我们再查验物理文件是否真的存在
                if row:
                    cached_url = row[0]
                    physical_path = xbmcvfs.translatePath(f"special://thumbnails/{cached_url}")
                    if os.path.exists(physical_path):
                        return True
        except Exception as e:
            xbmc.log(f"ImageCacher: DB Query failed for {kodi_url}: {e}", xbmc.LOGERROR)
        return False

    def write_to_db(self, kodi_url, cachedurl, width, height):
        try:
            with self.db_lock:
                conn = sqlite3.connect(self.db_path, timeout=10.0)
                c = conn.cursor()
                
                # 遵循 Kodi 原生底层逻辑: 先删除再插入 (利用 trigger 自动清空 sizes 中的关联)
                c.execute("DELETE FROM texture WHERE url=?", (kodi_url,))
                
                # 插入新的 texture 记录
                c.execute("INSERT INTO texture (url, cachedurl, imagehash, lasthashcheck) VALUES (?, ?, '', datetime('now', 'localtime'))", (kodi_url, cachedurl))
                texture_id = c.lastrowid
                
                # 写入 sizes 表 (Kodi 19+ 将使用次数和最后使用时间等迁至此表)
                c.execute("INSERT INTO sizes (idtexture, size, width, height, usecount, lastusetime) VALUES (?, 1, ?, ?, 1, datetime('now', 'localtime'))", (texture_id, width, height))
                
                conn.commit()
                conn.close()
            return True
        except Exception as e:
            xbmc.log(f"ImageCacher: DB write failed for {kodi_url}: {e}", xbmc.LOGERROR)
            return False

    def download_image(self, kodi_url):
        if not kodi_url:
            return "skipped"
            
        # 1. 解析(Unwrap) URL：Kodi内部对image://开头的链接会先剥离外层，存入真实的直链
        unwrapped_url = kodi_url
        if kodi_url.startswith("image://"):
            try:
                unwrapped_url = kodi_url[8:]
                if unwrapped_url.endswith("/"):
                    unwrapped_url = unwrapped_url[:-1]
                unwrapped_url = urllib.parse.unquote(unwrapped_url)
            except:
                pass
                
        if not (unwrapped_url.startswith("http") or unwrapped_url.startswith("webdav") or unwrapped_url.startswith("dav")):
            return "skipped"

        # 2. 优先查库：如果数据库已有解包后真实URL的记录，且文件存在，则跳过
        if self.check_exists_in_db(unwrapped_url):
            return "skipped"
            
        # 3. 计算预存路径 (严格按照 Kodi 原生逻辑根据 URL 后缀决定扩展名)
        physical_path, cached_url = get_kodi_texture_path_info(unwrapped_url)
        
        # 4. 下载存盘并登记库
        try:
            download_url = unwrapped_url
            if download_url.startswith("webdavs://"):
                download_url = download_url.replace("webdavs://", "https://", 1)
            elif download_url.startswith("webdav://"):
                download_url = download_url.replace("webdav://", "http://", 1)
            elif download_url.startswith("davs://"):
                download_url = download_url.replace("davs://", "https://", 1)
            elif download_url.startswith("dav://"):
                download_url = download_url.replace("dav://", "http://", 1)

            import urllib3
            urllib3.disable_warnings() # 屏蔽警告
            r = self.session.get(download_url, timeout=10, stream=True, verify=False)
            if r.status_code == 200:
                out_dir = os.path.dirname(physical_path)
                if not os.path.exists(out_dir):
                    os.makedirs(out_dir, exist_ok=True)
                
                # 5. 写入本地文件系统
                img_data = b''
                with open(physical_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            img_data += chunk
                
                # 6. 解析宽高并登记到 Kodi 核心 Textures13.db
                width, height = get_image_metadata(img_data)
                
                if self.write_to_db(unwrapped_url, cached_url, width, height):
                    return "success"
                else:
                    return "failed"
            else:
                return "failed"
        except Exception as e:
            xbmc.log(f"ImageCacher: Download/DB Exception for {unwrapped_url}: {e}", xbmc.LOGERROR)
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
                if v and isinstance(v, str) and ("http" in v or "image://" in v or "webdav" in v or "dav" in v):
                    urls_to_cache.add(v)
                    
        urls_to_cache = list(urls_to_cache)
        total = len(urls_to_cache)
        if total == 0:
            dp_main.close()
            xbmcgui.Dialog().ok("全量缓存图片", "无需缓存或数据库里没有海报")
            return
            
        dp_main.update(0, "初始化完成，正在进行多线程下载与写入数据库...")
        
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
                
                msg = f"总计:{total} 成功写入/验证:{success_count} 跳过已缓存:{skip_count} 失败:{fail_count}"
                dp_main.update(int((i+1)*100/total), f"正使用 {self.thread_count} 线程缓存海报...\n{msg}")

        dp_main.close()
        if canceled:
            xbmcgui.Dialog().ok("缓存已取消", f"任务已中止。\n成功写入: {success_count}\n跳过已缓存: {skip_count}\n失败: {fail_count}")
        else:
            xbmcgui.Dialog().ok("缓存完成", f"总计处理图片: {total}\n成功写入: {success_count}\n跳过已缓存: {skip_count}\n失败: {fail_count}")

if __name__ == "__main__":
    cacher = ImageCacher()
    cacher.start()