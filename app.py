import os
import json
import re
import base64
import requests
import threading
import time
import ssl
import socket
import struct
import random
import binascii
from collections import deque
from urllib.parse import urljoin, quote, urlparse, unquote
from flask import Flask, request, jsonify, render_template_string, redirect, url_for, Response, stream_with_context

app = Flask(__name__)


def _d(s):
    return base64.b64decode(s).decode()


XC_HOST = _d("aHR0cDovL2Fwb250YWFpbWV1cGF0cmFvLmxhdDo4MA==")
XC_USERNAME = _d("amFqYWl0YQ==")
XC_PASSWORD = _d("MTIzNDU2")
F4M_PROXY = ""

ADDON_NAME = "OnePlay"
ADDON_ID = "org.oneplay.addon"
ADDON_VERSION = "1.0.0"
ADDON_DESCRIPTION = "OnePlay - Xtream Codes Addon para Stremio com F4M Proxy Embutido"

ICON_URL = "https://raw.githubusercontent.com/zoreu/oneplay_stremio/refs/heads/main/icon.png"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'
}

PROXY_CACHE_DURATION = 5
PROXY_MAX_CHUNKS = 250
PROXY_BUFFER_SIZE = 32768
PROXY_MAX_RETRIES = 7
PROXY_RETRY_DELAY = 0.5

M3U8_CACHE = {}
M3U8_CACHE_TTL = 2  # segundos

CHANNEL_CACHES = {}
MP4_CACHES = {}
IMG_CACHE = {}
CACHE_LOCK = threading.Lock()
STREAM_LOCK = threading.Lock()
IMG_CACHE_LOCK = threading.Lock()

SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE


class CircularBuffer:
    def __init__(self, max_seconds=5, max_chunks=250):
        self.buffer = deque(maxlen=max_chunks)
        self.timestamps = deque(maxlen=max_chunks)
        self.max_seconds = max_seconds
        self.total_bytes = 0
        self.lock = threading.Lock()
        self.last_update = 0
        self.stream_started = False

    def add_chunk(self, chunk):
        with self.lock:
            self.buffer.append(chunk)
            self.timestamps.append(time.time())
            self.total_bytes += len(chunk)
            self.last_update = time.time()
            cutoff = time.time() - self.max_seconds
            while self.timestamps and self.timestamps[0] < cutoff:
                removed = self.buffer.popleft()
                self.timestamps.popleft()
                self.total_bytes -= len(removed)

    def get_recovery_chunks(self, duration=3):
        with self.lock:
            if not self.buffer:
                return []
            cutoff = time.time() - duration
            recovery = []
            for i, ts in enumerate(self.timestamps):
                if ts >= cutoff:
                    recovery.append(self.buffer[i])
            if not recovery and self.buffer:
                recovery = list(self.buffer)[-20:]
            return recovery

    def get_continuous_chunks(self, count=30):
        with self.lock:
            if not self.buffer:
                return []
            return list(self.buffer)[-count:]


class MP4Cache:
    def __init__(self, max_chunks=250):
        self.chunks = {}
        self.max_chunks = max_chunks
        self.lock = threading.Lock()
        self.total_size = 0
        self.content_length = None

    def add_chunk(self, start_byte, data):
        if not data:
            return
        with self.lock:
            if start_byte not in self.chunks:
                self.chunks[start_byte] = data
                self.total_size += len(data)
                while len(self.chunks) > self.max_chunks:
                    oldest = min(self.chunks.keys())
                    self.total_size -= len(self.chunks[oldest])
                    del self.chunks[oldest]

    def get_range(self, start, end):
        with self.lock:
            keys = sorted(self.chunks.keys())
            if not keys:
                return None
            result = bytearray()
            pos = start
            while pos < end:
                found = False
                for chunk_start in keys:
                    chunk = self.chunks[chunk_start]
                    chunk_end = chunk_start + len(chunk)
                    if chunk_start <= pos < chunk_end:
                        offset = pos - chunk_start
                        take = min(end - pos, chunk_end - pos)
                        result.extend(chunk[offset:offset + take])
                        pos += take
                        found = True
                        break
                if not found:
                    return None
            return bytes(result)


def get_channel_cache(url):
    parsed = urlparse(url)
    clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    with STREAM_LOCK:
        if clean_url not in CHANNEL_CACHES:
            CHANNEL_CACHES[clean_url] = CircularBuffer(PROXY_CACHE_DURATION, PROXY_MAX_CHUNKS)
        return CHANNEL_CACHES[clean_url]


def get_mp4_cache(url):
    clean_url = re.sub(r'(_=\d+|timestamp=\d+|t=\d+|seq=\d+)', '', url)
    with CACHE_LOCK:
        if clean_url not in MP4_CACHES:
            MP4_CACHES[clean_url] = MP4Cache(PROXY_MAX_CHUNKS)
        return MP4_CACHES[clean_url]


def get_random_user_agent():
    random_bytes = binascii.b2a_hex(os.urandom(20))[:32].decode('ascii')
    return f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{random_bytes} Safari/537.36"


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def extract_url_from_path(path):
    if '/tsdownloader' in path and '?url=' in path:
        params = path.split('?', 1)[1]
        for param in params.split('&'):
            if param.startswith('url='):
                return unquote(param[4:])
    if path.startswith('/?url='):
        url_part = path[6:]
        if '&' in url_part:
            url_part = url_part.split('&', 1)[0]
        return unquote(url_part)
    if path.startswith('/http://') or path.startswith('/https://'):
        return unquote(path[1:])
    if path.startswith('http://') or path.startswith('https://'):
        return unquote(path)
    return None


def fetch_with_fallback(url, headers=None, range_header=None, cache=None, max_retries=PROXY_MAX_RETRIES):
    if headers is None:
        headers = {}

    for attempt in range(max_retries):
        if attempt == 0:
            user_agent = HEADERS['User-Agent']
        else:
            user_agent = get_random_user_agent()

        req_headers = {
            'User-Agent': user_agent,
            'Accept': '*/*',
            'Accept-Language': 'pt-BR,pt;q=0.9',
            'Accept-Encoding': 'identity',
            'Connection': 'keep-alive'
        }

        for key, value in headers.items():
            if key.lower() not in ['host', 'connection', 'content-length', 'range', 'user-agent', 'accept-encoding']:
                req_headers[key] = value

        if range_header:
            req_headers['Range'] = range_header

        try:
            resp = requests.get(url, headers=req_headers, timeout=15, stream=True, verify=False)
            if resp.status_code not in [200, 206]:
                if attempt < max_retries - 1:
                    time.sleep(PROXY_RETRY_DELAY * (attempt + 1))
                    continue
                return None, resp.status_code, None
            return resp, resp.status_code, None
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(PROXY_RETRY_DELAY * (attempt + 1))
                continue
            return None, 0, None
    return None, 0, None


def rewrite_m3u8_urls(playlist_content, base_url, proxy_host):
    def replace_url(match):
        segment = match.group(0).strip()
        if segment.startswith('#') or not segment:
            return segment
        try:
            absolute = urljoin(base_url + '/', segment)
            parsed = urlparse(absolute)
            path_lower = parsed.path.lower()

            is_segment = path_lower.endswith('.ts') or path_lower.endswith('.m3u8')
            if is_segment:
                return f"http://{proxy_host}/tsdownloader?url={quote(absolute, safe='')}"

            return segment
        except Exception:
            return segment

    return re.sub(r'^(?!#)\S+', replace_url, playlist_content, flags=re.MULTILINE)


def parse_range(range_header):
    if not range_header:
        return None
    match = re.search(r'bytes=(\d+)-(\d*)', range_header)
    if not match:
        return None
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else None
    return start, end


def parse_total_size(headers):
    content_range = headers.get('Content-Range', '') or headers.get('content-range', '')
    match = re.search(r"/(\d+)$", content_range)
    if match:
        return int(match.group(1))
    content_length = headers.get('Content-Length') or headers.get('content-length')
    if content_length and content_length.isdigit():
        return int(content_length)
    return None


def detect_mp4(url):
    lower = url.lower()
    if any(ext in lower for ext in ['.mp4', '.mkv', '.webm', '.f4v', '.mov', '.avi']):
        return True
    if '/play/' in lower:
        return True
    if 'xtream' in lower and ('/movie/' in lower or '/series/' in lower):
        return True
    return False


def fetch_mp4(url, range_header=None, method='GET', max_retries=PROXY_MAX_RETRIES):
    for attempt in range(max_retries):
        try:
            parsed = urlparse(url)
            referer = f"{parsed.scheme}://{parsed.netloc}/"

            headers = {
                'User-Agent': HEADERS['User-Agent'],
                'Accept': 'video/mp4,video/*;q=0.9,*/*;q=0.8',
                'Accept-Language': 'pt-BR,pt;q=0.9',
                'Accept-Encoding': 'identity',
                'Connection': 'keep-alive',
                'Referer': referer,
                'Origin': f"{parsed.scheme}://{parsed.netloc}",
            }

            if range_header:
                headers['Range'] = range_header

            resp = requests.request(method, url, headers=headers, timeout=30, stream=True, verify=False)
            return resp
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(PROXY_RETRY_DELAY)
                continue
            return None
    return None


def get_base_url():
    if request.headers.get('X-Forwarded-Host'):
        scheme = request.headers.get('X-Forwarded-Proto', 'https')
        host = request.headers.get('X-Forwarded-Host')
        final = f"{scheme}://{host}"
    else:
        final = request.url_root.rstrip('/')
    if not 'localhost' in final and not '127.0.0.1' in final and not '.' in final:
        final = final + '.baby-beamup.club'
    return final


def xc_request(action, params=None):
    if params is None:
        params = {}
    params.update({
        'username': XC_USERNAME,
        'password': XC_PASSWORD,
        'action': action
    })
    try:
        response = requests.get(f"{XC_HOST}/player_api.php", params=params, headers=HEADERS, timeout=15, verify=False)
        return response.json() if response.status_code == 200 else []
    except Exception:
        return []


def clean_text(text):
    if not text:
        return ''
    text = text.lower()
    accents = {
        'à': 'a', 'á': 'a', 'â': 'a', 'ã': 'a', 'ä': 'a', 'å': 'a', 'æ': 'ae', 'ç': 'c',
        'è': 'e', 'é': 'e', 'ê': 'e', 'ë': 'e', 'ì': 'i', 'í': 'i', 'î': 'i', 'ï': 'i',
        'ð': 'd', 'ñ': 'n', 'ò': 'o', 'ó': 'o', 'ô': 'o', 'õ': 'o', 'ö': 'o', 'ø': 'o',
        'ù': 'u', 'ú': 'u', 'û': 'u', 'ü': 'u', 'ý': 'y', 'þ': 'th', 'ÿ': 'y'
    }
    for k, v in accents.items():
        text = text.replace(k, v)
    return re.sub(r'[^a-z0-9]', '', text)


def build_stream_url(content_type, stream_id, episode_id=None):
    if content_type == 'movie':
        url = f"{XC_HOST}/movie/{XC_USERNAME}/{XC_PASSWORD}/{stream_id}.mp4"
    elif content_type == 'tv':
        url = f"{XC_HOST}/live/{XC_USERNAME}/{XC_PASSWORD}/{stream_id}.m3u8"
    elif content_type == 'series' and episode_id:
        url = f"{XC_HOST}/series/{XC_USERNAME}/{XC_PASSWORD}/{episode_id}.mp4"
    else:
        url = f"{XC_HOST}/series/{XC_USERNAME}/{XC_PASSWORD}/{stream_id}.mp4"

    if F4M_PROXY:
        return f"{F4M_PROXY}/?url={quote(url, safe='')}"

    try:
        base = get_base_url()
        return f"{base}/proxy/?url={quote(url, safe='')}"
    except RuntimeError:
        return url


def proxify_image_url(poster):
    if not poster:
        return poster
    if not poster.startswith('http'):
        poster = urljoin(XC_HOST + '/', poster.lstrip('/'))
    try:
        base = get_base_url()
        return f"{base}/img?url={quote(poster, safe='')}"
    except RuntimeError:
        return poster


def map_item(item, content_type):
    if content_type == 'series':
        item_id = item.get('series_id') or item.get('id', '')
        poster = (
            item.get('cover')
            or item.get('cover_big')
            or item.get('stream_icon')
            or item.get('movie_image')
        )

    elif content_type == 'tv':
        item_id = item.get('stream_id') or item.get('id', '')
        poster = item.get('stream_icon') or item.get('cover')

    else:
        item_id = item.get('stream_id') or item.get('id', '')

        poster = (
            item.get('stream_icon')
            or item.get('movie_image')
            or item.get('cover_big')
            or item.get('cover')
        )

    poster = proxify_image_url(poster)

    name = item.get('name') or item.get('title', 'Sem título')

    rating = None
    try:
        if item.get('rating'):
            rating = float(item['rating'])
    except (ValueError, TypeError):
        pass

    year = None
    try:
        if item.get('year'):
            year = int(str(item['year'])[:4])
    except (ValueError, TypeError):
        pass

    return {
        'id': f"oneplay:{content_type}:{item_id}",
        'type': content_type,
        'name': name,
        'poster': poster or None,
        'posterShape': 'square' if content_type == 'tv' else 'poster',
        'imdbRating': rating,
        'year': year,
        'description': item.get('plot') or f"OnePlay {content_type.capitalize()}"
    }


@app.route('/img')
def proxy_image():
    if request.method == 'OPTIONS':
        return Response('', status=200, headers=cors_headers())
    url = request.args.get('url')
    if not url:
        return Response('Missing url parameter', status=400, headers=cors_headers())

    with IMG_CACHE_LOCK:
        if url in IMG_CACHE:
            content_type, content = IMG_CACHE[url]
            return Response(content, status=200, headers={
                **cors_headers(),
                'Content-Type': content_type,
                'Cache-Control': 'public, max-age=86400',
            })

    try:
        parsed = urlparse(url)
        referer = f"{parsed.scheme}://{parsed.netloc}/"
        resp = requests.get(
            url,
            headers={
                'User-Agent': HEADERS['User-Agent'],
                'Accept': 'image/*,*/*;q=0.8',
                'Accept-Encoding': 'identity',
                'Referer': referer,
            },
            timeout=15,
            verify=False,
        )
        if resp.status_code not in (200, 206):
            return Response('Image fetch failed', status=resp.status_code or 502, headers=cors_headers())

        content_type = resp.headers.get('Content-Type', 'image/jpeg')
        content = resp.content
        resp.close()

        with IMG_CACHE_LOCK:
            if len(IMG_CACHE) > 500:
                IMG_CACHE.clear()
            IMG_CACHE[url] = (content_type, content)

        return Response(content, status=200, headers={
            **cors_headers(),
            'Content-Type': content_type,
            'Cache-Control': 'public, max-age=86400',
        })
    except Exception:
        return Response('Image proxy error', status=502, headers=cors_headers())


@app.route('/')
def index():
    base_url = get_base_url()
    manifest_url = f"{base_url}/manifest.json"

    html = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OnePlay - Stremio Addon</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; color: #fff; }
        .container { background: rgba(255,255,255,0.05); border-radius: 20px; padding: 40px; max-width: 500px; width: 100%; border: 1px solid rgba(255,255,255,0.1); backdrop-filter: blur(10px); }
        .logo { text-align: center; margin-bottom: 30px; }
        .logo img { width: 80px; height: 80px; border-radius: 16px; box-shadow: 0 8px 32px rgba(0,0,0,0.3); }
        h1 { text-align: center; font-size: 28px; margin-bottom: 10px; background: linear-gradient(135deg, #00d4ff, #ff006e); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .subtitle { text-align: center; color: rgba(255,255,255,0.7); margin-bottom: 30px; }
        .btn { display: block; width: 100%; padding: 16px; margin: 12px 0; border: none; border-radius: 12px; font-size: 16px; font-weight: 600; cursor: pointer; text-decoration: none; text-align: center; transition: all 0.3s; }
        .btn-stremio { background: linear-gradient(135deg, #00d4ff, #0099cc); color: white; }
        .btn-stremio:hover { transform: translateY(-2px); box-shadow: 0 8px 24px rgba(0,212,255,0.4); }
        .btn-web { background: linear-gradient(135deg, #ff006e, #cc0055); color: white; }
        .btn-web:hover { transform: translateY(-2px); box-shadow: 0 8px 24px rgba(255,0,110,0.4); }
        .btn-copy { background: rgba(255,255,255,0.1); color: #fff; border: 1px solid rgba(255,255,255,0.2); }
        .btn-copy:hover { background: rgba(255,255,255,0.2); }
        .manifest-box { background: rgba(0,0,0,0.3); border-radius: 8px; padding: 16px; margin-top: 20px; font-family: monospace; font-size: 12px; word-break: break-all; color: #00ff88; }
        .info { text-align: center; color: rgba(255,255,255,0.5); font-size: 13px; margin-top: 20px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="logo">
            <img src=""" + ICON_URL + """ alt="OnePlay">
        </div>
        <h1>OnePlay</h1>
        <p class="subtitle">Addon Stremio para Xtream Codes com F4M Proxy</p>

        <a href="stremio://addon?url=""" + quote(manifest_url) + """\" class="btn btn-stremio">📱 Instalar no App Stremio</a>
        <a href="https://web.stremio.com/#!/addons/""" + quote(manifest_url) + """\" target="_blank" class="btn btn-web">🌐 Instalar no Stremio Web</a>
        <button onclick="copyManifest()" class="btn btn-copy">📋 Copiar URL do Manifest</button>

        <div class="manifest-box" id="manifestUrl">""" + manifest_url + """</div>

        <p class="info">Funciona com servidores atrás de proxy reverso. O host base é detectado automaticamente.</p>
    </div>
    <script>
        function copyManifest() {
            const text = document.getElementById('manifestUrl').textContent;
            navigator.clipboard.writeText(text).then(() => {
                const btn = document.querySelector('.btn-copy');
                const original = btn.textContent;
                btn.textContent = '✅ Copiado!';
                setTimeout(() => btn.textContent = original, 2000);
            });
        }
    </script>
</body>
</html>
    """
    return html


@app.route('/manifest.json')
def manifest_root():
    movie_cats = xc_request('get_vod_categories')
    series_cats = xc_request('get_series_categories')
    live_cats = xc_request('get_live_categories')

    movie_genres = [c.get('category_name') for c in movie_cats if c.get('category_name')]
    series_genres = [c.get('category_name') for c in series_cats if c.get('category_name')]
    live_genres = [c.get('category_name') for c in live_cats if c.get('category_name')]

    return jsonify({
        'id': ADDON_ID,
        'name': ADDON_NAME,
        'version': ADDON_VERSION,
        'description': ADDON_DESCRIPTION,
        'logo': ICON_URL,
        'idPrefixes': ['oneplay'],
        'types': ['movie', 'series', 'tv'],
        'resources': ['catalog', 'meta', 'stream'],
        'behaviorHints': {
            'configurable': False,
            'configurationRequired': False
        },
        'catalogs': [
            {
                'id': 'oneplay_movie',
                'type': 'movie',
                'name': 'OnePlay - Filmes',
                'extra': [
                    {'name': 'genre', 'isRequired': False, 'options': movie_genres},
                    {'name': 'search', 'isRequired': False},
                    {'name': 'skip', 'isRequired': False}
                ]
            },
            {
                'id': 'oneplay_series',
                'type': 'series',
                'name': 'OnePlay - Séries',
                'extra': [
                    {'name': 'genre', 'isRequired': False, 'options': series_genres},
                    {'name': 'search', 'isRequired': False},
                    {'name': 'skip', 'isRequired': False}
                ]
            },
            {
                'id': 'oneplay_tv',
                'type': 'tv',
                'name': 'OnePlay - Canais de TV',
                'extra': [
                    {'name': 'genre', 'isRequired': False, 'options': live_genres},
                    {'name': 'search', 'isRequired': False},
                    {'name': 'skip', 'isRequired': False}
                ]
            }
        ]
    })


import time as time_module
REQUEST_START_TIME = {}


@app.before_request
def log_request():
    REQUEST_START_TIME[request.path] = time_module.time()
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    ua = request.headers.get('User-Agent', '')[:50]
    print(f">>> {request.method} {request.path} | IP: {client_ip} | UA: {ua}")


@app.after_request
def add_cors_headers(response):
    if (request.path.startswith('/proxy') or request.path.startswith('/tsdownloader')
            or request.path.startswith('/img')
            or request.path.startswith('/manifest') or request.path.startswith('/catalog')
            or request.path.startswith('/meta') or request.path.startswith('/stream')):
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, HEAD, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Range, Origin, Content-Type, Accept'

    start = REQUEST_START_TIME.pop(request.path, None)
    if start:
        elapsed = (time_module.time() - start) * 1000
        print(f"<<< {request.method} {request.path} -> {response.status_code} ({elapsed:.0f}ms)")

    return response


@app.route('/catalog/<content_type>/<catalog_id>.json')
@app.route('/catalog/<content_type>/<catalog_id>/<path:extra>.json')
def catalog(content_type, catalog_id, extra=None):
    genre = ''
    search = ''
    skip = 0

    if extra:
        for part in extra.split('&'):
            if '=' in part:
                key, value = part.split('=', 1)
                key = key.strip().lower()
                value = unquote(value)
                if key == 'genre':
                    genre = value
                elif key == 'search':
                    search = value
                elif key == 'skip':
                    try:
                        skip = int(value)
                    except ValueError:
                        skip = 0

    if not genre:
        genre = request.args.get('genre', '')
    if not search:
        search = request.args.get('search', '')

    if content_type == 'movie':
        action = 'get_vod_streams'
        cat_action = 'get_vod_categories'
    elif content_type == 'series':
        action = 'get_series'
        cat_action = 'get_series_categories'
    else:
        action = 'get_live_streams'
        cat_action = 'get_live_categories'

    metas = []

    if search:
        items = xc_request(action)
        cleaned = clean_text(search)
        count = 0
        for item in items:
            if count >= 65:
                break
            name = item.get('name') or item.get('title', '')
            if cleaned in clean_text(name):
                metas.append(map_item(item, content_type))
                count += 1
    elif genre:
        cats = xc_request(cat_action)
        cat_id = None
        for cat in cats:
            cat_name = cat.get('category_name', '')
            if cat_name == genre or cat_name.lower() == genre.lower() or clean_text(cat_name) == clean_text(genre):
                cat_id = cat.get('category_id')
                break
        if cat_id:
            items = xc_request(action, {'category_id': cat_id})
            for item in items:
                metas.append(map_item(item, content_type))
    else:
        cats = xc_request(cat_action)
        if cats:
            first_cat = cats[0].get('category_id')
            if first_cat:
                items = xc_request(action, {'category_id': first_cat})
                for item in items:
                    metas.append(map_item(item, content_type))

    if skip and skip > 0:
        metas = metas[skip:]

    metas = metas[:100]

    return jsonify({'metas': metas})


@app.route('/meta/<content_type>/<id>.json')
def meta(content_type, id):
    match = re.search(r'oneplay:(?:movie|series|tv):(\d+)', id)
    if not match:
        return jsonify({'meta': None})

    numeric_id = match.group(1)

    if content_type == 'movie':
        detail = xc_request('get_vod_info', {'vod_id': numeric_id})
        if not detail.get('info'):
            return jsonify({'meta': None})
        info = detail['info']

        poster = info.get('cover_big') or info.get('cover') or info.get('movie_image', '')
        background = info.get('backdrop_path', [poster])[0] if info.get('backdrop_path') else poster

        poster = proxify_image_url(poster)
        background = proxify_image_url(background)

        year = None
        for field in ['year', 'releasedate', 'releaseDate']:
            if info.get(field):
                try:
                    year = int(str(info[field])[:4])
                    break
                except Exception:
                    pass

        rating = None
        for field in ['rating', 'rating_5based']:
            if info.get(field):
                try:
                    rating = float(info[field])
                    break
                except Exception:
                    pass

        genres = []
        if info.get('genre'):
            genres = [g.strip() for g in info['genre'].split(',')]

        meta_data = {
            'id': f"oneplay:movie:{numeric_id}",
            'type': 'movie',
            'name': info.get('name') or info.get('title', 'Filme'),
            'description': info.get('plot') or info.get('description', ''),
            'poster': poster,
            'background': background,
            'posterShape': 'poster',
            'releaseInfo': str(year) if year else '',
            'year': year,
            'imdbRating': rating,
            'genres': genres
        }

    elif content_type == 'series':
        detail = xc_request('get_series_info', {'series_id': numeric_id})
        if not detail.get('info'):
            return jsonify({'meta': None})
        info = detail['info']

        poster = info.get('cover') or info.get('cover_big', '')
        background = info.get('backdrop_path', [poster])[0] if info.get('backdrop_path') else poster

        poster = proxify_image_url(poster)
        background = proxify_image_url(background)

        year = None
        for field in ['year', 'releaseDate', 'release_date']:
            if info.get(field):
                try:
                    year = int(str(info[field])[:4])
                    break
                except Exception:
                    pass

        rating = float(info['rating']) if info.get('rating') else None
        genres = [g.strip() for g in info['genre'].split(',')] if info.get('genre') else []

        videos = []
        episodes = detail.get('episodes', {})
        for season_num, eps in episodes.items():
            if not isinstance(eps, list):
                continue
            season = int(season_num)
            for ep in eps:
                ep_num = int(ep.get('episode_num', 0))
                thumb = ep.get('info', {}).get('movie_image') or ep.get('info', {}).get('cover_big') or ep.get('info', {}).get('cover', '')
                if thumb and not thumb.startswith('http'):
                    thumb = urljoin(XC_HOST + '/', thumb.lstrip('/'))
                thumb = proxify_image_url(thumb)
                videos.append({
                    'id': f"oneplay:series:{numeric_id}:{season}:{ep_num}",
                    'title': ep.get('title', f"Episode {ep_num}"),
                    'season': season,
                    'episode': ep_num,
                    'thumbnail': thumb,
                    'overview': ep.get('info', {}).get('plot', '')
                })

        videos.sort(key=lambda x: (x['season'], x['episode']))

        meta_data = {
            'id': f"oneplay:series:{numeric_id}",
            'type': 'series',
            'name': info.get('name') or info.get('title', 'Série'),
            'description': info.get('plot') or info.get('description', ''),
            'poster': poster,
            'background': background,
            'posterShape': 'poster',
            'releaseInfo': str(year) if year else '',
            'year': year,
            'imdbRating': rating,
            'genres': genres,
            'videos': videos
        }

    else:
        streams = xc_request('get_live_streams')
        channel = None
        for s in streams:
            if str(s.get('stream_id')) == numeric_id:
                channel = s
                break
        if not channel:
            return jsonify({'meta': None})

        poster = channel.get('stream_icon', '')
        poster = proxify_image_url(poster)

        meta_data = {
            'id': f"oneplay:tv:{numeric_id}",
            'type': 'tv',
            'name': channel.get('name', 'Canal'),
            'description': 'OnePlay Canal ao Vivo',
            'poster': poster,
            'posterShape': 'square'
        }

    meta_data = {k: v for k, v in meta_data.items() if v not in [None, '', []]}
    return jsonify({'meta': meta_data})


@app.route('/stream/<content_type>/<id>.json')
def stream(content_type, id):
    parts = id.split(':')
    if len(parts) < 3:
        return jsonify({'streams': []})

    numeric_id = parts[2]

    if content_type == 'movie':
        stream_url = build_stream_url('movie', numeric_id)
        title = 'Filme (Direto)'
    elif content_type == 'tv':
        stream_url = build_stream_url('tv', numeric_id)
        title = 'Canais ao Vivo (Direto)'
    else:
        season = parts[3] if len(parts) > 3 else ''
        episode = parts[4] if len(parts) > 4 else ''

        detail = xc_request('get_series_info', {'series_id': numeric_id})
        episodes = detail.get('episodes', {})
        episode_id = ''

        if season and episode:
            season_eps = episodes.get(season, [])
            for ep in season_eps:
                if str(ep.get('episode_num')) == episode:
                    episode_id = ep.get('id', '')
                    break

        stream_url = build_stream_url('series', numeric_id, episode_id or numeric_id)
        title = 'Série (Direto)'

    if F4M_PROXY:
        title += ' (Via F4M Proxy)'

    return jsonify({
        'streams': [{
            'name': 'OnePlay',
            'title': title,
            'url': stream_url
        }]
    })


def cors_headers():
    return {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
        'Access-Control-Allow-Headers': 'Range, Origin, Content-Type, Accept',
    }


def handle_channel_proxy(url, cache, headers, range_header):
    response, status_code, _ = fetch_with_fallback(url, headers, range_header, cache)

    if response is None:
        recovery_chunks = cache.get_recovery_chunks(PROXY_CACHE_DURATION)
        if recovery_chunks:
            def generate_recovery():
                for chunk in recovery_chunks:
                    yield chunk
            return Response(stream_with_context(generate_recovery()),
                            status=200,
                            headers={**cors_headers(),
                                     'Content-Type': 'video/mp2t',
                                     'Cache-Control': 'no-cache',
                                     'Connection': 'keep-alive'})
        return Response('No cache available', status=503, headers=cors_headers())

    content_type = response.headers.get('content-type', '').lower()
    content_url = response.url

    try:
        peek = response.raw.read(7, decode_content=False) or b''
    except Exception:
        peek = b''

    is_m3u8 = (
        'mpegurl' in content_type
        or '.m3u8' in content_url.lower()
        or peek.startswith(b'#EXTM3U')
    )

    print(f"[CH] ct={content_type!r} url={content_url} peek={peek[:10]!r} is_m3u8={is_m3u8}")

    if is_m3u8:
        try:
            rest = response.raw.read(decode_content=False) or b''
        except Exception:
            rest = b''
        response.close()
        raw_content = peek + rest

        try:
            playlist_text = raw_content.decode('utf-8', errors='ignore')
            base_url = content_url.rsplit('/', 1)[0]
            proxy_base = get_base_url().replace('https://', '').replace('http://', '')
            rewritten = rewrite_m3u8_urls(playlist_text, base_url, proxy_base)

            print(f"[CH] rewrite OK, primeiras linhas:\n{rewritten[:300]}")

            return Response(rewritten,
                            status=200,
                            headers={**cors_headers(),
                                     'Content-Type': 'application/vnd.apple.mpegurl',
                                     'Content-Length': str(len(rewritten)),
                                     'Cache-Control': 'no-cache'})
        except Exception as e:
            return f'M3U8 error: {e}', 500

    status = 206 if status_code == 206 else 200

    def generate_ts():
        try:
            if peek:
                cache.add_chunk(peek)
                yield peek
            while True:
                try:
                    chunk = response.raw.read(PROXY_BUFFER_SIZE, decode_content=False)
                except Exception:
                    break
                if not chunk:
                    break
                cache.add_chunk(chunk)
                yield chunk
        except Exception:
            pass
        finally:
            try:
                response.close()
            except Exception:
                pass

    resp_headers = {
        **cors_headers(),
        'Content-Type': 'video/mp2t',
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive'
    }

    return Response(stream_with_context(generate_ts()), status=status, headers=resp_headers)


@app.route('/proxy/')
@app.route('/proxy/<path:path>')
def proxy_root(path=''):
    if request.method == 'OPTIONS':
        return Response('', status=200, headers=cors_headers())
    url = request.args.get('url')
    if not url:
        return 'Missing url parameter', 400

    is_mp4 = detect_mp4(url)
    cache = get_mp4_cache(url) if is_mp4 else get_channel_cache(url)
    range_header = request.headers.get('Range')
    method = request.method
    headers = {k: v for k, v in request.headers if k.lower() not in ['host', 'connection']}

    if is_mp4:
        return handle_mp4_proxy(url, cache, range_header, method)
    else:
        return handle_channel_proxy(url, cache, headers, range_header)


@app.route('/tsdownloader')
def proxy_tsdownloader():
    if request.method == 'OPTIONS':
        return Response('', status=200, headers=cors_headers())
    url = request.args.get('url')
    if not url:
        return Response('Missing url parameter', status=400, headers=cors_headers())

    cache = get_channel_cache(url)
    range_header = request.headers.get('Range')
    headers = {k: v for k, v in request.headers if k.lower() not in ['host', 'connection']}
    return handle_channel_proxy(url, cache, headers, range_header)


def handle_mp4_proxy(url, cache, range_header, method):
    parsed_range = parse_range(range_header)

    if parsed_range and parsed_range[1] is not None:
        start, end = parsed_range
        cached = cache.get_range(start, end + 1)
        if cached:
            total = cache.content_length or '*'
            return Response(cached,
                            status=206,
                            headers={**cors_headers(),
                                     'Content-Type': 'video/mp4',
                                     'Accept-Ranges': 'bytes',
                                     'Content-Length': str(len(cached)),
                                     'Content-Range': f'bytes {start}-{start + len(cached) - 1}/{total}'})

    upstream = fetch_mp4(url, range_header=range_header, method=method)

    if not upstream and range_header:
        upstream = fetch_mp4(url, range_header='bytes=0-', method=method)

    if not upstream:
        return Response('Failed to connect to origin', status=503, headers=cors_headers())

    status = upstream.status_code
    total_size = parse_total_size(upstream.headers)
    if total_size:
        cache.content_length = total_size

    upstream_headers = {
        **cors_headers(),
        'Content-Type': upstream.headers.get('Content-Type', 'video/mp4'),
        'Accept-Ranges': upstream.headers.get('Accept-Ranges', 'bytes')
    }
    if upstream.headers.get('Content-Length'):
        upstream_headers['Content-Length'] = upstream.headers.get('Content-Length')
    if upstream.headers.get('Content-Range'):
        upstream_headers['Content-Range'] = upstream.headers.get('Content-Range')

    if method in ('HEAD', 'OPTIONS'):
        upstream.close()
        return Response('', status=status, headers=upstream_headers)

    def generate_mp4():
        pos = 0
        if status == 206:
            content_range = upstream.headers.get('Content-Range', '')
            match = re.search(r'bytes\s+(\d+)-', content_range)
            if match:
                pos = int(match.group(1))
            elif parsed_range:
                pos = parsed_range[0]

        try:
            for chunk in upstream.iter_content(chunk_size=PROXY_BUFFER_SIZE):
                if not chunk:
                    break
                cache.add_chunk(pos, chunk)
                pos += len(chunk)
                yield chunk
        except Exception:
            pass
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    return Response(stream_with_context(generate_mp4()), status=status, headers=upstream_headers)


@app.route('/<path:path>')
def catch_all(path):
    return jsonify({'error': 'Rota não encontrada'}), 404


if __name__ == '__main__':
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    app.run(host='0.0.0.0', port=5000, debug=True)
