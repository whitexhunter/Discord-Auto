# database.py
import sqlite3
import secrets
import string
import json
import os
import zipfile
from datetime import datetime, timedelta
import threading

DB_LOCK = threading.Lock()
DATABASE = 'discord_bot.db'
BACKUP_DIR = 'backups'

def generate_license_key(length=24):
    alphabet = string.ascii_uppercase + string.digits
    segments = []
    for i in range(0, length, 6):
        segment = ''.join(secrets.choice(alphabet) for _ in range(min(6, length - i)))
        segments.append(segment)
    return '-'.join(segments)

def init_db():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        
        c.execute('''CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id TEXT UNIQUE NOT NULL
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS licenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE NOT NULL,
            max_accounts INTEGER NOT NULL DEFAULT 1,
            days_valid INTEGER NOT NULL DEFAULT 30,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_by TEXT NOT NULL,
            notes TEXT DEFAULT ''
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id TEXT NOT NULL,
            license_key TEXT NOT NULL,
            discord_token TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_login TEXT,
            FOREIGN KEY (license_key) REFERENCES licenses(key)
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_discord_id TEXT NOT NULL,
            job_name TEXT NOT NULL,
            channel_ids TEXT NOT NULL,
            message_content TEXT NOT NULL,
            interval_seconds INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            total_sent INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_run TEXT
        )''')
        
        conn.commit()
        conn.close()

# ===== License Operations =====

def create_licenses(num_keys, max_accounts, days_valid, admin_discord_id, notes=""):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        keys = []
        
        for _ in range(num_keys):
            key = generate_license_key()
            created_at = datetime.now().isoformat()
            expires_at = (datetime.now() + timedelta(days=days_valid)).isoformat()
            
            c.execute('''INSERT INTO licenses (key, max_accounts, days_valid, created_at, expires_at, active, created_by, notes)
                         VALUES (?, ?, ?, ?, ?, 1, ?, ?)''',
                      (key, max_accounts, days_valid, created_at, expires_at, admin_discord_id, notes))
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

def get_license_account_count(key):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users WHERE license_key=?", (key,))
        count = c.fetchone()[0]
        conn.close()
        return count

# ===== License Editing Functions =====

def update_license_days(key, additional_days):
    """Add extra days to a license"""
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
        
        return True, f"License extended by {additional_days} days. New expiry: {new_expiry.strftime('%Y-%m-%d')}"

def update_license_accounts(key, new_max_accounts):
    """Change max accounts for a license"""
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        
        c.execute("SELECT * FROM licenses WHERE key=?", (key,))
        lic = c.fetchone()
        if not lic:
            conn.close()
            return False, "License not found"
        
        current_count = get_license_account_count(key)
        if new_max_accounts < current_count:
            conn.close()
            return False, f"Cannot reduce to {new_max_accounts} — there are already {current_count} accounts registered"
        
        c.execute("UPDATE licenses SET max_accounts=? WHERE key=?", (new_max_accounts, key))
        conn.commit()
        conn.close()
        
        return True, f"Max accounts updated from {lic[2]} to {new_max_accounts}"

def set_license_expiry_date(key, new_expiry_date_str):
    """Set a specific expiry date (YYYY-MM-DD format)"""
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        
        c.execute("SELECT * FROM licenses WHERE key=?", (key,))
        lic = c.fetchone()
        if not lic:
            conn.close()
            return False, "License not found"
        
        try:
            new_date = datetime.strptime(new_expiry_date_str, '%Y-%m-%d')
            new_expiry = new_date.replace(hour=23, minute=59, second=59)
        except ValueError:
            conn.close()
            return False, "Invalid date format. Use YYYY-MM-DD"
        
        days_left = (new_expiry - datetime.now()).days
        if days_left < 0:
            days_left = 0
        
        c.execute("UPDATE licenses SET expires_at=?, days_valid=? WHERE key=?",
                  (new_expiry.isoformat(), max(1, days_left), key))
        conn.commit()
        conn.close()
        
        return True, f"Expiry set to {new_expiry_date_str} ({max(1, days_left)} days from now)"

def toggle_license_active(key, active_state):
    """Activate or deactivate a license"""
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("UPDATE licenses SET active=? WHERE key=?", (1 if active_state else 0, key))
        conn.commit()
        conn.close()
        
        state = "activated" if active_state else "deactivated"
        return True, f"License {state}"

# ===== User Operations =====

def register_or_login(discord_id, license_key, discord_token):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        
        c.execute("SELECT * FROM users WHERE discord_id=? AND discord_token=?", (discord_id, discord_token))
        user = c.fetchone()
        
        if user:
            c.execute("UPDATE users SET last_login=? WHERE id=?", 
                      (datetime.now().isoformat(), user[0]))
            conn.commit()
            conn.close()
            return True, "Logged in successfully"
        
        c.execute("SELECT * FROM users WHERE discord_id=?", (discord_id,))
        existing = c.fetchone()
        if existing:
            conn.close()
            return False, "You already have a registered account with a different token"
        
        c.execute('''INSERT INTO users (discord_id, license_key, discord_token, created_at, last_login)
                     VALUES (?, ?, ?, ?, ?)''',
                  (discord_id, license_key, discord_token, datetime.now().isoformat(), datetime.now().isoformat()))
        conn.commit()
        conn.close()
        return True, "Account created and logged in"

def get_user(discord_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE discord_id=?", (discord_id,))
        user = c.fetchone()
        conn.close()
        return user

def get_license_info_for_user(discord_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('''SELECT l.key, l.max_accounts, l.days_valid, l.created_at, l.expires_at, l.active, l.notes
                     FROM licenses l
                     JOIN users u ON u.license_key = l.key
                     WHERE u.discord_id=?''', (discord_id,))
        lic = c.fetchone()
        conn.close()
        return lic

# ===== Job Operations =====

def create_job(discord_id, job_name, channel_ids_str, message_content, interval_seconds):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        
        c.execute('''INSERT INTO jobs (user_discord_id, job_name, channel_ids, message_content, 
                     interval_seconds, status, total_sent, created_at)
                     VALUES (?, ?, ?, ?, ?, 'running', 0, ?)''',
                  (discord_id, job_name, channel_ids_str, message_content, 
                   interval_seconds, datetime.now().isoformat()))
        
        job_id = c.lastrowid
        conn.commit()
        conn.close()
        return job_id

def get_user_jobs(discord_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT * FROM jobs WHERE user_discord_id=? ORDER BY created_at DESC", (discord_id,))
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

def delete_job(job_id, discord_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("DELETE FROM jobs WHERE id=? AND user_discord_id=?", (job_id, discord_id))
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

# ===== Admin Operations =====

def is_admin(discord_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT * FROM admins WHERE discord_id=?", (discord_id,))
        admin = c.fetchone()
        conn.close()
        return admin is not None

def add_admin(discord_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO admins (discord_id) VALUES (?)", (discord_id,))
        conn.commit()
        conn.close()

def get_all_licenses():
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT * FROM licenses ORDER BY created_at DESC")
        licenses = c.fetchall()
        conn.close()
        return licenses

def get_license_by_key(key):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT * FROM licenses WHERE key=?", (key,))
        lic = c.fetchone()
        conn.close()
        return lic

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

def deactivate_license(key):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("UPDATE licenses SET active=0 WHERE key=?", (key,))
        conn.commit()
        conn.close()

def delete_license_complete(key):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute('''SELECT discord_id FROM users WHERE license_key=?''', (key,))
        user_ids = [row[0] for row in c.fetchall()]
        for uid in user_ids:
            c.execute("UPDATE jobs SET status='deleted' WHERE user_discord_id=?", (uid,))
        
        c.execute("DELETE FROM jobs WHERE user_discord_id IN (SELECT discord_id FROM users WHERE license_key=?)", (key,))
        c.execute("DELETE FROM users WHERE license_key=?", (key,))
        c.execute("DELETE FROM licenses WHERE key=?", (key,))
        conn.commit()
        conn.close()

# ===================================================================
# BACKUP & RESTORE SYSTEM
# ===================================================================

def export_full_backup():
    """Export entire database to a JSON backup data structure"""
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        
        backup_data = {
            'version': '1.0',
            'exported_at': datetime.now().isoformat(),
            'data': {}
        }
        
        # Export admins
        c.execute("SELECT * FROM admins")
        backup_data['data']['admins'] = [
            {'discord_id': row[1]} for row in c.fetchall()
        ]
        
        # Export licenses
        c.execute("SELECT * FROM licenses")
        backup_data['data']['licenses'] = [
            {
                'key': row[1],
                'max_accounts': row[2],
                'days_valid': row[3],
                'created_at': row[4],
                'expires_at': row[5],
                'active': row[6],
                'created_by': row[7],
                'notes': row[8] if len(row) > 8 else ''
            }
            for row in c.fetchall()
        ]
        
        # Export users (WITH tokens)
        c.execute("SELECT * FROM users")
        backup_data['data']['users'] = [
            {
                'discord_id': row[1],
                'license_key': row[2],
                'discord_token': row[3],
                'created_at': row[4],
                'last_login': row[5]
            }
            for row in c.fetchall()
        ]
        
        # Export jobs
        c.execute("SELECT * FROM jobs")
        backup_data['data']['jobs'] = [
            {
                'user_discord_id': row[1],
                'job_name': row[2],
                'channel_ids': row[3],
                'message_content': row[4],
                'interval_seconds': row[5],
                'status': row[6],
                'total_sent': row[7],
                'created_at': row[8],
                'last_run': row[9]
            }
            for row in c.fetchall()
        ]
        
        conn.close()
    
    return backup_data

def save_backup_to_file(filename=None):
    """Save backup as a JSON file (and optionally zip it)"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    
    if not filename:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"backup_{timestamp}"
    
    backup_data = export_full_backup()
    
    # Save JSON
    json_path = os.path.join(BACKUP_DIR, f"{filename}.json")
    with open(json_path, 'w') as f:
        json.dump(backup_data, f, indent=2)
    
    # Create ZIP with manifest
    zip_path = os.path.join(BACKUP_DIR, f"{filename}.zip")
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        manifest = {
            'filename': f"{filename}.json",
            'exported_at': backup_data['exported_at'],
            'version': backup_data['version'],
            'stats': {
                'admins': len(backup_data['data']['admins']),
                'licenses': len(backup_data['data']['licenses']),
                'users': len(backup_data['data']['users']),
                'jobs': len(backup_data['data']['jobs'])
            }
        }
        zf.writestr('manifest.json', json.dumps(manifest, indent=2))
        zf.write(json_path, arcname=f"{filename}.json")
    
    return {
        'json_path': json_path,
        'zip_path': zip_path,
        'filename': filename,
        'stats': {
            'admins': len(backup_data['data']['admins']),
            'licenses': len(backup_data['data']['licenses']),
            'users': len(backup_data['data']['users']),
            'jobs': len(backup_data['data']['jobs'])
        },
        'exported_at': backup_data['exported_at']
    }

def restore_from_backup(backup_data):
    """Restore entire database from backup JSON data.
    WARNING: This will REPLACE all current data.
    """
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        
        # Clear all existing data
        c.execute("DELETE FROM jobs")
        c.execute("DELETE FROM users")
        c.execute("DELETE FROM licenses")
        c.execute("DELETE FROM admins")
        
        # Restore admins
        for admin in backup_data['data']['admins']:
            c.execute("INSERT INTO admins (discord_id) VALUES (?)",
                      (admin['discord_id'],))
        
        # Restore licenses
        for lic in backup_data['data']['licenses']:
            c.execute('''INSERT INTO licenses (key, max_accounts, days_valid, created_at, 
                         expires_at, active, created_by, notes)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                      (lic['key'], lic['max_accounts'], lic['days_valid'],
                       lic['created_at'], lic['expires_at'], lic['active'],
                       lic['created_by'], lic.get('notes', '')))
        
        # Restore users
        for user in backup_data['data']['users']:
            c.execute('''INSERT INTO users (discord_id, license_key, discord_token, created_at, last_login)
                         VALUES (?, ?, ?, ?, ?)''',
                      (user['discord_id'], user['license_key'], user['discord_token'],
                       user['created_at'], user['last_login']))
        
        # Restore jobs (set to stopped so they don't auto-start during restore)
        for job in backup_data['data']['jobs']:
            c.execute('''INSERT INTO jobs (user_discord_id, job_name, channel_ids, message_content,
                         interval_seconds, status, total_sent, created_at, last_run)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                      (job['user_discord_id'], job['job_name'], job['channel_ids'],
                       job['message_content'], job['interval_seconds'], 'stopped',
                       job['total_sent'], job['created_at'], job['last_run']))
        
        conn.commit()
        conn.close()
    
    return {
        'admins_restored': len(backup_data['data']['admins']),
        'licenses_restored': len(backup_data['data']['licenses']),
        'users_restored': len(backup_data['data']['users']),
        'jobs_restored': len(backup_data['data']['jobs'])
    }

def list_backups():
    """List all available backup files"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    backups = []
    
    for f in os.listdir(BACKUP_DIR):
        if f.endswith('.json'):
            path = os.path.join(BACKUP_DIR, f)
            try:
                with open(path) as fh:
                    data = json.load(fh)
                backups.append({
                    'filename': f,
                    'exported_at': data.get('exported_at', 'unknown'),
                    'stats': {
                        'admins': len(data['data']['admins']),
                        'licenses': len(data['data']['licenses']),
                        'users': len(data['data']['users']),
                        'jobs': len(data['data']['jobs'])
                    },
                    'size': os.path.getsize(path)
                })
            except:
                backups.append({
                    'filename': f,
                    'exported_at': 'corrupt/invalid',
                    'stats': None,
                    'size': os.path.getsize(path)
                })
    
    return sorted(backups, key=lambda x: x['filename'], reverse=True)

def load_backup_from_file(filename):
    """Load a backup JSON file and return the data"""
    path = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(path):
        path2 = os.path.join(BACKUP_DIR, f"{filename}.json")
        if os.path.exists(path2):
            path = path2
        else:
            # Try without extension
            for f in os.listdir(BACKUP_DIR):
                if f.startswith(filename) and f.endswith('.json'):
                    path = os.path.join(BACKUP_DIR, f)
                    break
            else:
                return None
    
    with open(path) as f:
        return json.load(f)

def auto_backup():
    """Create an automatic timestamped backup (called periodically)"""
    result = save_backup_to_file()
    # Keep only last 10 auto-backups to save space
    backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith('.json') and f.startswith('backup_')])
    while len(backups) > 10:
        os.remove(os.path.join(BACKUP_DIR, backups.pop(0)))
    return result
