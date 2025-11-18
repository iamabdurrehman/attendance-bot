import discord
from discord.ext import commands, tasks
from discord import app_commands

import sqlite3
from datetime import datetime, date, time, timedelta
import csv
import os

# ===================== CONFIG =====================

BOT_TOKEN = "MTQ0MDAxNDI2MjU1Nzk5OTE3Ng.GUozvf.3x01MSdXj7CUbSxQa73AkNK02q5pgvVofi6OCU"  # <<== PUT YOUR REAL BOT TOKEN HERE
GUILD_ID = 1036605140083413086
LEADERSHIP_CHANNEL_ID = 1391757084109836358
ATTENDANCE_CHANNEL_ID = 1440240074196521041   # <--- YOUR ATTENDANCE CHANNEL

OFFICE_START_TIME = time(10, 20)    # 10:20 AM deadline
EXCLUDED_ROLES = ["CEO", "CTO", "CFO", "COO"]

DB_FILE = "attendance.db"

# ==================================================

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)


# ----------------- DATABASE SETUP -----------------

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            is_late INTEGER NOT NULL
        );
    """)
    conn.commit()
    conn.close()

init_db()


def mark_attendance_db(user_id: int, username: str, date_str: str, time_str: str, is_late: bool):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        INSERT INTO attendance (user_id, username, date, time, is_late)
        VALUES (?, ?, ?, ?, ?);
    """, (user_id, username, date_str, time_str, 1 if is_late else 0))
    conn.commit()
    conn.close()


def has_attendance_today(user_id: int, today_str: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM attendance WHERE user_id = ? AND date = ?;",
              (user_id, today_str))
    count = c.fetchone()[0]
    conn.close()
    return count > 0


def get_month_date_range(year: int, month: int):
    start_date = date(year, month, 1)
    if month == 12:
        end_date = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        end_date = date(year, month + 1, 1) - timedelta(days=1)
    return start_date, end_date


def query_monthly_lates(year: int, month: int):
    start_date, end_date = get_month_date_range(year, month)
    start_str = start_date.isoformat()
    end_str = end_date.isoformat()

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT user_id, username, COUNT(*) as late_count
        FROM attendance
        WHERE is_late = 1
          AND date >= ?
          AND date <= ?
        GROUP BY user_id, username
        ORDER BY late_count DESC;
    """, (start_str, end_str))
    rows = c.fetchall()
    conn.close()
    return rows, start_date, end_date


# ----------------- HELPER FUNCTIONS -----------------

def user_is_exempt(member: discord.Member) -> bool:
    for role in member.roles:
        if role.name in EXCLUDED_ROLES:
            return True
    return False


def calculate_fine(late_count: int) -> int:
    return 2000 if late_count > 3 else 0


# ----------------- AUTO CHECK: Message After 10:20 -----------------

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # Only track attendance channel messages
    if message.channel.id != ATTENDANCE_CHANNEL_ID:
        return

    now = datetime.now()
    today_str = now.date().isoformat()
    user = message.author
    time_str = now.strftime("%H:%M:%S")

    # Already marked attendance?
    if has_attendance_today(user.id, today_str):
        return

    # Is it after 10:20?
    if now.time() > OFFICE_START_TIME:
        mark_attendance_db(user.id, f"{user.name}#{user.discriminator}", today_str, time_str, True)

        await message.channel.send(
            f"🔔 {user.mention} you are **late today** because you arrived after 10:20 and did not mark attendance."
        )

    await bot.process_commands(message)


# ----------------- BOT EVENTS & COMMANDS -----------------

@bot.event
async def on_ready():
    guild = bot.get_guild(GUILD_ID)
    if guild:
        await bot.tree.sync(guild=guild)
        print(f"Commands synced to {guild.name}")
    print(f"Logged in as {bot.user}")
    monthly_report_task.start()


# -------- /present --------

@bot.tree.command(name="present", description="Mark your attendance for today.")
async def present(interaction: discord.Interaction):

    # Allow only inside attendance channel
    if interaction.channel_id != ATTENDANCE_CHANNEL_ID:
        await interaction.response.send_message(
            "❌ Please use this command in the attendance channel.", ephemeral=True
        )
        return

    user = interaction.user
    now = datetime.now()
    today_str = now.date().isoformat()
    time_str = now.strftime("%H:%M:%S")

    if has_attendance_today(user.id, today_str):
        await interaction.response.send_message("✅ Already marked attendance today.", ephemeral=True)
        return

    is_late = now.time() > OFFICE_START_TIME
    mark_attendance_db(user.id, f"{user.name}#{user.discriminator}", today_str, time_str, is_late)

    # Private message
    private_msg = (
        f"⏰ You are marked **LATE** for today."
        if is_late else
        f"✅ Attendance marked on time."
    )
    await interaction.response.send_message(private_msg, ephemeral=True)

    # Public late message
    if is_late:
        attendance_channel = interaction.guild.get_channel(ATTENDANCE_CHANNEL_ID)
        await attendance_channel.send(
            f"⏰ {user.mention} is **late today** (checked in at `{time_str}`)."
        )


# -------- /my_late_count --------

@bot.tree.command(name="my_late_count", description="Check your late count for a given month.")
@app_commands.describe(year="Year", month="Month 1-12")
async def my_late_count(interaction, year: int, month: int):
    user = interaction.user
    start_date, end_date = get_month_date_range(year, month)
    start_str = start_date.isoformat()
    end_str = end_date.isoformat()

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT COUNT(*) FROM attendance
        WHERE user_id = ? AND is_late = 1 AND date >= ? AND date <= ?;
    """, (user.id, start_str, end_str))
    late_count = c.fetchone()[0]
    conn.close()

    await interaction.response.send_message(
        f"📅 Late count for {year}-{month:02d}: **{late_count}** times.", ephemeral=True
    )


# -------- /monthly_report --------

@bot.tree.command(name="monthly_report", description="Generate monthly late/fine report.")
async def monthly_report(interaction, year: int, month: int):

    if not any(r.permissions.administrator for r in interaction.user.roles):
        await interaction.response.send_message("❌ Only admins can run this.", ephemeral=True)
        return

    guild = interaction.guild

    await interaction.response.defer(ephemeral=False, thinking=True)
    await generate_and_send_monthly_report(guild, interaction.channel, year, month, auto=False)
    await interaction.followup.send("📨 Monthly report generated.", ephemeral=True)


# -------- AUTO MONTHLY REPORT --------

@tasks.loop(hours=24)
async def monthly_report_task():
    await bot.wait_until_ready()

    today = datetime.now().date()
    if today.day != 1:
        return

    # Previous month
    if today.month == 1:
        year = today.year - 1
        month = 12
    else:
        year = today.year
        month = today.month - 1

    guild = bot.get_guild(GUILD_ID)
    channel = guild.get_channel(LEADERSHIP_CHANNEL_ID)
    await generate_and_send_monthly_report(guild, channel, year, month, auto=True)


bot.run(BOT_TOKEN)
