# app.py
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
import database as db
import scheduler
import bcrypt
import json
import os
from datetime import datetime, timedelta
from config import API_PORT, JWT_SECRET, ADMIN_USERNAME, ADMIN_PASSWORD

app = Flask(__name__)
app.config['JWT_SECRET_KEY'] = JWT_SECRET
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(days=7)
CORS(app)
jwt = JWTManager(app)

# ===== AUTH =====
@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username', '')
    password = data.get('password', '')
    
    # Admin login
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        token = create_access_token(identity='admin')
        return jsonify({'token': token, 'role': 'admin', 'username': 'admin'})
    
    # User login - check by user_id and password
    users = db.get_all_users()
    for user in users:
        user_id = user[0]
        user_pass = user[4]
        if user_pass and bcrypt.checkpw(password.encode(), user_pass.encode() if isinstance(user_pass, str) else user_pass):
            # Check license validity
            lic = db.get_license_by_key(user[2])
            if lic and lic[6] and datetime.fromisoformat(lic[5]) > datetime.now():
                token = create_access_token(identity=f'user_{user_id}')
                return jsonify({'token': token, 'role': 'user', 'user_id': user_id})
    
    return jsonify({'error': 'Invalid credentials'}), 401

# ===== ADMIN ENDPOINTS =====
@app.route('/api/admin/licenses', methods=['GET'])
@jwt_required()
def get_licenses():
    if get_jwt_identity() != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    licenses = db.get_all_licenses()
    result = []
    for lic in licenses:
        result.append({
            'id': lic[0], 'key': lic[1], 'max_accounts': lic[2], 'days_valid': lic[3],
            'created_at': lic[4], 'expires_at': lic[5], 'active': lic[6],
            'created_by': lic[7], 'notes': lic[8],
            'auto_responder_enabled': bool(lic[9]) if len(lic) > 9 else False,
            'max_auto_responders': lic[10] if len(lic) > 10 else 1
        })
    return jsonify(result)

@app.route('/api/admin/licenses/generate', methods=['POST'])
@jwt_required()
def generate_licenses():
    if get_jwt_identity() != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.json
    keys = db.create_licenses(
        data.get('count', 1),
        data.get('max_accounts', 1),
        data.get('days_valid', 30),
        'admin',
        data.get('notes', ''),
        data.get('auto_responder_enabled', False),
        data.get('max_auto_responders', 1)
    )
    return jsonify({'keys': keys})

@app.route('/api/admin/licenses/<key>/extend', methods=['POST'])
@jwt_required()
def extend_license(key):
    if get_jwt_identity() != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.json
    success, msg = db.update_license_days(key, data.get('days', 30))
    return jsonify({'success': success, 'message': msg})

@app.route('/api/admin/licenses/<key>/accounts', methods=['POST'])
@jwt_required()
def set_license_accounts(key):
    if get_jwt_identity() != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.json
    success, msg = db.update_license_accounts(key, data.get('max_accounts', 1))
    return jsonify({'success': success, 'message': msg})

@app.route('/api/admin/licenses/<key>/deactivate', methods=['POST'])
@jwt_required()
def deactivate_license_route(key):
    if get_jwt_identity() != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    db.deactivate_license(key)
    return jsonify({'success': True})

@app.route('/api/admin/licenses/<key>/activate', methods=['POST'])
@jwt_required()
def activate_license_route(key):
    if get_jwt_identity() != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    db.activate_license(key)
    return jsonify({'success': True})

@app.route('/api/admin/users', methods=['GET'])
@jwt_required()
def admin_get_users():
    if get_jwt_identity() != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    users = db.get_all_users()
    result = []
    for u in users:
        result.append({
            'id': u[0], 'discord_id': u[1], 'license_key': u[2][:16] + '...',
            'token_preview': u[3][:10] + '...' + u[3][-4:] if u[3] else 'N/A',
            'created_at': u[4], 'expires_at': u[5], 'max_accounts': u[6], 'active': u[7]
        })
    return jsonify(result)

@app.route('/api/admin/backup', methods=['POST'])
@jwt_required()
def create_backup():
    if get_jwt_identity() != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    backup = db.export_full_backup()
    filename = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path = os.path.join('backups', filename)
    os.makedirs('backups', exist_ok=True)
    with open(path, 'w') as f:
        json.dump(backup, f, indent=2)
    return jsonify({'filename': filename, 'stats': {
        'licenses': len(backup['data']['licenses']),
        'users': len(backup['data']['users']),
        'jobs': len(backup['data']['jobs']),
        'auto_responders': len(backup['data']['auto_responders'])
    }})

@app.route('/api/admin/backup/restore', methods=['POST'])
@jwt_required()
def restore_backup():
    if get_jwt_identity() != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.json
    backup_data = data.get('backup_data')
    if not backup_data:
        return jsonify({'error': 'No backup data provided'}), 400
    result = db.restore_from_backup(backup_data)
    return jsonify(result)

@app.route('/api/admin/stats', methods=['GET'])
@jwt_required()
def admin_stats():
    if get_jwt_identity() != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    licenses = db.get_all_licenses()
    users = db.get_all_users()
    total_jobs = 0
    running_jobs = 0
    total_sent = 0
    for u in users:
        jobs = db.get_user_jobs(u[0])
        total_jobs += len(jobs)
        for j in jobs:
            if j[6] == 'running':
                running_jobs += 1
            total_sent += j[7]
    active_licenses = sum(1 for l in licenses if l[6] and datetime.fromisoformat(l[5]) > datetime.now())
    return jsonify({
        'total_licenses': len(licenses),
        'active_licenses': active_licenses,
        'total_users': len(users),
        'total_jobs': total_jobs,
        'running_jobs': running_jobs,
        'total_sent': total_sent
    })

# ===== USER ENDPOINTS =====
@app.route('/api/user/register', methods=['POST'])
def user_register():
    data = request.json
    success, result = db.register_user(
        data.get('license_key', ''),
        data.get('discord_token', ''),
        data.get('password', '')
    )
    if success:
        return jsonify({'success': True, 'user_id': result['user_id']})
    return jsonify({'success': False, 'error': result}), 400

@app.route('/api/user/profile', methods=['GET'])
@jwt_required()
def user_profile():
    identity = get_jwt_identity()
    if identity == 'admin':
        return jsonify({'error': 'Admin has no user profile'}), 400
    user_id = int(identity.replace('user_', ''))
    user = db.get_user_by_id(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    lic = db.get_license_by_key(user[2])
    expires_at = datetime.fromisoformat(lic[5]) if lic else datetime.now()
    days_left = (expires_at - datetime.now()).days if lic else 0
    
    return jsonify({
        'user_id': user[0],
        'license_key': user[2][:16] + '...',
        'token_preview': user[3][:10] + '...' + user[3][-4:] if user[3] else 'N/A',
        'full_token': user[3],
        'max_accounts': user[7],
        'created_at': user[5],
        'license_expires': lic[5][:10] if lic else 'N/A',
        'days_left': max(0, days_left),
        'auto_responder_enabled': bool(lic[9]) if lic and len(lic) > 9 else False,
        'max_auto_responders': lic[10] if lic and len(lic) > 10 else 1
    })

@app.route('/api/user/tokens', methods=['GET'])
@jwt_required()
def user_tokens():
    identity = get_jwt_identity()
    if identity == 'admin':
        return jsonify([])
    user_id = int(identity.replace('user_', ''))
    user = db.get_user_by_id(user_id)
    if user:
        return jsonify([{'id': user[0], 'label': user[3][:15] + '...' + user[3][-4:], 'token': user[3]}])
    return jsonify([])

# ===== JOBS ENDPOINTS =====
@app.route('/api/user/jobs', methods=['GET'])
@jwt_required()
def get_jobs():
    identity = get_jwt_identity()
    if identity == 'admin':
        return jsonify({'error': 'Admin has no jobs'}), 400
    user_id = int(identity.replace('user_', ''))
    jobs = db.get_user_jobs(user_id)
    result = []
    for j in jobs:
        result.append({
            'id': j[0], 'name': j[2], 'channel_ids': j[3], 'channels': j[3].split(','),
            'message': j[4], 'interval': j[5], 'status': j[6],
            'total_sent': j[7], 'created_at': j[8], 'last_run': j[9] or 'Never',
            'token_used': j[10]
        })
    return jsonify(result)

@app.route('/api/user/jobs', methods=['POST'])
@jwt_required()
def create_job_route():
    identity = get_jwt_identity()
    if identity == 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    user_id = int(identity.replace('user_', ''))
    data = request.json
    
    channel_ids = ','.join([c.strip() for c in data.get('channel_ids', '').split(',') if c.strip()])
    if not channel_ids:
        return jsonify({'error': 'At least one channel ID required'}), 400
    
    interval = data.get('interval', 90)
    if interval < 90:
        return jsonify({'error': 'Minimum interval is 90 seconds'}), 400
    
    job_id = db.create_job(
        user_id, data.get('name', 'Untitled'), channel_ids,
        data.get('message', ''), interval, data.get('token_used')
    )
    
    # Start the job
    user = db.get_user_by_id(user_id)
    if user:
        scheduler.start_job(job_id, user[3], user_id)
    
    return jsonify({'success': True, 'job_id': job_id})

@app.route('/api/user/jobs/<int:job_id>', methods=['PUT'])
@jwt_required()
def update_job_route(job_id):
    identity = get_jwt_identity()
    if identity == 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    user_id = int(identity.replace('user_', ''))
    data = request.json
    
    scheduler.stop_job(job_id)
    
    channel_ids = ','.join([c.strip() for c in data.get('channel_ids', '').split(',') if c.strip()])
    db.update_job(job_id, user_id, data.get('name', 'Untitled'), channel_ids,
                  data.get('message', ''), data.get('interval', 90))
    
    db.update_job_status(job_id, 'running')
    user = db.get_user_by_id(user_id)
    if user:
        scheduler.start_job(job_id, user[3], user_id)
    
    return jsonify({'success': True})

@app.route('/api/user/jobs/<int:job_id>/stop', methods=['POST'])
@jwt_required()
def stop_job_route(job_id):
    scheduler.stop_job(job_id)
    return jsonify({'success': True})

@app.route('/api/user/jobs/<int:job_id>/resume', methods=['POST'])
@jwt_required()
def resume_job_route(job_id):
    identity = get_jwt_identity()
    if identity == 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    user_id = int(identity.replace('user_', ''))
    db.update_job_status(job_id, 'running')
    user = db.get_user_by_id(user_id)
    if user:
        scheduler.start_job(job_id, user[3], user_id)
    return jsonify({'success': True})

@app.route('/api/user/jobs/<int:job_id>', methods=['DELETE'])
@jwt_required()
def delete_job_route(job_id):
    identity = get_jwt_identity()
    if identity == 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    user_id = int(identity.replace('user_', ''))
    scheduler.stop_job(job_id)
    db.delete_job(job_id, user_id)
    return jsonify({'success': True})

# ===== AUTO RESPONDER ENDPOINTS =====
@app.route('/api/user/auto-responders', methods=['GET'])
@jwt_required()
def get_auto_responders():
    identity = get_jwt_identity()
    if identity == 'admin':
        return jsonify([])
    user_id = int(identity.replace('user_', ''))
    responders = db.get_user_auto_responders(user_id)
    result = []
    for r in responders:
        result.append({
            'id': r[0], 'name': r[2], 'trigger_keyword': r[3],
            'response_message': r[4], 'reply_to_new_dms': bool(r[5]),
            'reply_to_mentions': bool(r[6]), 'cooldown_seconds': r[7],
            'status': r[8], 'total_replies': r[9], 'created_at': r[10]
        })
    return jsonify(result)

@app.route('/api/user/auto-responders', methods=['POST'])
@jwt_required()
def create_auto_responder_route():
    identity = get_jwt_identity()
    if identity == 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    user_id = int(identity.replace('user_', ''))
    data = request.json
    
    success, result = db.create_auto_responder(
        user_id, data.get('name', 'Auto Responder'), data.get('response_message', ''),
        data.get('trigger_keyword', ''), data.get('reply_to_new_dms', True),
        data.get('reply_to_mentions', False), data.get('cooldown_seconds', 60)
    )
    
    if success:
        return jsonify({'success': True, 'responder_id': result['responder_id']})
    return jsonify({'success': False, 'error': result}), 400

@app.route('/api/user/auto-responders/<int:responder_id>', methods=['PUT'])
@jwt_required()
def update_auto_responder_route(responder_id):
    identity = get_jwt_identity()
    if identity == 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    user_id = int(identity.replace('user_', ''))
    data = request.json
    db.update_auto_responder(
        responder_id, user_id, data.get('name', 'Auto Responder'),
        data.get('response_message', ''), data.get('trigger_keyword', ''),
        data.get('reply_to_new_dms', True), data.get('reply_to_mentions', False),
        data.get('cooldown_seconds', 60)
    )
    return jsonify({'success': True})

@app.route('/api/user/auto-responders/<int:responder_id>/toggle', methods=['POST'])
@jwt_required()
def toggle_auto_responder(responder_id):
    responder = db.get_auto_responder(responder_id)
    if not responder:
        return jsonify({'error': 'Not found'}), 404
    new_status = 'inactive' if responder[8] == 'active' else 'active'
    db.update_auto_responder_status(responder_id, new_status)
    return jsonify({'success': True, 'status': new_status})

@app.route('/api/user/auto-responders/<int:responder_id>', methods=['DELETE'])
@jwt_required()
def delete_auto_responder_route(responder_id):
    identity = get_jwt_identity()
    if identity == 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    user_id = int(identity.replace('user_', ''))
    db.delete_auto_responder(responder_id, user_id)
    return jsonify({'success': True})

# ===== STARTUP =====
@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'time': datetime.now().isoformat()})

if __name__ == '__main__':
    db.init_db()
    # Start existing running jobs
    running_jobs = db.get_all_running_jobs()
    for job in running_jobs:
        user = db.get_user_by_id(job[1])
        if user:
            scheduler.start_job(job[0], user[3], job[1])
            print(f"Restarted job {job[0]}")
    app.run(host='0.0.0.0', port=API_PORT, debug=False)
