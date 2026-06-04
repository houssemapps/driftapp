from flask import Flask, request, jsonify, send_from_directory
import os
import json
import urllib.request
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
from collections import defaultdict
import time

app = Flask(__name__)

# ─── RATE LIMITER ─────────────────────────────────────────────────────────────
POST_LOG = defaultdict(list)

def is_rate_limited(ip):
    now   = time.time()
    times = [t for t in POST_LOG[ip] if now - t < 3600]
    POST_LOG[ip] = times
    if len(times) >= 5:
        return True
    POST_LOG[ip].append(now)
    return False

# ─── DATABASE ─────────────────────────────────────────────────────────────────
def get_db():
    # Pulls the database string directly from the Environment variables you set up in Render
    db_url = os.environ.get('DATABASE_URL')
    conn = psycopg2.connect(db_url)
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    # Converted to PostgreSQL compatible datatypes (SERIAL instead of AUTOINCREMENT)
    cur.execute('''
        CREATE TABLE IF NOT EXISTS cards (
            id         SERIAL PRIMARY KEY,
            flag       TEXT    NOT NULL DEFAULT '🌍',
            text       TEXT    NOT NULL,
            likes      INTEGER NOT NULL DEFAULT 0,
            score      REAL    NOT NULL DEFAULT 0,
            created_at TEXT    NOT NULL
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS comments (
            id         SERIAL PRIMARY KEY,
            card_id    INTEGER NOT NULL,
            flag       TEXT    NOT NULL DEFAULT '🌍',
            text       TEXT    NOT NULL,
            created_at TEXT    NOT NULL
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS reports (
            id         SERIAL PRIMARY KEY,
            card_id    INTEGER NOT NULL,
            reason     TEXT,
            created_at TEXT    NOT NULL
        )
    ''')
    conn.commit()
    cur.close()
    conn.close()
    print('✦ Supabase database ready')

# Run on startup — initializes your tables inside Supabase automatically if they don't exist
init_db()

# ─── STATIC FILES ─────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/manifest.json')
def manifest():
    return send_from_directory('.', 'manifest.json')

@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory('static', filename)

@app.route('/.well-known/assetlinks.json')
def assetlinks():
    return send_from_directory('.well-known', 'assetlinks.json')

# ─── CARDS ────────────────────────────────────────────────────────────────────

# GET /cards?mode=new|hot|mixed
@app.route('/cards', methods=['GET'])
def get_cards():
    mode = request.args.get('mode', 'mixed')
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    if mode == 'new':
        cur.execute('SELECT * FROM cards ORDER BY created_at DESC LIMIT 80')
        rows = cur.fetchall()

    elif mode == 'hot':
        cur.execute('SELECT * FROM cards ORDER BY score DESC LIMIT 80')
        rows = cur.fetchall()

    else:  # mixed: newest + most liked, no duplicates
        cur.execute('SELECT * FROM cards ORDER BY created_at DESC LIMIT 55')
        recent = cur.fetchall()
        cur.execute('SELECT * FROM cards ORDER BY score DESC LIMIT 25')
        popular = cur.fetchall()
        
        seen, rows = set(), []
        for r in recent + popular:
            if r['id'] not in seen:
                seen.add(r['id'])
                rows.append(r)

    cur.close()
    conn.close()
    return jsonify([dict(r) for r in rows])


# GET /cards/new?since=TIMESTAMP — real-time polling
@app.route('/cards/new')
def get_new_cards():
    since_ms = int(request.args.get('since', 0))
    since_dt = datetime.fromtimestamp(since_ms / 1000).isoformat()
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        'SELECT * FROM cards WHERE created_at > %s ORDER BY created_at ASC',
        (since_dt,)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(r) for r in rows])


# GET /cards/batch?ids=1,2,3 — fetch specific cards
@app.route('/cards/batch')
def get_cards_batch():
    ids_str = request.args.get('ids', '')
    if not ids_str:
        return jsonify([])
    ids = [int(i) for i in ids_str.split(',') if i.strip().isdigit()]
    if not ids:
        return jsonify([])
    placeholders = ','.join('%s' for _ in ids)
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        f'SELECT * FROM cards WHERE id IN ({placeholders})', ids
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(r) for r in rows])


# POST /cards — save a new card
@app.route('/cards', methods=['POST'])
def post_card():
    data = request.get_json() or {}
    text = (data.get('text') or '').strip()
    flag = data.get('flag', '🌍')

    if not text:
        return jsonify({'error': 'empty text'}), 400
    if len(text) > 220:
        return jsonify({'error': 'too long'}), 400

    BAD_WORDS = [
        'spam', 'click here', 'buy now', 'whatsapp', 'telegram',
        'follow me', 'subscribe', 'onlyfans', 'casino', 'bet now',
        'free money', 'discount code', 'promo', 'investment opportunity'
    ]
    if any(w in text.lower() for w in BAD_WORDS):
        return jsonify({'error': 'blocked'}), 400

    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if is_rate_limited(ip):
        return jsonify({'error': 'slow down'}), 429

    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    # Using PostgreSQL RETURNING syntax to instantly capture our inserted card row data
    cur.execute(
        'INSERT INTO cards (flag, text, created_at) VALUES (%s, %s, %s) RETURNING *',
        (flag, text, datetime.now().isoformat())
    )
    card = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()

    print(f'✦ new card [{card["id"]}] {flag} "{text[:40]}..."')
    return jsonify(dict(card)), 201


# POST /cards/<id>/like
@app.route('/cards/<int:cid>/like', methods=['POST'])
def like_card(cid):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute('UPDATE cards SET likes = likes + 1 WHERE id = %s', (cid,))
    # Converted SQLite's julianday over to PostgreSQL Native Extract Epoch math
    cur.execute('''
        UPDATE cards SET
        score = (likes * 3.0) / ((EXTRACT(EPOCH FROM (NOW() - created_at::timestamp)) / 3600.0) + 2)
        WHERE id = %s
    ''', (cid,))
    conn.commit()
    cur.execute('SELECT * FROM cards WHERE id = %s', (cid,))
    card = cur.fetchone()
    cur.close()
    conn.close()
    return jsonify(dict(card))

# ─── COMMENTS ─────────────────────────────────────────────────────────────────

# GET /cards/<id>/comments
@app.route('/cards/<int:cid>/comments', methods=['GET'])
def get_comments(cid):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        'SELECT * FROM comments WHERE card_id = %s ORDER BY created_at ASC',
        (cid,)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(r) for r in rows])


# POST /cards/<id>/comments
@app.route('/cards/<int:cid>/comments', methods=['POST'])
def post_comment(cid):
    data = request.get_json() or {}
    text = (data.get('text') or '').strip()
    flag = data.get('flag', '🌍')

    if not text or len(text) > 200:
        return jsonify({'error': 'invalid'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        'INSERT INTO comments (card_id, flag, text, created_at) VALUES (%s, %s, %s, %s)',
        (cid, flag, text, datetime.now().isoformat())
    )
    cur.execute('SELECT COUNT(*) FROM comments WHERE card_id = %s', (cid,))
    count = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()

    print(f'✦ comment on card {cid}: "{text[:30]}..."')
    return jsonify({'ok': True, 'count': count}), 201

# ─── REPORTS ──────────────────────────────────────────────────────────────────

@app.route('/flag', methods=['POST'])
def flag_card():
    data    = request.get_json() or {}
    card_id = data.get('id')
    reason  = data.get('reason', '')

    if card_id:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            'INSERT INTO reports (card_id, reason, created_at) VALUES (%s,%s,%s)',
            (card_id, reason, datetime.now().isoformat())
        )
        cur.execute('SELECT COUNT(*) FROM reports WHERE card_id = %s', (card_id,))
        count = cur.fetchone()[0]
        if count >= 3:
            cur.execute('DELETE FROM cards WHERE id = %s', (card_id,))
        conn.commit()
        if count >= 3:
            print(f'⚑ card {card_id} auto-removed after {count} reports')
        cur.close()
        conn.close()

    return jsonify({'ok': True})

# ─── GEOIP (proxy to ip-api.com — avoids mixed-content on HTTPS) ─────────────

@app.route('/geoip')
def geoip():
    raw_ip = request.headers.get('X-Forwarded-For', request.remote_addr) or ''
    ip = raw_ip.split(',')[0].strip()
    # strip port for plain IPv4 (e.g. "1.2.3.4:12345" → "1.2.3.4")
    if ip and not ip.startswith('[') and ':' in ip:
        ip = ip.rsplit(':', 1)[0]
    flag = '🌍'
    try:
        url = f'http://ip-api.com/json/{ip}?fields=status,countryCode'
        req = urllib.request.Request(url, headers={'User-Agent': 'drift-app/1.0'})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read())
        code = data.get('countryCode', '') if data.get('status') == 'success' else ''
        if len(code) == 2:
            # country code (e.g. "DZ") → flag emoji (e.g. 🇩🇿)
            flag = ''.join(chr(0x1F1E6 + ord(c) - 65) for c in code.upper())
    except Exception as e:
        print(f'geoip error: {e}')
    return jsonify({'flag': flag})


# ─── ECHO ─────────────────────────────────────────────────────────────────────

@app.route('/echo', methods=['POST'])
def echo():
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity

        data        = request.get_json() or {}
        source_text = data.get('text', '')

        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('SELECT id, text FROM cards')
        rows = cur.fetchall()
        cur.close()
        conn.close()

        if len(rows) < 2:
            return jsonify([])

        texts = [source_text] + [r['text'] for r in rows]
        ids   = [None]        + [r['id']   for r in rows]

        vec    = TfidfVectorizer(stop_words='english', min_df=1)
        matrix = vec.fit_transform(texts)
        scores = cosine_similarity(matrix[0:1], matrix[1:])[0]

        results = [
            {'id': str(ids[i + 1]), 'score': float(scores[i])}
            for i in range(len(scores))
            if scores[i] > 0.08
        ]
        results.sort(key=lambda x: -x['score'])
        return jsonify(results[:15])

    except Exception as e:
        print(f'echo error: {e}')
        return jsonify([])

# ─── ADMIN ────────────────────────────────────────────────────────────────────

ADMIN_KEY = 'drift2024admin'   # change this to something secret

@app.route('/admin')
def admin():
    if request.args.get('key') != ADMIN_KEY:
        return '403', 403
    conn   = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute('SELECT * FROM cards ORDER BY created_at DESC')
    cards  = cur.fetchall()
    cur.execute(
        'SELECT r.*, c.text as card_text FROM reports r '
        'LEFT JOIN cards c ON r.card_id = c.id '
        'ORDER BY r.created_at DESC LIMIT 50'
    )
    reports = cur.fetchall()
    cur.close()
    conn.close()

    rows = ''.join([
        f'<tr><td>{c["id"]}</td><td>{c["flag"]}</td>'
        f'<td style="max-width:300px">{c["text"][:80]}</td>'
        f'<td>{c["likes"]}</td>'
        f'<td><a href="/admin/delete/{c["id"]}?key={ADMIN_KEY}" '
        f'onclick="return confirm(\'delete?\')">✕</a></td></tr>'
        for c in cards
    ])
    report_rows = ''.join([
        f'<tr><td>{r["card_id"]}</td><td>{r.get("card_text","?")[:60]}</td>'
        f'<td>{r["reason"]}</td><td>{r["created_at"][:16]}</td></tr>'
        for r in reports
    ])
    return f'''
    <html><head><style>
      body{{font-family:monospace;background:#0a0a18;color:#aaa;padding:20px}}
      table{{border-collapse:collapse;width:100%;margin-bottom:40px}}
      th,td{{border:1px solid #222;padding:8px;text-align:left;font-size:12px}}
      th{{color:#5a7aff}}a{{color:#ff6b8a;text-decoration:none}}
    </style></head><body>
    <h2 style="color:#5a7aff">✦ Drift Admin</h2>
    <p style="color:#444">{len(cards)} cards total</p>
    <table><tr><th>id</th><th>flag</th><th>text</th><th>likes</th><th>del</th></tr>
    {rows}</table>
    <h3 style="color:#5a7aff">Recent Reports</h3>
    <table><tr><th>card_id</th><th>text</th><th>reason</th><th>time</th></tr>
    {report_rows}</table>
    </body></html>'''


@app.route('/admin/delete/<int:cid>')
def admin_delete(cid):
    if request.args.get('key') != ADMIN_KEY:
        return '403', 403
    conn = get_db()
    cur = conn.cursor()
    cur.execute('DELETE FROM cards WHERE id = %s', (cid,))
    conn.commit()
    cur.close()
    conn.close()
    return f'<script>history.back()</script>'

# ─── START ────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print('─' * 40)
    print('✦ drift is running on cloud postgres')
    print('─' * 40)
    app.run(debug=True, port=5000)