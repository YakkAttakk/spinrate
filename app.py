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
                created      TEXT    DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS')),
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
                created     TEXT    DEFAULT (datetime('now')),
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

def cover_art_url(mb_id):
    if not mb_id:
        return None
    try:
        req = urllib.request.Request(f"{CAA_BASE}/release/{mb_id}", headers=HEADERS)
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read())
        images = data.get('images', [])
        for img in images:
            if img.get('front'):
                t = img.get('thumbnails', {})
                return t.get('500') or t.get('large') or img.get('image')
        if images:
            t = images[0].get('thumbnails', {})
            return t.get('500') or t.get('large') or images[0].get('image')
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

# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.route('/api/search-album')
@login_required
def api_search_album():
    q        = request.args.get('q', '').strip()
    artist_q = request.args.get('artist', '').strip()
    if not q:
        return jsonify([])
    query_str = f'"{q}"' if artist_q else q
    if artist_q:
        query_str += f' AND artist:"{artist_q}"'
    data = mb_get('release', {'query': query_str, 'limit': 8})
    if not data:
        return jsonify([])
    results = []
    for rel in data.get('releases', []):
        mb_id  = rel.get('id')
        artist = ''
        if rel.get('artist-credit'):
            artist = rel['artist-credit'][0].get('artist', {}).get('name', '')
        results.append({
            'mb_id':     mb_id,
            'title':     rel.get('title', ''),
            'artist':    artist,
            'year':      (rel.get('date') or '')[:4],
            'cover_url': f'/api/cover/{mb_id}' if mb_id else None,
        })
    return jsonify(results)

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
    return render_template('artist.html', me=me, artist=a, albums=albums)

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
                # Re-fetch album with updated wiki info
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
    return render_template('album.html', me=me, album=al,
                           reviews=reviews, my_review=my_review,
                           comments=comments)

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
