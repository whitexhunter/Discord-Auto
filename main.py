# main.py - Bot entry point with interactive panels
import discord
from discord import app_commands, ui, ButtonStyle, TextStyle
from discord.ext import commands
import database as db
import scheduler
import threading
import time
from datetime import datetime
from config import OWNER_DISCORD_ID, ADMIN_SERVER_ID, BOT_TOKEN

# ===================================================================
# DISCORD BOT SETUP
# ===================================================================
intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True

bot = commands.Bot(command_prefix='!', intents=intents)

def is_owner(interaction: discord.Interaction) -> bool:
    return str(interaction.user.id) == OWNER_DISCORD_ID

def is_admin_server(interaction: discord.Interaction) -> bool:
    return str(interaction.guild_id) == ADMIN_SERVER_ID if interaction.guild_id else False

async def check_registered(interaction: discord.Interaction) -> bool:
    user = db.get_user(str(interaction.user.id))
    if not user:
        embed = discord.Embed(
            title="❌ Not Registered",
            description="You haven't registered yet!\nUse the button below to activate your license key.",
            color=0xe74c3c
        )
        view = ui.View()
        view.add_item(ui.Button(label="🔑 Activate License", style=ButtonStyle.primary, custom_id="activate_license"))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        return False
    return True

async def check_license_valid(interaction: discord.Interaction) -> bool:
    lic = db.get_license_info_for_user(str(interaction.user.id))
    if not lic:
        await interaction.response.send_message("❌ No license found linked to your account.", ephemeral=True)
        return False
    expires_at = datetime.fromisoformat(lic[4])
    if expires_at < datetime.now():
        embed = discord.Embed(
            title="❌ License Expired",
            description=f"Your license expired on **{lic[4][:10]}**.\nContact an admin to renew.",
            color=0xe74c3c
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return False
    return True

# ===================================================================
# MODALS (Forms)
# ===================================================================

class ActivateLicenseModal(ui.Modal, title="🔑 Activate Your License"):
    license_key = ui.TextInput(label="License Key", placeholder="e.g. ABCDEF-123456-GHIJKL", style=TextStyle.short, required=True)
    discord_token = ui.TextInput(label="Discord Token", placeholder="Paste your Discord account token here", style=TextStyle.short, required=True)
    
    async def on_submit(self, interaction: discord.Interaction):
        key = self.license_key.value.strip().upper()
        token = self.discord_token.value.strip()
        
        # Validate license
        valid, result = db.validate_license(key)
        if not valid:
            await interaction.response.send_message(f"❌ {result}", ephemeral=True)
            return
        
        lic = result
        current_count = db.get_license_account_count(key)
        if current_count >= lic[2]:
            await interaction.response.send_message(f"❌ This license has reached its max account limit ({lic[2]}).", ephemeral=True)
            return
        
        success, msg = db.register_or_login(str(interaction.user.id), key, token)
        if success:
            expires_at = datetime.fromisoformat(lic[5])
            days_left = (expires_at - datetime.now()).days
            embed = discord.Embed(title="✅ License Activated!", color=0x00ff00)
            embed.add_field(name="License Key", value=f"`{key[:16]}...`", inline=False)
            embed.add_field(name="Days Remaining", value=str(max(0, days_left)), inline=True)
            embed.add_field(name="Expires", value=lic[5][:10], inline=True)
            embed.add_field(name="Slots Used", value=f"{current_count + 1} / {lic[2]}", inline=True)
            embed.set_footer(text="Use the panel button below to access your dashboard")
            
            view = ui.View()
            view.add_item(ui.Button(label="📊 Open Panel", style=ButtonStyle.success, custom_id="open_panel"))
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ {msg}", ephemeral=True)

class AddTokenModal(ui.Modal, title="🔑 Add/Update Discord Token"):
    discord_token = ui.TextInput(label="Discord Token", placeholder="Paste your Discord account token here", style=TextStyle.short, required=True)
    
    async def on_submit(self, interaction: discord.Interaction):
        token = self.discord_token.value.strip()
        user = db.get_user(str(interaction.user.id))
        
        if not user:
            await interaction.response.send_message("❌ You need to activate a license first.", ephemeral=True)
            return
        
        # Update token in database
        import sqlite3
        conn = sqlite3.connect(db.DATABASE)
        c = conn.cursor()
        c.execute("UPDATE users SET discord_token=? WHERE discord_id=?", (token, str(interaction.user.id)))
        conn.commit()
        conn.close()
        
        embed = discord.Embed(title="✅ Token Updated", color=0x00ff00)
        embed.add_field(name="Token", value=f"`{token[:10]}...{token[-4:]}`", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

class CreateJobModal(ui.Modal, title="📨 Create New Job"):
    job_name = ui.TextInput(label="Job Name", placeholder="e.g. Marketing Campaign", style=TextStyle.short, required=True)
    channel_ids = ui.TextInput(label="Channel IDs", placeholder="123456789, 987654321 (comma separated)", style=TextStyle.short, required=True)
    interval = ui.TextInput(label="Interval (seconds, min 90)", placeholder="90", style=TextStyle.short, required=True, max_length=5)
    message_content = ui.TextInput(label="Message Content", placeholder="Your message to send...", style=TextStyle.paragraph, required=True)
    
    async def on_submit(self, interaction: discord.Interaction):
        if not await check_license_valid(interaction):
            return
        
        name = self.job_name.value.strip()
        channels_raw = self.channel_ids.value.strip()
        interval_str = self.interval.value.strip()
        message = self.message_content.value.strip()
        
        if not interval_str.isdigit() or int(interval_str) < 90:
            await interaction.response.send_message("❌ Interval must be at least 90 seconds.", ephemeral=True)
            return
        
        interval = int(interval_str)
        ids = [c.strip() for c in channels_raw.split(',') if c.strip()]
        
        if not ids:
            await interaction.response.send_message("❌ Please provide at least one valid channel ID.", ephemeral=True)
            return
        
        for cid in ids:
            if not cid.isdigit():
                await interaction.response.send_message(f"❌ Invalid channel ID: `{cid}`", ephemeral=True)
                return
        
        channel_ids_str = ','.join(ids)
        job_id = db.create_job(str(interaction.user.id), name, channel_ids_str, message, interval)
        success, msg = scheduler.start_job(job_id, str(interaction.user.id))
        
        embed = discord.Embed(title="✅ Job Created & Started", color=0x00ff00)
        embed.add_field(name="Job ID", value=f"`{job_id}`", inline=True)
        embed.add_field(name="Name", value=name, inline=True)
        embed.add_field(name="Channels", value=str(len(ids)), inline=True)
        embed.add_field(name="Interval", value=f"{interval}s", inline=True)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

class EditJobModal(ui.Modal, title="✏️ Edit Job"):
    def __init__(self, job_id, current_name, current_channels, current_interval, current_message):
        super().__init__()
        self.job_id = job_id
        self.add_item(ui.TextInput(label="Job Name", default=current_name, style=TextStyle.short, required=True))
        self.add_item(ui.TextInput(label="Channel IDs", default=current_channels, style=TextStyle.short, required=True))
        self.add_item(ui.TextInput(label="Interval (seconds, min 90)", default=str(current_interval), style=TextStyle.short, required=True, max_length=5))
        self.add_item(ui.TextInput(label="Message Content", default=current_message, style=TextStyle.paragraph, required=True))
    
    async def on_submit(self, interaction: discord.Interaction):
        name = self.children[0].value.strip()
        channels_raw = self.children[1].value.strip()
        interval_str = self.children[2].value.strip()
        message = self.children[3].value.strip()
        
        if not interval_str.isdigit() or int(interval_str) < 90:
            await interaction.response.send_message("❌ Interval must be at least 90 seconds.", ephemeral=True)
            return
        
        interval = int(interval_str)
        ids = [c.strip() for c in channels_raw.split(',') if c.strip()]
        
        if not ids:
            await interaction.response.send_message("❌ Please provide at least one valid channel ID.", ephemeral=True)
            return
        
        for cid in ids:
            if not cid.isdigit():
                await interaction.response.send_message(f"❌ Invalid channel ID: `{cid}`", ephemeral=True)
                return
        
        # Stop old job thread
        scheduler.stop_job(self.job_id)
        
        # Update in database
        import sqlite3
        conn = sqlite3.connect(db.DATABASE)
        c = conn.cursor()
        c.execute('''UPDATE jobs SET job_name=?, channel_ids=?, message_content=?, interval_seconds=?
                     WHERE id=? AND user_discord_id=?''',
                  (name, ','.join(ids), message, interval, self.job_id, str(interaction.user.id)))
        conn.commit()
        conn.close()
        
        # Restart job
        db.update_job_status(self.job_id, 'running')
        scheduler.start_job(self.job_id, str(interaction.user.id))
        
        embed = discord.Embed(title="✅ Job Updated & Restarted", color=0x00ff00)
        embed.add_field(name="Job ID", value=f"`{self.job_id}`", inline=True)
        embed.add_field(name="Name", value=name, inline=True)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

# ===================================================================
# VIEWS (Button Panels)
# ===================================================================

class MainPanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @ui.button(label="📊 My Subscription", style=ButtonStyle.primary, custom_id="panel_subscription", row=0)
    async def subscription_button(self, interaction: discord.Interaction, button: ui.Button):
        if not await check_registered(interaction):
            return
        await show_subscription(interaction)
    
    @ui.button(label="🔑 Add Token", style=ButtonStyle.secondary, custom_id="panel_add_token", row=0)
    async def add_token_button(self, interaction: discord.Interaction, button: ui.Button):
        if not await check_registered(interaction):
            return
        await interaction.response.send_modal(AddTokenModal())
    
    @ui.button(label="📋 Manage Jobs", style=ButtonStyle.success, custom_id="panel_manage_jobs", row=1)
    async def manage_jobs_button(self, interaction: discord.Interaction, button: ui.Button):
        if not await check_registered(interaction):
            return
        if not await check_license_valid(interaction):
            return
        await show_jobs_list(interaction)
    
    @ui.button(label="🆕 Create Job", style=ButtonStyle.danger, custom_id="panel_create_job", row=1)
    async def create_job_button(self, interaction: discord.Interaction, button: ui.Button):
        if not await check_registered(interaction):
            return
        if not await check_license_valid(interaction):
            return
        await interaction.response.send_modal(CreateJobModal())
    
    @ui.button(label="🔄 Refresh", style=ButtonStyle.gray, custom_id="panel_refresh", row=2)
    async def refresh_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(view=self)

class JobsListView(ui.View):
    def __init__(self, jobs, page=0):
        super().__init__(timeout=None)
        self.jobs = jobs
        self.page = page
        self.per_page = 5
        self.max_page = max(0, (len(jobs) - 1) // self.per_page)
        
        if self.max_page > 0:
            if page > 0:
                self.add_item(ui.Button(label="◀ Previous", style=ButtonStyle.secondary, custom_id=f"jobs_page_{page-1}", row=0))
            if page < self.max_page:
                self.add_item(ui.Button(label="Next ▶", style=ButtonStyle.secondary, custom_id=f"jobs_page_{page+1}", row=0))
        
        start = page * self.per_page
        end = start + self.per_page
        page_jobs = jobs[start:end]
        
        for i, job in enumerate(page_jobs):
            job_id = job[0]
            status = job[6]
            
            if status == 'running':
                label = f"⏹ Stop #{job_id} - {job[2][:20]}"
                style = ButtonStyle.danger
                cid = f"stop_job_{job_id}"
            elif status == 'stopped':
                label = f"▶ Resume #{job_id} - {job[2][:20]}"
                style = ButtonStyle.success
                cid = f"resume_job_{job_id}"
            elif status == 'expired':
                label = f"⚠ #{job_id} - {job[2][:20]} (Expired)"
                style = ButtonStyle.gray
                cid = f"none_{job_id}"
            else:
                label = f"#{job_id} - {job[2][:20]}"
                style = ButtonStyle.gray
                cid = f"none_{job_id}"
            
            self.add_item(ui.Button(label=label, style=style, custom_id=cid, row=i + 1))
        
        if page_jobs:
            # Add detail buttons
            row_start = min(len(page_jobs), 4)
            for i, job in enumerate(page_jobs[:5]):
                self.add_item(ui.Button(label=f"📄 Job #{job[0]}", style=ButtonStyle.primary, 
                                        custom_id=f"job_detail_{job[0]}", row=row_start + 1))
        
        self.add_item(ui.Button(label="🔙 Back to Panel", style=ButtonStyle.gray, custom_id="back_to_panel", row=5))
        self.add_item(ui.Button(label="🆕 Create New Job", style=ButtonStyle.danger, custom_id="create_job_from_list", row=5))

class JobDetailView(ui.View):
    def __init__(self, job):
        super().__init__(timeout=None)
        self.job = job
        job_id = job[0]
        
        if job[6] == 'running':
            self.add_item(ui.Button(label="⏹ Stop Job", style=ButtonStyle.danger, custom_id=f"stop_job_{job_id}", row=0))
        elif job[6] == 'stopped':
            self.add_item(ui.Button(label="▶ Resume Job", style=ButtonStyle.success, custom_id=f"resume_job_{job_id}", row=0))
        
        self.add_item(ui.Button(label="✏️ Edit Job", style=ButtonStyle.primary, custom_id=f"edit_job_{job_id}", row=0))
        self.add_item(ui.Button(label="🗑️ Delete Job", style=ButtonStyle.danger, custom_id=f"delete_job_{job_id}", row=0))
        self.add_item(ui.Button(label="🔙 Back to Jobs", style=ButtonStyle.gray, custom_id="back_to_jobs", row=1))

class AdminPanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @ui.button(label="🔑 Generate Keys", style=ButtonStyle.success, custom_id="admin_gen_keys", row=0)
    async def gen_keys_button(self, interaction: discord.Interaction, button: ui.Button):
        if not is_owner(interaction):
            return await interaction.response.send_message("❌ Not authorized.", ephemeral=True)
        await interaction.response.send_modal(GenerateKeysModal())
    
    @ui.button(label="📋 All Licenses", style=ButtonStyle.primary, custom_id="admin_list_licenses", row=0)
    async def list_licenses_button(self, interaction: discord.Interaction, button: ui.Button):
        if not is_owner(interaction):
            return await interaction.response.send_message("❌ Not authorized.", ephemeral=True)
        await show_admin_licenses(interaction)
    
    @ui.button(label="👥 All Users", style=ButtonStyle.primary, custom_id="admin_list_users", row=0)
    async def list_users_button(self, interaction: discord.Interaction, button: ui.Button):
        if not is_owner(interaction):
            return await interaction.response.send_message("❌ Not authorized.", ephemeral=True)
        await show_admin_users(interaction)
    
    @ui.button(label="📊 Stats", style=ButtonStyle.secondary, custom_id="admin_stats", row=1)
    async def stats_button(self, interaction: discord.Interaction, button: ui.Button):
        if not is_owner(interaction):
            return await interaction.response.send_message("❌ Not authorized.", ephemeral=True)
        await show_admin_stats(interaction)
    
    @ui.button(label="💾 Backup", style=ButtonStyle.secondary, custom_id="admin_backup", row=1)
    async def backup_button(self, interaction: discord.Interaction, button: ui.Button):
        if not is_owner(interaction):
            return await interaction.response.send_message("❌ Not authorized.", ephemeral=True)
        await handle_admin_backup(interaction)
    
    @ui.button(label="🔄 Resume All Jobs", style=ButtonStyle.danger, custom_id="admin_resume_all", row=1)
    async def resume_all_button(self, interaction: discord.Interaction, button: ui.Button):
        if not is_owner(interaction):
            return await interaction.response.send_message("❌ Not authorized.", ephemeral=True)
        await handle_admin_resume_all(interaction)

class GenerateKeysModal(ui.Modal, title="🔑 Generate License Keys"):
    count = ui.TextInput(label="Number of Keys", placeholder="1", style=TextStyle.short, required=True)
    accounts = ui.TextInput(label="Max Accounts Per Key", placeholder="1", style=TextStyle.short, required=True)
    days = ui.TextInput(label="Validity (days)", placeholder="30", style=TextStyle.short, required=True)
    
    async def on_submit(self, interaction: discord.Interaction):
        count_val = int(self.count.value) if self.count.value.isdigit() else 1
        accounts_val = int(self.accounts.value) if self.accounts.value.isdigit() else 1
        days_val = int(self.days.value) if self.days.value.isdigit() else 30
        
        keys = db.create_licenses(count_val, accounts_val, days_val, str(interaction.user.id))
        
        key_list = "\n".join([f"`{k}`" for k in keys])
        
        embed = discord.Embed(title="✅ License Keys Generated", color=0x00ff00)
        embed.add_field(name="Quantity", value=str(count_val), inline=True)
        embed.add_field(name="Accounts/Key", value=str(accounts_val), inline=True)
        embed.add_field(name="Valid Days", value=str(days_val), inline=True)
        embed.add_field(name="Keys", value=key_list, inline=False)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

class AdminLicenseDetailView(ui.View):
    def __init__(self, key):
        super().__init__(timeout=None)
        self.key = key
        
        self.add_item(ui.Button(label="📅 Extend Days", style=ButtonStyle.primary, custom_id=f"admin_extend_{key}", row=0))
        self.add_item(ui.Button(label="👤 Set Accounts", style=ButtonStyle.primary, custom_id=f"admin_set_accts_{key}", row=0))
        self.add_item(ui.Button(label="📆 Set Expiry Date", style=ButtonStyle.secondary, custom_id=f"admin_set_expiry_{key}", row=1))
        self.add_item(ui.Button(label="✅ Activate", style=ButtonStyle.success, custom_id=f"admin_activate_{key}", row=1))
        self.add_item(ui.Button(label="❌ Deactivate", style=ButtonStyle.danger, custom_id=f"admin_deactivate_{key}", row=1))
        self.add_item(ui.Button(label="🗑️ Delete Key", style=ButtonStyle.danger, custom_id=f"admin_delete_key_{key}", row=2))
        self.add_item(ui.Button(label="🔙 Back", style=ButtonStyle.gray, custom_id="admin_back_to_licenses", row=2))

# ===================================================================
# PANEL DISPLAY FUNCTIONS
# ===================================================================

async def show_main_panel(interaction: discord.Interaction, ephemeral=True):
    """Show the main user panel"""
    user = db.get_user(str(interaction.user.id))
    
    if not user:
        embed = discord.Embed(
            title="🔑 Discord Message Sender",
            description="Welcome! Activate your license to get started.\n\n"
                       "Click the button below and enter your **License Key** and **Discord Token**.",
            color=0x3498db
        )
        view = ui.View()
        view.add_item(ui.Button(label="🔑 Activate License", style=ButtonStyle.primary, custom_id="activate_license"))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=ephemeral)
        return
    
    lic = db.get_license_info_for_user(str(interaction.user.id))
    
    if lic:
        expires_at = datetime.fromisoformat(lic[4])
        days_left = (expires_at - datetime.now()).days
        status_text = "✅ Active" if days_left > 0 and lic[5] else "❌ Expired"
        status_color = 0x00ff00 if days_left > 0 else 0xe74c3c
    else:
        days_left = 0
        status_text = "⚠️ No License"
        status_color = 0xe67e22
    
    # Get job stats
    jobs = db.get_user_jobs(str(interaction.user.id))
    running_jobs = sum(1 for j in jobs if j[6] == 'running')
    total_sent = sum(j[7] for j in jobs)
    
    embed = discord.Embed(
        title="📊 Dashboard",
        description=f"Welcome back, {interaction.user.display_name}!",
        color=status_color
    )
    
    if lic:
        embed.add_field(name="License", value=f"`{lic[0][:16]}...`", inline=False)
        embed.add_field(name="Status", value=status_text, inline=True)
        embed.add_field(name="Days Left", value=str(max(0, days_left)), inline=True)
        embed.add_field(name="Expires", value=lic[4][:10], inline=True)
        embed.add_field(name="Token", value=f"`{user[3][:10]}...{user[3][-4:]}`", inline=True)
    
    embed.add_field(name="📋 Total Jobs", value=str(len(jobs)), inline=True)
    embed.add_field(name="🟢 Running", value=str(running_jobs), inline=True)
    embed.add_field(name="📤 Total Sent", value=str(total_sent), inline=True)
    embed.set_footer(text="Use the buttons below to manage everything")
    
    view = MainPanelView()
    await interaction.response.send_message(embed=embed, view=view, ephemeral=ephemeral)

async def show_subscription(interaction: discord.Interaction):
    lic = db.get_license_info_for_user(str(interaction.user.id))
    user = db.get_user(str(interaction.user.id))
    
    if not lic or not user:
        embed = discord.Embed(title="❌ No Subscription Found", color=0xe74c3c,
                             description="You haven't activated a license yet.")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    expires_at = datetime.fromisoformat(lic[4])
    days_left = (expires_at - datetime.now()).days
    status = "✅ Active" if days_left > 0 and lic[5] else "❌ Expired"
    
    embed = discord.Embed(title="📋 My Subscription", color=0x00ff00 if days_left > 0 else 0xe74c3c)
    embed.add_field(name="License Key", value=f"`{lic[0]}`", inline=False)
    embed.add_field(name="Status", value=status, inline=True)
    embed.add_field(name="Days Remaining", value=str(max(0, days_left)), inline=True)
    embed.add_field(name="Expires", value=lic[4][:10], inline=True)
    embed.add_field(name="Max Accounts", value=str(lic[1]), inline=True)
    embed.add_field(name="Token", value=f"`{user[3][:10]}...{user[3][-4:]}`", inline=False)
    
    view = ui.View()
    view.add_item(ui.Button(label="🔙 Back to Panel", style=ButtonStyle.gray, custom_id="back_to_panel"))
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

async def show_jobs_list(interaction: discord.Interaction, page=0):
    jobs = db.get_user_jobs(str(interaction.user.id))
    
    if not jobs:
        embed = discord.Embed(
            title="📋 Your Jobs",
            description="No jobs created yet. Click the button below to create one!",
            color=0x3498db
        )
        view = ui.View()
        view.add_item(ui.Button(label="🆕 Create Job", style=ButtonStyle.danger, custom_id="create_job_from_list"))
        view.add_item(ui.Button(label="🔙 Back to Panel", style=ButtonStyle.gray, custom_id="back_to_panel"))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        return
    
    start = page * 5
    end = start + 5
    page_jobs = jobs[start:end]
    
    embed = discord.Embed(
        title=f"📋 Your Jobs (Page {page + 1}/{(len(jobs) - 1) // 5 + 1})",
        description=f"**Total: {len(jobs)} jobs** | Running: {sum(1 for j in jobs if j[6] == 'running')}",
        color=0x3498db
    )
    
    for job in page_jobs:
        status_emoji = "🟢" if job[6] == 'running' else "🔴" if job[6] == 'stopped' else "🟡"
        channel_count = len(job[3].split(','))
        
        embed.add_field(
            name=f"{status_emoji} #{job[0]} - {job[2]}",
            value=f"📺 {channel_count} channels | ⏱ {job[5]}s | 📤 {job[7]} sent | **{job[6].upper()}**",
            inline=False
        )
    
    embed.set_footer(text="Use the buttons below to manage each job")
    
    view = JobsListView(jobs, page)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

async def show_job_detail(interaction: discord.Interaction, job_id):
    job = db.get_job(job_id)
    
    if not job or job[1] != str(interaction.user.id):
        await interaction.response.send_message("❌ Job not found.", ephemeral=True)
        return
    
    status_emoji = "🟢" if job[6] == 'running' else "🔴" if job[6] == 'stopped' else "🟡"
    channel_ids = job[3].split(',')
    channels_formatted = "\n".join([f"• `{cid}`" for cid in channel_ids[:8]])
    if len(channel_ids) > 8:
        channels_formatted += f"\n... and {len(channel_ids) - 8} more"
    
    embed = discord.Embed(
        title=f"{status_emoji} Job #{job[0]} - {job[2]}",
        color=0x00ff00 if job[6] == 'running' else 0xe74c3c
    )
    embed.add_field(name="Status", value=f"**{job[6].upper()}**", inline=True)
    embed.add_field(name="Interval", value=f"{job[5]}s", inline=True)
    embed.add_field(name="Messages Sent", value=str(job[7]), inline=True)
    embed.add_field(name="Channels", value=channels_formatted, inline=False)
    embed.add_field(name="Message Preview", value=f"```{job[4][:200]}{'...' if len(job[4]) > 200 else ''}```", inline=False)
    embed.add_field(name="Created", value=job[8][:16], inline=True)
    embed.add_field(name="Last Run", value=job[9][:16] if job[9] else "Never", inline=True)
    
    view = JobDetailView(job)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# ===================================================================
# ADMIN DISPLAY FUNCTIONS
# ===================================================================

async def show_admin_panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🛡️ Admin Control Panel",
        description="Manage licenses, users, backups and more.",
        color=0x9b59b6
    )
    
    # Get quick stats
    licenses = db.get_all_licenses()
    users = db.get_all_users()
    active_licenses = sum(1 for l in licenses if l[6] and datetime.fromisoformat(l[5]) > datetime.now())
    
    embed.add_field(name="🔑 Total Licenses", value=str(len(licenses)), inline=True)
    embed.add_field(name="✅ Active", value=str(active_licenses), inline=True)
    embed.add_field(name="👥 Registered Users", value=str(len(users)), inline=True)
    
    view = AdminPanelView()
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

async def show_admin_licenses(interaction: discord.Interaction):
    licenses = db.get_all_licenses()
    
    if not licenses:
        await interaction.response.send_message("📭 No licenses found.", ephemeral=True)
        return
    
    embed = discord.Embed(title="📋 All License Keys", color=0x3498db)
    
    for lic in licenses[:20]:
        expires = datetime.fromisoformat(lic[5])
        status = "✅ Active" if lic[6] and expires > datetime.now() else "❌ Inactive/Expired"
        account_count = db.get_license_account_count(lic[1])
        
        embed.add_field(
            name=f"`{lic[1][:16]}...`",
            value=f"Accounts: {account_count}/{lic[2]} | Days: {lic[3]} | Exp: {lic[5][:10]} | {status}",
            inline=False
        )
    
    if len(licenses) > 20:
        embed.set_footer(text=f"Showing 20 of {len(licenses)} licenses")
    else:
        embed.set_footer(text=f"Total: {len(licenses)} licenses | Click a key below to manage it")
    
    # Add 5 quick action buttons for first 5 licenses
    view = ui.View(timeout=None)
    for lic in licenses[:5]:
        view.add_item(ui.Button(label=f"🔑 {lic[1][:12]}...", style=ButtonStyle.primary, 
                                custom_id=f"admin_license_detail_{lic[1]}", row=0))
    
    view.add_item(ui.Button(label="🔙 Back", style=ButtonStyle.gray, custom_id="admin_back_to_panel", row=1))
    
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

async def show_admin_license_detail(interaction: discord.Interaction, key):
    lic = db.get_license_by_key(key)
    if not lic:
        await interaction.response.send_message("❌ License not found.", ephemeral=True)
        return
    
    expires_at = datetime.fromisoformat(lic[5])
    days_left = (expires_at - datetime.now()).days
    account_count = db.get_license_account_count(lic[1])
    status = "✅ Active" if lic[6] and days_left > 0 else "❌ Inactive/Expired"
    
    embed = discord.Embed(title=f"🔑 License Details", color=0x3498db)
    embed.add_field(name="Key", value=f"`{lic[1]}`", inline=False)
    embed.add_field(name="Status", value=status, inline=True)
    embed.add_field(name="Days Remaining", value=str(max(0, days_left)), inline=True)
    embed.add_field(name="Expires", value=lic[5][:10], inline=True)
    embed.add_field(name="Accounts", value=f"{account_count}/{lic[2]}", inline=True)
    embed.add_field(name="Created", value=lic[4][:10], inline=True)
    embed.add_field(name="Created By", value=f"<@{lic[7]}>" if lic[7] != 'web_admin' else 'Web Admin', inline=True)
    
    view = AdminLicenseDetailView(key)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

async def show_admin_users(interaction: discord.Interaction):
    users = db.get_all_users()
    
    if not users:
        await interaction.response.send_message("📭 No users registered.", ephemeral=True)
        return
    
    embed = discord.Embed(title="👥 Registered Users", color=0x3498db)
    
    for user in users[:15]:
        embed.add_field(
            name=f"<@{user[1]}>",
            value=f"License: `{user[2][:16]}...` | Token: `{user[3][:10]}...`\n"
                  f"Expires: {user[5][:10]} | Status: {'✅' if user[7] else '❌'}",
            inline=False
        )
    
    if len(users) > 15:
        embed.set_footer(text=f"Showing 15 of {len(users)} users")
    else:
        embed.set_footer(text=f"Total: {len(users)} users")
    
    view = ui.View()
    view.add_item(ui.Button(label="🔙 Back to Panel", style=ButtonStyle.gray, custom_id="admin_back_to_panel"))
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

async def show_admin_stats(interaction: discord.Interaction):
    licenses = db.get_all_licenses()
    users = db.get_all_users()
    
    total_jobs = 0
    running_jobs = 0
    total_sent = 0
    
    for user in users:
        jobs = db.get_user_jobs(user[1])
        total_jobs += len(jobs)
        for j in jobs:
            if j[6] == 'running':
                running_jobs += 1
            total_sent += j[7]
    
    active_licenses = sum(1 for l in licenses if l[6] and datetime.fromisoformat(l[5]) > datetime.now())
    
    embed = discord.Embed(title="📊 Bot Statistics", color=0x00ff00)
    embed.add_field(name="🔑 Total Licenses", value=str(len(licenses)), inline=True)
    embed.add_field(name="✅ Active Licenses", value=str(active_licenses), inline=True)
    embed.add_field(name="❌ Expired/Inactive", value=str(len(licenses) - active_licenses), inline=True)
    embed.add_field(name="👥 Registered Users", value=str(len(users)), inline=True)
    embed.add_field(name="📋 Total Jobs", value=str(total_jobs), inline=True)
    embed.add_field(name="🟢 Running Jobs", value=str(running_jobs), inline=True)
    embed.add_field(name="📤 Messages Sent", value=str(total_sent), inline=True)
    
    # Backup stats
    backups = db.list_backups()
    embed.add_field(name="💾 Backups Available", value=str(len(backups)), inline=True)
    
    view = ui.View()
    view.add_item(ui.Button(label="🔙 Back to Panel", style=ButtonStyle.gray, custom_id="admin_back_to_panel"))
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

async def handle_admin_backup(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    result = db.save_backup_to_file()
    
    embed = discord.Embed(title="💾 Backup Created", color=0x00ff00)
    embed.add_field(name="Filename", value=f"`{result['filename']}`", inline=False)
    embed.add_field(name="Licenses", value=str(result['stats']['licenses']), inline=True)
    embed.add_field(name="Users", value=str(result['stats']['users']), inline=True)
    embed.add_field(name="Jobs", value=str(result['stats']['jobs']), inline=True)
    embed.add_field(name="Time", value=result['exported_at'][:19], inline=True)
    
    await interaction.followup.send(embed=embed, ephemeral=True)

async def handle_admin_resume_all(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    import sqlite3
    conn = sqlite3.connect(db.DATABASE)
    c = conn.cursor()
    c.execute("SELECT id, user_discord_id, job_name FROM jobs")
    all_jobs = c.fetchall()
    conn.close()
    
    resumed = 0
    failed = 0
    
    for job_id, discord_id, job_name in all_jobs:
        lic = db.get_license_info_for_user(discord_id)
        if lic and lic[5] and datetime.fromisoformat(lic[4]) > datetime.now():
            db.update_job_status(job_id, 'running')
            success, msg = scheduler.start_job(job_id, discord_id)
            if success:
                resumed += 1
            else:
                failed += 1
        else:
            failed += 1
    
    embed = discord.Embed(title="🔄 Jobs Resume Complete", color=0x00ff00 if resumed > 0 else 0xe74c3c)
    embed.add_field(name="✅ Resumed", value=str(resumed), inline=True)
    embed.add_field(name="❌ Failed/Skipped", value=str(failed), inline=True)
    
    await interaction.followup.send(embed=embed, ephemeral=True)

# ===================================================================
# BUTTON INTERACTION HANDLER
# ===================================================================

@bot.event
async def on_interaction(interaction: discord.Interaction):
    """Handle all button interactions globally"""
    if interaction.type != discord.InteractionType.component:
        return
    
    custom_id = interaction.data.get("custom_id", "")
    
    # ===== USER PANEL BUTTONS =====
    
    if custom_id == "activate_license":
        await interaction.response.send_modal(ActivateLicenseModal())
    
    elif custom_id == "open_panel" or custom_id == "back_to_panel":
        await show_main_panel(interaction)
    
    elif custom_id == "panel_subscription":
        if not await check_registered(interaction):
            return
        await show_subscription(interaction)
    
    elif custom_id == "panel_add_token":
        if not await check_registered(interaction):
            return
        await interaction.response.send_modal(AddTokenModal())
    
    elif custom_id == "panel_manage_jobs":
        if not await check_registered(interaction) or not await check_license_valid(interaction):
            return
        await show_jobs_list(interaction)
    
    elif custom_id == "panel_create_job" or custom_id == "create_job_from_list":
        if not await check_registered(interaction) or not await check_license_valid(interaction):
            return
        await interaction.response.send_modal(CreateJobModal())
    
    elif custom_id == "panel_refresh":
        await show_main_panel(interaction)
    
    elif custom_id.startswith("jobs_page_"):
        page = int(custom_id.split("_")[2])
        await show_jobs_list(interaction, page)
    
    elif custom_id.startswith("back_to_jobs"):
        await show_jobs_list(interaction)
    
    elif custom_id.startswith("stop_job_"):
        job_id = int(custom_id.split("_")[2])
        job = db.get_job(job_id)
        if not job or job[1] != str(interaction.user.id) and not is_owner(interaction):
            await interaction.response.send_message("❌ Job not found.", ephemeral=True)
            return
        
        scheduler.stop_job(job_id)
        embed = discord.Embed(title=f"⏹️ Job #{job_id} Stopped", color=0xe74c3c)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # Refresh the view
        await show_job_detail(interaction, job_id)
    
    elif custom_id.startswith("resume_job_"):
        job_id = int(custom_id.split("_")[2])
        job = db.get_job(job_id)
        if not job or job[1] != str(interaction.user.id) and not is_owner(interaction):
            await interaction.response.send_message("❌ Job not found.", ephemeral=True)
            return
        
        if not await check_license_valid(interaction):
            return
        
        db.update_job_status(job_id, 'running')
        success, msg = scheduler.start_job(job_id, job[1])
        if success:
            embed = discord.Embed(title=f"▶️ Job #{job_id} Resumed", color=0x00ff00)
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ {msg}", ephemeral=True)
        
        await show_job_detail(interaction, job_id)
    
    elif custom_id.startswith("job_detail_"):
        job_id = int(custom_id.split("_")[2])
        await show_job_detail(interaction, job_id)
    
    elif custom_id.startswith("edit_job_"):
        job_id = int(custom_id.split("_")[2])
        job = db.get_job(job_id)
        if not job or job[1] != str(interaction.user.id):
            await interaction.response.send_message("❌ Job not found.", ephemeral=True)
            return
        
        modal = EditJobModal(job_id, job[2], job[3], job[5], job[4])
        await interaction.response.send_modal(modal)
    
    elif custom_id.startswith("delete_job_"):
        job_id = int(custom_id.split("_")[2])
        job = db.get_job(job_id)
        if not job or job[1] != str(interaction.user.id):
            await interaction.response.send_message("❌ Job not found.", ephemeral=True)
            return
        
        scheduler.stop_job(job_id)
        db.delete_job(job_id, str(interaction.user.id))
        embed = discord.Embed(title=f"🗑️ Job #{job_id} Deleted", color=0xe74c3c)
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    # ===== ADMIN BUTTONS =====
    
    elif custom_id == "admin_panel":
        if not is_owner(interaction):
            return await interaction.response.send_message("❌ Not authorized.", ephemeral=True)
        await show_admin_panel(interaction)
    
    elif custom_id == "admin_gen_keys":
        if not is_owner(interaction):
            return await interaction.response.send_message("❌ Not authorized.", ephemeral=True)
        await interaction.response.send_modal(GenerateKeysModal())
    
    elif custom_id == "admin_list_licenses":
        if not is_owner(interaction):
            return await interaction.response.send_message("❌ Not authorized.", ephemeral=True)
        await show_admin_licenses(interaction)
    
    elif custom_id == "admin_list_users":
        if not is_owner(interaction):
            return await interaction.response.send_message("❌ Not authorized.", ephemeral=True)
        await show_admin_users(interaction)
    
    elif custom_id == "admin_stats":
        if not is_owner(interaction):
            return await interaction.response.send_message("❌ Not authorized.", ephemeral=True)
        await show_admin_stats(interaction)
    
    elif custom_id == "admin_backup":
        if not is_owner(interaction):
            return await interaction.response.send_message("❌ Not authorized.", ephemeral=True)
        await handle_admin_backup(interaction)
    
    elif custom_id == "admin_resume_all":
        if not is_owner(interaction):
            return await interaction.response.send_message("❌ Not authorized.", ephemeral=True)
        await handle_admin_resume_all(interaction)
    
    elif custom_id == "admin_back_to_panel":
        if not is_owner(interaction):
            return await interaction.response.send_message("❌ Not authorized.", ephemeral=True)
        await show_admin_panel(interaction)
    
    elif custom_id == "admin_back_to_licenses":
        if not is_owner(interaction):
            return await interaction.response.send_message("❌ Not authorized.", ephemeral=True)
        await show_admin_licenses(interaction)
    
    elif custom_id.startswith("admin_license_detail_"):
        if not is_owner(interaction):
            return await interaction.response.send_message("❌ Not authorized.", ephemeral=True)
        key = custom_id.replace("admin_license_detail_", "")
        await show_admin_license_detail(interaction, key)
    
    elif custom_id.startswith("admin_extend_"):
        if not is_owner(interaction):
            return await interaction.response.send_message("❌ Not authorized.", ephemeral=True)
        key = custom_id.replace("admin_extend_", "")
        
        class ExtendDaysModal(ui.Modal, title="📅 Extend License Days"):
            days = ui.TextInput(label="Additional Days", placeholder="30", style=TextStyle.short, required=True)
            
            async def on_submit(self, modal_interaction):
                days_val = int(self.days.value) if self.days.value.isdigit() else 0
                if days_val < 1:
                    await modal_interaction.response.send_message("❌ Must be at least 1 day.", ephemeral=True)
                    return
                success, msg = db.update_license_days(key, days_val)
                if success:
                    lic = db.get_license_by_key(key)
                    embed = discord.Embed(title="✅ License Extended", color=0x00ff00)
                    embed.add_field(name="License", value=f"`{key[:16]}...`", inline=False)
                    embed.add_field(name="Added", value=f"{days_val} days", inline=True)
                    embed.add_field(name="New Expiry", value=lic[5][:10], inline=True)
                    await modal_interaction.response.send_message(embed=embed, ephemeral=True)
                else:
                    await modal_interaction.response.send_message(f"❌ {msg}", ephemeral=True)
        
        await interaction.response.send_modal(ExtendDaysModal())
    
    elif custom_id.startswith("admin_set_accts_"):
        if not is_owner(interaction):
            return await interaction.response.send_message("❌ Not authorized.", ephemeral=True)
        key = custom_id.replace("admin_set_accts_", "")
        
        class SetAccountsModal(ui.Modal, title="👤 Set Max Accounts"):
            accounts = ui.TextInput(label="Max Accounts", placeholder="5", style=TextStyle.short, required=True)
            
            async def on_submit(self, modal_interaction):
                accts = int(self.accounts.value) if self.accounts.value.isdigit() else 0
                if accts < 1:
                    await modal_interaction.response.send_message("❌ Must be at least 1.", ephemeral=True)
                    return
                success, msg = db.update_license_accounts(key, accts)
                if success:
                    embed = discord.Embed(title="✅ Max Accounts Updated", color=0x00ff00)
                    embed.add_field(name="License", value=f"`{key[:16]}...`", inline=False)
                    embed.add_field(name="New Max", value=str(accts), inline=True)
                    await modal_interaction.response.send_message(embed=embed, ephemeral=True)
                else:
                    await modal_interaction.response.send_message(f"❌ {msg}", ephemeral=True)
        
        await interaction.response.send_modal(SetAccountsModal())
    
    elif custom_id.startswith("admin_set_expiry_"):
        if not is_owner(interaction):
            return await interaction.response.send_message("❌ Not authorized.", ephemeral=True)
        key = custom_id.replace("admin_set_expiry_", "")
        
        class SetExpiryModal(ui.Modal, title="📆 Set Expiry Date"):
            date = ui.TextInput(label="Expiry Date (YYYY-MM-DD)", placeholder="2026-12-31", style=TextStyle.short, required=True)
            
            async def on_submit(self, modal_interaction):
                success, msg = db.set_license_expiry_date(key, self.date.value)
                if success:
                    lic = db.get_license_by_key(key)
                    embed = discord.Embed(title="✅ Expiry Date Set", color=0x00ff00)
                    embed.add_field(name="License", value=f"`{key[:16]}...`", inline=False)
                    embed.add_field(name="New Expiry", value=lic[5][:10], inline=True)
                    await modal_interaction.response.send_message(embed=embed, ephemeral=True)
                else:
                    await modal_interaction.response.send_message(f"❌ {msg}", ephemeral=True)
        
        await interaction.response.send_modal(SetExpiryModal())
    
    elif custom_id.startswith("admin_activate_"):
        if not is_owner(interaction):
            return await interaction.response.send_message("❌ Not authorized.", ephemeral=True)
        key = custom_id.replace("admin_activate_", "")
        success, msg = db.toggle_license_active(key, True)
        embed = discord.Embed(title="✅ License Activated", color=0x00ff00)
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    elif custom_id.startswith("admin_deactivate_"):
        if not is_owner(interaction):
            return await interaction.response.send_message("❌ Not authorized.", ephemeral=True)
        key = custom_id.replace("admin_deactivate_", "")
        db.deactivate_license(key)
        embed = discord.Embed(title="✅ License Deactivated", color=0xe74c3c)
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    elif custom_id.startswith("admin_delete_key_"):
        if not is_owner(interaction):
            return await interaction.response.send_message("❌ Not authorized.", ephemeral=True)
        key = custom_id.replace("admin_delete_key_", "")
        
        # Confirm first
        class ConfirmDeleteView(ui.View):
            def __init__(self):
                super().__init__(timeout=30)
            
            @ui.button(label="🗑️ Yes, Delete Everything", style=ButtonStyle.danger, custom_id=f"admin_confirm_delete_{key}")
            async def confirm(self, confirm_interaction, button):
                db.delete_license_complete(key)
                embed = discord.Embed(title="🗑️ License Deleted", color=0xe74c3c,
                                     description=f"License `{key[:16]}...` and all associated data deleted.")
                await confirm_interaction.response.send_message(embed=embed, ephemeral=True)
            
            @ui.button(label="Cancel", style=ButtonStyle.gray, custom_id="cancel")
            async def cancel(self, cancel_interaction, button):
                await cancel_interaction.response.edit_message(content="Cancelled.", view=None)
        
        view = ConfirmDeleteView()
        await interaction.response.send_message(
            "⚠️ **WARNING**: This will permanently delete the license AND all users & jobs associated with it.\n\nThis cannot be undone!",
            view=view, ephemeral=True
        )
    
    elif custom_id.startswith("admin_confirm_delete_"):
        key = custom_id.replace("admin_confirm_delete_", "")
        if is_owner(interaction):
            db.delete_license_complete(key)
            embed = discord.Embed(title="🗑️ License Deleted", color=0xe74c3c)
            await interaction.response.edit_message(embed=embed, view=None)
    
    elif custom_id == "cancel":
        await interaction.response.edit_message(content="Cancelled.", view=None)

# ===================================================================
# SLASH COMMANDS
# ===================================================================

@bot.tree.command(name="panel", description="Open your main dashboard panel")
async def panel_command(interaction: discord.Interaction):
    """Main command to open the panel - works in DMs and designated admin server"""
    await show_main_panel(interaction)

@bot.tree.command(name="admin", description="[OWNER] Open the admin control panel")
async def admin_command(interaction: discord.Interaction):
    if not is_owner(interaction):
        await interaction.response.send_message("❌ You are not authorized to use this command.", ephemeral=True)
        return
    await show_admin_panel(interaction)

# ===================================================================
# PREFIX COMMAND (for quick access in DMs)
# ===================================================================

@bot.command(name="panel")
async def panel_prefix(ctx):
    """!panel - Opens the dashboard in DMs"""
    if ctx.guild:
        # In a server, tell them to use /panel
        embed = discord.Embed(
            title="📊 Panel Access",
            description="Use `/panel` as a slash command to open your dashboard, or DM me `!panel`",
            color=0x3498db
        )
        await ctx.send(embed=embed, delete_after=10)
        return
    
    # In DMs - simulate interaction
    class FakeInteraction:
        def __init__(self, user, channel):
            self.user = user
            self.channel = channel
            self.type = discord.InteractionType.component
            self.response = FakeResponse(ctx)
            self.data = {"custom_id": "open_panel"}
        
        async def response(self):
            pass
    
    class FakeResponse:
        def __init__(self, ctx):
            self.ctx = ctx
        
        async def send_message(self, embed=None, view=None, ephemeral=False):
            await self.ctx.send(embed=embed, view=view)
        
        async def send_modal(self, modal):
            await self.ctx.send("Please use `/panel` in DMs for full interactive support.")
    
    await show_main_panel(FakeInteraction(ctx.author, ctx.channel), ephemeral=False)

# ===================================================================
# BOT STARTUP
# ===================================================================

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"📡 Owner ID: {OWNER_DISCORD_ID}")
    print(f"🏠 Admin Server ID: {ADMIN_SERVER_ID}")
    print("------")
    
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"❌ Failed to sync commands: {e}")
    
    # Restart running jobs
    running_jobs = db.get_all_running_jobs()
    for job in running_jobs:
        scheduler.start_job(job[0], job[1])
        print(f"🔄 Restarted job #{job[0]} ({job[2]})")
    
    print(f"✅ Bot is ready! Users can use /panel in DMs or the admin server")

# ===================================================================
# AUTO-BACKUP THREAD
# ===================================================================

def auto_backup_loop():
    while True:
        try:
            time.sleep(6 * 60 * 60)  # Every 6 hours
            result = db.auto_backup()
            print(f"[AUTO-BACKUP] Created: {result['filename']}")
        except Exception as e:
            print(f"[AUTO-BACKUP ERROR] {e}")

# ===================================================================
# MAIN ENTRY
# ===================================================================

if __name__ == '__main__':
    db.init_db()
    
    if OWNER_DISCORD_ID != "YOUR_DISCORD_USER_ID_HERE":
        db.add_admin(OWNER_DISCORD_ID)
        print(f"✅ Owner {OWNER_DISCORD_ID} added as admin")
    
    backup_thread = threading.Thread(target=auto_backup_loop, daemon=True)
    backup_thread.start()
    print("✅ Auto-backup thread started (every 6 hours)")
    
    bot.run(BOT_TOKEN)
