import asyncio
import csv
import difflib
import hashlib
import io
import json
import os
import re
import sqlite3
import time
import types
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")

# Railway:
# Set DATABASE_FILE=/data/pfo_signups.db
#
# Local computer:
# It will use pfo_signups.db
DATABASE_FILE = os.getenv(
    "DATABASE_FILE",
    "pfo_signups.db"
)


# ============================================================
# VISUALS (banners, logo, colours)
#
# This section is LOOKS ONLY. Nothing here changes how the
# signups or rankings work.
#
# The animated banners live in the "assets" folder. Put that
# folder in your GitHub repo next to this file, then set:
#
#   ASSET_BASE_URL=https://raw.githubusercontent.com/<you>/<repo>/main/assets
#
# in Railway's Variables tab. If ASSET_BASE_URL is not set the
# bot simply shows no banners/logo - everything else still works.
#
# Discord caches images by URL. If you ever regenerate the GIFs,
# bump ASSET_VERSION (e.g. 2, 3...) so Discord loads the new ones.
# ============================================================

ASSET_BASE_URL = os.getenv(
    "ASSET_BASE_URL",
    ""
).strip().rstrip("/")

ASSET_VERSION = os.getenv(
    "ASSET_VERSION",
    "3"
).strip()

# Gold from the official PFO logo (black / gold / silver scheme).
PFO_GOLD = discord.Color.from_rgb(212, 168, 76)

LOGO_FILE = "pfo_logo.gif"

RESULT_BANNER_FILE = "result.gif"
TITLE_RESULT_BANNER_FILE = "result_title.gif"
PROFILE_BANNER_FILE = "profile.gif"


def asset_url(
    filename: str
):

    if not ASSET_BASE_URL:
        return None

    return f"{ASSET_BASE_URL}/{filename}?v={ASSET_VERSION}"


def signup_banner_file(
    signup_type: str,
    closed: bool = False
):

    if closed:
        return f"signup_{signup_type}_closed.gif"

    return f"signup_{signup_type}.gif"


def ranking_banner_file(
    weight: str
):

    return (
        "rankings_"
        + weight.lower().replace(" ", "_")
        + ".gif"
    )


def apply_branding(
    embed: discord.Embed,
    banner_file: str = None,
    logo_file: str = LOGO_FILE,
    author_text: str = None,
    show_thumbnail: bool = True
):
    """
    Adds the animated banner (bottom image), the animated logo
    (top-right thumbnail) and a small author line to an embed.

    Each piece is skipped quietly if ASSET_BASE_URL is not set.
    """

    logo = asset_url(logo_file) if logo_file else None
    banner = asset_url(banner_file) if banner_file else None

    if author_text:

        if logo:
            embed.set_author(
                name=author_text,
                icon_url=logo
            )
        else:
            embed.set_author(
                name=author_text
            )

    if logo and show_thumbnail:
        embed.set_thumbnail(
            url=logo
        )

    if banner:
        embed.set_image(
            url=banner
        )

    return embed


# ============================================================
# RANKING CONFIGURATION
# ============================================================

WEIGHTS = {
    "P4P": "P4P",
    "Heavyweight": "HW",
    "Light Heavyweight": "LHW",
    "Middleweight": "MW",
    "Welterweight": "WW",
    "Lightweight": "LW",
    "Featherweight": "FW",
    "Bantamweight": "BW",
    "Flyweight": "FLW",
}

P4P_WEIGHT = "P4P"


# ============================================================
# BOT SETUP
# ============================================================

intents = discord.Intents.default()
intents.members = True

# Needed for /scanresults to read typed results in a channel.
# Turn on "Message Content Intent" in the Discord Developer Portal
# (Bot page). If it isn't turned on, the bot still starts and
# everything else works; only /scanresults explains what to do.
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


def now_utc():
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# DATABASE
# ============================================================

def get_db():

    connection = sqlite3.connect(
        DATABASE_FILE
    )

    connection.row_factory = sqlite3.Row

    return connection


def setup_database():

    db = get_db()
    cursor = db.cursor()

    # --------------------------------------------------------
    # SIGNUP SESSIONS
    # --------------------------------------------------------

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS signup_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            signup_type TEXT NOT NULL,
            message_id INTEGER,
            channel_id INTEGER,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            closed_at TEXT
        )
    """)

    # --------------------------------------------------------
    # SIGNUPS
    #
    # We still keep player_name in the database so this works
    # with an existing database.
    #
    # The IMPORTANT part is discord_user_id.
    # /signuppaste uses this ID to create a real @mention.
    # --------------------------------------------------------

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS signups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            discord_user_id INTEGER NOT NULL,
            player_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(session_id)
                REFERENCES signup_sessions(id)
        )
    """)

    # --------------------------------------------------------
    # RANKINGS
    #
    # rank 0 = Champion
    # rank 1-15 = ranked positions
    #
    # A fighter can be in:
    #
    # Lightweight #1
    # AND
    # P4P #3
    #
    # at the same time.
    # --------------------------------------------------------

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS rankings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            weight TEXT NOT NULL,
            discord_user_id INTEGER NOT NULL,
            rank INTEGER NOT NULL,
            movement INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,

            UNIQUE(guild_id, weight, rank),
            UNIQUE(guild_id, weight, discord_user_id)
        )
    """)

    # --------------------------------------------------------
    # RANKING MESSAGE IDs
    #
    # This allows the bot to EDIT the existing ranking message
    # rather than creating a new one every time.
    # --------------------------------------------------------

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ranking_messages (
            guild_id INTEGER NOT NULL,
            weight TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,

            PRIMARY KEY(guild_id, weight)
        )
    """)

    # --------------------------------------------------------
    # FIGHTS
    #
    # One row per fight. Fighters are stored by Discord user ID,
    # so records follow the account even if someone changes
    # their nickname. The name is also saved, which covers
    # fighters who have left the server or were imported by
    # name only.
    #
    # method: KO/TKO, SUB, DEC, DQ, DRAW or NC
    # For DRAW / NC, "winner" and "loser" are just fighter A / B.
    # --------------------------------------------------------

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS fights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            fight_date TEXT,
            event TEXT,
            winner_id INTEGER,
            winner_name TEXT NOT NULL,
            loser_id INTEGER,
            loser_name TEXT NOT NULL,
            method TEXT NOT NULL,
            round INTEGER,
            division TEXT,
            title_fight INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'result',
            logged_by INTEGER,
            created_at TEXT NOT NULL
        )
    """)

    # --------------------------------------------------------
    # STARTING RECORDS
    #
    # For fighters whose older fights were never written down.
    # Logged fights are added on top of this.
    # --------------------------------------------------------

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS starting_records (
            guild_id INTEGER NOT NULL,
            discord_user_id INTEGER NOT NULL,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            draws INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,

            PRIMARY KEY(guild_id, discord_user_id)
        )
    """)

    # Earlier streak (e.g. W3) at the end of the earlier record.
    # Added to existing databases without touching their data.
    cursor.execute("PRAGMA table_info(starting_records)")

    if "streak" not in [column[1] for column in cursor.fetchall()]:
        cursor.execute("ALTER TABLE starting_records ADD COLUMN streak TEXT")

    db.commit()
    db.close()

    # Tables for the newer features (safe to run every start).
    setup_card_tables()
    setup_fight_card_tables()
    setup_suggestion_tables()
    setup_vote_tables()
    setup_discipline_tables()
    setup_fighter_list_tables()
    setup_recap_tables()
    setup_card_draft_tables()
    setup_signup_time_columns()
    setup_coin_tables()
    setup_reminder_columns()
    setup_interview_tables()


# ============================================================
# SIGNUP DATABASE FUNCTIONS
# ============================================================

def get_active_session(
    guild_id: int,
    signup_type: str
):

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        SELECT *
        FROM signup_sessions
        WHERE guild_id = ?
        AND signup_type = ?
        AND active = 1
        ORDER BY id DESC
        LIMIT 1
    """, (
        guild_id,
        signup_type
    ))

    session = cursor.fetchone()

    db.close()

    return session


def get_session(
    session_id: int
):

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        SELECT *
        FROM signup_sessions
        WHERE id = ?
    """, (
        session_id,
    ))

    session = cursor.fetchone()

    db.close()

    return session


def create_session(
    guild_id: int,
    signup_type: str,
    message_id: int,
    channel_id: int
):

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        INSERT INTO signup_sessions
        (
            guild_id,
            signup_type,
            message_id,
            channel_id,
            active,
            created_at
        )
        VALUES (?, ?, ?, ?, 1, ?)
    """, (
        guild_id,
        signup_type,
        message_id,
        channel_id,
        now_utc()
    ))

    session_id = cursor.lastrowid

    db.commit()
    db.close()

    return session_id


def add_signup(
    session_id: int,
    discord_user_id: int,
    player_name: str
):

    db = get_db()
    cursor = db.cursor()

    # --------------------------------------------------------
    # Check if this Discord account has already signed up.
    # --------------------------------------------------------

    cursor.execute("""
        SELECT id
        FROM signups
        WHERE session_id = ?
        AND discord_user_id = ?
    """, (
        session_id,
        discord_user_id
    ))

    existing = cursor.fetchone()

    if existing:

        db.close()

        return False

    cursor.execute("""
        INSERT INTO signups
        (
            session_id,
            discord_user_id,
            player_name,
            created_at
        )
        VALUES (?, ?, ?, ?)
    """, (
        session_id,
        discord_user_id,
        player_name,
        now_utc()
    ))

    db.commit()
    db.close()

    return True


def get_signup_count(
    session_id: int
):

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        SELECT COUNT(*) AS count
        FROM signups
        WHERE session_id = ?
    """, (
        session_id,
    ))

    count = cursor.fetchone()["count"]

    db.close()

    return count


def get_signups(
    session_id: int
):

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        SELECT discord_user_id, player_name
        FROM signups
        WHERE session_id = ?
        ORDER BY id ASC
    """, (
        session_id,
    ))

    rows = cursor.fetchall()

    db.close()

    return rows


def close_session(
    session_id: int
):

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        UPDATE signup_sessions
        SET active = 0,
            closed_at = ?
        WHERE id = ?
    """, (
        now_utc(),
        session_id
    ))

    db.commit()
    db.close()


# ============================================================
# SIGNUP INFORMATION
# ============================================================

SIGNUP_INFO = {

    "fight_night": {

        "title": "PFO Fight Night Sign-Ups",

        "message": (
            "PFO Fight Night Sign-Ups, "
            "Press The Button Below To Sign Up!"
        )
    },

    "live_card": {

        "title": "PFO Live Card Sign Ups",

        "message": (
            "PFO Live Card Sign Ups, "
            "Press The Button Below To Sign Up!"
        )
    }
}

SIGNUP_NO_GUARANTEE = (
    "Signing up does not guarantee you a fight. Fights are picked by staff."
)

# Short names used only for the small line above the embed title.
SIGNUP_LABELS = {
    "fight_night": "FIGHT NIGHT",
    "live_card": "LIVE CARD",
}


# ============================================================
# SIGN-UP TIMES (when it closes, when the card is out)
# ============================================================

def setup_signup_time_columns():

    db = get_db()
    cursor = db.cursor()
    cursor.execute("PRAGMA table_info(signup_sessions)")
    columns = [column[1] for column in cursor.fetchall()]

    if "closes_at" not in columns:
        cursor.execute("ALTER TABLE signup_sessions ADD COLUMN closes_at INTEGER")

    if "card_out" not in columns:
        cursor.execute("ALTER TABLE signup_sessions ADD COLUMN card_out TEXT")

    db.commit()
    db.close()


def session_value(session, key: str):
    try:
        return session[key]
    except (IndexError, KeyError):
        return None


def signup_time_lines(
    session_id: int,
    closed: bool = False
):

    try:
        session = get_session(session_id)
    except Exception:
        return []

    if not session:
        return []

    lines = []
    closes_at = session_value(session, "closes_at")
    card_out = session_value(session, "card_out")

    if closes_at and not closed:
        lines.append(f"⏰ **Sign-ups close:** <t:{closes_at}:F> (<t:{closes_at}:R>)")

    if card_out:
        if card_out.startswith("ts:"):
            unix = int(card_out[3:])
            lines.append(f"📋 **Card out:** <t:{unix}:F> (<t:{unix}:R>)")
        else:
            lines.append(f"📋 **Card out:** {card_out}")

    return lines


def read_signup_times(
    closes: str,
    card_out: str
):
    """
    Returns (closes_at unix or None, card_out text or None, error or None).
    'none' clears a time. card_out can be a date or any text.
    """

    closes_at = None
    card_value = None

    if closes and closes.strip().lower() not in ("none", "clear", "-"):
        moment = parse_event_datetime(closes)
        if not moment:
            return None, None, (
                "❌ Write when sign-ups close like **24/10/2026 18:00** or **24/10 6pm** (UK time)."
            )
        if moment <= datetime.now(timezone.utc):
            return None, None, "❌ That closing time has already passed."
        closes_at = int(moment.timestamp())

    if card_out and card_out.strip().lower() not in ("none", "clear", "-"):
        moment = parse_event_datetime(card_out)
        card_value = f"ts:{int(moment.timestamp())}" if moment else card_out.strip()[:100]

    return closes_at, card_value, None


def set_signup_times(
    session_id: int,
    closes_at,
    card_out,
    change_closes: bool = True,
    change_card: bool = True
):

    db = get_db()
    cursor = db.cursor()

    if change_closes:
        cursor.execute("UPDATE signup_sessions SET closes_at = ? WHERE id = ?", (closes_at, session_id))

    if change_card:
        cursor.execute("UPDATE signup_sessions SET card_out = ? WHERE id = ?", (card_out, session_id))

    db.commit()
    db.close()


async def close_signup(
    guild: discord.Guild,
    session,
    signup_type: str
):
    """Closes a sign-up: list locked, post switched to CLOSED, missed sign-up report."""

    count = get_signup_count(session["id"])

    close_session(session["id"])

    try:
        channel = guild.get_channel(session["channel_id"])

        if channel:
            message = await channel.fetch_message(session["message_id"])
            await message.edit(
                embed=create_closed_embed(signup_type, session["id"]),
                view=None
            )

    except Exception as error:
        print(f"Could not update closed signup: {error}")

    # Flag fighters who didn't sign up (posted in the staff bot channel).
    try:
        await report_missed_signup(
            guild,
            session["id"],
            SIGNUP_INFO[signup_type]["title"]
        )
    except Exception as error:
        print(f"Missed sign-up report error: {error}")

    return count


async def close_due_signups():
    """Sign-ups with a closing time close by themselves."""

    now = int(time.time())

    for session in discipline_query(
        "SELECT * FROM signup_sessions WHERE active = 1 AND closes_at IS NOT NULL AND closes_at <= ?",
        (now,)
    ):
        guild = bot.get_guild(session["guild_id"])

        if guild:
            await close_signup(guild, session, session["signup_type"])
        else:
            close_session(session["id"])


async def start_signup(
    interaction: discord.Interaction,
    signup_type: str,
    closes: str = None,
    card_out: str = None
):

    if not is_admin(interaction):

        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can create signups.",
            ephemeral=True
        )

        return

    label = "Fight Night" if signup_type == "fight_night" else "Live Card"

    if get_active_session(interaction.guild.id, signup_type):

        await interaction.response.send_message(
            f"❌ There is already an active {label} signup.\n"
            "Close it first with `/signupclose`.",
            ephemeral=True
        )

        return

    closes_at, card_value, error = read_signup_times(closes, card_out)

    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return

    await interaction.response.send_message(
        f"Creating {label} signup..."
    )

    message = await interaction.original_response()

    session_id = create_session(
        interaction.guild.id,
        signup_type,
        message.id,
        interaction.channel.id
    )

    set_signup_times(session_id, closes_at, card_value)

    await message.edit(
        content=None,
        embed=create_signup_embed(
            signup_type,
            session_id
        ),
        view=SignupView(
            session_id,
            signup_type
        )
    )


# ============================================================
# SIGNUP EMBED
# ============================================================

def create_signup_embed(
    signup_type: str,
    session_id: int
):

    info = SIGNUP_INFO[signup_type]

    count = get_signup_count(
        session_id
    )

    times = signup_time_lines(session_id)

    embed = discord.Embed(
        title=info["title"],
        description=(
            f"**{info['message']}**\n\n"
            f"🥊 **Current Sign-Ups: {count}**\n"
            f"🟢 **Status:** Open\n"
            + (("\n" + "\n".join(times) + "\n") if times else "")
            + f"\n⚠️ *{SIGNUP_NO_GUARANTEE}*"
        ),
        color=PFO_GOLD,
        timestamp=discord.utils.utcnow()
    )

    apply_branding(
        embed,
        banner_file=signup_banner_file(signup_type),
        author_text=f"PFO • {SIGNUP_LABELS[signup_type]}"
    )

    embed.set_footer(
        text="Press Sign Up to join • Press Withdraw to take yourself off the list",
        icon_url=asset_url(LOGO_FILE)
    )

    return embed


def create_closed_embed(
    signup_type: str,
    session_id: int
):

    info = SIGNUP_INFO[signup_type]

    count = get_signup_count(
        session_id
    )

    embed = discord.Embed(
        title=info["title"],
        description=(
            f"🔒 **Sign-Ups Closed! [{count} SIGN-UPS]!**\n\n"
            "This signup is no longer accepting entries."
            + "".join(f"\n\n{line}" for line in signup_time_lines(session_id, closed=True))
        ),
        color=PFO_GOLD,
        timestamp=discord.utils.utcnow()
    )

    apply_branding(
        embed,
        banner_file=signup_banner_file(
            signup_type,
            closed=True
        ),
        author_text=f"PFO • {SIGNUP_LABELS[signup_type]}"
    )

    embed.set_footer(
        text="Closed"
    )

    return embed


# ============================================================
# SIGNUP BUTTON VIEW
# ============================================================

class SignupView(
    discord.ui.View
):

    def __init__(
        self,
        session_id: int,
        signup_type: str
    ):

        super().__init__(
            timeout=None
        )

        self.session_id = session_id
        self.signup_type = signup_type

        button = discord.ui.Button(
            label="Sign Up",
            style=discord.ButtonStyle.green,
            emoji="🥊",
            custom_id=f"pfo_signup_{session_id}"
        )

        button.callback = self.signup_button

        self.add_item(
            button
        )

        withdraw = discord.ui.Button(
            label="Withdraw",
            style=discord.ButtonStyle.red,
            emoji="↩️",
            custom_id=f"pfo_withdraw_{session_id}"
        )

        withdraw.callback = self.withdraw_button

        self.add_item(
            withdraw
        )

    async def withdraw_button(
        self,
        interaction: discord.Interaction
    ):

        session = get_session(
            self.session_id
        )

        if not session or session["active"] != 1:

            await interaction.response.send_message(
                "❌ Sign-ups are closed, so the list can't be changed. "
                "Please contact a member of staff.",
                ephemeral=True
            )

            return

        if not is_signed_up(self.session_id, interaction.user.id):

            await interaction.response.send_message(
                "❌ You're not on the sign-up list.",
                ephemeral=True
            )

            return

        await interaction.response.send_message(
            (
                "⚠️ **This will take you off the sign-up list** for "
                f"**{SIGNUP_INFO[self.signup_type]['title']}**.\n\n"
                "Are you sure you want to withdraw?"
            ),
            view=WithdrawConfirmView(
                self.session_id,
                self.signup_type,
                interaction.user.id
            ),
            ephemeral=True
        )

    async def signup_button(
        self,
        interaction: discord.Interaction
    ):

        session = get_session(
            self.session_id
        )

        # ----------------------------------------------------
        # Make sure the signup is still open.
        # ----------------------------------------------------

        if not session or session["active"] != 1:

            await interaction.response.send_message(
                "❌ This signup is closed.",
                ephemeral=True
            )

            return

        # ----------------------------------------------------
        # Suspended fighters (2 warnings) can't sign up.
        # ----------------------------------------------------

        try:
            suspension = get_active_suspension(interaction.guild.id, interaction.user.id)
        except Exception as error:
            print(f"Suspension check failed: {error}")
            suspension = None

        if suspension:

            await interaction.response.send_message(
                suspension_signup_text(suspension),
                ephemeral=True
            )

            return

        # ----------------------------------------------------
        # Check if they already signed up.
        # ----------------------------------------------------

        db = get_db()
        cursor = db.cursor()

        cursor.execute("""
            SELECT id
            FROM signups
            WHERE session_id = ?
            AND discord_user_id = ?
        """, (
            self.session_id,
            interaction.user.id
        ))

        existing = cursor.fetchone()

        db.close()

        if existing:

            await interaction.response.send_message(
                "❌ You are already signed up for this card.",
                ephemeral=True
            )

            return

        # ----------------------------------------------------
        # SAVE THEIR DISCORD ACCOUNT.
        #
        # We save the user ID.
        #
        # The player name is only saved as a backup/snapshot.
        # /signuppaste uses the Discord ID to create:
        #
        # <@123456789>
        #
        # which Discord displays as their @mention.
        # ----------------------------------------------------

        success = add_signup(
            self.session_id,
            interaction.user.id,
            interaction.user.display_name
        )

        if not success:

            await interaction.response.send_message(
                "❌ You are already signed up for this card.",
                ephemeral=True
            )

            return

        # ----------------------------------------------------
        # Tell the player they are signed up.
        # ----------------------------------------------------

        count = get_signup_count(
            self.session_id
        )

        await interaction.response.send_message(
            (
                "✅ **You have been signed up!**\n\n"
                f"**Current Sign-Ups: {count}**\n\n"
                f"⚠️ {SIGNUP_NO_GUARANTEE} "
                "Changed your mind? Press **Withdraw** to take yourself off the list."
            ),
            ephemeral=True
        )

        # ----------------------------------------------------
        # Update the signup box.
        # ----------------------------------------------------

        try:

            channel = interaction.guild.get_channel(
                session["channel_id"]
            )

            if channel:

                message = await channel.fetch_message(
                    session["message_id"]
                )

                await message.edit(
                    embed=create_signup_embed(
                        self.signup_type,
                        self.session_id
                    ),
                    view=SignupView(
                        self.session_id,
                        self.signup_type
                    )
                )

        except Exception as error:

            print(
                f"Could not update signup message: {error}"
            )


def is_signed_up(
    session_id: int,
    discord_user_id: int
):

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        SELECT id
        FROM signups
        WHERE session_id = ?
        AND discord_user_id = ?
    """, (
        session_id,
        discord_user_id
    ))

    found = cursor.fetchone() is not None

    db.close()

    return found


def remove_signup(
    session_id: int,
    discord_user_id: int
):

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        DELETE FROM signups
        WHERE session_id = ?
        AND discord_user_id = ?
    """, (
        session_id,
        discord_user_id
    ))

    removed = cursor.rowcount > 0

    db.commit()
    db.close()

    return removed


async def refresh_signup_message(
    guild: discord.Guild,
    session,
    signup_type: str
):

    try:

        channel = guild.get_channel(
            session["channel_id"]
        )

        if channel:

            message = await channel.fetch_message(
                session["message_id"]
            )

            await message.edit(
                embed=create_signup_embed(
                    signup_type,
                    session["id"]
                ),
                view=SignupView(
                    session["id"],
                    signup_type
                )
            )

    except Exception as error:

        print(
            f"Could not update signup message: {error}"
        )


class WithdrawConfirmView(
    discord.ui.View
):
    """'Are you sure?' step before taking someone off the list."""

    def __init__(
        self,
        session_id: int,
        signup_type: str,
        user_id: int
    ):

        super().__init__(
            timeout=120
        )

        self.session_id = session_id
        self.signup_type = signup_type
        self.user_id = user_id

        confirm = discord.ui.Button(
            label="Yes, take me off the list",
            style=discord.ButtonStyle.red
        )
        confirm.callback = self.confirm

        cancel = discord.ui.Button(
            label="Stay signed up",
            style=discord.ButtonStyle.grey
        )
        cancel.callback = self.cancel

        self.add_item(confirm)
        self.add_item(cancel)

    async def confirm(
        self,
        interaction: discord.Interaction
    ):

        self.stop()

        session = get_session(self.session_id)

        if not session or session["active"] != 1:

            await interaction.response.edit_message(
                content="❌ Sign-ups have closed, so the list can't be changed. "
                        "Please contact a member of staff.",
                view=None
            )

            return

        remove_signup(
            self.session_id,
            self.user_id
        )

        await interaction.response.edit_message(
            content=(
                "✅ **You've been taken off the sign-up list.**\n"
                "You can sign up again while sign-ups are still open."
            ),
            view=None
        )

        await refresh_signup_message(
            interaction.guild,
            session,
            self.signup_type
        )

    async def cancel(
        self,
        interaction: discord.Interaction
    ):

        self.stop()

        await interaction.response.edit_message(
            content="👍 You're still signed up.",
            view=None
        )


# ============================================================
# ADMIN CHECK
# ============================================================

# Who counts as staff. Every command except /profile is staff-only.
# Role names are matched ignoring capitals, spaces, dashes and emojis,
# so "👑 Owner", "owner" and "OWNER" all count as Owner.
#
# To use different role names without editing this file, set
# STAFF_ROLES / FIGHTER_ROLES in Railway, comma-separated, e.g.
#   STAFF_ROLES=Helper,Moderator,Vice President,Owner
STAFF_ROLE_NAMES = [
    name.strip()
    for name in os.getenv(
        "STAFF_ROLES",
        "Helper,Moderator,Vice President,Owner"
    ).split(",")
    if name.strip()
]

FIGHTER_ROLE_NAMES = [
    name.strip()
    for name in os.getenv(
        "FIGHTER_ROLES",
        "Fighter"
    ).split(",")
    if name.strip()
]

STAFF_ONLY_TEXT = (
    "Only staff ("
    + ", ".join(STAFF_ROLE_NAMES[:-1])
    + (" or " if len(STAFF_ROLE_NAMES) > 1 else "")
    + STAFF_ROLE_NAMES[-1]
    + ")"
)


def role_key(
    name: str
):

    return "".join(
        ch for ch in name.casefold()
        if ch.isalnum()
    )


def has_any_role(
    member,
    role_names: list
):

    wanted = {
        role_key(name)
        for name in role_names
    }

    return any(
        role_key(role.name) in wanted
        for role in getattr(member, "roles", [])
    )


def is_staff(
    interaction: discord.Interaction
):

    if not interaction.guild:
        return False

    # The server owner can always use everything, so nobody can
    # lock themselves out by renaming a role.
    if interaction.user.id == interaction.guild.owner_id:
        return True

    return has_any_role(
        interaction.user,
        STAFF_ROLE_NAMES
    )


def is_fighter_or_staff(
    interaction: discord.Interaction
):

    if is_staff(interaction):
        return True

    return has_any_role(
        interaction.user,
        FIGHTER_ROLE_NAMES
    )


def is_admin(
    interaction: discord.Interaction
):
    # Kept under its old name so every existing command uses the
    # new staff roles without any other changes.
    return is_staff(
        interaction
    )


# ============================================================
# /FNSIGNUP
# ============================================================

@bot.tree.command(
    name="fnsignup",
    description="Create a new PFO Fight Night signup."
)
@app_commands.describe(
    closes="Optional: when sign-ups close by themselves, UK time, e.g. 24/10/2026 18:00",
    card_out="Optional: when the card will be out, e.g. 25/10/2026 12:00 (or any text)"
)
async def fnsignup(
    interaction: discord.Interaction,
    closes: str = None,
    card_out: str = None
):

    await start_signup(interaction, "fight_night", closes, card_out)


# ============================================================
# /LIVESIGNUP
# ============================================================

@bot.tree.command(
    name="livesignup",
    description="Create a new PFO Live Card signup."
)
@app_commands.describe(
    closes="Optional: when sign-ups close by themselves, UK time, e.g. 24/10/2026 18:00",
    card_out="Optional: when the card will be out, e.g. 25/10/2026 12:00 (or any text)"
)
async def livesignup(
    interaction: discord.Interaction,
    closes: str = None,
    card_out: str = None
):

    await start_signup(interaction, "live_card", closes, card_out)


# ============================================================
# /SIGNUPCLOSE
# ============================================================

@bot.tree.command(
    name="signupclose",
    description="Close the current active signup."
)
@app_commands.describe(
    signup_type="Which signup do you want to close?"
)
@app_commands.choices(
    signup_type=[
        app_commands.Choice(
            name="Fight Night",
            value="fight_night"
        ),
        app_commands.Choice(
            name="Live Card",
            value="live_card"
        )
    ]
)
async def signupclose(
    interaction: discord.Interaction,
    signup_type: app_commands.Choice[str]
):

    if not is_admin(interaction):

        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can close signups.",
            ephemeral=True
        )

        return

    signup_type_value = signup_type.value

    session = get_active_session(
        interaction.guild.id,
        signup_type_value
    )

    if not session:

        await interaction.response.send_message(
            f"❌ There is no active "
            f"{SIGNUP_INFO[signup_type_value]['title']} signup.",
            ephemeral=True
        )

        return

    await interaction.response.defer(ephemeral=True)

    count = await close_signup(
        interaction.guild,
        session,
        signup_type_value
    )

    await interaction.followup.send(
        f"✅ Signup closed.\n\n"
        f"**Sign-Ups Closed! [{count}]!**",
        ephemeral=True
    )


@bot.tree.command(
    name="signuptimes",
    description="Change when an open sign-up closes or when the card will be out."
)
@app_commands.describe(
    signup_type="Which sign-up?",
    closes="When sign-ups close, UK time, e.g. 24/10/2026 18:00 ('none' to remove)",
    card_out="When the card will be out, e.g. 25/10/2026 12:00 or any text ('none' to remove)"
)
@app_commands.choices(
    signup_type=[
        app_commands.Choice(name="Fight Night", value="fight_night"),
        app_commands.Choice(name="Live Card", value="live_card"),
    ]
)
async def signuptimes(
    interaction: discord.Interaction,
    signup_type: app_commands.Choice[str],
    closes: str = None,
    card_out: str = None
):

    if not is_admin(interaction):
        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can change sign-ups.",
            ephemeral=True
        )
        return

    session = get_active_session(interaction.guild.id, signup_type.value)

    if not session:
        await interaction.response.send_message(
            f"❌ There is no open {SIGNUP_INFO[signup_type.value]['title']}.",
            ephemeral=True
        )
        return

    if closes is None and card_out is None:
        await interaction.response.send_message(
            "❌ Fill in **closes**, **card_out** or both.",
            ephemeral=True
        )
        return

    closes_at, card_value, error = read_signup_times(closes, card_out)

    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return

    set_signup_times(
        session["id"], closes_at, card_value,
        change_closes=closes is not None,
        change_card=card_out is not None
    )

    await interaction.response.send_message(
        "✅ Updated:\n" + ("\n".join(signup_time_lines(session["id"])) or "Times removed."),
        ephemeral=True
    )

    await refresh_signup_message(interaction.guild, get_session(session["id"]), signup_type.value)


# ============================================================
# /SIGNUPPASTE
# ============================================================

@bot.tree.command(
    name="signuppaste",
    description="Paste the current signup list."
)
@app_commands.describe(
    signup_type="Which signup list do you want to paste?"
)
@app_commands.choices(
    signup_type=[
        app_commands.Choice(
            name="Fight Night",
            value="fight_night"
        ),
        app_commands.Choice(
            name="Live Card",
            value="live_card"
        )
    ]
)
async def signuppaste(
    interaction: discord.Interaction,
    signup_type: app_commands.Choice[str]
):

    if not is_staff(interaction):

        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can paste the signup list.",
            ephemeral=True
        )

        return

    signup_type_value = signup_type.value

    session = get_active_session(
        interaction.guild.id,
        signup_type_value
    )

    if not session:

        await interaction.response.send_message(
            f"❌ There is no active "
            f"{SIGNUP_INFO[signup_type_value]['title']} signup.",
            ephemeral=True
        )

        return

    signups = get_signups(
        session["id"]
    )

    if not signups:

        await interaction.response.send_message(
            "❌ Nobody has signed up yet.",
            ephemeral=True
        )

        return

    # --------------------------------------------------------
    # BUILD CLICKABLE SERVER DISPLAY NAMES
    #
    # We resolve the member from this server so the current
    # server display name/nickname is shown. The name links
    # directly to their Discord profile.
    # --------------------------------------------------------

    lines = []

    for number, signup in enumerate(
        signups,
        start=1
    ):
        member = await get_server_member(
            interaction.guild,
            signup["discord_user_id"]
        )

        if member:
            fighter_name = member.display_name
            fighter_link = (
                f"[{fighter_name}]"
                f"(https://discord.com/users/{member.id})"
            )
        else:
            fighter_link = "Unknown Fighter"

        lines.append(
            f"**{number}.** {fighter_link}"
        )

    embed = discord.Embed(
        title=SIGNUP_INFO[
            signup_type_value
        ]["title"],
        description="\n".join(lines),
        color=PFO_GOLD,
        timestamp=discord.utils.utcnow()
    )

    apply_branding(
        embed,
        banner_file=signup_banner_file(signup_type_value),
        author_text=f"PFO • {SIGNUP_LABELS[signup_type_value]} • FIGHTER LIST"
    )

    embed.set_footer(
        text=f"🥊 Total Sign-Ups: {len(signups)}",
        icon_url=asset_url(LOGO_FILE)
    )

    await interaction.response.send_message(
        embed=embed
    )


# ============================================================
# RANKING DATABASE
# ============================================================

def get_division_rankings(
    guild_id: int,
    weight: str
):

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        SELECT *
        FROM rankings
        WHERE guild_id = ?
        AND weight = ?
        ORDER BY rank ASC
    """, (
        guild_id,
        weight
    ))

    rows = cursor.fetchall()

    db.close()

    return rows


def get_user_ranking_in_division(
    guild_id: int,
    discord_user_id: int,
    weight: str
):

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        SELECT *
        FROM rankings
        WHERE guild_id = ?
        AND discord_user_id = ?
        AND weight = ?
        LIMIT 1
    """, (
        guild_id,
        discord_user_id,
        weight
    ))

    row = cursor.fetchone()

    db.close()

    return row


def get_user_weight_class(
    guild_id: int,
    discord_user_id: int
):

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        SELECT *
        FROM rankings
        WHERE guild_id = ?
        AND discord_user_id = ?
        AND weight != ?
        LIMIT 1
    """, (
        guild_id,
        discord_user_id,
        P4P_WEIGHT
    ))

    row = cursor.fetchone()

    db.close()

    return row


def delete_user_from_division(
    guild_id: int,
    weight: str,
    discord_user_id: int
):

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        DELETE FROM rankings
        WHERE guild_id = ?
        AND weight = ?
        AND discord_user_id = ?
    """, (
        guild_id,
        weight,
        discord_user_id
    ))

    db.commit()
    db.close()


def save_division_rankings(
    guild_id: int,
    weight: str,
    fighters: list
):

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        DELETE FROM rankings
        WHERE guild_id = ?
        AND weight = ?
    """, (
        guild_id,
        weight
    ))

    for fighter in fighters:

        cursor.execute("""
            INSERT INTO rankings
            (
                guild_id,
                weight,
                discord_user_id,
                rank,
                movement,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            guild_id,
            weight,
            fighter["user_id"],
            fighter["rank"],
            fighter["movement"],
            now_utc()
        ))

    db.commit()
    db.close()


# ============================================================
# RANKING MESSAGE DATABASE
# ============================================================

def save_ranking_message(
    guild_id: int,
    weight: str,
    channel_id: int,
    message_id: int
):

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        INSERT OR REPLACE INTO ranking_messages
        (
            guild_id,
            weight,
            channel_id,
            message_id
        )
        VALUES (?, ?, ?, ?)
    """, (
        guild_id,
        weight,
        channel_id,
        message_id
    ))

    db.commit()
    db.close()


def get_ranking_message(
    guild_id: int,
    weight: str
):

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        SELECT *
        FROM ranking_messages
        WHERE guild_id = ?
        AND weight = ?
    """, (
        guild_id,
        weight
    ))

    row = cursor.fetchone()

    db.close()

    return row


def ranking_author_text(
    weight: str
):
    """
    The small line at the top of every ranking board,
    e.g. "PFO • FLYWEIGHT DIVISION".
    """

    if weight == P4P_WEIGHT:
        return "PFO • POUND FOR POUND"

    return f"PFO • {weight.upper()} DIVISION"


async def find_existing_ranking_message(
    channel,
    weight: str
):

    # Boards posted by the older version of the bot have a title.
    # Boards posted by this version have no title, so they are
    # found by the line at the top instead. Both are matched, so
    # /rankingsp keeps editing your existing boards either way.

    if weight == P4P_WEIGHT:

        expected_title = (
            "🏆 PFO P4P RANKINGS"
        )

    else:

        expected_title = (
            f"🏆 PFO UFC RANKINGS — {weight.upper()}"
        )

    expected_author = ranking_author_text(
        weight
    )

    try:

        async for message in channel.history(
            limit=100
        ):

            if not message.embeds:
                continue

            title = message.embeds[0].title
            author = message.embeds[0].author.name

            if title == expected_title or author == expected_author:

                return message

    except Exception as error:

        print(
            f"Could not search for existing "
            f"{weight} ranking: {error}"
        )

    return None


# ============================================================
# RANKING DISPLAY
# ============================================================

# People who aren't in the server are remembered for 10 minutes,
# so refreshing ranking boards doesn't ask Discord about them
# again and again (too many requests gets the bot blocked).
MISSING_MEMBER_CACHE = {}
MISSING_MEMBER_SECONDS = 600


async def get_server_member(
    guild: discord.Guild,
    user_id: int
):
    member = guild.get_member(user_id)

    if member:
        return member

    key = (guild.id, user_id)
    missing_since = MISSING_MEMBER_CACHE.get(key)

    if missing_since and time.monotonic() - missing_since < MISSING_MEMBER_SECONDS:
        return None

    try:
        member = await guild.fetch_member(user_id)
        MISSING_MEMBER_CACHE.pop(key, None)
        return member
    except (
        discord.NotFound,
        discord.Forbidden,
        discord.HTTPException
    ):
        MISSING_MEMBER_CACHE[key] = time.monotonic()
        return None


def movement_icon(
    movement: int
):

    if movement > 0:

        return "🟢⬆️"

    if movement < 0:

        return "🔴⬇️"

    return "▫️"


# ------------------------------------------------------------
# Ranking text formatting (looks only)
# ------------------------------------------------------------

def rank_tag(
    rank: int
):
    """
    Rank number in a small grey box, padded so #1 to #15
    all line up:  ` #1`  ` #9`  `#10`
    """

    return f"`{('#' + str(rank)).rjust(3)}`"


def fighter_link_text(
    member
):

    if member:
        return (
            f"**[{member.display_name}]"
            f"(https://discord.com/users/{member.id})**"
        )

    return "*Unknown Fighter*"


def ranking_movement_icon(
    movement: int
):
    """
    Single-emoji version of the movement marker, so it never
    drops onto its own line on phones.
    """

    if movement > 0:
        return "🟢"

    if movement < 0:
        return "🔴"

    return "▫️"


async def build_rank_lines(
    guild: discord.Guild,
    ranking_dict: dict
):

    lines = []

    for rank in range(1, 16):
        row = ranking_dict.get(
            rank
        )

        if row:
            member = await get_server_member(
                guild,
                row["discord_user_id"]
            )

            lines.append(
                f"{rank_tag(rank)} "
                f"{ranking_movement_icon(row['movement'])} "
                f"{fighter_link_text(member)}"
            )

        else:
            lines.append(
                f"{rank_tag(rank)} ▫️ *Vacant*"
            )

    return lines


RANKING_FOOTER = (
    "🟢 Moved Up    "
    "🔴 Moved Down    "
    "▫️ No Change"
)


async def create_ranking_embed(
    guild: discord.Guild,
    weight: str
):

    rankings = get_division_rankings(
        guild.id,
        weight
    )

    ranking_dict = {
        row["rank"]: row
        for row in rankings
    }

    # No title: the line at the top (author line) names the board.
    embed = discord.Embed(
        color=PFO_GOLD,
        timestamp=discord.utils.utcnow()
    )

    apply_branding(
        embed,
        banner_file=ranking_banner_file(weight),
        logo_file=LOGO_FILE,
        author_text=ranking_author_text(weight),
        # No top-right logo on ranking boards: on phones it squeezes
        # the names into a narrow column. The logo still shows in
        # the top line and in the banner.
        show_thumbnail=False
    )

    lines = await build_rank_lines(
        guild,
        ranking_dict
    )

    # ========================================================
    # P4P (no champion)
    # ========================================================

    if weight == P4P_WEIGHT:

        description = (
            "**🥊 RANKINGS**\n"
            + "\n".join(lines)
        )

    # ========================================================
    # WEIGHT CLASS (champion + #1-#15)
    # ========================================================

    else:

        champion = ranking_dict.get(
            0
        )

        if champion:
            member = await get_server_member(
                guild,
                champion["discord_user_id"]
            )

            champion_text = f"🏆 {fighter_link_text(member)}"

        else:
            champion_text = "🏆 *Vacant*"

        description = (
            "**👑 CHAMPION**\n"
            f"{champion_text}\n"
            "\n"
            "**🥊 RANKINGS**\n"
            + "\n".join(lines)
        )

    # The whole board sits in one block (limit 4096 characters),
    # so it never splits into separate chunks with gaps.
    embed.description = description

    embed.set_footer(
        text=RANKING_FOOTER,
        icon_url=asset_url(LOGO_FILE)
    )

    return embed


# ============================================================
# MOVEMENT CALCULATIONS
# ============================================================

def calculate_movements(
    old_positions: dict,
    new_positions: dict
):

    movements = {}

    for user_id, new_rank in new_positions.items():

        old_rank = old_positions.get(
            user_id
        )

        if old_rank is None:

            movements[user_id] = 0

        elif new_rank < old_rank:

            movements[user_id] = 1

        elif new_rank > old_rank:

            movements[user_id] = -1

        else:

            movements[user_id] = 0

    return movements


# ============================================================
# UPDATE RANKING
# ============================================================

def update_ranking(
    guild_id: int,
    weight: str,
    user_id: int,
    desired_rank: int
):
    """
    Places a fighter into the selected ranking only.

    P4P and every weight class are completely independent.

    A fighter can therefore be ranked in multiple weight classes
    AND in P4P at the same time.

    Example:
        Lightweight #1
        Welterweight #5
        Middleweight #12
        P4P #4

    Updating one division never removes the fighter from any
    other division.
    """

    # ========================================================
    # P4P
    # ========================================================

    if weight == P4P_WEIGHT:

        if desired_rank < 1 or desired_rank > 15:
            raise ValueError(
                "P4P rank must be between #1 and #15."
            )

        old_rows = get_division_rankings(
            guild_id,
            P4P_WEIGHT
        )

        old_positions = {
            row["discord_user_id"]: row["rank"]
            for row in old_rows
        }

        users = [
            row["discord_user_id"]
            for row in old_rows
            if row["discord_user_id"] != user_id
        ]

        insert_index = min(
            desired_rank - 1,
            len(users)
        )

        users.insert(
            insert_index,
            user_id
        )

        users = users[:15]

        new_positions = {
            fighter_id: index + 1
            for index, fighter_id in enumerate(users)
        }

        movements = calculate_movements(
            old_positions,
            new_positions
        )

        fighters = []

        for fighter_id, rank in new_positions.items():
            fighters.append({
                "user_id": fighter_id,
                "rank": rank,
                "movement": movements.get(
                    fighter_id,
                    0
                )
            })

        save_division_rankings(
            guild_id,
            P4P_WEIGHT,
            fighters
        )

        return None

    # ========================================================
    # WEIGHT CLASS
    # ========================================================

    if desired_rank < 0 or desired_rank > 15:
        raise ValueError(
            "Weight-class rank must be Champion or #1-#15."
        )

    # IMPORTANT:
    # Do NOT look for or remove the fighter from another weight class.
    # Fighters are allowed to exist in multiple weight classes.
    target_rows = get_division_rankings(
        guild_id,
        weight
    )

    target_positions = {
        row["discord_user_id"]: row["rank"]
        for row in target_rows
    }

    # Remove the fighter only from THIS division so we can insert
    # them at their new position without creating a duplicate row.
    delete_user_from_division(
        guild_id,
        weight,
        user_id
    )

    target_rows = get_division_rankings(
        guild_id,
        weight
    )

    champion_id = None

    for row in target_rows:
        if row["rank"] == 0:
            champion_id = row["discord_user_id"]
            break

    ranked_users = [
        row["discord_user_id"]
        for row in target_rows
        if row["rank"] >= 1
    ]

    # ========================================================
    # MAKE CHAMPION
    # ========================================================

    if desired_rank == 0:

        if champion_id is not None:
            ranked_users.insert(
                0,
                champion_id
            )

        ranked_users = list(
            dict.fromkeys(ranked_users)
        )

        ranked_users = ranked_users[:15]

        new_positions = {
            fighter_id: index + 1
            for index, fighter_id
            in enumerate(ranked_users)
        }

        movements = calculate_movements(
            target_positions,
            new_positions
        )

        fighters = [{
            "user_id": user_id,
            "rank": 0,
            "movement": 0
        }]

        for fighter_id, rank in new_positions.items():
            fighters.append({
                "user_id": fighter_id,
                "rank": rank,
                "movement": movements.get(
                    fighter_id,
                    0
                )
            })

        save_division_rankings(
            guild_id,
            weight,
            fighters
        )

    # ========================================================
    # MAKE #1-#15
    # ========================================================

    else:

        insert_index = min(
            desired_rank - 1,
            len(ranked_users)
        )

        ranked_users.insert(
            insert_index,
            user_id
        )

        ranked_users = list(
            dict.fromkeys(ranked_users)
        )

        ranked_users = ranked_users[:15]

        new_positions = {
            fighter_id: index + 1
            for index, fighter_id
            in enumerate(ranked_users)
        }

        movements = calculate_movements(
            target_positions,
            new_positions
        )

        fighters = []

        if champion_id is not None:
            fighters.append({
                "user_id": champion_id,
                "rank": 0,
                "movement": 0
            })

        for fighter_id, rank in new_positions.items():
            fighters.append({
                "user_id": fighter_id,
                "rank": rank,
                "movement": movements.get(
                    fighter_id,
                    0
                )
            })

        save_division_rankings(
            guild_id,
            weight,
            fighters
        )

    # No old weight class is returned because the fighter is not
    # removed from any other division anymore.
    return None


# ============================================================
# REMOVE FROM RANKING
# ============================================================

def remove_from_rankings(
    guild_id: int,
    user_id: int,
    weight: str
):
    """
    Removes a fighter from ONE ranking only.

    Example:

        /rankingsr P4P @Fighter

    removes them from P4P only.

    Their weight class remains untouched.
    """

    current = get_user_ranking_in_division(
        guild_id,
        user_id,
        weight
    )

    if not current:

        return False

    old_rows = get_division_rankings(
        guild_id,
        weight
    )

    old_positions = {
        row["discord_user_id"]: row["rank"]
        for row in old_rows
    }

    delete_user_from_division(
        guild_id,
        weight,
        user_id
    )

    remaining = get_division_rankings(
        guild_id,
        weight
    )

    # ========================================================
    # P4P
    # ========================================================

    if weight == P4P_WEIGHT:

        users = [
            row["discord_user_id"]
            for row in remaining
        ]

        new_positions = {
            fighter_id: index + 1
            for index, fighter_id
            in enumerate(users)
        }

        movements = calculate_movements(
            old_positions,
            new_positions
        )

        fighters = []

        for fighter_id, rank in new_positions.items():

            fighters.append({
                "user_id": fighter_id,
                "rank": rank,
                "movement": movements.get(
                    fighter_id,
                    0
                )
            })

        save_division_rankings(
            guild_id,
            weight,
            fighters
        )

        return True

    # ========================================================
    # WEIGHT CLASS
    # ========================================================

    champion = None
    ranked = []

    for row in remaining:

        if row["rank"] == 0:

            champion = row["discord_user_id"]

        else:

            ranked.append(
                row["discord_user_id"]
            )

    new_positions = {
        fighter_id: index + 1
        for index, fighter_id
        in enumerate(ranked)
    }

    movements = calculate_movements(
        old_positions,
        new_positions
    )

    fighters = []

    if champion is not None:

        fighters.append({
            "user_id": champion,
            "rank": 0,
            "movement": 0
        })

    for fighter_id, rank in new_positions.items():

        fighters.append({
            "user_id": fighter_id,
            "rank": rank,
            "movement": movements.get(
                fighter_id,
                0
            )
        })

    save_division_rankings(
        guild_id,
        weight,
        fighters
    )

    return True


# ============================================================
# RANKING CHOICES
# ============================================================

WEIGHT_CHOICES = [

    app_commands.Choice(
        name="P4P",
        value="P4P"
    ),

    app_commands.Choice(
        name="Heavyweight",
        value="Heavyweight"
    ),

    app_commands.Choice(
        name="Light Heavyweight",
        value="Light Heavyweight"
    ),

    app_commands.Choice(
        name="Middleweight",
        value="Middleweight"
    ),

    app_commands.Choice(
        name="Welterweight",
        value="Welterweight"
    ),

    app_commands.Choice(
        name="Lightweight",
        value="Lightweight"
    ),

    app_commands.Choice(
        name="Featherweight",
        value="Featherweight"
    ),

    app_commands.Choice(
        name="Bantamweight",
        value="Bantamweight"
    ),

    app_commands.Choice(
        name="Flyweight",
        value="Flyweight"
    )
]


# Champion + #1-#15
RANK_CHOICES = [

    app_commands.Choice(
        name="Champion",
        value=0
    )
]

for number in range(1, 16):

    RANK_CHOICES.append(
        app_commands.Choice(
            name=f"#{number}",
            value=number
        )
    )


# ============================================================
# /RANKINGSP
# ============================================================

@bot.tree.command(
    name="rankingsp",
    description="Create the PFO ranking boards."
)
async def rankingsp(
    interaction: discord.Interaction
):

    if not is_admin(interaction):

        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can create rankings.",
            ephemeral=True
        )

        return

    await interaction.response.send_message(
        "Creating PFO ranking boards..."
    )

    for weight in WEIGHTS.keys():

        existing = get_ranking_message(
            interaction.guild.id,
            weight
        )

        # ----------------------------------------------------
        # Try saved message first.
        # ----------------------------------------------------

        if existing:

            success = await refresh_ranking_message(
                interaction.guild,
                weight
            )

            if success:

                continue

        # ----------------------------------------------------
        # Search channel for an existing ranking box.
        # ----------------------------------------------------

        existing_message = (
            await find_existing_ranking_message(
                interaction.channel,
                weight
            )
        )

        if existing_message:

            save_ranking_message(
                interaction.guild.id,
                weight,
                interaction.channel.id,
                existing_message.id
            )

            await existing_message.edit(
                embed=await create_ranking_embed(
                    interaction.guild,
                    weight
                ),
                allowed_mentions=discord.AllowedMentions(
                    users=True
                )
            )

            continue

        # ----------------------------------------------------
        # Create a new message only if none exists.
        # ----------------------------------------------------

        message = await interaction.channel.send(
            embed=await create_ranking_embed(
                interaction.guild,
                weight
            )
        )

        save_ranking_message(
            interaction.guild.id,
            weight,
            interaction.channel.id,
            message.id
        )

    await interaction.edit_original_response(
        content=(
            "✅ **PFO Rankings have been created!**\n\n"
            "The ranking boards are now ready."
        )
    )


# ============================================================
# /RANKINGSU
# ============================================================

@bot.tree.command(
    name="rankingsu",
    description="Update a fighter's ranking."
)
@app_commands.describe(
    weight="Which ranking?",
    user="Which Discord member?",
    rank="Which position?"
)
@app_commands.choices(
    weight=WEIGHT_CHOICES,
    rank=RANK_CHOICES
)
async def rankingsu(
    interaction: discord.Interaction,
    weight: app_commands.Choice[str],
    user: discord.Member,
    rank: app_commands.Choice[int]
):

    if not is_admin(interaction):

        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can update rankings.",
            ephemeral=True
        )

        return

    if user.bot:

        await interaction.response.send_message(
            "❌ Bots cannot be added to the rankings.",
            ephemeral=True
        )

        return

    weight_value = weight.value
    rank_value = rank.value

    # --------------------------------------------------------
    # P4P validation.
    # --------------------------------------------------------

    if weight_value == P4P_WEIGHT:

        if rank_value < 1 or rank_value > 15:

            await interaction.response.send_message(
                "❌ P4P only uses ranks **#1-#15**.",
                ephemeral=True
            )

            return

    # --------------------------------------------------------
    # Weight class validation.
    # --------------------------------------------------------

    else:

        if rank_value < 0 or rank_value > 15:

            await interaction.response.send_message(
                "❌ Rank must be **Champion or #1-#15**.",
                ephemeral=True
            )

            return

    await interaction.response.defer(
        ephemeral=True
    )

    try:

        old_weight = update_ranking(
            interaction.guild.id,
            weight_value,
            user.id,
            rank_value
        )

        # Update the ranking that changed.
        await refresh_ranking_message(
            interaction.guild,
            weight_value
        )

        # If they changed weight classes,
        # update the old weight-class box too.
        if old_weight and old_weight != weight_value:

            await refresh_ranking_message(
                interaction.guild,
                old_weight
            )

        if weight_value == P4P_WEIGHT:

            position_text = f"#{rank_value}"

        elif rank_value == 0:

            position_text = "Champion"

        else:

            position_text = f"#{rank_value}"

        await interaction.followup.send(
            (
                f"✅ **{user.display_name}** has been placed at "
                f"**{position_text}** in **{weight_value}**."
            ),
            ephemeral=True
        )

    except Exception as error:

        print(
            f"Ranking update error: {error}"
        )

        await interaction.followup.send(
            "❌ Something went wrong while updating the rankings.",
            ephemeral=True
        )


# ============================================================
# /RANKINGSR
# ============================================================

@bot.tree.command(
    name="rankingsr",
    description="Remove a fighter from a specific ranking."
)
@app_commands.describe(
    weight="Which ranking do you want to remove them from?",
    user_id="Enter the fighter's Discord User ID. This works even if they left the server."
)
@app_commands.choices(
    weight=WEIGHT_CHOICES
)
async def rankingsr(
    interaction: discord.Interaction,
    weight: app_commands.Choice[str],
    user_id: str
):

    if not is_admin(interaction):

        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can remove fighters.",
            ephemeral=True
        )

        return

    weight_value = weight.value

    # Discord User IDs are 18-19 digit numbers, so they must be
    # accepted as a STRING slash-command option rather than an
    # INTEGER option.
    user_id = user_id.strip()

    if not user_id.isdigit():

        await interaction.response.send_message(
            "❌ That is not a valid Discord User ID. "
            "Enter the numeric User ID, for example `123456789012345678`.",
            ephemeral=True
        )

        return

    try:
        fighter_id = int(user_id)
    except ValueError:

        await interaction.response.send_message(
            "❌ That is not a valid Discord User ID.",
            ephemeral=True
        )

        return

    await interaction.response.defer(
        ephemeral=True
    )

    try:

        removed = remove_from_rankings(
            interaction.guild.id,
            fighter_id,
            weight_value
        )

        if not removed:

            await interaction.followup.send(
                (
                    f"❌ <@{fighter_id}> is not ranked "
                    f"in **{weight_value}**."
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions(
                    users=True
                )
            )

            return

        await refresh_ranking_message(
            interaction.guild,
            weight_value
        )

        await interaction.followup.send(
            (
                f"✅ <@{fighter_id}> has been removed "
                f"from the **{weight_value}** rankings."
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions(
                users=True
            )
        )

    except Exception as error:

        print(
            f"Ranking removal error: {error}"
        )

        await interaction.followup.send(
            "❌ Something went wrong while removing the fighter.",
            ephemeral=True
        )


# ============================================================
# /RANKINGSCLEARUNKNOWN
# ============================================================

@bot.tree.command(
    name="rankingsclearunknown",
    description="Remove fighters who have left the server from rankings."
)
async def rankingsclearunknown(
    interaction: discord.Interaction
):

    if not is_admin(interaction):
        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can clear unknown fighters.",
            ephemeral=True
        )
        return

    await interaction.response.defer(
        ephemeral=True
    )

    removed_by_weight = {}
    member_cache = {}

    # Check every ranking independently. This is important because
    # the same fighter can now appear in multiple weight classes and P4P.
    for weight in WEIGHTS.keys():

        rows = get_division_rankings(
            interaction.guild.id,
            weight
        )

        for row in rows:

            fighter_id = row["discord_user_id"]

            # Avoid checking the same Discord member repeatedly when
            # they appear in several divisions. The result is cached
            # for this command run.
            if fighter_id not in member_cache:
                member_cache[fighter_id] = await get_server_member(
                    interaction.guild,
                    fighter_id
                )

            if member_cache[fighter_id] is not None:
                continue

            removed = remove_from_rankings(
                interaction.guild.id,
                fighter_id,
                weight
            )

            if removed:
                removed_by_weight[weight] = (
                    removed_by_weight.get(weight, 0) + 1
                )

    # Refresh every board that may have changed.
    for weight in removed_by_weight:
        await refresh_ranking_message(
            interaction.guild,
            weight
        )

    total_removed = sum(
        removed_by_weight.values()
    )

    if total_removed == 0:
        await interaction.followup.send(
            "✅ No unknown fighters were found in the rankings.",
            ephemeral=True
        )
        return

    details = "\n".join(
        f"• **{weight}:** {count} removed"
        for weight, count in removed_by_weight.items()
    )

    await interaction.followup.send(
        (
            f"✅ **{total_removed} unknown ranking entr"
            f"{'y' if total_removed == 1 else 'ies'} removed.**\n\n"
            f"{details}"
        ),
        ephemeral=True
    )


# ============================================================
# REFRESH EXISTING RANKING MESSAGE
# ============================================================

async def refresh_ranking_message(
    guild: discord.Guild,
    weight: str
):

    saved_message = get_ranking_message(
        guild.id,
        weight
    )

    if not saved_message:

        return False

    try:

        channel = guild.get_channel(
            saved_message["channel_id"]
        )

        if not channel:

            return False

        message = await channel.fetch_message(
            saved_message["message_id"]
        )

        await message.edit(
            embed=await create_ranking_embed(
                guild,
                weight
            ),
            allowed_mentions=discord.AllowedMentions(
                users=True
            )
        )

        return True

    except Exception as error:

        print(
            f"Could not refresh {weight} ranking: {error}"
        )

        return False


# ============================================================
# FIGHTER RECORDS
#
# Commands:
#   /result        log a fight (staff)
#   /profile       show a fighter's record (Fighter role or staff)
#   /setrecord     set a starting record for older, untracked fights (staff)
#   /importfights  import past fights from a CSV spreadsheet (staff)
#   /importtemplate  get a blank CSV to fill in (staff)
#   /removefight   remove a fight logged by mistake (staff)
#
# None of this touches the rankings or sign-ups.
# ============================================================

METHOD_LABELS = {
    "KO/TKO": "KO/TKO",
    "SUB": "Submission",
    "DEC": "Decision",
    "DQ": "DQ",
    "DRAW": "Draw",
    "NC": "No Contest",
}

# Words accepted in spreadsheets for each method.
METHOD_ALIASES = {
    "KO/TKO": ["ko", "tko", "ko/tko", "tko/ko", "knockout", "ko tko", "ko-tko"],
    "SUB": ["sub", "submission", "subs"],
    "DEC": ["dec", "decision", "ud", "sd", "md", "unanimous decision",
            "split decision", "majority decision", "points"],
    "DQ": ["dq", "disqualification"],
    "DRAW": ["draw", "drew", "d"],
    "NC": ["nc", "no contest", "nocontest"],
}

METHOD_CHOICES = [
    app_commands.Choice(name="KO/TKO", value="KO/TKO"),
    app_commands.Choice(name="Submission", value="SUB"),
    app_commands.Choice(name="Decision", value="DEC"),
    app_commands.Choice(name="DQ", value="DQ"),
    app_commands.Choice(name="Draw", value="DRAW"),
    app_commands.Choice(name="No Contest", value="NC"),
]

DIVISION_CHOICES = [
    choice for choice in WEIGHT_CHOICES
    if choice.value != P4P_WEIGHT
]

CSV_COLUMNS = [
    "date",
    "event",
    "winner",
    "loser",
    "method",
    "round",
    "division",
    "title_fight",
]

MAX_IMPORT_ROWS = 2000


def today_iso():
    return datetime.now(timezone.utc).date().isoformat()


def normalise_name(
    text: str
):
    return " ".join(str(text).split()).casefold()


def format_record(
    wins: int,
    losses: int,
    draws: int
):
    return f"{wins}-{losses}-{draws}"


def profile_link(
    name: str,
    user_id
):

    if user_id:
        return f"[{name}](https://discord.com/users/{user_id})"

    return name


# ------------------------------------------------------------
# Fight database functions
# ------------------------------------------------------------

def add_fight(
    guild_id: int,
    fight: dict,
    source: str,
    logged_by: int,
    cursor=None
):

    own = cursor is None

    if own:
        db = get_db()
        cursor = db.cursor()

    cursor.execute("""
        INSERT INTO fights
        (
            guild_id, fight_date, event,
            winner_id, winner_name, loser_id, loser_name,
            method, round, division, title_fight,
            source, logged_by, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        guild_id,
        fight.get("date"),
        fight.get("event"),
        fight.get("winner_id"),
        fight["winner_name"],
        fight.get("loser_id"),
        fight["loser_name"],
        fight["method"],
        fight.get("round"),
        fight.get("division"),
        1 if fight.get("title_fight") else 0,
        source,
        logged_by,
        now_utc()
    ))

    fight_id = cursor.lastrowid

    if own:
        db.commit()
        db.close()

    return fight_id


def get_fight(
    guild_id: int,
    fight_id: int
):

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        SELECT *
        FROM fights
        WHERE guild_id = ?
        AND id = ?
    """, (
        guild_id,
        fight_id
    ))

    row = cursor.fetchone()

    db.close()

    return row


def delete_fight(
    guild_id: int,
    fight_id: int
):

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        DELETE FROM fights
        WHERE guild_id = ?
        AND id = ?
    """, (
        guild_id,
        fight_id
    ))

    deleted = cursor.rowcount > 0

    db.commit()
    db.close()

    return deleted


def get_fighter_fights(
    guild_id: int,
    user_id: int,
    name: str = None
):
    """
    All fights for one fighter, oldest first.
    Fights with no date (old imports) count as the oldest.

    Fighters who left and were saved by name only (no user ID)
    are looked up by name instead.
    """

    db = get_db()
    cursor = db.cursor()

    if not user_id:

        cursor.execute("""
            SELECT *
            FROM fights
            WHERE guild_id = ?
            AND (winner_id IS NULL OR loser_id IS NULL)
            ORDER BY
                fight_date IS NOT NULL,
                fight_date ASC,
                id ASC
        """, (
            guild_id,
        ))

        key = normalise_name(name or "")

        rows = [
            row for row in cursor.fetchall()
            if (row["winner_id"] is None and normalise_name(row["winner_name"]) == key)
            or (row["loser_id"] is None and normalise_name(row["loser_name"]) == key)
        ]

        db.close()

        return rows

    cursor.execute("""
        SELECT *
        FROM fights
        WHERE guild_id = ?
        AND (winner_id = ? OR loser_id = ?)
        ORDER BY
            fight_date IS NOT NULL,
            fight_date ASC,
            id ASC
    """, (
        guild_id,
        user_id,
        user_id
    ))

    rows = cursor.fetchall()

    db.close()

    return rows


def get_existing_fight_keys(
    guild_id: int
):
    """
    Used by the importer to skip fights that are already saved.
    """

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        SELECT fight_date, winner_id, winner_name,
               loser_id, loser_name, method
        FROM fights
        WHERE guild_id = ?
    """, (
        guild_id,
    ))

    rows = cursor.fetchall()

    db.close()

    return {
        fight_key(
            row["fight_date"],
            row["winner_id"], row["winner_name"],
            row["loser_id"], row["loser_name"],
            row["method"]
        )
        for row in rows
    }


def fight_key(
    date,
    winner_id,
    winner_name,
    loser_id,
    loser_name,
    method
):

    winner = str(winner_id) if winner_id else normalise_name(winner_name)
    loser = str(loser_id) if loser_id else normalise_name(loser_name)

    return (date or "", winner, loser, method)


def get_starting_record(
    guild_id: int,
    user_id: int
):

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        SELECT *
        FROM starting_records
        WHERE guild_id = ?
        AND discord_user_id = ?
    """, (
        guild_id,
        user_id
    ))

    row = cursor.fetchone()

    db.close()

    return row


def set_starting_record(
    guild_id: int,
    user_id: int,
    wins: int,
    losses: int,
    draws: int,
    streak: str = None
):

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        INSERT OR REPLACE INTO starting_records
        (
            guild_id, discord_user_id,
            wins, losses, draws, streak, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        guild_id,
        user_id,
        wins,
        losses,
        draws,
        streak,
        now_utc()
    ))

    db.commit()
    db.close()


def clear_starting_record(
    guild_id: int,
    user_id: int
):

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        DELETE FROM starting_records
        WHERE guild_id = ?
        AND discord_user_id = ?
    """, (
        guild_id,
        user_id
    ))

    db.commit()
    db.close()


def delete_fighter_fights(
    guild_id: int,
    user_id,
    name: str = None
):

    ids = [
        fight["id"] for fight in get_fighter_fights(guild_id, user_id, name)
    ]

    if not ids:
        return 0

    db = get_db()
    cursor = db.cursor()

    cursor.executemany(
        "DELETE FROM fights WHERE guild_id = ? AND id = ?",
        [(guild_id, fight_id) for fight_id in ids]
    )

    db.commit()
    db.close()

    return len(ids)


def get_user_rankings(
    guild_id: int,
    user_id: int
):

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        SELECT weight, rank
        FROM rankings
        WHERE guild_id = ?
        AND discord_user_id = ?
    """, (
        guild_id,
        user_id
    ))

    rows = cursor.fetchall()

    db.close()

    return rows


# ------------------------------------------------------------
# Stats
# ------------------------------------------------------------

def fight_is_win_for(
    fight,
    user_id,
    name: str = None
):

    if user_id:
        return fight["winner_id"] == user_id

    return (
        fight["winner_id"] is None
        and normalise_name(fight["winner_name"]) == normalise_name(name or "")
    )


def get_fighter_stats(
    guild_id: int,
    user_id: int,
    name: str = None
):

    fights = get_fighter_fights(
        guild_id,
        user_id,
        name
    )

    start = get_starting_record(
        guild_id,
        user_id
    ) if user_id else None

    wins = losses = draws = no_contests = 0
    wins_by = {}
    losses_by = {}
    title_wins = 0
    results = []  # "W", "L", "D" in date order (no contests skipped)

    for fight in fights:

        method = fight["method"]

        if method == "NC":
            no_contests += 1
            continue

        if method == "DRAW":
            draws += 1
            results.append("D")
            continue

        if fight_is_win_for(fight, user_id, name):
            wins += 1
            wins_by[method] = wins_by.get(method, 0) + 1
            results.append("W")

            if fight["title_fight"]:
                title_wins += 1

        else:
            losses += 1
            losses_by[method] = losses_by.get(method, 0) + 1
            results.append("L")

    start_w = start["wins"] if start else 0
    start_l = start["losses"] if start else 0
    start_d = start["draws"] if start else 0

    # An earlier streak (set with /setrecord) carries on into the
    # logged fights, e.g. earlier W3 + two logged wins = W5.
    if start and start["streak"]:
        results = [start["streak"][0]] * int(start["streak"][1:]) + results

    streak = ""

    if results:
        last = results[-1]
        count = 0

        for result in reversed(results):
            if result != last:
                break
            count += 1

        streak = f"{last}{count}"

    finishes = wins_by.get("KO/TKO", 0) + wins_by.get("SUB", 0)

    return {
        "fights": fights,
        "wins": wins + start_w,
        "losses": losses + start_l,
        "draws": draws + start_d,
        "no_contests": no_contests,
        "logged_wins": wins,
        "wins_by": wins_by,
        "losses_by": losses_by,
        "title_wins": title_wins,
        "streak": streak,
        "finish_rate": (
            round(100 * finishes / wins) if wins else None
        ),
        "starting_record": (
            format_record(start_w, start_l, start_d)
            if start and min(start_w, start_l, start_d) >= 0 else None
        ),
        # Set with /editrecord to a number lower than the logged fights.
        "adjusted": bool(start) and min(start_w, start_l, start_d) < 0,
        "logged": (wins, losses, draws),
    }


def stats_record(
    stats: dict
):
    return format_record(
        stats["wins"],
        stats["losses"],
        stats["draws"]
    )


# ------------------------------------------------------------
# Embeds
# ------------------------------------------------------------

DEPARTED_NAME_CACHE = {}


async def departed_name(
    user_id: int
):
    """
    The Discord username of someone who isn't in the server
    (any more). Discord still knows who they are by their ID.
    """

    if user_id in DEPARTED_NAME_CACHE:
        return DEPARTED_NAME_CACHE[user_id]

    try:
        user = await bot.fetch_user(user_id)
        name = user.global_name or user.name
    except Exception:
        name = "Unknown Fighter"

    DEPARTED_NAME_CACHE[user_id] = name

    return name


async def fill_departed_names(
    fights: list
):
    """Swaps "Unknown Fighter" for the real username where we have an ID."""

    for fight in fights:
        for side in ("winner", "loser"):
            if fight.get(f"{side}_id") and fight[f"{side}_name"] == "Unknown Fighter":
                fight[f"{side}_name"] = await departed_name(fight[f"{side}_id"])

    return fights


async def opponent_text(
    guild: discord.Guild,
    user_id,
    saved_name: str
):

    if user_id:
        member = await get_server_member(
            guild,
            user_id
        )

        if member:
            return profile_link(member.display_name, member.id)

        if saved_name == "Unknown Fighter":
            saved_name = await departed_name(user_id)

    return saved_name


def fight_line_tag(
    fight,
    user_id: int,
    name: str = None
):

    if fight["method"] == "NC":
        return "⚪ **NC**"

    if fight["method"] == "DRAW":
        return "🟡 **D**"

    if fight_is_win_for(fight, user_id, name):
        return "🟢 **W**"

    return "🔴 **L**"


def fight_method_text(
    fight
):

    method = fight["method"]

    if method in ("DRAW", "NC"):
        return METHOD_LABELS[method]

    text = METHOD_LABELS.get(method, method)

    if fight["round"] and method != "DEC":
        text += f" R{fight['round']}"

    return text


async def create_profile_embed(
    guild: discord.Guild,
    member
):

    stats = get_fighter_stats(
        guild.id,
        member.id,
        member.display_name
    )

    ranks = get_user_rankings(
        guild.id,
        member.id
    ) if member.id else []

    # Champion lines + ranking list
    champion_of = [
        row["weight"] for row in ranks
        if row["rank"] == 0 and row["weight"] != P4P_WEIGHT
    ]

    rank_lines = []

    # Divisions first, P4P last.
    weight_order = [w for w in WEIGHTS.keys() if w != P4P_WEIGHT] + [P4P_WEIGHT]

    for weight in weight_order:
        for row in ranks:
            if row["weight"] != weight:
                continue

            if row["rank"] == 0:
                rank_lines.append(f"👑 {weight}")
            elif weight == P4P_WEIGHT:
                rank_lines.append(f"#{row['rank']} P4P")
            else:
                rank_lines.append(f"#{row['rank']} {weight}")

    embed = discord.Embed(
        title=member.display_name,
        url=(
            f"https://discord.com/users/{member.id}"
            if member.id else None
        ),
        color=PFO_GOLD,
        timestamp=discord.utils.utcnow()
    )

    description = []

    for weight in champion_of:
        description.append(f"👑 **{weight} Champion**")

    if stats["title_wins"]:
        description.append(
            f"🏆 {stats['title_wins']} title fight "
            f"win{'s' if stats['title_wins'] != 1 else ''}"
        )

    # Title reigns (from title fights)
    try:
        description += title_summary_for_member(guild.id, member.id, member.display_name)
    except Exception as error:
        print(f"Profile title lookup failed: {error}")

    # Suspension / warnings
    if member.id:
        try:
            suspension = get_active_suspension(guild.id, member.id)

            if suspension:
                description.insert(
                    0,
                    f"⛔ **Suspended** until <t:{suspension['ends_at']}:f> "
                    f"(<t:{suspension['ends_at']}:R>)"
                )
            else:
                warns = len(active_warnings(guild.id, member.id))

                if warns:
                    description.append(f"⚠️ {warns}/{MAX_WARNINGS} warnings")
        except Exception as error:
            print(f"Profile warning lookup failed: {error}")

    if description:
        embed.description = "\n".join(description)

    apply_branding(
        embed,
        banner_file=PROFILE_BANNER_FILE,
        author_text="PFO • FIGHTER PROFILE",
        show_thumbnail=False
    )

    # Fighter's own avatar in the corner.
    avatar = getattr(member, "display_avatar", None)

    if avatar:
        embed.set_thumbnail(
            url=avatar.url
        )

    streak = stats["streak"]

    if streak.startswith("W") and int(streak[1:]) >= 3:
        streak_text = f"🔥 {streak}"
    elif streak:
        streak_text = streak
    else:
        streak_text = "—"

    embed.add_field(
        name="Record",
        value=f"**{stats_record(stats)}**",
        inline=True
    )

    embed.add_field(
        name="Streak",
        value=streak_text,
        inline=True
    )

    embed.add_field(
        name="Finish Rate",
        value=(
            f"{stats['finish_rate']}%"
            if stats["finish_rate"] is not None else "—"
        ),
        inline=True
    )

    embed.add_field(
        name="Rankings",
        value="\n".join(rank_lines) if rank_lines else "Unranked",
        inline=True
    )

    def method_lines(counts):
        lines = [
            f"{METHOD_LABELS[method]}  {counts[method]}"
            for method in ("KO/TKO", "SUB", "DEC", "DQ")
            if counts.get(method)
        ]
        return "\n".join(lines) if lines else "—"

    embed.add_field(
        name="Wins By",
        value=method_lines(stats["wins_by"]),
        inline=True
    )

    embed.add_field(
        name="Losses By",
        value=method_lines(stats["losses_by"]),
        inline=True
    )

    # Last 5 fights, newest first
    recent = list(reversed(stats["fights"]))[:5]

    if recent:
        lines = []

        for fight in recent:
            if fight_is_win_for(fight, member.id, member.display_name):
                opp = await opponent_text(guild, fight["loser_id"], fight["loser_name"])
            else:
                opp = await opponent_text(guild, fight["winner_id"], fight["winner_name"])

            extra = ""

            if fight["event"]:
                extra += f" · *{fight['event']}*"
            elif fight["fight_date"]:
                y, m, d = fight["fight_date"].split("-")
                extra += f" · *{d}/{m}/{y}*"

            if fight["title_fight"]:
                extra += " 👑"

            lines.append(
                f"{fight_line_tag(fight, member.id, member.display_name)} vs {opp} — "
                f"{fight_method_text(fight)}{extra}"
            )

        embed.add_field(
            name="🥊 Last 5 Fights",
            value="\n".join(lines)[:1024],
            inline=False
        )

    footer = f"{len(stats['fights'])} PFO fight{'s' if len(stats['fights']) != 1 else ''} logged"

    if stats["starting_record"]:
        footer += f" • includes earlier record {stats['starting_record']}"

    if stats["adjusted"]:
        footer += " • record set by staff"

    if getattr(member, "left_server", False):
        footer += " • no longer in the server"

    embed.set_footer(
        text=footer,
        icon_url=asset_url(LOGO_FILE)
    )

    return embed


def create_result_embed(
    fight_id: int,
    fight: dict,
    before: dict,
    after: dict,
    logged_by_name: str
):
    """
    before / after: {"winner": "6-0-0", "loser": "4-2-0"}
    """

    method = fight["method"]
    winner = profile_link(fight["winner_name"], fight.get("winner_id"))
    loser = profile_link(fight["loser_name"], fight.get("loser_id"))

    if fight.get("event"):
        author = f"PFO • {fight['event'].upper()} • RESULT"
    else:
        author = "PFO • FIGHT RESULT"

    if fight.get("title_fight") and fight.get("division"):
        title = f"👑 {fight['division']} Title Fight"
    elif fight.get("title_fight"):
        title = "👑 Title Fight"
    elif fight.get("division"):
        title = f"{fight['division']} Bout"
    else:
        title = None

    if method == "DRAW":
        headline = f"🤝 **{winner}** and **{loser}** fought to a draw"
        tags = ("D", "D")
    elif method == "NC":
        headline = f"⚪ **{winner}** vs **{loser}** ended in a no contest"
        tags = ("NC", "NC")
    else:
        headline = f"🏆 **{winner}** def. **{loser}**"
        tags = ("W", "L")

    lines = [
        headline,
        "",
        f"**Method:** {METHOD_LABELS[method]}",
    ]

    if fight.get("round") and method not in ("DRAW", "NC"):
        lines.append(f"**Round:** {fight['round']}")

    lines += [
        "",
        "**Updated records**",
        f"`{tags[0]:^3}` {fight['winner_name']}  "
        f"{before['winner']} → **{after['winner']}**",
        f"`{tags[1]:^3}` {fight['loser_name']}  "
        f"{before['loser']} → **{after['loser']}**",
    ]

    embed = discord.Embed(
        title=title,
        description="\n".join(lines),
        color=PFO_GOLD,
        timestamp=discord.utils.utcnow()
    )

    apply_branding(
        embed,
        banner_file=(
            TITLE_RESULT_BANNER_FILE
            if fight.get("title_fight") else RESULT_BANNER_FILE
        ),
        author_text=author
    )

    embed.set_footer(
        text=f"Fight #{fight_id} • Logged by {logged_by_name}",
        icon_url=asset_url(LOGO_FILE)
    )

    return embed


# ------------------------------------------------------------
# Fighters who have left the server
#
# Discord only lets you pick current members in a command, so
# the "..._left" options take a user ID (best: records follow
# them if they come back) or just a name.
#
# Getting a user ID: Discord settings → Advanced → turn on
# Developer Mode, then right-click their name on an old message
# → Copy User ID.
# ------------------------------------------------------------

class FighterInputError(Exception):
    pass


async def resolve_left_fighter(
    guild: discord.Guild,
    text: str
):
    """Returns (user_id or None, name, still_in_server)."""

    text = text.strip()

    mention = re.fullmatch(r"<@!?(\d+)>", text)
    digits = mention.group(1) if mention else (
        text if text.isdigit() and 15 <= len(text) <= 20 else None
    )

    if digits:
        user_id = int(digits)
        member = guild.get_member(user_id)

        if member:
            return member.id, member.display_name, True

        return user_id, await departed_name(user_id), False

    user_id, name, status = resolve_fighter(
        guild,
        build_member_lookup(guild),
        text
    )

    if status == "member":
        return user_id, name, True

    if status == "ambiguous":
        raise FighterInputError(
            f"`{text}` matches more than one member. Use their user ID instead."
        )

    name = " ".join(text.lstrip("@").split())

    if not name:
        raise FighterInputError("Please enter a name or user ID.")

    # Reuse the spelling already on record for this person.
    for fight in get_fighter_fights(guild.id, None, name):
        for side in ("winner", "loser"):
            if fight[f"{side}_id"] is None and normalise_name(fight[f"{side}_name"]) == normalise_name(name):
                return None, fight[f"{side}_name"], False

    return None, name, False


async def pick_fighter(
    guild: discord.Guild,
    member,
    left_text: str,
    role: str
):
    """
    Uses whichever of the two options was filled in.
    Returns (user_id or None, name, still_in_server).
    """

    if member and left_text:
        raise FighterInputError(
            f"Fill in only one of **{role}** or **{role}_left**."
        )

    if member:
        if member.bot:
            raise FighterInputError("Bots can't have fight records.")

        return member.id, member.display_name, True

    if left_text:
        return await resolve_left_fighter(guild, left_text)

    raise FighterInputError(
        f"Pick the **{role}**, or for someone who has left use **{role}_left** "
        f"(their user ID or name)."
    )


# ------------------------------------------------------------
# Nicknames show each fighter's record, e.g. "Razor Reyes (9-1-0)"
#
# Updated automatically by /profile, /result, /removefight,
# /editfight, /editrecord, /setrecord and /resetrecord.
#
# The bot needs the "Manage Nicknames" permission, and its role
# must be ABOVE the fighters' roles. Discord never lets a bot change
# the server owner's nickname.
#
# Turn it off with NICKNAME_RECORDS=off in Railway's Variables.
# ------------------------------------------------------------

NICKNAME_RECORDS = os.getenv(
    "NICKNAME_RECORDS",
    "on"
).strip().lower() not in ("off", "0", "false", "no")

RECORD_IN_NAME = re.compile(
    r"\s*\(\s*(\d{1,3})\s*-\s*(\d{1,3})(?:\s*-\s*(\d{1,3}))?\s*\)\s*$"
)

NICKNAME_LIMIT = 32  # Discord's maximum


def record_from_name(
    name: str
):
    """(wins, losses, draws) from a name like "Razor (6-1-0)", or None."""

    found = RECORD_IN_NAME.search(name or "")

    if not found:
        return None

    return (
        int(found.group(1)),
        int(found.group(2)),
        int(found.group(3) or 0),
    )


def name_without_record(
    name: str
):

    return RECORD_IN_NAME.sub("", name or "").strip() or (name or "")


def nickname_with_record(
    current: str,
    record: str,
    full_name: str = None
):

    base = full_name or name_without_record(current)
    suffix = f" ({record})"
    room = NICKNAME_LIMIT - len(suffix)

    if len(base) > room:
        base = base[:room].rstrip(" /-|,")

    return f"{base}{suffix}"


def has_record_data(
    guild_id: int,
    user_id: int
):

    return bool(
        get_fighter_fights(guild_id, user_id)
        or get_starting_record(guild_id, user_id)
    )


def seed_record_from_nickname(
    guild_id: int,
    member
):
    """
    If the bot knows nothing about a fighter yet, start them from the
    record already in their nickname, so it isn't reset to 0-0-0.
    """

    if not member or not getattr(member, "id", None) or getattr(member, "bot", False):
        return False

    if has_record_data(guild_id, member.id):
        return False

    found = record_from_name(member.display_name)

    if not found:
        return False

    set_starting_record(guild_id, member.id, *found)

    return True


def planned_nickname(
    guild_id: int,
    member,
    save: bool = False
):
    """
    The nickname this fighter should have, or None if it's already
    right. save=False only works it out (used by the /syncnicknames
    preview) without remembering anything.
    """

    record = stats_record(get_fighter_stats(guild_id, member.id))

    # Remember the full name, so a long name cut short to fit
    # Discord's 32-character limit doesn't get shorter every time.
    current_base = name_without_record(member.display_name)
    key = f"nickname_base:{guild_id}:{member.id}"
    full_name = get_meta(key)

    was_cut_short = (
        full_name
        and len(member.display_name) >= NICKNAME_LIMIT - 1
        and full_name.startswith(current_base.rstrip(" /-|,"))
    )

    if not was_cut_short:
        full_name = current_base
        if save:
            set_meta(key, full_name)

    new_nick = nickname_with_record(member.display_name, record, full_name)

    if new_nick == member.display_name:
        return None

    return new_nick


async def sync_nickname(
    guild: discord.Guild,
    user_id,
    force: bool = True
):
    """
    Puts the fighter's current record in their nickname.
    Returns (problem or None, new nickname or None).

    force=False (used by /profile) skips fighters the bot has no
    record for, so viewing a profile never adds "(0-0-0)".
    """

    if not NICKNAME_RECORDS or not user_id:
        return None, None

    member = await get_server_member(guild, user_id)

    if member is None or member.bot:
        return None, None

    if not force and not has_record_data(guild.id, user_id):
        return None, None

    new_nick = planned_nickname(guild.id, member, save=True)

    if not new_nick:
        return None, None

    if member.id == guild.owner_id:
        return (
            f"**{member.display_name}** is the server owner, and Discord doesn't "
            f"let bots change the owner's nickname (should be **{new_nick}**)",
            None
        )

    try:
        await member.edit(nick=new_nick, reason="Fight record updated")
    except discord.Forbidden:
        return (
            f"**{member.display_name}**: the bot needs the **Manage Nicknames** "
            "permission, and its role must be above this fighter's roles",
            None
        )
    except Exception as error:
        print(f"Nickname update failed for {user_id}: {error}")
        return f"**{member.display_name}**: Discord error, try again later", None

    return None, new_nick


async def sync_nicknames(
    guild: discord.Guild,
    user_ids
):

    problems = []
    seen = set()

    for user_id in user_ids:

        if not user_id or user_id in seen:
            continue

        if seen:
            await asyncio.sleep(0.5)

        seen.add(user_id)

        problem, _ = await sync_nickname(guild, user_id)

        if problem:
            problems.append(problem)

    return problems


def nickname_problem_text(
    problems: list
):

    if not problems:
        return ""

    return (
        "\n\n⚠️ **Nickname not updated:**\n"
        + "\n".join(f"• {problem}" for problem in problems[:10])
    )


# ------------------------------------------------------------
# /RESULT
# ------------------------------------------------------------

@bot.tree.command(
    name="result",
    description="Log a fight result."
)
@app_commands.describe(
    method="How the fight ended",
    winner="The winner (for a draw or no contest, either fighter)",
    loser="The loser (for a draw or no contest, the other fighter)",
    winner_left="Winner who has LEFT the server: their user ID (best) or name",
    loser_left="Loser who has LEFT the server: their user ID (best) or name",
    round="Round it ended in (leave empty for decisions)",
    division="Weight class",
    title_fight="Was a belt on the line?",
    event="Event name, e.g. Fight Night 15",
    date="Only for older fights: the date it happened, e.g. 14/09/2026 (default: today)"
)
@app_commands.choices(
    method=METHOD_CHOICES,
    division=DIVISION_CHOICES
)
async def result(
    interaction: discord.Interaction,
    method: app_commands.Choice[str],
    winner: discord.Member = None,
    loser: discord.Member = None,
    winner_left: str = None,
    loser_left: str = None,
    round: app_commands.Range[int, 1, 5] = None,
    division: app_commands.Choice[str] = None,
    title_fight: bool = False,
    event: str = None,
    date: str = None
):

    if not is_staff(interaction):

        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can log results.",
            ephemeral=True
        )

        return

    fight_date = today_iso()

    if date:
        fight_date, ok = parse_date(date)

        if not ok or not fight_date:
            await interaction.response.send_message(
                "❌ Write the date like 14/09/2026.",
                ephemeral=True
            )
            return

        if fight_date > today_iso():
            await interaction.response.send_message(
                "❌ That date is in the future.",
                ephemeral=True
            )
            return

    await interaction.response.defer()

    try:
        winner_id, winner_name, _ = await pick_fighter(
            interaction.guild, winner, winner_left, "winner"
        )
        loser_id, loser_name, _ = await pick_fighter(
            interaction.guild, loser, loser_left, "loser"
        )
    except FighterInputError as error:
        await interaction.followup.send(f"❌ {error}", ephemeral=True)
        return

    if (winner_id and winner_id == loser_id) or (
        not winner_id and not loser_id
        and normalise_name(winner_name) == normalise_name(loser_name)
    ):

        await interaction.followup.send(
            "❌ The winner and loser must be two different fighters.",
            ephemeral=True
        )

        return

    # Fighters in the server: show names without the old record,
    # and start anyone new to the bot from the record in their nickname.
    if winner:
        seed_record_from_nickname(interaction.guild.id, winner)
        winner_name = name_without_record(winner_name)

    if loser:
        seed_record_from_nickname(interaction.guild.id, loser)
        loser_name = name_without_record(loser_name)

    try:

        guild_id = interaction.guild.id

        before = {
            "winner": stats_record(get_fighter_stats(guild_id, winner_id, winner_name)),
            "loser": stats_record(get_fighter_stats(guild_id, loser_id, loser_name)),
        }

        fight = {
            "date": fight_date,
            "event": event.strip() if event and event.strip() else None,
            "winner_id": winner_id,
            "winner_name": winner_name,
            "loser_id": loser_id,
            "loser_name": loser_name,
            "method": method.value,
            "round": round,
            "division": division.value if division else None,
            "title_fight": title_fight,
        }

        fight_id = add_fight(
            guild_id,
            fight,
            "result",
            interaction.user.id
        )

        after = {
            "winner": stats_record(get_fighter_stats(guild_id, winner_id, winner_name)),
            "loser": stats_record(get_fighter_stats(guild_id, loser_id, loser_name)),
        }

        await interaction.followup.send(
            embed=create_result_embed(
                fight_id,
                fight,
                before,
                after,
                interaction.user.display_name
            )
        )

    except Exception as error:

        print(
            f"Result error: {error}"
        )

        await interaction.followup.send(
            "❌ Something went wrong while logging the result.",
            ephemeral=True
        )

        return

    problems = await sync_nicknames(
        interaction.guild,
        [winner_id, loser_id]
    )

    if problems:
        await interaction.followup.send(
            nickname_problem_text(problems).strip(),
            ephemeral=True
        )

    # Extras after the result is safely saved; a problem here never
    # undoes or blocks the result itself.
    suggestion_problem = None

    try:
        for poll_id in resolve_votes_for_fight(interaction.guild.id, get_fight(interaction.guild.id, fight_id)):
            await refresh_vote_message(poll_id)

        # Fight pay, vote rewards and bet payouts.
        await coins_after_result(interaction.guild, get_fight(interaction.guild.id, fight_id))

        suggestion_problem = await post_ranking_suggestions(
            interaction.guild,
            fight_id,
            fight
        )
    except Exception as error:
        print(f"After-result extras failed: {error}")

    if suggestion_problem:
        await interaction.followup.send(
            suggestion_problem,
            ephemeral=True
        )


# ------------------------------------------------------------
# /PROFILE
# ------------------------------------------------------------

@bot.tree.command(
    name="profile",
    description="Show a fighter's PFO record."
)
@app_commands.describe(
    fighter="Whose profile? Leave empty for your own.",
    fighter_left="Someone who has LEFT the server: their user ID or name"
)
async def profile(
    interaction: discord.Interaction,
    fighter: discord.Member = None,
    fighter_left: str = None
):

    if not is_fighter_or_staff(interaction):

        await interaction.response.send_message(
            "❌ You need the Fighter role to use /profile.",
            ephemeral=True
        )

        return

    if fighter_left and not fighter:

        await interaction.response.defer()

        try:
            user_id, name, in_server = await resolve_left_fighter(
                interaction.guild,
                fighter_left
            )
        except FighterInputError as error:
            await interaction.followup.send(f"❌ {error}", ephemeral=True)
            return

        if in_server:
            member = interaction.guild.get_member(user_id)
        else:
            avatar = None

            if user_id:
                try:
                    avatar = (await bot.fetch_user(user_id)).display_avatar
                except Exception:
                    avatar = None

            member = types.SimpleNamespace(
                id=user_id,
                display_name=name,
                display_avatar=avatar,
                bot=False,
                left_server=True
            )

            if not get_fighter_fights(interaction.guild.id, user_id, name) and not (
                user_id and get_starting_record(interaction.guild.id, user_id)
            ):
                await interaction.followup.send(
                    f"❌ No fights found for **{name}**.",
                    ephemeral=True
                )
                return

        try:
            await interaction.followup.send(
                embed=await create_profile_embed(interaction.guild, member)
            )
        except Exception as error:
            print(f"Profile error: {error}")
            await interaction.followup.send(
                "❌ Something went wrong while loading that profile.",
                ephemeral=True
            )

        return

    member = fighter or interaction.user

    if member.bot:

        await interaction.response.send_message(
            "❌ Bots don't have fight records.",
            ephemeral=True
        )

        return

    await interaction.response.defer()

    # Keep their nickname in step with their record.
    seed_record_from_nickname(interaction.guild.id, member)

    problem, new_nick = await sync_nickname(
        interaction.guild,
        member.id,
        force=False
    )

    if new_nick:
        member = types.SimpleNamespace(
            id=member.id,
            display_name=new_nick,
            display_avatar=getattr(member, "display_avatar", None),
            bot=False
        )

    try:

        await interaction.followup.send(
            embed=await create_profile_embed(
                interaction.guild,
                member
            )
        )

        if problem and is_staff(interaction):
            await interaction.followup.send(
                nickname_problem_text([problem]).strip(),
                ephemeral=True
            )

    except Exception as error:

        print(
            f"Profile error: {error}"
        )

        await interaction.followup.send(
            "❌ Something went wrong while loading that profile.",
            ephemeral=True
        )


# ------------------------------------------------------------
# /SETRECORD
# ------------------------------------------------------------

@bot.tree.command(
    name="setrecord",
    description="Set a fighter's earlier record (fights that were never logged)."
)
@app_commands.describe(
    wins="Earlier wins",
    losses="Earlier losses",
    draws="Earlier draws",
    fighter="Which fighter?",
    fighter_left="Someone who has LEFT the server: their user ID",
    streak="Their streak at the end of the earlier record, e.g. W3 or L1 (optional)"
)
async def setrecord(
    interaction: discord.Interaction,
    wins: app_commands.Range[int, 0, 999],
    losses: app_commands.Range[int, 0, 999],
    draws: app_commands.Range[int, 0, 999] = 0,
    fighter: discord.Member = None,
    fighter_left: str = None,
    streak: str = None
):

    if not is_staff(interaction):

        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can set records.",
            ephemeral=True
        )

        return

    await interaction.response.defer(
        ephemeral=True
    )

    try:
        user_id, name, _ = await pick_fighter(
            interaction.guild, fighter, fighter_left, "fighter"
        )
    except FighterInputError as error:
        await interaction.followup.send(f"❌ {error}", ephemeral=True)
        return

    if not user_id:

        await interaction.followup.send(
            "❌ An earlier record needs their **user ID**, not just a name. "
            "Turn on Developer Mode in Discord, right-click their name on an "
            "old message and choose **Copy User ID**.",
            ephemeral=True
        )

        return

    if streak:
        streak = streak.strip().upper().replace(" ", "")
        valid = re.fullmatch(r"([WLD])(\d{1,3})", streak)
        limits = {"W": wins, "L": losses, "D": draws}

        if not valid or int(valid.group(2)) < 1:
            await interaction.followup.send(
                "❌ Write the streak like **W3** (3 wins in a row), **L1** or **D1**.",
                ephemeral=True
            )
            return

        if int(valid.group(2)) > limits[valid.group(1)]:
            await interaction.followup.send(
                f"❌ A streak of {streak} is longer than the earlier record allows.",
                ephemeral=True
            )
            return

    set_starting_record(
        interaction.guild.id,
        user_id,
        wins,
        losses,
        draws,
        streak or None
    )

    stats = get_fighter_stats(
        interaction.guild.id,
        user_id
    )

    problems = await sync_nicknames(interaction.guild, [user_id])

    await interaction.followup.send(
        (
            f"✅ Earlier record for **{name}** set to "
            f"**{format_record(wins, losses, draws)}**"
            f"{f' (ending on a {streak} streak)' if streak else ''}.\n"
            f"Their full record is now **{stats_record(stats)}** "
            f"including logged fights."
            f"{nickname_problem_text(problems)}"
        ),
        ephemeral=True
    )


# ------------------------------------------------------------
# /EDITFIGHT
# ------------------------------------------------------------

def update_fight(
    guild_id: int,
    fight_id: int,
    changes: dict
):

    if not changes:
        return

    columns = ", ".join(f"{column} = ?" for column in changes)

    db = get_db()
    cursor = db.cursor()

    cursor.execute(
        f"UPDATE fights SET {columns} WHERE guild_id = ? AND id = ?",
        (*changes.values(), guild_id, fight_id)
    )

    db.commit()
    db.close()


def fight_summary(
    fight
):

    if fight["fight_date"]:
        y, m, d = fight["fight_date"].split("-")
        when = f"{d}/{m}/{y}"
    else:
        when = "no date"

    if fight["method"] in ("DRAW", "NC"):
        who = f"{fight['winner_name']} vs {fight['loser_name']}"
    else:
        who = f"{fight['winner_name']} def. {fight['loser_name']}"

    extras = [fight_method_text(fight)]

    if fight["division"]:
        extras.append(fight["division"])

    if fight["event"]:
        extras.append(fight["event"])

    if fight["title_fight"]:
        extras.append("👑 title fight")

    return f"**#{fight['id']}** · {when} · {who} ({', '.join(extras)})"


@bot.tree.command(
    name="editfight",
    description="Fix a fight that's already logged (date, method, round and more)."
)
@app_commands.describe(
    fight_id="The fight number (shown on result cards and in /fights)",
    date="When it happened, e.g. 14/09/2026",
    method="How it ended",
    round="Round it ended in (0 = clear it)",
    division="Weight class",
    title_fight="Was a belt on the line?",
    event="Event name (type 'none' to clear it)",
    swap_winner="Swap the winner and loser"
)
@app_commands.choices(
    method=METHOD_CHOICES,
    division=DIVISION_CHOICES
)
async def editfight(
    interaction: discord.Interaction,
    fight_id: int,
    date: str = None,
    method: app_commands.Choice[str] = None,
    round: app_commands.Range[int, 0, 5] = None,
    division: app_commands.Choice[str] = None,
    title_fight: bool = None,
    event: str = None,
    swap_winner: bool = False
):

    if not is_staff(interaction):

        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can edit fights.",
            ephemeral=True
        )

        return

    fight = get_fight(
        interaction.guild.id,
        fight_id
    )

    if not fight:

        await interaction.response.send_message(
            f"❌ There is no fight #{fight_id}. Use `/fights` to find the right number.",
            ephemeral=True
        )

        return

    changes = {}

    if date:
        new_date, ok = parse_date(date)

        if not ok or not new_date:
            await interaction.response.send_message(
                "❌ Write the date like 14/09/2026.",
                ephemeral=True
            )
            return

        if new_date > today_iso():
            await interaction.response.send_message(
                "❌ That date is in the future.",
                ephemeral=True
            )
            return

        changes["fight_date"] = new_date

    if method:
        changes["method"] = method.value

    if round is not None:
        changes["round"] = round or None

    if division:
        changes["division"] = division.value

    if title_fight is not None:
        changes["title_fight"] = 1 if title_fight else 0

    if event:
        changes["event"] = None if event.strip().lower() == "none" else event.strip()

    if swap_winner:
        changes.update({
            "winner_id": fight["loser_id"],
            "winner_name": fight["loser_name"],
            "loser_id": fight["winner_id"],
            "loser_name": fight["winner_name"],
        })

    if not changes:

        await interaction.response.send_message(
            "❌ Nothing to change. Fill in at least one option, e.g. **date**.",
            ephemeral=True
        )

        return

    update_fight(
        interaction.guild.id,
        fight_id,
        changes
    )

    updated = get_fight(
        interaction.guild.id,
        fight_id
    )

    polls_changed = set(unresolve_votes_for_fight(fight_id))
    await coins_after_removal(interaction.guild, fight_id)
    polls_changed.update(resolve_votes_for_fight(interaction.guild.id, updated))
    await coins_after_result(interaction.guild, updated)

    await interaction.response.send_message(
        (
            "✏️ **Fight updated.** Records, streaks and profiles now use the new details.\n\n"
            f"Before: {fight_summary(fight)}\n"
            f"After: {fight_summary(updated)}"
        ),
        ephemeral=True
    )

    for poll_id in polls_changed:
        await refresh_vote_message(poll_id)

    problems = await sync_nicknames(
        interaction.guild,
        [updated["winner_id"], updated["loser_id"]]
    )

    if problems:
        await interaction.followup.send(
            nickname_problem_text(problems).strip(),
            ephemeral=True
        )


# ------------------------------------------------------------
# /FIGHTS
# ------------------------------------------------------------

FIGHTS_PER_PAGE = 15


@bot.tree.command(
    name="fights",
    description="List a fighter's logged fights with their fight numbers."
)
@app_commands.describe(
    fighter="Which fighter?",
    fighter_left="Someone who has LEFT the server: their user ID or name",
    page="Page number for long histories"
)
async def fights(
    interaction: discord.Interaction,
    fighter: discord.Member = None,
    fighter_left: str = None,
    page: app_commands.Range[int, 1, 999] = 1
):

    if not is_staff(interaction):

        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can list fights.",
            ephemeral=True
        )

        return

    await interaction.response.defer(
        ephemeral=True
    )

    try:
        user_id, name, _ = await pick_fighter(
            interaction.guild, fighter, fighter_left, "fighter"
        )
    except FighterInputError as error:
        await interaction.followup.send(f"❌ {error}", ephemeral=True)
        return

    history = list(reversed(get_fighter_fights(
        interaction.guild.id,
        user_id,
        name
    )))

    if not history:
        await interaction.followup.send(
            f"❌ No fights logged for **{name}** yet.",
            ephemeral=True
        )
        return

    pages = (len(history) + FIGHTS_PER_PAGE - 1) // FIGHTS_PER_PAGE
    page = min(page, pages)
    shown = history[(page - 1) * FIGHTS_PER_PAGE: page * FIGHTS_PER_PAGE]

    lines = []

    for fight in shown:

        if fight["fight_date"]:
            y, m, d = fight["fight_date"].split("-")
            when = f"{d}/{m}/{y}"
        else:
            when = "no date"

        if fight_is_win_for(fight, user_id, name):
            opponent = fight["loser_name"]
        else:
            opponent = fight["winner_name"]

        lines.append(
            f"`#{fight['id']}` {when} · {fight_line_tag(fight, user_id, name)} "
            f"vs {opponent} — {fight_method_text(fight)}"
            f"{' 👑' if fight['title_fight'] else ''}"
        )

    stats = get_fighter_stats(
        interaction.guild.id,
        user_id,
        name
    )

    embed = discord.Embed(
        title=f"{name}: {stats_record(stats)}",
        description=(
            "\n".join(lines)
            + "\n\nNewest first. Fix a fight with `/editfight fight_id:`"
        )[:4000],
        color=PFO_GOLD
    )

    apply_branding(
        embed,
        author_text="PFO • FIGHT HISTORY"
    )

    embed.set_footer(
        text=(
            f"Page {page} of {pages} • {len(history)} fights"
            + (f" • streak {stats['streak']}" if stats["streak"] else "")
        ),
        icon_url=asset_url(LOGO_FILE)
    )

    await interaction.followup.send(
        embed=embed,
        ephemeral=True
    )


# ------------------------------------------------------------
# /EDITRECORD
#
# Sets a fighter's record to an exact number. Logged fights stay
# as they are (so profiles still show them); the bot works out the
# difference and stores it as an adjustment. New fights are then
# added on top as normal.
# ------------------------------------------------------------

def parse_record_text(
    text: str
):

    found = re.fullmatch(
        r"\s*(\d{1,3})\s*-\s*(\d{1,3})(?:\s*-\s*(\d{1,3}))?\s*",
        text or ""
    )

    if not found:
        return None

    return (
        int(found.group(1)),
        int(found.group(2)),
        int(found.group(3) or 0),
    )


@bot.tree.command(
    name="editrecord",
    description="Set a fighter's record to an exact number, e.g. 7-2-0."
)
@app_commands.describe(
    record="Their correct full record, e.g. 7-2-0 (wins-losses-draws)",
    fighter="Which fighter?",
    fighter_left="Someone who has LEFT the server: their user ID",
    streak="Their current streak, e.g. W3 or L1 (optional)"
)
async def editrecord(
    interaction: discord.Interaction,
    record: str,
    fighter: discord.Member = None,
    fighter_left: str = None,
    streak: str = None
):

    if not is_staff(interaction):

        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can edit records.",
            ephemeral=True
        )

        return

    target = parse_record_text(record)

    if not target:

        await interaction.response.send_message(
            "❌ Write the record like **7-2-0** (wins-losses-draws) or **7-2**.",
            ephemeral=True
        )

        return

    await interaction.response.defer(
        ephemeral=True
    )

    try:
        user_id, name, _ = await pick_fighter(
            interaction.guild, fighter, fighter_left, "fighter"
        )
    except FighterInputError as error:
        await interaction.followup.send(f"❌ {error}", ephemeral=True)
        return

    if not user_id:

        await interaction.followup.send(
            "❌ Editing a record needs their **user ID**, not just a name. "
            "Turn on Developer Mode in Discord, right-click their name on an "
            "old message and choose **Copy User ID**.",
            ephemeral=True
        )

        return

    if streak:
        streak = streak.strip().upper().replace(" ", "")
        valid = re.fullmatch(r"([WLD])(\d{1,3})", streak)
        totals = {"W": target[0], "L": target[1], "D": target[2]}

        if not valid or int(valid.group(2)) < 1:
            await interaction.followup.send(
                "❌ Write the streak like **W3** (3 wins in a row), **L1** or **D1**.",
                ephemeral=True
            )
            return

        if int(valid.group(2)) > totals[valid.group(1)]:
            await interaction.followup.send(
                f"❌ A streak of {streak} doesn't fit a {format_record(*target)} record.",
                ephemeral=True
            )
            return

    before = get_fighter_stats(
        interaction.guild.id,
        user_id
    )

    logged_w, logged_l, logged_d = before["logged"]

    # A typed streak becomes the streak before their logged fights,
    # so it carries on into them. Without one, keep any old one.
    old_start = get_starting_record(interaction.guild.id, user_id)

    if streak:
        new_streak = streak
    else:
        new_streak = old_start["streak"] if old_start else None

    set_starting_record(
        interaction.guild.id,
        user_id,
        target[0] - logged_w,
        target[1] - logged_l,
        target[2] - logged_d,
        new_streak
    )

    after = get_fighter_stats(
        interaction.guild.id,
        user_id
    )

    note = ""

    if streak and after["streak"] != streak:
        note = (
            f"\nℹ️ The streak shows as **{after['streak']}** because it's worked "
            "out from their logged fights. Use `/fights` and `/editfight` "
            "if a fight there is wrong."
        )

    await interaction.followup.send(
        (
            f"✅ **{name}**'s record changed from **{stats_record(before)}** "
            f"to **{stats_record(after)}**.\n"
            f"Their {len(before['fights'])} logged fight"
            f"{'s' if len(before['fights']) != 1 else ''} stay on their profile, "
            f"and new results add on top as normal."
            f"{note}"
            f"{nickname_problem_text(await sync_nicknames(interaction.guild, [user_id]))}"
        ),
        ephemeral=True
    )


# ------------------------------------------------------------
# /RESETRECORD
# ------------------------------------------------------------

class ResetRecordView(
    discord.ui.View
):

    def __init__(
        self,
        author_id: int,
        guild_id: int,
        user_id,
        name: str,
        delete_fights: bool
    ):

        super().__init__(
            timeout=300
        )

        self.author_id = author_id
        self.guild_id = guild_id
        self.user_id = user_id
        self.name = name
        self.delete_fights = delete_fights

        confirm = discord.ui.Button(
            label="Yes, reset",
            style=discord.ButtonStyle.red,
            emoji="🗑️"
        )

        confirm.callback = self.confirm

        cancel = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.grey
        )

        cancel.callback = self.cancel

        self.add_item(confirm)
        self.add_item(cancel)

    async def interaction_check(
        self,
        interaction: discord.Interaction
    ):

        if interaction.user.id != self.author_id:

            await interaction.response.send_message(
                "❌ Only the person who started this can confirm it.",
                ephemeral=True
            )

            return False

        return True

    async def confirm(
        self,
        interaction: discord.Interaction
    ):

        removed = 0

        # Everyone whose record changes: this fighter, plus their
        # opponents if their fights are being deleted.
        affected = [self.user_id]

        if self.delete_fights:
            for fight in get_fighter_fights(self.guild_id, self.user_id, self.name):
                affected += [fight["winner_id"], fight["loser_id"]]

        if self.user_id:
            clear_starting_record(self.guild_id, self.user_id)

        if self.delete_fights:
            removed = delete_fighter_fights(
                self.guild_id,
                self.user_id,
                self.name
            )

        self.stop()

        if self.delete_fights:
            text = (
                f"🗑️ **{self.name}**'s record has been reset to **0-0-0** "
                f"and {removed} fight{'s were' if removed != 1 else ' was'} deleted."
            )
        else:
            stats = get_fighter_stats(self.guild_id, self.user_id, self.name)
            text = (
                f"🗑️ Removed **{self.name}**'s earlier record and manual changes. "
                f"Their record is now **{stats_record(stats)}** from logged fights only."
            )

        await interaction.response.edit_message(
            content=text,
            embed=None,
            view=None
        )

        problems = await sync_nicknames(interaction.guild or bot.get_guild(self.guild_id), affected)

        if problems:
            await interaction.edit_original_response(
                content=text + nickname_problem_text(problems)
            )

    async def cancel(
        self,
        interaction: discord.Interaction
    ):

        self.stop()

        await interaction.response.edit_message(
            content="Reset cancelled. Nothing was changed.",
            embed=None,
            view=None
        )


@bot.tree.command(
    name="resetrecord",
    description="Wipe a fighter's record (asks you to confirm first)."
)
@app_commands.describe(
    fighter="Which fighter?",
    fighter_left="Someone who has LEFT the server: their user ID or name",
    delete_fights="Also delete all their logged fights (start from 0-0-0)"
)
async def resetrecord(
    interaction: discord.Interaction,
    fighter: discord.Member = None,
    fighter_left: str = None,
    delete_fights: bool = False
):

    if not is_staff(interaction):

        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can reset records.",
            ephemeral=True
        )

        return

    await interaction.response.defer(
        ephemeral=True
    )

    try:
        user_id, name, _ = await pick_fighter(
            interaction.guild, fighter, fighter_left, "fighter"
        )
    except FighterInputError as error:
        await interaction.followup.send(f"❌ {error}", ephemeral=True)
        return

    stats = get_fighter_stats(
        interaction.guild.id,
        user_id,
        name
    )

    has_start = bool(user_id and get_starting_record(interaction.guild.id, user_id))
    fight_count = len(stats["fights"])

    if not has_start and not (delete_fights and fight_count):

        await interaction.followup.send(
            (
                f"❌ **{name}** has no earlier record or manual changes to remove."
                + (
                    f" To also delete their {fight_count} logged fights, "
                    "run it again with **delete_fights: True**."
                    if fight_count else ""
                )
            ),
            ephemeral=True
        )

        return

    if delete_fights:
        warning = (
            f"This resets **{name}** to **0-0-0** and deletes all "
            f"**{fight_count}** of their logged fights.\n\n"
            "⚠️ Those fights are also removed from their **opponents'** records. "
            "To fix a single fight instead, use `/editfight` or `/removefight`."
        )
    else:
        logged = format_record(*stats["logged"])
        warning = (
            f"This removes **{name}**'s earlier record and any manual changes.\n"
            f"Their record goes from **{stats_record(stats)}** to **{logged}** "
            "(logged fights only). Nothing changes for anyone else."
        )

    embed = discord.Embed(
        title="Reset this record?",
        description=warning + "\n\nThis can't be undone.",
        color=PFO_GOLD
    )

    apply_branding(
        embed,
        author_text="PFO • RESET RECORD"
    )

    await interaction.followup.send(
        embed=embed,
        view=ResetRecordView(
            interaction.user.id,
            interaction.guild.id,
            user_id,
            name,
            delete_fights
        ),
        ephemeral=True
    )


# ------------------------------------------------------------
# /REMOVEFIGHT
#
# Removes a fight that was logged by mistake. Pick it by:
#   fight_id               the number on the result card / in /fights
#   fighter                their most recent fight
#   fighter + opponent     the most recent fight between those two
# Asks to confirm first, then updates both records and nicknames.
# ------------------------------------------------------------

def fight_involves(
    fight,
    user_id,
    name: str = None
):

    for side in ("winner", "loser"):

        if user_id and fight[f"{side}_id"] == user_id:
            return True

        if not user_id and fight[f"{side}_id"] is None and normalise_name(
            fight[f"{side}_name"]
        ) == normalise_name(name or ""):
            return True

    return False


def record_after_removing(
    guild_id: int,
    fight,
    side: str
):
    """(before, after) record strings for one side of a fight being removed."""

    user_id = fight[f"{side}_id"]
    name = fight[f"{side}_name"]

    stats = get_fighter_stats(guild_id, user_id, name)
    wins, losses, draws = stats["wins"], stats["losses"], stats["draws"]
    before = format_record(wins, losses, draws)

    if fight["method"] == "DRAW":
        draws -= 1
    elif fight["method"] == "NC":
        pass
    elif side == "winner":
        wins -= 1
    else:
        losses -= 1

    return before, format_record(wins, losses, draws)


class RemoveFightView(
    discord.ui.View
):

    def __init__(
        self,
        author_id: int,
        guild_id: int,
        fight
    ):

        super().__init__(
            timeout=300
        )

        self.author_id = author_id
        self.guild_id = guild_id
        self.fight = fight

        confirm = discord.ui.Button(
            label="Yes, remove it",
            style=discord.ButtonStyle.red,
            emoji="🗑️"
        )
        confirm.callback = self.confirm

        cancel = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.grey
        )
        cancel.callback = self.cancel

        self.add_item(confirm)
        self.add_item(cancel)

    async def interaction_check(
        self,
        interaction: discord.Interaction
    ):

        if interaction.user.id != self.author_id:

            await interaction.response.send_message(
                "❌ Only the person who started this can confirm it.",
                ephemeral=True
            )

            return False

        return True

    async def confirm(
        self,
        interaction: discord.Interaction
    ):

        self.stop()

        fight = self.fight

        if not get_fight(self.guild_id, fight["id"]):

            await interaction.response.edit_message(
                content=f"❌ Fight #{fight['id']} was already removed.",
                embed=None,
                view=None
            )

            return

        changes = [
            (fight[f"{side}_name"], *record_after_removing(self.guild_id, fight, side))
            for side in ("winner", "loser")
        ]

        delete_fight(
            self.guild_id,
            fight["id"]
        )

        for poll_id in unresolve_votes_for_fight(fight["id"]):
            await refresh_vote_message(poll_id)

        await coins_after_removal(interaction.guild, fight["id"])

        text = (
            f"🗑️ **Removed fight #{fight['id']}**: {fight_summary(fight)}\n\n"
            + "\n".join(
                f"• **{name_without_record(name)}**: {before} → **{after}**"
                for name, before, after in changes
            )
        )

        await interaction.response.edit_message(
            content=text,
            embed=None,
            view=None
        )

        guild = interaction.guild or bot.get_guild(self.guild_id)

        problems = await sync_nicknames(
            guild,
            [fight["winner_id"], fight["loser_id"]]
        )

        if problems:
            await interaction.edit_original_response(
                content=text + nickname_problem_text(problems)
            )

    async def cancel(
        self,
        interaction: discord.Interaction
    ):

        self.stop()

        await interaction.response.edit_message(
            content="Cancelled. Nothing was removed.",
            embed=None,
            view=None
        )


@bot.tree.command(
    name="removefight",
    description="Remove a fight that was logged by mistake (asks to confirm first)."
)
@app_commands.describe(
    fighter="Remove this fighter's most recent fight",
    opponent="Only fights against this opponent",
    opponent_left="Opponent who has LEFT the server: their user ID or name",
    fight_id="Or: the exact fight number (shown on result cards and in /fights)"
)
async def removefight(
    interaction: discord.Interaction,
    fighter: discord.Member = None,
    opponent: discord.Member = None,
    opponent_left: str = None,
    fight_id: int = None
):

    if not is_staff(interaction):

        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can remove fights.",
            ephemeral=True
        )

        return

    await interaction.response.defer(
        ephemeral=True
    )

    guild_id = interaction.guild.id

    if fight_id is not None:

        fight = get_fight(guild_id, fight_id)

        if not fight:
            await interaction.followup.send(
                f"❌ There is no fight #{fight_id}. Use `/fights` to find the right number.",
                ephemeral=True
            )
            return

    elif fighter:

        history = list(reversed(get_fighter_fights(guild_id, fighter.id)))

        if opponent or opponent_left:
            try:
                opp_id, opp_name, _ = await pick_fighter(
                    interaction.guild, opponent, opponent_left, "opponent"
                )
            except FighterInputError as error:
                await interaction.followup.send(f"❌ {error}", ephemeral=True)
                return

            history = [
                fight for fight in history
                if fight_involves(fight, opp_id, opp_name)
            ]

            if not history:
                await interaction.followup.send(
                    f"❌ No fights found between **{name_without_record(fighter.display_name)}** "
                    f"and **{name_without_record(opp_name)}**.",
                    ephemeral=True
                )
                return

        if not history:
            await interaction.followup.send(
                f"❌ **{name_without_record(fighter.display_name)}** has no logged fights.",
                ephemeral=True
            )
            return

        fight = history[0]

    else:

        await interaction.followup.send(
            "❌ Pick a **fighter** (and optionally their **opponent**), or give a **fight_id**.",
            ephemeral=True
        )

        return

    lines = [
        fight_summary(fight),
        "",
        "**Records after removing it:**",
    ]

    for side in ("winner", "loser"):
        before, after = record_after_removing(guild_id, fight, side)
        lines.append(
            f"• **{name_without_record(fight[f'{side}_name'])}**: {before} → **{after}**"
        )

    lines += ["", "This can't be undone."]

    embed = discord.Embed(
        title="Remove this fight?",
        description="\n".join(lines),
        color=PFO_GOLD
    )

    apply_branding(
        embed,
        author_text="PFO • REMOVE FIGHT"
    )

    await interaction.followup.send(
        embed=embed,
        view=RemoveFightView(
            interaction.user.id,
            guild_id,
            fight
        ),
        ephemeral=True
    )


# ------------------------------------------------------------
# /SYNCNICKNAMES
#
# Updates every fighter's nickname to match their record in one go.
# Shows a preview first; nothing changes until you press confirm.
# Fighters the bot has no record for are left alone.
# ------------------------------------------------------------

NICKNAME_SYNC_DELAY = 0.75  # seconds between changes, gentle on Discord


def nickname_sync_plan(
    guild: discord.Guild
):
    """
    Returns (changes, owner_change, no_record_count):
      changes        [(member, new_nick)] to apply
      owner_change   (member, new_nick) or None; Discord won't allow it
      no_record      fighters the bot has no record for (left alone)
    """

    changes = []
    owner_change = None
    no_record = 0

    for member in guild.members:

        if member.bot:
            continue

        is_fighter = has_any_role(member, FIGHTER_ROLE_NAMES)

        if not has_record_data(guild.id, member.id):
            # Their nickname record (if any) becomes their starting
            # record when they're next in a command, so nothing to do.
            if is_fighter and not record_from_name(member.display_name):
                no_record += 1
            continue

        new_nick = planned_nickname(guild.id, member)

        if not new_nick:
            continue

        if member.id == guild.owner_id:
            owner_change = (member, new_nick)
        else:
            changes.append((member, new_nick))

    changes.sort(key=lambda change: change[0].display_name.casefold())

    return changes, owner_change, no_record


class SyncNicknamesView(
    discord.ui.View
):

    def __init__(
        self,
        author_id: int,
        guild: discord.Guild,
        changes: list
    ):

        super().__init__(
            timeout=600
        )

        self.author_id = author_id
        self.guild = guild
        self.changes = changes

        confirm = discord.ui.Button(
            label=f"Update {len(changes)} nickname{'s' if len(changes) != 1 else ''}",
            style=discord.ButtonStyle.green,
            emoji="✏️",
            disabled=not changes
        )
        confirm.callback = self.confirm

        cancel = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.grey
        )
        cancel.callback = self.cancel

        self.add_item(confirm)
        self.add_item(cancel)

    async def interaction_check(
        self,
        interaction: discord.Interaction
    ):

        if interaction.user.id != self.author_id:

            await interaction.response.send_message(
                "❌ Only the person who started this can confirm it.",
                ephemeral=True
            )

            return False

        return True

    async def cancel(
        self,
        interaction: discord.Interaction
    ):

        self.stop()

        await interaction.response.edit_message(
            content="Cancelled. No nicknames were changed.",
            embed=None,
            view=None
        )

    async def confirm(
        self,
        interaction: discord.Interaction
    ):

        self.stop()

        total = len(self.changes)

        await interaction.response.edit_message(
            content=f"✏️ Updating {total} nicknames… this takes about "
                    f"{max(1, round(total * NICKNAME_SYNC_DELAY))} seconds.",
            embed=None,
            view=None
        )

        changed = 0
        problems = []

        for index, (member, _) in enumerate(self.changes):

            if index:
                await asyncio.sleep(NICKNAME_SYNC_DELAY)

            problem, new_nick = await sync_nickname(
                self.guild,
                member.id
            )

            if new_nick:
                changed += 1
            elif problem:
                problems.append(problem)

                # Missing permission affects everyone; stop early.
                if "Manage Nicknames" in problem:
                    problems.append("Stopped early, so fix the permission and run it again.")
                    break

            if index and index % 10 == 0:
                try:
                    await interaction.edit_original_response(
                        content=f"✏️ Updating nicknames… {index}/{total} done."
                    )
                except Exception:
                    pass

        text = f"✅ **Updated {changed} of {total} nicknames.**"

        await interaction.edit_original_response(
            content=(text + nickname_problem_text(problems))[:2000]
        )


@bot.tree.command(
    name="syncnicknames",
    description="Update every fighter's nickname to match their record (preview first)."
)
async def syncnicknames(
    interaction: discord.Interaction
):

    if not is_staff(interaction):

        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can sync nicknames.",
            ephemeral=True
        )

        return

    if not NICKNAME_RECORDS:

        await interaction.response.send_message(
            "❌ Nickname records are turned off (NICKNAME_RECORDS=off in Railway).",
            ephemeral=True
        )

        return

    await interaction.response.defer(
        ephemeral=True
    )

    changes, owner_change, no_record = nickname_sync_plan(
        interaction.guild
    )

    lines = []

    if changes:
        lines.append(f"**{len(changes)} nickname{'s' if len(changes) != 1 else ''} will change:**")
        for member, new_nick in changes[:40]:
            lines.append(f"• {member.display_name} → **{new_nick}**")
        if len(changes) > 40:
            lines.append(f"• …and {len(changes) - 40} more")
    else:
        lines.append("✅ **Every nickname already matches its record.** Nothing to change.")

    if owner_change:
        member, new_nick = owner_change
        lines += [
            "",
            f"👑 **{member.display_name}** is the server owner. Discord doesn't let "
            f"bots change the owner's nickname, so change it yourself to **{new_nick}**.",
        ]

    if no_record:
        lines += [
            "",
            f"ℹ️ {no_record} fighter{'s have' if no_record != 1 else ' has'} no record in the "
            "bot or their nickname, so they're left alone.",
        ]

    if changes:
        lines += ["", "Nothing changes until you press **Update**."]

    embed = discord.Embed(
        title="Sync nicknames with records?",
        description="\n".join(lines)[:4000],
        color=PFO_GOLD
    )

    apply_branding(
        embed,
        author_text="PFO • SYNC NICKNAMES"
    )

    embed.set_footer(
        text="Records come from the bot (the same as /profile)",
        icon_url=asset_url(LOGO_FILE)
    )

    if changes:
        await interaction.followup.send(
            embed=embed,
            view=SyncNicknamesView(
                interaction.user.id,
                interaction.guild,
                changes
            ),
            ephemeral=True
        )
    else:
        await interaction.followup.send(
            embed=embed,
            ephemeral=True
        )


# ------------------------------------------------------------
# /CREATEFIGHTS and /VIEWCARD
#
# Saves a fight card to the records: @mention fighters in fight
# order, every two mentions are a fight. Nothing is posted publicly.
# /dmfightnight, /dmlivecard and /mainvote use the last saved card
# when their "fighters" box is left empty.
# ------------------------------------------------------------

def setup_fight_card_tables():

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS fight_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            event TEXT NOT NULL,
            created_by INTEGER,
            created_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS fight_card_bouts (
            card_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            fighter_a INTEGER NOT NULL,
            fighter_b INTEGER NOT NULL,

            PRIMARY KEY(card_id, position)
        )
    """)

    cursor.execute("PRAGMA table_info(fight_card_bouts)")

    if "weight" not in [column[1] for column in cursor.fetchall()]:
        cursor.execute("ALTER TABLE fight_card_bouts ADD COLUMN weight TEXT")

    db.commit()
    db.close()


def save_fight_card(
    guild_id: int,
    event: str,
    created_by: int,
    bouts: list
):
    """bouts: [(fighter_a, fighter_b, weight or None)]"""

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        INSERT INTO fight_cards (guild_id, event, created_by, created_at)
        VALUES (?, ?, ?, ?)
    """, (guild_id, event, created_by, now_utc()))

    card_id = cursor.lastrowid

    cursor.executemany("""
        INSERT INTO fight_card_bouts (card_id, position, fighter_a, fighter_b, weight)
        VALUES (?, ?, ?, ?, ?)
    """, [
        (card_id, position, bout[0], bout[1], bout[2] if len(bout) > 2 else None)
        for position, bout in enumerate(bouts, start=1)
    ])

    db.commit()
    db.close()

    return card_id


def get_latest_fight_card(
    guild_id: int,
    event_search: str = None
):

    setup_fight_card_tables()

    db = get_db()
    cursor = db.cursor()

    if event_search:
        cursor.execute("""
            SELECT * FROM fight_cards
            WHERE guild_id = ? AND LOWER(event) LIKE ?
            ORDER BY id DESC LIMIT 1
        """, (guild_id, f"%{event_search.lower()}%"))
    else:
        cursor.execute("""
            SELECT * FROM fight_cards
            WHERE guild_id = ?
            ORDER BY id DESC LIMIT 1
        """, (guild_id,))

    row = cursor.fetchone()
    db.close()

    return row


def get_fight_card_bouts(
    card_id: int
):

    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT * FROM fight_card_bouts
        WHERE card_id = ?
        ORDER BY position ASC
    """, (card_id,))
    rows = cursor.fetchall()
    db.close()

    return rows


def get_latest_signup_session(
    guild_id: int,
    signup_type: str = None
):
    """Most recent sign-up (open or closed), optionally of one type."""

    db = get_db()
    cursor = db.cursor()

    if signup_type:
        cursor.execute("""
            SELECT * FROM signup_sessions
            WHERE guild_id = ? AND signup_type = ?
            ORDER BY id DESC LIMIT 1
        """, (guild_id, signup_type))
    else:
        cursor.execute("""
            SELECT * FROM signup_sessions
            WHERE guild_id = ?
            ORDER BY id DESC LIMIT 1
        """, (guild_id,))

    row = cursor.fetchone()
    db.close()

    return row


# ------------------------------------------------------------
# Card text: names, records, rankings and weight classes
# ------------------------------------------------------------

DIVISION_NAMES = [weight for weight in WEIGHTS.keys() if weight != P4P_WEIGHT]


def auto_weight(
    guild_id: int,
    fighter_a: int,
    fighter_b: int
):
    """The division both are ranked in, else either one's division."""

    def divisions(user_id):
        return [
            weight for weight in DIVISION_NAMES
            if get_user_ranking_in_division(guild_id, user_id, weight)
        ]

    a, b = divisions(fighter_a), divisions(fighter_b)
    both = [weight for weight in a if weight in b]

    if both:
        return both[0]

    if a:
        return a[0]

    if b:
        return b[0]

    return None


def ranking_tag(
    guild_id: int,
    user_id: int,
    weight: str
):
    """'[C]', '[#3]' or '' for this fighter in this weight class."""

    if not weight:
        return ""

    row = get_user_ranking_in_division(guild_id, user_id, weight)

    if not row:
        return ""

    return "[C]" if row["rank"] == 0 else f"[#{row['rank']}]"


def card_fighter_text(
    guild: discord.Guild,
    user_id: int,
    weight: str
):

    member = guild.get_member(user_id)
    name = member.display_name if member else "Unknown fighter"
    tag = ranking_tag(guild.id, user_id, weight)

    return f"{name} {tag}".strip()


def card_bout_line(
    guild: discord.Guild,
    number: int,
    fighter_a: int,
    fighter_b: int,
    weight: str
):

    line = (
        f"{number}. {card_fighter_text(guild, fighter_a, weight)} vs "
        f"{card_fighter_text(guild, fighter_b, weight)}"
    )

    if weight:
        line += f" - {weight}"

    return line


def card_as_text(
    guild: discord.Guild,
    event: str,
    bouts: list
):
    """Plain text, ready to copy and paste."""

    lines = [event, ""]

    lines += [
        card_bout_line(guild, number, a, b, weight)
        for number, (a, b, weight) in enumerate(bouts, start=1)
    ]

    return "\n".join(lines)


async def send_card_text(
    interaction: discord.Interaction,
    text: str
):
    """Sends the card as plain text (split if it's very long)."""

    chunks = []
    current = ""

    for line in text.split("\n"):
        if len(current) + len(line) + 1 > 1900:
            chunks.append(current)
            current = ""
        current += line + "\n"

    if current.strip():
        chunks.append(current)

    if not chunks:
        chunks = ["(The card is empty.)"]

    for index, chunk in enumerate(chunks):
        if index == 0 and not interaction.response.is_done():
            await interaction.response.send_message(chunk, ephemeral=True)
        else:
            await interaction.followup.send(chunk, ephemeral=True)


async def send_card_text_safely(
    interaction: discord.Interaction,
    make_text
):
    """Copy as text, but if anything goes wrong say so instead of failing silently."""

    try:
        text = make_text()
    except Exception as error:
        print(f"Copy as text failed: {error!r}")
        await interaction.response.send_message(
            f"❌ Couldn't make the text ({type(error).__name__}). Please tell whoever runs the bot.",
            ephemeral=True
        )
        return

    try:
        await send_card_text(interaction, text)
    except Exception as error:
        print(f"Copy as text send failed: {error!r}")


def saved_card_bouts(
    card_id: int
):

    return [
        (bout["fighter_a"], bout["fighter_b"], bout["weight"])
        for bout in get_fight_card_bouts(card_id)
    ]


class CopyCardView(
    discord.ui.View
):
    """
    The copy button under a saved card (after saving, and on /viewcard).
    It keeps working after the bot restarts.
    """

    def __init__(
        self,
        card_id: int
    ):

        super().__init__(
            timeout=None
        )

        self.card_id = card_id

        copy = discord.ui.Button(
            label="Copy as text",
            emoji="📋",
            style=discord.ButtonStyle.blurple,
            custom_id=f"pfo_copycard_{card_id}"
        )
        copy.callback = self.copy
        self.add_item(copy)

    async def copy(self, interaction):

        rows = discipline_query("SELECT * FROM fight_cards WHERE id = ?", (self.card_id,))

        if not rows:
            await interaction.response.send_message("❌ That card no longer exists.", ephemeral=True)
            return

        await send_card_text_safely(
            interaction,
            lambda: card_as_text(interaction.guild, rows[0]["event"], saved_card_bouts(self.card_id))
        )


def restore_copy_buttons():

    try:
        for row in discipline_query("SELECT id FROM fight_cards ORDER BY id DESC LIMIT 50"):
            bot.add_view(CopyCardView(row["id"]))
    except Exception as error:
        print(f"Could not restore copy buttons: {error}")


# ------------------------------------------------------------
# Builder drafts: the panel is saved as you go, so it keeps
# working after the bot restarts (kept for 3 days).
# ------------------------------------------------------------

DRAFT_DAYS = 3


def setup_card_draft_tables():

    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS card_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            author_id INTEGER NOT NULL,
            event TEXT NOT NULL,
            signup_ids TEXT NOT NULL,
            signup_type TEXT,
            shared INTEGER NOT NULL DEFAULT 1,
            bouts TEXT NOT NULL DEFAULT '[]',
            page INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL
        )
    """)
    db.commit()
    db.close()


def create_card_draft(
    guild_id: int,
    author_id: int,
    event: str,
    signup_ids: list,
    signup_type: str,
    shared: bool
):

    return discipline_query("""
        INSERT INTO card_drafts (guild_id, author_id, event, signup_ids, signup_type, shared, bouts, page, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, '[]', 0, ?)
    """, (guild_id, author_id, event, json.dumps(signup_ids), signup_type, 1 if shared else 0, int(time.time())),
        write=True)


# ------------------------------------------------------------
# The fight card builder
#
# /createfights opens a builder loaded with the sign-up list. It's
# posted in the channel so any staff member can work on it at the
# same time (private: Yes keeps it to just you). Pick two names from the dropdown = one fight. A checklist
# shows who still needs a fight. Each fight gets a weight class
# (worked out from the rankings, changeable). "Copy as text" gives
# the card as plain text. Saving keeps it in the records only.
# ------------------------------------------------------------

SELECT_PAGE_SIZE = 25  # Discord's limit per dropdown
NO_WEIGHT = "none"


class CardBuilderView(
    discord.ui.View
):

    def __init__(
        self,
        author_id: int,
        guild: discord.Guild,
        event: str,
        signup_ids: list,
        signup_type: str = None,
        shared: bool = True,
        draft_id: int = None
    ):

        super().__init__(
            timeout=None
        )

        self.shared = shared
        self.draft_id = draft_id

        self.author_id = author_id
        self.guild = guild
        self.event = event
        self.signup_ids = signup_ids
        self.signup_type = signup_type
        self.bouts = []      # [fighter_a, fighter_b, weight]
        self.page = 0

        self.rebuild()

    # -------- state --------

    def cid(self, name: str):
        """Button ids unique to this panel, so it can be restored after a restart."""
        return f"pfo_cb_{self.draft_id}_{name}" if self.draft_id else None

    def save_draft(self):

        if not self.draft_id:
            return

        try:
            discipline_query(
                "UPDATE card_drafts SET bouts = ?, page = ?, updated_at = ? WHERE id = ?",
                (json.dumps(self.bouts), self.page, int(time.time()), self.draft_id),
                write=True
            )
        except Exception as error:
            print(f"Could not save card draft: {error}")

    def delete_draft(self):

        if self.draft_id:
            try:
                discipline_query("DELETE FROM card_drafts WHERE id = ?", (self.draft_id,), write=True)
            except Exception as error:
                print(f"Could not delete card draft: {error}")

    @property
    def pairs(self):
        return [(a, b) for a, b, _ in self.bouts]

    @pairs.setter
    def pairs(self, value):
        self.bouts = [
            [a, b, auto_weight(self.guild.id, a, b)]
            for a, b in value
        ]

    def paired_ids(self):
        return {user_id for a, b, _ in self.bouts for user_id in (a, b)}

    def unpaired(self):
        paired = self.paired_ids()
        return [user_id for user_id in self.signup_ids if user_id not in paired]

    def name(self, user_id):
        member = self.guild.get_member(user_id)
        return name_without_record(member.display_name) if member else f"User {user_id}"

    def label(self, user_id):
        member = self.guild.get_member(user_id)
        return (member.display_name if member else f"User {user_id}")[:100]

    # -------- display --------

    def embed(self):

        lines = []

        if self.bouts:
            lines.append(f"**Fights ({len(self.bouts)})**")
            for number, (a, b, weight) in enumerate(self.bouts, start=1):
                lines.append(
                    f"`{number:>2}` **{card_fighter_text(self.guild, a, weight)}**  vs  "
                    f"**{card_fighter_text(self.guild, b, weight)}**"
                    f"  ·  *{weight or 'no weight class'}*"
                )
        else:
            lines.append("**No fights yet.** Pick two fighters from the first dropdown to make a fight.")

        lines.append("")

        if self.signup_ids:
            paired = self.paired_ids()
            left = len(self.unpaired())
            lines.append(
                f"**Sign-up checklist** ({len(self.signup_ids) - left}/{len(self.signup_ids)} matched"
                + (f", **{left} left**" if left else ", all done ✅") + ")"
            )
            # Still needing a fight first, then the ones already matched.
            lines += [f"⬜ {self.name(user_id)}" for user_id in self.signup_ids if user_id not in paired]
            lines += [f"✅ ~~{self.name(user_id)}~~" for user_id in self.signup_ids if user_id in paired]
        else:
            lines.append("ℹ️ No sign-up list found, so use the second dropdown to pick anyone.")

        embed = discord.Embed(
            title=self.event,
            description="\n".join(lines)[:4000],
            color=PFO_GOLD
        )

        apply_branding(
            embed,
            author_text="PFO • CREATE FIGHTS"
        )

        embed.set_footer(
            text="✅ has a fight  ⬜ still needs one • Weight classes come from the rankings",
            icon_url=asset_url(LOGO_FILE)
        )

        return embed

    def rebuild(self):

        self.clear_items()

        left = self.unpaired()
        pages = max(1, (len(left) + SELECT_PAGE_SIZE - 1) // SELECT_PAGE_SIZE)
        self.page = min(self.page, pages - 1)
        page_ids = left[self.page * SELECT_PAGE_SIZE:(self.page + 1) * SELECT_PAGE_SIZE]

        # Row 0: the sign-up list (only people still needing a fight)
        if len(page_ids) >= 2:
            signup_select = discord.ui.Select(
                placeholder=(
                    "Pick 2 signed-up fighters to make a fight"
                    + (f" (page {self.page + 1}/{pages})" if pages > 1 else "")
                ),
                min_values=2,
                max_values=2,
                options=[
                    discord.SelectOption(label=self.label(user_id), value=str(user_id))
                    for user_id in page_ids
                ],
                row=0,
                custom_id=self.cid("signups")
            )
            signup_select.callback = self.pick_from_signups
            self.add_item(signup_select)
        else:
            self.add_item(discord.ui.Select(
                placeholder=(
                    "Everyone on the list has a fight" if self.signup_ids and not left
                    else "Not enough unmatched sign-ups on this page"
                ),
                options=[discord.SelectOption(label="-", value="0")],
                disabled=True,
                row=0,
                custom_id=self.cid("signups_off")
            ))

        # Row 1: anyone in the server (people who didn't sign up)
        anyone = discord.ui.UserSelect(
            placeholder="Or pick any 2 members (search by typing)",
            min_values=2,
            max_values=2,
            row=1,
            custom_id=self.cid("anyone")
        )
        anyone.callback = self.pick_anyone
        self.add_item(anyone)

        # Row 2: weight class for the newest fight
        if self.bouts:
            number = len(self.bouts)
            current = self.bouts[-1][2]
            weight_select = discord.ui.Select(
                placeholder=f"Weight class for fight {number}: {current or 'none'} (change)",
                options=[
                    discord.SelectOption(label=weight, value=weight, default=(weight == current))
                    for weight in DIVISION_NAMES
                ] + [discord.SelectOption(label="No weight class", value=NO_WEIGHT, default=current is None)],
                row=2,
                custom_id=self.cid("weight")
            )
            weight_select.callback = self.pick_weight
            self.add_item(weight_select)
        else:
            self.add_item(discord.ui.Select(
                placeholder="Weight class (add a fight first)",
                options=[discord.SelectOption(label="-", value="0")],
                disabled=True,
                row=2,
                custom_id=self.cid("weight_off")
            ))

        # Row 3: paging, undo, copy
        previous = discord.ui.Button(label="◀ Page", style=discord.ButtonStyle.grey,
                                     disabled=self.page == 0, row=3, custom_id=self.cid("prev"))
        previous.callback = self.previous_page
        following = discord.ui.Button(label="Page ▶", style=discord.ButtonStyle.grey,
                                      disabled=self.page >= pages - 1, row=3, custom_id=self.cid("next"))
        following.callback = self.next_page
        undo = discord.ui.Button(label="Undo last fight", emoji="↩️", style=discord.ButtonStyle.grey,
                                 disabled=not self.bouts, row=3, custom_id=self.cid("undo"))
        undo.callback = self.undo
        copy = discord.ui.Button(label="Copy as text", emoji="📋", style=discord.ButtonStyle.blurple,
                                 disabled=not self.bouts, row=3, custom_id=self.cid("copy"))
        copy.callback = self.copy
        for item in (previous, following, undo, copy):
            self.add_item(item)

        # Row 4: finish
        save = discord.ui.Button(label="Save card", emoji="💾", style=discord.ButtonStyle.green,
                                 disabled=not self.bouts, row=4, custom_id=self.cid("save"))
        save.callback = self.save
        cancel = discord.ui.Button(label="Close", style=discord.ButtonStyle.grey, row=4,
                                   custom_id=self.cid("close"))
        cancel.callback = self.cancel
        self.add_item(save)
        self.add_item(cancel)

    async def redraw(self, interaction, note: str = None):
        self.rebuild()
        self.save_draft()

        # With several staff on the same panel, say who did what.
        if note and self.shared:
            note = f"{note}  ·  *{name_without_record(interaction.user.display_name)}*"

        await interaction.response.edit_message(
            content=note,
            embed=self.embed(),
            view=self
        )

    # -------- actions --------

    async def interaction_check(self, interaction):

        if self.shared:
            if is_staff(interaction):
                return True

            await interaction.response.send_message(
                f"❌ {STAFF_ONLY_TEXT} can edit the fight card.",
                ephemeral=True
            )
            return False

        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the person who opened this builder can use it.",
                ephemeral=True
            )
            return False

        return True

    async def add_pair(self, interaction, a: int, b: int):

        if a == b:
            await self.redraw(interaction, "❌ Pick two different fighters.")
            return

        paired = self.paired_ids()
        already = [user_id for user_id in (a, b) if user_id in paired]

        if already:
            await self.redraw(interaction, f"❌ **{self.name(already[0])}** already has a fight. Undo it first to change it.")
            return

        weight = auto_weight(self.guild.id, a, b)
        self.bouts.append([a, b, weight])

        await self.redraw(
            interaction,
            f"✅ Added **{self.name(a)}** vs **{self.name(b)}**"
            + (f" at **{weight}**" if weight else " (no weight class: pick one below if needed)")
        )

    async def pick_from_signups(self, interaction):
        values = [int(value) for value in interaction.data.get("values", [])]
        if len(values) == 2:
            await self.add_pair(interaction, values[0], values[1])

    async def pick_anyone(self, interaction):
        values = [int(value) for value in interaction.data.get("values", [])]
        if len(values) == 2:
            for user_id in values:
                member = self.guild.get_member(user_id)
                if member and member.bot:
                    await self.redraw(interaction, "❌ Bots can't fight.")
                    return
            await self.add_pair(interaction, values[0], values[1])

    async def pick_weight(self, interaction):
        values = interaction.data.get("values", [])
        if values and self.bouts:
            self.bouts[-1][2] = None if values[0] == NO_WEIGHT else values[0]
            await self.redraw(interaction, f"⚖️ Fight {len(self.bouts)} is now **{self.bouts[-1][2] or 'no weight class'}**")

    async def previous_page(self, interaction):
        self.page = max(0, self.page - 1)
        await self.redraw(interaction)

    async def next_page(self, interaction):
        self.page += 1
        await self.redraw(interaction)

    async def undo(self, interaction):
        if self.bouts:
            a, b, _ = self.bouts.pop()
            await self.redraw(interaction, f"↩️ Removed **{self.name(a)}** vs **{self.name(b)}**")
        else:
            await self.redraw(interaction)

    async def copy(self, interaction):
        await send_card_text_safely(
            interaction,
            lambda: card_as_text(self.guild, self.event, [tuple(bout) for bout in self.bouts])
        )

    async def cancel(self, interaction):
        self.stop()
        self.delete_draft()
        await interaction.response.edit_message(
            content=f"Closed by {interaction.user.display_name}. Nothing was saved.",
            embed=None,
            view=None
        )

    async def save(self, interaction):

        self.stop()
        self.delete_draft()

        card_id = save_fight_card(
            self.guild.id,
            self.event,
            interaction.user.id,
            [tuple(bout) for bout in self.bouts]
        )

        await interaction.response.edit_message(
            content=(
                f"💾 **Saved {self.event}** ({len(self.bouts)} fights) to the records. "
                "See it again any time with `/viewcard`."
            ),
            embed=self.embed(),
            view=CopyCardView(card_id)
        )


@bot.tree.command(
    name="createfights",
    description="Build a fight card from the sign-up list by picking pairs (records only)."
)
@app_commands.describe(
    sign_ups="Which sign-up list to build from (default: the most recent one)",
    event="Event name for the records, e.g. PFO Fight Night 16",
    fighters="Optional shortcut: @mention in fight order instead of using the dropdowns",
    private="Yes = only you can see and edit it. Default: posted so all staff can edit together"
)
@app_commands.choices(
    sign_ups=[
        app_commands.Choice(name="Fight Night", value="fight_night"),
        app_commands.Choice(name="Live Card", value="live_card"),
    ],
    private=[
        app_commands.Choice(name="Yes", value="yes"),
        app_commands.Choice(name="No", value="no"),
    ]
)
async def createfights(
    interaction: discord.Interaction,
    sign_ups: app_commands.Choice[str] = None,
    event: str = None,
    fighters: str = None,
    private: app_commands.Choice[str] = None
):

    if not is_staff(interaction):

        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can create fights.",
            ephemeral=True
        )

        return

    session = get_latest_signup_session(
        interaction.guild.id,
        sign_ups.value if sign_ups else None
    )

    signup_type = session["signup_type"] if session else (sign_ups.value if sign_ups else None)

    if event and event.strip():
        event = event.strip()
    elif signup_type:
        event = f"PFO {SIGNUP_LABELS[signup_type].title()} {datetime.now(timezone.utc):%d/%m/%Y}"
    else:
        event = f"PFO Card {datetime.now(timezone.utc):%d/%m/%Y}"

    signup_ids = []

    if session:
        for row in get_signups(session["id"]):
            member = interaction.guild.get_member(row["discord_user_id"])
            if member and not member.bot and member.id not in signup_ids:
                signup_ids.append(member.id)

    shared = not private or private.value != "yes"

    try:
        draft_id = create_card_draft(
            interaction.guild.id, interaction.user.id, event, signup_ids, signup_type, shared
        )
    except Exception as error:
        print(f"Could not create card draft: {error}")
        draft_id = None

    builder = CardBuilderView(
        interaction.user.id,
        interaction.guild,
        event,
        signup_ids,
        signup_type,
        shared,
        draft_id
    )

    # Shortcut: typed @mentions fill the fights straight away.
    if fighters and MENTION_PATTERN.search(fighters):

        pairs = parse_fight_pairs(fighters)

        if pairs[-1][1] is None:
            await interaction.response.send_message(
                f"❌ That's an odd number of fighters: <@{pairs[-1][0]}> has no opponent.",
                ephemeral=True
            )
            return

        builder.pairs = [(a, b) for a, b in pairs if a != b]
        builder.rebuild()
        builder.save_draft()

        await interaction.response.send_message(
            content="Check the pairings and weight classes, then press **Save card** or **Copy as text**.",
            embed=builder.embed(),
            view=builder,
            ephemeral=not shared
        )
        return

    intro = (
        f"Loaded **{len(signup_ids)}** fighters from the "
        f"**{SIGNUP_INFO[signup_type]['title']}** list. "
        if session else ""
    ) + "Pick two names to make a fight; repeat until the card is done."

    if shared:
        intro += "\n👥 Any staff member can use this panel at the same time. Copy as text only shows to whoever presses it."

    await interaction.response.send_message(
        content=intro,
        embed=builder.embed(),
        view=builder,
        ephemeral=not shared
    )


@bot.tree.command(
    name="viewcard",
    description="Show the last fight card saved with /createfights."
)
@app_commands.describe(
    event="Search for an older card by event name (optional)"
)
async def viewcard(
    interaction: discord.Interaction,
    event: str = None
):

    if not is_staff(interaction):

        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can view saved cards.",
            ephemeral=True
        )

        return

    card = get_latest_fight_card(
        interaction.guild.id,
        event.strip() if event else None
    )

    if not card:

        await interaction.response.send_message(
            "❌ No saved card found. Save one with `/createfights`.",
            ephemeral=True
        )

        return

    bouts = [
        (bout["fighter_a"], bout["fighter_b"], bout["weight"])
        for bout in get_fight_card_bouts(card["id"])
    ]

    lines = []

    for number, (a, b, weight) in enumerate(bouts, start=1):
        lines.append(
            f"`{number:>2}` **{card_fighter_text(interaction.guild, a, weight)}**  vs  "
            f"**{card_fighter_text(interaction.guild, b, weight)}**"
            f"  ·  *{weight or 'no weight class'}*"
        )

    embed = discord.Embed(
        title=card["event"],
        description="\n".join(lines)[:4000],
        color=PFO_GOLD
    )

    apply_branding(
        embed,
        author_text="PFO • SAVED FIGHT CARD"
    )

    embed.set_footer(
        text=f"{len(bouts)} fights • saved {card['created_at'][:10]} • rankings shown as they are now",
        icon_url=asset_url(LOGO_FILE)
    )

    await interaction.response.send_message(
        embed=embed,
        view=CopyCardView(card["id"]),
        ephemeral=True
    )


# ------------------------------------------------------------
# RANKING SUGGESTIONS
#
# After /result, the bot checks whether the rankings should change
# and posts a suggestion in the staff channel (🔒│pfo-bot) with
# Apply / Dismiss buttons. Nothing changes until staff press Apply.
#
# Rules:
#   • Title fight won by a non-champion -> winner becomes Champion
#   • Winner beats someone ranked above them (or is unranked) ->
#     winner takes the loser's spot, loser drops one place
#   • Beating the champion in a non-title fight -> winner to #1
#   • P4P works the same way (no champion)
#
# Set RANKING_SUGGESTIONS_CHANNEL in Railway to use another channel
# (name or channel ID).
# ------------------------------------------------------------

RANKING_SUGGESTIONS_CHANNEL = os.getenv(
    "RANKING_SUGGESTIONS_CHANNEL",
    "pfo-bot"
).strip()


def setup_suggestion_tables():

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ranking_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            fight_id INTEGER,
            weight TEXT NOT NULL,
            winner_id INTEGER NOT NULL,
            loser_id INTEGER,
            target_rank INTEGER NOT NULL,
            reason TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            channel_id INTEGER,
            message_id INTEGER,
            created_at TEXT NOT NULL
        )
    """)

    db.commit()
    db.close()


def find_suggestions_channel(
    guild: discord.Guild
):

    setting = RANKING_SUGGESTIONS_CHANNEL

    if setting.isdigit():
        return guild.get_channel(int(setting))

    wanted = role_key(setting)

    for channel in getattr(guild, "text_channels", []):
        if role_key(channel.name) == wanted:
            return channel

    return None


def rank_text(
    weight: str,
    rank
):

    if rank is None:
        return "Unranked"

    if rank == 0:
        return "👑 Champion"

    return f"#{rank}"


def work_out_suggestions(
    guild_id: int,
    fight: dict
):
    """[(weight, target_rank, reason)] for a fight result."""

    if fight["method"] in ("DRAW", "NC"):
        return []

    winner_id = fight.get("winner_id")
    loser_id = fight.get("loser_id")

    if not winner_id:
        return []

    def rank_of(user_id, weight):
        if not user_id:
            return None
        row = get_user_ranking_in_division(guild_id, user_id, weight)
        return row["rank"] if row else None

    if fight.get("division"):
        weights = [fight["division"]]
    else:
        weights = [
            weight for weight in WEIGHTS.keys()
            if weight != P4P_WEIGHT and rank_of(loser_id, weight) is not None
        ]

    suggestions = []

    for weight in weights + [P4P_WEIGHT]:

        winner_rank = rank_of(winner_id, weight)
        loser_rank = rank_of(loser_id, weight)

        if (
            weight != P4P_WEIGHT
            and fight.get("title_fight")
            and fight.get("division") == weight
            and winner_rank != 0
        ):
            suggestions.append((
                weight, 0,
                f"won the {weight} title"
            ))
            continue

        if loser_rank is None:
            continue

        target = 1 if loser_rank == 0 else loser_rank

        if winner_rank is not None and winner_rank != 0 and winner_rank <= target:
            continue  # already ranked at or above that spot

        if winner_rank == 0:
            continue  # champion beating a contender: no change

        suggestions.append((
            weight, target,
            f"{rank_text(weight, loser_rank)} in {'P4P' if weight == P4P_WEIGHT else weight}"
        ))

    return suggestions


def create_suggestion_embed(
    guild: discord.Guild,
    row,
    status_line: str = None
):

    def name(user_id):
        if not user_id:
            return "Unknown"
        member = guild.get_member(user_id)
        return name_without_record(member.display_name) if member else f"<@{user_id}>"

    weight = row["weight"]
    board = "P4P" if weight == P4P_WEIGHT else weight

    current = get_user_ranking_in_division(guild.id, row["winner_id"], weight)
    now_rank = current["rank"] if current else None

    if row["target_rank"] == 0 and row["reason"].startswith("won"):
        headline = (
            f"👑 **{name(row['winner_id'])}** {row['reason']}"
            + (f" against **{name(row['loser_id'])}**" if row["loser_id"] else "") + "."
        )
    else:
        headline = (
            f"🥊 **{name(row['winner_id'])}** beat **{name(row['loser_id'])}** "
            f"({row['reason']})."
        )

    lines = [
        headline,
        "",
        f"**Suggestion:** move **{name(row['winner_id'])}** to "
        f"**{rank_text(weight, row['target_rank'])}** in **{board}**",
        f"Currently: {rank_text(weight, now_rank)}",
    ]

    if row["target_rank"] == 0:
        lines.append("The current champion would move to #1.")
    elif row["loser_id"]:
        lines.append(f"Everyone from {rank_text(weight, row['target_rank'])} down moves one place.")

    if status_line:
        lines += ["", status_line]

    embed = discord.Embed(
        title=f"Ranking suggestion: {board}",
        description="\n".join(lines),
        color=PFO_GOLD,
        timestamp=discord.utils.utcnow()
    )

    apply_branding(
        embed,
        author_text="PFO • RANKING SUGGESTION"
    )

    if row["fight_id"]:
        embed.set_footer(
            text=f"From fight #{row['fight_id']}",
            icon_url=asset_url(LOGO_FILE)
        )

    return embed


def get_suggestion(
    suggestion_id: int
):

    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM ranking_suggestions WHERE id = ?", (suggestion_id,))
    row = cursor.fetchone()
    db.close()

    return row


def update_suggestion(
    suggestion_id: int,
    **changes
):

    columns = ", ".join(f"{column} = ?" for column in changes)

    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        f"UPDATE ranking_suggestions SET {columns} WHERE id = ?",
        (*changes.values(), suggestion_id)
    )
    db.commit()
    db.close()


class SuggestionView(
    discord.ui.View
):
    """Apply / Dismiss buttons. Work after restarts."""

    def __init__(
        self,
        suggestion_id: int
    ):

        super().__init__(
            timeout=None
        )

        self.suggestion_id = suggestion_id

        apply = discord.ui.Button(
            label="Apply",
            style=discord.ButtonStyle.green,
            emoji="✅",
            custom_id=f"pfo_rsug_apply_{suggestion_id}"
        )
        apply.callback = self.apply

        dismiss = discord.ui.Button(
            label="Dismiss",
            style=discord.ButtonStyle.grey,
            custom_id=f"pfo_rsug_dismiss_{suggestion_id}"
        )
        dismiss.callback = self.dismiss

        self.add_item(apply)
        self.add_item(dismiss)

    async def interaction_check(self, interaction):

        if not is_staff(interaction):
            await interaction.response.send_message(
                f"❌ {STAFF_ONLY_TEXT} can change the rankings.",
                ephemeral=True
            )
            return False

        return True

    async def apply(self, interaction):

        row = get_suggestion(self.suggestion_id)

        if not row or row["status"] != "pending":
            await interaction.response.send_message("This suggestion has already been dealt with.", ephemeral=True)
            return

        target = row["target_rank"]

        # If the loser has moved since, take their current spot instead.
        if target != 0 and row["loser_id"]:
            current = get_user_ranking_in_division(interaction.guild.id, row["loser_id"], row["weight"])
            if current is not None:
                target = 1 if current["rank"] == 0 else current["rank"]

        update_ranking(
            interaction.guild.id,
            row["weight"],
            row["winner_id"],
            target
        )

        update_suggestion(self.suggestion_id, status="applied")

        await interaction.response.edit_message(
            embed=create_suggestion_embed(
                interaction.guild,
                get_suggestion(self.suggestion_id),
                f"✅ **Applied by {interaction.user.display_name}**: now "
                f"{rank_text(row['weight'], target)} in "
                f"{'P4P' if row['weight'] == P4P_WEIGHT else row['weight']}."
            ),
            view=None
        )

        await refresh_ranking_message(
            interaction.guild,
            row["weight"]
        )

    async def dismiss(self, interaction):

        row = get_suggestion(self.suggestion_id)

        if not row or row["status"] != "pending":
            await interaction.response.send_message("This suggestion has already been dealt with.", ephemeral=True)
            return

        update_suggestion(self.suggestion_id, status="dismissed")

        await interaction.response.edit_message(
            embed=create_suggestion_embed(
                interaction.guild,
                row,
                f"✖️ **Dismissed by {interaction.user.display_name}**. Rankings unchanged."
            ),
            view=None
        )


async def post_ranking_suggestions(
    guild: discord.Guild,
    fight_id: int,
    fight: dict
):
    """Returns a problem message for staff, or None."""

    suggestions = work_out_suggestions(guild.id, fight)

    if not suggestions:
        return None

    channel = find_suggestions_channel(guild)

    if channel is None:
        return (
            f"⚠️ This result should change the rankings, but I couldn't find the "
            f"**{RANKING_SUGGESTIONS_CHANNEL}** channel to post the suggestion in."
        )

    problems = []

    for weight, target, reason in suggestions:

        db = get_db()
        cursor = db.cursor()
        cursor.execute("""
            INSERT INTO ranking_suggestions
            (guild_id, fight_id, weight, winner_id, loser_id, target_rank, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            guild.id, fight_id, weight, fight["winner_id"], fight.get("loser_id"),
            target, reason, now_utc()
        ))
        suggestion_id = cursor.lastrowid
        db.commit()
        db.close()

        try:
            message = await channel.send(
                embed=create_suggestion_embed(guild, get_suggestion(suggestion_id)),
                view=SuggestionView(suggestion_id)
            )
            update_suggestion(
                suggestion_id,
                channel_id=channel.id,
                message_id=message.id
            )
        except discord.Forbidden:
            problems.append(f"⚠️ I can't post in {channel.mention}. Give the bot **Send Messages** there.")
            update_suggestion(suggestion_id, status="failed")
            break
        except Exception as error:
            print(f"Could not post ranking suggestion: {error}")

    return problems[0] if problems else None


def restore_suggestion_buttons():

    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id FROM ranking_suggestions WHERE status = 'pending'")
    rows = cursor.fetchall()
    db.close()

    for row in rows:
        try:
            bot.add_view(SuggestionView(row["id"]))
        except Exception as error:
            print(f"Could not restore suggestion {row['id']}: {error}")


# ============================================================
# FIGHT VOTING
#
# /mainvote posts the fights (as many as you like) with a
# "Make your picks" button. It opens a private ballot with a dropdown
# per fight. Anyone can pick who they think will win each fight, and
# change their picks until the deadline. At the deadline voting
# closes by itself and shows how everyone voted. When results are
# logged with /result, correct picks are counted for the voting
# leaderboard.
# ============================================================

VOTE_BANNER = "vote.gif"
VOTE_CLOSED_BANNER = "vote_closed.gif"
LEADERBOARD_BANNER = "leaderboard.gif"
MAX_VOTE_FIGHTS = 30       # more than any card; keeps the post readable
BALLOT_PAGE_SIZE = 4       # dropdowns per ballot page (Discord allows 5 rows)
SIDE_A, SIDE_B = "🔴", "🔵"

# Polls with new picks; their public post is refreshed once a minute
# (not on every pick, so busy votes don't hit Discord's limits).
_polls_to_refresh = set()


def setup_vote_tables():

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS vote_polls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            event TEXT NOT NULL,
            deadline INTEGER NOT NULL,
            channel_id INTEGER,
            message_id INTEGER,
            status TEXT NOT NULL DEFAULT 'open',
            created_by INTEGER,
            created_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS vote_bouts (
            poll_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            fighter_a INTEGER NOT NULL,
            fighter_b INTEGER NOT NULL,
            name_a TEXT NOT NULL,
            name_b TEXT NOT NULL,
            winner_id INTEGER,
            result_fight_id INTEGER,
            resolved INTEGER NOT NULL DEFAULT 0,

            PRIMARY KEY(poll_id, position)
        )
    """)

    cursor.execute("PRAGMA table_info(vote_bouts)")

    if "weight" not in [column[1] for column in cursor.fetchall()]:
        cursor.execute("ALTER TABLE vote_bouts ADD COLUMN weight TEXT")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS votes (
            poll_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            pick INTEGER NOT NULL,
            voted_at TEXT NOT NULL,

            PRIMARY KEY(poll_id, position, user_id)
        )
    """)

    db.commit()
    db.close()


def vote_query(
    sql: str,
    params: tuple = (),
    one: bool = False,
    write: bool = False
):

    db = get_db()
    cursor = db.cursor()
    cursor.execute(sql, params)

    if write:
        db.commit()
        result = cursor.lastrowid
    else:
        result = cursor.fetchone() if one else cursor.fetchall()

    db.close()

    return result


def get_poll(poll_id: int):
    return vote_query("SELECT * FROM vote_polls WHERE id = ?", (poll_id,), one=True)


def get_poll_bouts(poll_id: int):
    return vote_query(
        "SELECT * FROM vote_bouts WHERE poll_id = ? ORDER BY position", (poll_id,)
    )


def vote_counts(poll_id: int):
    """{position: {fighter_id: count}}"""

    counts = {}

    for row in vote_query(
        "SELECT position, pick, COUNT(*) AS n FROM votes WHERE poll_id = ? GROUP BY position, pick",
        (poll_id,)
    ):
        counts.setdefault(row["position"], {})[row["pick"]] = row["n"]

    return counts


def poll_is_open(poll):
    return poll and poll["status"] == "open" and time.time() < poll["deadline"]


def user_picks(poll_id: int, user_id: int):
    """{position: fighter_id}"""

    return {
        row["position"]: row["pick"]
        for row in vote_query(
            "SELECT position, pick FROM votes WHERE poll_id = ? AND user_id = ?", (poll_id, user_id)
        )
    }


def save_pick(poll_id: int, position: int, user_id: int, pick: int):

    vote_query(
        "INSERT OR REPLACE INTO votes (poll_id, position, user_id, pick, voted_at) VALUES (?, ?, ?, ?, ?)",
        (poll_id, position, user_id, pick, now_utc()), write=True
    )

    _polls_to_refresh.add(poll_id)


def bout_weight(bout):
    try:
        return bout["weight"]
    except (IndexError, KeyError):
        return None


def vote_bar(percent: int):
    filled = round(percent / 10)
    return "▰" * filled + "▱" * (10 - filled)


def create_vote_embeds(
    poll
):
    """The public vote post (more than one embed if the card is very long)."""

    bouts = get_poll_bouts(poll["id"])
    is_open = poll_is_open(poll)
    counts = vote_counts(poll["id"])
    voters = vote_query(
        "SELECT COUNT(DISTINCT user_id) AS n FROM votes WHERE poll_id = ?", (poll["id"],), one=True
    )["n"]

    if is_open:
        header = [
            "🗳️ **Pick the winner of every fight.** Press **Make your picks** below.",
            f"⏰ Voting closes <t:{poll['deadline']}:F> (<t:{poll['deadline']}:R>)",
            "You can change your picks until then.",
        ]
    else:
        header = ["🔒 **Voting has closed.** Here's how everyone picked."]

    blocks = []

    for bout in bouts:

        a, b = bout["fighter_a"], bout["fighter_b"]
        weight = bout_weight(bout)
        number = f"`{bout['position']:>2}`"
        tag_a = ranking_tag(poll["guild_id"], a, weight)
        tag_b = ranking_tag(poll["guild_id"], b, weight)
        tag_a = f" {tag_a}" if tag_a else ""
        tag_b = f" {tag_b}" if tag_b else ""

        if is_open:
            line = f"{number} **{bout['name_a']}**{tag_a}  vs  **{bout['name_b']}**{tag_b}"
            if weight:
                line += f"\n└ ⚖️ {weight}"
            blocks.append(line)
            continue

        split = counts.get(bout["position"], {})
        total = sum(split.values())
        pa = round(100 * split.get(a, 0) / total) if total else 0
        pb = 100 - pa if total else 0

        mark_a = mark_b = ""

        if bout["resolved"] and bout["winner_id"]:
            mark_a = " ✅" if bout["winner_id"] == a else ""
            mark_b = " ✅" if bout["winner_id"] == b else ""
        elif bout["resolved"]:
            mark_a = mark_b = " 🤝"

        result_line = (
            f"└ {pa}% {vote_bar(pa)} {pb}% · {plural(total, 'vote')}" if total
            else "└ No votes"
        )

        blocks.append(
            f"{number} {SIDE_A} **{bout['name_a']}**{mark_a}  vs  **{bout['name_b']}**{mark_b} {SIDE_B}\n"
            + result_line
        )

    footer_line = f"👥 **{voters}** {'person has' if voters == 1 else 'people have'} voted"

    # Split into embeds of up to ~3,800 characters.
    pages = [header + [""]]

    for block in blocks:
        if len("\n".join(pages[-1])) + len(block) > 3800:
            pages.append([])
        pages[-1].append(block)

    pages[-1] += ["", footer_line]

    embeds = []

    for index, page in enumerate(pages):

        embed = discord.Embed(
            title=poll["event"] if index == 0 else None,
            description="\n".join(page),
            color=PFO_GOLD
        )

        if index == 0:
            apply_branding(
                embed,
                banner_file=(VOTE_BANNER if is_open else VOTE_CLOSED_BANNER) if len(pages) == 1 else None,
                author_text="PFO • FIGHT VOTE"
            )

        if index == len(pages) - 1:
            if index:
                embed.set_image(url=asset_url(VOTE_BANNER if is_open else VOTE_CLOSED_BANNER))
            embed.timestamp = discord.utils.utcnow()
            embed.set_footer(
                text=f"{plural(len(bouts), 'fight')} • correct picks count towards the /leaderboard",
                icon_url=asset_url(LOGO_FILE)
            )

        embeds.append(embed)

    return embeds


def create_vote_embed(poll):
    """First embed only (kept for older code paths)."""
    return create_vote_embeds(poll)[0]


def picks_summary_lines(poll, user_id: int):

    picks = user_picks(poll["id"], user_id)
    lines = []

    for bout in get_poll_bouts(poll["id"]):

        pick = picks.get(bout["position"])
        names = {bout["fighter_a"]: (SIDE_A, bout["name_a"]), bout["fighter_b"]: (SIDE_B, bout["name_b"])}

        if pick is None:
            lines.append(f"`{bout['position']:>2}` ⬜ {bout['name_a']} vs {bout['name_b']}")
            continue

        side, name = names.get(pick, ("", "?"))
        result = ""

        if bout["resolved"]:
            result = " ✅" if bout["winner_id"] == pick else (" 🤝" if not bout["winner_id"] else " ❌")

        lines.append(f"`{bout['position']:>2}` {side} **{name}**{result}")

    return lines, len(picks)


def ballot_embed(poll, user_id: int, page: int, pages: int):

    lines, picked = picks_summary_lines(poll, user_id)
    total = len(lines)

    embed = discord.Embed(
        title=f"Your picks: {poll['event']}",
        description=(
            f"**{picked} of {total}** picked"
            + (" · all done ✅" if picked == total else "")
            + f"\n⏰ Closes <t:{poll['deadline']}:R>. Change any pick until then.\n\n"
            + "\n".join(lines)
        )[:4000],
        color=PFO_GOLD
    )

    embed.set_author(name="PFO • YOUR BALLOT", icon_url=asset_url(LOGO_FILE))

    if pages > 1:
        embed.set_footer(text=f"Page {page + 1} of {pages} • use ◀ ▶ for the other fights")

    return embed


class VoteBallotView(
    discord.ui.View
):
    """Someone's own ballot (only they see it): a dropdown per fight, a few per page."""

    def __init__(self, poll_id: int, user_id: int, page: int = 0):

        super().__init__(timeout=900)

        self.poll_id = poll_id
        self.user_id = user_id
        self.page = page
        self.rebuild()

    def pages(self):
        return max(1, (len(get_poll_bouts(self.poll_id)) + BALLOT_PAGE_SIZE - 1) // BALLOT_PAGE_SIZE)

    def rebuild(self):

        self.clear_items()

        bouts = get_poll_bouts(self.poll_id)
        picks = user_picks(self.poll_id, self.user_id)
        pages = self.pages()
        self.page = max(0, min(self.page, pages - 1))
        shown = bouts[self.page * BALLOT_PAGE_SIZE:(self.page + 1) * BALLOT_PAGE_SIZE]

        for row, bout in enumerate(shown):

            pick = picks.get(bout["position"])
            picked_name = (
                bout["name_a"] if pick == bout["fighter_a"]
                else bout["name_b"] if pick == bout["fighter_b"] else None
            )

            select = discord.ui.Select(
                placeholder=(
                    f"{bout['position']}. ✅ {picked_name} (change)" if picked_name
                    else f"{bout['position']}. {bout['name_a']} vs {bout['name_b']}: pick a winner"
                )[:150],
                options=[
                    discord.SelectOption(
                        label=bout["name_a"][:100], value=str(bout["fighter_a"]), emoji=SIDE_A,
                        description=f"to beat {bout['name_b']}"[:100], default=pick == bout["fighter_a"]
                    ),
                    discord.SelectOption(
                        label=bout["name_b"][:100], value=str(bout["fighter_b"]), emoji=SIDE_B,
                        description=f"to beat {bout['name_a']}"[:100], default=pick == bout["fighter_b"]
                    ),
                ],
                row=row
            )
            select.callback = self.make_pick(bout["position"])
            self.add_item(select)

        if pages > 1:
            previous = discord.ui.Button(label="◀", style=discord.ButtonStyle.grey,
                                         disabled=self.page == 0, row=4)
            previous.callback = self.previous_page
            following = discord.ui.Button(label="▶", style=discord.ButtonStyle.grey,
                                          disabled=self.page >= pages - 1, row=4)
            following.callback = self.next_page
            self.add_item(previous)
            self.add_item(following)

        done = discord.ui.Button(label="Done", emoji="✅", style=discord.ButtonStyle.green, row=4)
        done.callback = self.done
        self.add_item(done)

    def make_pick(self, position: int):

        async def callback(interaction):

            poll = get_poll(self.poll_id)

            if not poll_is_open(poll):
                await interaction.response.edit_message(
                    content="🔒 Voting for this card has closed.", embed=None, view=None
                )
                return

            values = interaction.data.get("values", [])

            if values:
                save_pick(self.poll_id, position, self.user_id, int(values[0]))

            # Jump to the next page once this one is all picked.
            bouts = get_poll_bouts(self.poll_id)
            picks = user_picks(self.poll_id, self.user_id)
            on_page = bouts[self.page * BALLOT_PAGE_SIZE:(self.page + 1) * BALLOT_PAGE_SIZE]

            if all(bout["position"] in picks for bout in on_page) and self.page < self.pages() - 1:
                self.page += 1

            await self.redraw(interaction)

        return callback

    async def redraw(self, interaction):
        self.rebuild()
        await interaction.response.edit_message(
            embed=ballot_embed(get_poll(self.poll_id), self.user_id, self.page, self.pages()),
            view=self
        )

    async def previous_page(self, interaction):
        self.page -= 1
        await self.redraw(interaction)

    async def next_page(self, interaction):
        self.page += 1
        await self.redraw(interaction)

    async def done(self, interaction):

        poll = get_poll(self.poll_id)
        lines, picked = picks_summary_lines(poll, self.user_id)

        await interaction.response.edit_message(
            content=(
                f"✅ **Picks saved** ({picked} of {len(lines)}). "
                "Press **Make your picks** again any time before the deadline to change them."
            ),
            embed=None,
            view=None
        )


class VoteView(
    discord.ui.View
):
    """The buttons under the public vote post. Works after restarts."""

    def __init__(self, poll_id: int):

        super().__init__(timeout=None)

        self.poll_id = poll_id

        ballot = discord.ui.Button(
            label="Make your picks", emoji="🗳️", style=discord.ButtonStyle.green,
            custom_id=f"pfo_vote_{poll_id}_ballot"
        )
        ballot.callback = self.open_ballot
        mine = discord.ui.Button(
            label="My picks", emoji="📋", style=discord.ButtonStyle.grey,
            custom_id=f"pfo_vote_{poll_id}_mine"
        )
        mine.callback = self.my_picks
        self.add_item(ballot)
        self.add_item(mine)

    async def open_ballot(self, interaction):

        poll = get_poll(self.poll_id)

        if not poll_is_open(poll):
            await interaction.response.send_message("🔒 Voting for this card has closed.", ephemeral=True)
            return

        view = VoteBallotView(self.poll_id, interaction.user.id)

        await interaction.response.send_message(
            embed=ballot_embed(poll, interaction.user.id, view.page, view.pages()),
            view=view,
            ephemeral=True
        )

    async def my_picks(self, interaction):

        poll = get_poll(self.poll_id)
        lines, picked = picks_summary_lines(poll, interaction.user.id)

        if not picked:
            await interaction.response.send_message(
                "You haven't picked anyone yet."
                + (" Press **Make your picks**." if poll_is_open(poll) else ""),
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            embed=discord.Embed(
                title=f"Your picks: {poll['event']}",
                description=f"**{picked} of {len(lines)}** picked\n\n" + "\n".join(lines)[:3900],
                color=PFO_GOLD
            ),
            ephemeral=True
        )


class LegacyVoteView(
    discord.ui.View
):
    """
    Keeps the buttons on votes posted before the update working.
    The first press swaps that post to the new layout.
    """

    def __init__(self, poll_id: int):

        super().__init__(timeout=None)

        self.poll_id = poll_id

        for bout in get_poll_bouts(poll_id)[:5]:
            for side, name in (("a", bout["name_a"]), ("b", bout["name_b"])):
                button = discord.ui.Button(
                    label=name[:80],
                    style=discord.ButtonStyle.blurple if side == "a" else discord.ButtonStyle.red,
                    custom_id=f"pfo_vote_{poll_id}_{bout['position']}_{side}",
                    row=bout["position"] - 1
                )
                button.callback = self.make_callback(bout["position"], side)
                self.add_item(button)

    def make_callback(self, position: int, side: str):

        async def callback(interaction):

            poll = get_poll(self.poll_id)

            if not poll_is_open(poll):
                await interaction.response.send_message("🔒 Voting for this card has closed.", ephemeral=True)
                return

            bout = vote_query(
                "SELECT * FROM vote_bouts WHERE poll_id = ? AND position = ?",
                (self.poll_id, position), one=True
            )
            pick = bout["fighter_a"] if side == "a" else bout["fighter_b"]
            save_pick(self.poll_id, position, interaction.user.id, pick)

            await interaction.response.send_message(
                f"✅ You picked **{bout['name_a'] if side == 'a' else bout['name_b']}**.",
                ephemeral=True
            )

            await refresh_vote_message(self.poll_id)

        return callback


async def refresh_vote_message(
    poll_id: int
):

    poll = get_poll(poll_id)

    if not poll or not poll["message_id"]:
        return

    try:
        channel = bot.get_channel(poll["channel_id"]) or await bot.fetch_channel(poll["channel_id"])
        message = await channel.fetch_message(poll["message_id"])

        if poll_is_open(poll):
            await message.edit(embeds=create_vote_embeds(poll), view=VoteView(poll_id))
        else:
            await message.edit(embeds=create_vote_embeds(poll), view=None)

    except Exception as error:
        print(f"Could not refresh vote {poll_id}: {error}")


def resolve_votes_for_fight(
    guild_id: int,
    fight
):
    """Marks main card vote fights decided by this result. Returns poll ids."""

    if not fight or not fight["winner_id"] or not fight["loser_id"]:
        return []

    pair = {fight["winner_id"], fight["loser_id"]}
    changed = []

    for bout in vote_query("""
        SELECT vote_bouts.* FROM vote_bouts
        JOIN vote_polls ON vote_polls.id = vote_bouts.poll_id
        WHERE vote_polls.guild_id = ? AND vote_bouts.resolved = 0
    """, (guild_id,)):

        if {bout["fighter_a"], bout["fighter_b"]} != pair:
            continue

        winner = None if fight["method"] in ("DRAW", "NC") else fight["winner_id"]

        vote_query(
            "UPDATE vote_bouts SET resolved = 1, winner_id = ?, result_fight_id = ? "
            "WHERE poll_id = ? AND position = ?",
            (winner, fight["id"], bout["poll_id"], bout["position"]), write=True
        )

        changed.append(bout["poll_id"])

    return changed


def unresolve_votes_for_fight(
    fight_id: int
):

    rows = vote_query(
        "SELECT DISTINCT poll_id FROM vote_bouts WHERE result_fight_id = ?", (fight_id,)
    )

    vote_query(
        "UPDATE vote_bouts SET resolved = 0, winner_id = NULL, result_fight_id = NULL "
        "WHERE result_fight_id = ?",
        (fight_id,), write=True
    )

    return [row["poll_id"] for row in rows]


async def close_due_polls():

    now = int(time.time())

    # Update the voter count on posts that had new picks.
    for poll_id in list(_polls_to_refresh):
        _polls_to_refresh.discard(poll_id)
        poll = get_poll(poll_id)
        if poll_is_open(poll):
            await refresh_vote_message(poll_id)

    for poll in vote_query(
        "SELECT * FROM vote_polls WHERE status = 'open' AND deadline <= ?", (now,)
    ):
        vote_query("UPDATE vote_polls SET status = 'closed' WHERE id = ?", (poll["id"],), write=True)
        await refresh_vote_message(poll["id"])


@tasks.loop(minutes=1)
async def vote_closer():
    try:
        await close_due_polls()
    except Exception as error:
        print(f"Vote closer error: {error}")

    # Interviews: catch up after restarts, expire old invitations.
    try:
        await check_interviews()
    except Exception as error:
        print(f"Interview check error: {error}")

    # Reminder DMs for the latest Live Card / Fight Night.
    try:
        await check_card_reminders()
    except Exception as error:
        print(f"Reminder error: {error}")

    # Betting: lock at closing time, refresh live odds.
    try:
        await sportsbook_tick()
    except Exception as error:
        print(f"Sportsbook error: {error}")

    # Sign-ups with a closing time.
    try:
        await close_due_signups()
    except Exception as error:
        print(f"Sign-up closer error: {error}")

    # Suspensions ending.
    await run_discipline_checks()


@vote_closer.before_loop
async def before_vote_closer():
    await bot.wait_until_ready()


def restore_vote_buttons():

    for poll in vote_query("SELECT id FROM vote_polls WHERE status = 'open'"):
        try:
            bot.add_view(VoteView(poll["id"]))
            bot.add_view(LegacyVoteView(poll["id"]))
        except Exception as error:
            print(f"Could not restore vote {poll['id']}: {error}")


@bot.tree.command(
    name="mainvote",
    description="Post a vote where everyone picks the winners of the fights (any number of fights)."
)
@app_commands.describe(
    event="Event name, e.g. PFO Fight Night 16",
    deadline="When voting closes, in UK time, e.g. 24/10/2026 19:00",
    fighters="Fights in order: @Joe @Dan @Tom @Ben = Joe v Dan, Tom v Ben. Empty = last saved card"
)
async def mainvote(
    interaction: discord.Interaction,
    event: str,
    deadline: str,
    fighters: str = None
):

    if not is_staff(interaction):
        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can start votes.",
            ephemeral=True
        )
        return

    moment = parse_event_datetime(deadline)

    if not moment:
        await interaction.response.send_message(
            "❌ Write the deadline like **24/10/2026 19:00** or **24/10 7pm** (UK time).",
            ephemeral=True
        )
        return

    if moment <= datetime.now(timezone.utc):
        await interaction.response.send_message(
            "❌ That deadline has already passed.",
            ephemeral=True
        )
        return

    if fighters and MENTION_PATTERN.search(fighters):
        pairs = parse_fight_pairs(fighters)
    else:
        card = get_latest_fight_card(interaction.guild.id)
        pairs = [
            (bout["fighter_a"], bout["fighter_b"])
            for bout in get_fight_card_bouts(card["id"])
        ] if card else []

        if not pairs:
            await interaction.response.send_message(
                "❌ @mention the main card in fight order, e.g. `@Joe @Dan @Tom @Ben`, "
                "or save a card with `/createfights` first.",
                ephemeral=True
            )
            return

    if pairs[-1][1] is None:
        await interaction.response.send_message(
            f"❌ Odd number of fighters: <@{pairs[-1][0]}> has no opponent.",
            ephemeral=True
        )
        return

    if len(pairs) > MAX_VOTE_FIGHTS:
        await interaction.response.send_message(
            f"❌ That's {len(pairs)} fights. A vote can have up to {MAX_VOTE_FIGHTS}.",
            ephemeral=True
        )
        return

    def name(user_id):
        member = interaction.guild.get_member(user_id)
        return name_without_record(member.display_name) if member else f"User {user_id}"

    poll_id = vote_query(
        "INSERT INTO vote_polls (guild_id, event, deadline, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
        (interaction.guild.id, event.strip(), int(moment.timestamp()), interaction.user.id, now_utc()),
        write=True
    )

    for position, (a, b) in enumerate(pairs, start=1):
        vote_query(
            "INSERT INTO vote_bouts (poll_id, position, fighter_a, fighter_b, name_a, name_b, weight) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (poll_id, position, a, b, name(a), name(b), auto_weight(interaction.guild.id, a, b)), write=True
        )

    await interaction.response.send_message(
        embeds=create_vote_embeds(get_poll(poll_id)),
        view=VoteView(poll_id)
    )

    message = await interaction.original_response()

    vote_query(
        "UPDATE vote_polls SET channel_id = ?, message_id = ? WHERE id = ?",
        (message.channel.id, message.id, poll_id), write=True
    )


# ============================================================
# LEADERBOARDS
# ============================================================

LEADERBOARD_CHOICES = [
    app_commands.Choice(name="Most wins", value="wins"),
    app_commands.Choice(name="Current win streak", value="streak"),
    app_commands.Choice(name="KO/TKO wins", value="ko"),
    app_commands.Choice(name="Submission wins", value="sub"),
    app_commands.Choice(name="Finish rate", value="finish"),
    app_commands.Choice(name="Title fight wins", value="titles"),
    app_commands.Choice(name="Voting: correct picks", value="votes"),
    app_commands.Choice(name="Suspended fighters (countdown)", value="suspended"),
    app_commands.Choice(name="PFO Coins: richest", value="coins"),
]

LEADERBOARD_TITLES = {
    "wins": "🏆 Most Wins",
    "streak": "🔥 Current Win Streak",
    "ko": "💥 KO/TKO Wins",
    "sub": "🔒 Submission Wins",
    "finish": "⚡ Finish Rate",
    "titles": "👑 Title Fight Wins",
    "votes": "🗳️ Voting: Correct Picks",
    "suspended": "⛔ Suspended Fighters",
    "coins": "🪙 Richest in PFO",
}


def fighter_leaderboard(
    guild: discord.Guild,
    category: str
):
    """[(member, value, display_text)] best first."""

    rows = []

    for member in guild.members:

        if member.bot or not has_record_data(guild.id, member.id):
            continue

        stats = get_fighter_stats(guild.id, member.id)

        if category == "wins":
            value = stats["wins"]
            text = f"{value} win{'s' if value != 1 else ''} ({stats_record(stats)})"
        elif category == "streak":
            streak = stats["streak"]
            if not streak.startswith("W"):
                continue
            value = int(streak[1:])
            text = f"{value} win{'s' if value != 1 else ''} in a row"
        elif category == "ko":
            value = stats["wins_by"].get("KO/TKO", 0)
            text = f"{value} KO/TKO win{'s' if value != 1 else ''}"
        elif category == "sub":
            value = stats["wins_by"].get("SUB", 0)
            text = f"{value} submission win{'s' if value != 1 else ''}"
        elif category == "finish":
            if stats["logged_wins"] < 3 or stats["finish_rate"] is None:
                continue
            value = stats["finish_rate"]
            text = f"{value}% of {stats['logged_wins']} wins"
        else:
            value = stats["title_wins"]
            text = f"{value} title fight win{'s' if value != 1 else ''}"

        if value <= 0:
            continue

        rows.append((member, value, stats["wins"], text))

    rows.sort(key=lambda row: (row[1], row[2]), reverse=True)

    return [(member, value, text) for member, value, _, text in rows]


def voting_leaderboard(
    guild: discord.Guild
):

    rows = []

    for row in vote_query("""
        SELECT votes.user_id,
               SUM(CASE WHEN votes.pick = vote_bouts.winner_id THEN 1 ELSE 0 END) AS correct,
               COUNT(*) AS total
        FROM votes
        JOIN vote_bouts ON vote_bouts.poll_id = votes.poll_id
                       AND vote_bouts.position = votes.position
        JOIN vote_polls ON vote_polls.id = votes.poll_id
        WHERE vote_polls.guild_id = ?
          AND vote_bouts.resolved = 1
          AND vote_bouts.winner_id IS NOT NULL
        GROUP BY votes.user_id
    """, (guild.id,)):

        member = guild.get_member(row["user_id"])

        if not member or not row["correct"]:
            continue

        accuracy = round(100 * row["correct"] / row["total"])
        rows.append((member, row["correct"], accuracy,
                     f"{row['correct']} correct of {row['total']} ({accuracy}%)"))

    rows.sort(key=lambda row: (row[1], row[2]), reverse=True)

    return [(member, value, text) for member, value, _, text in rows]


@bot.tree.command(
    name="leaderboard",
    description="Post a top 10 leaderboard for fights or voting."
)
@app_commands.describe(
    category="What to rank by"
)
@app_commands.choices(
    category=LEADERBOARD_CHOICES
)
async def leaderboard(
    interaction: discord.Interaction,
    category: app_commands.Choice[str]
):

    if not is_staff(interaction):
        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can post leaderboards.",
            ephemeral=True
        )
        return

    await interaction.response.defer()

    if category.value == "suspended":

        lines = suspended_board_lines(interaction.guild) or ["✅ Nobody is suspended right now."]

        embed = discord.Embed(
            title=LEADERBOARD_TITLES["suspended"],
            description="\n".join(lines)[:4000],
            color=PFO_GOLD,
            timestamp=discord.utils.utcnow()
        )

        apply_branding(
            embed,
            banner_file=SUSPENDED_BANNER,
            author_text="PFO • SUSPENSIONS"
        )

        embed.set_footer(
            text=f"{MAX_WARNINGS} missed fights = {SUSPENSION_DAYS}-day suspension • countdowns update live",
            icon_url=asset_url(LOGO_FILE)
        )

        await interaction.followup.send(embed=embed)
        return

    if category.value == "coins":
        rows = []
        for row in discipline_query(
            "SELECT user_id, balance FROM coin_wallets WHERE guild_id = ? ORDER BY balance DESC LIMIT 25",
            (interaction.guild.id,)
        ):
            member = interaction.guild.get_member(row["user_id"])
            if member:
                rows.append((member, row["balance"], f"🪙 {row['balance']:,}"))
    elif category.value == "votes":
        rows = voting_leaderboard(interaction.guild)
    else:
        rows = fighter_leaderboard(interaction.guild, category.value)

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = []
    place = 0
    previous = None

    for index, (member, value, text) in enumerate(rows[:10], start=1):
        if value != previous:
            place = index
            previous = value
        badge = medals.get(place, f"`{place:>2}.`")
        lines.append(f"{badge} **{name_without_record(member.display_name)}**  ·  {text}")

    if not lines:
        lines = ["Nobody on this leaderboard yet."]

    embed = discord.Embed(
        title=LEADERBOARD_TITLES[category.value],
        description="\n".join(lines),
        color=PFO_GOLD,
        timestamp=discord.utils.utcnow()
    )

    apply_branding(
        embed,
        banner_file=LEADERBOARD_BANNER,
        author_text="PFO • LEADERBOARD"
    )

    footers = {
        "coins": "PFO Coins are points only • /coins to see yours",
        "finish": "Finish rate = KO/TKO + submission wins • minimum 3 logged wins",
        "votes": "Correct picks from /mainvote once results are logged",
    }

    embed.set_footer(
        text=footers.get(category.value, "From the bot's fight records (same as /profile)"),
        icon_url=asset_url(LOGO_FILE)
    )

    await interaction.followup.send(embed=embed)


# ------------------------------------------------------------
# IMPORTING PAST FIGHTS
# ------------------------------------------------------------

def template_csv():

    return (
        "date,event,winner,loser,method,round,division,title_fight\n"
        "2026-03-14,Fight Night 1,razorreyes,leonward,KO/TKO,1,Lightweight,no\n"
        "2026-03-14,Fight Night 1,dimapetrenko,jordanprice,SUB,2,Lightweight,no\n"
        "2026-03-21,Live Card 1,razorreyes,dimapetrenko,DEC,,Lightweight,yes\n"
    )


def parse_method(
    text: str
):

    key = normalise_name(text)

    for method, aliases in METHOD_ALIASES.items():
        if key in aliases:
            return method

    return None


def parse_division(
    text: str
):

    key = role_key(text)

    if not key:
        return None, True

    for weight, short in WEIGHTS.items():
        if weight == P4P_WEIGHT:
            continue

        if key in (role_key(weight), role_key(short)):
            return weight, True

    return None, False


def parse_date(
    text: str
):

    text = text.strip()

    if not text:
        return None, True

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat(), True
        except ValueError:
            pass

    return None, False


def parse_yes_no(
    text: str
):

    return normalise_name(text) in ("yes", "y", "true", "1", "title", "x")


def build_member_lookup(
    guild: discord.Guild
):

    lookup = {}

    for member in guild.members:

        if member.bot:
            continue

        names = {
            member.name,
            member.display_name,
            getattr(member, "global_name", None),
        }

        for name in names:
            if not name:
                continue

            # Also match the name without a record or tags,
            # e.g. "Razor Reyes [C] (9-1-0)" matches "Razor Reyes".
            plain = re.sub(r"\s*\([^()]*\)\s*$", "", name)
            plain = re.sub(r"\s*\[[^\[\]]*\]", "", plain)

            for variant in {name, plain}:
                if variant.strip():
                    lookup.setdefault(
                        normalise_name(variant),
                        set()
                    ).add(member)

    return lookup


def resolve_fighter(
    guild: discord.Guild,
    lookup: dict,
    text: str
):
    """
    Returns (user_id, display_name, status)
      status: "member"    matched a member
              "id_only"   a user ID for someone not in the server
              "name_only" no member found, saved by name
              "ambiguous" several members match that name
    """

    text = text.strip()

    mention = re.fullmatch(r"<@!?(\d+)>", text)
    digits = mention.group(1) if mention else (
        text if text.isdigit() and 15 <= len(text) <= 20 else None
    )

    if digits:
        user_id = int(digits)
        member = guild.get_member(user_id)

        if member:
            return member.id, member.display_name, "member"

        return user_id, "Unknown Fighter", "id_only"

    text = text.lstrip("@").strip()

    plain = re.sub(r"\s*\([^()]*\)\s*$", "", text)
    plain = re.sub(r"\s*\[[^\[\]]*\]", "", plain).strip()

    matches = lookup.get(
        normalise_name(text),
        set()
    ) or lookup.get(
        normalise_name(plain),
        set()
    )

    if len(matches) == 1:
        member = next(iter(matches))
        return member.id, member.display_name, "member"

    if len(matches) > 1:
        return None, text, "ambiguous"

    return None, text, "name_only"


def parse_fight_csv(
    guild: discord.Guild,
    raw: bytes
):
    """
    Checks every row and returns:
      ready      fights that can be imported
      problems   (row number, reason) for rows that need fixing
      name_only  names that didn't match a member (saved by name)
      skipped    rows already in the database
    """

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))

    if not reader.fieldnames:
        return [], [(0, "The file is empty.")], set(), 0

    headers = {
        normalise_name(h).replace(" ", "_"): h
        for h in reader.fieldnames if h
    }

    missing = [
        col for col in ("winner", "loser", "method")
        if col not in headers
    ]

    if missing:
        return [], [(1, "Missing column(s): " + ", ".join(missing)
                     + ". Use /importtemplate for the right layout.")], set(), 0

    def cell(row, col):
        header = headers.get(col)
        value = row.get(header) if header else None
        return (value or "").strip()

    lookup = build_member_lookup(guild)
    existing = get_existing_fight_keys(guild.id)

    ready = []
    problems = []
    name_only = set()
    skipped = 0
    seen = set()

    for number, row in enumerate(reader, start=2):

        if number - 1 > MAX_IMPORT_ROWS:
            problems.append((number, f"Only the first {MAX_IMPORT_ROWS} rows are read."))
            break

        if not any((value or "").strip() for value in row.values() if isinstance(value, str)):
            continue

        winner_text = cell(row, "winner")
        loser_text = cell(row, "loser")

        if not winner_text or not loser_text:
            problems.append((number, "winner and loser are both needed"))
            continue

        method = parse_method(cell(row, "method"))

        if not method:
            problems.append((
                number,
                f"method `{cell(row, 'method') or 'empty'}` isn't recognised "
                "(use KO/TKO, SUB, DEC, DQ, DRAW or NC)"
            ))
            continue

        round_text = cell(row, "round")
        fight_round = None

        if round_text:
            if round_text.isdigit() and 1 <= int(round_text) <= 5:
                fight_round = int(round_text)
            else:
                problems.append((number, f"round `{round_text}` should be 1 to 5 or empty"))
                continue

        division, ok = parse_division(cell(row, "division"))

        if not ok:
            problems.append((number, f"division `{cell(row, 'division')}` isn't a PFO weight class"))
            continue

        date, ok = parse_date(cell(row, "date"))

        if not ok:
            problems.append((number, f"date `{cell(row, 'date')}` should look like 2026-03-14 or 14/03/2026"))
            continue

        bad = False
        resolved = []

        for text in (winner_text, loser_text):
            user_id, name, status = resolve_fighter(guild, lookup, text)

            if status == "ambiguous":
                problems.append((
                    number,
                    f"`{text}` matches more than one member; use their username or user ID"
                ))
                bad = True
                break

            if status == "name_only":
                name_only.add(name)

            resolved.append((user_id, name))

        if bad:
            continue

        (winner_id, winner_name), (loser_id, loser_name) = resolved

        if (winner_id and winner_id == loser_id) or (
            not winner_id and normalise_name(winner_name) == normalise_name(loser_name)
        ):
            problems.append((number, "winner and loser are the same fighter"))
            continue

        key = fight_key(date, winner_id, winner_name, loser_id, loser_name, method)

        if key in existing or key in seen:
            skipped += 1
            continue

        seen.add(key)

        event = cell(row, "event")

        ready.append({
            "date": date,
            "event": event or None,
            "winner_id": winner_id,
            "winner_name": winner_name,
            "loser_id": loser_id,
            "loser_name": loser_name,
            "method": method,
            "round": fight_round,
            "division": division,
            "title_fight": parse_yes_no(cell(row, "title_fight")),
        })

    return ready, problems, name_only, skipped


def create_import_summary_embed(
    ready: list,
    problems: list,
    name_only: set,
    skipped: int,
    found_text: str = None,
    author_text: str = "PFO • IMPORT FIGHT HISTORY"
):

    fighters = set()

    for fight in ready:
        fighters.add(fight["winner_id"] or normalise_name(fight["winner_name"]))
        fighters.add(fight["loser_id"] or normalise_name(fight["loser_name"]))

    lines = [
        found_text or f"Found **{len(ready) + len(problems) + skipped} fight rows**.",
        "",
        f"✅ **{len(ready)} fights** for **{len(fighters)} fighters** are ready to import",
    ]

    if skipped:
        lines.append(f"⏭️ **{skipped}** already saved, so they'll be skipped")

    if name_only:
        names = sorted(name_only)
        shown = ", ".join(f"`{n}`" for n in names[:8])
        more = f" and {len(names) - 8} more" if len(names) > 8 else ""
        lines += [
            "",
            f"👤 **{len(names)} name(s) didn't match a member** and will be "
            f"saved by name only (fine for people who've left; "
            f"check for typos): {shown}{more}",
        ]

    if problems:
        lines += ["", f"⚠️ **{len(problems)}** need fixing and won't be imported:"]

        for label, reason in problems[:10]:
            if isinstance(label, int):
                label = f"Row {label}"
            lines.append(f"• {label}: {reason}")

        if len(problems) > 10:
            lines.append(f"• …and {len(problems) - 10} more")

    lines += ["", "Nothing is saved until you press **Import**."]

    embed = discord.Embed(
        title="Check before importing",
        description="\n".join(lines)[:4000],
        color=PFO_GOLD
    )

    apply_branding(
        embed,
        author_text=author_text
    )

    embed.set_footer(
        text="This expires in 10 minutes",
        icon_url=asset_url(LOGO_FILE)
    )

    return embed


class ImportConfirmView(
    discord.ui.View
):

    def __init__(
        self,
        author_id: int,
        guild_id: int,
        fights: list
    ):

        super().__init__(
            timeout=600
        )

        self.author_id = author_id
        self.guild_id = guild_id
        self.fights = fights

        confirm = discord.ui.Button(
            label=f"Import {len(fights)} fight{'s' if len(fights) != 1 else ''}",
            style=discord.ButtonStyle.green,
            emoji="✅",
            disabled=not fights
        )

        confirm.callback = self.confirm

        cancel = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.grey
        )

        cancel.callback = self.cancel

        self.add_item(confirm)
        self.add_item(cancel)

    async def interaction_check(
        self,
        interaction: discord.Interaction
    ):

        if interaction.user.id != self.author_id:

            await interaction.response.send_message(
                "❌ Only the person who started this import can confirm it.",
                ephemeral=True
            )

            return False

        return True

    async def confirm(
        self,
        interaction: discord.Interaction
    ):

        db = get_db()
        cursor = db.cursor()

        new_ids = []

        try:
            for fight in self.fights:
                new_ids.append(add_fight(
                    self.guild_id,
                    fight,
                    "import",
                    self.author_id,
                    cursor=cursor
                ))

            db.commit()

        except Exception as error:

            db.rollback()
            db.close()

            print(f"Import error: {error}")

            await interaction.response.edit_message(
                content="❌ The import failed and nothing was saved. Please try again.",
                embed=None,
                view=None
            )

            return

        db.close()

        # Imported results also settle any main card votes.
        polls_changed = set()

        for fight_id in new_ids:
            polls_changed.update(
                resolve_votes_for_fight(self.guild_id, get_fight(self.guild_id, fight_id))
            )
            # Old fights settle bets, but don't pay fighters.
            if interaction.guild:
                await coins_after_result(interaction.guild, get_fight(self.guild_id, fight_id), pay_fighters=False)

        fighters = {
            f["winner_id"] or normalise_name(f["winner_name"]) for f in self.fights
        } | {
            f["loser_id"] or normalise_name(f["loser_name"]) for f in self.fights
        }

        self.stop()

        await interaction.response.edit_message(
            content=(
                f"✅ **Imported {len(self.fights)} fights.** "
                f"Records for {len(fighters)} fighters are updated; "
                f"check anyone with /profile."
            ),
            embed=None,
            view=None
        )

        for poll_id in polls_changed:
            await refresh_vote_message(poll_id)

    async def cancel(
        self,
        interaction: discord.Interaction
    ):

        self.stop()

        await interaction.response.edit_message(
            content="Import cancelled. Nothing was saved.",
            embed=None,
            view=None
        )


# ------------------------------------------------------------
# Google Sheets support
#
# Staff paste the sheet's link into /importfights. The sheet
# must be shared as "Anyone with the link: Viewer" so the bot
# can read it (it never needs edit access).
# ------------------------------------------------------------

GOOGLE_SHEET_ID = re.compile(
    r"docs\.google\.com/spreadsheets/d/([a-zA-Z0-9_-]{20,})"
)

GOOGLE_SHEET_TAB = re.compile(
    r"[#&?]gid=(\d+)"
)

SHEET_HEADER_ROW = (
    "date | event | winner | loser | method | round | division | title_fight"
)


class SheetReadError(Exception):
    pass


async def download_google_sheet(
    link: str
):
    """
    Downloads one tab of a Google Sheet as CSV.
    Only docs.google.com links are accepted.
    """

    import aiohttp  # installed with discord.py

    match = GOOGLE_SHEET_ID.search(link)

    if not match:
        raise SheetReadError(
            "That doesn't look like a Google Sheets link. Copy it from "
            "the address bar while the sheet is open; it starts with "
            "`https://docs.google.com/spreadsheets/d/`."
        )

    sheet_id = match.group(1)
    tab = GOOGLE_SHEET_TAB.search(link)
    tab_id = tab.group(1) if tab else "0"

    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/export?format=csv&gid={tab_id}"
    )

    not_shared = SheetReadError(
        "I couldn't open that sheet. In Google Sheets press **Share**, "
        "set **General access** to **Anyone with the link** (Viewer), "
        "then try again."
    )

    try:
        timeout = aiohttp.ClientTimeout(total=20)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:

                if response.status in (401, 403, 404):
                    raise not_shared

                if response.status != 200:
                    raise SheetReadError(
                        f"Google Sheets returned an error ({response.status}). "
                        "Please try again in a minute."
                    )

                content_type = response.headers.get("Content-Type", "")

                # A private sheet sends back a Google sign-in page
                # instead of the data.
                if "text/html" in content_type:
                    raise not_shared

                raw = await response.content.read(2_000_001)

    except SheetReadError:
        raise

    except Exception as error:
        print(f"Google Sheets download error: {error}")
        raise SheetReadError(
            "I couldn't reach Google Sheets. Please try again in a minute."
        )

    if len(raw) > 2_000_000:
        raise SheetReadError("That sheet is too big (2 MB max).")

    return raw


@bot.tree.command(
    name="importtemplate",
    description="How to set up a Google Sheet (or CSV file) for importing past fights."
)
async def importtemplate(
    interaction: discord.Interaction
):

    if not is_staff(interaction):

        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can import fights.",
            ephemeral=True
        )

        return

    await interaction.response.send_message(
        (
            "📄 **Importing past fights with Google Sheets**\n\n"
            "**1.** Make a new Google Sheet and put these headings in row 1, "
            "one per column:\n"
            f"```{SHEET_HEADER_ROW}```"
            "Or open the attached file in Google Sheets "
            "(*File → Import → Upload*), which has the headings and 3 examples.\n"
            "**2.** Add one row per fight.\n"
            "**3.** Press **Share** and set **General access** to "
            "**Anyone with the link** (Viewer).\n"
            "**4.** Copy the link and use `/importfights sheet_link:`. "
            "The bot reads the tab that's open when you copy the link.\n\n"
            "**Columns**\n"
            "• **winner / loser:** Discord username works best (or user ID). "
            "Names of people who've left are saved by name only.\n"
            "• **method:** KO/TKO, SUB, DEC, DQ, DRAW or NC. "
            "For DRAW or NC put either fighter as winner.\n"
            "• **round:** 1-5, or empty for decisions.\n"
            "• **date:** 2026-03-14 or 14/03/2026 (can be empty).\n"
            "• **division:** e.g. Lightweight or LW (can be empty).\n"
            "• **title_fight:** yes or no.\n\n"
            "You can keep adding fights to the same sheet and import it again; "
            "fights already saved are skipped, never doubled."
        ),
        file=discord.File(
            io.BytesIO(template_csv().encode("utf-8")),
            filename="pfo_fight_history_template.csv"
        ),
        ephemeral=True
    )


@bot.tree.command(
    name="importfights",
    description="Import past fights from a Google Sheet (or a CSV file)."
)
@app_commands.describe(
    sheet_link="Link to the Google Sheet (shared as 'Anyone with the link')",
    file="Or upload a CSV file instead"
)
async def importfights(
    interaction: discord.Interaction,
    sheet_link: str = None,
    file: discord.Attachment = None
):

    if not is_staff(interaction):

        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can import fights.",
            ephemeral=True
        )

        return

    if not sheet_link and not file:

        await interaction.response.send_message(
            "❌ Paste your Google Sheet link into **sheet_link**. "
            "Use `/importtemplate` to see how to set the sheet up.",
            ephemeral=True
        )

        return

    if file and not sheet_link:

        if not file.filename.lower().endswith((".csv", ".txt")):

            await interaction.response.send_message(
                "❌ Please upload a **.csv** file, or use **sheet_link** "
                "with a Google Sheets link instead.",
                ephemeral=True
            )

            return

        if file.size > 2_000_000:

            await interaction.response.send_message(
                "❌ That file is too big (2 MB max).",
                ephemeral=True
            )

            return

    await interaction.response.defer(
        ephemeral=True
    )

    try:

        if sheet_link:
            raw = await download_google_sheet(
                sheet_link.strip()
            )
        else:
            raw = await file.read()

        ready, problems, name_only, skipped = parse_fight_csv(
            interaction.guild,
            raw
        )

        await fill_departed_names(ready)

        await interaction.followup.send(
            embed=create_import_summary_embed(
                ready,
                problems,
                name_only,
                skipped
            ),
            view=ImportConfirmView(
                interaction.user.id,
                interaction.guild.id,
                ready
            ),
            ephemeral=True
        )

    except SheetReadError as error:

        await interaction.followup.send(
            f"❌ {error}",
            ephemeral=True
        )

    except Exception as error:

        print(
            f"Import read error: {error}"
        )

        await interaction.followup.send(
            "❌ Couldn't read that sheet. Make sure row 1 has the headings "
            "from `/importtemplate`.",
            ephemeral=True
        )


# ============================================================
# /SCANRESULTS
#
# Reads typed results from a channel, e.g.
#   @A finishes @B in RD3
#   @A submits @B in RD3 via Guillotine
#   @A Wins Against @B In RD2 AND STILL 🏆
# and imports them like a spreadsheet would (check first, then
# press Import). Nothing in the channel is changed.
# ============================================================

FIGHTER_TOKEN = r"<@!?\d+>|[^\n<>]+?"

RESULT_VERBS = {
    # verb pattern -> what it tells us
    r"submits|submitted|subs|subbed|taps|tapped": "SUB",
    r"knocks out|knocked out|kos|ko'?d|ko'?s|tkos|tko'?d|knocks|knocked": "KO/TKO",
    r"finishes|finished|stops|stopped": "FINISH",
    r"outpoints|outpointed|decisions|decisioned": "DEC",
    r"wins against|won against|wins vs\.?|won vs\.?|beats|beat|defeats|defeated|def\.?|wins over|won over": "WIN",
    r"draws with|drew with|draw with|draws against|drew against": "DRAW",
    r"no contest with|no contest against|nc with|nc against": "NC",
}

RESULT_LINE = re.compile(
    rf"^\s*(?P<a>{FIGHTER_TOKEN})\s+"
    rf"(?P<verb>{'|'.join(RESULT_VERBS)})\s+"
    rf"(?P<tail>.+)$",
    re.IGNORECASE
)

# Where a typed (non-@) opponent's name ends.
NAME_END = re.compile(
    r"\s+(?:in|at|via|by|with)\s+|\s+(?:rd|round|rnd)\s*\.?\s*\d|\s+r\d\b|\s*[-–,!]\s|\s*\*\*|\s*[🥊🏆🔥]",
    re.IGNORECASE
)

ROUND_PATTERN = re.compile(
    r"\b(?:rd|round|rnd|r)\s*\.?\s*([1-5])\b",
    re.IGNORECASE
)

SUB_WORDS = [
    "submission", "sub", "guillotine", "choke", "rnc", "rear naked",
    "rear-naked", "armbar", "arm bar", "triangle", "kimura", "americana",
    "heel hook", "kneebar", "knee bar", "darce", "d'arce", "anaconda",
    "ezekiel", "arm triangle", "omoplata", "ankle lock", "tap",
]

KO_WORDS = [
    "ko", "tko", "knockout", "punch", "punches", "kick", "head kick",
    "body kick", "elbow", "knee", "ground and pound", "gnp", "slam",
    "spinning", "stoppage", "doctor", "uppercut", "hook", "jab", "cross",
    "body shot", "liver", "flying",
]

DEC_WORDS = [
    "decision", "dec", "ud", "sd", "md", "points", "unanimous",
    "split", "majority", "judges", "scorecards",
]


def has_word(
    text: str,
    words: list
):

    text = text.casefold()

    return any(
        re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", text)
        for word in words
    )


def method_from_line(
    verb_kind: str,
    rest: str
):
    """
    Works out the method from the verb and anything after it.
      submits                         -> SUB
      finishes ... via Guillotine     -> SUB
      finishes / knocks out           -> KO/TKO
      wins against ... via decision   -> DEC
      wins against ... in RD2         -> KO/TKO (finished in a round)
      wins against (no round)         -> DEC
    """

    if verb_kind in ("SUB", "DEC", "DRAW", "NC"):
        return verb_kind

    via = re.search(r"\b(?:via|by|with)\b(.*)", rest, re.IGNORECASE)
    detail = via.group(1) if via else rest

    if has_word(detail, SUB_WORDS):
        return "SUB"

    if has_word(detail, DEC_WORDS):
        return "DEC"

    if verb_kind in ("KO/TKO", "FINISH"):
        return "KO/TKO"

    if has_word(detail, KO_WORDS) or ROUND_PATTERN.search(rest):
        return "KO/TKO"

    return "DEC"


def clean_fighter_text(
    text: str
):

    return text.strip().strip("*_~|").strip()


def parse_result_line(
    line: str
):
    """
    Returns (winner_text, loser_text, method, round, title_fight)
    or None if the line isn't a result.
    """

    match = RESULT_LINE.match(line.strip())

    if not match:
        return None

    verb = match.group("verb").casefold()
    verb_kind = next(
        kind for pattern, kind in RESULT_VERBS.items()
        if re.fullmatch(pattern, verb, re.IGNORECASE)
    )

    tail = match.group("tail")

    # "@A finishes @B in RD3": the opponent is the mention.
    # For a typed name, it ends where the round / via part starts.
    mention = re.match(r"\s*(<@!?\d+>)", tail)

    if mention:
        b_text = mention.group(1)
        rest = tail[mention.end():]
    else:
        end = NAME_END.search(tail)
        b_text = tail[:end.start()] if end else tail
        rest = tail[end.start():] if end else ""

    if not b_text.strip():
        return None

    method = method_from_line(verb_kind, rest)

    fight_round = None

    if method not in ("DEC", "DRAW", "NC"):
        found = ROUND_PATTERN.search(rest)
        if found:
            fight_round = int(found.group(1))

    title_fight = bool(re.search(
        r"\band (?:still|new)\b|\btitle\b|\bbelt\b|\bchampion",
        rest,
        re.IGNORECASE
    ))

    return (
        clean_fighter_text(match.group("a")),
        clean_fighter_text(b_text),
        method,
        fight_round,
        title_fight,
    )


def looks_like_result(
    line: str
):
    """A line we couldn't read but that probably is a result."""

    mentions = len(re.findall(r"<@!?\d+>", line))

    return mentions >= 2 and bool(
        ROUND_PATTERN.search(line)
        or re.search(r"\bvia\b|\bwins?\b|\bdef\b|\bko\b|\bsub", line, re.IGNORECASE)
    )


async def scan_channel_results(
    guild: discord.Guild,
    channel,
    since=None
):

    lookup = build_member_lookup(guild)
    existing = get_existing_fight_keys(guild.id)

    ready = []
    problems = []
    name_only = set()
    skipped = 0
    seen = set()
    messages_read = 0

    async for message in channel.history(
        limit=None,
        oldest_first=True,
        after=since
    ):

        if message.author.bot or not message.content:
            continue

        messages_read += 1
        date = message.created_at.date().isoformat()

        for line in message.content.splitlines():

            if not line.strip():
                continue

            parsed = parse_result_line(line)

            if not parsed:
                if looks_like_result(line):
                    problems.append((
                        f"[Message from {message.created_at:%d/%m/%Y}]({message.jump_url})",
                        "couldn't tell who won; add it with /result"
                    ))
                continue

            a_text, b_text, method, fight_round, title_fight = parsed

            resolved = []
            bad = False

            for text in (a_text, b_text):
                user_id, name, status = resolve_fighter(guild, lookup, text)

                if status == "ambiguous":
                    problems.append((
                        f"[Message from {message.created_at:%d/%m/%Y}]({message.jump_url})",
                        f"`{text}` matches more than one member; add it with /result"
                    ))
                    bad = True
                    break

                if status == "name_only":
                    name_only.add(name)

                resolved.append((user_id, name))

            if bad:
                continue

            (winner_id, winner_name), (loser_id, loser_name) = resolved

            # A line with no @mentions where neither name is a member
            # is probably just chat ("he finishes everyone in rd1").
            if not winner_id and not loser_id:
                name_only.discard(winner_name)
                name_only.discard(loser_name)
                continue

            if winner_id and winner_id == loser_id:
                continue

            key = fight_key(date, winner_id, winner_name, loser_id, loser_name, method)

            if key in existing or key in seen:
                skipped += 1
                continue

            seen.add(key)

            ready.append({
                "date": date,
                "event": None,
                "winner_id": winner_id,
                "winner_name": winner_name,
                "loser_id": loser_id,
                "loser_name": loser_name,
                "method": method,
                "round": fight_round,
                "division": None,
                "title_fight": title_fight,
            })

    return ready, problems, name_only, skipped, messages_read


@bot.tree.command(
    name="scanresults",
    description="Read typed results from a channel and import them as fights."
)
@app_commands.describe(
    channel="The channel where results are posted",
    since="Only read messages from this date on, e.g. 01/09/2026 (optional)"
)
async def scanresults(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    since: str = None
):

    if not is_staff(interaction):

        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can import fights.",
            ephemeral=True
        )

        return

    if not bot.intents.message_content:

        await interaction.response.send_message(
            "❌ I can't read message text yet. In the **Discord Developer "
            "Portal**, open your bot → **Bot** → turn on **Message Content "
            "Intent** → Save, then restart the bot on Railway.",
            ephemeral=True
        )

        return

    since_date = None

    if since:
        parsed, ok = parse_date(since)

        if not ok or not parsed:
            await interaction.response.send_message(
                "❌ Write the date like 01/09/2026.",
                ephemeral=True
            )
            return

        since_date = datetime.fromisoformat(parsed).replace(tzinfo=timezone.utc)

    await interaction.response.defer(
        ephemeral=True
    )

    try:

        ready, problems, name_only, skipped, messages_read = (
            await scan_channel_results(
                interaction.guild,
                channel,
                since_date
            )
        )

    except discord.Forbidden:

        await interaction.followup.send(
            f"❌ I don't have permission to read {channel.mention}. "
            "Give the bot **View Channel** and **Read Message History** there.",
            ephemeral=True
        )

        return

    except Exception as error:

        print(f"Scan error: {error}")

        await interaction.followup.send(
            "❌ Something went wrong while reading that channel.",
            ephemeral=True
        )

        return

    found = len(ready) + skipped

    await fill_departed_names(ready)

    await interaction.followup.send(
        embed=create_import_summary_embed(
            ready,
            problems,
            name_only,
            skipped,
            found_text=(
                f"Read **{messages_read} messages** in {channel.mention} "
                f"and found **{found} results**."
            ),
            author_text="PFO • SCAN RESULTS CHANNEL"
        ),
        view=ImportConfirmView(
            interaction.user.id,
            interaction.guild.id,
            ready
        ),
        ephemeral=True
    )


# ============================================================
# FIGHT CARD DMS AND LIVE CHECK-IN
#
#   /dmfightnight  DM everyone on a Fight Night card (staff)
#   /dmlivecard    DM everyone on a Live Card, with a Check In
#                  button in the DM (staff)
#   /livecheckin   post a board showing who has checked in; it
#                  updates by itself when people press the button
#                  (staff)
#
# Who gets the DM: @mention everyone in the "fighters" box, any
# number of people in one go (up to 60).
#
# Live Card dates become Discord timestamps, so every fighter sees
# the time in their own timezone. Staff type the time in UK time
# (change EVENT_TIMEZONE in Railway to use a different one).
# ============================================================

SELECTED_FIGHT_NIGHT_BANNER = "selected_fight_night.gif"
SELECTED_LIVE_CARD_BANNER = "selected_live_card.gif"
CHECKIN_BANNER = "checkin.gif"
CHECKED_IN_BANNER = "checked_in.gif"

MAX_CARD_FIGHTERS = 60
DM_DELAY_SECONDS = 1.0  # gentle on Discord, avoids being blocked

MENTION_PATTERN = re.compile(r"<@!?(\d+)>")
EVENT_TIMEZONE = os.getenv(
    "EVENT_TIMEZONE",
    "Europe/London"
).strip()


FIGHT_NIGHT_DM_TEXT = (
    "You have been selected to fight on a PFO Fight Night, please check "
    "the card and organise a time with your opponent before {deadline}.\n\n"
    "No attempt to fight will result in a suspension from upcoming cards or "
    "instant removal of your fighter. If you are struggling to organise a "
    "time with your opponent please contact a member of staff in PFO."
)

LIVE_CARD_DM_TEXT = (
    "You have been selected to fight on **{event}** on {date}. Please "
    "check the card and make contact with your opponent as soon as possible.\n\n"
    "One person per fight **MUST** stream, please sort this out before the day "
    "and ensure you are lobbied up a few fights before yours so that the event "
    "runs smoothly.\n\n"
    "Failure to show will result in a suspension from upcoming events or "
    "removal of your fighter. Please press the button below to check in so "
    "we know you are going to show."
)


# ------------------------------------------------------------
# Database
# ------------------------------------------------------------

def setup_card_tables():

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS card_notices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            event TEXT,
            event_date TEXT,
            deadline TEXT,
            card_url TEXT,
            created_by INTEGER,
            created_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            board_channel_id INTEGER,
            board_message_id INTEGER
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS card_fighters (
            notice_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            name TEXT NOT NULL,
            opponent_id INTEGER,
            dm_status TEXT NOT NULL DEFAULT 'pending',
            checked_in_at TEXT,

            PRIMARY KEY(notice_id, user_id)
        )
    """)

    db.commit()
    db.close()


def create_notice(
    guild_id: int,
    kind: str,
    created_by: int,
    fighters: list,
    event: str = None,
    event_date: str = None,
    deadline: str = None,
    card_url: str = None
):
    """fighters: list of (user_id, name, opponent_id)"""

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        INSERT INTO card_notices
        (guild_id, kind, event, event_date, deadline, card_url, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        guild_id, kind, event, event_date, deadline, card_url, created_by, now_utc()
    ))

    notice_id = cursor.lastrowid

    cursor.executemany("""
        INSERT INTO card_fighters
        (notice_id, user_id, position, name, opponent_id)
        VALUES (?, ?, ?, ?, ?)
    """, [
        (notice_id, user_id, position, name, opponent_id)
        for position, (user_id, name, opponent_id) in enumerate(fighters)
    ])

    db.commit()
    db.close()

    return notice_id


def get_notice(
    notice_id: int
):

    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM card_notices WHERE id = ?", (notice_id,))
    row = cursor.fetchone()
    db.close()

    return row


def get_latest_live_notice(
    guild_id: int
):

    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT * FROM card_notices
        WHERE guild_id = ? AND kind = 'live_card'
        ORDER BY id DESC
        LIMIT 1
    """, (guild_id,))
    row = cursor.fetchone()
    db.close()

    return row


def get_active_live_notices():

    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT * FROM card_notices
        WHERE kind = 'live_card' AND active = 1
    """)
    rows = cursor.fetchall()
    db.close()

    return rows


def get_card_fighters(
    notice_id: int
):

    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT * FROM card_fighters
        WHERE notice_id = ?
        ORDER BY position ASC
    """, (notice_id,))
    rows = cursor.fetchall()
    db.close()

    return rows


def get_card_fighter(
    notice_id: int,
    user_id: int
):

    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT * FROM card_fighters
        WHERE notice_id = ? AND user_id = ?
    """, (notice_id, user_id))
    row = cursor.fetchone()
    db.close()

    return row


def update_card_fighter(
    notice_id: int,
    user_id: int,
    **changes
):

    columns = ", ".join(f"{column} = ?" for column in changes)

    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        f"UPDATE card_fighters SET {columns} WHERE notice_id = ? AND user_id = ?",
        (*changes.values(), notice_id, user_id)
    )
    db.commit()
    db.close()


def update_notice(
    notice_id: int,
    **changes
):

    columns = ", ".join(f"{column} = ?" for column in changes)

    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        f"UPDATE card_notices SET {columns} WHERE id = ?",
        (*changes.values(), notice_id)
    )
    db.commit()
    db.close()


# ------------------------------------------------------------
# Dates and times
# ------------------------------------------------------------

def uk_offset_hours(
    naive: datetime
):
    """
    Fallback when the server has no timezone data. UK clocks are
    GMT, or BST (+1 hour) from the last Sunday of March (01:00) to
    the last Sunday of October (02:00 local).
    """

    def last_sunday(year, month):
        next_month = datetime(year + (month == 12), month % 12 + 1, 1)
        day = next_month - timedelta(days=1)
        return day - timedelta(days=(day.weekday() - 6) % 7)

    starts = last_sunday(naive.year, 3).replace(hour=1)
    ends = last_sunday(naive.year, 10).replace(hour=2)

    return 1 if starts <= naive < ends else 0


def to_utc(
    naive: datetime
):

    try:
        from zoneinfo import ZoneInfo
        return naive.replace(tzinfo=ZoneInfo(EVENT_TIMEZONE)).astimezone(timezone.utc)
    except Exception:
        offset = uk_offset_hours(naive) if EVENT_TIMEZONE == "Europe/London" else 0
        return (naive - timedelta(hours=offset)).replace(tzinfo=timezone.utc)


def parse_event_datetime(
    text: str
):
    """
    Reads a date and time typed in UK time, e.g.
      24/10/2026 20:00   24/10/2026 8pm   24/10 8:30pm   2026-10-24 20:00
    Returns a UTC datetime, or None if it can't be read.
    """

    text = " ".join((text or "").lower().replace(",", " ").replace(" at ", " ").split())
    text = re.sub(
        r"^(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day|nesday|sday|urday|rsday)?\s+",
        "",
        text
    )

    match = re.fullmatch(
        r"(?:(?P<d>\d{1,2})[/.-](?P<m>\d{1,2})(?:[/.-](?P<y>\d{2,4}))?"
        r"|(?P<y2>\d{4})-(?P<m2>\d{1,2})-(?P<d2>\d{1,2}))"
        r"\s+(?P<h>\d{1,2})(?:[:.](?P<min>\d{2}))?\s*(?P<ampm>am|pm)?",
        text
    )

    if not match:
        return None

    if match.group("y2"):
        year, month, day = int(match.group("y2")), int(match.group("m2")), int(match.group("d2"))
    else:
        day, month = int(match.group("d")), int(match.group("m"))
        year = match.group("y")
        year = int(year) + (2000 if year and len(year) == 2 else 0) if year else None

    hour = int(match.group("h"))
    minute = int(match.group("min") or 0)
    ampm = match.group("ampm")

    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)

    now = datetime.now(timezone.utc)

    try:
        if year is None:
            year = now.year
            candidate = datetime(year, month, day, hour, minute)
            if to_utc(candidate) < now:
                year += 1
        naive = datetime(year, month, day, hour, minute)
    except ValueError:
        return None

    return to_utc(naive)


def discord_time(
    moment: datetime
):
    """Shows in every reader's own timezone, e.g. 'Saturday, 24 October 2026 20:00 (in 12 days)'."""

    unix = int(moment.timestamp())

    return f"**<t:{unix}:F>** (<t:{unix}:R>)"


def format_saved_time(
    value: str
):

    if value and value.isdigit():
        return discord_time(datetime.fromtimestamp(int(value), timezone.utc))

    return f"**{value}**" if value else ""


# ------------------------------------------------------------
# Working out who is on the card
# ------------------------------------------------------------

class CardInputError(Exception):
    pass


def parse_fight_pairs(
    text: str
):
    """
    "@joe @dan @tom @ben" -> [(joe, dan), (tom, ben)]
    Consecutive @mentions are opponents. An odd one out gets None.
    """

    ids = [int(found) for found in MENTION_PATTERN.findall(text or "")]

    return [
        (ids[index], ids[index + 1] if index + 1 < len(ids) else None)
        for index in range(0, len(ids), 2)
    ]


async def collect_card(
    guild: discord.Guild,
    fighters_text: str
):
    """
    Everyone @mentioned, in the order typed. Each pair of mentions
    are opponents.

    Returns a list of (user_id, name, member or None, opponent_id).
    """

    if not MENTION_PATTERN.search(fighters_text or ""):
        raise CardInputError(
            "@mention everyone on the card in **fighters** in fight order, "
            "e.g. `@Joe @Dan @Tom @Ben` (Joe vs Dan, Tom vs Ben)."
        )

    pairs = parse_fight_pairs(fighters_text)

    order = []
    opponents = {}

    for a, b in pairs:
        for me, them in ((a, b), (b, a)):
            if me is None:
                continue
            if me not in order:
                order.append(me)
            if them is not None and them != me:
                opponents.setdefault(me, them)

    fighters = []

    for user_id in order:

        member = guild.get_member(user_id)

        if member and member.bot:
            continue

        name = member.display_name if member else await departed_name(user_id)

        fighters.append((user_id, name, member, opponents.get(user_id)))

    if not fighters:
        raise CardInputError(
            "I didn't find anyone. @mention everyone on the card in "
            "**fighters**, e.g. `@Joe @Dan @Tom @Ben`"
        )

    if len(fighters) > MAX_CARD_FIGHTERS:
        raise CardInputError(
            f"That's {len(fighters)} people; the limit is {MAX_CARD_FIGHTERS} per command."
        )

    return fighters


# ------------------------------------------------------------
# DM embeds and buttons
# ------------------------------------------------------------

def create_selection_dm_embed(
    kind: str,
    event: str = None,
    event_date: str = None,
    deadline: str = None,
    opponent_text: str = None
):

    if kind == "fight_night":
        description = FIGHT_NIGHT_DM_TEXT.format(deadline=format_saved_time(deadline))
        author = "PFO • FIGHT NIGHT"
        title = "🥊 You've been selected!"
        banner = SELECTED_FIGHT_NIGHT_BANNER
    else:
        description = LIVE_CARD_DM_TEXT.format(event=event, date=format_saved_time(event_date))
        if event and event.upper().startswith("PFO"):
            author = event.upper()
        elif event:
            author = f"PFO • {event.upper()}"
        else:
            author = "PFO • LIVE CARD"
        title = "🥊 You've been selected!"
        banner = SELECTED_LIVE_CARD_BANNER

    embed = discord.Embed(
        title=title,
        description=description,
        color=PFO_GOLD,
        timestamp=discord.utils.utcnow()
    )

    apply_branding(
        embed,
        banner_file=banner,
        author_text=author
    )

    if opponent_text:
        embed.add_field(
            name="🆚 Your Opponent",
            value=opponent_text,
            inline=False
        )

    embed.set_footer(
        text="Prestige Fighting Organisation",
        icon_url=asset_url(LOGO_FILE)
    )

    return embed


class CheckInView(
    discord.ui.View
):
    """The Check In button in Live Card DMs. Works after restarts."""

    def __init__(
        self,
        notice_id: int
    ):

        super().__init__(
            timeout=None
        )

        self.notice_id = notice_id

        button = discord.ui.Button(
            label="Check In",
            style=discord.ButtonStyle.green,
            emoji="✅",
            custom_id=f"pfo_checkin_{notice_id}"
        )

        button.callback = self.check_in

        self.add_item(button)

    async def check_in(
        self,
        interaction: discord.Interaction
    ):

        notice = get_notice(self.notice_id)

        if not notice or not notice["active"]:

            await interaction.response.send_message(
                "❌ Check-in for this event has closed. Please contact a member of staff in PFO."
            )

            return

        row = get_card_fighter(
            self.notice_id,
            interaction.user.id
        )

        if not row:

            await interaction.response.send_message(
                "❌ You're not on this card."
            )

            return

        if not row["checked_in_at"]:
            update_card_fighter(
                self.notice_id,
                interaction.user.id,
                checked_in_at=now_utc()
            )

        embed = (
            interaction.message.embeds[0].copy()
            if interaction.message and interaction.message.embeds
            else create_selection_dm_embed("live_card", event=notice["event"], event_date=notice["event_date"])
        )

        banner = asset_url(CHECKED_IN_BANNER)

        if banner:
            embed.set_image(url=banner)

        if not any(field.name.startswith("✅") for field in embed.fields):
            embed.add_field(
                name="✅ Checked in",
                value="Thanks! Staff can see you're coming. See you on the night.",
                inline=False
            )

        done = discord.ui.View(timeout=None)
        done.add_item(discord.ui.Button(
            label="Checked In",
            style=discord.ButtonStyle.grey,
            emoji="✅",
            disabled=True
        ))

        await interaction.response.edit_message(
            embed=embed,
            view=done
        )

        await refresh_checkin_board(self.notice_id)


# ------------------------------------------------------------
# Check-in board
# ------------------------------------------------------------

def create_checkin_board_embed(
    notice
):

    fighters = get_card_fighters(notice["id"])

    checked = sum(1 for row in fighters if row["checked_in_at"])

    def fighter_text(row):
        if row["checked_in_at"]:
            icon = "✅"
        elif row["dm_status"] == "failed":
            icon = "⚠️"
        else:
            icon = "⏳"
        return f"{icon} <@{row['user_id']}>"

    by_id = {row["user_id"]: row for row in fighters}
    lines = []
    shown = set()

    for row in fighters:

        if row["user_id"] in shown:
            continue

        shown.add(row["user_id"])
        opponent = by_id.get(row["opponent_id"])

        if opponent and opponent["user_id"] not in shown:
            shown.add(opponent["user_id"])
            lines.append(f"`{len(lines) + 1:>2}` {fighter_text(row)}  vs  {fighter_text(opponent)}")
        else:
            lines.append(f"`{len(lines) + 1:>2}` {fighter_text(row)}")

    header = []

    if notice["event_date"]:
        header.append(f"📅 {format_saved_time(notice['event_date'])}")

    header.append(f"**{checked} / {len(fighters)} checked in**")

    if not notice["active"]:
        header.append("🔒 **Check-in closed**")

    embed = discord.Embed(
        title=notice["event"] or "Live Card",
        description=("\n".join(header) + "\n\n" + "\n".join(lines))[:4000],
        color=PFO_GOLD,
        timestamp=discord.utils.utcnow()
    )

    apply_branding(
        embed,
        banner_file=CHECKIN_BANNER,
        author_text="PFO • LIVE CARD CHECK-IN"
    )

    embed.set_footer(
        text="✅ Checked in    ⏳ Waiting    ⚠️ Couldn't DM • Updates automatically",
        icon_url=asset_url(LOGO_FILE)
    )

    return embed


async def refresh_checkin_board(
    notice_id: int
):

    notice = get_notice(notice_id)

    if not notice or not notice["board_message_id"]:
        return

    try:
        channel = bot.get_channel(notice["board_channel_id"]) or await bot.fetch_channel(
            notice["board_channel_id"]
        )
        message = await channel.fetch_message(notice["board_message_id"])
        await message.edit(embed=create_checkin_board_embed(notice))
    except Exception as error:
        print(f"Could not refresh check-in board: {error}")


# ------------------------------------------------------------
# Sending
# ------------------------------------------------------------

class SendDMsView(
    discord.ui.View
):
    """Confirm step before DMing a whole card."""

    def __init__(
        self,
        author_id: int,
        guild: discord.Guild,
        kind: str,
        fighters: list,
        event: str = None,
        event_date: str = None,
        deadline: str = None
    ):

        super().__init__(
            timeout=600
        )

        self.author_id = author_id
        self.guild = guild
        self.kind = kind
        self.fighters = fighters
        self.event = event
        self.event_date = event_date
        self.deadline = deadline

        confirm = discord.ui.Button(
            label=f"Send {len(fighters)} DM{'s' if len(fighters) != 1 else ''}",
            style=discord.ButtonStyle.green,
            emoji="📨"
        )
        confirm.callback = self.confirm

        cancel = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.grey
        )
        cancel.callback = self.cancel

        self.add_item(confirm)
        self.add_item(cancel)

    async def interaction_check(
        self,
        interaction: discord.Interaction
    ):

        if interaction.user.id != self.author_id:

            await interaction.response.send_message(
                "❌ Only the person who started this can confirm it.",
                ephemeral=True
            )

            return False

        return True

    async def cancel(
        self,
        interaction: discord.Interaction
    ):

        self.stop()

        await interaction.response.edit_message(
            content="Cancelled. No DMs were sent.",
            embed=None,
            view=None
        )

    async def confirm(
        self,
        interaction: discord.Interaction
    ):

        self.stop()

        await interaction.response.edit_message(
            content=f"📨 Sending DMs to {len(self.fighters)} fighters… this takes about "
                    f"{max(1, round(len(self.fighters) * DM_DELAY_SECONDS))} seconds.",
            embed=None,
            view=None
        )

        notice_id = create_notice(
            self.guild.id,
            self.kind,
            self.author_id,
            [(user_id, name, opponent_id) for user_id, name, _, opponent_id in self.fighters],
            event=self.event,
            event_date=self.event_date,
            deadline=self.deadline
        )

        sent = []
        failed = []

        names = {user_id: name for user_id, name, _, _ in self.fighters}

        for index, (user_id, name, member, opponent_id) in enumerate(self.fighters):

            if index:
                await asyncio.sleep(DM_DELAY_SECONDS)

            if member is None:
                failed.append((user_id, "not in the server"))
                update_card_fighter(notice_id, user_id, dm_status="failed")
                continue

            if opponent_id:
                opponent_text = (
                    f"**{name_without_record(names.get(opponent_id, 'Your opponent'))}** "
                    f"(<@{opponent_id}>)"
                )
            else:
                opponent_text = None

            embed = create_selection_dm_embed(
                self.kind,
                opponent_text=opponent_text,
                event=self.event,
                event_date=self.event_date,
                deadline=self.deadline
            )

            view = CheckInView(notice_id) if self.kind == "live_card" else None

            try:
                if view:
                    await member.send(embed=embed, view=view)
                else:
                    await member.send(embed=embed)

                sent.append(user_id)
                update_card_fighter(notice_id, user_id, dm_status="sent")

            except discord.Forbidden:
                failed.append((user_id, "DMs closed"))
                update_card_fighter(notice_id, user_id, dm_status="failed")

            except Exception as error:
                print(f"DM to {user_id} failed: {error}")
                failed.append((user_id, "Discord error"))
                update_card_fighter(notice_id, user_id, dm_status="failed")

        lines = [
            f"✅ **DMed {len(sent)} of {len(self.fighters)} fighters.**"
        ]

        if failed:
            lines.append("")
            lines.append("⚠️ **Couldn't DM these, so let them know another way:**")
            for user_id, reason in failed[:25]:
                lines.append(f"• <@{user_id}> ({reason})")
            if len(failed) > 25:
                lines.append(f"• …and {len(failed) - 25} more")

        if self.kind == "live_card":
            lines.append("")
            lines.append("Use `/livecheckin` to post the check-in board.")

        await interaction.edit_original_response(
            content="\n".join(lines)[:2000]
        )

        await refresh_checkin_board(notice_id)


def create_dm_preview_embed(
    kind: str,
    fighters: list,
    event: str = None,
    event_date: str = None,
    deadline: str = None
):

    lines = []
    shown = set()
    number = 0
    by_id = {user_id: (name, member, opponent_id) for user_id, name, member, opponent_id in fighters}

    def tag(user_id):
        member = by_id.get(user_id, (None, None, None))[1]
        return f"<@{user_id}>" + ("" if member else " ⚠️ not in server")

    for user_id, name, member, opponent_id in fighters:

        if user_id in shown:
            continue

        number += 1
        shown.add(user_id)

        if opponent_id and opponent_id in by_id and opponent_id not in shown:
            shown.add(opponent_id)
            lines.append(f"`{number:>2}` {tag(user_id)}  vs  {tag(opponent_id)}")
        else:
            lines.append(f"`{number:>2}` {tag(user_id)}  ⚠️ no opponent (odd number of fighters)")

    if kind == "fight_night":
        details = f"**Deadline:** {format_saved_time(deadline)}"
        if CARD_REMINDERS:
            details += (
                "\n**Reminder:** sent automatically 1 day before the deadline"
                if deadline and str(deadline).isdigit() else
                "\n**Reminder:** none (type the deadline as a date, e.g. 19/11/2026 22:00, to get one)"
            )
        author = "PFO • DM FIGHT NIGHT CARD"
    else:
        details = (
            f"**Event:** {event}\n"
            f"**When:** {format_saved_time(event_date)}\n"
            "**Check-in button:** included"
        )
        if CARD_REMINDERS:
            details += "\n**Reminder:** sent automatically 1 hour before"
        author = "PFO • DM LIVE CARD"

    embed = discord.Embed(
        title=f"Send DMs to {len(fighters)} fighters?",
        description=(details + "\n\n" + "\n".join(lines))[:4000],
        color=PFO_GOLD
    )

    apply_branding(
        embed,
        author_text=author
    )

    embed.set_footer(
        text="Times show in each person's own timezone • Nothing is sent until you press Send",
        icon_url=asset_url(LOGO_FILE)
    )

    return embed


async def start_card_dms(
    interaction: discord.Interaction,
    kind: str,
    fighters_text: str,
    event: str = None,
    event_date: str = None,
    deadline: str = None
):

    if not is_staff(interaction):

        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can DM fight cards.",
            ephemeral=True
        )

        return

    await interaction.response.defer(
        ephemeral=True
    )

    try:
        fighters = await collect_card(
            interaction.guild,
            fighters_text
        )
    except CardInputError as error:
        await interaction.followup.send(f"❌ {error}", ephemeral=True)
        return

    await interaction.followup.send(
        embed=create_dm_preview_embed(
            kind, fighters, event, event_date, deadline
        ),
        view=SendDMsView(
            interaction.user.id,
            interaction.guild,
            kind,
            fighters,
            event=event,
            event_date=event_date,
            deadline=deadline
        ),
        ephemeral=True
    )


# ------------------------------------------------------------
# Commands
# ------------------------------------------------------------

@bot.tree.command(
    name="dmfightnight",
    description="DM everyone you @mention that they've been selected for a Fight Night."
)
@app_commands.describe(
    deadline="Deadline in UK time, e.g. 19/10/2026 22:00 (or any text, e.g. Sunday night)",
    fighters="@mention in fight order: @Joe @Dan @Tom @Ben = Joe v Dan, Tom v Ben"
)
async def dmfightnight(
    interaction: discord.Interaction,
    deadline: str,
    fighters: str
):

    moment = parse_event_datetime(deadline)

    await start_card_dms(
        interaction,
        "fight_night",
        fighters,
        deadline=str(int(moment.timestamp())) if moment else deadline.strip()
    )


@bot.tree.command(
    name="dmlivecard",
    description="DM everyone you @mention for a Live Card, with a button to check in."
)
@app_commands.describe(
    event="Event name, e.g. PFO Live Card 6",
    date="Date and time in UK time, e.g. 24/10/2026 20:00 or 24/10 8pm",
    fighters="@mention in fight order: @Joe @Dan @Tom @Ben = Joe v Dan, Tom v Ben"
)
async def dmlivecard(
    interaction: discord.Interaction,
    event: str,
    date: str,
    fighters: str
):

    moment = parse_event_datetime(date)

    if not moment:

        await interaction.response.send_message(
            "❌ Write the date and time like **24/10/2026 20:00** or **24/10 8pm** "
            "(UK time). It's turned into a timestamp everyone sees in their own timezone.",
            ephemeral=True
        )

        return

    if moment < datetime.now(timezone.utc):

        await interaction.response.send_message(
            "❌ That date and time has already passed.",
            ephemeral=True
        )

        return

    await start_card_dms(
        interaction,
        "live_card",
        fighters,
        event=event.strip(),
        event_date=str(int(moment.timestamp()))
    )


@bot.tree.command(
    name="livecheckin",
    description="Show who has checked in for the latest Live Card."
)
@app_commands.describe(
    close="Close check-in (the button stops working)"
)
async def livecheckin(
    interaction: discord.Interaction,
    close: bool = False
):

    if not is_staff(interaction):

        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can view check-ins.",
            ephemeral=True
        )

        return

    notice = get_latest_live_notice(
        interaction.guild.id
    )

    if not notice:

        await interaction.response.send_message(
            "❌ No Live Card has been DMed yet. Use `/dmlivecard` first.",
            ephemeral=True
        )

        return

    if close:

        update_notice(notice["id"], active=0)

        await refresh_checkin_board(notice["id"])

        await interaction.response.send_message(
            f"🔒 Check-in for **{notice['event']}** is closed.",
            ephemeral=True
        )

        return

    await interaction.response.send_message(
        embed=create_checkin_board_embed(notice)
    )

    message = await interaction.original_response()

    update_notice(
        notice["id"],
        board_channel_id=message.channel.id,
        board_message_id=message.id
    )


def restore_checkin_buttons():

    notices = get_active_live_notices()

    for notice in notices:
        try:
            bot.add_view(CheckInView(notice["id"]))
        except Exception as error:
            print(f"Could not restore check-in {notice['id']}: {error}")

    print(f"Loaded {len(notices)} active live check-in(s).")


# ============================================================
# STARTUP
#
# Runs once when the bot starts (not on every reconnect):
#   1. set up the database
#   2. restore the Sign Up buttons on open sign-ups
#   3. register slash commands with Discord, but ONLY when this
#      file has changed since the last time. Re-registering on
#      every start/reconnect is a common reason Discord
#      temporarily blocks a bot for too many requests.
# ============================================================

def get_meta(
    key: str
):

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bot_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    cursor.execute(
        "SELECT value FROM bot_meta WHERE key = ?",
        (key,)
    )

    row = cursor.fetchone()

    db.commit()
    db.close()

    return row["value"] if row else None


def set_meta(
    key: str,
    value: str
):

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bot_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    cursor.execute(
        "INSERT OR REPLACE INTO bot_meta (key, value) VALUES (?, ?)",
        (key, value)
    )

    db.commit()
    db.close()


def commands_fingerprint():

    try:
        with open(os.path.abspath(__file__), "rb") as source:
            code = source.read()
    except OSError:
        code = b""

    return hashlib.sha256(
        code + str(GUILD_ID).encode()
    ).hexdigest()


def restore_signup_buttons():

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        SELECT *
        FROM signup_sessions
        WHERE active = 1
    """)

    active_sessions = cursor.fetchall()

    db.close()

    for session in active_sessions:

        try:

            bot.add_view(
                SignupView(
                    session["id"],
                    session["signup_type"]
                )
            )

        except Exception as error:

            print(
                f"Could not restore signup "
                f"{session['id']}: {error}"
            )

    print(
        f"Loaded {len(active_sessions)} "
        f"active signup session(s)."
    )


async def sync_commands_if_changed():

    fingerprint = commands_fingerprint()

    if (
        os.getenv("FORCE_COMMAND_SYNC", "").strip() not in ("1", "true", "yes")
        and get_meta("commands_fingerprint") == fingerprint
    ):
        print("Slash commands unchanged; skipping sync.")
        return

    try:

        if GUILD_ID:

            guild = discord.Object(
                id=int(GUILD_ID)
            )

            bot.tree.copy_global_to(
                guild=guild
            )

            await bot.tree.sync(
                guild=guild
            )

            print(
                "Slash commands synced to your server."
            )

        else:

            await bot.tree.sync()

            print(
                "Global slash commands synced."
            )

        set_meta(
            "commands_fingerprint",
            fingerprint
        )

    except Exception as error:

        # Don't stop the bot over this: the existing commands keep
        # working, and it tries again on the next start.
        print(
            f"Could not sync slash commands (will retry next start): {error}"
        )


# ============================================================
# WARNINGS, SUSPENSIONS AND INACTIVITY
#
# /warn         warn a fighter for missing a booked fight (DM optional)
#               2 warnings = suspended from events for a week
# /warnings     see someone's warnings, or everyone with warnings
# /removewarning  take the latest warning back off
# /unsuspend    end a suspension early
# /inactive     fighters (record in their name) missing sign-ups
#
# Suspended fighters can't sign up. Suspensions end by themselves
# and a note is posted in the staff bot channel.
# When a sign-up is closed, everyone with a record in their name
# who didn't sign up is flagged in the same channel
# (INACTIVE_REPORT=off in Railway turns that off).
# ============================================================

WARNING_BANNER = "warning.gif"
SUSPENDED_BANNER = "suspended.gif"
INACTIVE_BANNER = "inactive.gif"

MAX_WARNINGS = 2
SUSPENSION_DAYS = int(os.getenv("SUSPENSION_DAYS", "7"))
INACTIVE_REPORT = os.getenv("INACTIVE_REPORT", "on").strip().lower() not in ("off", "no", "false", "0")

WARNING_DM_INTRO = (
    "You have been warned for missing your fight in PFO, in the future "
    "please let us know why and this can be avoided."
)


def setup_discipline_tables():

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            reason TEXT,
            warned_by INTEGER,
            created_at INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS suspensions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            starts_at INTEGER NOT NULL,
            ends_at INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_by INTEGER
        )
    """)

    db.commit()
    db.close()


def discipline_query(
    sql: str,
    params: tuple = (),
    write: bool = False
):

    db = get_db()
    cursor = db.cursor()
    cursor.execute(sql, params)

    if write:
        db.commit()
        result = cursor.lastrowid
    else:
        result = cursor.fetchall()

    db.close()

    return result


def active_warnings(
    guild_id: int,
    user_id: int
):

    return discipline_query("""
        SELECT * FROM warnings
        WHERE guild_id = ? AND user_id = ? AND active = 1
        ORDER BY id ASC
    """, (guild_id, user_id))


def get_active_suspension(
    guild_id: int,
    user_id: int
):

    rows = discipline_query("""
        SELECT * FROM suspensions
        WHERE guild_id = ? AND user_id = ? AND status = 'active'
        ORDER BY ends_at DESC LIMIT 1
    """, (guild_id, user_id))

    return rows[0] if rows else None


def all_active_suspensions(
    guild_id: int
):

    return discipline_query("""
        SELECT * FROM suspensions
        WHERE guild_id = ? AND status = 'active'
        ORDER BY ends_at ASC
    """, (guild_id,))


def clear_warnings(
    guild_id: int,
    user_id: int
):

    discipline_query(
        "UPDATE warnings SET active = 0 WHERE guild_id = ? AND user_id = ? AND active = 1",
        (guild_id, user_id),
        write=True
    )


def time_tag(
    unix: int
):
    """Full date in everyone's own timezone, plus a live countdown."""

    return f"<t:{unix}:f> (<t:{unix}:R>)"


def is_member_fighter(
    member
):

    return has_any_role(member, FIGHTER_ROLE_NAMES)


def suspension_signup_text(
    suspension
):

    return (
        "⛔ **You're suspended from PFO events** after missing "
        f"{MAX_WARNINGS} booked fights, so you can't sign up right now.\n\n"
        f"Your suspension ends {time_tag(suspension['ends_at'])}."
    )


def create_warning_dm_embed(
    count: int,
    ends_at: int = None,
    reason: str = None
):

    lines = [WARNING_DM_INTRO, ""]

    if count >= MAX_WARNINGS:
        lines += [
            f"You currently have **{count}/{MAX_WARNINGS}** warnings. Your fighter will now "
            "be removed and you will be suspended for one week from events.",
            "",
            f"⛔ **Suspended until:** {time_tag(ends_at)}",
        ]
    else:
        lines.append(
            f"You currently have **{count}/{MAX_WARNINGS}** warnings. You have one more "
            "chance at showing for the next event you sign up for."
        )

    embed = discord.Embed(
        title="⛔ You have been suspended" if count >= MAX_WARNINGS else "⚠️ You have been warned",
        description="\n".join(lines),
        color=PFO_GOLD,
        timestamp=discord.utils.utcnow()
    )

    if reason:
        embed.add_field(name="📝 Note from staff", value=reason[:1000], inline=False)

    apply_branding(
        embed,
        banner_file=SUSPENDED_BANNER if count >= MAX_WARNINGS else WARNING_BANNER,
        author_text="PFO • SUSPENSION" if count >= MAX_WARNINGS else "PFO • WARNING"
    )

    embed.set_footer(
        text="Prestige Fighting Organisation",
        icon_url=asset_url(LOGO_FILE)
    )

    return embed


async def post_staff_log(
    guild: discord.Guild,
    embed: discord.Embed
):
    """Posts in the staff bot channel (same one as ranking suggestions)."""

    channel = find_suggestions_channel(guild)

    if not channel:
        return False

    try:
        await channel.send(embed=embed)
        return True
    except Exception as error:
        print(f"Could not post staff log: {error}")
        return False


def staff_log_embed(
    title: str,
    lines: list,
    banner: str = None
):

    embed = discord.Embed(
        title=title,
        description="\n".join(lines),
        color=PFO_GOLD,
        timestamp=discord.utils.utcnow()
    )

    apply_branding(
        embed,
        banner_file=banner,
        author_text="PFO • DISCIPLINE"
    )

    return embed


async def take_off_open_signups(
    guild: discord.Guild,
    user_id: int
):
    """Suspended fighters come off any sign-up list that's still open."""

    removed_from = []

    for signup_type in SIGNUP_INFO.keys():

        session = get_active_session(guild.id, signup_type)

        if session and remove_signup(session["id"], user_id):
            removed_from.append(SIGNUP_INFO[signup_type]["title"])
            await refresh_signup_message(guild, session, signup_type)

    return removed_from


class WarnConfirmView(
    discord.ui.View
):

    def __init__(
        self,
        author_id: int,
        member,
        reason: str,
        send_dm: bool
    ):

        super().__init__(
            timeout=600
        )

        self.author_id = author_id
        self.member = member
        self.reason = reason
        self.send_dm = send_dm

        confirm = discord.ui.Button(label="Give warning", emoji="⚠️", style=discord.ButtonStyle.red)
        confirm.callback = self.confirm
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.grey)
        cancel.callback = self.cancel
        self.add_item(confirm)
        self.add_item(cancel)

    async def interaction_check(self, interaction):

        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the person who ran /warn can use these buttons.",
                ephemeral=True
            )
            return False

        return True

    async def confirm(self, interaction):

        self.stop()

        guild = interaction.guild
        member = self.member

        await interaction.response.edit_message(
            content="⏳ Giving the warning...",
            embed=None,
            view=None
        )

        if get_active_suspension(guild.id, member.id):
            await interaction.edit_original_response(
                content=f"❌ **{member.display_name}** is already suspended."
            )
            return

        now = int(time.time())

        discipline_query("""
            INSERT INTO warnings (guild_id, user_id, reason, warned_by, created_at, active)
            VALUES (?, ?, ?, ?, ?, 1)
        """, (guild.id, member.id, self.reason, interaction.user.id, now), write=True)

        count = len(active_warnings(guild.id, member.id))
        name = name_without_record(member.display_name)
        lines = [f"⚠️ **Warned {name}** ({count}/{MAX_WARNINGS})."]
        log = [f"⚠️ **{name}** (<@{member.id}>) was warned by <@{interaction.user.id}>: **{count}/{MAX_WARNINGS}**."]

        if self.reason:
            log.append(f"📝 {self.reason}")

        ends_at = None

        if count >= MAX_WARNINGS:

            ends_at = now + SUSPENSION_DAYS * 86400

            discipline_query("""
                INSERT INTO suspensions (guild_id, user_id, starts_at, ends_at, status, created_by)
                VALUES (?, ?, ?, ?, 'active', ?)
            """, (guild.id, member.id, now, ends_at, interaction.user.id), write=True)

            lines.append(f"⛔ Suspended until {time_tag(ends_at)}. They can't sign up until then.")
            log.append(f"⛔ Suspended until {time_tag(ends_at)}.")

            removed_from = await take_off_open_signups(guild, member.id)

            if removed_from:
                lines.append(f"↩️ Taken off: {', '.join(removed_from)}.")

        if self.send_dm:
            try:
                await member.send(embed=create_warning_dm_embed(count, ends_at, self.reason))
                lines.append("📨 DM sent.")
            except discord.Forbidden:
                lines.append("📭 **DM not sent**: they have DMs closed. Let them know another way.")
            except Exception as error:
                print(f"Warning DM failed: {error}")
                lines.append("📭 DM not sent (Discord error).")
        else:
            lines.append("🔕 No DM sent (you chose not to).")

        await interaction.edit_original_response(content="\n".join(lines))

        await post_staff_log(
            guild,
            staff_log_embed(
                "⛔ Fighter suspended" if ends_at else "⚠️ Warning given",
                log,
                SUSPENDED_BANNER if ends_at else None
            )
        )

    async def cancel(self, interaction):

        self.stop()

        await interaction.response.edit_message(
            content="Cancelled. No warning was given.",
            embed=None,
            view=None
        )


YES_NO_CHOICES = [
    app_commands.Choice(name="Yes", value="yes"),
    app_commands.Choice(name="No", value="no"),
]


@bot.tree.command(
    name="warn",
    description="Warn a fighter for missing a booked fight. 2 warnings = suspended for a week."
)
@app_commands.describe(
    member="Who missed their fight",
    reason="Optional note (shown in their DM and the staff log)",
    dm="DM them the warning? (default: yes)"
)
@app_commands.choices(
    dm=YES_NO_CHOICES
)
async def warn(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = None,
    dm: app_commands.Choice[str] = None
):

    if not is_staff(interaction):
        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can give warnings.",
            ephemeral=True
        )
        return

    if member.bot:
        await interaction.response.send_message("❌ Bots can't be warned.", ephemeral=True)
        return

    suspension = get_active_suspension(interaction.guild.id, member.id)

    if suspension:
        await interaction.response.send_message(
            f"❌ **{member.display_name}** is already suspended until {time_tag(suspension['ends_at'])}.",
            ephemeral=True
        )
        return

    send_dm = not dm or dm.value == "yes"
    count = len(active_warnings(interaction.guild.id, member.id)) + 1
    reason = reason.strip()[:500] if reason and reason.strip() else None
    preview_end = int(time.time()) + SUSPENSION_DAYS * 86400

    lines = [
        f"This will be warning **{count}/{MAX_WARNINGS}** for **{member.display_name}**."
    ]

    if count >= MAX_WARNINGS:
        lines += [
            "",
            "**It will also:**",
            f"⛔ suspend them for {SUSPENSION_DAYS} days (no sign-ups)",
            "↩️ take them off any sign-up list that's still open",
        ]

    lines += [
        "",
        "📨 They'll get this DM:" if send_dm else "🔕 **No DM will be sent.** This is what it would have said:",
    ]

    await interaction.response.send_message(
        content="\n".join(lines),
        embed=create_warning_dm_embed(count, preview_end, reason),
        view=WarnConfirmView(interaction.user.id, member, reason, send_dm),
        ephemeral=True
    )


@bot.tree.command(
    name="removewarning",
    description="Take the most recent warning off a fighter."
)
@app_commands.describe(
    member="Whose warning to remove"
)
async def removewarning(
    interaction: discord.Interaction,
    member: discord.Member
):

    if not is_staff(interaction):
        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can remove warnings.",
            ephemeral=True
        )
        return

    if get_active_suspension(interaction.guild.id, member.id):
        await interaction.response.send_message(
            f"❌ **{member.display_name}** is suspended. Use `/unsuspend` to end the "
            "suspension (that also clears their warnings).",
            ephemeral=True
        )
        return

    warnings_now = active_warnings(interaction.guild.id, member.id)

    if not warnings_now:
        await interaction.response.send_message(
            f"**{member.display_name}** has no warnings.",
            ephemeral=True
        )
        return

    discipline_query(
        "UPDATE warnings SET active = 0 WHERE id = ?",
        (warnings_now[-1]["id"],),
        write=True
    )

    left = len(warnings_now) - 1

    await interaction.response.send_message(
        f"✅ Removed a warning from **{member.display_name}**. They now have "
        f"**{left}/{MAX_WARNINGS}**.",
        ephemeral=True
    )

    await post_staff_log(
        interaction.guild,
        staff_log_embed(
            "↩️ Warning removed",
            [f"<@{interaction.user.id}> removed a warning from <@{member.id}>: now **{left}/{MAX_WARNINGS}**."]
        )
    )


@bot.tree.command(
    name="unsuspend",
    description="End a fighter's suspension early and clear their warnings."
)
@app_commands.describe(
    member="Who to unsuspend"
)
async def unsuspend(
    interaction: discord.Interaction,
    member: discord.Member
):

    if not is_staff(interaction):
        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can end suspensions.",
            ephemeral=True
        )
        return

    suspension = get_active_suspension(interaction.guild.id, member.id)

    if not suspension:
        await interaction.response.send_message(
            f"**{member.display_name}** isn't suspended.",
            ephemeral=True
        )
        return

    discipline_query(
        "UPDATE suspensions SET status = 'lifted' WHERE id = ?",
        (suspension["id"],),
        write=True
    )

    clear_warnings(interaction.guild.id, member.id)

    await interaction.response.send_message(
        f"✅ **{member.display_name}** is no longer suspended and can sign up again. "
        f"Warnings cleared (0/{MAX_WARNINGS}).",
        ephemeral=True
    )

    await post_staff_log(
        interaction.guild,
        staff_log_embed(
            "✅ Suspension ended early",
            [f"<@{interaction.user.id}> ended <@{member.id}>'s suspension. Warnings cleared."]
        )
    )


@bot.tree.command(
    name="warnings",
    description="See a fighter's warnings, or everyone who has warnings or is suspended."
)
@app_commands.describe(
    member="Leave empty to see everyone with warnings"
)
async def warnings_command(
    interaction: discord.Interaction,
    member: discord.Member = None
):

    if not is_staff(interaction):
        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can see warnings.",
            ephemeral=True
        )
        return

    guild = interaction.guild

    if member:

        suspension = get_active_suspension(guild.id, member.id)
        history = discipline_query(
            "SELECT * FROM warnings WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT 15",
            (guild.id, member.id)
        )
        active = len(active_warnings(guild.id, member.id))

        lines = []

        if suspension:
            lines.append(f"⛔ **Suspended** until {time_tag(suspension['ends_at'])}")

        lines.append(f"⚠️ **{active}/{MAX_WARNINGS}** warnings right now")
        lines.append("")

        if history:
            lines.append("**History**")
            for row in history:
                status = "" if row["active"] else " *(cleared)*"
                note = f": {row['reason']}" if row["reason"] else ""
                lines.append(f"• <t:{row['created_at']}:d> by <@{row['warned_by']}>{note}{status}")
        else:
            lines.append("No warnings ever given.")

        embed = discord.Embed(
            title=member.display_name,
            description="\n".join(lines)[:4000],
            color=PFO_GOLD,
            timestamp=discord.utils.utcnow()
        )

    else:

        suspended = all_active_suspensions(guild.id)
        suspended_ids = {row["user_id"] for row in suspended}
        warned = discipline_query("""
            SELECT user_id, COUNT(*) AS total FROM warnings
            WHERE guild_id = ? AND active = 1
            GROUP BY user_id ORDER BY total DESC
        """, (guild.id,))

        lines = []

        if suspended:
            lines.append(f"**⛔ Suspended ({len(suspended)})**")
            for row in suspended:
                lines.append(f"<@{row['user_id']}> · back <t:{row['ends_at']}:R>")
            lines.append("")

        on_warning = [row for row in warned if row["user_id"] not in suspended_ids]

        if on_warning:
            lines.append(f"**⚠️ On a warning ({len(on_warning)})**")
            for row in on_warning:
                lines.append(f"<@{row['user_id']}> · {row['total']}/{MAX_WARNINGS}")

        if not lines:
            lines = ["✅ Nobody has a warning or a suspension."]

        embed = discord.Embed(
            title="Warnings & Suspensions",
            description="\n".join(lines)[:4000],
            color=PFO_GOLD,
            timestamp=discord.utils.utcnow()
        )

    apply_branding(embed, author_text="PFO • WARNINGS")

    embed.set_footer(
        text=f"{MAX_WARNINGS} warnings = {SUSPENSION_DAYS}-day suspension from events",
        icon_url=asset_url(LOGO_FILE)
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


def suspended_board_lines(
    guild: discord.Guild
):

    lines = []

    for row in all_active_suspensions(guild.id):
        member = guild.get_member(row["user_id"])
        name = name_without_record(member.display_name) if member else f"<@{row['user_id']}>"
        lines.append(
            f"⛔ **{name}**  ·  back <t:{row['ends_at']}:R>\n"
            f"└ until <t:{row['ends_at']}:f>"
        )

    return lines


# ------------------------------------------------------------
# Inactivity
#
# Checks everyone with a record in their name, e.g. "Joe (3-1-0)".
# Names with N/A in them are skipped. Each time a sign-up is
# closed, anyone who didn't sign up is flagged in the staff bot
# channel with their last fight and last sign-up, worst first
# (most sign-ups missed in a row). /inactive shows the same any time.
# ------------------------------------------------------------

NA_IN_NAME = re.compile(r"n\s*/\s*a", re.IGNORECASE)


def is_tracked_fighter(
    member
):
    """Has a record in their name and isn't N/A."""

    if getattr(member, "bot", False):
        return False

    name = member.display_name or ""

    return bool(RECORD_IN_NAME.search(name)) and not NA_IN_NAME.search(name)


def parse_stamp(
    text: str
):

    try:
        stamp = datetime.fromisoformat(text)
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def activity_data(
    guild_id: int
):
    """Closed sign-ups (oldest first), who signed up to what, and last fights."""

    sessions = [
        (row["id"], parse_stamp(row["created_at"]))
        for row in discipline_query(
            "SELECT id, created_at FROM signup_sessions WHERE guild_id = ? AND active = 0 ORDER BY id ASC",
            (guild_id,)
        )
    ]

    signed = {}
    last_signup = {}

    for row in discipline_query("""
        SELECT signups.discord_user_id AS user_id, signups.session_id, signups.created_at
        FROM signups
        JOIN signup_sessions ON signup_sessions.id = signups.session_id
        WHERE signup_sessions.guild_id = ?
    """, (guild_id,)):
        signed.setdefault(row["user_id"], set()).add(row["session_id"])
        stamp = parse_stamp(row["created_at"])
        if stamp and (row["user_id"] not in last_signup or stamp > last_signup[row["user_id"]]):
            last_signup[row["user_id"]] = stamp

    last_fight = {}

    for row in discipline_query("""
        SELECT user_id, MAX(day) AS last FROM (
            SELECT winner_id AS user_id, COALESCE(fight_date, substr(created_at, 1, 10)) AS day
            FROM fights WHERE guild_id = ? AND winner_id IS NOT NULL
            UNION ALL
            SELECT loser_id AS user_id, COALESCE(fight_date, substr(created_at, 1, 10)) AS day
            FROM fights WHERE guild_id = ? AND loser_id IS NOT NULL
        ) GROUP BY user_id
    """, (guild_id, guild_id)):
        try:
            last_fight[row["user_id"]] = datetime.fromisoformat((row["last"] or "")[:10]).date()
        except Exception:
            pass

    return sessions, signed, last_signup, last_fight


def missed_in_a_row(
    member,
    sessions: list,
    signed_ids: set
):
    """Closed sign-ups missed since their last one (only ones posted after they joined)."""

    joined = getattr(member, "joined_at", None)
    missed = 0

    for session_id, created in reversed(sessions):

        if session_id in signed_ids:
            break

        if joined and created and created < joined:
            break

        missed += 1

    return missed


def find_inactive_fighters(
    guild: discord.Guild,
    minimum_missed: int = 1,
    only_session: int = None
):
    """
    [(member, missed, last_fight, last_signup)] worst first.
    only_session: just the people who didn't sign up for that one.
    """

    sessions, signed, last_signup, last_fight = activity_data(guild.id)
    suspended = {row["user_id"] for row in all_active_suspensions(guild.id)}
    found = []

    for member in guild.members:

        if not is_tracked_fighter(member) or member.id in suspended:
            continue

        signed_ids = signed.get(member.id, set())

        if only_session is not None and only_session in signed_ids:
            continue

        missed = missed_in_a_row(member, sessions, signed_ids)

        if missed < max(1, minimum_missed):
            continue

        signup_stamp = last_signup.get(member.id)
        found.append((
            member,
            missed,
            last_fight.get(member.id),
            signup_stamp.date() if signup_stamp else None
        ))

    oldest = datetime.min.date()

    # Most missed in a row first, then the longest since their last fight.
    found.sort(key=lambda row: (-row[1], row[2] or oldest, row[3] or oldest))

    return found


def short_date(
    day
):

    return f"{day.day} {day:%b %y}" if day else "never"


def inactive_embeds(
    guild: discord.Guild,
    found: list,
    header: str
):

    entries = []

    for member, missed, last_fight, last_signup in found:
        warns = len(active_warnings(guild.id, member.id))
        warn_tag = f"  ·  ⚠️ {warns}/{MAX_WARNINGS}" if warns else ""
        badge = "🔴" if missed >= 3 else "🟠" if missed == 2 else "🟡"
        entries.append(
            f"{badge} **{name_without_record(member.display_name)}**  ·  missed {missed}{warn_tag}\n"
            f"└ 🥊 {short_date(last_fight)}  ·  📝 {short_date(last_signup)}"
        )

    pages = []
    current = [header, ""]

    for entry in entries:
        if len("\n".join(current)) + len(entry) > 3800:
            pages.append(current)
            current = []
        current.append(entry)

    pages.append(current)
    pages = pages[:10]

    embeds = []

    for index, page in enumerate(pages):

        embed = discord.Embed(
            title="Inactive Fighters" if index == 0 else None,
            description="\n".join(page),
            color=PFO_GOLD
        )

        if index == 0:
            apply_branding(embed, banner_file=INACTIVE_BANNER, author_text="PFO • INACTIVITY")

        if index == len(pages) - 1:
            embed.timestamp = discord.utils.utcnow()
            embed.set_footer(
                text="Missed = sign-ups missed in a row (🔴 3+ 🟠 2 🟡 1) • 🥊 last fight • 📝 last sign-up",
                icon_url=asset_url(LOGO_FILE)
            )

        embeds.append(embed)

    return embeds


class MentionsCopyView(
    discord.ui.View
):

    def __init__(
        self,
        user_ids: list
    ):

        super().__init__(
            timeout=900
        )

        self.user_ids = user_ids

        copy = discord.ui.Button(label="Copy @mentions", emoji="📋", style=discord.ButtonStyle.blurple)
        copy.callback = self.copy
        self.add_item(copy)

    async def copy(self, interaction):
        await send_card_text(
            interaction,
            " ".join(f"<@{user_id}>" for user_id in self.user_ids)
        )


@bot.tree.command(
    name="inactive",
    description="Fighters (record in their name) who've been missing sign-ups, worst first."
)
@app_commands.describe(
    missed="Only show people who missed at least this many sign-ups in a row (default 1)"
)
async def inactive(
    interaction: discord.Interaction,
    missed: app_commands.Range[int, 1, 50] = None
):

    if not is_staff(interaction):
        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can check inactivity.",
            ephemeral=True
        )
        return

    missed = missed or 1
    found = find_inactive_fighters(interaction.guild, missed)

    header = (
        f"**{len(found)} fighter{'s' if len(found) != 1 else ''}** missed "
        + ("the last sign-up" if missed == 1 else f"the last {missed}+ sign-ups")
        + " (closed sign-ups only)."
        if found else
        "✅ Every fighter signed up to the last closed sign-up." if missed == 1 else
        f"✅ Nobody has missed {missed}+ sign-ups in a row."
    )

    embeds = inactive_embeds(interaction.guild, found, header)

    if found:
        await interaction.response.send_message(
            embeds=embeds,
            view=MentionsCopyView([member.id for member, *_ in found]),
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            embeds=embeds,
            ephemeral=True
        )


async def report_missed_signup(
    guild: discord.Guild,
    session_id: int,
    title: str
):
    """After a sign-up closes: who didn't sign up, worst first, in the staff bot channel."""

    if not INACTIVE_REPORT:
        return False

    key = f"missed_report_{session_id}"

    if get_meta(key):
        return False

    channel = find_suggestions_channel(guild)

    if not channel:
        return False

    set_meta(key, "1")

    found = find_inactive_fighters(guild, 1, only_session=session_id)

    if not found:
        header = f"✅ Every fighter signed up for **{title}**."
    else:
        header = (
            f"**{len(found)} fighter{'s' if len(found) != 1 else ''}** didn't sign up for "
            f"**{title}**. Worst first."
        )

    try:
        await channel.send(
            content=f"📋 **Sign-up check: {title}** (run `/inactive` for a list you can copy @mentions from)",
            embeds=inactive_embeds(guild, found, header)
        )
        return True
    except Exception as error:
        print(f"Missed sign-up report failed: {error}")
        return False


async def end_due_suspensions():

    now = int(time.time())

    for row in discipline_query(
        "SELECT * FROM suspensions WHERE status = 'active' AND ends_at <= ?", (now,)
    ):

        discipline_query("UPDATE suspensions SET status = 'ended' WHERE id = ?", (row["id"],), write=True)
        clear_warnings(row["guild_id"], row["user_id"])

        guild = bot.get_guild(row["guild_id"])

        if guild:
            await post_staff_log(
                guild,
                staff_log_embed(
                    "✅ Suspension over",
                    [
                        f"<@{row['user_id']}>'s suspension has ended. They can sign up again "
                        f"and their warnings are back to 0/{MAX_WARNINGS}.",
                    ]
                )
            )


async def run_discipline_checks():

    try:
        await end_due_suspensions()
    except Exception as error:
        print(f"Suspension check error: {error}")




# ============================================================
# AVAILABLE FIGHTER LIST
#
# /listpaste      post the available fighter list in this channel
# /addfighter     add a fighter to a weight class
# /removefighter  take a fighter off (e.g. once someone picks them)
#
# The last list posted with /listpaste updates by itself whenever
# a fighter is added or removed. Staff only.
# ============================================================

FIGHTER_LIST_BANNER = "available_fighters.gif"

# Lightest first, like the list in the server.
LIST_WEIGHTS = [weight for weight in reversed(list(WEIGHTS.keys())) if weight != P4P_WEIGHT]

# The list as it was when this feature was added. Only used once,
# to fill the list the first time (it's in the database after that).
STARTING_FIGHTER_LIST = {
    "Flyweight": [
        "Asu Almabayev", "Kai Asakura", "Steve Erceg", "Kai Kara-France", "Alex Perez",
        "Lone’er Kavanagh", "Tagir Ulambekov", "Charles Johnson",
    ],
    "Bantamweight": [
        "Amir Albazi", "Mario Bautista", "Deiveson Figueiredo", "Rob Font", "Charles Jourdain",
        "Jonathan Martinez", "Pedro Munhoz", "Kyler Phillips", "Ricky Simeon", "Aiemann Zahabi",
        "Raul Rosas JR", "Marlon Vera", "Payton Talbott",
    ],
    "Featherweight": [
        "Rafa Garcia", "Calvin Kattar", "Lerone Murphy", "David Onama", "Brian Ortega",
        "Cub Swanson", "Sodiq Yusuff", "Steve Garcia", "Jean Silva", "Arnold Allen",
        "Sean Woodson", "Yair Rodriguez", "Aaron Pico", "The Korean Zombie",
    ],
    "Lightweight": [
        "Ignacio Bahamondes", "Beneil Dariush", "Grant Dawson", "Frankie Edgar", "Rafael Fiziev",
        "Matt Frevola", "Clay Guida", "Alexander Hernandez", "Dan Hooker", "Chase Hooper",
        "Michael Johnson", "Jim Miller", "Renato Moicano", "Esteban Ribovics", "Benoit Saint Denis",
        "Jeremy Stephens", "Manuel Torres", "Daniel Zellhuber", "Islam Makhachev",
    ],
    "Welterweight": [
        "Randy Brown", "Joaquin Buckley", "Gilbert Burns", "Michael Chiesa", "Chris Curtis",
        "Rafael dos Anjos", "Mickey Gall", "Jack Hermansson", "Matt Hughes", "Li Jingliang",
        "Robbie Lawler", "Kevin Lee", "Vicente Luque", "Neil Magny", "Mike Malott",
        "Jorge Masvidal", "Jake Matthews", "Court McGee", "Geoff Neal", "Gunnar Nelson",
        "Santiago Ponzinibbio", "Daniel Rodriguez", "Colby Covington", "Uros Medic",
    ],
    "Middleweight": [
        "Brendan Allen", "Vitor Belfort", "Michael Bisping", "Jared Cannonier", "Roman Dolidze",
        "Kelvin Gastelum", "Royce Gracie", "Anthony Hernandez", "Roman Kopylov", "Michel Pereira",
        "Joe Pyfer", "Luke Rockhold", "Gregory Rodrigues", "Edmen Shahbazyan", "Wanderlei Silva",
        "Chael Sonnen", "Brad Tavares", "Marvin Vettori", "Chris Weidman", "Caio Borrahlo",
        "Paulo Costa", "Dricus Du Plessis", "Sharaputdin Magomedov", "Israel Adesanya",
    ],
    "Light Heavyweight": [
        "Mark Coleman", "Paul Craig", "Forrest Griffin", "Bogdan Guskov", "Nikita Krylov",
        "Lyoto Machida", "Azamat Murzakanov", "Volkan Oezdemir", "Tito Ortiz", "Shogun Rua",
        "Ovince Saint Preux", "Ken Shamrock", "Anthony Smith", "Alonzo Menifield",
        "Glover Teixeira", "Rumble Johnson", "Jan Blachowicz", "Dominick Reyes",
    ],
    "Heavyweight": [
        "Andrei Arlovski", "Randy Couture", "Rashad Evans", "Mark Kerr", "Stipe Miocic",
        "Vitor Petrino", "Jairzinho Rozenstruik", "Dan Severn", "Serghei Spivac", "Marcin Tybura",
        "Sergei Pavlovich", "Aleksander Rakic", "Derrick Lewis", "Daniel Cormier",
    ],
}

LIST_WEIGHT_CHOICES = [
    app_commands.Choice(name=weight, value=weight)
    for weight in LIST_WEIGHTS
]


def list_key(
    name: str
):
    """For matching names: ignores case, spaces, dots, accents-ish punctuation."""

    return "".join(ch for ch in name.casefold() if ch.isalnum())


def setup_fighter_list_tables():

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS available_fighters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            weight TEXT NOT NULL,
            name TEXT NOT NULL,
            added_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS fighter_list_messages (
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL
        )
    """)

    db.commit()
    db.close()


def get_fighter_list(
    guild_id: int
):
    """{weight: [rows]} in list order. Fills the starting list the first time."""

    if not get_meta(f"fighter_list_seeded_{guild_id}"):

        db = get_db()
        cursor = db.cursor()
        cursor.execute("SELECT COUNT(*) AS n FROM available_fighters WHERE guild_id = ?", (guild_id,))

        if cursor.fetchone()["n"] == 0:
            cursor.executemany("""
                INSERT INTO available_fighters (guild_id, weight, name, added_at)
                VALUES (?, ?, ?, ?)
            """, [
                (guild_id, weight, name, now_utc())
                for weight in LIST_WEIGHTS
                for name in STARTING_FIGHTER_LIST.get(weight, [])
            ])

        db.commit()
        db.close()

        set_meta(f"fighter_list_seeded_{guild_id}", "1")

    rows = discipline_query(
        "SELECT * FROM available_fighters WHERE guild_id = ? ORDER BY id ASC",
        (guild_id,)
    )

    grouped = {weight: [] for weight in LIST_WEIGHTS}

    for row in rows:
        grouped.setdefault(row["weight"], []).append(row)

    return grouped


def find_listed_fighter(
    guild_id: int,
    name: str,
    weight: str = None
):

    key = list_key(name)

    return [
        row
        for listed_weight, rows in get_fighter_list(guild_id).items()
        for row in rows
        if list_key(row["name"]) == key and (not weight or listed_weight == weight)
    ]


def closest_listed_names(
    guild_id: int,
    name: str,
    limit: int = 3
):

    names = [row["name"] for rows in get_fighter_list(guild_id).values() for row in rows]
    by_key = {list_key(listed): listed for listed in names}

    return [
        by_key[key]
        for key in difflib.get_close_matches(list_key(name), list(by_key.keys()), n=limit, cutoff=0.6)
    ]


def fighter_list_embed(
    guild_id: int
):

    grouped = get_fighter_list(guild_id)
    total = sum(len(rows) for rows in grouped.values())

    embed = discord.Embed(
        title="Available Fighters",
        description=(
            f"**{total}** fighters available. Ask a member of staff to claim one."
            if total else "No fighters are available right now."
        ),
        color=PFO_GOLD,
        timestamp=discord.utils.utcnow()
    )

    size = len(embed.title) + len(embed.description)

    for weight in LIST_WEIGHTS:

        names = [row["name"] for row in grouped.get(weight, [])]

        if not names:
            continue

        # A field holds 1024 characters; long classes carry on in a second field.
        chunks = [[]]

        for name in names:
            if len("\n".join(f"- {n}" for n in chunks[-1] + [name])) > 1000:
                chunks.append([])
            chunks[-1].append(name)

        for index, chunk in enumerate(chunks):

            field_name = f"🥊 {weight} ({len(names)})" if index == 0 else f"🥊 {weight} (cont.)"
            value = "\n".join(f"- {name}" for name in chunk)

            if size + len(field_name) + len(value) > 5700 or len(embed.fields) >= 25:
                embed.add_field(
                    name="…",
                    value="The list is too long for one message. Remove some fighters to show the rest.",
                    inline=False
                )
                break

            size += len(field_name) + len(value)
            embed.add_field(name=field_name, value=value, inline=False)

    apply_branding(
        embed,
        banner_file=FIGHTER_LIST_BANNER,
        author_text="PFO • AVAILABLE FIGHTERS"
    )

    embed.set_footer(
        text="Updates by itself when staff add or remove fighters",
        icon_url=asset_url(LOGO_FILE)
    )

    return embed


async def refresh_fighter_list_message(
    guild: discord.Guild
):
    """Edits the last list posted with /listpaste. Returns True if it did."""

    rows = discipline_query(
        "SELECT * FROM fighter_list_messages WHERE guild_id = ?", (guild.id,)
    )

    if not rows:
        return False

    try:
        channel = guild.get_channel(rows[0]["channel_id"])

        if not channel:
            return False

        message = await channel.fetch_message(rows[0]["message_id"])
        await message.edit(embed=fighter_list_embed(guild.id))
        return True

    except Exception as error:
        print(f"Could not update the fighter list message: {error}")
        return False


@bot.tree.command(
    name="listpaste",
    description="Post the available fighter list here (it then updates by itself)."
)
async def listpaste(
    interaction: discord.Interaction
):

    if not is_staff(interaction):
        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can post the fighter list.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(embed=fighter_list_embed(interaction.guild.id))

    try:
        message = await interaction.original_response()

        discipline_query("""
            INSERT OR REPLACE INTO fighter_list_messages (guild_id, channel_id, message_id)
            VALUES (?, ?, ?)
        """, (interaction.guild.id, message.channel.id, message.id), write=True)

    except Exception as error:
        print(f"Could not remember the fighter list message: {error}")


@bot.tree.command(
    name="addfighter",
    description="Add a fighter to the available fighter list."
)
@app_commands.describe(
    name="Fighter's name, e.g. Alex Pereira",
    weight="Weight class to list them under"
)
@app_commands.choices(
    weight=LIST_WEIGHT_CHOICES
)
async def addfighter(
    interaction: discord.Interaction,
    name: str,
    weight: app_commands.Choice[str]
):

    if not is_staff(interaction):
        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can edit the fighter list.",
            ephemeral=True
        )
        return

    name = " ".join(name.split())[:80]

    if not list_key(name):
        await interaction.response.send_message("❌ Type the fighter's name.", ephemeral=True)
        return

    existing = find_listed_fighter(interaction.guild.id, name)

    if existing:
        await interaction.response.send_message(
            f"❌ **{existing[0]['name']}** is already on the list under **{existing[0]['weight']}**.",
            ephemeral=True
        )
        return

    discipline_query("""
        INSERT INTO available_fighters (guild_id, weight, name, added_at)
        VALUES (?, ?, ?, ?)
    """, (interaction.guild.id, weight.value, name, now_utc()), write=True)

    # Editing the posted list can take a moment, so answer after it.
    await interaction.response.defer(ephemeral=True)

    updated = await refresh_fighter_list_message(interaction.guild)
    count = len(get_fighter_list(interaction.guild.id)[weight.value])

    await interaction.followup.send(
        f"✅ Added **{name}** to **{weight.value}** ({count} available)."
        + ("\n📋 The posted list has been updated." if updated else
           "\nℹ️ Post the list with `/listpaste` and it'll update by itself from then on."),
        ephemeral=True
    )


async def listed_fighter_autocomplete(
    interaction: discord.Interaction,
    current: str
):

    key = list_key(current or "")
    found = []

    for weight, rows in get_fighter_list(interaction.guild.id).items():
        for row in rows:
            if key in list_key(row["name"]):
                found.append(app_commands.Choice(name=f"{row['name']} ({weight})"[:100], value=row["name"][:100]))

    return found[:25]


@bot.tree.command(
    name="removefighter",
    description="Take a fighter off the available fighter list (e.g. once they're picked)."
)
@app_commands.describe(
    name="Start typing to pick from the list",
    weight="Only needed if the same name is listed in two weight classes"
)
@app_commands.choices(
    weight=LIST_WEIGHT_CHOICES
)
@app_commands.autocomplete(
    name=listed_fighter_autocomplete
)
async def removefighter(
    interaction: discord.Interaction,
    name: str,
    weight: app_commands.Choice[str] = None
):

    if not is_staff(interaction):
        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can edit the fighter list.",
            ephemeral=True
        )
        return

    matches = find_listed_fighter(interaction.guild.id, name, weight.value if weight else None)

    if not matches and weight:
        elsewhere = find_listed_fighter(interaction.guild.id, name)

        if elsewhere:
            await interaction.response.send_message(
                f"❌ **{elsewhere[0]['name']}** is listed under **{elsewhere[0]['weight']}**, "
                f"not {weight.value}.",
                ephemeral=True
            )
            return

    if not matches:
        close = closest_listed_names(interaction.guild.id, name)
        await interaction.response.send_message(
            f"❌ **{name}** isn't on the list"
            + (f" under **{weight.value}**" if weight else "") + "."
            + (f" Did you mean: {', '.join(f'**{n}**' for n in close)}?" if close else ""),
            ephemeral=True
        )
        return

    if len(matches) > 1:
        await interaction.response.send_message(
            f"❌ **{matches[0]['name']}** is listed in "
            + ", ".join(f"**{row['weight']}**" for row in matches)
            + ". Pick the **weight** too.",
            ephemeral=True
        )
        return

    row = matches[0]

    discipline_query("DELETE FROM available_fighters WHERE id = ?", (row["id"],), write=True)

    await interaction.response.defer(ephemeral=True)

    updated = await refresh_fighter_list_message(interaction.guild)
    count = len(get_fighter_list(interaction.guild.id)[row["weight"]])

    await interaction.followup.send(
        f"✅ Removed **{row['name']}** from **{row['weight']}** ({count} left)."
        + ("\n📋 The posted list has been updated." if updated else ""),
        ephemeral=True
    )



# ============================================================
# TITLE HISTORY AND EVENT RECAPS
#
# /titlehistory  champions of each division, worked out from fights
#                logged with title_fight: True
# /recap         a results recap for an event. Opens an editor first
#                so you can put fights in Main Event / Co-Main /
#                Main Card / Prelims / Early Prelims (or leave them
#                out), add notes, then post it.
# ============================================================

RECAP_BANNER = "recap.gif"
TITLE_HISTORY_BANNER = "title_history.gif"

RECAP_SECTIONS = ["Main Event", "Co-Main Event", "Main Card", "Prelims", "Early Prelims"]
RECAP_HIDDEN = "Hidden"
RECAP_SECTION_ICONS = {
    "Main Event": "🏆",
    "Co-Main Event": "⭐",
    "Main Card": "🥊",
    "Prelims": "🔸",
    "Early Prelims": "▫️",
    RECAP_HIDDEN: "🚫",
}


def setup_recap_tables():

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS recap_layouts (
            guild_id INTEGER NOT NULL,
            event_key TEXT NOT NULL,
            fight_id INTEGER NOT NULL,
            section TEXT NOT NULL,
            position INTEGER NOT NULL,

            PRIMARY KEY(guild_id, event_key, fight_id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS recap_details (
            guild_id INTEGER NOT NULL,
            event_key TEXT NOT NULL,
            title TEXT,
            notes TEXT,

            PRIMARY KEY(guild_id, event_key)
        )
    """)

    db.commit()
    db.close()


def short_day(
    text: str
):

    try:
        day = datetime.fromisoformat((text or "")[:10]).date()
        return f"{day.day} {day:%b %y}"
    except Exception:
        return "?"


def fight_day(
    fight
):

    return fight["fight_date"] or (fight["created_at"] or "")[:10]


def fighter_key(
    user_id,
    name: str
):

    return f"id:{user_id}" if user_id else f"name:{normalise_name(name or '')}"


def fight_display_name(
    guild: discord.Guild,
    user_id,
    stored_name: str
):
    """Their name as it is now (without the record), or the saved name."""

    member = guild.get_member(user_id) if (guild and user_id) else None

    return name_without_record(member.display_name if member else (stored_name or "Unknown"))


# ------------------------------------------------------------
# Title history
# ------------------------------------------------------------

def title_lineage(
    guild_id: int,
    division: str
):
    """
    Reigns oldest first, plus what each title fight meant.

    reigns:   [{key, user_id, name, won_fight, start, end, end_fight, defences}]
    outcomes: {fight_id: "new" | "defence" | "first" | "draw"}
    """

    fights = discipline_query("""
        SELECT * FROM fights
        WHERE guild_id = ? AND title_fight = 1 AND division = ?
        ORDER BY COALESCE(fight_date, substr(created_at, 1, 10)) ASC, id ASC
    """, (guild_id, division))

    reigns = []
    outcomes = {}
    current = None

    for fight in fights:

        if fight["method"] in ("DRAW", "NC"):
            outcomes[fight["id"]] = "draw"
            continue

        winner = fighter_key(fight["winner_id"], fight["winner_name"])

        if current and current["key"] == winner:
            current["defences"] += 1
            outcomes[fight["id"]] = "defence"
            continue

        if current:
            current["end"] = fight_day(fight)
            current["end_fight"] = fight

        outcomes[fight["id"]] = "new" if current else "first"

        current = {
            "key": winner,
            "user_id": fight["winner_id"],
            "name": fight["winner_name"],
            "won_fight": fight,
            "start": fight_day(fight),
            "end": None,
            "end_fight": None,
            "defences": 0,
        }

        reigns.append(current)

    return reigns, outcomes


def all_title_outcomes(
    guild_id: int
):

    outcomes = {}

    for division in DIVISION_NAMES:
        outcomes.update(title_lineage(guild_id, division)[1])

    return outcomes


def reign_days(
    reign
):

    try:
        start = datetime.fromisoformat(reign["start"]).date()
        end = datetime.fromisoformat(reign["end"]).date() if reign["end"] else datetime.now(timezone.utc).date()
        return (end - start).days
    except Exception:
        return None


def plural(
    count: int,
    word: str
):

    if count == 1:
        return f"{count} {word}"

    return f"{count} {word}{'es' if word.endswith(('sh', 'ch', 's', 'x')) else 's'}"


def title_summary_for_member(
    guild_id: int,
    user_id: int,
    name: str = None
):
    """Lines for /profile, e.g. 'Lightweight: 2× champion, 5 title defences'."""

    key = fighter_key(user_id, name)
    lines = []

    for division in DIVISION_NAMES:

        reigns = [reign for reign in title_lineage(guild_id, division)[0] if reign["key"] == key]

        if not reigns:
            continue

        defences = sum(reign["defences"] for reign in reigns)
        times = f"{len(reigns)}× champion" if len(reigns) > 1 else "champion"
        status = "" if reigns[-1]["end"] is None else " (former)"

        lines.append(f"🏅 {division} {times}{status} · {plural(defences, 'defence')}")

    return lines


@bot.tree.command(
    name="titlehistory",
    description="Champions of each division, from title fights logged with /result."
)
@app_commands.describe(
    division="Pick a division for its full list of champions (empty = every division)"
)
@app_commands.choices(
    division=DIVISION_CHOICES
)
async def titlehistory(
    interaction: discord.Interaction,
    division: app_commands.Choice[str] = None
):

    if not is_staff(interaction):
        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can post title history.",
            ephemeral=True
        )
        return

    guild = interaction.guild

    embed = discord.Embed(
        color=PFO_GOLD,
        timestamp=discord.utils.utcnow()
    )

    if division:

        reigns, _ = title_lineage(guild.id, division.value)
        embed.title = f"👑 {division.value} Title History"

        if not reigns:
            embed.description = (
                f"No {division.value} title fights have been logged yet.\n"
                "Log them with `/result` and **title_fight: True**."
            )
        else:
            lines = []

            for number, reign in enumerate(reigns, start=1):

                name = fight_display_name(guild, reign["user_id"], reign["name"])
                fight = reign["won_fight"]
                beat = fight_display_name(guild, fight["loser_id"], fight["loser_name"])
                days = reign_days(reign)
                length = f" · {plural(days, 'day')}" if days is not None else ""

                if reign["end"] is None:
                    lines.append(f"👑 **{name}** · *current champion*")
                else:
                    lines.append(f"`{number:>2}.` **{name}**")

                lines.append(
                    f"└ Won {short_day(reign['start'])} vs {beat} ({fight_method_text(fight)})"
                    f" · {plural(reign['defences'], 'defence')}{length}"
                )

            embed.description = "\n".join(lines)[:4000]

            current = reigns[-1]
            ranked = [
                row for row in get_division_rankings(guild.id, division.value)
                if row["rank"] == 0
            ]

            if ranked and ranked[0]["discord_user_id"] != current["user_id"]:
                embed.add_field(
                    name="ℹ️ Rankings don't match",
                    value=(
                        f"The rankings list <@{ranked[0]['discord_user_id']}> as champion. "
                        "Log their title fight with `/result` (title_fight: True) to add it here."
                    ),
                    inline=False
                )

    else:

        embed.title = "👑 Title History"
        any_found = False

        for weight in DIVISION_NAMES:

            reigns, _ = title_lineage(guild.id, weight)

            if not reigns:
                continue

            any_found = True
            current = reigns[-1]
            name = fight_display_name(guild, current["user_id"], current["name"])

            embed.add_field(
                name=f"{weight}",
                value=(
                    f"👑 **{name}** · {plural(current['defences'], 'defence')} · "
                    f"since {short_day(current['start'])}\n"
                    f"└ {plural(len(reigns), 'champion')} in total"
                ),
                inline=False
            )

        embed.description = (
            "Current champions and how many there have been. "
            "Pick a **division** for its full list."
            if any_found else
            "No title fights have been logged yet. Log them with `/result` and **title_fight: True**."
        )

    apply_branding(
        embed,
        banner_file=TITLE_HISTORY_BANNER,
        author_text="PFO • TITLE HISTORY"
    )

    embed.set_footer(
        text="From fights logged with title_fight: True • a draw or no contest keeps the champion",
        icon_url=asset_url(LOGO_FILE)
    )

    await interaction.response.send_message(embed=embed)


# ------------------------------------------------------------
# Event recaps
# ------------------------------------------------------------

def event_key(
    event: str
):

    return " ".join((event or "").split()).casefold()


def event_fights(
    guild_id: int,
    event: str
):
    """Fights logged under this event name (any capitals/spacing), in the order they happened."""

    key = event_key(event)

    return [
        fight for fight in discipline_query("""
            SELECT * FROM fights WHERE guild_id = ? AND event IS NOT NULL
            ORDER BY COALESCE(fight_date, substr(created_at, 1, 10)) ASC, id ASC
        """, (guild_id,))
        if event_key(fight["event"]) == key
    ]


def recent_events(
    guild_id: int
):
    """Event names, most recent first."""

    seen = {}

    for row in discipline_query("""
        SELECT event, MAX(id) AS last FROM fights
        WHERE guild_id = ? AND event IS NOT NULL AND TRIM(event) != ''
        GROUP BY event ORDER BY last DESC
    """, (guild_id,)):
        seen.setdefault(event_key(row["event"]), (row["last"], row["event"]))

    return [name for _, name in sorted(seen.values(), reverse=True)]


def default_recap_layout(
    fights: list
):
    """Last fight = Main Event, then Co-Main, next 3 Main Card, the rest Prelims."""

    layout = {}
    newest_first = list(reversed(fights))

    for index, fight in enumerate(newest_first):

        if index == 0:
            section = "Main Event"
        elif index == 1:
            section = "Co-Main Event"
        elif index <= 4:
            section = "Main Card"
        else:
            section = "Prelims"

        layout[fight["id"]] = [section, index]

    return layout


def load_recap_layout(
    guild_id: int,
    event: str,
    fights: list
):

    saved = {
        row["fight_id"]: [row["section"], row["position"]]
        for row in discipline_query(
            "SELECT * FROM recap_layouts WHERE guild_id = ? AND event_key = ?",
            (guild_id, event_key(event))
        )
    }

    layout = default_recap_layout(fights)

    # Saved spots win; fights logged since get the default spot at the end.
    for fight_id in layout:
        if fight_id in saved:
            layout[fight_id] = saved[fight_id]
        elif saved:
            layout[fight_id] = [layout[fight_id][0], 10_000 + layout[fight_id][1]]

    details = discipline_query(
        "SELECT * FROM recap_details WHERE guild_id = ? AND event_key = ?",
        (guild_id, event_key(event))
    )

    title = details[0]["title"] if details and details[0]["title"] else None
    notes = details[0]["notes"] if details else None

    return layout, title, notes


def save_recap(
    guild_id: int,
    event: str,
    layout: dict,
    title: str,
    notes: str
):

    key = event_key(event)
    db = get_db()
    cursor = db.cursor()

    cursor.execute("DELETE FROM recap_layouts WHERE guild_id = ? AND event_key = ?", (guild_id, key))
    cursor.executemany("""
        INSERT INTO recap_layouts (guild_id, event_key, fight_id, section, position)
        VALUES (?, ?, ?, ?, ?)
    """, [(guild_id, key, fight_id, section, position) for fight_id, (section, position) in layout.items()])

    cursor.execute("""
        INSERT OR REPLACE INTO recap_details (guild_id, event_key, title, notes)
        VALUES (?, ?, ?, ?)
    """, (guild_id, key, title, notes))

    db.commit()
    db.close()


def recap_fight_line(
    guild: discord.Guild,
    fight,
    outcome: str = None
):

    winner = fight_display_name(guild, fight["winner_id"], fight["winner_name"])
    loser = fight_display_name(guild, fight["loser_id"], fight["loser_name"])
    method = fight["method"]
    belt = "👑 " if fight["title_fight"] else ""
    weight = f" · {fight['division']}" if fight["division"] else ""

    if method == "DRAW":
        line = f"{belt}**{winner}** vs **{loser}** · Draw{weight}"
    elif method == "NC":
        line = f"{belt}**{winner}** vs **{loser}** · No Contest{weight}"
    else:
        line = f"{belt}**{winner}** def. **{loser}** · {fight_method_text(fight)}{weight}"

    division = fight["division"] or "the"

    if outcome == "new":
        line += f"\n└ 🆕 **New {division} Champion**"
    elif outcome == "first":
        line += f"\n└ 🆕 **Inaugural {division} Champion**"
    elif outcome == "defence":
        line += f"\n└ 🛡️ Retains the {division} title"
    elif outcome == "draw":
        line += "\n└ 🛡️ The champion keeps the belt"

    return line


def ordered_sections(
    layout: dict
):
    """{section: [fight_id, ...]} in card order."""

    grouped = {section: [] for section in RECAP_SECTIONS + [RECAP_HIDDEN]}

    for fight_id, (section, position) in sorted(layout.items(), key=lambda item: item[1][1]):
        grouped.setdefault(section, []).append(fight_id)

    return grouped


def recap_embed(
    guild: discord.Guild,
    event: str,
    fights: list,
    layout: dict,
    title: str = None,
    notes: str = None
):
    """The recap as it's posted."""

    by_id = {fight["id"]: fight for fight in fights}
    outcomes = all_title_outcomes(guild.id)
    grouped = ordered_sections(layout)
    shown = [fight_id for section in RECAP_SECTIONS for fight_id in grouped[section]]

    finishes = sum(1 for fight_id in shown if by_id[fight_id]["method"] in ("KO/TKO", "SUB"))
    new_champs = sum(1 for fight_id in shown if outcomes.get(fight_id) in ("new", "first"))

    summary = f"**{plural(len(shown), 'fight')}** · **{plural(finishes, 'finish')}**"

    if new_champs:
        summary += f" · 👑 **{plural(new_champs, 'new champion')}**"

    embed = discord.Embed(
        title=title or event,
        description=((notes.strip() + "\n\n") if notes and notes.strip() else "") + summary,
        color=PFO_GOLD,
        timestamp=discord.utils.utcnow()
    )

    size = len(embed.title) + len(embed.description)

    for section in RECAP_SECTIONS:

        lines = [recap_fight_line(guild, by_id[fight_id], outcomes.get(fight_id)) for fight_id in grouped[section]]

        if not lines:
            continue

        # Fields hold 1024 characters; long sections carry on in another.
        chunks = [[]]

        for line in lines:
            if len("\n".join(chunks[-1] + [line])) > 1000:
                chunks.append([])
            chunks[-1].append(line)

        for index, chunk in enumerate(chunks):

            name = f"{RECAP_SECTION_ICONS[section]} {section}" + (" (cont.)" if index else "")
            value = "\n".join(chunk)

            if size + len(name) + len(value) > 5700 or len(embed.fields) >= 25:
                break

            size += len(name) + len(value)
            embed.add_field(name=name, value=value, inline=False)

    apply_branding(
        embed,
        banner_file=RECAP_BANNER,
        author_text="PFO • EVENT RECAP"
    )

    embed.set_footer(
        text="Official PFO results",
        icon_url=asset_url(LOGO_FILE)
    )

    return embed


class RecapTextModal(
    discord.ui.Modal,
    title="Recap title & notes"
):

    recap_title = discord.ui.TextInput(
        label="Title",
        placeholder="e.g. PFO Fight Night 16: Van vs Lopes",
        max_length=200,
        required=False
    )

    recap_notes = discord.ui.TextInput(
        label="Notes (optional, shown under the title)",
        style=discord.TextStyle.paragraph,
        placeholder="e.g. Fight of the Night: Van vs Lopes",
        max_length=1000,
        required=False
    )

    def __init__(self, builder):
        super().__init__()
        self.builder = builder
        self.recap_title.default = builder.title or builder.event
        self.recap_notes.default = builder.notes or ""

    async def on_submit(self, interaction):
        title = (self.recap_title.value or "").strip()
        self.builder.title = title if title and title != self.builder.event else None
        self.builder.notes = (self.recap_notes.value or "").strip() or None
        self.builder.save()
        await self.builder.redraw(interaction, "✏️ Title and notes updated.")


class RecapBuilderView(
    discord.ui.View
):
    """Arrange an event's fights into card sections, then post the recap."""

    def __init__(
        self,
        guild: discord.Guild,
        event: str,
        fights: list
    ):

        super().__init__(
            timeout=3600
        )

        self.guild = guild
        self.event = event
        self.fights = fights
        self.by_id = {fight["id"]: fight for fight in fights}
        self.layout, self.title, self.notes = load_recap_layout(guild.id, event, fights)
        self.selected = []
        self.normalise()
        self.rebuild()

    # -------- state --------

    def normalise(self):
        """Renumbers positions 0..n in card order."""
        order = [
            fight_id
            for section in RECAP_SECTIONS + [RECAP_HIDDEN]
            for fight_id in ordered_sections(self.layout)[section]
        ]
        for position, fight_id in enumerate(order):
            self.layout[fight_id][1] = position

    def save(self):
        save_recap(self.guild.id, self.event, self.layout, self.title, self.notes)

    def short(self, fight_id):
        fight = self.by_id[fight_id]
        a = fight_display_name(self.guild, fight["winner_id"], fight["winner_name"])
        b = fight_display_name(self.guild, fight["loser_id"], fight["loser_name"])
        return f"{a} vs {b}"

    def number(self, fight_id):
        return sorted(self.layout, key=lambda f: self.layout[f][1]).index(fight_id) + 1

    # -------- display --------

    def embed(self):

        grouped = ordered_sections(self.layout)
        lines = []

        for section in RECAP_SECTIONS + [RECAP_HIDDEN]:

            if not grouped[section]:
                continue

            label = "Left out" if section == RECAP_HIDDEN else section
            lines.append(f"**{RECAP_SECTION_ICONS[section]} {label}**")

            for fight_id in grouped[section]:
                tick = "▶️ " if fight_id in self.selected else ""
                belt = " 👑" if self.by_id[fight_id]["title_fight"] else ""
                lines.append(f"`{self.number(fight_id):>2}` {tick}{self.short(fight_id)}{belt}")

            lines.append("")

        embed = discord.Embed(
            title=f"Recap editor: {self.title or self.event}",
            description="\n".join(lines)[:4000],
            color=PFO_GOLD
        )

        if self.notes:
            embed.add_field(name="Notes", value=self.notes[:1000], inline=False)

        apply_branding(embed, author_text="PFO • EVENT RECAP")

        embed.set_footer(
            text="1) pick fights  2) choose where they go  3) Post recap • saved as you go",
            icon_url=asset_url(LOGO_FILE)
        )

        return embed

    def rebuild(self):

        self.clear_items()
        order = sorted(self.layout, key=lambda f: self.layout[f][1])

        pick = discord.ui.Select(
            placeholder=(
                f"Selected: {', '.join('#' + str(n) for n in sorted(self.number(f) for f in self.selected))}"
                if self.selected else "1) Pick the fight(s) to move"
            ),
            min_values=1,
            max_values=min(25, len(order)),
            options=[
                discord.SelectOption(
                    label=f"#{self.number(fight_id)} {self.short(fight_id)}"[:100],
                    description=self.layout[fight_id][0] if self.layout[fight_id][0] != RECAP_HIDDEN else "Left out",
                    value=str(fight_id),
                    default=fight_id in self.selected
                )
                for fight_id in order[:25]
            ],
            row=0
        )
        pick.callback = self.pick
        self.add_item(pick)

        move = discord.ui.Select(
            placeholder="2) Move them to…",
            options=[
                discord.SelectOption(label=section, value=section, emoji=RECAP_SECTION_ICONS[section])
                for section in RECAP_SECTIONS
            ] + [discord.SelectOption(label="Leave out of the recap", value=RECAP_HIDDEN, emoji="🚫")],
            disabled=not self.selected,
            row=1
        )
        move.callback = self.move
        self.add_item(move)

        up = discord.ui.Button(label="Move up", emoji="⬆️", style=discord.ButtonStyle.grey,
                               disabled=len(self.selected) != 1, row=2)
        up.callback = self.move_up
        down = discord.ui.Button(label="Move down", emoji="⬇️", style=discord.ButtonStyle.grey,
                                 disabled=len(self.selected) != 1, row=2)
        down.callback = self.move_down
        text = discord.ui.Button(label="Title & notes", emoji="✏️", style=discord.ButtonStyle.grey, row=2)
        text.callback = self.edit_text
        reset = discord.ui.Button(label="Reset", emoji="🔄", style=discord.ButtonStyle.grey, row=2)
        reset.callback = self.reset
        for item in (up, down, text, reset):
            self.add_item(item)

        preview = discord.ui.Button(label="Preview", emoji="👁️", style=discord.ButtonStyle.blurple, row=3)
        preview.callback = self.preview
        post = discord.ui.Button(label="Post recap", emoji="📢", style=discord.ButtonStyle.green, row=3)
        post.callback = self.post
        close = discord.ui.Button(label="Close", style=discord.ButtonStyle.grey, row=3)
        close.callback = self.close
        for item in (preview, post, close):
            self.add_item(item)

    async def redraw(self, interaction, note: str = None):
        self.normalise()
        self.rebuild()
        await interaction.response.edit_message(content=note, embed=self.embed(), view=self)

    # -------- actions --------

    async def interaction_check(self, interaction):

        if is_staff(interaction):
            return True

        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can edit recaps.",
            ephemeral=True
        )
        return False

    async def pick(self, interaction):
        values = interaction.data.get("values", [])
        self.selected = [int(value) for value in values if int(value) in self.layout]
        await self.redraw(interaction)

    async def move(self, interaction):

        values = interaction.data.get("values", [])

        if not values or not self.selected:
            await self.redraw(interaction)
            return

        section = values[0]
        end = max((pos for _, pos in self.layout.values()), default=0) + 1

        # Keep their current order, put them at the bottom of the section.
        for offset, fight_id in enumerate(sorted(self.selected, key=lambda f: self.layout[f][1])):
            self.layout[fight_id] = [section, end + offset]

        moved = len(self.selected)
        self.selected = []
        self.save()

        label = "left out of the recap" if section == RECAP_HIDDEN else f"moved to **{section}**"
        await self.redraw(interaction, f"✅ {plural(moved, 'fight')} {label}.")

    async def shift(self, interaction, direction: int):

        fight_id = self.selected[0]
        section = self.layout[fight_id][0]
        same = ordered_sections(self.layout)[section]
        index = same.index(fight_id)
        other = index + direction

        if 0 <= other < len(same):
            swap = same[other]
            self.layout[fight_id][1], self.layout[swap][1] = self.layout[swap][1], self.layout[fight_id][1]
            self.save()
            await self.redraw(interaction)
        else:
            await self.redraw(interaction, f"Already at the {'top' if direction < 0 else 'bottom'} of **{section}**.")

    async def move_up(self, interaction):
        await self.shift(interaction, -1)

    async def move_down(self, interaction):
        await self.shift(interaction, 1)

    async def edit_text(self, interaction):
        await interaction.response.send_modal(RecapTextModal(self))

    async def reset(self, interaction):
        self.layout = default_recap_layout(self.fights)
        self.selected = []
        self.save()
        await self.redraw(interaction, "🔄 Back to the default order (last fight = Main Event).")

    async def preview(self, interaction):
        await interaction.response.send_message(
            content="👁️ Preview (only you can see this):",
            embed=recap_embed(self.guild, self.event, self.fights, self.layout, self.title, self.notes),
            ephemeral=True
        )

    async def post(self, interaction):

        shown = [f for f, (section, _) in self.layout.items() if section != RECAP_HIDDEN]

        if not shown:
            await self.redraw(interaction, "❌ Every fight is left out. Move at least one into a section.")
            return

        self.save()

        try:
            await interaction.channel.send(
                embed=recap_embed(self.guild, self.event, self.fights, self.layout, self.title, self.notes)
            )
        except discord.Forbidden:
            await self.redraw(interaction, "❌ I can't post in this channel. Check my permissions here.")
            return

        await self.redraw(
            interaction,
            f"📢 Recap posted by {interaction.user.display_name}. "
            "Changed something? Edit and press **Post recap** again."
        )

    async def close(self, interaction):
        self.stop()
        await interaction.response.edit_message(
            content="Closed. Your layout is saved for next time.",
            embed=None,
            view=None
        )


async def recap_event_autocomplete(
    interaction: discord.Interaction,
    current: str
):

    typed = event_key(current or "")

    return [
        app_commands.Choice(name=name[:100], value=name[:100])
        for name in recent_events(interaction.guild.id)
        if typed in event_key(name)
    ][:25]


@bot.tree.command(
    name="recap",
    description="Make an event recap: put fights in main card, prelims etc., then post it."
)
@app_commands.describe(
    event="The event name used in /result (start typing to pick)"
)
@app_commands.autocomplete(
    event=recap_event_autocomplete
)
async def recap(
    interaction: discord.Interaction,
    event: str
):

    if not is_staff(interaction):
        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can make recaps.",
            ephemeral=True
        )
        return

    fights = event_fights(interaction.guild.id, event)

    if not fights:
        names = {event_key(name): name for name in recent_events(interaction.guild.id)}
        close = difflib.get_close_matches(event_key(event), list(names), n=3, cutoff=0.5)
        await interaction.response.send_message(
            f"❌ No fights are logged under **{event}**."
            + (f" Did you mean: {', '.join(f'**{names[c]}**' for c in close)}?" if close else "")
            + "\nFights are matched by the **event** you typed in `/result`.",
            ephemeral=True
        )
        return

    # A dropdown holds 25, so very big events show their last 25 fights.
    if len(fights) > 25:
        fights = fights[-25:]

    event_name = fights[-1]["event"].strip()
    builder = RecapBuilderView(interaction.guild, event_name, fights)

    await interaction.response.send_message(
        content=(
            f"Found **{plural(len(fights), 'fight')}** for **{event_name}**. The last fight logged is the "
            "Main Event to start with: rearrange anything, then press **Post recap** "
            "(it posts in this channel). Your layout is saved, so you can come back to it."
        ),
        embed=builder.embed(),
        view=builder,
        ephemeral=True
    )



# ============================================================
# PFO COINS AND BETTING
#
# Members:  /coins            balance, daily bonus, your bets
#           buttons on the sportsbook post: Place a bet / My bets
# Staff:    /openbets         open betting on a card (live odds)
#           /lockbets         stop bets early (e.g. when the card starts)
#           /givecoins        add or take coins (giveaways, fixes)
#
# Coins are points only: they can't be bought or cashed out.
# Bets are paid out automatically when /result is logged.
# ============================================================

COINS_BANNER = "coins.gif"
SPORTSBOOK_BANNER = "sportsbook.gif"
COIN = "🪙"

START_BALANCE = int(os.getenv("COINS_START", "1000"))
DAILY_BONUS = int(os.getenv("COINS_DAILY", "100"))
DAILY_STREAK_STEP = 10            # +10 per day in a row
DAILY_STREAK_MAX = 100            # up to +100
BROKE_TOP_UP = 250                # daily tops you up to this if you're nearly out
MIN_BET = 10
MAX_BET = int(os.getenv("COINS_MAX_BET", "500"))
VOTE_PICK_REWARD = 25             # per correct /mainvote pick
FIGHT_PAY = {"win": 100, "finish": 50, "loss": 25}

ODDS_PRIOR = 2500                 # how much the opening odds weigh against real bets
HOUSE_EDGE = 0.95
METHOD_SHARE = {"KO/TKO": 0.45, "SUB": 0.25, "DEC": 0.30}
METHOD_NAMES = {"KO/TKO": "KO/TKO", "SUB": "Submission", "DEC": "Decision"}

_markets_to_refresh = set()


def setup_coin_tables():

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS coin_wallets (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            balance INTEGER NOT NULL,
            daily_last TEXT,
            daily_streak INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(guild_id, user_id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS coin_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bet_markets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            event TEXT NOT NULL,
            closes_at INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            channel_id INTEGER,
            message_id INTEGER,
            created_by INTEGER,
            created_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bet_fights (
            market_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            fighter_a INTEGER NOT NULL,
            fighter_b INTEGER NOT NULL,
            name_a TEXT NOT NULL,
            name_b TEXT NOT NULL,
            weight TEXT,
            open_pa REAL NOT NULL,
            result TEXT,
            method TEXT,
            fight_id INTEGER,
            PRIMARY KEY(market_id, position)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            stake INTEGER NOT NULL,
            odds REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            payout INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bet_legs (
            bet_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            pick TEXT NOT NULL,
            method TEXT,
            odds REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            PRIMARY KEY(bet_id, position)
        )
    """)

    db.commit()
    db.close()


def coins_text(amount: int):
    return f"{amount:,}"


# ------------------------------------------------------------
# Wallets
# ------------------------------------------------------------

def get_wallet(guild_id: int, user_id: int):
    """Creates the wallet (with the starting balance) the first time."""

    rows = discipline_query(
        "SELECT * FROM coin_wallets WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
    )

    if rows:
        return rows[0]

    discipline_query(
        "INSERT OR IGNORE INTO coin_wallets (guild_id, user_id, balance, daily_streak) VALUES (?, ?, ?, 0)",
        (guild_id, user_id, START_BALANCE), write=True
    )
    discipline_query(
        "INSERT INTO coin_ledger (guild_id, user_id, amount, reason, created_at) VALUES (?, ?, ?, 'start', ?)",
        (guild_id, user_id, START_BALANCE, now_utc()), write=True
    )

    return discipline_query(
        "SELECT * FROM coin_wallets WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
    )[0]


def add_coins(guild_id: int, user_id: int, amount: int, reason: str):

    if not amount:
        return

    get_wallet(guild_id, user_id)

    discipline_query(
        "UPDATE coin_wallets SET balance = balance + ? WHERE guild_id = ? AND user_id = ?",
        (amount, guild_id, user_id), write=True
    )
    discipline_query(
        "INSERT INTO coin_ledger (guild_id, user_id, amount, reason, created_at) VALUES (?, ?, ?, ?, ?)",
        (guild_id, user_id, amount, reason, now_utc()), write=True
    )


def reverse_coins(guild_id: int, reason: str):
    """Undoes every payment made for this reason (e.g. a fight that was removed)."""

    for row in discipline_query(
        "SELECT user_id, SUM(amount) AS total FROM coin_ledger WHERE guild_id = ? AND reason = ? GROUP BY user_id",
        (guild_id, reason)
    ):
        if row["total"]:
            add_coins(guild_id, row["user_id"], -row["total"], f"undo:{reason}")

    discipline_query(
        "UPDATE coin_ledger SET reason = ? WHERE guild_id = ? AND reason = ?",
        (f"undone:{reason}", guild_id, reason), write=True
    )


def claim_daily(guild_id: int, user_id: int):
    """Returns (claimed, amount, streak, next_claim_unix)."""

    wallet = get_wallet(guild_id, user_id)
    today = datetime.now(timezone.utc).date()
    tomorrow = int(datetime.combine(today + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp())

    if wallet["daily_last"] == today.isoformat():
        return False, 0, wallet["daily_streak"], tomorrow

    yesterday = (today - timedelta(days=1)).isoformat()
    streak = wallet["daily_streak"] + 1 if wallet["daily_last"] == yesterday else 1
    amount = DAILY_BONUS + min(DAILY_STREAK_MAX, DAILY_STREAK_STEP * (streak - 1))

    # Nearly broke: top up so nobody gets locked out.
    if wallet["balance"] + amount < BROKE_TOP_UP:
        amount = BROKE_TOP_UP - wallet["balance"]

    discipline_query(
        "UPDATE coin_wallets SET daily_last = ?, daily_streak = ? WHERE guild_id = ? AND user_id = ?",
        (today.isoformat(), streak, guild_id, user_id), write=True
    )
    add_coins(guild_id, user_id, amount, "daily")

    return True, amount, streak, tomorrow


def coin_rank(guild_id: int, user_id: int):

    balance = get_wallet(guild_id, user_id)["balance"]
    row = discipline_query(
        "SELECT COUNT(*) AS n FROM coin_wallets WHERE guild_id = ? AND balance > ?", (guild_id, balance)
    )

    return row[0]["n"] + 1


# ------------------------------------------------------------
# Odds
# ------------------------------------------------------------

def fighter_strength(guild_id: int, user_id: int, weight: str = None):
    """Rough rating from record, streak and ranking."""

    try:
        stats = get_fighter_stats(guild_id, user_id)
    except Exception:
        stats = {"wins": 0, "losses": 0, "streak": None}

    score = 50 + 4 * (stats["wins"] - stats["losses"])
    streak = stats.get("streak") or ""

    if len(streak) > 1 and streak[0] in "WL" and streak[1:].isdigit():
        score += (3 if streak[0] == "W" else -3) * min(5, int(streak[1:]))

    if weight:
        row = get_user_ranking_in_division(guild_id, user_id, weight)
        if row:
            score += 25 if row["rank"] == 0 else max(0, 16 - row["rank"]) * 1.5

    return score


def opening_probability(guild_id: int, a: int, b: int, weight: str = None):

    gap = fighter_strength(guild_id, b, weight) - fighter_strength(guild_id, a, weight)
    p = 1 / (1 + 10 ** (gap / 30))

    return min(0.88, max(0.12, p))


def fight_pools(market_id: int, position: int):
    """Coins bet on each side of a fight (accumulator legs count too)."""

    pools = {"a": 0, "b": 0}

    for row in discipline_query("""
        SELECT bet_legs.pick, SUM(bets.stake) AS total FROM bet_legs
        JOIN bets ON bets.id = bet_legs.bet_id
        WHERE bets.market_id = ? AND bet_legs.position = ?
        GROUP BY bet_legs.pick
    """, (market_id, position)):
        pools[row["pick"]] = row["total"] or 0

    return pools


def to_odds(p: float):
    return max(1.05, round(HOUSE_EDGE / max(p, 0.01), 2))


def live_odds(bout):
    """{'a': 1.45, 'b': 2.80, 'open_a': ..., 'open_b': ..., 'pool': total}"""

    pools = fight_pools(bout["market_id"], bout["position"])
    p = (ODDS_PRIOR * bout["open_pa"] + pools["a"]) / (ODDS_PRIOR + pools["a"] + pools["b"])

    return {
        "a": to_odds(p),
        "b": to_odds(1 - p),
        "open_a": to_odds(bout["open_pa"]),
        "open_b": to_odds(1 - bout["open_pa"]),
        "pool": pools["a"] + pools["b"],
    }


METHOD_PRIOR_FIGHTS = 4   # how many "average" fights to mix in, so 1-2 fights don't swing it


def league_method_shares(guild_id: int):
    """How wins end across the whole league (falls back to a sensible default)."""

    counts = {method: 0 for method in METHOD_SHARE}

    for row in discipline_query(
        "SELECT method, COUNT(*) AS n FROM fights WHERE guild_id = ? GROUP BY method", (guild_id,)
    ):
        if row["method"] in counts:
            counts[row["method"]] = row["n"]

    total = sum(counts.values())

    if total < 10:
        return dict(METHOD_SHARE)

    return {method: counts[method] / total for method in counts}


def method_shares(guild_id: int, winner_id: int, loser_id: int):
    """
    Chance of each method if this fighter wins, from how THEY win
    and how their OPPONENT loses, mixed with the league average.
    e.g. a big KO artist against someone who keeps getting knocked
    out gets short KO odds and long Decision odds.
    """

    league = league_method_shares(guild_id)
    counts = {method: league[method] * METHOD_PRIOR_FIGHTS for method in league}

    try:
        wins_by = get_fighter_stats(guild_id, winner_id)["wins_by"]
        losses_by = get_fighter_stats(guild_id, loser_id)["losses_by"]
    except Exception:
        wins_by, losses_by = {}, {}

    for method in counts:
        counts[method] += wins_by.get(method, 0) + losses_by.get(method, 0)

    total = sum(counts.values())
    shares = {method: max(0.08, counts[method] / total) for method in counts}
    scale = sum(shares.values())

    return {method: share / scale for method, share in shares.items()}


def pick_odds(bout, pick: str, method: str = None):

    odds = live_odds(bout)[pick]

    if method:
        market = get_market(bout["market_id"])
        winner, loser = (
            (bout["fighter_a"], bout["fighter_b"]) if pick == "a"
            else (bout["fighter_b"], bout["fighter_a"])
        )
        share = method_shares(market["guild_id"], winner, loser)[method]
        odds = min(50.0, round(odds / share, 2))

    return odds


def pick_label(bout, pick: str, method: str = None):

    name = bout["name_a"] if pick == "a" else bout["name_b"]

    return f"{name} by {METHOD_NAMES[method]}" if method else f"{name} to win"


# ------------------------------------------------------------
# Markets
# ------------------------------------------------------------

def get_market(market_id: int):
    rows = discipline_query("SELECT * FROM bet_markets WHERE id = ?", (market_id,))
    return rows[0] if rows else None


def market_bouts(market_id: int):
    return discipline_query("SELECT * FROM bet_fights WHERE market_id = ? ORDER BY position", (market_id,))


def market_is_open(market):
    return market and market["status"] == "open" and time.time() < market["closes_at"]


def arrow(now: float, opening: float):
    if now < opening - 0.01:
        return " ▼"
    if now > opening + 0.01:
        return " ▲"
    return ""


def sportsbook_embeds(market):

    is_open = market_is_open(market)
    bouts = market_bouts(market["id"])
    bettors = discipline_query(
        "SELECT COUNT(DISTINCT user_id) AS n, COALESCE(SUM(stake), 0) AS total FROM bets WHERE market_id = ?",
        (market["id"],)
    )[0]

    if is_open:
        header = [
            f"{COIN} **Bet PFO Coins on the fights.** Odds move as bets come in.",
            f"⏰ Betting closes <t:{market['closes_at']}:F> (<t:{market['closes_at']}:R>)",
        ]
    elif market["status"] == "settled":
        header = ["✅ **All fights settled.** Winnings have been paid out."]
    else:
        header = ["🔒 **Betting is closed.** Bets are paid out as results are logged."]

    blocks = []

    for bout in bouts:

        odds = live_odds(bout)
        number = f"`{bout['position']:>2}`"
        tag_a = ranking_tag(market["guild_id"], bout["fighter_a"], bout["weight"])
        tag_b = ranking_tag(market["guild_id"], bout["fighter_b"], bout["weight"])
        tag_a = f" {tag_a}" if tag_a else ""
        tag_b = f" {tag_b}" if tag_b else ""

        if bout["result"]:
            if bout["result"] == "void":
                outcome = "🤝 Draw / no contest: bets refunded"
            else:
                winner = bout["name_a"] if bout["result"] == "a" else bout["name_b"]
                how = METHOD_NAMES.get(bout["method"], bout["method"] or "")
                outcome = f"✅ **{winner}** won" + (f" by {how}" if how else "")
            blocks.append(
                f"**Fight {bout['position']}** · {bout['name_a']} vs {bout['name_b']}\n{outcome}"
            )
            continue

        weight = f" · {bout['weight']}" if bout["weight"] else ""
        blocks.append(
            f"**Fight {bout['position']}**{weight} · {COIN} {coins_text(odds['pool'])} bet\n"
            f"🔴 **{bout['name_a']}**{tag_a}  `{odds['a']:.2f}`{arrow(odds['a'], odds['open_a'])}\n"
            f"🔵 **{bout['name_b']}**{tag_b}  `{odds['b']:.2f}`{arrow(odds['b'], odds['open_b'])}"
        )

    footer = (
        f"👥 **{bettors['n']}** {'bettor' if bettors['n'] == 1 else 'bettors'} · "
        f"{COIN} **{coins_text(bettors['total'])}** coins staked"
    )

    pages = [header + [""]]

    for block in blocks:
        if len("\n".join(pages[-1])) + len(block) > 3800:
            pages.append([])
        pages[-1] += [block, ""]

    pages[-1].append(footer)
    embeds = []

    for index, page in enumerate(pages):

        embed = discord.Embed(
            title=market["event"] if index == 0 else None,
            description="\n".join(page),
            color=PFO_GOLD
        )

        if index == 0:
            apply_branding(embed, author_text="PFO • SPORTSBOOK")

        if index == len(pages) - 1:
            embed.set_image(url=asset_url(SPORTSBOOK_BANNER))
            embed.timestamp = discord.utils.utcnow()
            embed.set_footer(
                text="Odds update every minute • ▼ shortening ▲ drifting • coins are points only, no real money",
                icon_url=asset_url(LOGO_FILE)
            )

        embeds.append(embed)

    return embeds


async def refresh_sportsbook(market_id: int):

    market = get_market(market_id)

    if not market or not market["message_id"]:
        return

    try:
        channel = bot.get_channel(market["channel_id"]) or await bot.fetch_channel(market["channel_id"])
        message = await channel.fetch_message(market["message_id"])
        await message.edit(
            embeds=sportsbook_embeds(market),
            view=SportsbookView(market_id)
        )
    except Exception as error:
        print(f"Could not refresh sportsbook {market_id}: {error}")


# ------------------------------------------------------------
# Bets
# ------------------------------------------------------------

def bet_lines(guild: discord.Guild, user_id: int, limit_settled: int = 5):
    """Open bets, then the last few settled ones."""

    def describe(bet):

        legs = discipline_query("""
            SELECT bet_legs.*, bet_fights.name_a, bet_fights.name_b FROM bet_legs
            JOIN bets ON bets.id = bet_legs.bet_id
            JOIN bet_fights ON bet_fights.market_id = bets.market_id AND bet_fights.position = bet_legs.position
            WHERE bet_legs.bet_id = ? ORDER BY bet_legs.position
        """, (bet["id"],))

        icons = {"pending": "⏳", "won": "✅", "lost": "❌", "void": "🤝"}
        status = {"open": "🟡", "won": "🟢", "lost": "🔴", "void": "⚪"}[bet["status"]]
        returns = round(bet["stake"] * bet["odds"])

        if bet["status"] == "won":
            money = f"won **{coins_text(bet['payout'])}**"
        elif bet["status"] == "lost":
            money = "lost"
        elif bet["status"] == "void":
            money = "refunded"
        else:
            money = f"returns **{coins_text(returns)}**"

        if len(legs) == 1:
            leg = legs[0]
            return f"{status} {coins_text(bet['stake'])} on {pick_label(leg, leg['pick'], leg['method'])} @ {bet['odds']:.2f} · {money}"

        picks = ", ".join(
            f"{icons[leg['status']]} {pick_label(leg, leg['pick'], leg['method'])}" for leg in legs
        )
        return (
            f"{status} **Accumulator ({len(legs)})** {coins_text(bet['stake'])} @ {bet['odds']:.2f} · {money}\n"
            f"└ {picks}"
        )

    open_bets = discipline_query(
        "SELECT * FROM bets WHERE guild_id = ? AND user_id = ? AND status = 'open' ORDER BY id DESC",
        (guild.id, user_id)
    )
    settled = discipline_query(
        "SELECT * FROM bets WHERE guild_id = ? AND user_id = ? AND status != 'open' ORDER BY id DESC LIMIT ?",
        (guild.id, user_id, limit_settled)
    )

    return [describe(bet) for bet in open_bets], [describe(bet) for bet in settled]


def evaluate_bet(guild_id: int, bet_id: int):
    """Works out a bet from its legs and pays (or takes back) the difference."""

    bet = discipline_query("SELECT * FROM bets WHERE id = ?", (bet_id,))[0]
    legs = discipline_query("SELECT * FROM bet_legs WHERE bet_id = ?", (bet_id,))
    statuses = [leg["status"] for leg in legs]

    if "lost" in statuses:
        status, payout = "lost", 0
    elif "pending" in statuses:
        status, payout = "open", 0
    elif all(s == "void" for s in statuses):
        status, payout = "void", bet["stake"]
    else:
        odds = 1.0
        for leg in legs:
            if leg["status"] == "won":
                odds *= leg["odds"]
        status, payout = "won", round(bet["stake"] * odds)

    difference = payout - bet["payout"]

    if difference:
        add_coins(guild_id, bet["user_id"], difference, f"bet:{bet_id}")

    discipline_query("UPDATE bets SET status = ?, payout = ? WHERE id = ?", (status, payout, bet_id), write=True)


def settle_bets_for_fight(guild_id: int, fight):
    """Called after /result (and imports / edits). Returns market ids that changed."""

    if not fight or not fight["winner_id"] or not fight["loser_id"]:
        return []

    pair = {fight["winner_id"], fight["loser_id"]}
    changed = []

    for bout in discipline_query("""
        SELECT bet_fights.* FROM bet_fights
        JOIN bet_markets ON bet_markets.id = bet_fights.market_id
        WHERE bet_markets.guild_id = ? AND bet_fights.result IS NULL
    """, (guild_id,)):

        if {bout["fighter_a"], bout["fighter_b"]} != pair:
            continue

        if fight["method"] in ("DRAW", "NC"):
            result, method = "void", None
        else:
            result = "a" if fight["winner_id"] == bout["fighter_a"] else "b"
            method = fight["method"]

        discipline_query(
            "UPDATE bet_fights SET result = ?, method = ?, fight_id = ? WHERE market_id = ? AND position = ?",
            (result, method, fight["id"], bout["market_id"], bout["position"]), write=True
        )

        for leg in discipline_query("""
            SELECT bet_legs.* FROM bet_legs JOIN bets ON bets.id = bet_legs.bet_id
            WHERE bets.market_id = ? AND bet_legs.position = ?
        """, (bout["market_id"], bout["position"])):

            if result == "void":
                status = "void"
            elif leg["pick"] == result and (not leg["method"] or leg["method"] == method):
                status = "won"
            else:
                status = "lost"

            discipline_query(
                "UPDATE bet_legs SET status = ? WHERE bet_id = ? AND position = ?",
                (status, leg["bet_id"], leg["position"]), write=True
            )
            evaluate_bet(guild_id, leg["bet_id"])

        if not discipline_query(
            "SELECT 1 FROM bet_fights WHERE market_id = ? AND result IS NULL", (bout["market_id"],)
        ):
            discipline_query("UPDATE bet_markets SET status = 'settled' WHERE id = ?", (bout["market_id"],), write=True)

        changed.append(bout["market_id"])

    return changed


def unsettle_bets_for_fight(guild_id: int, fight_id: int):
    """A result was removed or changed: undo its payouts."""

    changed = []

    for bout in discipline_query("SELECT * FROM bet_fights WHERE fight_id = ?", (fight_id,)):

        discipline_query(
            "UPDATE bet_fights SET result = NULL, method = NULL, fight_id = NULL WHERE market_id = ? AND position = ?",
            (bout["market_id"], bout["position"]), write=True
        )

        legs = discipline_query("""
            SELECT bet_legs.* FROM bet_legs JOIN bets ON bets.id = bet_legs.bet_id
            WHERE bets.market_id = ? AND bet_legs.position = ?
        """, (bout["market_id"], bout["position"]))

        for leg in legs:
            discipline_query(
                "UPDATE bet_legs SET status = 'pending' WHERE bet_id = ? AND position = ?",
                (leg["bet_id"], leg["position"]), write=True
            )
            evaluate_bet(guild_id, leg["bet_id"])

        market = get_market(bout["market_id"])

        if market and market["status"] == "settled":
            new_status = "open" if time.time() < market["closes_at"] else "locked"
            discipline_query("UPDATE bet_markets SET status = ? WHERE id = ?", (new_status, market["id"]), write=True)

        changed.append(bout["market_id"])

    return changed


def pay_fighters_for_fight(guild_id: int, fight):
    """Show money for logged fights: win, finish bonus, and something for losing."""

    if not fight or fight["method"] in ("DRAW", "NC"):
        return

    reason = f"fight:{fight['id']}"

    if fight["winner_id"]:
        bonus = FIGHT_PAY["finish"] if fight["method"] in ("KO/TKO", "SUB") else 0
        add_coins(guild_id, fight["winner_id"], FIGHT_PAY["win"] + bonus, reason)

    if fight["loser_id"]:
        add_coins(guild_id, fight["loser_id"], FIGHT_PAY["loss"], reason)


def pay_vote_rewards(guild_id: int, fight):
    """Coins for correct /mainvote picks on this fight."""

    if not fight or fight["method"] in ("DRAW", "NC") or not fight["winner_id"]:
        return

    for row in discipline_query("""
        SELECT votes.user_id FROM votes
        JOIN vote_bouts ON vote_bouts.poll_id = votes.poll_id AND vote_bouts.position = votes.position
        WHERE vote_bouts.result_fight_id = ? AND votes.pick = ?
    """, (fight["id"], fight["winner_id"])):
        add_coins(guild_id, row["user_id"], VOTE_PICK_REWARD, f"votepick:{fight['id']}")


async def coins_after_result(guild: discord.Guild, fight, pay_fighters: bool = True):
    """Everything coins-related after a result. Never raises."""

    try:
        if pay_fighters:
            pay_fighters_for_fight(guild.id, fight)
        pay_vote_rewards(guild.id, fight)
        for market_id in settle_bets_for_fight(guild.id, fight):
            _markets_to_refresh.add(market_id)
    except Exception as error:
        print(f"Coins after result failed: {error}")


async def coins_after_removal(guild: discord.Guild, fight_id: int):

    try:
        reverse_coins(guild.id, f"fight:{fight_id}")
        reverse_coins(guild.id, f"votepick:{fight_id}")
        for market_id in unsettle_bets_for_fight(guild.id, fight_id):
            _markets_to_refresh.add(market_id)
    except Exception as error:
        print(f"Coins after removal failed: {error}")


# ------------------------------------------------------------
# The bet slip (only the bettor sees it)
# ------------------------------------------------------------

class StakeModal(
    discord.ui.Modal,
    title="How many coins?"
):

    amount = discord.ui.TextInput(
        label="Stake",
        placeholder=f"Between {MIN_BET} and {MAX_BET}",
        max_length=6
    )

    def __init__(self, slip):
        super().__init__()
        self.slip = slip

    async def on_submit(self, interaction):
        text = (self.amount.value or "").replace(",", "").strip()
        if not text.isdigit():
            await self.slip.redraw(interaction, "❌ Type a number of coins.")
            return
        self.slip.stake = int(text)
        await self.slip.redraw(interaction)


class BetSlipView(
    discord.ui.View
):

    STAKES = [50, 100, 250, 500]

    def __init__(self, market_id: int, guild: discord.Guild, user_id: int):

        super().__init__(timeout=900)

        self.market_id = market_id
        self.guild = guild
        self.user_id = user_id
        self.legs = {}          # position -> (pick, method)
        self.chosen = None      # fight picked in the first dropdown
        self.stake = 100
        self.rebuild()

    def bouts(self):
        return {bout["position"]: bout for bout in market_bouts(self.market_id) if not bout["result"]}

    def slip_odds(self):
        bouts = self.bouts()
        total = 1.0
        for position, (pick, method) in self.legs.items():
            if position in bouts:
                total *= pick_odds(bouts[position], pick, method)
        return round(total, 2)

    def embed(self, note: str = None):

        bouts = self.bouts()
        balance = get_wallet(self.guild.id, self.user_id)["balance"]
        lines = []

        if note:
            lines += [note, ""]

        if self.legs:
            kind = "Single" if len(self.legs) == 1 else f"Accumulator ({len(self.legs)} picks, all must win)"
            lines.append(f"**{kind}**")

            for position, (pick, method) in sorted(self.legs.items()):
                bout = bouts.get(position)
                if bout:
                    lines.append(
                        f"`{position:>2}` {pick_label(bout, pick, method)} · `{pick_odds(bout, pick, method):.2f}`"
                    )

            odds = self.slip_odds()
            lines += [
                "",
                f"Stake: {COIN} **{coins_text(self.stake)}** · Odds **{odds:.2f}**",
                f"Returns: {COIN} **{coins_text(round(self.stake * odds))}** if it wins",
            ]
        else:
            lines.append("1) Choose a fight  2) choose your pick. Add picks from other fights for an accumulator.")

        lines += ["", f"Your balance: {COIN} **{coins_text(balance)}**"]

        embed = discord.Embed(
            title="Your bet slip",
            description="\n".join(lines),
            color=PFO_GOLD
        )
        embed.set_author(name="PFO • SPORTSBOOK", icon_url=asset_url(LOGO_FILE))
        embed.set_footer(text=f"Bets {MIN_BET}–{MAX_BET} coins • odds lock when you place the bet")

        return embed

    def rebuild(self):

        self.clear_items()
        bouts = self.bouts()

        fight_select = discord.ui.Select(
            placeholder="1) Choose a fight",
            options=[
                discord.SelectOption(
                    label=f"{position}. {bout['name_a']} vs {bout['name_b']}"[:100],
                    value=str(position),
                    description=("✅ on your slip" if position in self.legs else None),
                    default=position == self.chosen
                )
                for position, bout in list(bouts.items())[:25]
            ] or [discord.SelectOption(label="No fights left to bet on", value="0")],
            disabled=not bouts,
            row=0
        )
        fight_select.callback = self.choose_fight
        self.add_item(fight_select)

        bout = bouts.get(self.chosen)

        if bout:
            current = self.legs.get(self.chosen)
            options = []
            for pick in ("a", "b"):
                for method in (None, "KO/TKO", "SUB", "DEC"):
                    options.append(discord.SelectOption(
                        label=f"{pick_label(bout, pick, method)} · {pick_odds(bout, pick, method):.2f}"[:100],
                        value=f"{pick}|{method or ''}",
                        default=current == (pick, method)
                    ))
            pick_select = discord.ui.Select(placeholder="2) Choose your pick", options=options, row=1)
        else:
            pick_select = discord.ui.Select(
                placeholder="2) Choose your pick (pick a fight first)",
                options=[discord.SelectOption(label="-", value="-")],
                disabled=True,
                row=1
            )
        pick_select.callback = self.choose_pick
        self.add_item(pick_select)

        for amount in self.STAKES:
            button = discord.ui.Button(
                label=coins_text(amount),
                style=discord.ButtonStyle.blurple if self.stake == amount else discord.ButtonStyle.grey,
                row=2
            )
            button.callback = self.make_stake(amount)
            self.add_item(button)

        other = discord.ui.Button(
            label="Other" if self.stake in self.STAKES else coins_text(self.stake),
            emoji="✏️",
            style=discord.ButtonStyle.grey if self.stake in self.STAKES else discord.ButtonStyle.blurple,
            row=2
        )
        other.callback = self.other_stake
        self.add_item(other)

        place = discord.ui.Button(label="Place bet", emoji="✅", style=discord.ButtonStyle.green,
                                  disabled=not self.legs, row=3)
        place.callback = self.place
        remove = discord.ui.Button(label="Remove pick", emoji="➖", style=discord.ButtonStyle.grey,
                                   disabled=self.chosen not in self.legs, row=3)
        remove.callback = self.remove_pick
        clear = discord.ui.Button(label="Clear slip", emoji="🗑️", style=discord.ButtonStyle.grey,
                                  disabled=not self.legs, row=3)
        clear.callback = self.clear
        for item in (place, remove, clear):
            self.add_item(item)

    async def redraw(self, interaction, note: str = None):
        self.rebuild()
        await interaction.response.edit_message(embed=self.embed(note), view=self)

    async def choose_fight(self, interaction):
        values = interaction.data.get("values", [])
        if values and values[0] != "0":
            self.chosen = int(values[0])
        await self.redraw(interaction)

    async def choose_pick(self, interaction):

        values = interaction.data.get("values", [])

        if values and self.chosen in self.bouts():
            pick, method = values[0].split("|")
            self.legs[self.chosen] = (pick, method or None)

        await self.redraw(interaction)

    def make_stake(self, amount: int):
        async def callback(interaction):
            self.stake = amount
            await self.redraw(interaction)
        return callback

    async def other_stake(self, interaction):
        await interaction.response.send_modal(StakeModal(self))

    async def remove_pick(self, interaction):
        self.legs.pop(self.chosen, None)
        await self.redraw(interaction)

    async def clear(self, interaction):
        self.legs = {}
        await self.redraw(interaction)

    async def place(self, interaction):

        market = get_market(self.market_id)

        if not market_is_open(market):
            await interaction.response.edit_message(content="🔒 Betting has closed.", embed=None, view=None)
            return

        bouts = self.bouts()
        legs = {position: leg for position, leg in self.legs.items() if position in bouts}

        if not legs:
            await self.redraw(interaction, "❌ Those fights have already finished. Pick again.")
            return

        if not MIN_BET <= self.stake <= MAX_BET:
            await self.redraw(interaction, f"❌ Bets must be between {MIN_BET} and {coins_text(MAX_BET)} coins.")
            return

        for position in legs:
            if self.user_id in (bouts[position]["fighter_a"], bouts[position]["fighter_b"]):
                await self.redraw(interaction, "❌ You can't bet on your own fight. Take that pick off your slip.")
                return

        balance = get_wallet(self.guild.id, self.user_id)["balance"]

        if balance < self.stake:
            await self.redraw(interaction, f"❌ You only have {COIN} **{coins_text(balance)}**. Lower your stake.")
            return

        odds_each = {position: pick_odds(bouts[position], pick, method) for position, (pick, method) in legs.items()}
        total = 1.0
        for value in odds_each.values():
            total *= value
        total = round(total, 2)

        bet_id = discipline_query(
            "INSERT INTO bets (market_id, guild_id, user_id, stake, odds, status, payout, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'open', 0, ?)",
            (self.market_id, self.guild.id, self.user_id, self.stake, total, now_utc()), write=True
        )

        for position, (pick, method) in legs.items():
            discipline_query(
                "INSERT INTO bet_legs (bet_id, position, pick, method, odds, status) VALUES (?, ?, ?, ?, ?, 'pending')",
                (bet_id, position, pick, method, odds_each[position]), write=True
            )

        add_coins(self.guild.id, self.user_id, -self.stake, f"stake:{bet_id}")
        _markets_to_refresh.add(self.market_id)

        summary = (
            pick_label(bouts[next(iter(legs))], *legs[next(iter(legs))])
            if len(legs) == 1 else f"a {len(legs)}-pick accumulator"
        )

        self.legs = {}
        self.chosen = None
        await self.redraw(
            interaction,
            f"✅ **Bet placed:** {COIN} {coins_text(self.stake)} on {summary} @ **{total:.2f}** "
            f"→ returns {COIN} **{coins_text(round(self.stake * total))}**"
        )


class SportsbookView(
    discord.ui.View
):
    """Buttons under the sportsbook post. Work after restarts."""

    def __init__(self, market_id: int):

        super().__init__(timeout=None)

        self.market_id = market_id
        market = get_market(market_id)

        bet = discord.ui.Button(
            label="Place a bet", emoji="💰", style=discord.ButtonStyle.green,
            custom_id=f"pfo_book_{market_id}_bet", disabled=not market_is_open(market)
        )
        bet.callback = self.place_bet
        mine = discord.ui.Button(
            label="My bets", emoji="🧾", style=discord.ButtonStyle.grey,
            custom_id=f"pfo_book_{market_id}_mine"
        )
        mine.callback = self.my_bets
        self.add_item(bet)
        self.add_item(mine)

    async def place_bet(self, interaction):

        if not market_is_open(get_market(self.market_id)):
            await interaction.response.send_message("🔒 Betting has closed.", ephemeral=True)
            return

        slip = BetSlipView(self.market_id, interaction.guild, interaction.user.id)
        await interaction.response.send_message(embed=slip.embed(), view=slip, ephemeral=True)

    async def my_bets(self, interaction):
        await interaction.response.send_message(
            embed=my_bets_embed(interaction.guild, interaction.user.id),
            ephemeral=True
        )


def my_bets_embed(guild: discord.Guild, user_id: int):

    open_lines, settled_lines = bet_lines(guild, user_id)
    lines = []

    if open_lines:
        lines += ["**Open bets**"] + open_lines + [""]

    if settled_lines:
        lines += ["**Recent results**"] + settled_lines

    if not lines:
        lines = ["No bets yet. Press **Place a bet** on a sportsbook post."]

    embed = discord.Embed(
        title="Your bets",
        description="\n".join(lines)[:4000],
        color=PFO_GOLD
    )
    embed.set_author(name="PFO • SPORTSBOOK", icon_url=asset_url(LOGO_FILE))
    embed.set_footer(
        text=f"Balance: {coins_text(get_wallet(guild.id, user_id)['balance'])} coins • 🟡 open 🟢 won 🔴 lost ⚪ refunded"
    )

    return embed


# ------------------------------------------------------------
# /coins
# ------------------------------------------------------------

def coins_embed(guild: discord.Guild, member, note: str = None):

    wallet = get_wallet(guild.id, member.id)
    won = discipline_query(
        "SELECT COUNT(*) AS n, COALESCE(SUM(payout - stake), 0) AS profit FROM bets "
        "WHERE guild_id = ? AND user_id = ? AND status = 'won'", (guild.id, member.id)
    )[0]
    lost = discipline_query(
        "SELECT COUNT(*) AS n, COALESCE(SUM(stake), 0) AS total FROM bets "
        "WHERE guild_id = ? AND user_id = ? AND status = 'lost'", (guild.id, member.id)
    )[0]
    open_bets = discipline_query(
        "SELECT COUNT(*) AS n, COALESCE(SUM(stake), 0) AS total FROM bets "
        "WHERE guild_id = ? AND user_id = ? AND status = 'open'", (guild.id, member.id)
    )[0]

    profit = won["profit"] - lost["total"]
    today = datetime.now(timezone.utc).date().isoformat()
    daily = "✅ claimed today" if wallet["daily_last"] == today else "🎁 ready to claim"

    lines = []

    if note:
        lines += [note, ""]

    lines += [
        f"# {COIN} {coins_text(wallet['balance'])}",
        f"**#{coin_rank(guild.id, member.id)}** richest in PFO",
        "",
        f"🎯 Bets: **{won['n']}** won · **{lost['n']}** lost · "
        f"{'+' if profit >= 0 else ''}{coins_text(profit)} profit",
    ]

    if open_bets["n"]:
        lines.append(f"🟡 **{open_bets['n']}** open {'bet' if open_bets['n'] == 1 else 'bets'} ({coins_text(open_bets['total'])} staked)")

    lines.append(f"🔥 Daily streak: **{wallet['daily_streak']}** · {daily}")

    embed = discord.Embed(
        title=name_without_record(member.display_name),
        description="\n".join(lines),
        color=PFO_GOLD,
        timestamp=discord.utils.utcnow()
    )

    apply_branding(embed, banner_file=COINS_BANNER, author_text="PFO • COINS", show_thumbnail=False)

    avatar = getattr(member, "display_avatar", None)
    if avatar:
        embed.set_thumbnail(url=avatar.url)

    embed.set_footer(
        text="Earn coins: daily bonus, correct /mainvote picks, fighting and winning bets",
        icon_url=asset_url(LOGO_FILE)
    )

    return embed


class CoinsView(
    discord.ui.View
):

    def __init__(self, guild: discord.Guild, user_id: int):
        super().__init__(timeout=600)
        self.guild = guild
        self.user_id = user_id

        daily = discord.ui.Button(label="Daily bonus", emoji="🎁", style=discord.ButtonStyle.green)
        daily.callback = self.daily
        bets = discord.ui.Button(label="My bets", emoji="🧾", style=discord.ButtonStyle.grey)
        bets.callback = self.bets
        self.add_item(daily)
        self.add_item(bets)

    async def daily(self, interaction):

        claimed, amount, streak, next_claim = claim_daily(self.guild.id, interaction.user.id)

        if not claimed:
            await interaction.response.send_message(
                f"⏳ You've already claimed today. Next bonus <t:{next_claim}:R>.",
                ephemeral=True
            )
            return

        note = (
            f"🎁 **+{coins_text(amount)} coins!** Daily streak: **{streak}** "
            + ("🔥" if streak > 1 else "")
        )

        if interaction.user.id == self.user_id:
            await interaction.response.edit_message(
                embed=coins_embed(self.guild, interaction.user, note), view=self
            )
        else:
            await interaction.response.send_message(note, ephemeral=True)

    async def bets(self, interaction):
        await interaction.response.send_message(
            embed=my_bets_embed(self.guild, interaction.user.id), ephemeral=True
        )


@bot.tree.command(
    name="coins",
    description="Your PFO Coins: balance, daily bonus and bets."
)
@app_commands.describe(
    member="Someone else's balance (optional)"
)
async def coins(
    interaction: discord.Interaction,
    member: discord.Member = None
):

    target = member or interaction.user

    if getattr(target, "bot", False):
        await interaction.response.send_message("❌ Bots don't have coins.", ephemeral=True)
        return

    await interaction.response.send_message(
        embed=coins_embed(interaction.guild, target),
        view=CoinsView(interaction.guild, target.id),
        ephemeral=True
    )


@bot.tree.command(
    name="givecoins",
    description="Give or take PFO Coins (e.g. giveaways or fixing a mistake)."
)
@app_commands.describe(
    member="Who",
    amount="How many (use a minus number to take coins away)",
    reason="Optional note"
)
async def givecoins(
    interaction: discord.Interaction,
    member: discord.Member,
    amount: int,
    reason: str = None
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can give coins.", ephemeral=True)
        return

    if not amount or abs(amount) > 1_000_000:
        await interaction.response.send_message("❌ Pick an amount between -1,000,000 and 1,000,000 (not 0).", ephemeral=True)
        return

    add_coins(interaction.guild.id, member.id, amount, f"staff:{interaction.user.id}:{(reason or '')[:80]}")
    balance = get_wallet(interaction.guild.id, member.id)["balance"]

    await interaction.response.send_message(
        f"{'✅ Gave' if amount > 0 else '➖ Took'} {COIN} **{coins_text(abs(amount))}** "
        f"{'to' if amount > 0 else 'from'} **{member.display_name}**. "
        f"They now have {COIN} **{coins_text(balance)}**.",
        ephemeral=True
    )


@bot.tree.command(
    name="openbets",
    description="Open betting on a card with live odds (the sportsbook post)."
)
@app_commands.describe(
    event="Event name, e.g. PFO Fight Night 16",
    closes="When betting closes, UK time, e.g. 31/10/2026 19:00",
    fighters="Fights in order: @Joe @Dan @Tom @Ben = Joe v Dan, Tom v Ben. Empty = last saved card"
)
async def openbets(
    interaction: discord.Interaction,
    event: str,
    closes: str,
    fighters: str = None
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can open betting.", ephemeral=True)
        return

    moment = parse_event_datetime(closes)

    if not moment:
        await interaction.response.send_message(
            "❌ Write when betting closes like **31/10/2026 19:00** or **31/10 7pm** (UK time).",
            ephemeral=True
        )
        return

    if moment <= datetime.now(timezone.utc):
        await interaction.response.send_message("❌ That time has already passed.", ephemeral=True)
        return

    if fighters and MENTION_PATTERN.search(fighters):
        pairs = parse_fight_pairs(fighters)
    else:
        card = get_latest_fight_card(interaction.guild.id)
        pairs = [(bout["fighter_a"], bout["fighter_b"]) for bout in get_fight_card_bouts(card["id"])] if card else []

        if not pairs:
            await interaction.response.send_message(
                "❌ @mention the fights in order, e.g. `@Joe @Dan @Tom @Ben`, or save a card with `/createfights` first.",
                ephemeral=True
            )
            return

    if pairs[-1][1] is None:
        await interaction.response.send_message(
            f"❌ Odd number of fighters: <@{pairs[-1][0]}> has no opponent.", ephemeral=True
        )
        return

    if len(pairs) > 25:
        await interaction.response.send_message("❌ Betting can cover up to 25 fights.", ephemeral=True)
        return

    def name(user_id):
        member = interaction.guild.get_member(user_id)
        return name_without_record(member.display_name) if member else f"User {user_id}"

    market_id = discipline_query(
        "INSERT INTO bet_markets (guild_id, event, closes_at, status, created_by, created_at) VALUES (?, ?, ?, 'open', ?, ?)",
        (interaction.guild.id, event.strip(), int(moment.timestamp()), interaction.user.id, now_utc()), write=True
    )

    for position, (a, b) in enumerate(pairs, start=1):
        weight = auto_weight(interaction.guild.id, a, b)
        discipline_query(
            "INSERT INTO bet_fights (market_id, position, fighter_a, fighter_b, name_a, name_b, weight, open_pa) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (market_id, position, a, b, name(a), name(b), weight,
             opening_probability(interaction.guild.id, a, b, weight)), write=True
        )

    await interaction.response.send_message(
        embeds=sportsbook_embeds(get_market(market_id)),
        view=SportsbookView(market_id)
    )

    message = await interaction.original_response()

    discipline_query(
        "UPDATE bet_markets SET channel_id = ?, message_id = ? WHERE id = ?",
        (message.channel.id, message.id, market_id), write=True
    )


@bot.tree.command(
    name="lockbets",
    description="Stop bets early (e.g. when the card starts). Payouts still happen with /result."
)
async def lockbets(
    interaction: discord.Interaction
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can lock betting.", ephemeral=True)
        return

    markets = discipline_query(
        "SELECT * FROM bet_markets WHERE guild_id = ? AND status = 'open'", (interaction.guild.id,)
    )

    if not markets:
        await interaction.response.send_message("There's no open betting to lock.", ephemeral=True)
        return

    for market in markets:
        discipline_query("UPDATE bet_markets SET status = 'locked' WHERE id = ?", (market["id"],), write=True)

    await interaction.response.send_message(
        f"🔒 Betting locked on **{', '.join(market['event'] for market in markets)}**.",
        ephemeral=True
    )

    for market in markets:
        await refresh_sportsbook(market["id"])


async def sportsbook_tick():
    """Every minute: lock betting at its closing time, refresh odds on posts with new bets."""

    now = int(time.time())

    for market in discipline_query(
        "SELECT id FROM bet_markets WHERE status = 'open' AND closes_at <= ?", (now,)
    ):
        discipline_query("UPDATE bet_markets SET status = 'locked' WHERE id = ?", (market["id"],), write=True)
        _markets_to_refresh.add(market["id"])

    for market_id in list(_markets_to_refresh):
        _markets_to_refresh.discard(market_id)
        await refresh_sportsbook(market_id)


def restore_sportsbook_buttons():

    try:
        for market in discipline_query("SELECT id FROM bet_markets ORDER BY id DESC LIMIT 30"):
            bot.add_view(SportsbookView(market["id"]))
    except Exception as error:
        print(f"Could not restore sportsbook buttons: {error}")



# ------------------------------------------------------------
# Reminder DMs
#
# Live Card:   1 hour before the event starts
# Fight Night: 1 day before the deadline (only when the deadline
#              was typed as a date, e.g. 19/11/2026 22:00)
#
# Sent to everyone on the most recent /dmlivecard or /dmfightnight.
# CARD_REMINDERS=off in Railway turns them off.
# ------------------------------------------------------------

REMINDER_BANNER = "reminder.gif"
CARD_REMINDERS = os.getenv("CARD_REMINDERS", "on").strip().lower() not in ("off", "no", "false", "0")
REMINDER_BEFORE = {"live_card": 3600, "fight_night": 86400}


def setup_reminder_columns():

    db = get_db()
    cursor = db.cursor()
    cursor.execute("PRAGMA table_info(card_notices)")

    if "reminder_sent" not in [column[1] for column in cursor.fetchall()]:
        cursor.execute("ALTER TABLE card_notices ADD COLUMN reminder_sent INTEGER NOT NULL DEFAULT 0")

    db.commit()
    db.close()


def reminder_time(notice):
    """(when to remind, the event/deadline time) as unix, or (None, None)."""

    value = notice["event_date"] if notice["kind"] == "live_card" else notice["deadline"]

    if not value or not str(value).isdigit():
        return None, None

    target = int(value)

    return target - REMINDER_BEFORE[notice["kind"]], target


def create_reminder_dm_embed(notice, fighter, opponent_text: str = None):

    _, target = reminder_time(notice)

    if notice["kind"] == "live_card":
        event = notice["event"] or "the Live Card"
        title = "⏰ Starts in 1 hour!"
        lines = [f"**{event}** starts <t:{target}:F> (<t:{target}:R>)."]

        if fighter["checked_in_at"]:
            lines += ["", "✅ **You're checked in.** See you there!"]
        else:
            lines += [
                "",
                "⚠️ **You haven't checked in yet.** Press **Check In** below so staff know you're coming.",
            ]

        author = event.upper() if event.upper().startswith("PFO") else f"PFO • {event.upper()}"
    else:
        title = "⏰ 1 day left"
        lines = [
            "Reminder: you're on the **PFO Fight Night** card.",
            f"Your fight needs to be done by <t:{target}:F> (<t:{target}:R>).",
            "",
            "If you can't make it, let a member of staff know as soon as possible.",
        ]
        author = "PFO • FIGHT NIGHT"

    embed = discord.Embed(
        title=title,
        description="\n".join(lines),
        color=PFO_GOLD,
        timestamp=discord.utils.utcnow()
    )

    apply_branding(embed, banner_file=REMINDER_BANNER, author_text=author)

    if opponent_text:
        embed.add_field(name="🆚 Your Opponent", value=opponent_text, inline=False)

    embed.set_footer(text="Prestige Fighting Organisation", icon_url=asset_url(LOGO_FILE))

    return embed


async def send_reminder_dms(notice_id: int):

    notice = get_notice(notice_id)
    guild = bot.get_guild(notice["guild_id"]) if notice else None

    if not guild:
        return

    fighters = get_card_fighters(notice_id)
    names = {row["user_id"]: row["name"] for row in fighters}
    sent, failed = 0, []

    for index, fighter in enumerate(fighters):

        if index:
            await asyncio.sleep(DM_DELAY_SECONDS)

        member = guild.get_member(fighter["user_id"])

        if not member:
            failed.append((fighter["user_id"], "not in the server"))
            continue

        opponent_text = None

        if fighter["opponent_id"]:
            opponent_text = (
                f"**{name_without_record(names.get(fighter['opponent_id'], 'Your opponent'))}** "
                f"(<@{fighter['opponent_id']}>)"
            )

        embed = create_reminder_dm_embed(notice, fighter, opponent_text)
        needs_button = notice["kind"] == "live_card" and notice["active"] and not fighter["checked_in_at"]

        try:
            if needs_button:
                await member.send(embed=embed, view=CheckInView(notice_id))
            else:
                await member.send(embed=embed)
            sent += 1
        except discord.Forbidden:
            failed.append((fighter["user_id"], "DMs closed"))
        except Exception as error:
            print(f"Reminder DM to {fighter['user_id']} failed: {error}")
            failed.append((fighter["user_id"], "Discord error"))

    label = notice["event"] or ("Live Card" if notice["kind"] == "live_card" else "Fight Night")
    lines = [f"⏰ Reminder sent to **{sent} of {len(fighters)}** fighters for **{label}**."]

    if notice["kind"] == "live_card":
        waiting = [row for row in get_card_fighters(notice_id) if not row["checked_in_at"]]
        if waiting:
            mentions = " ".join(f"<@{row['user_id']}>" for row in waiting)
            lines.append(f"⚠️ Not checked in yet: {mentions}")

    if failed:
        lines.append("📭 Couldn't DM: " + ", ".join(f"<@{user_id}> ({why})" for user_id, why in failed))

    await post_staff_log(guild, staff_log_embed("⏰ Reminder DMs", lines))


async def check_card_reminders():

    if not CARD_REMINDERS:
        return

    now = int(time.time())

    # Only the most recent DM run of each kind, per server.
    latest = discipline_query("""
        SELECT * FROM card_notices
        WHERE id IN (SELECT MAX(id) FROM card_notices GROUP BY guild_id, kind)
          AND reminder_sent = 0
    """)

    for notice in latest:

        remind_at, target = reminder_time(notice)

        if remind_at is None:
            continue

        if now < remind_at:
            continue

        update_notice(notice["id"], reminder_sent=1)

        created = parse_stamp(notice["created_at"])
        created_unix = int(created.timestamp()) if created else 0

        # Too late (event already started), or the DMs only just went out.
        if now >= target or created_unix >= remind_at:
            continue

        asyncio.create_task(send_reminder_dms(notice["id"]))


# ============================================================
# INTERVIEWS
#
# /interview @member   staff type up to 5 questions; the member gets
#                      a DM invitation (Accept / Decline). Once they
#                      accept, the questions are asked one by one in
#                      the interview channel. Each one waits for their
#                      reply, then the next is asked. At the end the
#                      bot thanks them.
#
# The interview channel is any channel with "interview" in its name,
# or set INTERVIEW_CHANNEL in Railway (name or ID).
# ============================================================

INTERVIEW_BANNER = "interview.gif"
INTERVIEW_CHANNEL = os.getenv("INTERVIEW_CHANNEL", "interviews")
INTERVIEW_NEXT_DELAY = 6          # seconds of quiet after their reply before the next question
INVITE_DAYS = 3                   # invitations expire after this
INTERVIEW_THANKS = "Thank you for taking part in the interview, good luck in your future fights"

_interview_timers = {}


def setup_interview_tables():

    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS interviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            member_id INTEGER NOT NULL,
            staff_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            questions TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'invited',
            current INTEGER NOT NULL DEFAULT 0,
            answered INTEGER NOT NULL DEFAULT 0,
            last_reply_at INTEGER,
            created_at INTEGER NOT NULL
        )
    """)
    db.commit()
    db.close()


def get_interview(interview_id: int):
    rows = discipline_query("SELECT * FROM interviews WHERE id = ?", (interview_id,))
    return rows[0] if rows else None


def update_interview(interview_id: int, **changes):
    columns = ", ".join(f"{column} = ?" for column in changes)
    discipline_query(
        f"UPDATE interviews SET {columns} WHERE id = ?", (*changes.values(), interview_id), write=True
    )


def find_interview_channel(guild: discord.Guild):

    setting = INTERVIEW_CHANNEL.strip()

    if setting.isdigit():
        return guild.get_channel(int(setting))

    wanted = role_key(setting)
    channels = list(getattr(guild, "text_channels", []))

    for channel in channels:
        if role_key(channel.name) == wanted:
            return channel

    for channel in channels:
        if "interview" in role_key(channel.name):
            return channel

    return None


def interview_questions(interview):
    return json.loads(interview["questions"])


def open_interview_for(guild_id: int, member_id: int):
    rows = discipline_query(
        "SELECT * FROM interviews WHERE guild_id = ? AND member_id = ? AND status IN ('invited', 'live') "
        "ORDER BY id DESC LIMIT 1",
        (guild_id, member_id)
    )
    return rows[0] if rows else None


def invitation_embed(guild: discord.Guild, interview, note: str = None):

    channel = guild.get_channel(interview["channel_id"]) if guild else None
    count = len(interview_questions(interview))

    staff = guild.get_member(interview["staff_id"]) if guild else None
    staff_name = name_without_record(staff.display_name) if staff else "PFO staff"

    lines = [
        "**PFO would like to interview you!** 🎙️",
        f"Interview from **{staff_name}**.",
        "",
        f"There {'is' if count == 1 else 'are'} **{count}** question{'s' if count != 1 else ''}. "
        f"They'll be asked one at a time in {channel.mention if channel else 'the interview channel'}. "
        "Just reply in the channel after each one and the next will follow.",
        "",
        "Press **Accept** when you're ready to start.",
    ]

    if note:
        lines = [note, ""] + lines

    embed = discord.Embed(
        title="🎙️ Interview invitation",
        description="\n".join(lines),
        color=PFO_GOLD,
        timestamp=discord.utils.utcnow()
    )

    apply_branding(embed, banner_file=INTERVIEW_BANNER, author_text="PFO • INTERVIEW")
    embed.set_footer(
        text=f"Invitation expires in {INVITE_DAYS} days • Prestige Fighting Organisation",
        icon_url=asset_url(LOGO_FILE)
    )

    return embed


def question_embed(interview, number: int, member):

    questions = interview_questions(interview)

    embed = discord.Embed(
        title=f"Question {number} of {len(questions)}",
        description=f"## {questions[number - 1]}",
        color=PFO_GOLD
    )

    embed.set_author(name="PFO • INTERVIEW", icon_url=asset_url(LOGO_FILE))
    embed.set_footer(text=f"{name_without_record(member.display_name)}, reply below 👇")

    return embed


async def ask_question(guild: discord.Guild, interview_id: int):
    """Posts the next question, or the thank-you once they're all answered."""

    interview = get_interview(interview_id)

    if not interview or interview["status"] != "live":
        return

    channel = guild.get_channel(interview["channel_id"])
    member = guild.get_member(interview["member_id"])

    if not channel or not member:
        update_interview(interview_id, status="cancelled")
        return

    questions = interview_questions(interview)
    number = interview["current"] + 1

    if number > len(questions):

        update_interview(interview_id, status="done")

        embed = discord.Embed(
            description=f"🎙️ **{INTERVIEW_THANKS}**, {member.mention}! 🥊",
            color=PFO_GOLD,
            timestamp=discord.utils.utcnow()
        )
        apply_branding(embed, banner_file=INTERVIEW_BANNER, author_text="PFO • INTERVIEW")
        embed.set_footer(text="Prestige Fighting Organisation", icon_url=asset_url(LOGO_FILE))

        await channel.send(embed=embed)
        return

    update_interview(interview_id, current=number, answered=0)

    await channel.send(
        content=member.mention,
        embed=question_embed(interview, number, member)
    )


async def start_interview(guild: discord.Guild, interview_id: int):

    interview = get_interview(interview_id)
    channel = guild.get_channel(interview["channel_id"])
    member = guild.get_member(interview["member_id"])

    update_interview(interview_id, status="live", current=0, answered=0)

    record = ""
    try:
        record = f" · {stats_record(get_fighter_stats(guild.id, member.id))}"
    except Exception:
        pass

    embed = discord.Embed(
        title=f"🎙️ {name_without_record(member.display_name)}",
        description=(
            f"**{member.mention}** sits down with PFO{record}.\n"
            f"{plural(len(interview_questions(interview)), 'question')}"
        ),
        color=PFO_GOLD,
        timestamp=discord.utils.utcnow()
    )
    apply_branding(embed, banner_file=INTERVIEW_BANNER, author_text="PFO • INTERVIEW", show_thumbnail=False)

    avatar = getattr(member, "display_avatar", None)
    if avatar:
        embed.set_thumbnail(url=avatar.url)

    await channel.send(embed=embed)
    await ask_question(guild, interview_id)


class InterviewInviteView(
    discord.ui.View
):
    """Accept / Decline in the member's DMs. Works after restarts."""

    def __init__(self, interview_id: int):

        super().__init__(timeout=None)

        self.interview_id = interview_id

        accept = discord.ui.Button(label="Accept", emoji="🎙️", style=discord.ButtonStyle.green,
                                   custom_id=f"pfo_interview_{interview_id}_yes")
        accept.callback = self.accept
        decline = discord.ui.Button(label="Decline", style=discord.ButtonStyle.grey,
                                    custom_id=f"pfo_interview_{interview_id}_no")
        decline.callback = self.decline
        self.add_item(accept)
        self.add_item(decline)

    async def answer(self, interaction, accepted: bool):

        interview = get_interview(self.interview_id)

        if not interview or interview["status"] != "invited":
            await interaction.response.edit_message(
                content="This invitation is no longer open.", embed=None, view=None
            )
            return

        if time.time() - interview["created_at"] > INVITE_DAYS * 86400:
            update_interview(self.interview_id, status="expired")
            await interaction.response.edit_message(
                content="⌛ This invitation has expired. Ask a member of staff if you'd still like to do it.",
                embed=None, view=None
            )
            return

        guild = bot.get_guild(interview["guild_id"])

        if not guild:
            await interaction.response.send_message("❌ I can't find the PFO server right now. Try again soon.")
            return

        if not accepted:
            update_interview(self.interview_id, status="declined")
            await interaction.response.edit_message(
                content="No problem, the interview has been cancelled.", embed=None, view=None
            )
            await post_staff_log(guild, staff_log_embed(
                "🎙️ Interview declined",
                [f"<@{interview['member_id']}> declined the interview from <@{interview['staff_id']}>."]
            ))
            return

        channel = guild.get_channel(interview["channel_id"])

        await interaction.response.edit_message(
            content=(
                f"✅ **Let's go!** Head to {channel.mention if channel else 'the interview channel'}: "
                "the first question is waiting for you."
            ),
            embed=None,
            view=None
        )

        await start_interview(guild, self.interview_id)

    async def accept(self, interaction):
        await self.answer(interaction, True)

    async def decline(self, interaction):
        await self.answer(interaction, False)


class InterviewQuestionsModal(
    discord.ui.Modal,
    title="Interview questions"
):

    q1 = discord.ui.TextInput(label="Question 1", style=discord.TextStyle.paragraph, max_length=300)
    q2 = discord.ui.TextInput(label="Question 2", style=discord.TextStyle.paragraph, max_length=300, required=False)
    q3 = discord.ui.TextInput(label="Question 3", style=discord.TextStyle.paragraph, max_length=300, required=False)
    q4 = discord.ui.TextInput(label="Question 4", style=discord.TextStyle.paragraph, max_length=300, required=False)
    q5 = discord.ui.TextInput(label="Question 5", style=discord.TextStyle.paragraph, max_length=300, required=False)

    def __init__(self, member, channel):
        super().__init__()
        self.member = member
        self.channel = channel
        self.q1.placeholder = "e.g. How did it feel to get the finish tonight?"
        self.q2.placeholder = "e.g. Who do you want next?"

    async def on_submit(self, interaction):

        questions = [
            (field.value or "").strip()
            for field in (self.q1, self.q2, self.q3, self.q4, self.q5)
            if (field.value or "").strip()
        ]

        if not questions:
            await interaction.response.send_message("❌ Add at least one question.", ephemeral=True)
            return

        interview_id = discipline_query("""
            INSERT INTO interviews (guild_id, member_id, staff_id, channel_id, questions, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'invited', ?)
        """, (interaction.guild.id, self.member.id, interaction.user.id, self.channel.id,
              json.dumps(questions), int(time.time())), write=True)

        interview = get_interview(interview_id)

        try:
            await self.member.send(
                embed=invitation_embed(interaction.guild, interview),
                view=InterviewInviteView(interview_id)
            )
        except discord.Forbidden:
            update_interview(interview_id, status="cancelled")
            await interaction.response.send_message(
                f"📭 **{self.member.display_name}** has DMs closed, so the invitation couldn't be sent.",
                ephemeral=True
            )
            return

        listed = "\n".join(f"`{number}.` {question}" for number, question in enumerate(questions, start=1))

        await interaction.response.send_message(
            f"📨 **Invitation sent to {self.member.display_name}.** Once they accept, the questions will be "
            f"asked one by one in {self.channel.mention}:\n\n{listed}",
            ephemeral=True
        )


class CancelInterviewView(
    discord.ui.View
):

    def __init__(self, interview_id: int, member, channel):
        super().__init__(timeout=300)
        self.interview_id = interview_id
        self.member = member
        self.channel = channel

        cancel = discord.ui.Button(label="Cancel it and start a new one", style=discord.ButtonStyle.red)
        cancel.callback = self.cancel
        self.add_item(cancel)

    async def cancel(self, interaction):
        update_interview(self.interview_id, status="cancelled")
        await interaction.response.send_modal(InterviewQuestionsModal(self.member, self.channel))


@bot.tree.command(
    name="interview",
    description="Invite a member to an interview: you write up to 5 questions, asked one by one."
)
@app_commands.describe(
    member="Who to interview",
    channel="Where to hold it (default: the interview channel)"
)
async def interview(
    interaction: discord.Interaction,
    member: discord.Member,
    channel: discord.TextChannel = None
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can set up interviews.", ephemeral=True)
        return

    if member.bot:
        await interaction.response.send_message("❌ Bots can't be interviewed.", ephemeral=True)
        return

    channel = channel or find_interview_channel(interaction.guild)

    if not channel:
        await interaction.response.send_message(
            "❌ I couldn't find an interview channel. Make a channel with **interview** in its name, "
            "or pick one in **channel**.",
            ephemeral=True
        )
        return

    existing = open_interview_for(interaction.guild.id, member.id)

    if existing:
        state = "is waiting for them to accept" if existing["status"] == "invited" else "is in progress"
        await interaction.response.send_message(
            f"⚠️ An interview with **{member.display_name}** {state}.",
            view=CancelInterviewView(existing["id"], member, channel),
            ephemeral=True
        )
        return

    await interaction.response.send_modal(InterviewQuestionsModal(member, channel))


async def advance_after_quiet(guild: discord.Guild, interview_id: int, number: int):
    """Waits a few seconds after their last message (they may send more), then asks the next question."""

    try:
        await asyncio.sleep(INTERVIEW_NEXT_DELAY)
    except asyncio.CancelledError:
        return

    interview = get_interview(interview_id)

    if interview and interview["status"] == "live" and interview["current"] == number and interview["answered"]:
        await ask_question(guild, interview_id)


@bot.listen("on_message")
async def interview_replies(message):

    if getattr(message.author, "bot", False) or not message.guild:
        return

    rows = discipline_query(
        "SELECT * FROM interviews WHERE guild_id = ? AND member_id = ? AND channel_id = ? AND status = 'live'",
        (message.guild.id, message.author.id, message.channel.id)
    )

    if not rows:
        return

    interview = rows[0]

    if interview["current"] < 1:
        return

    update_interview(interview["id"], answered=1, last_reply_at=int(time.time()))

    # Restart the short wait each time they send another message.
    previous = _interview_timers.pop(interview["id"], None)
    if previous:
        previous.cancel()

    _interview_timers[interview["id"]] = asyncio.create_task(
        advance_after_quiet(message.guild, interview["id"], interview["current"])
    )


async def check_interviews():
    """Catches up after a restart (an answered question with no next one yet), and expires old invites."""

    now = int(time.time())

    discipline_query(
        "UPDATE interviews SET status = 'expired' WHERE status = 'invited' AND created_at < ?",
        (now - INVITE_DAYS * 86400,), write=True
    )

    for interview in discipline_query(
        "SELECT * FROM interviews WHERE status = 'live' AND answered = 1 AND last_reply_at < ?",
        (now - INTERVIEW_NEXT_DELAY - 30,)
    ):
        if interview["id"] in _interview_timers and not _interview_timers[interview["id"]].done():
            continue

        guild = bot.get_guild(interview["guild_id"])

        if guild:
            await ask_question(guild, interview["id"])


def restore_interview_buttons():

    try:
        for interview in discipline_query("SELECT id FROM interviews WHERE status = 'invited'"):
            bot.add_view(InterviewInviteView(interview["id"]))
    except Exception as error:
        print(f"Could not restore interview buttons: {error}")


# ============================================================
# TALE OF THE TAPE
#
# /taleofthetape @A @B   side-by-side stats for a fight: record,
#                        streak, rank, finishes, title wins, recent
#                        form, head-to-head, plus fan vote and betting
#                        odds if those are open. Optional notes.
#                        Shows a preview first; press Post to share.
# ============================================================

TALE_BANNER = "tale_of_the_tape.gif"
TAPE_WIDTH = 9    # characters per side in the table (fits phones)


def recent_form(guild_id: int, user_id: int, count: int = 5):
    """e.g. 'W W L W' most recent last."""

    fights = get_fighter_fights(guild_id, user_id)
    form = []

    for fight in fights[-count:]:
        if fight["method"] == "NC":
            form.append("NC")
        elif fight["method"] == "DRAW":
            form.append("D")
        else:
            form.append("W" if fight["winner_id"] == user_id else "L")

    return form


def head_to_head(guild_id: int, a: int, b: int):

    fights = [
        fight for fight in get_fighter_fights(guild_id, a)
        if {fight["winner_id"], fight["loser_id"]} == {a, b}
    ]

    return fights


def tape_value(text: str):
    text = str(text)
    return text if len(text) <= TAPE_WIDTH else text[:TAPE_WIDTH - 1] + "…"


def tape_row(left, label: str, right):
    return f"{tape_value(left):>{TAPE_WIDTH}}  {label:^10}  {tape_value(right):<{TAPE_WIDTH}}"


def fighter_tape(guild_id: int, user_id: int, weight: str):

    stats = get_fighter_stats(guild_id, user_id)
    rank = get_user_ranking_in_division(guild_id, user_id, weight) if weight else None
    p4p = get_user_ranking_in_division(guild_id, user_id, P4P_WEIGHT)
    finishes = stats["wins_by"].get("KO/TKO", 0) + stats["wins_by"].get("SUB", 0)
    form = recent_form(guild_id, user_id, 3)

    reigns = 0
    defences = 0

    if weight:
        for reign in title_lineage(guild_id, weight)[0]:
            if reign["user_id"] == user_id:
                reigns += 1
                defences += reign["defences"]

    return {
        "record": stats_record(stats),
        "streak": stats["streak"] or "-",
        "rank": ("Champ" if rank["rank"] == 0 else f"#{rank['rank']}") if rank else "Unranked",
        "p4p": f"#{p4p['rank']}" if p4p else "-",
        "ko": stats["wins_by"].get("KO/TKO", 0),
        "sub": stats["wins_by"].get("SUB", 0),
        "dec": stats["wins_by"].get("DEC", 0),
        "finish": f"{stats['finish_rate']}%" if stats["finish_rate"] is not None else "-",
        "titles": stats["title_wins"],
        "defences": defences,
        "reigns": reigns,
        "form": " ".join(form) if form else "-",
    }


FORM_ICONS = {"W": "🟢", "L": "🔴", "D": "⚪", "NC": "⚫"}


def tape_cards(ta, tb):
    """Two stat cards with the same lines, kept short so they line up side by side."""

    show_p4p = ta["p4p"] != "-" or tb["p4p"] != "-"

    def card(tape):

        rank = tape["rank"]
        rank_line = "👑 Champion" if rank == "Champ" else ("Unranked" if rank == "Unranked" else f"🏅 Ranked {rank}")
        streak = tape["streak"]
        streak_line = (
            f"🔥 {streak} streak" if streak.startswith("W")
            else f"❄️ {streak} streak" if streak.startswith("L")
            else "No streak"
        )
        form = " ".join(FORM_ICONS.get(result, result) for result in tape["form"].split()) if tape["form"] != "-" else "No fights yet"

        lines = [f"**{tape['record']}**", rank_line]

        if show_p4p:
            lines.append(f"P4P {tape['p4p']}" if tape["p4p"] != "-" else "P4P –")

        lines += [
            streak_line,
            "",
            f"💥 {tape['ko']} KO/TKO",
            f"🔒 {tape['sub']} Sub",
            f"📋 {tape['dec']} Dec",
            f"⚡ {tape['finish']} finishes",
            f"🏆 {tape['titles']} title wins",
            "",
            form,
        ]

        return "\n".join(lines)

    return card(ta), card(tb)


def tale_of_the_tape_embed(
    guild: discord.Guild,
    a,
    b,
    weight: str = None,
    event: str = None,
    notes: str = None
):

    weight = weight or auto_weight(guild.id, a.id, b.id)
    ta = fighter_tape(guild.id, a.id, weight)
    tb = fighter_tape(guild.id, b.id, weight)
    name_a = name_without_record(a.display_name)
    name_b = name_without_record(b.display_name)

    header = [
        f"🔴 **{name_a}**  vs  **{name_b}** 🔵",
        " · ".join(part for part in (event, f"⚖️ {weight}" if weight else None) if part),
    ]

    description = "\n".join(line for line in header if line)

    if notes:
        description += "\n\n" + "\n".join(f"> {line}" for line in notes.split("\n") if line.strip())

    embed = discord.Embed(
        title="Tale of the Tape",
        description=description[:4000],
        color=PFO_GOLD,
        timestamp=discord.utils.utcnow()
    )

    apply_branding(embed, banner_file=TALE_BANNER, author_text="PFO • TALE OF THE TAPE")

    # One stat card per fighter (side by side on desktop, stacked on phones)
    card_a, card_b = tape_cards(ta, tb)
    embed.add_field(name=f"🔴 {name_a}", value=card_a, inline=True)
    embed.add_field(name=f"🔵 {name_b}", value=card_b, inline=True)

    # Head to head
    meetings = head_to_head(guild.id, a.id, b.id)

    if meetings:
        a_wins = sum(1 for fight in meetings if fight["winner_id"] == a.id and fight["method"] not in ("DRAW", "NC"))
        b_wins = sum(1 for fight in meetings if fight["winner_id"] == b.id and fight["method"] not in ("DRAW", "NC"))
        last = meetings[-1]
        last_winner = name_a if last["winner_id"] == a.id else name_b

        if a_wins == b_wins:
            standing = f"Level at {a_wins}-{b_wins}"
        else:
            leader = name_a if a_wins > b_wins else name_b
            standing = f"{leader} leads {max(a_wins, b_wins)}-{min(a_wins, b_wins)}"

        last_text = (
            "Draw" if last["method"] == "DRAW" else "No contest" if last["method"] == "NC"
            else f"{last_winner} by {fight_method_text(last)}"
        )

        embed.add_field(
            name="🤝 Head to head",
            value=f"{standing}\nLast time: {last_text}"
            + (f" ({last['event']})" if last["event"] else ""),
            inline=False
        )
    else:
        embed.add_field(name="🤝 Head to head", value="First meeting", inline=False)

    # Betting split + odds from the sportsbook for this fight
    for bout in discipline_query("""
        SELECT bet_fights.* FROM bet_fights JOIN bet_markets ON bet_markets.id = bet_fights.market_id
        WHERE bet_markets.guild_id = ? AND bet_markets.status IN ('open', 'locked') AND bet_fights.result IS NULL
        ORDER BY bet_markets.id DESC
    """, (guild.id,)):

        if {bout["fighter_a"], bout["fighter_b"]} != {a.id, b.id}:
            continue

        backers = {"a": set(), "b": set()}

        for row in discipline_query("""
            SELECT bets.user_id, bet_legs.pick FROM bet_legs JOIN bets ON bets.id = bet_legs.bet_id
            WHERE bets.market_id = ? AND bet_legs.position = ?
        """, (bout["market_id"], bout["position"])):
            backers[row["pick"]].add(row["user_id"])

        # Line them up with this Tale of the Tape's red/blue corners.
        side_a, side_b = ("a", "b") if bout["fighter_a"] == a.id else ("b", "a")
        count_a, count_b = len(backers[side_a]), len(backers[side_b])
        total = count_a + count_b
        odds = live_odds(bout)

        lines = []

        if total:
            pa = round(100 * count_a / total)
            lines.append(f"{name_a} **{pa}%** {vote_bar(pa)} **{100 - pa}%** {name_b}")
            lines.append(f"👥 {plural(total, 'bettor')}")
        else:
            lines.append("No bets yet")

        lines.append(f"Odds: {name_a} `{odds[side_a]:.2f}` · {name_b} `{odds[side_b]:.2f}`")

        embed.add_field(name="🪙 Who people are backing", value="\n".join(lines), inline=False)
        break

    embed.set_footer(
        text="From PFO records • rank is for the weight class shown",
        icon_url=asset_url(LOGO_FILE)
    )

    return embed


class TapePostView(
    discord.ui.View
):

    def __init__(self, author_id: int, embed: discord.Embed):
        super().__init__(timeout=900)
        self.author_id = author_id
        self.tape = embed

        post = discord.ui.Button(label="Post it", emoji="📢", style=discord.ButtonStyle.green)
        post.callback = self.post
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.grey)
        cancel.callback = self.cancel
        self.add_item(post)
        self.add_item(cancel)

    async def interaction_check(self, interaction):
        return interaction.user.id == self.author_id

    async def post(self, interaction):

        self.stop()

        try:
            await interaction.channel.send(embed=self.tape)
        except discord.Forbidden:
            await interaction.response.edit_message(
                content="❌ I can't post in this channel. Check my permissions here.", embed=None, view=None
            )
            return

        await interaction.response.edit_message(content="📢 Posted!", embed=None, view=None)

    async def cancel(self, interaction):
        self.stop()
        await interaction.response.edit_message(content="Cancelled. Nothing was posted.", embed=None, view=None)


@bot.tree.command(
    name="taleofthetape",
    description="Post a side-by-side Tale of the Tape for two fighters (preview first)."
)
@app_commands.describe(
    fighter_a="Red corner",
    fighter_b="Blue corner",
    event="Optional event name, e.g. PFO Fight Night 16 Main Event",
    weight="Weight class for the ranks (default: worked out from the rankings)",
    notes="Optional notes shown under the names. Use | for a new line"
)
@app_commands.choices(
    weight=DIVISION_CHOICES
)
async def taleofthetape(
    interaction: discord.Interaction,
    fighter_a: discord.Member,
    fighter_b: discord.Member,
    event: str = None,
    weight: app_commands.Choice[str] = None,
    notes: str = None
):

    if not is_staff(interaction):
        await interaction.response.send_message(f"❌ {STAFF_ONLY_TEXT} can post a Tale of the Tape.", ephemeral=True)
        return

    if fighter_a.id == fighter_b.id:
        await interaction.response.send_message("❌ Pick two different fighters.", ephemeral=True)
        return

    notes = "\n".join(part.strip() for part in notes.split("|")) if notes and notes.strip() else None

    embed = tale_of_the_tape_embed(
        interaction.guild,
        fighter_a,
        fighter_b,
        weight.value if weight else None,
        event.strip() if event and event.strip() else None,
        notes[:800] if notes else None
    )

    await interaction.response.send_message(
        content="👁️ **Preview** (only you can see this). Press **Post it** to share it in this channel.",
        embed=embed,
        view=TapePostView(interaction.user.id, embed),
        ephemeral=True
    )


_drafts_restored = False


def restore_card_drafts():
    """Puts open /createfights panels back after a restart (needs the servers loaded)."""

    global _drafts_restored

    if _drafts_restored:
        return

    _drafts_restored = True

    try:
        cutoff = int(time.time()) - DRAFT_DAYS * 86400
        discipline_query("DELETE FROM card_drafts WHERE updated_at < ?", (cutoff,), write=True)

        restored = 0

        for draft in discipline_query("SELECT * FROM card_drafts"):

            guild = bot.get_guild(draft["guild_id"])

            if not guild:
                continue

            view = CardBuilderView(
                draft["author_id"],
                guild,
                draft["event"],
                json.loads(draft["signup_ids"]),
                draft["signup_type"],
                bool(draft["shared"]),
                draft["id"]
            )
            view.bouts = [list(bout) for bout in json.loads(draft["bouts"])]
            view.page = draft["page"]
            view.rebuild()
            bot.add_view(view)
            restored += 1

        print(f"Restored {restored} fight card panel(s).")

    except Exception as error:
        print(f"Could not restore fight card panels: {error}")


async def setup_hook():

    setup_database()
    setup_card_tables()
    setup_fight_card_tables()
    setup_suggestion_tables()
    setup_vote_tables()
    setup_discipline_tables()
    setup_fighter_list_tables()
    setup_recap_tables()
    setup_card_draft_tables()
    setup_signup_time_columns()
    setup_coin_tables()
    setup_reminder_columns()
    setup_interview_tables()

    # Buttons first, so they work even if the sync below fails.
    restore_signup_buttons()
    restore_checkin_buttons()
    restore_suggestion_buttons()
    restore_vote_buttons()
    restore_copy_buttons()
    restore_sportsbook_buttons()
    restore_interview_buttons()

    if not vote_closer.is_running():
        vote_closer.start()

    await sync_commands_if_changed()


bot.setup_hook = setup_hook


@bot.event
async def on_ready():

    restore_card_drafts()

    print(
        f"Logged in as {bot.user}"
    )

    print(
        f"Bot ID: {bot.user.id}"
    )


# ============================================================
# START BOT
# ============================================================

if not TOKEN:

    raise RuntimeError(
        "DISCORD_TOKEN is missing from your environment variables."
    )


setup_database()

# How long to wait before giving up when Discord has temporarily
# blocked the bot (Cloudflare "error 1015" / 429). Restarting
# straight away makes the block last longer.
BLOCKED_WAIT_SECONDS = int(
    os.getenv("BLOCKED_WAIT_SECONDS", "1800")
)


def run_bot():

    try:

        bot.run(TOKEN)

    except discord.PrivilegedIntentsRequired:

        print(
            "Message Content Intent is not turned on in the Discord "
            "Developer Portal, so /scanresults can't read messages. "
            "Starting without it; everything else works normally."
        )

        bot.clear()
        bot._connection._intents.message_content = False

        bot.run(TOKEN)


try:

    run_bot()

except discord.HTTPException as error:

    if error.status == 429:

        print(
            "Discord has temporarily blocked this bot for too many "
            "requests (Cloudflare error 1015 / 429). This is usually "
            "Railway's shared address and clears on its own. Waiting "
            f"{BLOCKED_WAIT_SECONDS // 60} minutes before trying again "
            "so the block isn't extended."
        )

        time.sleep(BLOCKED_WAIT_SECONDS)

    raise
