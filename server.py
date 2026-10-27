from flask import Flask, request, jsonify, render_template, redirect, send_from_directory, send_file, make_response, abort
import subprocess
import json
import os
import socket
import time
import re
import threading
import queue
from urllib.parse import unquote
from concurrent.futures import ThreadPoolExecutor
import yt_dlp
from artists_data import get_artist_profile, get_featured_artists, CURATED_ARTISTS

try:
    from ytmusicapi import YTMusic
    YTMUSIC_CLIENT = YTMusic()
    HAS_YTMUSIC = True
    print("[✓] YouTube Music API engine initialized successfully")
except Exception as e:
    YTMUSIC_CLIENT = None
    HAS_YTMUSIC = False
    print(f"[!] Warning: ytmusicapi could not be initialized: {e}")

app = Flask(__name__)

app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True

# Persistent local disk audio cache
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, 'audio_cache')
os.makedirs(CACHE_DIR, exist_ok=True)
COOKIE_FILE = os.path.join(BASE_DIR, 'cookies.txt')

# Direct raw audio download without slow FFmpeg re-encoding (YouTube format 251 is already native Opus)
YDL_OPTS = {
    'format': '251/bestaudio[ext=webm]/bestaudio/best',
    'outtmpl': os.path.join(CACHE_DIR, '%(id)s.%(ext)s'),
    'quiet': True,
    'no_warnings': True,
    'remote_components': ['ejs:github'],
}
if os.path.exists(COOKIE_FILE) and os.path.getsize(COOKIE_FILE) > 500:
    YDL_OPTS['cookiefile'] = COOKIE_FILE

PREWARM_POOL = ThreadPoolExecutor(max_workers=4)

# Background download queue & worker management (Async Queue Engine)
download_queue = queue.Queue()
download_status = {}  # {video_id: 'pending' | 'downloading' | 'ready' | 'error'}
download_lock = threading.Lock()

def download_worker():
    """Background worker that continuously pulls from download_queue and downloads audio to disk."""
    while True:
        try:
            item = download_queue.get()
            if item is None:
                break
            video_id, url = item

            # If already cached, mark ready
            if get_cached_file(video_id):
                with download_lock:
                    download_status[video_id] = 'ready'
                download_queue.task_done()
                continue

            with download_lock:
                download_status[video_id] = 'downloading'
            print(f"[*] [Async Queue] Downloading track to cache: {video_id}")

            download_audio(video_id, url)

            cached = get_cached_file(video_id)
            with download_lock:
                download_status[video_id] = 'ready' if cached else 'error'
            if cached:
                print(f"[✓] [Async Queue] Track ready in cache: {video_id}")
        except Exception as e:
            print(f"[!] download_worker exception for {video_id}: {e}")
            with download_lock:
                download_status[video_id] = 'error'
        finally:
            download_queue.task_done()

# Start 1 dedicated background download worker (avoids socket exhaustion and thread thrashing)
for _ in range(1):
    t = threading.Thread(target=download_worker, daemon=True)
    t.start()

def enqueue_background_download(video_id):
    if not video_id:
        return
    if get_cached_file(video_id):
        with download_lock:
            download_status[video_id] = 'ready'
        return

    with download_lock:
        st = download_status.get(video_id)
        if st in ['pending', 'downloading', 'ready']:
            return
        download_status[video_id] = 'pending'

    url = f"https://www.youtube.com/watch?v={video_id}"
    download_queue.put((video_id, url))
    print(f"[+] [Async Queue] Enqueued: {video_id}")

def get_cached_file(video_id):
    """Check if the video is already cached on disk. Returns file path if valid."""
    for ext in ['opus', 'webm', 'm4a', 'mp3']:
        path = os.path.join(CACHE_DIR, f"{video_id}.{ext}")
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
    return None

def download_audio(video_id, url):
    """Downloads audio to disk cache if not cached, returns file path."""
    cached = get_cached_file(video_id)
    if cached:
        return cached

    print(f"[*] Caching audio to disk: {video_id}")
    opts = YDL_OPTS.copy()
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            final_path = ydl.prepare_filename(info)
            base_path = os.path.splitext(final_path)[0]
            for ext in ['opus', 'webm', 'm4a', 'mp3']:
                candidate = f"{base_path}.{ext}"
                if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
                    return candidate
            for ext in ['opus', 'webm', 'm4a', 'mp3']:
                candidate = os.path.join(CACHE_DIR, f"{video_id}.{ext}")
                if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
                    return candidate
    except Exception as e:
        print(f"[!] Disk cache download failed for {video_id}: {e}")
        return None
    return None

# In-memory stream cache with TTL for fallback streaming
STREAM_CACHE = {}
CACHE_TTL = 3600 * 3  # 3 hours

# In-memory search & discovery cache
SEARCH_CACHE = {}
SEARCH_CACHE_TTL = 3600 * 2

# In-memory artist discography & album view engine caches
ARTIST_SEARCH_CACHE = {}
ARTIST_ALBUMS_CACHE = {}
ALBUM_TRACKS_CACHE = {}
ARTIST_CACHE_TTL = 3600 * 12

# Heuristic filtering standards to reject news, podcasts, interviews, and non-music content
NON_MUSIC_CHANNEL_KEYWORDS = [
    'news', 'tv', 'podcast', 'radio', 'documentary', 'interview', 
    'live stream', 'ktn', 'citizen', 'breaking news', 'k24', 'ntv kenya',
    'daily nation', 'the standard', 'tuko', 'bbc news', 'cnn', 'al jazeera', 
    'press conference', 'talk show', 'commentary', 'reaction video', 'full episode',
    'media house', 'broadcast', 'parliament', 'nation media'
]

NON_MUSIC_TITLE_KEYWORDS = [
    'full interview', 'press conference', 'breaking news', 'news update',
    'exclusive interview', 'documentary film', 'podcast episode', 'reaction to',
    'talking about', 'confesses why', 'arrested', 'in court', 'drama',
    'heated debate', 'state of the nation', 'parliamentary session'
]

MIN_SONG_DURATION = 30    # Under 30s: snippets, ringtones, sound effects
MAX_SONG_DURATION = 900   # Over 15 mins: documentaries, news broadcasts, full TV shows, podcasts

def is_valid_music_channel(channel_name, user_query=""):
    if not channel_name:
        return False
    c_lower = channel_name.lower().strip()
    for kw in NON_MUSIC_CHANNEL_KEYWORDS:
        if kw in c_lower:
            return False
    return True


def is_valid_song(title, channel_name="", duration=None, user_query=""):
    # 1. Duration check (Reject < 30s snippets or > 15m documentaries/news)
    if duration is not None:
        try:
            dur = float(duration)
            if dur < MIN_SONG_DURATION or dur > MAX_SONG_DURATION:
                return False
        except (ValueError, TypeError):
            pass

    # 2. Channel check
    if channel_name and not is_valid_music_channel(channel_name, user_query):
        return False

    # 3. Title check
    t_lower = (title or "").lower().strip()
    q_lower = (user_query or "").lower().strip()
    for kw in NON_MUSIC_TITLE_KEYWORDS:
        if kw in t_lower and kw not in q_lower:
            return False

    return True

def sanitize_title(title):

    if not title:
        return "Unknown Track"
    # Clean up standard YouTube title boilerplate
    cleaned = re.sub(r'[\(\[\{](?:official|music|video|audio|lyrics|hd|4k|visualizer|remastered|explicit)[\)\]\}]', '', title, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*\|\s*.*$', '', cleaned)
    cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip()
    return cleaned if cleaned else title

def format_duration(seconds):
    if not seconds:
        return "--:--"
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m}:{s:02d}"

def extract_direct_url(video_id):
    """Extracts direct high-bitrate Opus stream URL bypassing YouTube bot check."""
    cmd = [
        'yt-dlp',
        '--remote-components', 'ejs:github',
        '-f', '251/bestaudio[ext=webm]/bestaudio/best',
    ]
    if os.path.exists(COOKIE_FILE) and os.path.getsize(COOKIE_FILE) > 500:
        cmd.extend(['--cookies', COOKIE_FILE])
    cmd.extend([
        '--get-url',
        '--no-warnings',
        f'https://www.youtube.com/watch?v={video_id}'
    ])
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=35)
        if res.returncode == 0 and res.stdout.strip():
            urls = res.stdout.strip().split('\n')
            for u in urls:
                if u.startswith('http'):
                    return u.strip()
    except Exception as e:
        print(f"[!] extract_direct_url failed for {video_id}: {e}")
    return None

def warm_single_track(video_id):
    if not video_id:
        return
    now = time.time()
    if video_id in STREAM_CACHE and (now - STREAM_CACHE[video_id][1] < CACHE_TTL):
        return
    if get_cached_file(video_id):
        return
    try:
        url = extract_direct_url(video_id)
        if url:
            STREAM_CACHE[video_id] = (url, now)
            print(f"[⚡] Pre-warmed stream URL for {video_id} into RAM cache")
    except Exception as e:
        pass

def prewarm_tracks(video_ids):
    """Asynchronously pre-resolves stream URLs so clicking any song initiates playback in 0.001s."""
    if not video_ids:
        return
    for vid in video_ids:
        if vid:
            PREWARM_POOL.submit(warm_single_track, vid)

@app.after_request
def add_cors_and_nocache_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, PUT, DELETE"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, Range"
    response.headers["Access-Control-Expose-Headers"] = "Content-Length, Content-Range"

    # DeepSeek Fix: Force WebView & mobile browsers to never serve stale cached DOM
    ct = response.content_type or ""
    if any(t in ct for t in ['text/html', 'text/css', 'application/javascript', 'text/javascript', 'application/json']):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response

@app.route('/', defaults={'path': ''}, methods=['OPTIONS'])
@app.route('/<path:path>', methods=['OPTIONS'])
def options_handler(path):
    response = make_response('', 204)
    return response

# Force the root to ALWAYS serve the Vanilla UI (Termux, Laptop & WebView)
@app.route('/')
def index():
    resp = make_response(render_template('index.html'))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

@app.route('/classic')
def classic_index():
    return redirect('/')

@app.route('/static/<path:path>')
def serve_static(path):
    resp = make_response(send_from_directory('static', path))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

@app.route('/manifest.json')
def manifest():
    resp = make_response(send_from_directory('templates', 'manifest.json'))
    resp.headers['Content-Type'] = 'application/manifest+json'
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    return resp

@app.route('/sw.js')
def service_worker():
    resp = make_response(send_from_directory('static', 'sw.js'))
    resp.headers['Content-Type'] = 'application/javascript'
    resp.headers['Service-Worker-Allowed'] = '/'
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

@app.route('/icon.png')
def icon():
    if os.path.exists(os.path.join(app.root_path, 'static', 'icon.png')):
        return send_from_directory('static', 'icon.png')
    return send_from_directory('templates', 'icon.png')

def get_code_version():
    """Returns a hash / signature of the web frontend files to detect machine edits."""
    paths = [
        os.path.join(app.root_path, 'templates', 'index.html'),
        os.path.join(app.root_path, 'server.py')
    ]
    mtimes = []
    for p in paths:
        if os.path.exists(p):
            mtimes.append(str(int(os.path.getmtime(p))))
    return "-".join(mtimes)

@app.route('/api/ping')
def ping():
    return jsonify({
        'status': 'ok',
        'app': 'b33t',
        'version': '2.3.4',
        'hostname': socket.gethostname()
    })

@app.route('/api/version')
def app_version():
    return jsonify({
        "status": "ok",
        "app": "b33t",
        "tagline": "Freedom to Music",
        "version": "3.0.0-desktop",
        "edition": "Laptop & Desktop Edition",
        "streaming_engine": "Opus Native (Format 251)",
        "timestamp": int(time.time()),
        "hostname": socket.gethostname()
    })

@app.route('/api/search/artist')
@app.route('/api/search/artist_v2')
@app.route('/api/artist/search')
def search_artist_endpoint():
    """Searches for official artist channel and top-level profile metadata with heuristic music verification."""
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({"error": "Missing query"}), 400

    clean_q = query.lower().strip()

    # 1. Check curated artists first (0ms instantaneous response)
    for k, v in CURATED_ARTISTS.items():
        if k == clean_q or clean_q in k or k in clean_q or (v.get('name') and v['name'].lower() == clean_q):
            artist_obj = {
                "id": k,
                "channel_id": k,
                "name": v['name'],
                "thumbnail": v.get('avatar') or v.get('banner') or f"https://i.ytimg.com/vi/{v.get('top_tracks', [{}])[0].get('id', '')}/hqdefault.jpg",
                "description": v.get('bio', f"Official Artist Profile for {v['name']}"),
                "bio": v.get('bio', f"Official Artist Profile for {v['name']}"),
                "genres": v.get('genres', ['Artist'])
            }
            if request.path == '/api/artist/search' and request.args.get('format') == 'list':
                return jsonify([artist_obj])
            return jsonify(artist_obj)

    # 2. Check in-memory search cache
    now = time.time()
    if clean_q in ARTIST_SEARCH_CACHE:
        cached_obj, cached_time = ARTIST_SEARCH_CACHE[clean_q]
        if now - cached_time < ARTIST_CACHE_TTL:
            if request.path == '/api/artist/search' and request.args.get('format') == 'list':
                return jsonify([cached_obj])
            return jsonify(cached_obj)

    # 3. Query YouTube Music dedicated Artist catalog (Guaranteed authentic musician entities)
    if HAS_YTMUSIC and YTMUSIC_CLIENT:
        try:
            results = YTMUSIC_CLIENT.search(query, filter="artists", limit=6)
            for res in results:
                cname = res.get('artist') or res.get('name')
                cid = res.get('browseId')
                if not cname or not cid:
                    continue
                # Heuristic verification: Reject news, tv, podcasts, media houses
                if not is_valid_music_channel(cname, clean_q):
                    continue
                thumb = None
                if res.get('thumbnails'):
                    thumb = res['thumbnails'][-1].get('url')

                desc = None
                try:
                    art_info = YTMUSIC_CLIENT.get_artist(cid)
                    if art_info:
                        desc = art_info.get('description')
                        if not thumb and art_info.get('thumbnails'):
                            thumb = art_info['thumbnails'][-1].get('url')
                except Exception:
                    pass

                if not desc:
                    desc = f"Official music, full albums, collaborations, and singles by {cname}."

                artist_obj = {
                    "id": cid,
                    "channel_id": cid,
                    "name": cname,
                    "thumbnail": thumb,
                    "description": desc,
                    "bio": desc
                }
                ARTIST_SEARCH_CACHE[clean_q] = (artist_obj, now)
                if request.path == '/api/artist/search' and request.args.get('format') == 'list':
                    return jsonify([artist_obj])
                return jsonify(artist_obj)
        except Exception as e:
            print(f"[!] ytmusicapi search_artist error for '{query}': {e}")

    # 4. Fallback: Use yt-dlp with strict heuristic filtering
    try:
        search_query = f"ytsearch5:{query} artist channel"
        ydl_opts = {
            'quiet': True,
            'extract_flat': True,
            'force_generic_extractor': False,
            'remote_components': ['ejs:github']
        }
        if os.path.exists(COOKIE_FILE) and os.path.getsize(COOKIE_FILE) > 500:
            ydl_opts['cookiefile'] = COOKIE_FILE

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(search_query, download=False)

        if 'entries' in result and len(result['entries']) > 0:
            for entry in result['entries']:
                cname = entry.get('channel') or entry.get('uploader') or query.title()
                # Heuristic check
                if not is_valid_music_channel(cname, clean_q):
                    continue
                cid = entry.get('channel_id') or entry.get('id')
                thumb = entry.get('thumbnails', [{}])[-1].get('url') if entry.get('thumbnails') else f"https://i.ytimg.com/vi/{entry.get('id', '')}/hqdefault.jpg"
                
                artist_obj = {
                    "id": cid,
                    "channel_id": cid,
                    "name": cname,
                    "thumbnail": thumb,
                    "description": entry.get('description', f"Official channel for {cname}")
                }
                ARTIST_SEARCH_CACHE[clean_q] = (artist_obj, now)
                if request.path == '/api/artist/search' and request.args.get('format') == 'list':
                    return jsonify([artist_obj])
                return jsonify(artist_obj)

        return jsonify({"error": "Artist not found"}), 404
    except Exception as e:
        print(f"[!] /api/search/artist fallback error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/artist/<channel_id>/albums')
@app.route('/api/artist_v2/<channel_id>/albums')
def get_artist_albums_endpoint(channel_id):
    """Pulls the playlists/albums tab for an artist, grouping official albums & singles."""
    cid_clean = channel_id.lower().strip()

    # 1. Curated artists: Return instant verified albums & singles
    target_artist = None
    if cid_clean in CURATED_ARTISTS:
        target_artist = CURATED_ARTISTS[cid_clean]
    else:
        for k, v in CURATED_ARTISTS.items():
            if k in cid_clean or cid_clean in k or (v.get('name') and v['name'].lower() == cid_clean):
                target_artist = v
                break

    if target_artist:
        albums = []
        for alb in target_artist.get('albums', []):
            albums.append({
                "id": alb.get('id'),
                "title": alb.get('title'),
                "thumbnail": alb.get('cover'),
                "track_count": alb.get('tracks_count') or len(alb.get('tracks', [])),
                "year": alb.get('year', '2024'),
                "type": alb.get('type', 'Album')
            })
        for s in target_artist.get('singles', []):
            albums.append({
                "id": f"single_{s.get('id')}",
                "title": s.get('title'),
                "thumbnail": s.get('thumbnail'),
                "track_count": 1,
                "year": s.get('year', '2024'),
                "type": "Single"
            })
        return jsonify(albums)

    # 2. Check in-memory albums cache
    now = time.time()
    if channel_id in ARTIST_ALBUMS_CACHE:
        cached_albums, cached_time = ARTIST_ALBUMS_CACHE[channel_id]
        if now - cached_time < ARTIST_CACHE_TTL:
            return jsonify(cached_albums)

    # 3. Pull via YouTube Music API if browseId or artist name
    if HAS_YTMUSIC and YTMUSIC_CLIENT:
        try:
            target_browse_id = channel_id
            if not target_browse_id.startswith('UC') and not target_browse_id.startswith('FE'):
                # Try finding browseId via search
                s_res = YTMUSIC_CLIENT.search(channel_id, filter="artists", limit=3)
                for cand in s_res:
                    cand_name = cand.get('artist') or cand.get('name') or ''
                    if is_valid_music_channel(cand_name, channel_id) and cand.get('browseId'):
                        target_browse_id = cand['browseId']
                        break
            
            if target_browse_id.startswith('UC') or target_browse_id.startswith('FE'):
                art_details = YTMUSIC_CLIENT.get_artist(target_browse_id)
                albums = []
                for alb in art_details.get('albums', {}).get('results', []):
                    pid = alb.get('browseId') or alb.get('audioPlaylistId')
                    title = alb.get('title')
                    if pid and title and is_valid_song(title):
                        thumb = alb.get('thumbnails', [{}])[-1].get('url') if alb.get('thumbnails') else None
                        albums.append({
                            "id": pid,
                            "title": title,
                            "thumbnail": thumb,
                            "track_count": alb.get('trackCount') or 0,
                            "year": alb.get('year', ''),
                            "type": "Album"
                        })
                for s in art_details.get('singles', {}).get('results', []):
                    pid = s.get('browseId')
                    title = s.get('title')
                    if pid and title and is_valid_song(title):
                        thumb = s.get('thumbnails', [{}])[-1].get('url') if s.get('thumbnails') else None
                        albums.append({
                            "id": pid,
                            "title": title,
                            "thumbnail": thumb,
                            "track_count": 1,
                            "year": s.get('year', ''),
                            "type": "Single"
                        })
                if albums:
                    ARTIST_ALBUMS_CACHE[channel_id] = (albums, now)
                    return jsonify(albums)
        except Exception as e:
            print(f"[!] ytmusicapi get_artist albums error for {channel_id}: {e}")

    # 4. Fallback: Pull YouTube playlists tab with heuristic filtering
    try:
        channel_url = f"https://www.youtube.com/channel/{channel_id}/playlists"
        ydl_opts = {
            'quiet': True,
            'extract_flat': True,
            'playlistend': 30,
            'remote_components': ['ejs:github']
        }
        if os.path.exists(COOKIE_FILE) and os.path.getsize(COOKIE_FILE) > 500:
            ydl_opts['cookiefile'] = COOKIE_FILE

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(channel_url, download=False)

        albums = []
        if 'entries' in result and result['entries']:
            for entry in result['entries']:
                pid = entry.get('id')
                title = entry.get('title')
                if pid and title:
                    title_lower = title.lower()
                    if any(bad in title_lower for bad in ['shorts', 'liked videos', 'favorites', 'history']):
                        continue
                    if not is_valid_song(title):
                        continue
                    thumb = entry.get('thumbnails', [{}])[-1].get('url') if entry.get('thumbnails') else None
                    albums.append({
                        "id": pid,
                        "title": title,
                        "thumbnail": thumb,
                        "track_count": entry.get('playlist_count', 0),
                        "type": "Album"
                    })

        # If channel has no playlists tab, fallback to searching for albums/playlists
        if not albums:
            fallback_query = f"ytsearch10:{channel_id} album playlist"
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                fb_res = ydl.extract_info(fallback_query, download=False)
            if 'entries' in fb_res:
                for entry in fb_res['entries']:
                    if entry.get('id') and entry.get('title'):
                        if not is_valid_song(entry.get('title')):
                            continue
                        thumb = entry.get('thumbnails', [{}])[-1].get('url') if entry.get('thumbnails') else None
                        albums.append({
                            "id": entry.get('id'),
                            "title": entry.get('title'),
                            "thumbnail": thumb,
                            "track_count": entry.get('playlist_count', 0),
                            "type": "Album"
                        })

        ARTIST_ALBUMS_CACHE[channel_id] = (albums, now)
        return jsonify(albums)
    except Exception as e:
        print(f"[!] /api/artist/{channel_id}/albums fallback error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/album/<playlist_id>/tracks')
@app.route('/api/album_v2/<playlist_id>/tracks')
def get_album_tracks_endpoint(playlist_id):
    """Pulls tracks inside a playlist or album with strict duration and heuristic filtering."""
    # 1. Check curated artists & albums
    for artist_key, a_data in CURATED_ARTISTS.items():
        if playlist_id.startswith("single_"):
            single_id = playlist_id[7:]
            for s in a_data.get('singles', []):
                if s.get('id') == single_id:
                    return jsonify([{
                        "id": s.get('id'),
                        "title": s.get('title'),
                        "duration": s.get('duration', 180),
                        "duration_str": s.get('duration_str', '3:00'),
                        "thumbnail": s.get('thumbnail'),
                        "channel": a_data['name']
                    }])
        for alb in a_data.get('albums', []):
            if alb.get('id') == playlist_id:
                tracks = []
                for t in alb.get('tracks', []):
                    tracks.append({
                        "id": t.get('id'),
                        "title": t.get('title'),
                        "duration": t.get('duration', 200),
                        "duration_str": t.get('duration_str', '3:20'),
                        "thumbnail": t.get('thumbnail') or alb.get('cover'),
                        "channel": t.get('artist') or a_data['name']
                    })
                return jsonify(tracks)

    # 2. Check in-memory cache
    now = time.time()
    if playlist_id in ALBUM_TRACKS_CACHE:
        cached_tracks, cached_time = ALBUM_TRACKS_CACHE[playlist_id]
        if now - cached_time < ARTIST_CACHE_TTL:
            return jsonify(cached_tracks)

    # 3. Pull via YouTube Music API if browseId (MPREb_... or OLAK5uy_...)
    if HAS_YTMUSIC and YTMUSIC_CLIENT and (playlist_id.startswith('MPRE') or playlist_id.startswith('OLAK')):
        try:
            alb = YTMUSIC_CLIENT.get_album(playlist_id)
            album_thumb = alb.get('thumbnails', [{}])[-1].get('url') if alb.get('thumbnails') else None
            tracks = []
            for t in alb.get('tracks', []):
                vid = t.get('videoId')
                if not vid:
                    continue
                dur = t.get('duration_seconds')
                if not dur and t.get('duration'):
                    parts = str(t['duration']).split(':')
                    if len(parts) == 2: dur = int(parts[0])*60 + int(parts[1])
                    elif len(parts) == 3: dur = int(parts[0])*3600 + int(parts[1])*60 + int(parts[2])
                dur = dur or 200

                # Duration & Title heuristic filter (reject <30s snippets or >900s news/documentaries)
                if not is_valid_song(t.get('title', ''), duration=dur):
                    continue

                artist_name = t.get('artists', [{}])[0].get('name') if t.get('artists') else alb.get('artists', [{}])[0].get('name', 'Artist')
                thumb = album_thumb
                if not thumb and t.get('thumbnails'):
                    thumb = t['thumbnails'][-1].get('url')
                if not thumb:
                    thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

                tracks.append({
                    "id": vid,
                    "title": sanitize_title(t.get('title')),
                    "raw_title": t.get('title'),
                    "duration": dur,
                    "duration_str": t.get('duration') or format_duration(dur),
                    "thumbnail": thumb,
                    "channel": artist_name
                })
            if tracks:
                ALBUM_TRACKS_CACHE[playlist_id] = (tracks, now)
                return jsonify(tracks)
        except Exception as e:
            print(f"[!] ytmusicapi get_album error for {playlist_id}: {e}")

    # 4. Fallback: Pull YouTube playlist tracks with heuristic duration and title filtering
    try:
        playlist_url = f"https://www.youtube.com/playlist?list={playlist_id}" if not playlist_id.startswith("http") else playlist_id
        ydl_opts = {
            'quiet': True,
            'extract_flat': True,
            'remote_components': ['ejs:github']
        }
        if os.path.exists(COOKIE_FILE) and os.path.getsize(COOKIE_FILE) > 500:
            ydl_opts['cookiefile'] = COOKIE_FILE

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(playlist_url, download=False)

        tracks = []
        if 'entries' in result:
            for entry in result['entries']:
                if entry.get('id'):
                    dur = entry.get('duration') or 200
                    # Apply heuristic filter (reject non-music videos)
                    channel_name = entry.get('uploader') or entry.get('channel', 'Artist')
                    if not is_valid_song(entry.get('title', ''), channel_name=channel_name, duration=dur):
                        continue
                    dur_str = f"{int(dur)//60}:{int(dur)%60:02d}" if isinstance(dur, (int, float)) else "--:--"
                    thumb = entry.get('thumbnails', [{}])[-1].get('url') if entry.get('thumbnails') else f"https://i.ytimg.com/vi/{entry.get('id')}/hqdefault.jpg"
                    tracks.append({
                        "id": entry.get('id'),
                        "title": sanitize_title(entry.get('title')),
                        "raw_title": entry.get('title'),
                        "duration": dur,
                        "duration_str": dur_str,
                        "thumbnail": thumb,
                        "channel": channel_name
                    })
        ALBUM_TRACKS_CACHE[playlist_id] = (tracks, now)
        return jsonify(tracks)
    except Exception as e:
        print(f"[!] /api/album/{playlist_id}/tracks fallback error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/artist/channel/<channel_id>/tracks')
def get_channel_tracks_endpoint(channel_id):
    """Constructs URL for artist channel videos and pulls up to 50 tracks."""
    try:
        channel_url = f"https://www.youtube.com/channel/{channel_id}/videos"
        ydl_opts = {
            'quiet': True,
            'extract_flat': True,
            'playlistend': 50,
            'remote_components': ['ejs:github']
        }
        if os.path.exists(COOKIE_FILE) and os.path.getsize(COOKIE_FILE) > 500:
            ydl_opts['cookiefile'] = COOKIE_FILE

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(channel_url, download=False)

        tracks = []
        if 'entries' in result:
            for entry in result['entries']:
                if entry.get('id'):
                    dur = entry.get('duration') or 200
                    dur_str = f"{int(dur)//60}:{int(dur)%60:02d}" if isinstance(dur, (int, float)) else "--:--"
                    thumb = entry.get('thumbnails', [{}])[-1].get('url') if entry.get('thumbnails') else f"https://i.ytimg.com/vi/{entry.get('id')}/hqdefault.jpg"
                    tracks.append({
                        "id": entry.get('id'),
                        "title": entry.get('title', 'Unknown Track'),
                        "duration": dur,
                        "duration_str": dur_str,
                        "thumbnail": thumb,
                        "artist": entry.get('channel') or entry.get('uploader', 'Artist')
                    })
        return jsonify(tracks)
    except Exception as e:
        print(f"[!] /api/artist/channel tracks error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/artist/<name>')
@app.route('/api/artist_v2/<name>')
def artist_endpoint(name):
    clean_name = unquote(name).strip()
    profile = get_artist_profile(clean_name)
    if not profile:
        # Dynamic channel & artist resolution via YouTube Music API
        if HAS_YTMUSIC and YTMUSIC_CLIENT:
            try:
                search_res = YTMUSIC_CLIENT.search(clean_name, filter="artists", limit=4)
                target_browse_id = None
                for cand in search_res:
                    cand_name = cand.get('artist') or cand.get('name') or ''
                    if is_valid_music_channel(cand_name, clean_name) and cand.get('browseId'):
                        target_browse_id = cand['browseId']
                        break
                
                if target_browse_id:
                    art = YTMUSIC_CLIENT.get_artist(target_browse_id)
                    avatar = art.get('thumbnails', [{}])[-1].get('url') if art.get('thumbnails') else None
                    banner = avatar
                    
                    tracks = []
                    songs_data = art.get('songs', {})
                    # Try fetching full top songs playlist
                    pl_browse_id = songs_data.get('browseId')
                    if pl_browse_id:
                        try:
                            pl = YTMUSIC_CLIENT.get_playlist(pl_browse_id, limit=35)
                            for t in pl.get('tracks', []):
                                vid = t.get('videoId')
                                if not vid: continue
                                dur = t.get('duration_seconds') or 200
                                if not is_valid_song(t.get('title', ''), duration=dur): continue
                                thumb = t.get('thumbnails', [{}])[-1].get('url') if t.get('thumbnails') else f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
                                tracks.append({
                                    "id": vid,
                                    "title": sanitize_title(t.get('title')),
                                    "raw_title": t.get('title'),
                                    "artist": t.get('artists', [{}])[0].get('name') if t.get('artists') else art.get('name', clean_name),
                                    "duration": dur,
                                    "duration_str": t.get('duration') or format_duration(dur),
                                    "thumbnail": thumb
                                })
                        except Exception as pe:
                            print(f"[!] Warning: could not fetch top songs playlist for {clean_name}: {pe}")
                    
                    if not tracks and songs_data.get('results'):
                        for t in songs_data['results']:
                            vid = t.get('videoId')
                            if not vid: continue
                            dur = t.get('duration_seconds') or 200
                            if not is_valid_song(t.get('title', ''), duration=dur): continue
                            thumb = t.get('thumbnails', [{}])[-1].get('url') if t.get('thumbnails') else f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
                            tracks.append({
                                "id": vid,
                                "title": sanitize_title(t.get('title')),
                                "raw_title": t.get('title'),
                                "artist": t.get('artists', [{}])[0].get('name') if t.get('artists') else art.get('name', clean_name),
                                "duration": dur,
                                "duration_str": t.get('duration') or format_duration(dur),
                                "thumbnail": thumb
                            })

                    # Official Albums
                    albums = []
                    for alb in art.get('albums', {}).get('results', []):
                        pid = alb.get('browseId') or alb.get('audioPlaylistId')
                        if pid and is_valid_song(alb.get('title', '')):
                            albums.append({
                                "id": pid,
                                "title": alb.get('title'),
                                "thumbnail": alb.get('thumbnails', [{}])[-1].get('url') if alb.get('thumbnails') else None,
                                "year": alb.get('year', ''),
                                "type": "Album"
                            })

                    # Official Singles
                    singles = []
                    for s in art.get('singles', {}).get('results', []):
                        pid = s.get('browseId')
                        if pid and is_valid_song(s.get('title', '')):
                            singles.append({
                                "id": pid,
                                "title": s.get('title'),
                                "thumbnail": s.get('thumbnails', [{}])[-1].get('url') if s.get('thumbnails') else None,
                                "year": s.get('year', ''),
                                "type": "Single"
                            })

                    if tracks or albums:
                        profile = {
                            "name": art.get('name', clean_name),
                            "channel_id": target_browse_id,
                            "verified": True,
                            "listeners": f"{art.get('subscribers', 'Official Artist')} • Official Channel",
                            "location": "Global",
                            "bio": art.get('description') or f"Official releases, studio albums, and singles by {art.get('name', clean_name)}.",
                            "avatar": avatar or (f"https://i.ytimg.com/vi/{tracks[0]['id']}/hqdefault.jpg" if tracks else ""),
                            "banner": banner or avatar or "",
                            "genres": ["Contemporary Music"],
                            "top_tracks": tracks[:20],
                            "albums": albums,
                            "singles": singles,
                            "collabos": [],
                            "related_artists": []
                        }
            except Exception as e:
                print(f"[!] Dynamic artist generation via ytmusicapi error for {clean_name}: {e}")

        # Fallback to yt-dlp with heuristic validation
        if not profile:
            try:
                search_query = f"ytsearch15:{clean_name} official audio"
                ydl_opts = {
                    'quiet': True,
                    'extract_flat': True,
                    'remote_components': ['ejs:github']
                }
                if os.path.exists(COOKIE_FILE) and os.path.getsize(COOKIE_FILE) > 500:
                    ydl_opts['cookiefile'] = COOKIE_FILE
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    res = ydl.extract_info(search_query, download=False)
                entries = res.get('entries', [])
                tracks = []
                avatar = None
                for e in entries:
                    if e.get('id'):
                        dur = e.get('duration') or 200
                        channel_cand = e.get('channel') or e.get('uploader') or clean_name
                        if not is_valid_song(e.get('title', ''), channel_name=channel_cand, duration=dur, user_query=clean_name):
                            continue
                        dur_str = f"{int(dur)//60}:{int(dur)%60:02d}" if isinstance(dur, (int, float)) else "--:--"
                        thumb = e.get('thumbnails', [{}])[-1].get('url') if e.get('thumbnails') else f"https://i.ytimg.com/vi/{e.get('id')}/hqdefault.jpg"
                        if not avatar and thumb:
                            avatar = thumb
                        tracks.append({
                            "id": e.get('id'),
                            "title": sanitize_title(e.get('title', 'Song')),
                            "artist": channel_cand,
                            "duration": dur,
                            "duration_str": dur_str,
                            "thumbnail": thumb
                        })
                if tracks:
                    profile = {
                        "name": clean_name,
                        "verified": True,
                        "listeners": "Popular Artist",
                        "location": "Global",
                        "bio": f"Official music releases, top tracks, and features by {clean_name}.",
                        "avatar": avatar or (f"https://i.ytimg.com/vi/{tracks[0]['id']}/hqdefault.jpg" if tracks else ""),
                        "banner": avatar or (f"https://i.ytimg.com/vi/{tracks[0]['id']}/hqdefault.jpg" if tracks else ""),
                        "genres": ["Contemporary"],
                        "top_tracks": tracks[:10],
                        "albums": [],
                        "singles": tracks[10:15] if len(tracks) > 10 else [],
                        "collabos": [],
                        "related_artists": []
                    }
            except Exception as e:
                print(f"[!] Dynamic artist generation fallback error: {e}")

    if not profile:
        return jsonify({'error': 'Artist not found'}), 404
    # Pre-warm stream URLs for the top 4 songs immediately in background
    top_ids = [t['id'] for t in profile.get('top_tracks', [])[:4] if t.get('id')]
    if top_ids:
        threading.Thread(target=prewarm_tracks, args=(top_ids,), daemon=True).start()
    return jsonify(profile)

@app.route('/api/artist/<name>/tracks')
def artist_tracks_endpoint(name):
    offset = int(request.args.get('offset', 0))
    limit = int(request.args.get('limit', 12))
    
    clean_name = unquote(name).strip()
    profile = get_artist_profile(clean_name)
    curated_top = []
    seen_ids = set()
    if profile:
        for t in profile.get('top_tracks', []):
            if t.get('id') and t['id'] not in seen_ids:
                curated_top.append(t)
                seen_ids.add(t['id'])
        for alb in profile.get('albums', []):
            for t in alb.get('tracks', []):
                if t.get('id') and t['id'] not in seen_ids:
                    t_copy = dict(t)
                    t_copy['thumbnail'] = t_copy.get('thumbnail') or alb.get('cover') or profile.get('avatar')
                    curated_top.append(t_copy)
                    seen_ids.add(t['id'])
        for c in profile.get('collabos', []):
            if c.get('id') and c['id'] not in seen_ids:
                c_copy = dict(c)
                c_copy['artist'] = c_copy.get('collab_with', profile.get('name'))
                c_copy['thumbnail'] = c_copy.get('thumbnail') or profile.get('avatar')
                curated_top.append(c_copy)
                seen_ids.add(c['id'])
        for s in profile.get('singles', []):
            if s.get('id') and s['id'] not in seen_ids:
                s_copy = dict(s)
                s_copy['thumbnail'] = s_copy.get('thumbnail') or profile.get('avatar')
                curated_top.append(s_copy)
                seen_ids.add(s['id'])
    
    # 1. If offset falls within curated top tracks:
    if offset < len(curated_top):
        slice_curated = curated_top[offset : offset + limit]
        if len(slice_curated) >= limit:
            return jsonify(slice_curated)
        needed = limit - len(slice_curated)
        search_query = f"{clean_name} official audio songs"
        more_tracks = get_search_tracks_list(search_query, offset=0, limit=needed + 8)
        curated_ids = {t['id'] for t in curated_top if t.get('id')}
        unique_more = [t for t in more_tracks if t.get('id') not in curated_ids][:needed]
        combined = slice_curated + unique_more
        prewarm_tracks([t['id'] for t in combined[:3] if t.get('id')])
        return jsonify(combined)
    
    # 2. If offset is beyond curated top tracks:
    search_offset = offset - len(curated_top)
    search_query = f"{clean_name} official audio songs"
    more_tracks = get_search_tracks_list(search_query, offset=search_offset, limit=limit + 8)
    curated_ids = {t['id'] for t in curated_top if t.get('id')}
    unique_more = [t for t in more_tracks if t.get('id') not in curated_ids][:limit]
    prewarm_tracks([t['id'] for t in unique_more[:3] if t.get('id')])
    return jsonify(unique_more)

@app.route('/api/featured_artists')
def featured_artists_endpoint():
    return jsonify(get_featured_artists())

def get_search_tracks_list(query, offset=0, limit=12):
    now = time.time()
    if query in SEARCH_CACHE:
        cached_data, cached_time = SEARCH_CACHE[query]
        if now - cached_time < SEARCH_CACHE_TTL:
            if offset < len(cached_data):
                res = cached_data[offset : offset + limit]
                prewarm_tracks([t['id'] for t in res[:3] if t.get('id')])
                return res

    # 1. Primary: YouTube Music API (Clean studio tracks, zero news or documentary noise)
    if HAS_YTMUSIC and YTMUSIC_CLIENT:
        try:
            fetch_count = max(40, offset + limit + 12)
            raw_songs = YTMUSIC_CLIENT.search(query, filter="songs", limit=fetch_count)
            tracks = []
            seen_ids = set()
            for s in raw_songs:
                vid = s.get('videoId')
                if not vid or vid in seen_ids:
                    continue
                title = s.get('title', '')
                artists_list = s.get('artists', [])
                art_name = artists_list[0].get('name') if artists_list else 'Artist'
                dur = s.get('duration_seconds')
                if not dur and s.get('duration'):
                    parts = str(s['duration']).split(':')
                    if len(parts) == 2: dur = int(parts[0])*60 + int(parts[1])
                    elif len(parts) == 3: dur = int(parts[0])*3600 + int(parts[1])*60 + int(parts[2])
                dur = dur or 210

                # Strict heuristic filter (30s <= dur <= 900s, filter out news/podcasts/interviews)
                if not is_valid_song(title, channel_name=art_name, duration=dur, user_query=query):
                    continue

                thumb = s.get('thumbnails', [{}])[-1].get('url') if s.get('thumbnails') else f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

                tracks.append({
                    'id': vid,
                    'title': sanitize_title(title),
                    'raw_title': title,
                    'artist': art_name,
                    'duration': dur,
                    'duration_str': s.get('duration') or format_duration(dur),
                    'thumbnail': thumb
                })
                seen_ids.add(vid)

            if tracks:
                SEARCH_CACHE[query] = (tracks, now)
                res = tracks[offset : offset + limit]
                prewarm_tracks([t['id'] for t in res[:4] if t.get('id')])
                return res
        except Exception as ye:
            print(f"[!] ytmusicapi search error for '{query}': {ye}")

    # 2. Fallback: yt-dlp with strict heuristic filtering
    fetch_count = max(40, offset + limit + 14)
    cmd = ['yt-dlp']
    if os.path.exists(COOKIE_FILE) and os.path.getsize(COOKIE_FILE) > 500:
        cmd.extend(['--cookies', COOKIE_FILE])
    cmd.extend([
        f'ytsearch{fetch_count}:{query}', 
        '--dump-json', 
        '--flat-playlist', 
        '--no-warnings'
    ])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        tracks = []
        for line in result.stdout.strip().split('\n'):
            if not line: continue
            data = json.loads(line)
            duration_sec = data.get('duration')
            
            # Filter out livestreams, radios, 24/7 channels
            if data.get('is_live') or data.get('live_status') in ('is_live', 'is_upcoming', 'post_live'):
                continue
            if not duration_sec or duration_sec < MIN_SONG_DURATION or duration_sec > MAX_SONG_DURATION:
                continue

            channel_cand = data.get('uploader') or data.get('channel', '')
            title_cand = data.get('title') or ''

            # Strict heuristic check
            if not is_valid_song(title_cand, channel_name=channel_cand, duration=duration_sec, user_query=query):
                continue
            
            vid = data.get('id')
            if not vid: continue
            
            thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
            if data.get('thumbnails'):
                for t_cand in reversed(data['thumbnails']):
                    u = t_cand.get('url', '')
                    if u and ('hq720' in u or 'hqdefault' in u or 'maxresdefault' in u):
                        thumb = u
                        break

            tracks.append({
                'id': vid,
                'title': sanitize_title(title_cand),
                'raw_title': title_cand,
                'artist': channel_cand or 'Unknown Artist',
                'duration': duration_sec,
                'duration_str': format_duration(duration_sec),
                'thumbnail': thumb
            })
        SEARCH_CACHE[query] = (tracks, now)
        res = tracks[offset : offset + limit]
        prewarm_tracks([t['id'] for t in res[:4] if t.get('id')])
        return res
    except Exception as e:
        print("Search/discover error:", str(e))
        return []


def search_internal(query, offset=0, limit=12):
    return jsonify(get_search_tracks_list(query, offset=offset, limit=limit))

@app.route('/api/search')
def search():
    query = request.args.get('q')
    if not query:
        return jsonify([])
    
    query = query.strip()
    offset = int(request.args.get('offset', 0))
    limit = int(request.args.get('limit', 18))
    return jsonify(get_search_tracks_list(query, offset=offset, limit=limit))

@app.route('/api/discover')
def discover():
    category = request.args.get('category', 'top hits music')
    offset = int(request.args.get('offset', 0))
    limit = int(request.args.get('limit', 12))
    
    clean_cat = category.strip()
    if clean_cat.lower() in ('top hits music', 'all', 'top hits'):
        query = 'top hits music songs official audio'
    else:
        query = clean_cat
    return search_internal(query, offset=offset, limit=limit)

@app.route('/api/status/<video_id>', methods=['GET'])
def check_status(video_id):
    cached_file = get_cached_file(video_id)
    if cached_file:
        return jsonify({
            'id': video_id,
            'status': 'ready',
            'cached': True,
            'size': os.path.getsize(cached_file),
            'stream_warmed': True
        })

    with download_lock:
        status = download_status.get(video_id, 'not_found')

    now = time.time()
    stream_warmed = video_id in STREAM_CACHE and (now - STREAM_CACHE[video_id][1] < CACHE_TTL)

    return jsonify({
        'id': video_id,
        'status': status,
        'cached': False,
        'stream_warmed': stream_warmed
    })

@app.route('/api/stream/<video_id>')
def stream(video_id):
    # 1. If already cached on disk, serve immediately with HTTP 206 Range support (0.001s instant seek)
    cached_file = get_cached_file(video_id)
    if cached_file:
        ext = os.path.splitext(cached_file)[1].lower()
        mime_type = {
            '.opus': 'audio/ogg; codecs=opus',
            '.webm': 'audio/webm',
            '.m4a': 'audio/mp4',
            '.mp3': 'audio/mpeg'
        }.get(ext, 'application/octet-stream')
        return send_file(cached_file, mimetype=mime_type, conditional=True, as_attachment=False)

    # 2. Enqueue into background download queue to save full track to disk cache
    enqueue_background_download(video_id)

    # 3. Check fast in-memory stream URL cache first (<0.001s)
    now = time.time()
    if video_id in STREAM_CACHE:
        audio_url, cached_at = STREAM_CACHE[video_id]
        if now - cached_at < CACHE_TTL:
            return redirect(audio_url)

    # 4. Extract direct stream URL with YouTube challenge bypass and redirect immediately
    audio_url = extract_direct_url(video_id)
    if audio_url:
        STREAM_CACHE[video_id] = (audio_url, now)
        return redirect(audio_url)

    # 5. Direct extraction failed or SABR-only format: download to cache and serve directly!
    print(f"[*] Direct stream extraction fallback: downloading {video_id} directly to cache...")
    youtube_url = f"https://www.youtube.com/watch?v={video_id}"
    cached_file = download_audio(video_id, youtube_url)
    if cached_file:
        ext = os.path.splitext(cached_file)[1].lower()
        mime_type = {
            '.opus': 'audio/ogg; codecs=opus',
            '.webm': 'audio/webm',
            '.m4a': 'audio/mp4',
            '.mp3': 'audio/mpeg'
        }.get(ext, 'application/octet-stream')
        return send_file(cached_file, mimetype=mime_type, conditional=True, as_attachment=False)

    abort(404, description="Audio stream could not be extracted.")

@app.route('/api/prefetch/<video_id>', methods=['GET', 'POST'])
def prefetch_endpoint(video_id):
    """Triggers asynchronous background pre-downloading and caching of upcoming tracks."""
    if not video_id:
        return jsonify({'error': 'Missing video_id'}), 400

    # If already cached on disk, return ready immediately (0 overhead)
    if get_cached_file(video_id):
        return jsonify({'id': video_id, 'status': 'ready', 'cached': True})

    # Enqueue into background disk download queue
    enqueue_background_download(video_id)

    return jsonify({
        'id': video_id,
        'status': 'prefetching',
        'cached': False
    })

@app.route('/download')
@app.route('/install')
def desktop_info():
    return redirect('/')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    print(f"[*] 🎵 b33t — Freedom to Music (Desktop Edition)")
    print(f"[*] Starting streaming server on 0.0.0.0:{port}...")
    app.run(host='0.0.0.0', port=port, debug=False)
