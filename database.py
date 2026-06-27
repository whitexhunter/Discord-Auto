# database.py (enhanced)
import sqlite3
import secrets
import string
import json
import os
import zipfile
from datetime import datetime, timedelta
import threading
import bcrypt

DB_LOCK = threading.Lock()
DATABASE = 'discord_bot.db'

def generate_license_key(length=24):
    alphabet = string.ascii_uppercase + string.digits
    segments = []
    for i in range(0, length, 6):
        segment = ''.join(secrets.choice(alphabet) for _ in range(min(6, length - i)))
        segments.append(segment)
    return '-'.join(segments)

def init_db():
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        
        c.execute('''CREATE TABLE IF NOT EXISTS licenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE NOT NULL,
            max_accounts INTEGER NOT NULL DEFAULT 1,
            days_valid INTEGER NOT NULL DEFAULT 30,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_by TEXT NOT NULL,
            notes TEXT DEFAULT '',
            auto_responder_enabled INTEGER NOT NULL DEFAULT 0,
            max_auto_responders INTEGER NOT NULL DEFAULT 1
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id TEXT,
            license_key TEXT NOT NULL,
            discord_token TEXT NOT NULL,
            password_hash TEXT,
            created_at TEXT NOT NULL,
            last_login TEXT,
            max_accounts INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (license_key) REFERENCES licenses(key)
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            job_name TEXT NOT NULL,
            channel_ids TEXT NOT NULL,
            message_content TEXT NOT NULL,
            interval_seconds INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            total_sent INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_run TEXT,
            token_used TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS auto_responders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            trigger_keyword TEXT DEFAULT '',
            response_message TEXT NOT NULL,
            reply_to_new_dms INTEGER NOT NULL DEFAULT 1,
            reply_to_mentions INTEGER NOT NULL DEFAULT 0,
            cooldown_seconds INTEGER NOT NULL DEFAULT 60,
            status TEXT NOT NULL DEFAULT 'active',
            total_replies INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )''')
        
        conn.commit()
        conn.close()

# ===== License Operations =====
def create_licenses(num_keys, max_accounts, days_valid, admin_username, notes="", 
                    auto_responder_enabled=False, max_auto_responders=1):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        keys = []
        
        for _ in range(num_keys):
            key = generate_license_key()
            created_at = datetime.now().isoformat()
            expires_at = (datetime.now() + timedelta(days=days_valid)).isoformat()
            
            c.execute('''INSERT INTO licenses (key, max_accounts, days_valid, created_at, 
                         expires_at, active, created_by, notes, auto_responder_enabled, 
                         max_auto_responders)
                         VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)''',
                      (key, max_accounts, days_valid, created_at, expires_at, 
                       admin_username, notes, 1 if auto_responder_enabled else 0, 
                       max_auto_responders))
            keys.append(key)
        
        conn.commit()
        conn.close()
        return keys

def validate_license(key):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT * FROM licenses WHERE key=? AND active=1", (key,))
        lic = c.fetchone()
        conn.close()
        
        if not lic:
            return False, "License key not found or deactivated"
        
        expires_at = datetime.fromisoformat(lic[5])
        if expires_at < datetime.now():
            return False, "License has expired"
        
        return True, lic

def get_license_by_key(key):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT * FROM licenses WHERE key=?", (key,))
        lic = c.fetchone()
        conn.close()
        return lic

def get_all_licenses():
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT * FROM licenses ORDER BY created_at DESC")
        licenses = c.fetchall()
        conn.close()
        return licenses

def update_license_days(key, additional_days):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT * FROM licenses WHERE key=?", (key,))
        lic = c.fetchone()
        if not lic:
            conn.close()
            return False, "License not found"
        
        current_expiry = datetime.fromisoformat(lic[5])
        now = datetime.now()
        
        if current_expiry < now:
            new_expiry = now + timedelta(days=additional_days)
        else:
            new_expiry = current_expiry + timedelta(days=additional_days)
        
        new_days_valid = lic[3] + additional_days
        
        c.execute("UPDATE licenses SET expires_at=?, days_valid=? WHERE key=?",
                  (new_expiry.isoformat(), new_days_valid, key))
        conn.commit()
        conn.close()
        return True, f"Extended by {additional_days} days"

def update_license_accounts(key, new_max_accounts):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        lic = get_license_by_key(key)
        if not lic:
            conn.close()
            return False, "License not found"
        if new_max_accounts < 1:
            conn.close()
            return False, "Must be at least 1"
        c.execute("UPDATE licenses SET max_accounts=? WHERE key=?", (new_max_accounts, key))
        conn.commit()
        conn.close()
        return True, f"Max accounts updated to {new_max_accounts}"

def deactivate_license(key):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("UPDATE licenses SET active=0 WHERE key=?", (key,))
        conn.commit()
        conn.close()

def activate_license(key):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("UPDATE licenses SET active=1 WHERE key=?", (key,))
        conn.commit()
        conn.close()

# ===== User Operations =====
def register_user(license_key, discord_token, password=None):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        
        # Validate license
        valid, lic = validate_license(license_key)
        if not valid:
            conn.close()
            return False, lic
        
        # Check account limit
        c.execute("SELECT COUNT(*) FROM users WHERE license_key=?", (license_key,))
        count = c.fetchone()[0]
        if count >= lic[2]:
            conn.close()
            return False, f"License max accounts reached ({lic[2]})"
        
        # Check if token already registered
        c.execute("SELECT * FROM users WHERE discord_token=?", (discord_token,))
        existing = c.fetchone()
        if existing:
            conn.close()
            return False, "This Discord token is already registered"
        
        # Hash password if provided
        password_hash = None
        if password:
            password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        
        c.execute('''INSERT INTO users (discord_id, license_key, discord_token, password_hash, 
                     created_at, last_login, max_accounts)
                     VALUES (?, ?, ?, ?, ?, ?, ?)''',
                  (None, license_key, discord_token, password_hash, 
                   datetime.now().isoformat(), datetime.now().isoformat(), lic[2]))
        
        user_id = c.lastrowid
        conn.commit()
        conn.close()
        return True, {"user_id": user_id, "message": "Account created"}

def get_user_by_id(user_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE id=?", (user_id,))
        user = c.fetchone()
        conn.close()
        return user

def get_user_tokens(user_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT id, discord_token FROM users WHERE id=?", (user_id,))
        user = c.fetchone()
        conn.close()
        if user:
            return [{"id": user[0], "token": user[2][:15] + "..." + user[2][-4:], "full_token": user[2]}]
        return []

def get_all_users():
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('''SELECT u.id, u.discord_id, u.license_key, u.discord_token, u.created_at,
                            l.expires_at, l.max_accounts, l.active
                     FROM users u
                     JOIN licenses l ON u.license_key = l.key
                     ORDER BY u.created_at DESC''')
        users = c.fetchall()
        conn.close()
        return users

# ===== Job Operations =====
def create_job(user_id, job_name, channel_ids_str, message_content, interval_seconds, token_used=None):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        
        c.execute('''INSERT INTO jobs (user_id, job_name, channel_ids, message_content, 
                     interval_seconds, status, total_sent, created_at, token_used)
                     VALUES (?, ?, ?, ?, ?, 'running', 0, ?, ?)''',
                  (user_id, job_name, channel_ids_str, message_content, 
                   interval_seconds, datetime.now().isoformat(), token_used))
        
        job_id = c.lastrowid
        conn.commit()
        conn.close()
        return job_id

def get_user_jobs(user_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT * FROM jobs WHERE user_id=? ORDER BY created_at DESC", (user_id,))
        jobs = c.fetchall()
        conn.close()
        return jobs

def get_job(job_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
        job = c.fetchone()
        conn.close()
        return job

def update_job(job_id, user_id, job_name, channel_ids, message_content, interval_seconds):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('''UPDATE jobs SET job_name=?, channel_ids=?, message_content=?, 
                     interval_seconds=? WHERE id=? AND user_id=?''',
                  (job_name, channel_ids, message_content, interval_seconds, job_id, user_id))
        conn.commit()
        conn.close()

def update_job_status(job_id, status):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
        conn.commit()
        conn.close()

def increment_job_sent(job_id, count):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("UPDATE jobs SET total_sent = total_sent + ?, last_run = ? WHERE id=?",
                  (count, datetime.now().isoformat(), job_id))
        conn.commit()
        conn.close()

def delete_job(job_id, user_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("DELETE FROM jobs WHERE id=? AND user_id=?", (job_id, user_id))
        conn.commit()
        conn.close()

def get_all_running_jobs():
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT * FROM jobs WHERE status='running'")
        jobs = c.fetchall()
        conn.close()
        return jobs

# ===== Auto Responder Operations =====
def create_auto_responder(user_id, name, response_message, trigger_keyword="",
                          reply_to_new_dms=True, reply_to_mentions=False, cooldown_seconds=60):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        
        # Check if user's license allows auto-responders
        c.execute('''SELECT l.auto_responder_enabled, l.max_auto_responders 
                     FROM licenses l JOIN users u ON u.license_key = l.key 
                     WHERE u.id=?''', (user_id,))
        lic_info = c.fetchone()
        
        if not lic_info or not lic_info[0]:
            conn.close()
            return False, "Your license does not support auto-responders"
        
        # Check max auto-responders
        c.execute("SELECT COUNT(*) FROM auto_responders WHERE user_id=?", (user_id,))
        count = c.fetchone()[0]
        if count >= lic_info[1]:
            conn.close()
            return False, f"Max auto-responders reached ({lic_info[1]})"
        
        c.execute('''INSERT INTO auto_responders (user_id, name, trigger_keyword, response_message,
                     reply_to_new_dms, reply_to_mentions, cooldown_seconds, status, total_replies, created_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?, 'active', 0, ?)''',
                  (user_id, name, trigger_keyword, response_message,
                   1 if reply_to_new_dms else 0, 1 if reply_to_mentions else 0, 
                   cooldown_seconds, datetime.now().isoformat()))
        
        responder_id = c.lastrowid
        conn.commit()
        conn.close()
        return True, {"responder_id": responder_id}

def get_user_auto_responders(user_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT * FROM auto_responders WHERE user_id=? ORDER BY created_at DESC", (user_id,))
        responders = c.fetchall()
        conn.close()
        return responders

def get_auto_responder(responder_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT * FROM auto_responders WHERE id=?", (responder_id,))
        responder = c.fetchone()
        conn.close()
        return responder

def update_auto_responder(responder_id, user_id, name, response_message, trigger_keyword,
                          reply_to_new_dms, reply_to_mentions, cooldown_seconds):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('''UPDATE auto_responders SET name=?, trigger_keyword=?, response_message=?,
                     reply_to_new_dms=?, reply_to_mentions=?, cooldown_seconds=?
                     WHERE id=? AND user_id=?''',
                  (name, trigger_keyword, response_message, 
                   1 if reply_to_new_dms else 0, 1 if reply_to_mentions else 0,
                   cooldown_seconds, responder_id, user_id))
        conn.commit()
        conn.close()

def update_auto_responder_status(responder_id, status):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("UPDATE auto_responders SET status=? WHERE id=?", (status, responder_id))
        conn.commit()
        conn.close()

def increment_responder_replies(responder_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("UPDATE auto_responders SET total_replies = total_replies + 1 WHERE id=?", (responder_id,))
        conn.commit()
        conn.close()

def delete_auto_responder(responder_id, user_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("DELETE FROM auto_responders WHERE id=? AND user_id=?", (responder_id, user_id))
        conn.commit()
        conn.close()

# ===== Backup =====
def export_full_backup():
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        
        backup_data = {'version': '2.0', 'exported_at': datetime.now().isoformat(), 'data': {}}
        
        c.execute("SELECT * FROM licenses")
        backup_data['data']['licenses'] = [
            {'key': r[1], 'max_accounts': r[2], 'days_valid': r[3], 'created_at': r[4],
             'expires_at': r[5], 'active': r[6], 'created_by': r[7], 'notes': r[8] if len(r) > 8 else '',
             'auto_responder_enabled': r[9] if len(r) > 9 else 0,
             'max_auto_responders': r[10] if len(r) > 10 else 1}
            for r in c.fetchall()
        ]
        
        c.execute("SELECT * FROM users")
        backup_data['data']['users'] = [
            {'id': r[0], 'discord_id': r[1], 'license_key': r[2], 'discord_token': r[3],
             'password_hash': r[4], 'created_at': r[5], 'last_login': r[6], 'max_accounts': r[7]}
            for r in c.fetchall()
        ]
        
        c.execute("SELECT * FROM jobs")
        backup_data['data']['jobs'] = [
            {'user_id': r[1], 'job_name': r[2], 'channel_ids': r[3], 'message_content': r[4],
             'interval_seconds': r[5], 'status': r[6], 'total_sent': r[7], 'created_at': r[8],
             'last_run': r[9], 'token_used': r[10]}
            for r in c.fetchall()
        ]
        
        c.execute("SELECT * FROM auto_responders")
        backup_data['data']['auto_responders'] = [
            {'user_id': r[1], 'name': r[2], 'trigger_keyword': r[3], 'response_message': r[4],
             'reply_to_new_dms': r[5], 'reply_to_mentions': r[6], 'cooldown_seconds': r[7],
             'status': r[8], 'total_replies': r[9], 'created_at': r[10]}
            for r in c.fetchall()
        ]
        
        conn.close()
        return backup_data

def restore_from_backup(backup_data):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        
        c.execute("DELETE FROM auto_responders")
        c.execute("DELETE FROM jobs")
        c.execute("DELETE FROM users")
        c.execute("DELETE FROM licenses")
        
        for lic in backup_data['data']['licenses']:
            c.execute('''INSERT INTO licenses (key, max_accounts, days_valid, created_at, 
                         expires_at, active, created_by, notes, auto_responder_enabled, max_auto_responders)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                      (lic['key'], lic['max_accounts'], lic['days_valid'], lic['created_at'],
                       lic['expires_at'], lic['active'], lic['created_by'], lic.get('notes', ''),
                       lic.get('auto_responder_enabled', 0), lic.get('max_auto_responders', 1)))
        
        for user in backup_data['data']['users']:
            c.execute('''INSERT INTO users (id, discord_id, license_key, discord_token, 
                         password_hash, created_at, last_login, max_accounts)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                      (user['id'], user['discord_id'], user['license_key'], user['discord_token'],
                       user.get('password_hash'), user['created_at'], user['last_login'], user['max_accounts']))
        
        for job in backup_data['data']['jobs']:
            c.execute('''INSERT INTO jobs (user_id, job_name, channel_ids, message_content,
                         interval_seconds, status, total_sent, created_at, last_run, token_used)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                      (job['user_id'], job['job_name'], job['channel_ids'], job['message_content'],
                       job['interval_seconds'], 'stopped', job['total_sent'], job['created_at'],
                       job['last_run'], job.get('token_used')))
        
        for ar in backup_data['data']['auto_responders']:
            c.execute('''INSERT INTO auto_responders (user_id, name, trigger_keyword, response_message,
                         reply_to_new_dms, reply_to_mentions, cooldown_seconds, status, total_replies, created_at)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                      (ar['user_id'], ar['name'], ar['trigger_keyword'], ar['response_message'],
                       ar['reply_to_new_dms'], ar['reply_to_mentions'], ar['cooldown_seconds'],
                       ar['status'], ar['total_replies'], ar['created_at']))
        
        conn.commit()
        conn.close()
        return {'success': True}
