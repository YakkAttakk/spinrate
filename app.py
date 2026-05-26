import os
import sqlite3
import hashlib
import secrets
import json
import urllib.request
import urllib.parse
from datetime import datetime
from flask import (Flask, request, redirect, url_for, session,
                   render_template, jsonify, abort, g)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

DB_PATH = os.path.join(os.path.dirname(__file__), 'db', 'spinrate.db')

# ---------------------------------------------------------------------------
# DB helpers — always read DATABASE_URL fresh from environment
# ---------------------------------------------------------------------------

def _db_url():
    url = os.environ.get('DATABASE_URL', '')
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql://', 1)
    return url

def _using_pg():
    return bool(os.environ.get('DATABASE_URL', ''))

def get_db():
    if 'db' not in g:
        if _using_pg():
            import psycopg2
            conn = psycopg2.connect(_db_url())
            conn.autocommit = False
            g.db = conn
            g.db_pg = True
        else:
            g.db = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA journal_mode=WAL")
            g.db.execute("PRAGMA foreign_keys=ON")
            g.db_pg = False
    return g.db

def query(sql, params=(), one=False):
    db = get_db()
    if g.get('db_pg'):
        import psycopg2.extras
        sql = sql.replace('?', '%s')
        with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return rows[0] if (one and rows) else (None if one else rows)
    else:
        cur = db.execute(sql, params)
        rows = cur.fetchall()
        return rows[0] if (one and rows) else (None if one else rows)

def execute(sql, params=()):
    db = get_db()
    if g.get('db_pg'):
        sql = sql.replace('?', '%s')
        if sql.strip().upper().startswith('INSERT') and 'RETURNING' not in sql.upper():
            sql = sql.rstrip('; ') + ' RETURNING id'
        with db.cursor() as cur:
            cur.execute(sql, params)
            if 'RETURNING' in sql.upper():
                row = cur.fetchone()
                return row[0] if row else None
        return None
    else:
        cur = db.execute(sql, params)
        return cur.lastrowid

def commit():
    get_db().commit()

@app.teardown_appcontext
def close_db(exc):
    db = g.pop('db', None)
    if db:
        if exc:
            try: db.rollback()
            except Exception: pass
        db.close()

def init_db():
    if _using_pg():
        import psycopg2
        conn = psycopg2.connect(_db_url())
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id        SERIAL PRIMARY KEY,
                username  TEXT   NOT NULL UNIQUE,
                password  TEXT   NOT NULL,
                bio       TEXT   DEFAULT '',
                avatar    TEXT   DEFAULT '',
                created   TEXT   DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS'))
            );
            CREATE TABLE IF NOT EXISTS artists (
                id           SERIAL PRIMARY KEY,
                name         TEXT   NOT NULL UNIQUE,
                mb_id        TEXT,
                wiki_url     TEXT,
                wiki_summary TEXT,
                wiki_infobox TEXT,
                image_url    TEXT,
                created      TEXT   DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS'))
            );
            CREATE TABLE IF NOT EXISTS albums (
                id           SERIAL PRIMARY KEY,
                artist_id    INTEGER NOT NULL REFERENCES artists(id),
                title        TEXT    NOT NULL,
                mb_id        TEXT,
                year         TEXT,
                cover_url    TEXT,
                genre        TEXT,
                wiki_url     TEXT,
                wiki_summary TEXT,
                wiki_infobox  TEXT,
                wiki_reception TEXT,
                created       TEXT    DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS')),
                UNIQUE(artist_id, title)
            );
            CREATE TABLE IF NOT EXISTS reviews (
                id        SERIAL PRIMARY KEY,
                user_id   INTEGER NOT NULL REFERENCES users(id),
                album_id  INTEGER NOT NULL REFERENCES albums(id),
                rating    INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
                body      TEXT    NOT NULL,
                created   TEXT    DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS')),
                UNIQUE(user_id, album_id)
            );
            CREATE TABLE IF NOT EXISTS comments (
                id         SERIAL PRIMARY KEY,
                review_id  INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
                user_id    INTEGER NOT NULL REFERENCES users(id),
                body       TEXT    NOT NULL,
                created    TEXT    DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS'))
            );
        """)
        cur.close()
        conn.close()
        print("Database initialised successfully (Postgres)")
    else:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        db = sqlite3.connect(DB_PATH)
        db.executescript("""
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS users (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                username  TEXT    NOT NULL UNIQUE COLLATE NOCASE,
                password  TEXT    NOT NULL,
                bio       TEXT    DEFAULT '',
                avatar    TEXT    DEFAULT '',
                created   TEXT    DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS artists (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
                mb_id       TEXT,
                wiki_url    TEXT,
                wiki_summary TEXT,
                wiki_infobox TEXT,
                image_url   TEXT,
                created     TEXT    DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS albums (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                artist_id   INTEGER NOT NULL REFERENCES artists(id),
                title       TEXT    NOT NULL,
                mb_id       TEXT,
                year        TEXT,
                cover_url   TEXT,
                genre       TEXT,
                wiki_url    TEXT,
                wiki_summary TEXT,
                wiki_infobox  TEXT,
                wiki_reception TEXT,
                created       TEXT    DEFAULT (datetime('now')),
                UNIQUE(artist_id, title)
            );
            CREATE TABLE IF NOT EXISTS reviews (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id),
                album_id    INTEGER NOT NULL REFERENCES albums(id),
                rating      INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
                body        TEXT    NOT NULL,
                created     TEXT    DEFAULT (datetime('now')),
                UNIQUE(user_id, album_id)
            );
            CREATE TABLE IF NOT EXISTS comments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                review_id   INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
                user_id     INTEGER NOT NULL REFERENCES users(id),
                body        TEXT    NOT NULL,
                created     TEXT    DEFAULT (datetime('now'))
            );
        """)
        db.commit()
        db.close()
        print("Database initialised successfully (SQLite)")

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def current_user():
    uid = session.get('user_id')
    if not uid:
        return None
    return query("SELECT * FROM users WHERE id=?", (uid,), one=True)

def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_id'):
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return decorated

# ---------------------------------------------------------------------------
# MusicBrainz + Cover Art + Wikipedia helpers
# ---------------------------------------------------------------------------

MB_BASE  = "https://musicbrainz.org/ws/2"
CAA_BASE = "https://coverartarchive.org"
HEADERS  = {'User-Agent': 'SpinRate/1.0 (music-social-app)'}

def mb_get(path, params=None):
    qs  = urllib.parse.urlencode({**(params or {}), 'fmt': 'json'})
    url = f"{MB_BASE}/{path}?{qs}"
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=6) as r:
            return json.loads(r.read())
    except Exception:
        return None

def _best_caa_image(images):
    """Pick the best image URL from a Cover Art Archive images list."""
    for img in images:
        if img.get('front'):
            t = img.get('thumbnails', {})
            return t.get('500') or t.get('large') or img.get('image')
    if images:
        t = images[0].get('thumbnails', {})
        return t.get('500') or t.get('large') or images[0].get('image')
    return None

def cover_art_url(mb_id):
    """Try Cover Art Archive for a specific release."""
    if not mb_id:
        return None
    try:
        req = urllib.request.Request(f"{CAA_BASE}/release/{mb_id}", headers=HEADERS)
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read())
        result = _best_caa_image(data.get('images', []))
        if result:
            return result
    except Exception:
        pass
    return None

def fetch_cover_art(mb_id, artist_name, album_title, wiki_url=None):
    """Multi-source cover art fetcher. Returns a URL or None.

    Sources tried in order:
    1. Cover Art Archive (specific release)
    2. Cover Art Archive (release group — broader search)
    3. Wikipedia page thumbnail (lead image on album article)
    4. MusicBrainz release search -> Cover Art Archive
    """
    # 1. CAA by release ID
    if mb_id:
        url = cover_art_url(mb_id)
        if url:
            return url

        # 2. CAA via release group
        try:
            rel_data = mb_get(f'release/{mb_id}', {'inc': 'release-groups'})
            rg_id = rel_data.get('release-group', {}).get('id') if rel_data else None
            if rg_id:
                req = urllib.request.Request(
                    f"{CAA_BASE}/release-group/{rg_id}", headers=HEADERS)
                with urllib.request.urlopen(req, timeout=6) as r:
                    rg_data = json.loads(r.read())
                url = _best_caa_image(rg_data.get('images', []))
                if url:
                    return url
        except Exception:
            pass

    # 3. Wikipedia page thumbnail (REST summary has a 'thumbnail' field)
    if wiki_url:
        try:
            title = wiki_url.rstrip('/').split('/wiki/')[-1]
            encoded = urllib.parse.quote(title)
            req = urllib.request.Request(
                f"https://en.wikipedia.org/api/rest_v1/page/summary/{encoded}",
                headers=HEADERS)
            with urllib.request.urlopen(req, timeout=6) as r:
                data = json.loads(r.read())
            thumb = data.get('thumbnail', {}).get('source')
            if thumb:
                # Request a larger version by bumping the pixel width in the URL
                thumb = thumb.replace('/320px-', '/500px-')
                return thumb
        except Exception:
            pass

    # 4. MusicBrainz text search -> CAA (catches albums entered manually without mb_id)
    if not mb_id and artist_name and album_title:
        try:
            qs = f'artist:"{artist_name}" AND release:"{album_title}"'
            data = mb_get('release', {'query': qs, 'limit': 3})
            for rel in (data or {}).get('releases', []):
                found_id = rel.get('id')
                if found_id:
                    url = cover_art_url(found_id)
                    if url:
                        return url
        except Exception:
            pass

    return None

def wikipedia_info(artist_name):
    """Get Wikipedia URL + summary for a music artist.

    Uses Wikipedia search API with music-specific terms to avoid
    grabbing the wrong article (e.g. Swans the animal vs the band).
    """
    music_words = ['band', 'music', 'singer', 'rapper', 'musician',
                   'album', 'record', 'rock', 'jazz', 'pop', 'artist',
                   'guitarist', 'drummer', 'songwriter', 'producer',
                   'group', 'duo', 'trio', 'ensemble']

    def fetch_summary(title):
        try:
            encoded = urllib.parse.quote(title.replace(' ', '_'))
            req = urllib.request.Request(
                f"https://en.wikipedia.org/api/rest_v1/page/summary/{encoded}",
                headers=HEADERS)
            with urllib.request.urlopen(req, timeout=6) as r:
                return json.loads(r.read())
        except Exception:
            return None

    def is_music_page(data):
        if not data or data.get('type') == 'disambiguation':
            return False
        description = data.get('description', '').lower()
        extract     = data.get('extract', '').lower()[:400]
        return any(w in description or w in extract for w in music_words)

    def extract_result(data):
        summary = data.get('extract', '')
        if len(summary) > 400:
            summary = summary[:400].rsplit(' ', 1)[0] + '…'
        wiki_url = data.get('content_urls', {}).get('desktop', {}).get('page')
        return wiki_url, summary

    # Strategy 1: Wikipedia search API with disambiguating music terms
    for search_term in [f"{artist_name} band", f"{artist_name} musician",
                        f"{artist_name} rapper", f"{artist_name} music group",
                        artist_name]:
        try:
            qs = urllib.parse.urlencode({
                'action': 'query', 'list': 'search',
                'srsearch': search_term, 'srlimit': 5,
                'format': 'json'
            })
            req = urllib.request.Request(
                f"https://en.wikipedia.org/w/api.php?{qs}", headers=HEADERS)
            with urllib.request.urlopen(req, timeout=6) as r:
                results = json.loads(r.read())
            for hit in results.get('query', {}).get('search', []):
                data = fetch_summary(hit.get('title', ''))
                if is_music_page(data):
                    return extract_result(data)
        except Exception:
            continue

    # Strategy 2: direct name lookup as last resort
    data = fetch_summary(artist_name)
    if is_music_page(data):
        return extract_result(data)

    return None, None


def album_wikipedia_info(artist_name, album_title):
    """Search Wikipedia for a specific album and return its URL + summary."""
    music_words = ['album', 'record', 'ep', 'lp', 'studio', 'soundtrack',
                   'compilation', 'single', 'release', 'discography']

    def fetch_summary(title):
        try:
            encoded = urllib.parse.quote(title.replace(' ', '_'))
            req = urllib.request.Request(
                f"https://en.wikipedia.org/api/rest_v1/page/summary/{encoded}",
                headers=HEADERS)
            with urllib.request.urlopen(req, timeout=6) as r:
                return json.loads(r.read())
        except Exception:
            return None

    def is_album_page(data):
        if not data or data.get('type') == 'disambiguation':
            return False
        description = data.get('description', '').lower()
        extract     = data.get('extract', '').lower()[:400]
        return any(w in description or w in extract for w in music_words)

    def extract_result(data):
        summary = data.get('extract', '')
        if len(summary) > 400:
            summary = summary[:400].rsplit(' ', 1)[0] + '…'
        wiki_url = data.get('content_urls', {}).get('desktop', {}).get('page')
        return wiki_url, summary

    # Search Wikipedia for the album with artist name for disambiguation
    for search_term in [
        f"{album_title} {artist_name} album",
        f"{album_title} album",
        f"{album_title} {artist_name}",
    ]:
        try:
            qs = urllib.parse.urlencode({
                'action': 'query', 'list': 'search',
                'srsearch': search_term, 'srlimit': 5,
                'format': 'json'
            })
            req = urllib.request.Request(
                f"https://en.wikipedia.org/w/api.php?{qs}", headers=HEADERS)
            with urllib.request.urlopen(req, timeout=6) as r:
                results = json.loads(r.read())
            for hit in results.get('query', {}).get('search', []):
                page_title = hit.get('title', '')
                data = fetch_summary(page_title)
                if is_album_page(data):
                    return extract_result(data)
        except Exception:
            continue

    return None, None

def fetch_infobox(wiki_url):
    """Fetch and parse the Wikipedia infobox for a given page URL.
    Returns a JSON string of [{label, value}, ...] rows, or None.
    """
    if not wiki_url:
        return None
    try:
        from html.parser import HTMLParser

        # Extract page title from URL
        title = wiki_url.rstrip('/').split('/wiki/')[-1]

        # Fetch parsed HTML via MediaWiki API
        qs = urllib.parse.urlencode({
            'action': 'parse', 'page': urllib.parse.unquote(title),
            'prop': 'text', 'section': '0', 'format': 'json',
            'disablelot': '1', 'disableeditsection': '1'
        })
        req = urllib.request.Request(
            f"https://en.wikipedia.org/w/api.php?{qs}", headers=HEADERS)
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())

        html = data.get('parse', {}).get('text', {}).get('*', '')
        if not html:
            return None

        # Parse infobox rows from HTML
        class InfoboxParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.rows = []
                self.in_infobox = False
                self.in_th = False
                self.in_td = False
                self.current_label = ''
                self.current_value = ''
                self.depth = 0
                self.infobox_depth = 0
                self.skip_depth = 0  # for nested tables

            def handle_starttag(self, tag, attrs):
                attrs_dict = dict(attrs)
                classes = attrs_dict.get('class', '')
                if tag == 'table' and ('infobox' in classes):
                    self.in_infobox = True
                    self.infobox_depth = self.depth
                if self.in_infobox:
                    if tag == 'table' and self.depth > self.infobox_depth:
                        self.skip_depth = self.depth  # nested table, skip
                    if tag == 'th':
                        self.in_th = True
                        self.current_label = ''
                    if tag == 'td':
                        self.in_td = True
                        self.current_value = ''
                    if tag in ('br', 'li'):
                        if self.in_td:
                            self.current_value += ' / '
                self.depth += 1

            def handle_endtag(self, tag):
                self.depth -= 1
                if not self.in_infobox:
                    return
                if tag == 'table' and self.depth == self.infobox_depth:
                    self.in_infobox = False
                if tag == 'th':
                    self.in_th = False
                if tag == 'td':
                    self.in_td = False
                    label = self.current_label.strip()
                    value = self.current_value.strip().strip('/ ').strip()
                    if label and value and len(value) < 300:
                        self.rows.append({'label': label, 'value': value})
                if tag == 'tr':
                    self.current_label = ''
                    self.current_value = ''

            def handle_data(self, data):
                if not self.in_infobox:
                    return
                if self.skip_depth and self.depth > self.skip_depth:
                    return
                text = data.strip()
                if not text:
                    return
                if self.in_th:
                    self.current_label += text + ' '
                elif self.in_td:
                    self.current_value += text + ' '

        parser = InfoboxParser()
        parser.feed(html)

        # Filter out useless rows (image captions, empty, coords, etc.)
        skip_labels = {'', 'background', 'label name', 'website', 'coordinates'}
        skip_prefixes = ('°', '↑', 'List of')
        rows = []
        seen_labels = set()
        for row in parser.rows:
            label = row['label'].rstrip(':').strip()
            value = row['value']
            # Clean up common artifacts
            import re
            value = re.sub(r'\s+', ' ', value).strip()
            value = re.sub(r'[.*?]', '', value).strip()  # remove [note] refs
            value = value.strip('/ ').strip()
            if not label or not value:
                continue
            if label.lower() in skip_labels:
                continue
            if any(value.startswith(p) for p in skip_prefixes):
                continue
            if label in seen_labels:
                continue
            if len(value) > 200:
                continue
            seen_labels.add(label)
            rows.append({'label': label, 'value': value})

        return json.dumps(rows) if rows else None

    except Exception:
        return None

def fetch_critical_reception(wiki_url):
    """Extract critical reception data from a Wikipedia album page.

    Returns a JSON string with:
      {
        "reviews": [{"publication": str, "score": str}, ...],
        "summary": str   # prose from the reception section
      }
    Or None if nothing found.
    """
    if not wiki_url:
        return None
    try:
        import re
        from html.parser import HTMLParser

        title = wiki_url.rstrip('/').split('/wiki/')[-1]

        # Fetch full page HTML (all sections)
        qs = urllib.parse.urlencode({
            'action': 'parse', 'page': urllib.parse.unquote(title),
            'prop': 'text', 'format': 'json',
            'disablelot': '1', 'disableeditsection': '1'
        })
        req = urllib.request.Request(
            f"https://en.wikipedia.org/w/api.php?{qs}", headers=HEADERS)
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())

        html = data.get('parse', {}).get('text', {}).get('*', '')
        if not html:
            return None

        # ----------------------------------------------------------------
        # Step 1: Extract review score table (wikitable inside reception)
        # Wikipedia encodes these as a table with columns Publication | Score
        # ----------------------------------------------------------------
        class ReceptionParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.reviews = []
                self.summary_paragraphs = []

                # State for finding the reception section
                self.in_reception = False
                self.reception_keywords = {
                    'critical reception', 'critical response',
                    'reception', 'reviews', 'critical acclaim',
                    'commercial performance and critical reception'
                }

                # State for parsing the review table
                self.in_table = False
                self.in_row = False
                self.in_cell = False
                self.cell_idx = 0
                self.current_pub = ''
                self.current_score = ''
                self.table_depth = 0
                self.depth = 0
                self.header_row = True

                # State for prose paragraphs
                self.in_para = False
                self.current_para = ''
                self.after_heading = False

            def handle_starttag(self, tag, attrs):
                attrs_dict = dict(attrs)
                classes = attrs_dict.get('class', '')

                # Detect reception heading (h2/h3 with matching text)
                if tag in ('h2', 'h3', 'h4'):
                    self.pending_heading = True
                    self.heading_text = ''

                if self.in_reception:
                    # Another h2 ends the reception section
                    if tag == 'h2':
                        self.in_reception = False

                    if tag == 'table' and ('wikitable' in classes or 'mw-collapsible' in classes):
                        self.in_table = True
                        self.table_depth = self.depth
                        self.header_row = True

                    if self.in_table:
                        if tag == 'tr':
                            self.in_row = True
                            self.cell_idx = 0
                            self.current_pub = ''
                            self.current_score = ''
                        if tag in ('td', 'th'):
                            self.in_cell = True
                        if tag == 'br':
                            if self.in_cell:
                                self.current_score += ' '

                    if tag == 'p' and not self.in_table:
                        self.in_para = True
                        self.current_para = ''
                        self.after_heading = False

                    if tag == 'br' and self.in_para:
                        self.current_para += ' '

                self.depth += 1

            def handle_endtag(self, tag):
                self.depth -= 1

                if tag in ('h2', 'h3', 'h4'):
                    heading = getattr(self, 'heading_text', '').lower().strip()
                    if any(k in heading for k in self.reception_keywords):
                        self.in_reception = True
                        self.after_heading = True
                    self.pending_heading = False

                if not self.in_reception:
                    return

                if self.in_table:
                    if tag in ('td', 'th'):
                        self.in_cell = False
                        self.cell_idx += 1
                    if tag == 'tr' and self.in_row:
                        self.in_row = False
                        if not self.header_row:
                            pub   = self.current_pub.strip()
                            score = self.current_score.strip()
                            # Clean up score: remove footnote refs like [1]
                            score = re.sub(r'[.*?]', '', score).strip()
                            score = re.sub(r'\s+', ' ', score).strip()
                            if pub and score and len(pub) < 80 and len(score) < 40:
                                self.reviews.append({
                                    'publication': pub,
                                    'score': score
                                })
                        self.header_row = False

                    if tag == 'table' and self.depth == self.table_depth:
                        self.in_table = False

                if tag == 'p' and self.in_para:
                    self.in_para = False
                    para = self.current_para.strip()
                    para = re.sub(r'[.*?]', '', para)  # strip footnote refs
                    para = re.sub(r'\s+', ' ', para).strip()
                    if para and len(para) > 80:
                        self.summary_paragraphs.append(para)

            def handle_data(self, data):
                if getattr(self, 'pending_heading', False):
                    self.heading_text = getattr(self, 'heading_text', '') + data

                if not self.in_reception:
                    return

                text = data.strip()
                if not text:
                    return

                if self.in_table and self.in_cell:
                    if self.cell_idx == 0:
                        self.current_pub   += text + ' '
                    elif self.cell_idx == 1:
                        self.current_score += text + ' '

                if self.in_para:
                    self.current_para += data

        parser = ReceptionParser()
        parser.feed(html)

        # Build prose summary from first 1-2 paragraphs
        prose = ''
        for p in parser.summary_paragraphs[:2]:
            if len(prose) + len(p) < 600:
                prose += p + ' '
        prose = prose.strip()
        if len(prose) > 550:
            prose = prose[:550].rsplit(' ', 1)[0] + '…'

        if not parser.reviews and not prose:
            return None

        result = {}
        if parser.reviews:
            result['reviews'] = parser.reviews[:15]  # cap at 15 publications
        if prose:
            result['summary'] = prose

        return json.dumps(result)

    except Exception:
        return None

# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

def _mb_search_releases(query_str, limit=8):
    """Run a MusicBrainz release search and return normalised result list."""
    data = mb_get('release', {'query': query_str, 'limit': limit})
    if not data:
        return []
    results = []
    seen = set()
    for rel in data.get('releases', []):
        mb_id  = rel.get('id')
        artist = ''
        if rel.get('artist-credit'):
            artist = rel['artist-credit'][0].get('artist', {}).get('name', '')
        title  = rel.get('title', '')
        key    = (artist.lower(), title.lower())
        if key in seen:
            continue
        seen.add(key)
        results.append({
            'mb_id':     mb_id,
            'title':     title,
            'artist':    artist,
            'year':      (rel.get('date') or '')[:4],
            'cover_url': f'/api/cover/{mb_id}' if mb_id else None,
            'score':     int(rel.get('score', 0)),
        })
    return results

@app.route('/api/search-album')
@login_required
def api_search_album():
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify([])

    seen_keys = set()
    merged = []

    def add_results(rels):
        for r in rels:
            key = (r['artist'].lower(), r['title'].lower())
            if key not in seen_keys:
                seen_keys.add(key)
                merged.append(r)

    words = q.split()

    # Strategy 1: release title search on full query
    add_results(_mb_search_releases(f'release:"{q}"'))

    # Strategy 2: if query looks like "Artist Album" (2+ words),
    # try splitting at every word boundary and search as artist + title
    if len(words) >= 2:
        for i in range(1, len(words)):
            artist_part = ' '.join(words[:i])
            title_part  = ' '.join(words[i:])
            qs = f'artist:"{artist_part}" AND release:"{title_part}"'
            add_results(_mb_search_releases(qs))
            # Also try title first, artist second (e.g. "Rumours Fleetwood Mac")
            qs2 = f'artist:"{title_part}" AND release:"{artist_part}"'
            add_results(_mb_search_releases(qs2))

    # Strategy 3: loose title search as fallback
    if not merged:
        add_results(_mb_search_releases(q))

    # Sort by score descending, cap at 8
    merged.sort(key=lambda r: r.get('score', 0), reverse=True)
    # Remove score field before returning
    for r in merged:
        r.pop('score', None)

    return jsonify(merged[:8])

@app.route('/api/cover/<mb_id>')
def api_cover(mb_id):
    url = cover_art_url(mb_id)
    if url:
        return redirect(url)
    return ('', 204)

@app.route('/api/artist-info')
@login_required
def api_artist_info():
    name = request.args.get('name', '').strip()
    if not name:
        return jsonify({})
    wiki_url, summary = wikipedia_info(name)
    return jsonify({'wiki_url': wiki_url, 'summary': summary})

# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = query("SELECT * FROM users WHERE LOWER(username)=LOWER(?)",
                     (username,), one=True)
        if user and user['password'] == hash_pw(password):
            session['user_id'] = user['id']
            return redirect(request.args.get('next') or url_for('home'))
        error = 'Invalid username or password.'
    return render_template('login.html', error=error)

@app.route('/register', methods=['GET', 'POST'])
def register():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        bio      = request.form.get('bio', '').strip()
        if not username or not password:
            error = 'Username and password are required.'
        elif len(password) < 4:
            error = 'Password must be at least 4 characters.'
        else:
            try:
                execute("INSERT INTO users (username, password, bio) VALUES (?,?,?)",
                        (username, hash_pw(password), bio))
                commit()
                user = query("SELECT * FROM users WHERE LOWER(username)=LOWER(?)",
                             (username,), one=True)
                session['user_id'] = user['id']
                return redirect(url_for('home'))
            except Exception:
                error = 'That username is already taken.'
                try: get_db().rollback()
                except Exception: pass
    return render_template('register.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ---------------------------------------------------------------------------
# Main routes
# ---------------------------------------------------------------------------

@app.route('/')
@login_required
def home():
    me      = current_user()
    reviews = query("""
        SELECT r.*, u.username, al.title as album_title, al.cover_url, al.year,
               ar.name as artist_name, ar.id as artist_id, al.id as album_id
        FROM reviews r
        JOIN users u    ON u.id  = r.user_id
        JOIN albums al  ON al.id = r.album_id
        JOIN artists ar ON ar.id = al.artist_id
        ORDER BY r.created DESC LIMIT 20
    """)
    return render_template('home.html', me=me, reviews=reviews)

@app.route('/profile/<username>')
@login_required
def profile(username):
    me   = current_user()
    user = query("SELECT * FROM users WHERE LOWER(username)=LOWER(?)",
                 (username,), one=True)
    if not user:
        abort(404)
    reviews = query("""
        SELECT r.*, al.title as album_title, al.cover_url, al.year,
               ar.name as artist_name, ar.id as artist_id, al.id as album_id
        FROM reviews r
        JOIN albums al  ON al.id = r.album_id
        JOIN artists ar ON ar.id = al.artist_id
        WHERE r.user_id = ?
        ORDER BY r.created DESC
    """, (user['id'],))
    return render_template('profile.html', me=me, user=user, reviews=reviews)

@app.route('/artist/<int:artist_id>')
@login_required
def artist(artist_id):
    me = current_user()
    a  = query("SELECT * FROM artists WHERE id=?", (artist_id,), one=True)
    if not a:
        abort(404)
    pg = _using_pg()
    albums = query("""
        SELECT al.*,
               COUNT(r.id) as review_count,
               ROUND(AVG(r.rating::numeric), 1) as avg_rating
        FROM albums al
        LEFT JOIN reviews r ON r.album_id = al.id
        WHERE al.artist_id = ?
        GROUP BY al.id
        ORDER BY al.year DESC
    """ if pg else """
        SELECT al.*,
               COUNT(r.id) as review_count,
               ROUND(AVG(r.rating), 1) as avg_rating
        FROM albums al
        LEFT JOIN reviews r ON r.album_id = al.id
        WHERE al.artist_id = ?
        GROUP BY al.id
        ORDER BY al.year DESC
    """, (artist_id,))
    # Lazily fetch and cache artist infobox on first visit
    if a['wiki_url'] and not a['wiki_infobox']:
        try:
            infobox = fetch_infobox(a['wiki_url'])
            if infobox:
                execute("UPDATE artists SET wiki_infobox=? WHERE id=?", (infobox, artist_id))
                commit()
                a = query("SELECT * FROM artists WHERE id=?", (artist_id,), one=True)
        except Exception:
            pass
    infobox = json.loads(a['wiki_infobox']) if a['wiki_infobox'] else []
    return render_template('artist.html', me=me, artist=a, albums=albums, infobox=infobox)

@app.route('/album/<int:album_id>')
@login_required
def album(album_id):
    me = current_user()
    al = query("""
        SELECT al.*, ar.name as artist_name, ar.id as artist_id,
               ar.wiki_url as ar_wiki_url, ar.wiki_summary as ar_wiki_summary
        FROM albums al JOIN artists ar ON ar.id = al.artist_id
        WHERE al.id = ?
    """, (album_id,), one=True)
    if not al:
        abort(404)

    # Lazily fetch and cache album Wikipedia info on first visit
    if not al['wiki_url'] and not al['wiki_summary']:
        try:
            awiki_url, awiki_summary = album_wikipedia_info(al['artist_name'], al['title'])
            if awiki_url or awiki_summary:
                execute("UPDATE albums SET wiki_url=?, wiki_summary=? WHERE id=?",
                        (awiki_url, awiki_summary, album_id))
                commit()
                al = query("""
                    SELECT al.*, ar.name as artist_name, ar.id as artist_id,
                           ar.wiki_url as ar_wiki_url, ar.wiki_summary as ar_wiki_summary
                    FROM albums al JOIN artists ar ON ar.id = al.artist_id
                    WHERE al.id = ?
                """, (album_id,), one=True)
        except Exception:
            pass

    # Lazily fetch and cache cover art on first visit
    if not al['cover_url']:
        try:
            art = fetch_cover_art(
                al['mb_id'], al['artist_name'], al['title'], al['wiki_url'])
            if art:
                execute("UPDATE albums SET cover_url=? WHERE id=?", (art, album_id))
                commit()
                al = query("""
                    SELECT al.*, ar.name as artist_name, ar.id as artist_id,
                           ar.wiki_url as ar_wiki_url, ar.wiki_summary as ar_wiki_summary
                    FROM albums al JOIN artists ar ON ar.id = al.artist_id
                    WHERE al.id = ?
                """, (album_id,), one=True)
        except Exception:
            pass

    # Lazily fetch and cache album infobox on first visit
    if al['wiki_url'] and not al.get('wiki_infobox'):
        try:
            infobox_json = fetch_infobox(al['wiki_url'])
            if infobox_json:
                execute("UPDATE albums SET wiki_infobox=? WHERE id=?", (infobox_json, album_id))
                commit()
                al = query("""
                    SELECT al.*, ar.name as artist_name, ar.id as artist_id,
                           ar.wiki_url as ar_wiki_url, ar.wiki_summary as ar_wiki_summary
                    FROM albums al JOIN artists ar ON ar.id = al.artist_id
                    WHERE al.id = ?
                """, (album_id,), one=True)
        except Exception:
            pass

    reviews   = query("""
        SELECT r.*, u.username
        FROM reviews r JOIN users u ON u.id = r.user_id
        WHERE r.album_id = ?
        ORDER BY r.created DESC
    """, (album_id,))
    my_review = query(
        "SELECT * FROM reviews WHERE user_id=? AND album_id=?",
        (session['user_id'], album_id), one=True)
    # Fetch all comments for this album's reviews
    comments_raw = query("""
        SELECT c.*, u.username
        FROM comments c JOIN users u ON u.id = c.user_id
        WHERE c.review_id IN (
            SELECT id FROM reviews WHERE album_id=?
        )
        ORDER BY c.created ASC
    """, (album_id,))
    # Group comments by review_id
    comments = {}
    for c in comments_raw:
        comments.setdefault(c['review_id'], []).append(c)
    infobox = json.loads(al['wiki_infobox']) if al['wiki_infobox'] else []

    # Lazily fetch and cache critical reception on first visit
    if al['wiki_url'] and not al['wiki_reception']:
        try:
            reception_json = fetch_critical_reception(al['wiki_url'])
            if reception_json:
                execute("UPDATE albums SET wiki_reception=? WHERE id=?",
                        (reception_json, album_id))
                commit()
                al = query("""
                    SELECT al.*, ar.name as artist_name, ar.id as artist_id,
                           ar.wiki_url as ar_wiki_url, ar.wiki_summary as ar_wiki_summary
                    FROM albums al JOIN artists ar ON ar.id = al.artist_id
                    WHERE al.id = ?
                """, (album_id,), one=True)
        except Exception:
            pass

    reception = json.loads(al['wiki_reception']) if al['wiki_reception'] else {}
    return render_template('album.html', me=me, album=al,
                           reviews=reviews, my_review=my_review,
                           comments=comments, infobox=infobox,
                           reception=reception)

@app.route('/new-review', methods=['GET', 'POST'])
@login_required
def new_review():
    me    = current_user()
    error = None
    if request.method == 'POST':
        artist_name  = request.form.get('artist_name',  '').strip()
        album_title  = request.form.get('album_title',  '').strip()
        mb_id        = request.form.get('mb_id',        '').strip()
        year         = request.form.get('year',         '').strip()
        cover_url    = request.form.get('cover_url',    '').strip()
        wiki_url     = request.form.get('wiki_url',     '').strip()
        wiki_summary = request.form.get('wiki_summary', '').strip()
        rating       = request.form.get('rating',       '').strip()
        body         = request.form.get('body',         '').strip()

        if not all([artist_name, album_title, rating, body]):
            error = 'Artist, album, rating, and review text are required.'
        else:
            rating = int(rating)
            existing = query(
                "SELECT id FROM artists WHERE LOWER(name)=LOWER(?)",
                (artist_name,), one=True)
            if existing:
                artist_id = existing['id']
                if wiki_url:
                    execute("UPDATE artists SET wiki_url=?, wiki_summary=? WHERE id=?",
                            (wiki_url, wiki_summary, artist_id))
            else:
                artist_id = execute(
                    "INSERT INTO artists (name, wiki_url, wiki_summary) VALUES (?,?,?)",
                    (artist_name, wiki_url or None, wiki_summary or None))

            existing_al = query(
                "SELECT id FROM albums WHERE artist_id=? AND LOWER(title)=LOWER(?)",
                (artist_id, album_title), one=True)
            if existing_al:
                album_id = existing_al['id']
            else:
                album_id = execute(
                    "INSERT INTO albums (artist_id, title, mb_id, year, cover_url) VALUES (?,?,?,?,?)",
                    (artist_id, album_title, mb_id or None, year or None, cover_url or None))

            existing_rev = query(
                "SELECT id FROM reviews WHERE user_id=? AND album_id=?",
                (me['id'], album_id), one=True)
            if existing_rev:
                execute("UPDATE reviews SET rating=?, body=?, created=? WHERE id=?",
                        (rating, body,
                         datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
                         existing_rev['id']))
            else:
                execute("INSERT INTO reviews (user_id, album_id, rating, body) VALUES (?,?,?,?)",
                        (me['id'], album_id, rating, body))
            commit()
            return redirect(url_for('album', album_id=album_id))

    return render_template('new_review.html', me=me, error=error)

@app.route('/delete-review/<int:review_id>', methods=['POST'])
@login_required
def delete_review(review_id):
    me  = current_user()
    rev = query("SELECT * FROM reviews WHERE id=?", (review_id,), one=True)
    if rev and rev['user_id'] == me['id']:
        execute("DELETE FROM reviews WHERE id=?", (review_id,))
        commit()
    return redirect(request.referrer or url_for('home'))

@app.route('/members')
@login_required
def members():
    me    = current_user()
    users = query("""
        SELECT u.*, COUNT(r.id) as review_count
        FROM users u LEFT JOIN reviews r ON r.user_id = u.id
        GROUP BY u.id ORDER BY u.username
    """)
    return render_template('members.html', me=me, users=users)

@app.route('/comment/<int:review_id>', methods=['POST'])
@login_required
def add_comment(review_id):
    me   = current_user()
    body = request.form.get('body', '').strip()
    if body:
        rev = query("SELECT album_id FROM reviews WHERE id=?", (review_id,), one=True)
        if rev:
            execute("INSERT INTO comments (review_id, user_id, body) VALUES (?,?,?)",
                    (review_id, me['id'], body))
            commit()
            return redirect(url_for('album', album_id=rev['album_id']) + f'#review-{review_id}')
    return redirect(request.referrer or url_for('home'))

@app.route('/delete-comment/<int:comment_id>', methods=['POST'])
@login_required
def delete_comment(comment_id):
    me  = current_user()
    c   = query("SELECT * FROM comments WHERE id=?", (comment_id,), one=True)
    if c and c['user_id'] == me['id']:
        execute("DELETE FROM comments WHERE id=?", (comment_id,))
        commit()
    return redirect(request.referrer or url_for('home'))

# ---------------------------------------------------------------------------
# Init DB and run
# ---------------------------------------------------------------------------

init_db()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=False)
