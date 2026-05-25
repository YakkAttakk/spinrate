#!/usr/bin/env python3
"""Entry point — initialises DB then starts Flask."""
from app import app, init_db

if __name__ == '__main__':
    init_db()
    print("\n🎵  SpinRate is running at http://localhost:5000\n")
    app.run(host='0.0.0.0', port=5001, debug=False)
