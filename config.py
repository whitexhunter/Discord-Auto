# config.py - Configuration file
import os

# ===== CONFIGURATION =====
# CHANGE THESE VALUES
OWNER_DISCORD_ID = "YOUR_DISCORD_USER_ID_HERE"  # Your Discord user ID
ADMIN_SERVER_ID = "YOUR_SERVER_ID_HERE"          # Your admin server ID (guild ID)
BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')

DATABASE = 'discord_bot.db'
BACKUP_DIR = 'backups'
