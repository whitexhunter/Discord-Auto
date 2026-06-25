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
    """Send a message to a Discord channel using a user token"""
    headers = {
        'Authorization': token,
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    
    payload = {
        'content': message,
        'tts': False
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        if response.status_code in (200, 201):
            return True, None
        elif response.status_code == 429:
            retry_after = response.json().get('retry_after', 5)
            return False, f"Rate limited ({retry_after}s)"
        elif response.status_code == 401:
            return False, "Invalid token (401 Unauthorized)"
        elif response.status_code == 403:
            return False, "No permission to send in channel (403)"
        elif response.status_code == 404:
            return False, "Channel not found (404)"
        else:
            return False, f"HTTP {response.status_code}"
    except requests.exceptions.Timeout:
        return False, "Request timed out"
    except Exception as e:
        return False, str(e)


def job_worker(job_id, discord_id):
    """Background thread that sends messages on interval"""
    stop_event = job_stop_events.get(job_id)
    
    while not stop_event.is_set():
        # Get job data
        job = db.get_job(job_id)
        if not job or job[6] != 'running':
            break
        
        # Get user token
        user = db.get_user(discord_id)
        if not user:
            break
        
        token = user[3]
        channel_ids = [c.strip() for c in job[3].split(',') if c.strip()]
        message = job[4]
        interval = job[5]
        
        # Check license validity
        lic = db.get_license_info_for_user(discord_id)
        if not lic or not lic[5] or datetime.fromisoformat(lic[4]) < datetime.now():
            db.update_job_status(job_id, 'expired')
            break
        
        # Send to each channel
        success_count = 0
        error_messages = []
        
        for channel_id in channel_ids:
            channel_id = channel_id.strip()
            if not channel_id:
                continue
            
            success, error = send_discord_message(token, channel_id, message)
            if success:
                success_count += 1
            else:
                error_messages.append(f"Channel {channel_id}: {error}")
            
            # Small delay between channels to avoid rate limits
            time.sleep(1.5)
        
        # Update sent count
        if success_count > 0:
            db.increment_job_sent(job_id, success_count)
        
        # Wait for interval (check stop event periodically)
        waited = 0
        while waited < interval and not stop_event.is_set():
            time.sleep(min(5, interval - waited))
            waited += 5
    
    # Cleanup
    with scheduler_lock:
        if job_id in job_threads:
            del job_threads[job_id]
        if job_id in job_stop_events:
            del job_stop_events[job_id]


def start_job(job_id, discord_id):
    """Start a background thread for a job"""
    with scheduler_lock:
        if job_id in job_threads and job_threads[job_id].is_alive():
            return False, "Job is already running"
        
        stop_event = threading.Event()
        job_stop_events[job_id] = stop_event
        
        thread = threading.Thread(target=job_worker, args=(job_id, discord_id), daemon=True)
        thread.start()
        job_threads[job_id] = thread
        return True, "Job started"


def stop_job(job_id):
    """Signal a job thread to stop"""
    with scheduler_lock:
        if job_id in job_stop_events:
            job_stop_events[job_id].set()
        db.update_job_status(job_id, 'stopped')
    return True


def resume_job(job_id, discord_id):
    """Resume a stopped job"""
    db.update_job_status(job_id, 'running')
    return start_job(job_id, discord_id)
