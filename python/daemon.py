from urllib.parse import urlparse

import socket
import threading
import json
import os
import ssl
import xbmc
import xbmcaddon
import xbmcgui
import requests
import select
import concurrent.futures
import itertools
import time


# --- DoH Implementation ---
ORIGINAL_GETADDRINFO = socket.getaddrinfo
DNS_CACHE = {}
DNS_LOCK = threading.Lock()
CUSTOM_IP_MAP = {}
SYSTEM_HOSTS_MAP = {}

BUFFER_SIZE = 4096
DEFAULT_PORT = 56789
HOST = '127.0.0.1'

ADDON = xbmcaddon.Addon(id='metadata.tmdb.cn.optimization')
CHAR_MAP = {}

def load_char_map():
    global CHAR_MAP
    try:
        addon_path = ADDON.getAddonInfo('path')
        # Handle potential encoding issues with path on Windows
        if isinstance(addon_path, bytes):
            addon_path = addon_path.decode('utf-8')
            
        map_path = os.path.join(addon_path, 'resources', 'char_map.json')
        if os.path.exists(map_path):
            with open(map_path, 'r', encoding='utf-8') as f:
                CHAR_MAP = json.load(f)
            xbmc.log(f'[TMDB Daemon] Loaded char_map.json with {len(CHAR_MAP)} entries', xbmc.LOGINFO)
        else:
            xbmc.log(f'[TMDB Daemon] char_map.json not found at {map_path}', xbmc.LOGWARNING)
    except Exception as e:
        xbmc.log(f'[TMDB Daemon] Failed to load char_map.json: {e}', xbmc.LOGERROR)

MAX_HETERONYM_CHARS = 3  # 最多允许几个多音字参与排列组合，超出的取第一个读音
MAX_ORIGINALTITLE_BYTES = 64 * 1024  # originaltitle 字段最大字节数

def get_pinyin_permutations(text):
    if not text:
        return ""
    
    char_initials = []
    for char in text:
        options = set()

        pinyin_data = CHAR_MAP.get(char)
        if not pinyin_data:
            pinyin_data = CHAR_MAP.get(char.upper())

        if pinyin_data:
            readings = pinyin_data if isinstance(pinyin_data, list) else [pinyin_data]
            for p in readings:
                if p and isinstance(p, str):
                    initial = p[0].upper()
                    if "A" <= initial <= "Z":
                        options.add(initial)
        else:
            c = char.upper()
            if "A" <= c <= "Z":
                options.add(c)

        # 数字字符保留数字本身
        c = char.upper()
        if "0" <= c <= "9":
            options.add(c)

        if options:
            char_initials.append(sorted(options)[:3])

    # 限制多音字数量，超出的只保留第一个读音
    heteronym_count = 0
    for i, inits in enumerate(char_initials):
        if len(inits) > 1:
            heteronym_count += 1
            if heteronym_count > MAX_HETERONYM_CHARS:
                char_initials[i] = [inits[0]]

    # Generate Cartesian product
    try:
        permutations = list(itertools.product(*char_initials))
        # Join them back into strings
        results = ["".join(p) for p in permutations]
        # Deduplicate preserving order
        seen_res = set()
        unique_results = []
        for r in results:
            if r not in seen_res:
                seen_res.add(r)
                unique_results.append(r)
        result = "|".join(unique_results)
        if len(result.encode('utf-8')) > MAX_ORIGINALTITLE_BYTES:
            # 超出64KB限制时截断
            truncated = []
            total_len = 0
            for r in unique_results:
                added = len(r.encode('utf-8')) + (1 if truncated else 0)  # +1 for |
                if total_len + added > MAX_ORIGINALTITLE_BYTES:
                    break
                truncated.append(r)
                total_len += added
            result = "|".join(truncated)
        return result
    except Exception as e:
        xbmc.log(f'[TMDB Daemon] Pinyin generation error: {e}', xbmc.LOGERROR)
        return text

def parse_hosts_file(path):
    mapping = {}
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    parts = line.split()
                    if len(parts) >= 2:
                        ip = parts[0]
                        if is_ip_address(ip):
                            for domain in parts[1:]:
                                mapping[domain] = ip
            xbmc.log(f'[TMDB Daemon] Loaded {len(mapping)} entries from {path}', xbmc.LOGINFO)
    except Exception as e:
        xbmc.log(f'[TMDB Daemon] Failed to read hosts file {path}: {e}', xbmc.LOGWARNING)
    return mapping

def load_hosts():
    global SYSTEM_HOSTS_MAP
    SYSTEM_HOSTS_MAP = {}
    
    # System Hosts
    try:
        if os.name == 'nt':
            system_hosts = os.path.join(os.environ.get('SystemRoot', 'C:\\Windows'), 'System32', 'drivers', 'etc', 'hosts')
            SYSTEM_HOSTS_MAP.update(parse_hosts_file(system_hosts))
        else:
            system_hosts = '/etc/hosts'
            SYSTEM_HOSTS_MAP.update(parse_hosts_file(system_hosts))
    except:
        pass


def is_ip_address(host):
    try:
        socket.inet_aton(host)
        return True
    except:
        return ':' in host

def lookup_local_override(host):
    # 1. Custom IP Map (User Settings - Highest Priority)
    if host in CUSTOM_IP_MAP:
        return CUSTOM_IP_MAP[host]
        
    # 2. System Hosts File
    if host in SYSTEM_HOSTS_MAP:
        return SYSTEM_HOSTS_MAP[host]
        
    return None

def lookup_doh(host):
    with DNS_LOCK:
        if host in DNS_CACHE:
            return DNS_CACHE[host]

    doh_providers = [
        ("https://1.1.1.1/dns-query", "application/dns-json"),
        ("https://223.5.5.5/resolve", "application/json"),
        ("https://223.6.6.6/resolve", "application/json"),
    ]

    for url, accept_header in doh_providers:
        try:
            resp = requests.get(
                url,
                params={"name": host, "type": "A"},
                headers={"Accept": accept_header},
                timeout=2 
            )
            if resp.status_code == 200:
                data = resp.json()
                if 'Answer' in data:
                    for answer in data['Answer']:
                        if answer['type'] == 1: 
                            ip = answer['data']
                            with DNS_LOCK:
                                DNS_CACHE[host] = ip
                            xbmc.log(f'[TMDB Daemon] DoH Resolved {host} -> {ip} via {url}', xbmc.LOGINFO)
                            return ip
        except Exception:
            continue
    
    return None

def set_custom_ip_map(ip_map):
    """
    Allows external setting of custom IP map (e.g. from per-path scraper settings).
    ip_map: { 'domain': 'ip' }
    """
    global CUSTOM_IP_MAP
    
    for domain, ip in ip_map.items():
        if not ip:
            # Remove override if exists
            if domain in CUSTOM_IP_MAP:
                del CUSTOM_IP_MAP[domain]
                xbmc.log(f'[TMDB Daemon] Removed Global Custom IP for {domain}', xbmc.LOGINFO)
        else:
            # Update/Overwrite
            if CUSTOM_IP_MAP.get(domain) != ip:
                CUSTOM_IP_MAP[domain] = ip
                xbmc.log(f'[TMDB Daemon] Updated Global Custom IP for {domain} -> {ip}', xbmc.LOGINFO)
        

def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if is_ip_address(host):
        return ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags)
        
    # 1. Local Override
    ip = lookup_local_override(host)
    if ip:
         return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port))]

    # 2. Try DoH
    ip = lookup_doh(host)
    if ip:
        # Return IPv4 TCP address
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port))]
            
    return ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags)

socket.getaddrinfo = patched_getaddrinfo
# --------------------------

class SessionManager:
    def __init__(self):
        self._sessions = {}
        self._lock = threading.Lock()

    def get_session(self, url):
        try:
            domain = urlparse(url).netloc
        except:
            domain = "default"
        
        with self._lock:
            if domain not in self._sessions:
                s = requests.Session()
                # Configure session (e.g., headers, adapters)
                adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=20)
                s.mount('http://', adapter)
                s.mount('https://', adapter)
                self._sessions[domain] = s
                xbmc.log(f'[TMDB Daemon] -----New session created for {domain} {url}', xbmc.LOGINFO)
            return self._sessions[domain]

session_manager = SessionManager()


# Thread Pool Management
THREAD_POOL = None
POOL_LOCK = threading.Lock()
LAST_POOL_USE = 0
POOL_TIMEOUT = 20  # Seconds to keep pool alive

def get_thread_pool():
    global THREAD_POOL, LAST_POOL_USE
    with POOL_LOCK:
        LAST_POOL_USE = time.time()
        if THREAD_POOL is None:
            xbmc.log('[TMDB Daemon] Creating new ThreadPoolExecutor', xbmc.LOGDEBUG)
            THREAD_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=5)
        return THREAD_POOL

def execute_request(request):
    url = request.get('url')
    params = request.get('params')
    headers = request.get('headers', {})
    
    if not url:
        return {'error': 'No URL provided'}

    session = session_manager.get_session(url)
    
    try:
        resp = session.get(url, params=params, headers=headers, timeout=30)
        xbmc.log(f'[TMDB Daemon] -----Fetched URL: {resp.url} Status: {resp.status_code}', xbmc.LOGDEBUG)
        resp.raise_for_status()
        
        result = {
            'status': resp.status_code,
            'text': resp.text,
            'json': None
        }
        try:
            result['json'] = resp.json()
        except:
            pass
        return result
    except Exception as e:
        return {'error': str(e)}

def handle_client(conn, addr):
    try:
        data = b""
        while True:
            chunk = conn.recv(BUFFER_SIZE)
            if not chunk:
                break
            data += chunk
            try:
                json.loads(data)
                break 
            except:
                continue
        
        if not data:
            return

        payload = json.loads(data)
        if not isinstance(payload, dict):
            xbmc.log('[TMDB Daemon] Invalid payload format (not dict)', xbmc.LOGERROR)
            return

        response = {}

        # 1. Handle Custom Hosts
        if 'custom_ip' in payload:
            hosts_map = payload['custom_ip']
            if isinstance(hosts_map, dict):
                set_custom_ip_map(hosts_map)
                # Keep the same key in response, value can be success indicator
                response['custom_ip'] = {'success': True, 'count': len(hosts_map)}

        # 2. Handle HTTP Requests
        if 'requests' in payload:
            req_list = payload['requests']
            if isinstance(req_list, list):
                if not req_list:
                    response['requests'] = []
                elif len(req_list) == 1:
                    # Single request optimization potentially, but consistent return type needed
                    response['requests'] = [execute_request(req_list[0])]
                else:
                    executor = get_thread_pool()
                    response['requests'] = list(executor.map(execute_request, req_list))

        # 3. Handle Pinyin
        if 'pinyin' in payload:
            text_list = payload['pinyin']
            if isinstance(text_list, list):
                pinyin_results = []
                for text in text_list:
                    try:
                        pinyin_results.append(get_pinyin_permutations(text))
                    except Exception as e:
                        xbmc.log(f'[TMDB Daemon] Pinyin error for "{text}": {e}', xbmc.LOGERROR)
                        pinyin_results.append(text) # Fallback to original
                response['pinyin'] = pinyin_results

        # Log summary
        log_keys = list(response.keys())
        req_count = len(response.get('requests', [])) if 'requests' in response else 0
        pinyin_count = len(response.get('pinyin', [])) if 'pinyin' in response else 0
        
        xbmc.log(f'[TMDB Daemon] Processed keys: {log_keys} | Reqs: {req_count} | Pinyin: {pinyin_count}', xbmc.LOGDEBUG)

        conn.sendall(json.dumps(response).encode('utf-8'))

    except Exception as e:
        xbmc.log(f'[TMDB Daemon] Client Error: {e}', xbmc.LOGERROR)
    finally:
        conn.close()

def start_server():
    global THREAD_POOL
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    
    port = DEFAULT_PORT
    bound = False
    
    try:
        # Try default port first
        try:
            server.bind((HOST, port))
            bound = True
        except OSError:
            # Fallback to random port
            xbmc.log(f'[TMDB Daemon] Port {port} in use, trying random port', xbmc.LOGWARNING)
            server.bind((HOST, 0))
            bound = True
            
        if bound:
            port = server.getsockname()[1]
            server.listen(5)
            server.setblocking(False) # Non-blocking for select
            
            # Announce port via Window Property
            window = xbmcgui.Window(10000)
            window.setProperty('TMDB_OPTIMIZATION_SERVICE_PORT', str(port))
            
            xbmc.log(f'[TMDB Daemon] Daemon started on {HOST}:{port}', xbmc.LOGINFO)
            
            last_activity = time.time()
            IDLE_TIMEOUT = 20 # seconds
            monitor = xbmc.Monitor()
            while not monitor.abortRequested():
                # Use select to wait for connections or timeout to check abortRequested
                readable, _, _ = select.select([server], [], [], 1.0)
                
                if server in readable:
                    last_activity = time.time()
                    conn, addr = server.accept()
                    conn.setblocking(True) 
                    # Handle directly in current thread
                    handle_client(conn, addr)
                elif time.time() - last_activity > IDLE_TIMEOUT:
                    xbmc.log('[TMDB Daemon] No activity for 20s, shutting down daemon', xbmc.LOGINFO)
                    break
                
                # Check pool cleanup
                with POOL_LOCK:
                    if THREAD_POOL and (time.time() - LAST_POOL_USE > POOL_TIMEOUT):
                        xbmc.log('[TMDB Daemon] Shutting down idle ThreadPoolExecutor', xbmc.LOGINFO)
                        THREAD_POOL.shutdown(wait=False)
                        THREAD_POOL = None
                    
    except Exception as e:
        xbmc.log(f'[TMDB Daemon] Server Error: {e}', xbmc.LOGERROR)
    finally:
        server.close()
        # Clean up property
        xbmcgui.Window(10000).clearProperty('TMDB_OPTIMIZATION_SERVICE_PORT')
        
        # Ensure ThreadPool is shut down
        with POOL_LOCK:
            if THREAD_POOL:
                xbmc.log('[TMDB Daemon] Shutting down ThreadPoolExecutor on exit', xbmc.LOGINFO)
                THREAD_POOL.shutdown(wait=False)
                THREAD_POOL = None

        xbmc.log('[TMDB Daemon] Daemon stopped', xbmc.LOGINFO)

if __name__ == '__main__':
    load_char_map() # Load pinyin map
    load_hosts() # Load system and profile hosts
    start_server()
