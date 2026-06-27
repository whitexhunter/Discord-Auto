# scheduler.py
import threading
import time
import requests
from datetime import datetime
import database as db

job_threads = {}
job_stop_events = {}
scheduler_lock = threading.Lock()

def send_discord_message(token, channel_id, message):
    headers = {
        'Authorization': token,
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    try:
        response = requests.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers=headers, json={'content': message, 'tts': False}, timeout=15
        )
        if response.status_code in (200, 201):
            return True, None
        elif response.status_code == 429:
            return False, f"Rate limited ({response.json().get('retry_after', 5)}s)"
        elif response.status_code == 401:
            return False, "Invalid token"
        elif response.status_code == 403:
            return False, "No permission"
        elif response.status_code == 404:
            return False, "Channel not found"
        else:
            return False, f"HTTP {response.status_code}"
    except Exception as e:
        return False, str(e)

def job_worker(job_id, token, user_id):
    stop_event = job_stop_events.get(job_id)
    
    while not stop_event.is_set():
        job = db.get_job(job_id)
        if not job or job[6] != 'running':
            break
        
        # Check license validity
        user = db.get_user_by_id(user_id)
        if not user:
            break
        
        lic = db.get_license_by_key(user[2])
        if not lic or not lic[6] or datetime.fromisoformat(lic[5]) < datetime.now():
            db.update_job_status(job_id, 'expired')
            break
        
        channel_ids = [c.strip() for c in job[3].split(',') if c.strip()]
        message = job[4]
        interval = job[5]
        
        success_count = 0
        for channel_id in channel_ids:
            success, _ = send_discord_message(token, channel_id, message)
            if success:
                success_count += 1
            time.sleep(1.5)
        
        if success_count > 0:
            db.increment_job_sent(job_id, success_count)
        
        waited = 0
        while waited < interval and not stop_event.is_set():
            time.sleep(min(5, interval - waited))
            waited += 5
    
    with scheduler_lock:
        job_threads.pop(job_id, None)
        job_stop_events.pop(job_id, None)

def start_job(job_id, token, user_id):
    with scheduler_lock:
        if job_id in job_threads and job_threads[job_id].is_alive():
            return False, "Already running"
        
        stop_event = threading.Event()
        job_stop_events[job_id] = stop_event
        thread = threading.Thread(target=job_worker, args=(job_id, token, user_id), daemon=True)
        thread.start()
        job_threads[job_id] = thread
        return True, "Started"

def stop_job(job_id):
    with scheduler_lock:
        if job_id in job_stop_events:
            job_stop_events[job_id].set()
        db.update_job_status(job_id, 'stopped')
    return True
