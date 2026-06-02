# -*- coding: utf-8 -*-
# ╔══════════════════════════════════════════════════════╗
# ║       ZYROX ULTRA PRO MAX HOSTING BOT                ║
# ║  Firebase Auto-Restart + Ultra Auto-Install          ║
# ║  Process Watchdog + Crash Recovery                   ║
# ╚══════════════════════════════════════════════════════╝

import telebot
import subprocess
import os
import zipfile
import tempfile
import shutil
from telebot import types
import time
from datetime import datetime, timedelta
import psutil
import sqlite3
import json
import logging
import threading
import re
import sys
import atexit
import requests
from flask import Flask
from threading import Thread

# ════════════════════════════════════════════
#   FLASK KEEP-ALIVE
# ════════════════════════════════════════════
flask_app = Flask('')

@flask_app.route('/')
def home():
    return "🚀 ZYROX Ultra Hosting Bot - ONLINE"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host='0.0.0.0', port=port, threaded=True, use_reloader=False)

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()
    print("✅ Flask Keep-Alive started.")

# ════════════════════════════════════════════
#   CONFIGURATION  (set via Render Environment Variables)
# ════════════════════════════════════════════
TOKEN          = os.environ.get('TOKEN', '')
OWNER_ID       = int(os.environ.get('OWNER_ID', 0))
ADMIN_ID       = int(os.environ.get('ADMIN_ID', 0))
YOUR_USERNAME  = os.environ.get('YOUR_USERNAME', '@patelkrish9')
UPDATE_CHANNEL = os.environ.get('UPDATE_CHANNEL', 't.me/kpbotmaker')

# Firebase Realtime DB URL
FIREBASE_DB_URL = os.environ.get('FIREBASE_DB_URL', 'https://webhostingbot-default-rtdb.firebaseio.com')

# Dirs — use /tmp for Render (only writable path at runtime)
BASE_DIR         = os.path.abspath(os.path.dirname(__file__))
UPLOAD_BOTS_DIR  = '/tmp/upload_bots'
DATA_DIR         = '/tmp/inf'
DATABASE_PATH    = os.path.join(DATA_DIR, 'bot_data.db')

# File limits
FREE_USER_LIMIT       = 999
SUBSCRIBED_USER_LIMIT = 999
ADMIN_LIMIT           = 999
OWNER_LIMIT           = float('inf')

# Watchdog interval (seconds)
WATCHDOG_INTERVAL = 15

os.makedirs(UPLOAD_BOTS_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

bot = telebot.TeleBot(TOKEN)

# ════════════════════════════════════════════
#   IN-MEMORY STATE
# ════════════════════════════════════════════
bot_scripts        = {}   # {script_key: info_dict}
user_subscriptions = {}   # {user_id: {'expiry': datetime}}
user_files         = {}   # {user_id: [(file_name, file_type), ...]}
active_users       = set()
admin_ids          = {ADMIN_ID, OWNER_ID}
bot_locked         = False

# ════════════════════════════════════════════
#   LOGGING
# ════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

# ════════════════════════════════════════════
#   FIREBASE REST API WRAPPER
# ════════════════════════════════════════════
class FirebaseDB:
    """
    Thin wrapper around Firebase Realtime Database REST API.
    No SDK required — pure requests.
    """
    def __init__(self, db_url: str):
        self.base = db_url.rstrip('/')

    def _url(self, path: str) -> str:
        return f"{self.base}/{path}.json"

    def get(self, path: str):
        try:
            r = requests.get(self._url(path), timeout=10)
            return r.json() if r.status_code == 200 else None
        except Exception as e:
            logger.error(f"[Firebase GET] {path}: {e}")
            return None

    def set(self, path: str, data: dict) -> bool:
        try:
            r = requests.put(self._url(path), json=data, timeout=10)
            return r.status_code == 200
        except Exception as e:
            logger.error(f"[Firebase SET] {path}: {e}")
            return False

    def update(self, path: str, data: dict) -> bool:
        try:
            r = requests.patch(self._url(path), json=data, timeout=10)
            return r.status_code == 200
        except Exception as e:
            logger.error(f"[Firebase UPDATE] {path}: {e}")
            return False

    def delete(self, path: str) -> bool:
        try:
            r = requests.delete(self._url(path), timeout=10)
            return r.status_code == 200
        except Exception as e:
            logger.error(f"[Firebase DELETE] {path}: {e}")
            return False

    # ── Process helpers ─────────────────────

    @staticmethod
    def _key(user_id: int, file_name: str) -> str:
        """Safe Firebase key (no . $ # [ ] /)"""
        safe = re.sub(r'[.$#\[\]/]', '_', file_name)
        return f"{user_id}__{safe}"

    def register_process(self, user_id: int, file_name: str,
                         file_type: str, chat_id: int) -> bool:
        """Mark process as intentionally running → auto_restart = True."""
        key = self._key(user_id, file_name)
        return self.set(f"processes/{key}", {
            "user_id":       user_id,
            "file_name":     file_name,
            "file_type":     file_type,
            "chat_id":       chat_id,
            "status":        "running",
            "auto_restart":  True,
            "start_time":    datetime.now().isoformat(),
            "restart_count": 0,
            "last_crash":    None,
        })

    def mark_stopped(self, user_id: int, file_name: str) -> bool:
        """Admin/user intentionally stopped → auto_restart = False."""
        key = self._key(user_id, file_name)
        return self.update(f"processes/{key}", {
            "status":       "stopped",
            "auto_restart": False,
            "stop_time":    datetime.now().isoformat(),
        })

    def mark_deleted(self, user_id: int, file_name: str) -> bool:
        """File deleted → remove from Firebase entirely."""
        key = self._key(user_id, file_name)
        return self.delete(f"processes/{key}")

    def mark_crashed(self, user_id: int, file_name: str,
                     restart_count: int) -> bool:
        key = self._key(user_id, file_name)
        return self.update(f"processes/{key}", {
            "status":        "crashed",
            "auto_restart":  True,
            "last_crash":    datetime.now().isoformat(),
            "restart_count": restart_count,
        })

    def mark_restarting(self, user_id: int, file_name: str,
                        restart_count: int) -> bool:
        key = self._key(user_id, file_name)
        return self.update(f"processes/{key}", {
            "status":        "running",
            "auto_restart":  True,
            "start_time":    datetime.now().isoformat(),
            "restart_count": restart_count,
        })

    def all_processes(self) -> dict:
        data = self.get("processes")
        return data if isinstance(data, dict) else {}


firebase = FirebaseDB(FIREBASE_DB_URL)

# ════════════════════════════════════════════
#   ULTRA AUTO-INSTALL  (100 000+ packages)
# ════════════════════════════════════════════

# Core stdlib modules — NEVER install these
PYTHON_STDLIB = {
    'asyncio','json','datetime','os','sys','re','time','math','random',
    'logging','threading','subprocess','zipfile','tempfile','shutil',
    'sqlite3','atexit','signal','pathlib','io','gc','abc','ast','base64',
    'binascii','calendar','cgi','cgitb','chunk','cmath','cmd','code',
    'codecs','codeop','colorsys','compileall','concurrent','configparser',
    'contextlib','contextvars','copy','copyreg','csv','ctypes','curses',
    'dataclasses','dbm','decimal','difflib','dis','doctest','email',
    'encodings','enum','errno','faulthandler','fcntl','filecmp','fileinput',
    'fnmatch','fractions','ftplib','functools','getopt','getpass','gettext',
    'glob','gzip','hashlib','heapq','hmac','html','http','imaplib',
    'importlib','inspect','ipaddress','itertools','keyword','linecache',
    'locale','lzma','mailbox','marshal','mimetypes','mmap','multiprocessing',
    'netrc','numbers','operator','optparse','pickle','pickletools','pkgutil',
    'platform','plistlib','poplib','pprint','profile','pstats','pty',
    'pwd','py_compile','pydoc','queue','readline','reprlib','resource',
    'rlcompleter','runpy','sched','secrets','select','selectors','shelve',
    'site','smtplib','socket','socketserver','ssl','stat','statistics',
    'string','struct','symtable','sysconfig','syslog','tabnanny','tarfile',
    'telnetlib','test','textwrap','token','tokenize','trace','traceback',
    'tracemalloc','types','typing','unicodedata','unittest','urllib','uuid',
    'venv','warnings','wave','weakref','webbrowser','wsgiref','xdrlib',
    'xml','xmlrpc','zipapp','zipimport','zlib','zoneinfo','_thread',
    'builtins','collections','struct','typing_extensions','tkinter',
}

# Known import-name → PyPI-package-name mapping
KNOWN_PACKAGES = {
    # ── Telegram Frameworks ──────────────────────────
    'telebot':                  'pyTelegramBotAPI',
    'telegram':                 'python-telegram-bot',
    'aiogram':                  'aiogram',
    'pyrogram':                 'pyrogram',
    'telethon':                 'telethon',
    'hydrogram':                'hydrogram',
    'tgcrypto':                 'tgcrypto',
    # ── Web Frameworks ───────────────────────────────
    'flask':                    'Flask',
    'django':                   'Django',
    'fastapi':                  'fastapi',
    'aiohttp':                  'aiohttp',
    'tornado':                  'tornado',
    'sanic':                    'sanic',
    'bottle':                   'bottle',
    'falcon':                   'falcon',
    'starlette':                'starlette',
    'uvicorn':                  'uvicorn',
    'gunicorn':                 'gunicorn',
    'quart':                    'quart',
    'litestar':                 'litestar',
    # ── Data Science / ML ────────────────────────────
    'numpy':                    'numpy',
    'np':                       'numpy',
    'pandas':                   'pandas',
    'pd':                       'pandas',
    'matplotlib':               'matplotlib',
    'seaborn':                  'seaborn',
    'sklearn':                  'scikit-learn',
    'scipy':                    'scipy',
    'tensorflow':               'tensorflow',
    'tf':                       'tensorflow',
    'torch':                    'torch',
    'torchvision':              'torchvision',
    'cv2':                      'opencv-python',
    'PIL':                      'Pillow',
    'pillow':                   'Pillow',
    'imageio':                  'imageio',
    'skimage':                  'scikit-image',
    'statsmodels':              'statsmodels',
    'xgboost':                  'xgboost',
    'lightgbm':                 'lightgbm',
    'catboost':                 'catboost',
    'keras':                    'keras',
    'nltk':                     'nltk',
    'spacy':                    'spacy',
    'transformers':             'transformers',
    'gensim':                   'gensim',
    'plotly':                   'plotly',
    'bokeh':                    'bokeh',
    'altair':                   'altair',
    'dash':                     'dash',
    'streamlit':                'streamlit',
    'gradio':                   'gradio',
    'huggingface_hub':          'huggingface-hub',
    # ── Database ─────────────────────────────────────
    'sqlalchemy':               'SQLAlchemy',
    'pymongo':                  'pymongo',
    'motor':                    'motor',
    'redis':                    'redis',
    'aioredis':                 'aioredis',
    'psycopg2':                 'psycopg2-binary',
    'psycopg':                  'psycopg',
    'pymysql':                  'PyMySQL',
    'aiomysql':                 'aiomysql',
    'aiosqlite':                'aiosqlite',
    'databases':                'databases',
    'tortoise':                 'tortoise-orm',
    'peewee':                   'peewee',
    'tinydb':                   'tinydb',
    'elasticsearch':            'elasticsearch',
    'firebase_admin':           'firebase-admin',
    'pyrebase':                 'pyrebase4',
    # ── HTTP / Networking ─────────────────────────────
    'requests':                 'requests',
    'httpx':                    'httpx',
    'urllib3':                  'urllib3',
    'websockets':               'websockets',
    'socketio':                 'python-socketio',
    'websocket':                'websocket-client',
    'pydantic':                 'pydantic',
    'certifi':                  'certifi',
    'chardet':                  'chardet',
    'charset_normalizer':       'charset-normalizer',
    'idna':                     'idna',
    # ── Parsing / Utils ───────────────────────────────
    'bs4':                      'beautifulsoup4',
    'lxml':                     'lxml',
    'yaml':                     'PyYAML',
    'toml':                     'toml',
    'dotenv':                   'python-dotenv',
    'dateutil':                 'python-dateutil',
    'arrow':                    'arrow',
    'pendulum':                 'pendulum',
    'humanize':                 'humanize',
    'rich':                     'rich',
    'click':                    'click',
    'typer':                    'typer',
    'colorama':                 'colorama',
    'tqdm':                     'tqdm',
    'loguru':                   'loguru',
    'tabulate':                 'tabulate',
    'prettytable':              'prettytable',
    'termcolor':                'termcolor',
    'pyfiglet':                 'pyfiglet',
    'art':                      'art',
    'psutil':                   'psutil',
    # ── Crypto / Security ─────────────────────────────
    'cryptography':             'cryptography',
    'Crypto':                   'pycryptodome',
    'nacl':                     'PyNaCl',
    'jwt':                      'PyJWT',
    'bcrypt':                   'bcrypt',
    'passlib':                  'passlib',
    'paramiko':                 'paramiko',
    'pyotp':                    'pyotp',
    'rsa':                      'rsa',
    'OpenSSL':                  'pyOpenSSL',
    # ── Cloud / Storage ───────────────────────────────
    'boto3':                    'boto3',
    'botocore':                 'botocore',
    'googleapiclient':          'google-api-python-client',
    'dropbox':                  'dropbox',
    'cloudinary':               'cloudinary',
    'minio':                    'minio',
    # ── Media ─────────────────────────────────────────
    'ffmpeg':                   'ffmpeg-python',
    'pydub':                    'pydub',
    'moviepy':                  'moviepy',
    'mutagen':                  'mutagen',
    'PyPDF2':                   'PyPDF2',
    'pypdf':                    'pypdf',
    'pdfplumber':               'pdfplumber',
    'docx':                     'python-docx',
    'openpyxl':                 'openpyxl',
    'qrcode':                   'qrcode',
    'pdf2image':                'pdf2image',
    'pytesseract':              'pytesseract',
    'easyocr':                  'easyocr',
    # ── Scraping / Browser ────────────────────────────
    'selenium':                 'selenium',
    'playwright':               'playwright',
    'scrapy':                   'scrapy',
    'pyppeteer':                'pyppeteer',
    'parsel':                   'parsel',
    # ── Async ─────────────────────────────────────────
    'anyio':                    'anyio',
    'trio':                     'trio',
    'gevent':                   'gevent',
    'eventlet':                 'eventlet',
    'celery':                   'celery',
    'apscheduler':              'APScheduler',
    'schedule':                 'schedule',
    # ── Social / Media APIs ───────────────────────────
    'tweepy':                   'tweepy',
    'instagrapi':               'instagrapi',
    'discord':                  'discord.py',
    'nextcord':                 'nextcord',
    'disnake':                  'disnake',
    'slack_sdk':                'slack-sdk',
    'vk_api':                   'vk-api',
    'vkbottle':                 'vkbottle',
    'yt_dlp':                   'yt-dlp',
    'youtube_dl':               'youtube-dl',
    'pytube':                   'pytube',
    'pytubefix':                'pytubefix',
    # ── Payment ───────────────────────────────────────
    'stripe':                   'stripe',
    'razorpay':                 'razorpay',
    'web3':                     'web3',
    # ── Testing ───────────────────────────────────────
    'pytest':                   'pytest',
    'faker':                    'Faker',
    'hypothesis':               'hypothesis',
}


def resolve_package(module_name: str):
    """
    Returns (candidates_list, is_core).
    candidates_list: list of package names to try in order.
    is_core: True → stdlib, don't install.
    """
    base = module_name.split('.')[0]
    base_lower = base.lower()

    # Stdlib check
    if base_lower in PYTHON_STDLIB or module_name.lower() in PYTHON_STDLIB:
        return [], True

    # Known map (exact → lower fallback)
    for key in (module_name, base, base_lower):
        if key in KNOWN_PACKAGES:
            pkg = KNOWN_PACKAGES[key]
            if pkg is None:
                return [], True   # explicitly marked as core
            return [pkg], False

    # Universal fallback: try multiple name variants
    candidates = []
    for name in (module_name, base):
        candidates.append(name)
        if '_' in name:
            candidates.append(name.replace('_', '-'))
    # Deduplicate while preserving order
    seen = set()
    unique = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique, False


def attempt_install_pip(module_name: str, message) -> bool:
    """Try to pip-install a missing Python module. Sends status to user."""
    candidates, is_core = resolve_package(module_name)
    if is_core:
        logger.info(f"Stdlib module '{module_name}' — skipping install.")
        return False
    if not candidates:
        bot.reply_to(message, f"⚠️ Cannot resolve package for `{module_name}`.", parse_mode='Markdown')
        return False

    for pkg in candidates:
        try:
            bot.reply_to(message, f"🔧 Installing `{pkg}` for `{module_name}`...", parse_mode='Markdown')
            cmd = [sys.executable, '-m', 'pip', 'install', pkg, '--quiet']
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    check=False, encoding='utf-8', errors='ignore', timeout=120)
            if result.returncode == 0:
                bot.reply_to(message, f"✅ `{pkg}` installed!", parse_mode='Markdown')
                return True
            else:
                logger.warning(f"pip install {pkg} failed: {result.stderr[:200]}")
        except subprocess.TimeoutExpired:
            bot.reply_to(message, f"⏳ Timeout installing `{pkg}`. Trying next...")
        except Exception as e:
            logger.error(f"pip install error for {pkg}: {e}")

    bot.reply_to(message,
                 f"❌ All install attempts failed for `{module_name}`.\nTried: `{', '.join(candidates)}`",
                 parse_mode='Markdown')
    return False


def attempt_install_npm(module_name: str, user_folder: str, message) -> bool:
    """Try to npm-install a missing Node module."""
    try:
        bot.reply_to(message, f"🟠 Installing npm `{module_name}`...", parse_mode='Markdown')
        result = subprocess.run(
            ['npm', 'install', module_name, '--save'],
            capture_output=True, text=True, check=False,
            cwd=user_folder, encoding='utf-8', errors='ignore', timeout=120
        )
        if result.returncode == 0:
            bot.reply_to(message, f"✅ npm `{module_name}` installed!", parse_mode='Markdown')
            return True
        err = (result.stderr or result.stdout)[:400]
        bot.reply_to(message, f"❌ npm install failed:\n```\n{err}\n```", parse_mode='Markdown')
        return False
    except FileNotFoundError:
        bot.reply_to(message, "❌ `npm` not found — install Node.js first!")
        return False
    except subprocess.TimeoutExpired:
        bot.reply_to(message, f"⏳ Timeout installing npm `{module_name}`.")
        return False
    except Exception as e:
        bot.reply_to(message, f"❌ npm error: {e}")
        return False

# ════════════════════════════════════════════
#   SQLITE DATABASE
# ════════════════════════════════════════════
DB_LOCK = threading.Lock()

def init_db():
    try:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS subscriptions
                     (user_id INTEGER PRIMARY KEY, expiry TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS user_files
                     (user_id INTEGER, file_name TEXT, file_type TEXT,
                      PRIMARY KEY (user_id, file_name))''')
        c.execute('''CREATE TABLE IF NOT EXISTS active_users
                     (user_id INTEGER PRIMARY KEY)''')
        c.execute('''CREATE TABLE IF NOT EXISTS admins
                     (user_id INTEGER PRIMARY KEY)''')
        c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (OWNER_ID,))
        if ADMIN_ID != OWNER_ID:
            c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (ADMIN_ID,))
        conn.commit()
        conn.close()
        logger.info("✅ SQLite DB initialized.")
    except Exception as e:
        logger.error(f"DB init error: {e}", exc_info=True)

def load_data():
    try:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute('SELECT user_id, expiry FROM subscriptions')
        for uid, exp in c.fetchall():
            try: user_subscriptions[uid] = {'expiry': datetime.fromisoformat(exp)}
            except ValueError: pass
        c.execute('SELECT user_id, file_name, file_type FROM user_files')
        for uid, fn, ft in c.fetchall():
            user_files.setdefault(uid, []).append((fn, ft))
        c.execute('SELECT user_id FROM active_users')
        active_users.update(uid for (uid,) in c.fetchall())
        c.execute('SELECT user_id FROM admins')
        admin_ids.update(uid for (uid,) in c.fetchall())
        conn.close()
        logger.info(f"✅ Loaded {len(active_users)} users, {len(user_subscriptions)} subs.")
    except Exception as e:
        logger.error(f"DB load error: {e}", exc_info=True)

init_db()
load_data()

# ── DB write helpers ────────────────────────
def _db(fn):
    """Wrap a DB operation with the global lock."""
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        try: fn(conn)
        except Exception as e: logger.error(f"DB error: {e}")
        finally: conn.close()

def save_user_file(user_id, file_name, file_type='py'):
    def op(conn):
        conn.execute('INSERT OR REPLACE INTO user_files VALUES (?,?,?)',
                     (user_id, file_name, file_type))
        conn.commit()
        user_files.setdefault(user_id, [])
        user_files[user_id] = [(fn,ft) for fn,ft in user_files[user_id] if fn != file_name]
        user_files[user_id].append((file_name, file_type))
    _db(op)

def remove_user_file_db(user_id, file_name):
    def op(conn):
        conn.execute('DELETE FROM user_files WHERE user_id=? AND file_name=?', (user_id, file_name))
        conn.commit()
        if user_id in user_files:
            user_files[user_id] = [f for f in user_files[user_id] if f[0] != file_name]
            if not user_files[user_id]: del user_files[user_id]
    _db(op)

def add_active_user(user_id):
    active_users.add(user_id)
    def op(conn):
        conn.execute('INSERT OR IGNORE INTO active_users VALUES (?)', (user_id,))
        conn.commit()
    _db(op)

def save_subscription(user_id, expiry):
    def op(conn):
        conn.execute('INSERT OR REPLACE INTO subscriptions VALUES (?,?)',
                     (user_id, expiry.isoformat()))
        conn.commit()
        user_subscriptions[user_id] = {'expiry': expiry}
    _db(op)

def remove_subscription_db(user_id):
    def op(conn):
        conn.execute('DELETE FROM subscriptions WHERE user_id=?', (user_id,))
        conn.commit()
        user_subscriptions.pop(user_id, None)
    _db(op)

def add_admin_db(admin_id):
    def op(conn):
        conn.execute('INSERT OR IGNORE INTO admins VALUES (?)', (admin_id,))
        conn.commit()
        admin_ids.add(admin_id)
    _db(op)

def remove_admin_db(admin_id):
    try:
        with DB_LOCK:
            conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
            conn.execute('DELETE FROM admins WHERE user_id=?', (admin_id,))
            conn.commit()
            conn.close()
        admin_ids.discard(admin_id)
        return True
    except Exception as e:
        logger.error(f"remove_admin_db error: {e}")
        return False

# ════════════════════════════════════════════
#   HELPER FUNCTIONS
# ════════════════════════════════════════════
def get_user_folder(user_id):
    folder = os.path.join(UPLOAD_BOTS_DIR, str(user_id))
    os.makedirs(folder, exist_ok=True)
    return folder

def get_user_file_limit(user_id):
    if user_id == OWNER_ID: return OWNER_LIMIT
    if user_id in admin_ids: return ADMIN_LIMIT
    sub = user_subscriptions.get(user_id, {})
    if sub.get('expiry') and sub['expiry'] > datetime.now():
        return SUBSCRIBED_USER_LIMIT
    return FREE_USER_LIMIT

def get_user_file_count(user_id):
    return len(user_files.get(user_id, []))

def _close_log(info: dict):
    lf = info.get('log_file')
    if lf and hasattr(lf, 'close') and not lf.closed:
        try: lf.close()
        except Exception: pass

def is_bot_running(owner_id: int, file_name: str) -> bool:
    key = f"{owner_id}_{file_name}"
    info = bot_scripts.get(key)
    if not info or not info.get('process'):
        return False
    try:
        proc = psutil.Process(info['process'].pid)
        alive = proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
        if not alive:
            _close_log(info)
            bot_scripts.pop(key, None)
        return alive
    except psutil.NoSuchProcess:
        _close_log(info)
        bot_scripts.pop(key, None)
        return False
    except Exception:
        return False

def kill_process_tree(info: dict):
    key = info.get('script_key', '?')
    _close_log(info)
    proc = info.get('process')
    if not proc or not hasattr(proc, 'pid'):
        return
    pid = proc.pid
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for ch in children:
            try: ch.terminate()
            except psutil.NoSuchProcess: pass
        gone, alive = psutil.wait_procs(children, timeout=2)
        for p in alive:
            try: p.kill()
            except Exception: pass
        try:
            parent.terminate()
            try: parent.wait(timeout=2)
            except psutil.TimeoutExpired: parent.kill()
        except psutil.NoSuchProcess: pass
    except psutil.NoSuchProcess:
        pass
    except Exception as e:
        logger.error(f"kill_process_tree error ({key}): {e}")

# ════════════════════════════════════════════
#   CORE SCRIPT RUNNER
# ════════════════════════════════════════════
def _start_process(script_path, owner_id, user_folder, file_name,
                   file_type, chat_id, is_auto=False):
    """
    Spawns the subprocess and registers it in bot_scripts + Firebase.
    Returns the subprocess.Popen object or None on failure.
    """
    key = f"{owner_id}_{file_name}"
    log_path = os.path.join(user_folder, f"{os.path.splitext(file_name)[0]}.log")
    try:
        mode = 'a' if is_auto else 'w'
        log_file = open(log_path, mode, encoding='utf-8', errors='ignore')
    except Exception as e:
        logger.error(f"Cannot open log file {log_path}: {e}")
        return None

    try:
        si = None
        if os.name == 'nt':
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = subprocess.SW_HIDE

        cmd = ([sys.executable, script_path] if file_type == 'py'
               else ['node', script_path])
        process = subprocess.Popen(
            cmd, cwd=user_folder,
            stdout=log_file, stderr=log_file,
            stdin=subprocess.PIPE,
            startupinfo=si,
            encoding='utf-8', errors='ignore',
        )
        bot_scripts[key] = {
            'process':       process,
            'log_file':      log_file,
            'file_name':     file_name,
            'chat_id':       chat_id,
            'script_owner_id': owner_id,
            'start_time':    datetime.now(),
            'user_folder':   user_folder,
            'type':          file_type,
            'script_key':    key,
            'auto_restart':  True,
        }
        firebase.register_process(owner_id, file_name, file_type, chat_id)
        logger.info(f"✅ Started {file_type} PID={process.pid}  key={key}")
        return process
    except FileNotFoundError:
        log_file.close()
        runtime = 'Python' if file_type == 'py' else 'Node.js'
        logger.error(f"{runtime} interpreter not found for {key}")
        return None
    except Exception as e:
        log_file.close()
        logger.error(f"_start_process error ({key}): {e}")
        return None


def run_script(script_path, owner_id, user_folder, file_name,
               msg_obj, attempt=1, is_auto=False):
    """Run Python file with multi-pass auto-install on ModuleNotFoundError."""
    MAX = 5
    key = f"{owner_id}_{file_name}"
    if attempt > MAX:
        if not is_auto:
            bot.reply_to(msg_obj, f"❌ '{file_name}' failed after {MAX} attempts.")
        return
    if not os.path.exists(script_path):
        if not is_auto:
            bot.reply_to(msg_obj, f"❌ File '{file_name}' not found on disk!")
        remove_user_file_db(owner_id, file_name)
        firebase.mark_deleted(owner_id, file_name)
        return

    # ── Pre-check pass ───────────────────────
    if attempt == 1:
        check_proc = None
        try:
            check_proc = subprocess.Popen(
                [sys.executable, script_path], cwd=user_folder,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8', errors='ignore',
            )
            _, stderr = check_proc.communicate(timeout=6)
            rc = check_proc.returncode
            if rc != 0 and stderr:
                missing = re.findall(r"No module named '(.+?)'", stderr)
                if missing:
                    installed = False
                    for mod in missing:
                        mod = mod.strip().strip("'\"")
                        if attempt_install_pip(mod, msg_obj):
                            installed = True
                    if installed:
                        if not is_auto:
                            bot.reply_to(msg_obj, f"🔄 Modules installed. Retrying '{file_name}'...")
                        time.sleep(2)
                        threading.Thread(
                            target=run_script,
                            args=(script_path, owner_id, user_folder,
                                  file_name, msg_obj, attempt + 1, is_auto),
                            daemon=True
                        ).start()
                        return
                    else:
                        if not is_auto:
                            bot.reply_to(msg_obj, f"❌ Install failed. Cannot run '{file_name}'.")
                        return
                else:
                    if not is_auto:
                        bot.reply_to(msg_obj,
                                     f"❌ Script error:\n```\n{stderr[:500]}\n```",
                                     parse_mode='Markdown')
                    return
        except subprocess.TimeoutExpired:
            logger.info(f"Pre-check timeout for {key} (normal).")
        except FileNotFoundError:
            if not is_auto:
                bot.reply_to(msg_obj, "❌ Python interpreter not found!")
            return
        except Exception as e:
            logger.error(f"Pre-check error {key}: {e}")
        finally:
            if check_proc and check_proc.poll() is None:
                check_proc.kill(); check_proc.communicate()

    # ── Launch ───────────────────────────────
    chat_id = msg_obj.chat.id
    proc = _start_process(script_path, owner_id, user_folder,
                          file_name, 'py', chat_id, is_auto)
    if proc:
        if not is_auto:
            bot.reply_to(msg_obj,
                         f"✅ `{file_name}` started! PID: `{proc.pid}`",
                         parse_mode='Markdown')
    else:
        if not is_auto:
            bot.reply_to(msg_obj, f"❌ Failed to start '{file_name}'.")


def run_js_script(script_path, owner_id, user_folder, file_name,
                  msg_obj, attempt=1, is_auto=False):
    """Run JS file with npm auto-install on Cannot find module."""
    MAX = 5
    key = f"{owner_id}_{file_name}"
    if attempt > MAX:
        if not is_auto:
            bot.reply_to(msg_obj, f"❌ '{file_name}' failed after {MAX} attempts.")
        return
    if not os.path.exists(script_path):
        if not is_auto:
            bot.reply_to(msg_obj, f"❌ File '{file_name}' not found!")
        remove_user_file_db(owner_id, file_name)
        firebase.mark_deleted(owner_id, file_name)
        return

    if attempt == 1:
        check_proc = None
        try:
            check_proc = subprocess.Popen(
                ['node', script_path], cwd=user_folder,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8', errors='ignore',
            )
            _, stderr = check_proc.communicate(timeout=6)
            rc = check_proc.returncode
            if rc != 0 and stderr:
                m = re.search(r"Cannot find module '(.+?)'", stderr)
                if m:
                    mod = m.group(1).strip().strip("'\"")
                    if not mod.startswith('.') and not mod.startswith('/'):
                        if attempt_install_npm(mod, user_folder, msg_obj):
                            if not is_auto:
                                bot.reply_to(msg_obj, f"🔄 npm installed. Retrying...")
                            time.sleep(2)
                            threading.Thread(
                                target=run_js_script,
                                args=(script_path, owner_id, user_folder,
                                      file_name, msg_obj, attempt + 1, is_auto),
                                daemon=True
                            ).start()
                            return
                        else:
                            if not is_auto:
                                bot.reply_to(msg_obj, "❌ npm install failed.")
                            return
                else:
                    if not is_auto:
                        bot.reply_to(msg_obj,
                                     f"❌ Script error:\n```\n{stderr[:500]}\n```",
                                     parse_mode='Markdown')
                    return
        except subprocess.TimeoutExpired:
            pass
        except FileNotFoundError:
            if not is_auto:
                bot.reply_to(msg_obj, "❌ `node` not found — install Node.js!")
            return
        except Exception as e:
            logger.error(f"JS pre-check error {key}: {e}")
        finally:
            if check_proc and check_proc.poll() is None:
                check_proc.kill(); check_proc.communicate()

    chat_id = msg_obj.chat.id
    proc = _start_process(script_path, owner_id, user_folder,
                          file_name, 'js', chat_id, is_auto)
    if proc:
        if not is_auto:
            bot.reply_to(msg_obj,
                         f"✅ JS `{file_name}` started! PID: `{proc.pid}`",
                         parse_mode='Markdown')
    else:
        if not is_auto:
            bot.reply_to(msg_obj, f"❌ Failed to start '{file_name}'.")

# ════════════════════════════════════════════
#   WATCHDOG THREAD
# ════════════════════════════════════════════
class _DummyMsg:
    """Fake message object so runner functions can send Telegram replies."""
    def __init__(self, chat_id, user_id):
        class _Chat: id = chat_id
        class _User: id = user_id; first_name = "AutoRestart"
        self.chat = _Chat()
        self.from_user = _User()


def _watchdog_tick():
    """
    One cycle:
    • Read Firebase for all processes with auto_restart=True.
    • For each one not currently running → restart it.
    • If disk file missing → remove from Firebase.
    """
    all_procs = firebase.all_processes()
    if not all_procs:
        return

    for _, pdata in all_procs.items():
        if not isinstance(pdata, dict): continue
        if not pdata.get('auto_restart'): continue
        if pdata.get('status') not in ('running', 'crashed'): continue

        uid      = pdata.get('user_id')
        fname    = pdata.get('file_name')
        ftype    = pdata.get('file_type', 'py')
        chat_id  = pdata.get('chat_id', OWNER_ID)

        if not uid or not fname: continue
        if is_bot_running(uid, fname): continue  # still alive — ok

        # Dead process that should be alive
        rc = pdata.get('restart_count', 0) + 1
        logger.warning(f"🐕 Watchdog: '{fname}' (user {uid}) crashed. Restart #{rc}")

        folder = get_user_folder(uid)
        fpath  = os.path.join(folder, fname)
        if not os.path.exists(fpath):
            logger.error(f"Watchdog: '{fpath}' missing — removing from Firebase.")
            firebase.mark_deleted(uid, fname)
            continue

        firebase.mark_crashed(uid, fname, rc)

        # Notify
        try:
            bot.send_message(
                OWNER_ID,
                f"⚠️ *Auto-Restart*\n📄 `{fname}`\n👤 `{uid}`\n🔄 Restart #{rc}\n⏱️ {datetime.now():%H:%M:%S}",
                parse_mode='Markdown'
            )
            if chat_id and chat_id != OWNER_ID:
                try:
                    bot.send_message(chat_id,
                                     f"🔄 Auto-restarting `{fname}` (#{rc})...",
                                     parse_mode='Markdown')
                except Exception: pass
        except Exception: pass

        dummy = _DummyMsg(chat_id, uid)
        time.sleep(3)

        if ftype == 'py':
            threading.Thread(target=run_script,
                             args=(fpath, uid, folder, fname, dummy, 1, True),
                             daemon=True).start()
        elif ftype == 'js':
            threading.Thread(target=run_js_script,
                             args=(fpath, uid, folder, fname, dummy, 1, True),
                             daemon=True).start()

        firebase.mark_restarting(uid, fname, rc)
        time.sleep(2)


def watchdog_loop():
    logger.info("🐕 Watchdog started.")
    time.sleep(30)
    while True:
        try:
            _watchdog_tick()
        except Exception as e:
            logger.error(f"Watchdog error: {e}", exc_info=True)
        time.sleep(WATCHDOG_INTERVAL)


def restore_from_firebase():
    """On startup: relaunch everything Firebase says should be running."""
    logger.info("🔁 Restoring processes from Firebase…")
    try:
        all_procs = firebase.all_processes()
        if not all_procs:
            logger.info("Firebase: nothing to restore.")
            return
        count = 0
        for _, pdata in all_procs.items():
            if not isinstance(pdata, dict): continue
            if not pdata.get('auto_restart'): continue

            uid     = pdata.get('user_id')
            fname   = pdata.get('file_name')
            ftype   = pdata.get('file_type', 'py')
            chat_id = pdata.get('chat_id', OWNER_ID)

            if not uid or not fname: continue
            if is_bot_running(uid, fname): continue

            folder = get_user_folder(uid)
            fpath  = os.path.join(folder, fname)
            if not os.path.exists(fpath):
                firebase.mark_deleted(uid, fname)
                continue

            logger.info(f"  → Restoring '{fname}' for user {uid}")
            dummy = _DummyMsg(chat_id, uid)
            if ftype == 'py':
                threading.Thread(target=run_script,
                                 args=(fpath, uid, folder, fname, dummy, 1, True),
                                 daemon=True).start()
            elif ftype == 'js':
                threading.Thread(target=run_js_script,
                                 args=(fpath, uid, folder, fname, dummy, 1, True),
                                 daemon=True).start()
            count += 1
            time.sleep(1)

        logger.info(f"✅ Restored {count} process(es).")
        if count:
            try:
                bot.send_message(
                    OWNER_ID,
                    f"🔁 *Bot Restarted!*\n✅ Restored `{count}` process(es) from Firebase.",
                    parse_mode='Markdown'
                )
            except Exception: pass
    except Exception as e:
        logger.error(f"restore_from_firebase error: {e}", exc_info=True)

# ════════════════════════════════════════════
#   UI BUILDERS
# ════════════════════════════════════════════
def create_reply_keyboard(user_id):
    is_admin = user_id in admin_ids
    layout = (
        [["📢 Updates Channel"],
         ["📤 Upload File", "📂 Check Files"],
         ["⚡ Bot Speed", "📊 Statistics"],
         ["💳 Subscriptions", "📢 Broadcast"],
         ["🔒 Lock Bot", "🟢 Running All Code"],
         ["👑 Admin Panel", "📞 Contact Owner"]]
        if is_admin else
        [["📢 Updates Channel"],
         ["📤 Upload File", "📂 Check Files"],
         ["⚡ Bot Speed", "📊 Statistics"],
         ["📞 Contact Owner"]]
    )
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for row in layout:
        markup.add(*[types.KeyboardButton(b) for b in row])
    return markup

def create_main_inline(user_id):
    m = types.InlineKeyboardMarkup(row_width=2)
    m.add(
        types.InlineKeyboardButton("📤 Upload",   callback_data='upload_file'),
        types.InlineKeyboardButton("📂 My Files", callback_data='check_files'),
    )
    m.add(
        types.InlineKeyboardButton("⚡ Speed", callback_data='speed'),
        types.InlineKeyboardButton("📊 Stats", callback_data='stats'),
    )
    if user_id in admin_ids:
        m.add(
            types.InlineKeyboardButton("💳 Subs",       callback_data='subscription_management'),
            types.InlineKeyboardButton("👑 Admin Panel", callback_data='admin_panel'),
        )
        m.add(
            types.InlineKeyboardButton("🔒 Lock",   callback_data='lock_bot'),
            types.InlineKeyboardButton("🔓 Unlock", callback_data='unlock_bot'),
        )
        m.add(types.InlineKeyboardButton("🟢 Run All Scripts", callback_data='run_all_scripts'))
    return m

def create_control_buttons(owner_id, file_name, running):
    m = types.InlineKeyboardMarkup(row_width=2)
    if running:
        m.add(
            types.InlineKeyboardButton("🔴 Stop",    callback_data=f'stop_{owner_id}_{file_name}'),
            types.InlineKeyboardButton("🔄 Restart", callback_data=f'restart_{owner_id}_{file_name}'),
        )
    else:
        m.add(types.InlineKeyboardButton("▶️ Start", callback_data=f'start_{owner_id}_{file_name}'))
    m.add(
        types.InlineKeyboardButton("📜 Logs",   callback_data=f'logs_{owner_id}_{file_name}'),
        types.InlineKeyboardButton("🗑️ Delete", callback_data=f'delete_{owner_id}_{file_name}'),
    )
    m.add(types.InlineKeyboardButton("🔙 Back", callback_data='check_files'))
    return m

def create_sub_menu():
    m = types.InlineKeyboardMarkup(row_width=1)
    m.add(
        types.InlineKeyboardButton("➕ Add Sub",    callback_data='add_subscription'),
        types.InlineKeyboardButton("➖ Remove Sub", callback_data='remove_subscription'),
        types.InlineKeyboardButton("🔍 Check Sub",  callback_data='check_subscription'),
        types.InlineKeyboardButton("🔙 Back",       callback_data='back_to_main'),
    )
    return m

def create_admin_panel():
    m = types.InlineKeyboardMarkup(row_width=2)
    m.add(
        types.InlineKeyboardButton("➕ Add Admin",    callback_data='add_admin'),
        types.InlineKeyboardButton("➖ Remove Admin", callback_data='remove_admin'),
    )
    m.add(types.InlineKeyboardButton("📋 List Admins", callback_data='list_admins'))
    m.add(types.InlineKeyboardButton("🔙 Back",        callback_data='back_to_main'))
    return m

# ════════════════════════════════════════════
#   FILE UPLOAD HANDLING
# ════════════════════════════════════════════
def handle_py_file(fpath, owner_id, folder, fname, msg):
    save_user_file(owner_id, fname, 'py')
    threading.Thread(target=run_script,
                     args=(fpath, owner_id, folder, fname, msg),
                     daemon=True).start()

def handle_js_file(fpath, owner_id, folder, fname, msg):
    save_user_file(owner_id, fname, 'js')
    threading.Thread(target=run_js_script,
                     args=(fpath, owner_id, folder, fname, msg),
                     daemon=True).start()

def handle_zip_file(fpath, user_id, folder, msg):
    tmp = None
    try:
        tmp = tempfile.mkdtemp()
        with zipfile.ZipFile(fpath, 'r') as zf:
            zf.extractall(tmp)
        py_files, js_files = [], []
        for root, _, files in os.walk(tmp):
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), tmp)
                if f.endswith('.py'): py_files.append(rel)
                elif f.endswith('.js'): js_files.append(rel)
        main = None; ftype = None
        for p in ['main.py','bot.py','app.py']:
            if p in py_files: main = p; ftype = 'py'; break
        if not main:
            for p in ['index.js','main.js','bot.js','app.js']:
                if p in js_files: main = p; ftype = 'js'; break
        if not main:
            main = (py_files or js_files or [None])[0]
            ftype = 'py' if py_files else 'js'
        if not main:
            bot.reply_to(msg, "❌ No `.py` or `.js` found in archive!"); return
        for item in os.listdir(tmp):
            src = os.path.join(tmp, item); dst = os.path.join(folder, item)
            if os.path.isdir(dst): shutil.rmtree(dst)
            elif os.path.exists(dst): os.remove(dst)
            shutil.move(src, dst)
        save_user_file(user_id, main, ftype)
        spath = os.path.join(folder, main)
        bot.reply_to(msg, f"✅ Extracted. Starting `{main}`...", parse_mode='Markdown')
        if ftype == 'py':
            threading.Thread(target=run_script,
                             args=(spath, user_id, folder, main, msg), daemon=True).start()
        else:
            threading.Thread(target=run_js_script,
                             args=(spath, user_id, folder, main, msg), daemon=True).start()
    except zipfile.BadZipFile as e:
        bot.reply_to(msg, f"❌ Invalid ZIP: {e}")
    except Exception as e:
        logger.error(f"ZIP error for {user_id}: {e}", exc_info=True)
        bot.reply_to(msg, f"❌ ZIP error: {e}")
    finally:
        if tmp and os.path.exists(tmp):
            try: shutil.rmtree(tmp)
            except Exception: pass

# ════════════════════════════════════════════
#   LOGIC FUNCTIONS
# ════════════════════════════════════════════
def _user_status_str(user_id):
    if user_id == OWNER_ID: return "👑 Owner", ""
    if user_id in admin_ids: return "🛡️ Admin", ""
    sub = user_subscriptions.get(user_id, {})
    exp = sub.get('expiry')
    if exp and exp > datetime.now():
        return "⭐ Premium", f"\n⏳ Expires in {(exp - datetime.now()).days} days"
    return "🆓 Free User", ""

def send_welcome(message):
    uid  = message.from_user.id
    cid  = message.chat.id
    name = message.from_user.first_name
    uname = message.from_user.username
    if bot_locked and uid not in admin_ids:
        bot.send_message(cid, "⚠️ Bot is locked by admin."); return
    photo = None
    try:
        photos = bot.get_user_profile_photos(uid, limit=1)
        if photos.photos: photo = photos.photos[0][-1].file_id
    except Exception: pass
    if uid not in active_users:
        add_active_user(uid)
        try:
            bot.send_message(OWNER_ID,
                             f"🎉 New user!\n👤 {name}\n✳️ @{uname or 'N/A'}\n🆔 `{uid}`",
                             parse_mode='Markdown')
        except Exception: pass
    status, expiry_info = _user_status_str(uid)
    limit = get_user_file_limit(uid)
    lstr  = str(limit) if limit != float('inf') else "∞"
    cnt   = get_user_file_count(uid)
    run_c = sum(1 for sk in list(bot_scripts)
                if sk.startswith(f"{uid}_") and is_bot_running(uid, bot_scripts[sk]['file_name']))
    text = (f"〽️ Welcome, *{name}*!\n\n"
            f"🆔 `{uid}`  |  ✳️ @{uname or 'N/A'}\n"
            f"🔰 Status: {status}{expiry_info}\n"
            f"📁 Files: `{cnt}` / `{lstr}`   🟢 Running: `{run_c}`\n\n"
            f"🚀 *ZYROX Ultra Hosting* — Python & JS Bot Hosting\n"
            f"Upload `.py` `.js` or `.zip` — auto-install, auto-restart!\n\n"
            f"👇 Use buttons below:")
    markup = create_reply_keyboard(uid)
    try:
        if photo: bot.send_photo(cid, photo)
        bot.send_message(cid, text, reply_markup=markup, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Welcome error {uid}: {e}")
        try: bot.send_message(cid, text, reply_markup=markup, parse_mode='Markdown')
        except Exception: pass


def logic_statistics(message):
    uid = message.from_user.id
    total_u = len(active_users)
    total_f = sum(len(v) for v in user_files.values())
    running = 0; user_run = 0
    for sk, info in list(bot_scripts.items()):
        try:
            oid = int(sk.split('_')[0])
            if is_bot_running(oid, info['file_name']):
                running += 1
                if oid == uid: user_run += 1
        except Exception: pass
    text = (f"📊 *Statistics*\n\n"
            f"👥 Users: `{total_u}`\n"
            f"📂 Files: `{total_f}`\n"
            f"🟢 Running Bots: `{running}`\n"
            f"🤖 Your Running: `{user_run}`")
    if uid in admin_ids:
        text += (f"\n\n🔒 Bot: `{'Locked' if bot_locked else 'Unlocked'}`\n"
                 f"🛡️ Admins: `{len(admin_ids)}`\n"
                 f"💾 RAM: `{psutil.virtual_memory().percent}%`\n"
                 f"💻 CPU: `{psutil.cpu_percent(interval=0.1)}%`")
    bot.reply_to(message, text, parse_mode='Markdown')


def logic_bot_speed(message):
    uid = message.from_user.id
    t0  = time.time()
    msg = bot.reply_to(message, "⏱️ Testing...")
    ms  = round((time.time() - t0) * 1000, 2)
    status = "🔒 Locked" if bot_locked else "🔓 Unlocked"
    lvl, _  = _user_status_str(uid)
    run_c = sum(1 for sk in list(bot_scripts)
                if is_bot_running(int(sk.split('_')[0]), bot_scripts[sk]['file_name']))
    text = (f"⚡ *Speed & Status*\n\n"
            f"⏱️ Response: `{ms} ms`\n"
            f"🚦 Bot: {status}\n"
            f"👤 Level: {lvl}\n"
            f"🟢 Running: `{run_c}`\n"
            f"💾 RAM: `{psutil.virtual_memory().percent}%`\n"
            f"💻 CPU: `{psutil.cpu_percent(interval=0.1)}%`")
    bot.edit_message_text(text, message.chat.id, msg.message_id, parse_mode='Markdown')


def logic_check_files(message):
    uid = message.from_user.id
    files = user_files.get(uid, [])
    if not files:
        bot.reply_to(message, "📂 No files uploaded yet."); return
    m = types.InlineKeyboardMarkup(row_width=1)
    for fn, ft in sorted(files):
        icon = "🟢" if is_bot_running(uid, fn) else "🔴"
        m.add(types.InlineKeyboardButton(f"{icon} {fn} ({ft})",
                                          callback_data=f'file_{uid}_{fn}'))
    bot.reply_to(message, "📂 *Your Files* — tap to manage:",
                 reply_markup=m, parse_mode='Markdown')


def logic_upload_prompt(message):
    uid = message.from_user.id
    if bot_locked and uid not in admin_ids:
        bot.reply_to(message, "⚠️ Bot locked."); return
    limit = get_user_file_limit(uid)
    cnt   = get_user_file_count(uid)
    if cnt >= limit:
        lstr = str(limit) if limit != float('inf') else "∞"
        bot.reply_to(message, f"⚠️ File limit reached ({cnt}/{lstr}). Delete a file first."); return
    bot.reply_to(message, "📤 Send your `.py`, `.js` or `.zip` file now.")


def logic_run_all_scripts(src):
    is_msg = isinstance(src, telebot.types.Message)
    uid    = src.from_user.id if is_msg else src.from_user.id
    cid    = src.chat.id if is_msg else src.message.chat.id
    reply  = (lambda t, **kw: bot.reply_to(src, t, **kw)) if is_msg \
             else (lambda t, **kw: bot.send_message(cid, t, **kw))
    if not is_msg: bot.answer_callback_query(src.id)
    if uid not in admin_ids: reply("⚠️ Admin only."); return
    reply("⏳ Starting all stopped scripts...")
    started = 0; skipped = 0
    msg_obj = src if is_msg else src.message
    for oid, files in dict(user_files).items():
        folder = get_user_folder(oid)
        for fn, ft in files:
            if is_bot_running(oid, fn): skipped += 1; continue
            fp = os.path.join(folder, fn)
            if not os.path.exists(fp): continue
            runner = run_script if ft == 'py' else run_js_script
            threading.Thread(target=runner,
                             args=(fp, oid, folder, fn, msg_obj),
                             daemon=True).start()
            started += 1; time.sleep(0.5)
    reply(f"✅ Started: `{started}`  |  Skipped: `{skipped}`", parse_mode='Markdown')

# ════════════════════════════════════════════
#   BROADCAST
# ════════════════════════════════════════════
def logic_broadcast_init(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin only."); return
    msg = bot.reply_to(message, "📢 Send the broadcast message (/cancel to abort):")
    bot.register_next_step_handler(msg, _do_broadcast)

def _do_broadcast(message):
    if message.from_user.id not in admin_ids: return
    if message.text and message.text.lower() == '/cancel':
        bot.reply_to(message, "Cancelled."); return
    sent = failed = 0
    bot.reply_to(message, f"📢 Broadcasting to {len(active_users)} users...")
    for uid in list(active_users):
        try: bot.copy_message(uid, message.chat.id, message.message_id); sent += 1; time.sleep(0.05)
        except Exception: failed += 1
    bot.reply_to(message, f"✅ Done — Sent: {sent}  Failed: {failed}")

# ════════════════════════════════════════════
#   COMMAND HANDLERS
# ════════════════════════════════════════════
@bot.message_handler(commands=['start', 'help'])
def cmd_start(m): add_active_user(m.from_user.id); send_welcome(m)

@bot.message_handler(commands=['upload'])
def cmd_upload(m): logic_upload_prompt(m)

@bot.message_handler(commands=['files', 'checkfiles'])
def cmd_files(m): logic_check_files(m)

@bot.message_handler(commands=['stats'])
def cmd_stats(m): logic_statistics(m)

@bot.message_handler(commands=['broadcast'])
def cmd_broadcast(m): logic_broadcast_init(m)

@bot.message_handler(commands=['lock'])
def cmd_lock(m):
    global bot_locked
    if m.from_user.id not in admin_ids: bot.reply_to(m, "⚠️ Admin only."); return
    bot_locked = True; bot.reply_to(m, "🔒 Bot locked.")

@bot.message_handler(commands=['unlock'])
def cmd_unlock(m):
    global bot_locked
    if m.from_user.id not in admin_ids: bot.reply_to(m, "⚠️ Admin only."); return
    bot_locked = False; bot.reply_to(m, "🔓 Bot unlocked.")

@bot.message_handler(commands=['runall'])
def cmd_runall(m): logic_run_all_scripts(m)

@bot.message_handler(commands=['cancel'])
def cmd_cancel(m):
    bot.clear_step_handler_by_chat_id(m.chat.id)
    bot.reply_to(m, "✅ Cancelled.")

# ════════════════════════════════════════════
#   BUTTON / TEXT HANDLER
# ════════════════════════════════════════════
_BTN_MAP = {
    "📢 Updates Channel":  lambda m: bot.reply_to(m, "📢 Channel:", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("Join", url=UPDATE_CHANNEL))),
    "📤 Upload File":      logic_upload_prompt,
    "📂 Check Files":      logic_check_files,
    "⚡ Bot Speed":        logic_bot_speed,
    "📊 Statistics":       logic_statistics,
    "📞 Contact Owner":    lambda m: bot.reply_to(m, "Contact:", reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("📞 Owner", url=f"https://t.me/{YOUR_USERNAME.replace('@','')}"))),
    "💳 Subscriptions":    lambda m: (m.from_user.id in admin_ids and bot.reply_to(m, "💳 Sub Menu", reply_markup=create_sub_menu())) or bot.reply_to(m, "⚠️ Admin only."),
    "📢 Broadcast":        logic_broadcast_init,
    "🔒 Lock Bot":         lambda m: (setattr(__builtins__, '_', None) or cmd_lock(m)),  # delegates below
    "🟢 Running All Code": logic_run_all_scripts,
    "👑 Admin Panel":      lambda m: (m.from_user.id in admin_ids and bot.reply_to(m, "👑 Admin Panel", reply_markup=create_admin_panel())) or bot.reply_to(m, "⚠️ Admin only."),
}

@bot.message_handler(func=lambda m: m.text and not m.text.startswith('/'))
def handle_text(m):
    fn = _BTN_MAP.get(m.text.strip())
    if fn:
        if m.text.strip() == "🔒 Lock Bot":
            global bot_locked
            if m.from_user.id not in admin_ids: bot.reply_to(m, "⚠️ Admin only."); return
            bot_locked = not bot_locked
            bot.reply_to(m, f"Bot {'🔒 locked' if bot_locked else '🔓 unlocked'}.")
        else:
            fn(m)

# ════════════════════════════════════════════
#   DOCUMENT HANDLER
# ════════════════════════════════════════════
@bot.message_handler(content_types=['document'])
def handle_document(message):
    uid = message.from_user.id
    if bot_locked and uid not in admin_ids:
        bot.reply_to(message, "⚠️ Bot locked."); return
    doc  = message.document
    fname = doc.file_name or "unknown"
    if not (fname.endswith('.py') or fname.endswith('.js') or fname.endswith('.zip')):
        bot.reply_to(message, "⚠️ Only `.py`, `.js`, `.zip` supported."); return
    limit = get_user_file_limit(uid)
    cnt   = get_user_file_count(uid)
    existing = [f[0] for f in user_files.get(uid, [])]
    if fname not in existing and cnt >= limit:
        lstr = str(limit) if limit != float('inf') else "∞"
        bot.reply_to(message, f"⚠️ Limit ({cnt}/{lstr}) reached."); return
    wmsg = bot.reply_to(message, f"⬇️ Downloading `{fname}`...", parse_mode='Markdown')
    try:
        fi    = bot.get_file(doc.file_id)
        fbytes = bot.download_file(fi.file_path)
        folder = get_user_folder(uid)
        fpath  = os.path.join(folder, fname)
        with open(fpath, 'wb') as f: f.write(fbytes)
        bot.edit_message_text(f"✅ Downloaded `{fname}`. Processing...",
                              message.chat.id, wmsg.message_id, parse_mode='Markdown')
        if fname.endswith('.py'):   handle_py_file(fpath, uid, folder, fname, message)
        elif fname.endswith('.js'): handle_js_file(fpath, uid, folder, fname, message)
        elif fname.endswith('.zip'): handle_zip_file(fpath, uid, folder, message)
    except Exception as e:
        logger.error(f"Document handler error for {uid}: {e}", exc_info=True)
        bot.reply_to(message, f"❌ Error: {e}")

# ════════════════════════════════════════════
#   CALLBACK ROUTER
# ════════════════════════════════════════════
@bot.callback_query_handler(func=lambda c: True)
def callback_router(call):
    d = call.data
    try:
        if   d == 'back_to_main':            cb_back_to_main(call)
        elif d == 'upload_file':             cb_upload_file(call)
        elif d == 'check_files':             cb_check_files(call)
        elif d == 'speed':                   cb_speed(call)
        elif d == 'stats':                   cb_stats(call)
        elif d == 'subscription_management': cb_sub_management(call)
        elif d == 'lock_bot':                cb_lock(call)
        elif d == 'unlock_bot':              cb_unlock(call)
        elif d == 'admin_panel':             cb_admin_panel(call)
        elif d == 'run_all_scripts':         logic_run_all_scripts(call)
        elif d == 'add_admin':               cb_add_admin(call)
        elif d == 'remove_admin':            cb_remove_admin(call)
        elif d == 'list_admins':             cb_list_admins(call)
        elif d == 'add_subscription':        cb_add_sub(call)
        elif d == 'remove_subscription':     cb_remove_sub(call)
        elif d == 'check_subscription':      cb_check_sub(call)
        elif d.startswith('file_'):          cb_file_control(call)
        elif d.startswith('start_'):         cb_start(call)
        elif d.startswith('stop_'):          cb_stop(call)
        elif d.startswith('restart_'):       cb_restart(call)
        elif d.startswith('delete_'):        cb_delete(call)
        elif d.startswith('logs_'):          cb_logs(call)
        else: bot.answer_callback_query(call.id, "Unknown action.")
    except Exception as e:
        logger.error(f"Callback error '{d}': {e}", exc_info=True)
        try: bot.answer_callback_query(call.id, "Error.", show_alert=True)
        except Exception: pass

# ── General callbacks ───────────────────────
def cb_back_to_main(call):
    uid = call.from_user.id
    status, ei = _user_status_str(uid)
    limit = get_user_file_limit(uid)
    lstr  = str(limit) if limit != float('inf') else "∞"
    cnt   = get_user_file_count(uid)
    text = (f"〽️ *Main Menu*\n\n"
            f"🆔 `{uid}` | 🔰 {status}{ei}\n"
            f"📁 `{cnt}` / `{lstr}`")
    try:
        bot.answer_callback_query(call.id)
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              reply_markup=create_main_inline(uid), parse_mode='Markdown')
    except telebot.apihelper.ApiTelegramException as e:
        if "not modified" not in str(e): logger.error(f"cb_back_to_main: {e}")

def cb_upload_file(call):
    uid = call.from_user.id
    limit = get_user_file_limit(uid); cnt = get_user_file_count(uid)
    if cnt >= limit:
        lstr = str(limit) if limit != float('inf') else "∞"
        bot.answer_callback_query(call.id, f"⚠️ Limit {cnt}/{lstr} reached!", show_alert=True); return
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "📤 Send your `.py`, `.js` or `.zip` file.")

def cb_check_files(call):
    uid = call.from_user.id
    files = user_files.get(uid, [])
    bot.answer_callback_query(call.id)
    if not files:
        m = types.InlineKeyboardMarkup()
        m.add(types.InlineKeyboardButton("🔙 Back", callback_data='back_to_main'))
        try: bot.edit_message_text("📂 No files yet.", call.message.chat.id,
                                    call.message.message_id, reply_markup=m)
        except Exception: pass; return
    markup = types.InlineKeyboardMarkup(row_width=1)
    for fn, ft in sorted(files):
        icon = "🟢" if is_bot_running(uid, fn) else "🔴"
        markup.add(types.InlineKeyboardButton(f"{icon} {fn} ({ft})",
                                               callback_data=f'file_{uid}_{fn}'))
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data='back_to_main'))
    try:
        bot.edit_message_text("📂 *Your Files:*", call.message.chat.id,
                              call.message.message_id, reply_markup=markup, parse_mode='Markdown')
    except telebot.apihelper.ApiTelegramException as e:
        if "not modified" not in str(e): logger.error(f"cb_check_files: {e}")

def cb_speed(call):
    uid = call.from_user.id; t0 = time.time()
    try:
        bot.edit_message_text("⏱️ Testing...", call.message.chat.id, call.message.message_id)
        ms   = round((time.time() - t0) * 1000, 2)
        status = "🔒 Locked" if bot_locked else "🔓 Unlocked"
        lvl, _ = _user_status_str(uid)
        run_c = sum(1 for sk in list(bot_scripts)
                    if is_bot_running(int(sk.split('_')[0]), bot_scripts[sk]['file_name']))
        text = (f"⚡ *Speed*\n⏱️ `{ms} ms`\n🚦 {status}\n"
                f"👤 {lvl}\n🟢 Running: `{run_c}`\n"
                f"💾 RAM: `{psutil.virtual_memory().percent}%`\n"
                f"💻 CPU: `{psutil.cpu_percent(interval=0.1)}%`")
        bot.answer_callback_query(call.id)
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              reply_markup=create_main_inline(uid), parse_mode='Markdown')
    except Exception as e:
        bot.answer_callback_query(call.id, "Error.", show_alert=True)

def cb_stats(call):
    bot.answer_callback_query(call.id)
    logic_statistics(call.message)
    try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                        reply_markup=create_main_inline(call.from_user.id))
    except Exception: pass

def cb_lock(call):
    global bot_locked
    if call.from_user.id not in admin_ids:
        bot.answer_callback_query(call.id, "⚠️ Admin only.", show_alert=True); return
    bot_locked = True
    bot.answer_callback_query(call.id, "🔒 Bot locked.")
    try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                        reply_markup=create_main_inline(call.from_user.id))
    except Exception: pass

def cb_unlock(call):
    global bot_locked
    if call.from_user.id not in admin_ids:
        bot.answer_callback_query(call.id, "⚠️ Admin only.", show_alert=True); return
    bot_locked = False
    bot.answer_callback_query(call.id, "🔓 Bot unlocked.")
    try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                        reply_markup=create_main_inline(call.from_user.id))
    except Exception: pass

def cb_sub_management(call):
    if call.from_user.id not in admin_ids:
        bot.answer_callback_query(call.id, "⚠️ Admin only.", show_alert=True); return
    bot.answer_callback_query(call.id)
    try: bot.edit_message_text("💳 *Subscription Management*", call.message.chat.id,
                                call.message.message_id, reply_markup=create_sub_menu(),
                                parse_mode='Markdown')
    except Exception: pass

def cb_admin_panel(call):
    if call.from_user.id not in admin_ids:
        bot.answer_callback_query(call.id, "⚠️ Admin only.", show_alert=True); return
    bot.answer_callback_query(call.id)
    try: bot.edit_message_text("👑 *Admin Panel*", call.message.chat.id,
                                call.message.message_id, reply_markup=create_admin_panel(),
                                parse_mode='Markdown')
    except Exception: pass

# ── File control callbacks ──────────────────
def _parse_cb(data):
    """Split callback data: prefix_ownerid_filename → (owner_id, file_name)"""
    parts = data.split('_', 2)
    return int(parts[1]), parts[2]

def cb_file_control(call):
    try:
        owner_id, fname = _parse_cb(call.data)
        req = call.from_user.id
        if not (req == owner_id or req in admin_ids):
            bot.answer_callback_query(call.id, "⚠️ Permission denied.", show_alert=True); return
        files = user_files.get(owner_id, [])
        if not any(f[0] == fname for f in files):
            bot.answer_callback_query(call.id, "⚠️ File not found.", show_alert=True); return
        bot.answer_callback_query(call.id)
        running = is_bot_running(owner_id, fname)
        ft = next((f[1] for f in files if f[0] == fname), '?')
        try:
            bot.edit_message_text(
                f"⚙️ *{fname}* (`{ft}`)\n👤 `{owner_id}`\n📊 {'🟢 Running' if running else '🔴 Stopped'}",
                call.message.chat.id, call.message.message_id,
                reply_markup=create_control_buttons(owner_id, fname, running),
                parse_mode='Markdown')
        except telebot.apihelper.ApiTelegramException as e:
            if "not modified" not in str(e): raise
    except Exception as e: logger.error(f"cb_file_control: {e}"); bot.answer_callback_query(call.id, "Error.")

def cb_start(call):
    try:
        owner_id, fname = _parse_cb(call.data)
        req = call.from_user.id; cid = call.message.chat.id
        if not (req == owner_id or req in admin_ids):
            bot.answer_callback_query(call.id, "⚠️ Permission denied.", show_alert=True); return
        files = user_files.get(owner_id, [])
        fi    = next((f for f in files if f[0] == fname), None)
        if not fi: bot.answer_callback_query(call.id, "⚠️ File not found.", show_alert=True); return
        if is_bot_running(owner_id, fname): bot.answer_callback_query(call.id, "⚠️ Already running!", show_alert=True); return
        ft     = fi[1]; folder = get_user_folder(owner_id)
        fpath  = os.path.join(folder, fname)
        if not os.path.exists(fpath):
            bot.answer_callback_query(call.id, "⚠️ File missing! Re-upload.", show_alert=True)
            remove_user_file_db(owner_id, fname); return
        bot.answer_callback_query(call.id, f"▶️ Starting {fname}...")
        runner = run_script if ft == 'py' else run_js_script
        threading.Thread(target=runner, args=(fpath, owner_id, folder, fname, call.message), daemon=True).start()
        time.sleep(1.5)
        running = is_bot_running(owner_id, fname)
        try:
            bot.edit_message_text(
                f"⚙️ *{fname}* | {'🟢 Running' if running else '🟡 Starting...'}",
                cid, call.message.message_id,
                reply_markup=create_control_buttons(owner_id, fname, running), parse_mode='Markdown')
        except telebot.apihelper.ApiTelegramException as e:
            if "not modified" not in str(e): raise
    except Exception as e: logger.error(f"cb_start: {e}"); bot.answer_callback_query(call.id, "Error.")

def cb_stop(call):
    try:
        owner_id, fname = _parse_cb(call.data)
        req = call.from_user.id; cid = call.message.chat.id
        if not (req == owner_id or req in admin_ids):
            bot.answer_callback_query(call.id, "⚠️ Permission denied.", show_alert=True); return
        if not is_bot_running(owner_id, fname):
            bot.answer_callback_query(call.id, "⚠️ Already stopped.", show_alert=True); return
        bot.answer_callback_query(call.id, f"🔴 Stopping {fname}...")
        key = f"{owner_id}_{fname}"
        pi  = bot_scripts.get(key)
        if pi: kill_process_tree(pi); bot_scripts.pop(key, None)
        # ← CRITICAL: mark stopped in Firebase → watchdog will NOT restart
        firebase.mark_stopped(owner_id, fname)
        ft = next((f[1] for f in user_files.get(owner_id, []) if f[0] == fname), '?')
        try:
            bot.edit_message_text(
                f"⚙️ *{fname}* (`{ft}`) | 🔴 Stopped",
                cid, call.message.message_id,
                reply_markup=create_control_buttons(owner_id, fname, False), parse_mode='Markdown')
        except telebot.apihelper.ApiTelegramException as e:
            if "not modified" not in str(e): raise
    except Exception as e: logger.error(f"cb_stop: {e}"); bot.answer_callback_query(call.id, "Error.")

def cb_restart(call):
    try:
        owner_id, fname = _parse_cb(call.data)
        req = call.from_user.id; cid = call.message.chat.id
        if not (req == owner_id or req in admin_ids):
            bot.answer_callback_query(call.id, "⚠️ Permission denied.", show_alert=True); return
        fi = next((f for f in user_files.get(owner_id, []) if f[0] == fname), None)
        if not fi: bot.answer_callback_query(call.id, "⚠️ File not found.", show_alert=True); return
        ft     = fi[1]; folder = get_user_folder(owner_id)
        fpath  = os.path.join(folder, fname)
        if not os.path.exists(fpath):
            bot.answer_callback_query(call.id, "⚠️ File missing! Re-upload.", show_alert=True)
            remove_user_file_db(owner_id, fname); return
        bot.answer_callback_query(call.id, f"🔄 Restarting {fname}...")
        key = f"{owner_id}_{fname}"
        if is_bot_running(owner_id, fname):
            pi = bot_scripts.get(key)
            if pi: kill_process_tree(pi)
            bot_scripts.pop(key, None); time.sleep(1.5)
        runner = run_script if ft == 'py' else run_js_script
        threading.Thread(target=runner, args=(fpath, owner_id, folder, fname, call.message), daemon=True).start()
        time.sleep(1.5)
        running = is_bot_running(owner_id, fname)
        try:
            bot.edit_message_text(
                f"⚙️ *{fname}* | {'🟢 Running' if running else '🟡 Starting...'}",
                cid, call.message.message_id,
                reply_markup=create_control_buttons(owner_id, fname, running), parse_mode='Markdown')
        except telebot.apihelper.ApiTelegramException as e:
            if "not modified" not in str(e): raise
    except Exception as e: logger.error(f"cb_restart: {e}"); bot.answer_callback_query(call.id, "Error.")

def cb_delete(call):
    try:
        owner_id, fname = _parse_cb(call.data)
        req = call.from_user.id; cid = call.message.chat.id
        if not (req == owner_id or req in admin_ids):
            bot.answer_callback_query(call.id, "⚠️ Permission denied.", show_alert=True); return
        if not any(f[0] == fname for f in user_files.get(owner_id, [])):
            bot.answer_callback_query(call.id, "⚠️ File not found.", show_alert=True); return
        bot.answer_callback_query(call.id, f"🗑️ Deleting {fname}...")
        key = f"{owner_id}_{fname}"
        # Stop process if running
        if is_bot_running(owner_id, fname):
            pi = bot_scripts.get(key)
            if pi: kill_process_tree(pi)
            bot_scripts.pop(key, None)
        # ← CRITICAL: remove from Firebase → watchdog will NOT restart
        firebase.mark_deleted(owner_id, fname)
        # Remove from disk
        folder = get_user_folder(owner_id)
        for name in [fname, f"{os.path.splitext(fname)[0]}.log"]:
            fp = os.path.join(folder, name)
            if os.path.exists(fp):
                try: os.remove(fp)
                except Exception: pass
        remove_user_file_db(owner_id, fname)
        m2 = types.InlineKeyboardMarkup()
        m2.add(types.InlineKeyboardButton("📂 My Files", callback_data='check_files'))
        m2.add(types.InlineKeyboardButton("🔙 Menu",     callback_data='back_to_main'))
        try:
            bot.edit_message_text(f"🗑️ `{fname}` deleted.", cid, call.message.message_id,
                                  reply_markup=m2, parse_mode='Markdown')
        except Exception: pass
    except Exception as e: logger.error(f"cb_delete: {e}"); bot.answer_callback_query(call.id, "Error.")

def cb_logs(call):
    try:
        owner_id, fname = _parse_cb(call.data)
        req = call.from_user.id; cid = call.message.chat.id
        if not (req == owner_id or req in admin_ids):
            bot.answer_callback_query(call.id, "⚠️ Permission denied.", show_alert=True); return
        bot.answer_callback_query(call.id)
        folder   = get_user_folder(owner_id)
        log_path = os.path.join(folder, f"{os.path.splitext(fname)[0]}.log")
        if not os.path.exists(log_path):
            bot.send_message(cid, f"📭 No logs for `{fname}`.", parse_mode='Markdown'); return
        fsz = os.path.getsize(log_path)
        max_kb = 100; max_tg = 3900
        if fsz == 0:
            content = "(Log empty)"
        elif fsz > max_kb * 1024:
            with open(log_path, 'rb') as f:
                f.seek(-max_kb * 1024, os.SEEK_END)
                content = f"(Last {max_kb}KB)\n...\n" + f.read().decode('utf-8', errors='ignore')
        else:
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
        if len(content) > max_tg:
            content = "...\n" + content[-max_tg:]
        if not content.strip(): content = "(Empty)"
        bot.send_message(cid,
                         f"📜 *Logs: {fname}*\n```\n{content}\n```",
                         parse_mode='Markdown')
    except Exception as e: logger.error(f"cb_logs: {e}"); bot.answer_callback_query(call.id, "Error.")

# ── Admin management callbacks ──────────────
def cb_add_admin(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, "⚠️ Owner only.", show_alert=True); return
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "Enter User ID to promote (/cancel):")
    bot.register_next_step_handler(msg, _process_add_admin)

def _process_add_admin(message):
    if message.from_user.id != OWNER_ID: return
    if message.text.lower() == '/cancel': bot.reply_to(message, "Cancelled."); return
    try:
        nid = int(message.text.strip())
        if nid in admin_ids: bot.reply_to(message, f"⚠️ `{nid}` already admin.", parse_mode='Markdown'); return
        add_admin_db(nid)
        bot.reply_to(message, f"✅ `{nid}` promoted.", parse_mode='Markdown')
        try: bot.send_message(nid, "🎉 You are now an Admin!")
        except Exception: pass
    except ValueError:
        bot.reply_to(message, "⚠️ Invalid ID.")
        msg = bot.send_message(message.chat.id, "Enter valid User ID (/cancel):")
        bot.register_next_step_handler(msg, _process_add_admin)

def cb_remove_admin(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, "⚠️ Owner only.", show_alert=True); return
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "Enter Admin ID to remove (/cancel):")
    bot.register_next_step_handler(msg, _process_remove_admin)

def _process_remove_admin(message):
    if message.from_user.id != OWNER_ID: return
    if message.text.lower() == '/cancel': bot.reply_to(message, "Cancelled."); return
    try:
        rid = int(message.text.strip())
        if rid == OWNER_ID: bot.reply_to(message, "⚠️ Cannot remove Owner."); return
        if rid not in admin_ids: bot.reply_to(message, f"⚠️ `{rid}` not an admin.", parse_mode='Markdown'); return
        if remove_admin_db(rid):
            bot.reply_to(message, f"✅ Admin `{rid}` removed.", parse_mode='Markdown')
            try: bot.send_message(rid, "ℹ️ You are no longer an admin.")
            except Exception: pass
        else: bot.reply_to(message, "❌ Failed.")
    except ValueError:
        bot.reply_to(message, "⚠️ Invalid ID.")
        msg = bot.send_message(message.chat.id, "Enter Admin ID (/cancel):")
        bot.register_next_step_handler(msg, _process_remove_admin)

def cb_list_admins(call):
    bot.answer_callback_query(call.id)
    lst = "\n".join(f"• `{aid}` {'👑 Owner' if aid == OWNER_ID else ''}" for aid in sorted(admin_ids))
    try:
        bot.edit_message_text(f"👑 *Admins:*\n\n{lst or '(none)'}",
                              call.message.chat.id, call.message.message_id,
                              reply_markup=create_admin_panel(), parse_mode='Markdown')
    except telebot.apihelper.ApiTelegramException as e:
        if "not modified" not in str(e): logger.error(f"cb_list_admins: {e}")

# ── Subscription callbacks ──────────────────
def cb_add_sub(call):
    if call.from_user.id not in admin_ids:
        bot.answer_callback_query(call.id, "⚠️ Admin only.", show_alert=True); return
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "Enter `USER_ID DAYS` (e.g. `123456 30`) or /cancel:")
    bot.register_next_step_handler(msg, _process_add_sub)

def _process_add_sub(message):
    if message.from_user.id not in admin_ids: return
    if message.text.lower() == '/cancel': bot.reply_to(message, "Cancelled."); return
    try:
        parts = message.text.split()
        if len(parts) != 2: raise ValueError("Format: USER_ID DAYS")
        sub_uid = int(parts[0]); days = int(parts[1])
        if sub_uid <= 0 or days <= 0: raise ValueError("Positive values only")
        cur_exp = user_subscriptions.get(sub_uid, {}).get('expiry')
        start   = datetime.now()
        if cur_exp and cur_exp > start: start = cur_exp
        new_exp = start + timedelta(days=days)
        save_subscription(sub_uid, new_exp)
        bot.reply_to(message, f"✅ Sub for `{sub_uid}`: +{days}d → `{new_exp:%Y-%m-%d}`", parse_mode='Markdown')
        try: bot.send_message(sub_uid, f"🎉 Subscription! Expires: {new_exp:%Y-%m-%d}")
        except Exception: pass
    except ValueError as e:
        bot.reply_to(message, f"⚠️ {e}")
        msg = bot.send_message(message.chat.id, "Try again: `USER_ID DAYS` (/cancel)")
        bot.register_next_step_handler(msg, _process_add_sub)

def cb_remove_sub(call):
    if call.from_user.id not in admin_ids:
        bot.answer_callback_query(call.id, "⚠️ Admin only.", show_alert=True); return
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "Enter User ID to remove sub (/cancel):")
    bot.register_next_step_handler(msg, _process_remove_sub)

def _process_remove_sub(message):
    if message.from_user.id not in admin_ids: return
    if message.text.lower() == '/cancel': bot.reply_to(message, "Cancelled."); return
    try:
        uid = int(message.text.strip())
        if uid not in user_subscriptions:
            bot.reply_to(message, f"⚠️ No sub for `{uid}`.", parse_mode='Markdown'); return
        remove_subscription_db(uid)
        bot.reply_to(message, f"✅ Sub removed for `{uid}`.", parse_mode='Markdown')
        try: bot.send_message(uid, "ℹ️ Your subscription was removed.")
        except Exception: pass
    except ValueError:
        bot.reply_to(message, "⚠️ Invalid ID.")
        msg = bot.send_message(message.chat.id, "Enter User ID (/cancel):")
        bot.register_next_step_handler(msg, _process_remove_sub)

def cb_check_sub(call):
    if call.from_user.id not in admin_ids:
        bot.answer_callback_query(call.id, "⚠️ Admin only.", show_alert=True); return
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "Enter User ID to check (/cancel):")
    bot.register_next_step_handler(msg, _process_check_sub)

def _process_check_sub(message):
    if message.from_user.id not in admin_ids: return
    if message.text.lower() == '/cancel': bot.reply_to(message, "Cancelled."); return
    try:
        uid = int(message.text.strip())
        if uid in user_subscriptions:
            exp = user_subscriptions[uid].get('expiry')
            if exp and exp > datetime.now():
                days = (exp - datetime.now()).days
                bot.reply_to(message, f"✅ `{uid}` active sub — {days}d left (`{exp:%Y-%m-%d}`)", parse_mode='Markdown')
            else:
                bot.reply_to(message, f"⚠️ `{uid}` subscription expired.", parse_mode='Markdown')
                remove_subscription_db(uid)
        else:
            bot.reply_to(message, f"ℹ️ `{uid}` has no subscription.", parse_mode='Markdown')
    except ValueError:
        bot.reply_to(message, "⚠️ Invalid ID.")
        msg = bot.send_message(message.chat.id, "Enter User ID (/cancel):")
        bot.register_next_step_handler(msg, _process_check_sub)

# ════════════════════════════════════════════
#   CLEANUP ON EXIT
# ════════════════════════════════════════════
def cleanup():
    logger.warning("⚠️ Shutdown — stopping all processes...")
    for key in list(bot_scripts.keys()):
        info = bot_scripts.get(key)
        if info: kill_process_tree(info)
    logger.warning("✅ Cleanup done.")

atexit.register(cleanup)

# ════════════════════════════════════════════
#   MAIN
# ════════════════════════════════════════════
if __name__ == '__main__':
    logger.info("=" * 55)
    logger.info("  🚀 ZYROX ULTRA PRO MAX HOSTING BOT")
    logger.info(f"  🐍 Python {sys.version.split()[0]}")
    logger.info(f"  📁 Base dir:  {BASE_DIR}")
    logger.info(f"  🔑 Owner ID:  {OWNER_ID}")
    logger.info(f"  🔥 Firebase:  {FIREBASE_DB_URL}")
    logger.info("=" * 55)

    keep_alive()

    # Watchdog thread
    threading.Thread(target=watchdog_loop, daemon=True, name="Watchdog").start()
    logger.info("🐕 Watchdog thread started.")

    # Delayed restore (give bot time to fully init)
    def _delayed_restore():
        time.sleep(6)
        restore_from_firebase()
    threading.Thread(target=_delayed_restore, daemon=True).start()

    # Startup notification
    def _notify():
        time.sleep(9)
        try:
            bot.send_message(OWNER_ID,
                             "🚀 *ZYROX Ultra Hosting Bot Online!*\n\n"
                             "✅ Firebase Watchdog: Active\n"
                             "🔁 Auto-Restore: Running\n"
                             "📦 Auto-Install: Ready\n"
                             "🐕 Crash Recovery: ON",
                             parse_mode='Markdown')
        except Exception: pass
    threading.Thread(target=_notify, daemon=True).start()

    logger.info("🔄 Starting polling loop…")
    while True:
        try:
            bot.infinity_polling(logger_level=logging.WARNING, timeout=60,
                                 long_polling_timeout=30)
        except requests.exceptions.ReadTimeout:
            logger.warning("Polling timeout. Retrying in 5s…"); time.sleep(5)
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error: {e}. Retrying in 15s…"); time.sleep(15)
        except Exception as e:
            logger.critical(f"💥 Polling crashed: {e}", exc_info=True); time.sleep(30)
        finally:
            time.sleep(1)
