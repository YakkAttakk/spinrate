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
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(exc):
    db = g.pop('db', None)
    if db:
        db.close()

def init_db():
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
    """)
    db.commit()
    db.close()

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def current_user():
    uid = session.get('user_id')
    if not uid:
        return None
    return get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_id'):
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return decorated

# ---------------------------------------------------------------------------
# MusicBrainz + Cover Art helpers
# ---------------------------------------------------------------------------

MB_BASE = "https://musicbrainz.org/ws/2"
CAA_BASE = "https://coverartarchive.org"
HEADERS = {'User-Agent': 'SpinRate/1.0 (music-social-app)'}

def mb_get(path, params=None):
    qs = urllib.parse.urlencode({**(params or {}), 'fmt': 'json'})
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
    url = f"{CAA_BASE}/release/{mb_id}"
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read())
        images = data.get('images', [])
        for img in images:
            if img.get('front'):
                thumbs = img.get('thumbnails', {})
                return thumbs.get('500') or thumbs.get('large') or img.get('image')
        if images:
            thumbs = images[0].get('thumbnails', {})
            return thumbs.get('500') or thumbs.get('large') or images[0].get('image')
    except Exception:
        pass
    return None

def wikipedia_info(artist_name):
    title = urllib.parse.quote(artist_name.replace(' ', '_'))
    url = (f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}")
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read())
        if data.get('type') == 'disambiguation':
            return None, None
        summary = data.get('extract', '')
        if len(summary) > 400:
            summary = summary[:400].rsplit(' ', 1)[0] + '…'
        wiki_url = data.get('content_urls', {}).get('desktop', {}).get('page')
        return wiki_url, summary
    except Exception:
        return None, None

# ---------------------------------------------------------------------------
# API: album/artist search (called from JS)
# ---------------------------------------------------------------------------

@app.route('/api/search-album')
@login_required
def api_search_album():
    q = request.args.get('q', '').strip()
    artist_q = request.args.get('artist', '').strip()
    if not q:
        return jsonify([])
    query = f'"{q}"' if artist_q else q
    if artist_q:
        query += f' AND artist:"{artist_q}"'
    data = mb_get('release', {'query': query, 'limit': 8})
    if not data:
        return jsonify([])
    results = []
    for rel in data.get('releases', []):
        mb_id = rel.get('id')
        artist = ''
        if rel.get('artist-credit'):
            artist = rel['artist-credit'][0].get('artist', {}).get('name', '')
        results.append({
            'mb_id': mb_id,
            'title': rel.get('title', ''),
            'artist': artist,
            'year': (rel.get('date') or '')[:4],
            'cover_url': f'/api/cover/{mb_id}' if mb_id else None,
        })
    return jsonify(results)

@app.route('/api/cover/<mb_id>')
def api_cover(mb_id):
    url = cover_art_url(mb_id)
    if url:
        return redirect(url)
    return redirect('/static/no-cover.svg')

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
        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE username=?", (username,)).fetchone()
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
        bio = request.form.get('bio', '').strip()
        if not username or not password:
            error = 'Username and password are required.'
        elif len(password) < 4:
            error = 'Password must be at least 4 characters.'
        else:
            try:
                get_db().execute(
                    "INSERT INTO users (username, password, bio) VALUES (?,?,?)",
                    (username, hash_pw(password), bio))
                get_db().commit()
                user = get_db().execute(
                    "SELECT * FROM users WHERE username=?", (username,)).fetchone()
                session['user_id'] = user['id']
                return redirect(url_for('home'))
            except sqlite3.IntegrityError:
                error = 'That username is already taken.'
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
    db = get_db()
    me = current_user()
    # Recent reviews from all users
    reviews = db.execute("""
        SELECT r.*, u.username, al.title as album_title, al.cover_url, al.year,
               ar.name as artist_name, ar.id as artist_id, al.id as album_id
        FROM reviews r
        JOIN users u ON u.id = r.user_id
        JOIN albums al ON al.id = r.album_id
        JOIN artists ar ON ar.id = al.artist_id
        ORDER BY r.created DESC LIMIT 20
    """).fetchall()
    return render_template('home.html', me=me, reviews=reviews)

@app.route('/profile/<username>')
@login_required
def profile(username):
    db = get_db()
    me = current_user()
    user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not user:
        abort(404)
    reviews = db.execute("""
        SELECT r.*, al.title as album_title, al.cover_url, al.year,
               ar.name as artist_name, ar.id as artist_id, al.id as album_id
        FROM reviews r
        JOIN albums al ON al.id = r.album_id
        JOIN artists ar ON ar.id = al.artist_id
        WHERE r.user_id = ?
        ORDER BY r.created DESC
    """, (user['id'],)).fetchall()
    return render_template('profile.html', me=me, user=user, reviews=reviews)

@app.route('/artist/<int:artist_id>')
@login_required
def artist(artist_id):
    db = get_db()
    me = current_user()
    a = db.execute("SELECT * FROM artists WHERE id=?", (artist_id,)).fetchone()
    if not a:
        abort(404)
    albums = db.execute("""
        SELECT al.*,
               COUNT(r.id) as review_count,
               ROUND(AVG(r.rating), 1) as avg_rating
        FROM albums al
        LEFT JOIN reviews r ON r.album_id = al.id
        WHERE al.artist_id = ?
        GROUP BY al.id
        ORDER BY al.year DESC
    """, (artist_id,)).fetchall()
    return render_template('artist.html', me=me, artist=a, albums=albums)

@app.route('/album/<int:album_id>')
@login_required
def album(album_id):
    db = get_db()
    me = current_user()
    al = db.execute("""
        SELECT al.*, ar.name as artist_name, ar.id as artist_id,
               ar.wiki_url, ar.wiki_summary
        FROM albums al JOIN artists ar ON ar.id = al.artist_id
        WHERE al.id = ?
    """, (album_id,)).fetchone()
    if not al:
        abort(404)
    reviews = db.execute("""
        SELECT r.*, u.username
        FROM reviews r JOIN users u ON u.id = r.user_id
        WHERE r.album_id = ?
        ORDER BY r.created DESC
    """, (album_id,)).fetchall()
    my_review = db.execute(
        "SELECT * FROM reviews WHERE user_id=? AND album_id=?",
        (me['id'], album_id)).fetchone()
    return render_template('album.html', me=me, album=al,
                           reviews=reviews, my_review=my_review)

@app.route('/new-review', methods=['GET', 'POST'])
@login_required
def new_review():
    db = get_db()
    me = current_user()
    error = None
    if request.method == 'POST':
        artist_name = request.form.get('artist_name', '').strip()
        album_title = request.form.get('album_title', '').strip()
        mb_id       = request.form.get('mb_id', '').strip()
        year        = request.form.get('year', '').strip()
        cover_url   = request.form.get('cover_url', '').strip()
        wiki_url    = request.form.get('wiki_url', '').strip()
        wiki_summary= request.form.get('wiki_summary', '').strip()
        rating      = request.form.get('rating', '').strip()
        body        = request.form.get('body', '').strip()

        if not all([artist_name, album_title, rating, body]):
            error = 'Artist, album, rating, and review text are required.'
        else:
            rating = int(rating)
            # Upsert artist
            existing = db.execute(
                "SELECT id FROM artists WHERE name=?", (artist_name,)).fetchone()
            if existing:
                artist_id = existing['id']
                if wiki_url:
                    db.execute(
                        "UPDATE artists SET wiki_url=?, wiki_summary=? WHERE id=?",
                        (wiki_url, wiki_summary, artist_id))
            else:
                cur = db.execute(
                    "INSERT INTO artists (name, wiki_url, wiki_summary) VALUES (?,?,?)",
                    (artist_name, wiki_url or None, wiki_summary or None))
                artist_id = cur.lastrowid

            # Upsert album
            existing_al = db.execute(
                "SELECT id FROM albums WHERE artist_id=? AND title=?",
                (artist_id, album_title)).fetchone()
            if existing_al:
                album_id = existing_al['id']
            else:
                cur = db.execute(
                    "INSERT INTO albums (artist_id, title, mb_id, year, cover_url) VALUES (?,?,?,?,?)",
                    (artist_id, album_title, mb_id or None, year or None, cover_url or None))
                album_id = cur.lastrowid

            # Insert or replace review
            try:
                db.execute(
                    "INSERT INTO reviews (user_id, album_id, rating, body) VALUES (?,?,?,?)",
                    (me['id'], album_id, rating, body))
            except sqlite3.IntegrityError:
                db.execute(
                    "UPDATE reviews SET rating=?, body=?, created=datetime('now') WHERE user_id=? AND album_id=?",
                    (rating, body, me['id'], album_id))
            db.commit()
            return redirect(url_for('album', album_id=album_id))

    return render_template('new_review.html', me=me, error=error)

@app.route('/delete-review/<int:review_id>', methods=['POST'])
@login_required
def delete_review(review_id):
    db = get_db()
    me = current_user()
    rev = db.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
    if rev and rev['user_id'] == me['id']:
        db.execute("DELETE FROM reviews WHERE id=?", (review_id,))
        db.commit()
    return redirect(request.referrer or url_for('home'))

@app.route('/members')
@login_required
def members():
    db = get_db()
    me = current_user()
    users = db.execute("""
        SELECT u.*, COUNT(r.id) as review_count
        FROM users u LEFT JOIN reviews r ON r.user_id = u.id
        GROUP BY u.id ORDER BY u.username
    """).fetchall()
    return render_template('members.html', me=me, users=users)

# ---------------------------------------------------------------------------

# Initialise DB on startup (runs whether gunicorn or direct)
init_db()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=False)
