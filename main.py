# -*- coding: utf-8 -*-
"""
Telegram Bot Hosting Manager
Run this manager on a Python host that supports persistent background processes.
WARNING: Uploaded bot code is arbitrary Python code. Only use this on a trusted,
isolated host/container. Do not expose this service to untrusted users on a shared host.
"""
import os
import sys
import re
import time
import shutil
import sqlite3
import zipfile
import logging
import threading
import subprocess
from pathlib import Path
from datetime import datetime
from collections import defaultdict

import telebot
from telebot import types

# ---------------- CONFIG ----------------
# Paste your NEW BotFather token locally below. Never share this file publicly.
BOT_TOKEN = "9199635429:AAHH8jmQRycrzeKcXo8e5hv2Ii9fxE3sWrY"
MANAGER_BOT_TOKEN = (os.getenv("MANAGER_BOT_TOKEN", "").strip() or BOT_TOKEN.strip())
ADMIN_ID = int(os.getenv("ADMIN_ID", "8907916745"))
BASE_DIR = Path(os.getenv("HOSTING_DATA_DIR", "hosting_data")).resolve()
UPLOAD_DIR = BASE_DIR / "uploads"
LOG_DIR = BASE_DIR / "logs"
VENV_DIR = BASE_DIR / "venvs"
DB_PATH = BASE_DIR / "hosting.sqlite3"
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "20"))
MAX_BOTS_PER_USER = int(os.getenv("MAX_BOTS_PER_USER", "3"))
PIP_INSTALL_TIMEOUT = int(os.getenv("PIP_INSTALL_TIMEOUT", "300"))

if not MANAGER_BOT_TOKEN or not ADMIN_ID:
    raise SystemExit("Set MANAGER_BOT_TOKEN and ADMIN_ID environment variables first.")

for folder in (UPLOAD_DIR, LOG_DIR, VENV_DIR):
    folder.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(BASE_DIR / "manager.log", encoding="utf-8")]
)
logger = logging.getLogger("hosting-manager")
bot = telebot.TeleBot(MANAGER_BOT_TOKEN, parse_mode="HTML", threaded=True)

# In-memory state: user_id -> expected input
WAITING = {}
LOCK = threading.RLock()
PROCESSES = {}  # bot_id -> Popen
LOG_THREADS = {}
BOT_COUNTER = 0


# ---------------- DATABASE ----------------
def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            banned INTEGER DEFAULT 0, joined_at TEXT)""")
        con.execute("""CREATE TABLE IF NOT EXISTS hosted_bots(
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL,
            name TEXT NOT NULL, folder TEXT NOT NULL, entrypoint TEXT NOT NULL,
            token TEXT NOT NULL, status TEXT DEFAULT 'stopped',
            pid INTEGER, created_at TEXT, last_error TEXT DEFAULT '')""")

def touch_user(user):
    with db() as con:
        con.execute("""INSERT INTO users(user_id, username, first_name, joined_at)
            VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username, first_name=excluded.first_name""",
            (user.id, user.username or "", user.first_name or "", datetime.utcnow().isoformat()))

def is_banned(user_id):
    with db() as con:
        row = con.execute("SELECT banned FROM users WHERE user_id=?", (user_id,)).fetchone()
        return bool(row and row["banned"])

def user_bots(user_id):
    with db() as con:
        return con.execute("SELECT * FROM hosted_bots WHERE owner_id=? ORDER BY id DESC",
                           (user_id,)).fetchall()

def get_bot(bot_id):
    with db() as con:
        return con.execute("SELECT * FROM hosted_bots WHERE id=?", (bot_id,)).fetchone()

def set_status(bot_id, status, pid=None, error=None):
    with db() as con:
        con.execute("UPDATE hosted_bots SET status=?, pid=?, last_error=? WHERE id=?",
                    (status, pid, error or "", bot_id))

def safe_name(value):
    value = re.sub(r"[^a-zA-Z0-9_-]+", "_", value.strip())[:40]
    return value or "my_bot"

def is_admin(user_id):
    return user_id == ADMIN_ID

def authorized_owner(user_id, row):
    return row is not None and (row["owner_id"] == user_id or is_admin(user_id))


# ---------------- FILE HANDLING ----------------
def safe_extract(zip_path: Path, dest: Path):
    """Extract ZIP without allowing path traversal."""
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            target = (dest / info.filename).resolve()
            if target != dest_resolved and dest_resolved not in target.parents:
                raise ValueError("ZIP contains an unsafe path.")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)

def find_entrypoint(folder: Path):
    for candidate in ("main.py", "bot.py", "app.py", "run.py"):
        found = folder / candidate
        if found.is_file():
            return found
    py_files = list(folder.glob("*.py"))
    if len(py_files) == 1:
        return py_files[0]
    raise ValueError("ZIP-এর root folder-এ main.py বা bot.py রাখুন।")

def install_requirements(folder: Path, bot_id: int):
    req = folder / "requirements.txt"
    python_bin = sys.executable
    if os.name != "nt":
        venv_python = VENV_DIR / str(bot_id) / "bin" / "python"
    else:
        venv_python = VENV_DIR / str(bot_id) / "Scripts" / "python.exe"
    venv_path = VENV_DIR / str(bot_id)
    if not venv_python.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv_path)],
                       check=True, capture_output=True, text=True, timeout=120)
    if req.exists():
        result = subprocess.run(
            [str(venv_python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(req)],
            capture_output=True, text=True, timeout=PIP_INSTALL_TIMEOUT
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout)[-2500:])
    return str(venv_python)


# ---------------- PROCESS MANAGEMENT ----------------
def stream_output(bot_id, process, log_path):
    try:
        with open(log_path, "a", encoding="utf-8", errors="replace") as log:
            for line in iter(process.stdout.readline, ""):
                log.write(line)
                log.flush()
    except Exception:
        logger.exception("Log stream failed for bot %s", bot_id)

def start_hosted_bot(row):
    bot_id = row["id"]
    with LOCK:
        current = PROCESSES.get(bot_id)
        if current and current.poll() is None:
            return False, "এই Bot ইতিমধ্যে চালু আছে।"
        folder = Path(row["folder"])
        entry = folder / row["entrypoint"]
        if not entry.is_file():
            return False, "Entry file পাওয়া যায়নি। আবার ZIP/.py আপলোড করুন।"
        try:
            python_bin = install_requirements(folder, bot_id)
            env = os.environ.copy()
            env.update({
                "BOT_TOKEN": row["token"],
                "TELEGRAM_BOT_TOKEN": row["token"],
                "TOKEN": row["token"],
                "PYTHONUNBUFFERED": "1"
            })
            log_path = LOG_DIR / f"{bot_id}.log"
            with open(log_path, "a", encoding="utf-8") as log:
                log.write(f"\n--- START {datetime.utcnow().isoformat()} UTC ---\n")
            process = subprocess.Popen(
                [python_bin, str(entry)],
                cwd=str(folder),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            PROCESSES[bot_id] = process
            set_status(bot_id, "running", process.pid)
            thread = threading.Thread(target=stream_output, args=(bot_id, process, log_path), daemon=True)
            thread.start()
            LOG_THREADS[bot_id] = thread
            threading.Thread(target=watch_process, args=(bot_id, process), daemon=True).start()
            return True, f"Bot চালু হয়েছে। PID: <code>{process.pid}</code>"
        except Exception as exc:
            set_status(bot_id, "error", None, str(exc))
            logger.exception("Could not start bot %s", bot_id)
            return False, f"চালু করা যায়নি: <code>{str(exc)[:1200]}</code>"

def watch_process(bot_id, process):
    code = process.wait()
    row = get_bot(bot_id)
    if row:
        set_status(bot_id, "stopped" if code == 0 else "error", None,
                   "" if code == 0 else f"Process exited with code {code}")
    logger.info("Hosted bot %s exited with code %s", bot_id, code)

def stop_hosted_bot(bot_id):
    with LOCK:
        process = PROCESSES.get(bot_id)
        if not process or process.poll() is not None:
            set_status(bot_id, "stopped", None)
            return False, "Bot আগে থেকেই বন্ধ আছে।"
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        set_status(bot_id, "stopped", None)
        return True, "Bot বন্ধ করা হয়েছে।"

def tail_log(bot_id, lines=35):
    path = LOG_DIR / f"{bot_id}.log"
    if not path.exists():
        return "এখনও কোনো Log নেই।"
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return "".join(f.readlines()[-lines:])[-3500:] or "Log ফাঁকা।"


# ---------------- UI ----------------
def main_keyboard(user_id):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📤 Upload Bot", "🤖 My Bots")
    kb.row("📊 My Account", "❓ Help")
    if is_admin(user_id):
        kb.row("🛠 Admin Panel")
    return kb

def bot_actions_keyboard(bot_id):
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("▶️ Start", callback_data=f"start:{bot_id}"),
        types.InlineKeyboardButton("⏹ Stop", callback_data=f"stop:{bot_id}")
    )
    kb.row(
        types.InlineKeyboardButton("🔄 Restart", callback_data=f"restart:{bot_id}"),
        types.InlineKeyboardButton("📄 Logs", callback_data=f"logs:{bot_id}")
    )
    return kb

def send_home(message):
    touch_user(message.from_user)
    if is_banned(message.from_user.id):
        return bot.reply_to(message, "আপনার অ্যাকাউন্ট বন্ধ করা হয়েছে।")
    bot.send_message(message.chat.id,
        "🚀 <b>Bot Hosting Manager</b>\n\nPython bot ফাইল আপলোড করে Manage করুন।",
        reply_markup=main_keyboard(message.from_user.id))

@bot.message_handler(commands=["start", "help"])
def start_cmd(message):
    send_home(message)

@bot.message_handler(commands=["id"])
def id_cmd(message):
    bot.reply_to(message, f"Your Telegram ID: <code>{message.from_user.id}</code>")

@bot.message_handler(func=lambda m: True, content_types=["text"])
def text_handler(message):
    uid, text_value = message.from_user.id, (message.text or "").strip()
    touch_user(message.from_user)
    if is_banned(uid):
        return bot.reply_to(message, "আপনার অ্যাকাউন্ট বন্ধ করা হয়েছে।")
    if text_value == "📤 Upload Bot":
        WAITING[uid] = {"step": "file"}
        return bot.reply_to(message, "আপনার Bot-এর <b>.py</b> অথবা <b>.zip</b> ফাইল পাঠান। সর্বোচ্চ "
                            f"{MAX_UPLOAD_MB} MB। ZIP-এর মধ্যে main.py এবং ঐচ্ছিক requirements.txt রাখুন।")
    if text_value == "🤖 My Bots":
        rows = user_bots(uid)
        if not rows:
            return bot.reply_to(message, "আপনার কোনো Bot নেই। 📤 Upload Bot চাপুন।")
        for row in rows:
            bot.send_message(message.chat.id,
                f"🤖 <b>Bot #{row['id']}</b> — {row['name']}\n"
                f"Status: <b>{row['status']}</b>\nEntry: <code>{row['entrypoint']}</code>",
                reply_markup=bot_actions_keyboard(row["id"]))
        return
    if text_value == "📊 My Account":
        rows = user_bots(uid)
        running = sum(1 for r in rows if r["status"] == "running")
        return bot.reply_to(message, f"🆔 <code>{uid}</code>\nTotal Bots: {len(rows)}\nRunning: {running}\n"
                            f"Bot limit: {MAX_BOTS_PER_USER}")
    if text_value == "❓ Help":
        return bot.reply_to(message,
            "📌 <b>ব্যবহার:</b>\n1. Upload Bot চাপুন\n2. .py/.zip পাঠান\n"
            "3. Bot Token পাঠান\n4. My Bots থেকে Start/Stop/Logs ব্যবহার করুন\n\n"
            "আপনার bot code-এ token hardcode না করে os.getenv('BOT_TOKEN') ব্যবহার করুন।")
    if text_value == "🛠 Admin Panel" and is_admin(uid):
        return admin_panel(message.chat.id)
    if text_value.startswith("/"):
        return bot.reply_to(message, "এই কমান্ডটি পাওয়া যায়নি। /start লিখুন।")
    state = WAITING.get(uid)
    if state and state.get("step") == "token":
        token = text_value
        if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", token):
            return bot.reply_to(message, "Token format ঠিক নয়। BotFather থেকে পাওয়া token পাঠান।")
        bot_id = state["bot_id"]
        with db() as con:
            con.execute("UPDATE hosted_bots SET token=? WHERE id=? AND owner_id=?", (token, bot_id, uid))
        WAITING.pop(uid, None)
        row = get_bot(bot_id)
        ok, result = start_hosted_bot(row)
        return bot.reply_to(message, result, reply_markup=bot_actions_keyboard(bot_id))
    return bot.reply_to(message, "মেনু থেকে একটি অপশন বেছে নিন। /start")


@bot.message_handler(content_types=["document"])
def document_handler(message):
    uid = message.from_user.id
    touch_user(message.from_user)
    if is_banned(uid):
        return
    state = WAITING.get(uid)
    if not state or state.get("step") != "file":
        return bot.reply_to(message, "প্রথমে 📤 Upload Bot চাপুন।")
    rows = user_bots(uid)
    if len(rows) >= MAX_BOTS_PER_USER and not is_admin(uid):
        WAITING.pop(uid, None)
        return bot.reply_to(message, f"আপনার সর্বোচ্চ Bot limit হলো {MAX_BOTS_PER_USER}।")
    doc = message.document
    filename = Path(doc.file_name or "upload").name
    ext = Path(filename).suffix.lower()
    if ext not in (".py", ".zip"):
        return bot.reply_to(message, "শুধু .py অথবা .zip ফাইল গ্রহণ করা হয়।")
    if doc.file_size > MAX_UPLOAD_MB * 1024 * 1024:
        return bot.reply_to(message, f"ফাইল {MAX_UPLOAD_MB} MB-এর বেশি হতে পারবে না।")
    try:
        info = bot.get_file(doc.file_id)
        downloaded = bot.download_file(info.file_path)
        bot_id = int(time.time() * 1000) % 2_000_000_000
        folder = UPLOAD_DIR / str(uid) / str(bot_id)
        folder.mkdir(parents=True, exist_ok=True)
        saved = folder / filename
        saved.write_bytes(downloaded)
        if ext == ".zip":
            safe_extract(saved, folder)
            saved.unlink(missing_ok=True)
            entry = find_entrypoint(folder)
        else:
            entry = saved
        relative_entry = str(entry.relative_to(folder))
        name = safe_name(Path(filename).stem)
        with db() as con:
            cur = con.execute("""INSERT INTO hosted_bots
                (owner_id,name,folder,entrypoint,token,status,created_at)
                VALUES(?,?,?,?,?,'awaiting_token',?)""",
                (uid, name, str(folder), relative_entry, "", datetime.utcnow().isoformat()))
            real_id = cur.lastrowid
        WAITING[uid] = {"step": "token", "bot_id": real_id}
        bot.reply_to(message,
            f"✅ ফাইল আপলোড হয়েছে। Bot ID: <code>{real_id}</code>\n\n"
            "এখন BotFather থেকে তৈরি করা Bot Token পাঠান। Token গোপন রাখুন।")
    except Exception as exc:
        logger.exception("Upload failed")
        bot.reply_to(message, f"Upload error: <code>{str(exc)[:1000]}</code>")


# ---------------- CALLBACKS ----------------
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    uid = call.from_user.id
    try:
        action, raw_id = call.data.split(":", 1)
        bot_id = int(raw_id)
    except Exception:
        return bot.answer_callback_query(call.id, "Invalid action")
    row = get_bot(bot_id)
    if not authorized_owner(uid, row):
        return bot.answer_callback_query(call.id, "এটি আপনার Bot নয়।", show_alert=True)
    bot.answer_callback_query(call.id)
    if action == "start":
        ok, result = start_hosted_bot(row)
        bot.send_message(call.message.chat.id, result)
    elif action == "stop":
        ok, result = stop_hosted_bot(bot_id)
        bot.send_message(call.message.chat.id, result)
    elif action == "restart":
        stop_hosted_bot(bot_id)
        time.sleep(1)
        ok, result = start_hosted_bot(get_bot(bot_id))
        bot.send_message(call.message.chat.id, result)
    elif action == "logs":
        output = tail_log(bot_id)
        bot.send_message(call.message.chat.id, f"<b>Logs — Bot #{bot_id}</b>\n<pre>{html_escape(output)}</pre>")

def html_escape(value):
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# ---------------- ADMIN ----------------
def admin_panel(chat_id):
    with db() as con:
        users = con.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        bots = con.execute("SELECT COUNT(*) n FROM hosted_bots").fetchone()["n"]
        running = con.execute("SELECT COUNT(*) n FROM hosted_bots WHERE status='running'").fetchone()["n"]
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("👥 Users", callback_data="admin_users"))
    bot.send_message(chat_id,
        f"🛠 <b>Admin Panel</b>\nUsers: {users}\nBots: {bots}\nRunning: {running}\n\n"
        "Commands:\n/ban USER_ID\n/unban USER_ID\n/users\n/allbots",
        reply_markup=kb)

@bot.message_handler(commands=["users", "allbots"])
def admin_list(message):
    if not is_admin(message.from_user.id):
        return
    with db() as con:
        if message.text.startswith("/users"):
            rows = con.execute("SELECT user_id,username,banned FROM users ORDER BY joined_at DESC LIMIT 50").fetchall()
            out = ["👥 <b>Users (latest 50)</b>"]
            out += [f"{r['user_id']} @{html_escape(r['username'] or '')} | banned={r['banned']}" for r in rows]
        else:
            rows = con.execute("SELECT id,owner_id,name,status FROM hosted_bots ORDER BY id DESC LIMIT 50").fetchall()
            out = ["🤖 <b>Bots (latest 50)</b>"]
            out += [f"#{r['id']} owner={r['owner_id']} {html_escape(r['name'])} | {r['status']}" for r in rows]
    bot.reply_to(message, "\n".join(out)[:4000])

@bot.message_handler(commands=["ban", "unban"])
def admin_ban(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        return bot.reply_to(message, "ব্যবহার: /ban USER_ID অথবা /unban USER_ID")
    target = int(parts[1])
    banned = 1 if message.text.startswith("/ban") else 0
    with db() as con:
        con.execute("UPDATE users SET banned=? WHERE user_id=?", (banned, target))
    if banned:
        with db() as con:
            ids = [r["id"] for r in con.execute("SELECT id FROM hosted_bots WHERE owner_id=?", (target,))]
        for bid in ids:
            stop_hosted_bot(bid)
    bot.reply_to(message, f"User {target}: {'Banned' if banned else 'Unbanned'}")

@bot.callback_query_handler(func=lambda call: call.data == "admin_users")
def admin_users_callback(call):
    if not is_admin(call.from_user.id):
        return bot.answer_callback_query(call.id, "Not allowed", show_alert=True)
    bot.answer_callback_query(call.id)
    with db() as con:
        rows = con.execute("SELECT user_id,username,banned FROM users ORDER BY joined_at DESC LIMIT 30").fetchall()
    lines = ["👥 <b>Latest users</b>"]
    lines += [f"<code>{r['user_id']}</code> @{html_escape(r['username'] or '')} | banned={r['banned']}" for r in rows]
    bot.send_message(call.message.chat.id, "\n".join(lines)[:4000])


# ---------------- STARTUP ----------------
def reconcile_processes():
    # Processes do not survive a host restart. Mark stale database statuses stopped.
    with db() as con:
        con.execute("UPDATE hosted_bots SET status='stopped', pid=NULL WHERE status='running'")

def main():
    init_db()
    reconcile_processes()
    logger.info("Hosting manager started")
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)

if __name__ == "__main__":
    main()
