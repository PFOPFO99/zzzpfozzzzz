import asyncio
import csv
import hashlib
import io
import os
import re
import sqlite3
import time
import types
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands
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

# Short names used only for the small line above the embed title.
SIGNUP_LABELS = {
    "fight_night": "FIGHT NIGHT",
    "live_card": "LIVE CARD",
}


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

    embed = discord.Embed(
        title=info["title"],
        description=(
            f"**{info['message']}**\n\n"
            f"🥊 **Current Sign-Ups: {count}**\n"
            f"🟢 **Status:** Open"
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
        text="Press the button below to sign up.",
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
                f"**Current Sign-Ups: {count}**"
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
async def fnsignup(
    interaction: discord.Interaction
):

    if not is_admin(interaction):

        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can create signups.",
            ephemeral=True
        )

        return

    existing = get_active_session(
        interaction.guild.id,
        "fight_night"
    )

    if existing:

        await interaction.response.send_message(
            "❌ There is already an active Fight Night signup.\n"
            "Close it first with `/signupclose`.",
            ephemeral=True
        )

        return

    await interaction.response.send_message(
        "Creating Fight Night signup..."
    )

    message = await interaction.original_response()

    session_id = create_session(
        interaction.guild.id,
        "fight_night",
        message.id,
        interaction.channel.id
    )

    await message.edit(
        content=None,
        embed=create_signup_embed(
            "fight_night",
            session_id
        ),
        view=SignupView(
            session_id,
            "fight_night"
        )
    )


# ============================================================
# /LIVESIGNUP
# ============================================================

@bot.tree.command(
    name="livesignup",
    description="Create a new PFO Live Card signup."
)
async def livesignup(
    interaction: discord.Interaction
):

    if not is_admin(interaction):

        await interaction.response.send_message(
            f"❌ {STAFF_ONLY_TEXT} can create signups.",
            ephemeral=True
        )

        return

    existing = get_active_session(
        interaction.guild.id,
        "live_card"
    )

    if existing:

        await interaction.response.send_message(
            "❌ There is already an active Live Card signup.\n"
            "Close it first with `/signupclose`.",
            ephemeral=True
        )

        return

    await interaction.response.send_message(
        "Creating Live Card signup..."
    )

    message = await interaction.original_response()

    session_id = create_session(
        interaction.guild.id,
        "live_card",
        message.id,
        interaction.channel.id
    )

    await message.edit(
        content=None,
        embed=create_signup_embed(
            "live_card",
            session_id
        ),
        view=SignupView(
            session_id,
            "live_card"
        )
    )


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

    count = get_signup_count(
        session["id"]
    )

    close_session(
        session["id"]
    )

    try:

        channel = interaction.guild.get_channel(
            session["channel_id"]
        )

        if channel:

            message = await channel.fetch_message(
                session["message_id"]
            )

            await message.edit(
                embed=create_closed_embed(
                    signup_type_value,
                    session["id"]
                ),
                view=None
            )

    except Exception as error:

        print(
            f"Could not update closed signup: {error}"
        )

    await interaction.response.send_message(
        f"✅ Signup closed.\n\n"
        f"**Sign-Ups Closed! [{count}]!**",
        ephemeral=True
    )


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

    await interaction.response.send_message(
        (
            "✏️ **Fight updated.** Records, streaks and profiles now use the new details.\n\n"
            f"Before: {fight_summary(fight)}\n"
            f"After: {fight_summary(updated)}"
        ),
        ephemeral=True
    )

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

        problems = await sync_nicknames(interaction.client.get_guild(self.guild_id) or interaction.guild, affected)

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

        guild = interaction.client.get_guild(self.guild_id) or interaction.guild

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

        try:
            for fight in self.fights:
                add_fight(
                    self.guild_id,
                    fight,
                    "import",
                    self.author_id,
                    cursor=cursor
                )

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


async def collect_card(
    guild: discord.Guild,
    fighters_text: str
):
    """
    Everyone @mentioned, in the order typed, without repeats.
    Returns a list of (user_id, name, member or None).
    """

    order = []

    for found in MENTION_PATTERN.findall(fighters_text or ""):
        user_id = int(found)
        if user_id not in order:
            order.append(user_id)

    fighters = []

    for user_id in order:

        member = guild.get_member(user_id)

        if member and member.bot:
            continue

        name = member.display_name if member else await departed_name(user_id)

        fighters.append((user_id, name, member))

    if not fighters:
        raise CardInputError(
            "I didn't find anyone. @mention everyone on the card in "
            "**fighters**, e.g. `@Razor @Dima @Jordan ...`"
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
    deadline: str = None
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

    lines = [
        f"`{number:>2}` {fighter_text(row)}"
        for number, row in enumerate(fighters, start=1)
    ]

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
            [(user_id, name, None) for user_id, name, _ in self.fighters],
            event=self.event,
            event_date=self.event_date,
            deadline=self.deadline
        )

        sent = []
        failed = []

        for index, (user_id, name, member) in enumerate(self.fighters):

            if index:
                await asyncio.sleep(DM_DELAY_SECONDS)

            if member is None:
                failed.append((user_id, "not in the server"))
                update_card_fighter(notice_id, user_id, dm_status="failed")
                continue

            embed = create_selection_dm_embed(
                self.kind,
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

    for user_id, name, member in fighters[:60]:
        line = f"• <@{user_id}>"
        if member is None:
            line += " ⚠️ not in server"
        lines.append(line)

    if kind == "fight_night":
        details = f"**Deadline:** {format_saved_time(deadline)}"
        author = "PFO • DM FIGHT NIGHT CARD"
    else:
        details = (
            f"**Event:** {event}\n"
            f"**When:** {format_saved_time(event_date)}\n"
            "**Check-in button:** included"
        )
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
    fighters="@mention everyone on the card, e.g. @Razor @Dima @Jordan ...",
    deadline="Deadline in UK time, e.g. 19/10/2026 22:00 (or any text, e.g. Sunday night)"
)
async def dmfightnight(
    interaction: discord.Interaction,
    fighters: str,
    deadline: str
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
    fighters="@mention everyone on the card, e.g. @Razor @Dima @Jordan ...",
    event="Event name, e.g. PFO Live Card 6",
    date="Date and time in UK time, e.g. 24/10/2026 20:00 or 24/10 8pm"
)
async def dmlivecard(
    interaction: discord.Interaction,
    fighters: str,
    event: str,
    date: str
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


async def setup_hook():

    setup_database()
    setup_card_tables()

    # Buttons first, so they work even if the sync below fails.
    restore_signup_buttons()
    restore_checkin_buttons()

    await sync_commands_if_changed()


bot.setup_hook = setup_hook


@bot.event
async def on_ready():

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
