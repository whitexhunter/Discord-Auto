# config.py
import os

API_PORT = 5000
DATABASE = 'discord_bot.db'
BACKUP_DIR = 'backups'
JWT_SECRET = os.getenv('JWT_SECRET', 'change-this-to-a-long-random-string')
ADMIN_USERNAME = 'admin'
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'change-this-password')
