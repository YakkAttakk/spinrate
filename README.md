# SpinRate 🎵

A private music review platform for friends.

## Stack
- **Backend**: Python 3 + Flask
- **Database**: SQLite (file: `db/spinrate.db`)
- **Music data**: MusicBrainz API + Cover Art Archive (no API key needed)
- **Artist info**: Wikipedia REST API (auto-fetched when writing a review)

## Setup

```bash
# 1. Install dependencies
pip install flask

# 2. Start the server
python run.py
```

Then open **http://localhost:5000** in your browser.

The database is created automatically on first run at `db/spinrate.db`.

## Running in production

For a small private group, [Railway](https://railway.app), [Render](https://render.com), or a cheap VPS all work great.

```bash
# Example with gunicorn
pip install gunicorn
gunicorn run:app -b 0.0.0.0:5000
```

Set the `SECRET_KEY` environment variable to a long random string in production:

```bash
export SECRET_KEY="your-long-random-secret-here"
```

## iOS / React Native

The app is structured as a standard web app — wrapping it in a React Native `WebView` pointing at your deployed URL is the fastest path to an iOS app. All interactions use standard HTTP forms and JSON APIs, so a native shell with `WKWebView` / Expo WebBrowser works immediately.

For a fully native app later, the `/api/` routes return JSON and can back a native UI.

## Features
- ✅ Private (login required to see anything)
- ✅ Register with username + password (no email needed)
- ✅ Write reviews with 1–5 star ratings
- ✅ MusicBrainz album search with auto-fill
- ✅ Album artwork via Cover Art Archive
- ✅ Wikipedia artist summary auto-fetched
- ✅ Artist pages showing all reviewed albums
- ✅ Album pages with all friend reviews + avg rating
- ✅ Personal profile pages with review history
- ✅ Members page listing the whole crew
- ✅ Global feed of recent activity

## File structure
```
spinrate/
├── app.py          # All routes + DB logic
├── run.py          # Entry point
├── requirements.txt
├── db/             # SQLite database (auto-created)
└── templates/
    ├── base.html
    ├── login.html
    ├── register.html
    ├── home.html
    ├── new_review.html
    ├── profile.html
    ├── artist.html
    ├── album.html
    └── members.html
```
