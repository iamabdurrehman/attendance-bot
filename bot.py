import discord
from discord.ext import commands, tasks
from discord import app_commands

import sqlite3
from datetime import datetime, date, time, timedelta
import csv
import os
from zoneinfo import ZoneInfo  # for timezone handling

# ===================== CONFIG =====================

# Read token from environment (Railway variable: BOT_TOKEN)
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set!")

GUILD_ID = 1036605140083413086
LEADERSHIP_CHANNEL_ID = 1391757084109836358
ATTENDANCE_CHANNEL_ID = 1440240074196521041

OFFICE_START_TIME = time(10, 20)          # 10:20 AM
EXCLUDED_ROLES = ["CEO", "CTO", "CFO", "COO"]
DB_FILE = "attendance.db"

# Your local timezone (Pakistan)
TIMEZONE = ZoneInfo("Asia/Karachi")

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
    c.execute("SELECT COUNT(*) FROM attendance WHERE user_id = ? AND date = ?;", (user_id, today_str))
    count = c.fetchone()[0]
    conn.close()
    return count > 0


def get_month_date_range(year: int, month: int):
    start_date = date(year, month, 1)
    end_date = date(year + (month // 12), (month % 12) + 1, 1) - timedelta(days=1)
    return start_date, end_date


def query_monthly_lates(year: int, month: int):
    start_date, end_date = get_month_date_range(year, month)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT user_id, username, COUNT(*) as late_count
        FROM attendance
        WHERE is_late = 1 AND date BETWEEN ? AND ?
        GROUP BY user_id, username
        ORDER BY late_count DESC;
    """, (start_date.isoformat(), end_date.isoformat()))
    rows = c.fetchall()
    conn.close()
    return rows, start_date, end_date


# ----------------- HELPERS -----------------


def user_is_exempt(member: discord.Member) -> bool:
    return any(role.name in EXCLUDED_ROLES for role in member.roles)


def calculate_fine(late_count: int) -> int:
    return 2000 if late_count > 3 else 0


# ----------------- EVENT: on_message -----------------


@bot.event
async def on_message(message: discord.Message):
    # ignore bot messages and non-attendance channel
    if message.author.bot or message.channel.id != ATTENDANCE_CHANNEL_ID:
        return

    now = datetime.now(TIMEZONE)
    today_str = now.date().isoformat()
    time_str = now.strftime("%H:%M:%S")
    user = message.author

    # already marked via /present or earlier message?
    if has_attendance_today(user.id, today_str):
        await bot.process_commands(message)
        return

    is_late = now.time() > OFFICE_START_TIME
    mark_attendance_db(user.id, f"{user.name}#{user.discriminator}", today_str, time_str, is_late)

    if is_late:
        await message.channel.send(
            f"🔔 {user.mention} you are **late today** because you arrived after 10:20 and "
            f"did not mark attendance."
        )

    await bot.process_commands(message)


# ----------------- EVENT: on_ready -----------------


@bot.event
async def on_ready():
    guild = bot.get_guild(GUILD_ID)
    if guild:
        await bot.tree.sync(guild=guild)
        print(f"Commands synced to {guild.name}")
    else:
        await bot.tree.sync()
        print("Commands synced globally")

    print(f"Logged in as {bot.user}")
    monthly_report_task.start()


# ----------------- COMMAND: /present -----------------


@bot.tree.command(name="present", description="Mark your attendance for today.")
async def present(interaction: discord.Interaction):
    if interaction.channel_id != ATTENDANCE_CHANNEL_ID:
        await interaction.response.send_message("❌ Use this command in the attendance channel.", ephemeral=True)
        return

    now = datetime.now(TIMEZONE)
    today_str = now.date().isoformat()
    time_str = now.strftime("%H:%M:%S")
    user = interaction.user

    if has_attendance_today(user.id, today_str):
        await interaction.response.send_message("✅ Already marked attendance today.", ephemeral=True)
        return

    is_late = now.time() > OFFICE_START_TIME
    mark_attendance_db(user.id, f"{user.name}#{user.discriminator}", today_str, time_str, is_late)

    await interaction.response.send_message(
        "⏰ You are marked **LATE** for today." if is_late else "✅ Attendance marked on time.",
        ephemeral=True
    )

    if is_late:
        channel = interaction.guild.get_channel(ATTENDANCE_CHANNEL_ID)
        if channel:
            await channel.send(f"⏰ {user.mention} is **late today** (checked in at `{time_str}`).")


# ----------------- COMMAND: /my_late_count -----------------


@bot.tree.command(name="my_late_count", description="Check how many times you were late this month.")
@app_commands.describe(year="Year", month="Month from 1 to 12")
async def my_late_count(interaction: discord.Interaction, year: int, month: int):
    user = interaction.user
    start, end = get_month_date_range(year, month)

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT COUNT(*) FROM attendance
        WHERE user_id = ? AND is_late = 1 AND date BETWEEN ? AND ?;
    """, (user.id, start.isoformat(), end.isoformat()))
    count = c.fetchone()[0]
    conn.close()

    await interaction.response.send_message(
        f"📅 You were late **{count}** time(s) in {year}-{month:02d}.", ephemeral=True
    )


# ----------------- COMMAND: /monthly_report (late+fine summary, CSV) -----------------


@bot.tree.command(name="monthly_report", description="Generate late fine report for a month (admins only).")
@app_commands.describe(year="Year", month="Month from 1 to 12")
async def monthly_report(interaction: discord.Interaction, year: int, month: int):
    if not any(r.permissions.administrator for r in interaction.user.roles):
        await interaction.response.send_message("❌ Only admins can run this.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    await generate_and_send_monthly_report(interaction.guild, interaction.channel, year, month, auto=False)
    await interaction.followup.send("📨 Monthly fine report generated.", ephemeral=True)


# ----------------- NEW: /attendance_report (raw attendance CSV) -----------------


@bot.tree.command(
    name="attendance_report",
    description="Raw attendance list for all employees in a given month (admins only)."
)
@app_commands.describe(year="Year", month="Month from 1 to 12")
async def attendance_report(interaction: discord.Interaction, year: int, month: int):
    if not any(r.permissions.administrator for r in interaction.user.roles):
        await interaction.response.send_message("❌ Only admins can run this.", ephemeral=True)
        return

    start_date, end_date = get_month_date_range(year, month)

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT username, date, time, is_late
        FROM attendance
        WHERE date BETWEEN ? AND ?
        ORDER BY date, time;
    """, (start_date.isoformat(), end_date.isoformat()))
    rows = c.fetchall()
    conn.close()

    if not rows:
        await interaction.response.send_message(
            f"📂 No attendance records for {year}-{month:02d}.", ephemeral=True
        )
        return

    filename = f"attendance_full_{year}_{month:02d}.csv"
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Username", "Date", "Time", "Status"])
        for username, d, t, is_late in rows:
            status = "Late" if is_late == 1 else "On Time"
            writer.writerow([username, d, t, status])

    await interaction.response.send_message(
        content=f"📂 Full attendance report for **{year}-{month:02d}**.",
        file=discord.File(filename),
        ephemeral=False
    )

    if os.path.exists(filename):
        os.remove(filename)


# ----------------- NEW: /attendance_today (present vs absent) -----------------


@bot.tree.command(
    name="attendance_today",
    description="Show who is present and absent today (admins only)."
)
async def attendance_today(interaction: discord.Interaction):
    if not any(r.permissions.administrator for r in interaction.user.roles):
        await interaction.response.send_message("❌ Only admins can run this.", ephemeral=True)
        return

    guild = interaction.guild
    today = datetime.now(TIMEZONE).date()
    today_str = today.isoformat()

    # All human members (ignore bots)
    eligible_members = [m for m in guild.members if not m.bot]

    # Get all user_ids who have attendance today
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT DISTINCT user_id FROM attendance WHERE date = ?;", (today_str,))
    present_ids = {row[0] for row in c.fetchall()}
    conn.close()

    present_members = [m for m in eligible_members if m.id in present_ids]
    absent_members = [m for m in eligible_members if m.id not in present_ids]

    def fmt_list(members):
        if not members:
            return "_None_"
        return "\n".join(f"- {m.mention}" for m in members)

    text = [
        f"📅 **Attendance for {today_str}**",
        "",
        f"✅ **Present ({len(present_members)})**",
        fmt_list(present_members),
        "",
        f"❌ **Absent ({len(absent_members)})**",
        fmt_list(absent_members),
    ]

    await interaction.response.send_message("\n".join(text), ephemeral=False)


# ----------------- NEW: /employee_summary (per-employee stats + fine) -----------------


@bot.tree.command(
    name="employee_summary",
    description="Per-employee monthly summary (on-time, late, total, fine). Admins only."
)
@app_commands.describe(year="Year", month="Month from 1 to 12")
async def employee_summary(interaction: discord.Interaction, year: int, month: int):
    if not any(r.permissions.administrator for r in interaction.user.roles):
        await interaction.response.send_message("❌ Only admins can run this.", ephemeral=True)
        return

    start_date, end_date = get_month_date_range(year, month)

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT user_id, username,
               COUNT(*) as total_days,
               SUM(is_late) as late_days
        FROM attendance
        WHERE date BETWEEN ? AND ?
        GROUP BY user_id, username
        ORDER BY username COLLATE NOCASE;
    """, (start_date.isoformat(), end_date.isoformat()))
    rows = c.fetchall()
    conn.close()

    if not rows:
        await interaction.response.send_message(
            f"📊 No attendance data for {year}-{month:02d}.", ephemeral=True
        )
        return

    filename = f"employee_summary_{year}_{month:02d}.csv"
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Username", "On Time", "Late", "Total Days", "Fine (Rs)"])
        for _, username, total_days, late_days in rows:
            late_days = late_days or 0
            on_time = total_days - late_days
            fine = calculate_fine(late_days)
            writer.writerow([username, on_time, late_days, total_days, fine])

    await interaction.response.send_message(
        content=f"📊 Employee summary for **{year}-{month:02d}**.",
        file=discord.File(filename),
        ephemeral=False
    )

    if os.path.exists(filename):
        os.remove(filename)


# ----------------- BACKGROUND: Auto Monthly Fine Report -----------------


@tasks.loop(hours=24)
async def monthly_report_task():
    await bot.wait_until_ready()
    today = datetime.now(TIMEZONE).date()
    if today.day != 1:
        return

    year = today.year - 1 if today.month == 1 else today.year
    month = 12 if today.month == 1 else today.month - 1

    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    channel = guild.get_channel(LEADERSHIP_CHANNEL_ID)
    if not channel:
        return

    await generate_and_send_monthly_report(guild, channel, year, month, auto=True)


# ----------------- FINE REPORT GENERATOR -----------------


async def generate_and_send_monthly_report(guild, channel, year: int, month: int, auto=False):
    report_rows, start_date, end_date = query_monthly_lates(year, month)

    if not report_rows:
        await channel.send(f"📊 No late records for {year}-{month:02d}.")
        return

    filename = f"late_fine_report_{year}_{month:02d}.csv"
    with open(filename, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["Username", "Late Count", "Fine (Rs)"])
        for _, username, late_count in report_rows:
            fine = calculate_fine(late_count)
            writer.writerow([username, late_count, fine])

    await channel.send(
        content=f"📊 {'Auto-' if auto else ''}Monthly Late Fine Report ({start_date} to {end_date})",
        file=discord.File(filename)
    )

    if os.path.exists(filename):
        os.remove(filename)


# ----------------- BOT START -----------------

bot.run(BOT_TOKEN)
