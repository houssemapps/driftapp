from flask import Flask, request, jsonify, send_from_directory
import os
import sqlite3
from datetime import datetime

app = Flask(__name__)
DB  = 'drift.db'

# ─── DATABASE ────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row   # rows behave like dicts
    return conn

def init_db():
    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS cards (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            flag       TEXT    NOT NULL DEFAULT '🌍',
            text       TEXT    NOT NULL,
            likes      INTEGER NOT NULL DEFAULT 0,
            created_at TEXT    NOT NULL
        )
    ''')
    conn.commit()
    conn.close()
    print('✦ database ready — drift.db created')

# ─── ROUTES ──────────────────────────────────────────────────────────────────

# Serve the HTML app — Flask acts as both frontend and backend server
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

# GET /cards → return all cards as JSON (newest first, max 300)
@app.route('/cards', methods=['GET'])
def get_cards():
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM cards ORDER BY created_at DESC LIMIT 300'
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

# POST /cards → receive a new card, validate it, save to database
@app.route('/cards', methods=['POST'])
def post_card():
    data = request.get_json() or {}
    text = (data.get('text') or '').strip()
    flag = data.get('flag', '🌍')

    # ── Basic validation ─────────────────────────────────────────────────────
    if not text:
        return jsonify({'error': 'empty text'}), 400
    if len(text) > 220:
        return jsonify({'error': 'too long'}), 400

    # ── Keyword filter (expand this list later) ──────────────────────────────
    BAD_WORDS = ['spam', 'click here', 'buy now']
    if any(w in text.lower() for w in BAD_WORDS):
        return jsonify({'error': 'blocked'}), 400

    # ── Save to database ─────────────────────────────────────────────────────
    conn = get_db()
    cur  = conn.execute(
        'INSERT INTO cards (flag, text, created_at) VALUES (?, ?, ?)',
        (flag, text, datetime.now().isoformat())
    )
    conn.commit()
    # Return the saved card with its real database id
    card = dict(conn.execute(
        'SELECT * FROM cards WHERE id = ?', (cur.lastrowid,)
    ).fetchone())
    conn.close()

    print(f'✦ new card [{card["id"]}] {flag} "{text[:40]}..."')
    return jsonify(card), 201

# POST /cards/<id>/like → add one like to a card
@app.route('/cards/<int:cid>/like', methods=['POST'])
def like_card(cid):
    conn = get_db()
    conn.execute('UPDATE cards SET likes = likes + 1 WHERE id = ?', (cid,))
    conn.commit()
    card = conn.execute('SELECT * FROM cards WHERE id = ?', (cid,)).fetchone()
    conn.close()
    return jsonify(dict(card))

# POST /flag → receive a report (log it for now, store later)
@app.route('/flag', methods=['POST'])
def flag_card():
    data = request.get_json() or {}
    print(f'⚑  flagged card: {data}')
    return jsonify({'ok': True})

@app.route('/manifest.json')
def manifest():
    return send_from_directory('.', 'manifest.json')

@app.route('/.well-known/assetlinks.json')
def assetlinks():
    return send_from_directory('.well-known', 'assetlinks.json')

@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory('static', filename)

# ─── START ───────────────────────────────────────────────────────────────────

init_db()              # ← now runs always, gunicorn OR python

if __name__ == '__main__':
    print('─' * 40)
    print('✦ drift is running')
    print('✦ http://localhost:5000')
    print('─' * 40)
    app.run(debug=True, port=5000)
