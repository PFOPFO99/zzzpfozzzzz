import os
import sqlite3
from datetime import datetime, timezone

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
# If you have a Railway Volume mounted at /data, use:
# DATABASE_FILE=/data/pfo_signups.db
#
# Otherwise it will use a local database file.
DATABASE_FILE = os.getenv("DATABASE_FILE", "pfo_signups.db")


# ============================================================
# WEIGHT CLASSES
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
# DISCORD BOT
# ============================================================

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# ============================================================
# CHOICES FOR SLASH COMMANDS
# ============================================================

WEIGHT_CHOICES = [
    app_commands.Choice(name="P4P", value="P4P"),
    app_commands.Choice(name="Heavyweight", value="Heavyweight"),
    app_commands.Choice(name="Light Heavyweight", value="Light Heavyweight"),
    app_commands.Choice(name="Middleweight", value="Middleweight"),
    app_commands.Choice(name="Welterweight", value="Welterweight"),
    app_commands.Choice(name="Lightweight", value="Lightweight"),
    app_commands.Choice(name="Featherweight", value="Featherweight"),
    app_commands.Choice(name="Bantamweight", value="Bantamweight"),
    app_commands.Choice(name="Flyweight", value="Flyweight"),
]

RANK_CHOICES = [
    app_commands.Choice(name="Champion", value=0),
    app_commands.Choice(name="#1", value=1),
    app_commands.Choice(name="#2", value=2),
    app_commands.Choice(name="#3", value=3),
    app_commands.Choice(name="#4", value=4),
    app_commands.Choice(name="#5", value=5),
    app_commands.Choice(name="#6", value=6),
    app_commands.Choice(name="#7", value=7),
    app_commands.Choice(name="#8", value=8),
    app_commands.Choice(name="#9", value=9),
    app_commands.Choice(name="#10", value=10),
    app_commands.Choice(name="#11", value=11),
    app_commands.Choice(name="#12", value=12),
    app_commands.Choice(name="#13", value=13),
    app_commands.Choice(name="#14", value=14),
    app_commands.Choice(name="#15", value=15),
]


# ============================================================
# DATABASE
# ============================================================

def get_db():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cursor = conn.cursor()

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
    # discord_user_id is the actual source of truth.
    # player_name is retained for compatibility with older
    # databases, but users no longer type their name.
    # --------------------------------------------------------

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS signups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            discord_user_id INTEGER NOT NULL,
            player_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES signup_sessions(id)
        )
    """)

    # --------------------------------------------------------
    # RANKINGS
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
    # RANKING MESSAGE STORAGE
    #
    # Stores the Discord message ID of each ranking box so
    # the bot can edit the existing message instead of creating
    # another one.
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

    conn.commit()
    conn.close()


# ============================================================
# GENERAL HELPERS
# ============================================================

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def get_guild_id(interaction: discord.Interaction):
    if interaction.guild is None:
        return None

    return interaction.guild.id


# ============================================================
# SIGNUP DATABASE FUNCTIONS
# ============================================================

def get_active_session(guild_id: int, signup_type: str):
    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM signup_sessions
        WHERE guild_id = ?
          AND signup_type = ?
          AND active = 1
        ORDER BY id DESC
        LIMIT 1
    """, (guild_id, signup_type)).fetchone()

    conn.close()

    return row


def get_session(session_id: int):
    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM signup_sessions
        WHERE id = ?
    """, (session_id,)).fetchone()

    conn.close()

    return row


def create_session(
    guild_id: int,
    signup_type: str,
    channel_id: int
):
    conn = get_db()

    cursor = conn.execute("""
        INSERT INTO signup_sessions
        (
            guild_id,
            signup_type,
            channel_id,
            active,
            created_at
        )
        VALUES (?, ?, ?, 1, ?)
    """, (
        guild_id,
        signup_type,
        channel_id,
        now_iso()
    ))

    session_id = cursor.lastrowid

    conn.commit()
    conn.close()

    return session_id


def set_session_message(
    session_id: int,
    message_id: int,
    channel_id: int
):
    conn = get_db()

    conn.execute("""
        UPDATE signup_sessions
        SET message_id = ?,
            channel_id = ?
        WHERE id = ?
    """, (
        message_id,
        channel_id,
        session_id
    ))

    conn.commit()
    conn.close()


def add_signup(
    session_id: int,
    discord_user_id: int,
    player_name: str
):
    conn = get_db()

    # Prevent duplicate signups.
    existing = conn.execute("""
        SELECT id
        FROM signups
        WHERE session_id = ?
          AND discord_user_id = ?
    """, (
        session_id,
        discord_user_id
    )).fetchone()

    if existing:
        conn.close()
        return False

    conn.execute("""
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
        now_iso()
    ))

    conn.commit()
    conn.close()

    return True


def get_signup_count(session_id: int):
    conn = get_db()

    row = conn.execute("""
        SELECT COUNT(*) AS count
        FROM signups
        WHERE session_id = ?
    """, (session_id,)).fetchone()

    conn.close()

    return row["count"]


def get_signups(session_id: int):
    conn = get_db()

    rows = conn.execute("""
        SELECT *
        FROM signups
        WHERE session_id = ?
        ORDER BY id ASC
    """, (session_id,)).fetchall()

    conn.close()

    return rows


def close_session(session_id: int):
    conn = get_db()

    conn.execute("""
        UPDATE signup_sessions
        SET active = 0,
            closed_at = ?
        WHERE id = ?
    """, (
        now_iso(),
        session_id
    ))

    conn.commit()
    conn.close()


# ============================================================
# SIGNUP EMBEDS
# ============================================================

def signup_embed(
    signup_type: str,
    count: int,
    active: bool = True
):
    if signup_type == "Fight Night":
        title = "🥊 PFO Fight Night Sign-Ups"
    else:
        title = "🔴 PFO Live Card Sign-Ups"

    if active:
        description = (
            "Press the button below to sign up.\n\n"
            "You do **not** need to type your name.\n"
            "Your Discord account is automatically registered."
        )
    else:
        description = (
            "Sign-ups for this event are now closed."
        )

    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.red()
    )

    embed.add_field(
        name="Current Sign-Ups",
        value=f"**{count}** fighter(s)",
        inline=False
    )

    if active:
        embed.set_footer(
            text="PFO UFC 6 League • Press the button to sign up"
        )
    else:
        embed.set_footer(
            text="PFO UFC 6 League • Sign-ups closed"
        )

    return embed


# ============================================================
# SIGNUP BUTTON
# ============================================================

class SignupView(discord.ui.View):

    def __init__(self, session_id: int):
        super().__init__(timeout=None)

        self.session_id = session_id

        button = discord.ui.Button(
            label="Sign Up",
            style=discord.ButtonStyle.green,
            emoji="🟢",
            custom_id=f"pfo_signup_{session_id}"
        )

        button.callback = self.signup_button

        self.add_item(button)

    async def signup_button(
        self,
        interaction: discord.Interaction
    ):
        session = get_session(self.session_id)

        if session is None:
            await interaction.response.send_message(
                "❌ This signup session no longer exists.",
                ephemeral=True
            )
            return

        if not session["active"]:
            await interaction.response.send_message(
                "❌ Sign-ups for this event are closed.",
                ephemeral=True
            )
            return

        # ----------------------------------------------------
        # CHECK IF USER ALREADY SIGNED UP
        # ----------------------------------------------------

        conn = get_db()

        existing = conn.execute("""
            SELECT id
            FROM signups
            WHERE session_id = ?
              AND discord_user_id = ?
        """, (
            self.session_id,
            interaction.user.id
        )).fetchone()

        conn.close()

        if existing:
            await interaction.response.send_message(
                "⚠️ You are already signed up!",
                ephemeral=True
            )
            return

        # ----------------------------------------------------
        # SAVE DISCORD USER ID
        #
        # The player name is only saved as a backup/display
        # value for compatibility. The Discord ID is what is
        # actually used by /signuppaste.
        # ----------------------------------------------------

        player_name = interaction.user.display_name

        added = add_signup(
            self.session_id,
            interaction.user.id,
            player_name
        )

        if not added:
            await interaction.response.send_message(
                "⚠️ You are already signed up!",
                ephemeral=True
            )
            return

        count = get_signup_count(self.session_id)

        await interaction.response.send_message(
            "✅ You have successfully signed up!",
            ephemeral=True
        )

        # ----------------------------------------------------
        # UPDATE ORIGINAL SIGNUP MESSAGE
        # ----------------------------------------------------

        try:
            channel = interaction.guild.get_channel(
                session["channel_id"]
            )

            if channel is not None:
                message = await channel.fetch_message(
                    session["message_id"]
                )

                await message.edit(
                    embed=signup_embed(
                        session["signup_type"],
                        count,
                        True
                    ),
                    view=self
                )

        except Exception as error:
            print(
                f"Could not update signup message: {error}"
            )


# ============================================================
# RANKING DATABASE FUNCTIONS
# ============================================================

def get_division_rankings(
    guild_id: int,
    weight: str
):
    conn = get_db()

    rows = conn.execute("""
        SELECT *
        FROM rankings
        WHERE guild_id = ?
          AND weight = ?
        ORDER BY rank ASC
    """, (
        guild_id,
        weight
    )).fetchall()

    conn.close()

    return rows


def get_user_ranking_in_division(
    guild_id: int,
    discord_user_id: int,
    weight: str
):
    conn = get_db()

    row = conn.execute("""
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
    )).fetchone()

    conn.close()

    return row


def get_user_weight_class(
    guild_id: int,
    discord_user_id: int
):
    conn = get_db()

    row = conn.execute("""
        SELECT weight
        FROM rankings
        WHERE guild_id = ?
          AND discord_user_id = ?
          AND weight != ?
        LIMIT 1
    """, (
        guild_id,
        discord_user_id,
        P4P_WEIGHT
    )).fetchone()

    conn.close()

    if row:
        return row["weight"]

    return None


def delete_user_from_division(
    guild_id: int,
    discord_user_id: int,
    weight: str
):
    conn = get_db()

    conn.execute("""
        DELETE FROM rankings
        WHERE guild_id = ?
          AND discord_user_id = ?
          AND weight = ?
    """, (
        guild_id,
        discord_user_id,
        weight
    ))

    conn.commit()
    conn.close()


def save_division_rankings(
    guild_id: int,
    weight: str,
    rankings
):
    conn = get_db()

    # Remove the current division.
    conn.execute("""
        DELETE FROM rankings
        WHERE guild_id = ?
          AND weight = ?
    """, (
        guild_id,
        weight
    ))

    # Reinsert the rebuilt division.
    for item in rankings:
        conn.execute("""
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
            item["discord_user_id"],
            item["rank"],
            item.get("movement", 0),
            now_iso()
        ))

    conn.commit()
    conn.close()


# ============================================================
# RANKING UPDATE LOGIC
# ============================================================

def update_ranking(
    guild_id: int,
    weight: str,
    discord_user_id: int,
    new_rank: int
):
    """
    Updates a fighter's ranking.

    P4P is completely independent from weight classes.

    Therefore:

        Lightweight #1 + P4P #3

    is perfectly valid.

    Changing P4P does not touch Lightweight.
    Changing Lightweight does not touch P4P.
    """

    old_weight = None

    # --------------------------------------------------------
    # P4P
    # --------------------------------------------------------

    if weight == P4P_WEIGHT:

        current = get_division_rankings(
            guild_id,
            P4P_WEIGHT
        )

        old_entry = get_user_ranking_in_division(
            guild_id,
            discord_user_id,
            P4P_WEIGHT
        )

        # Remove fighter from current P4P list if already there.
        current_without_user = [
            dict(row)
            for row in current
            if row["discord_user_id"] != discord_user_id
        ]

        if old_entry:
            old_rank = old_entry["rank"]
        else:
            old_rank = None

        # Clamp P4P to #1-#15.
        if new_rank < 1:
            new_rank = 1

        if new_rank > 15:
            new_rank = 15

        # Insert fighter at requested position.
        new_entry = {
            "discord_user_id": discord_user_id,
            "rank": new_rank,
            "movement": 0
        }

        current_without_user.insert(
            new_rank - 1,
            new_entry
        )

        # Keep only 15 P4P fighters.
        current_without_user = current_without_user[:15]

        # Rebuild ranks.
        rebuilt = []

        for index, item in enumerate(
            current_without_user,
            start=1
        ):
            previous_rank = item.get("rank")

            if item["discord_user_id"] == discord_user_id:
                if old_rank is None:
                    movement = 0
                else:
                    movement = old_rank - index
            else:
                movement = item.get("movement", 0)

            rebuilt.append({
                "discord_user_id": item["discord_user_id"],
                "rank": index,
                "movement": movement
            })

        save_division_rankings(
            guild_id,
            P4P_WEIGHT,
            rebuilt
        )

        return old_weight

    # --------------------------------------------------------
    # WEIGHT CLASS
    # --------------------------------------------------------

    current_weight = get_user_weight_class(
        guild_id,
        discord_user_id
    )

    old_weight = current_weight

    # If the fighter is already in another weight class,
    # remove them from it.
    if current_weight and current_weight != weight:
        delete_user_from_division(
            guild_id,
            discord_user_id,
            current_weight
        )

    current = get_division_rankings(
        guild_id,
        weight
    )

    old_entry = get_user_ranking_in_division(
        guild_id,
        discord_user_id,
        weight
    )

    current_without_user = [
        dict(row)
        for row in current
        if row["discord_user_id"] != discord_user_id
    ]

    old_rank = old_entry["rank"] if old_entry else None

    # --------------------------------------------------------
    # Rank 0 = Champion
    # Rank 1-15 = Rankings
    # --------------------------------------------------------

    if new_rank < 0:
        new_rank = 0

    if new_rank > 15:
        new_rank = 15

    # --------------------------------------------------------
    # Champion
    # --------------------------------------------------------

    if new_rank == 0:

        # If someone is already champion, move them to #1.
        champion = None

        for item in current_without_user:
            if item["rank"] == 0:
                champion = item
                break

        current_without_user = [
            item
            for item in current_without_user
            if item["rank"] != 0
        ]

        new_entry = {
            "discord_user_id": discord_user_id,
            "rank": 0,
            "movement": 0
        }

        current_without_user.insert(
            0,
            new_entry
        )

        if champion:
            current_without_user.insert(
                1,
                {
                    "discord_user_id": champion["discord_user_id"],
                    "rank": 1,
                    "movement": 0
                }
            )

    else:

        # Remove current champion temporarily.
        champion = None

        for item in current_without_user:
            if item["rank"] == 0:
                champion = item
                break

        non_champions = [
            item
            for item in current_without_user
            if item["rank"] != 0
        ]

        # Insert fighter into requested ranked position.
        insert_index = new_rank - 1

        if insert_index < 0:
            insert_index = 0

        if insert_index > len(non_champions):
            insert_index = len(non_champions)

        non_champions.insert(
            insert_index,
            {
                "discord_user_id": discord_user_id,
                "rank": new_rank,
                "movement": 0
            }
        )

        # Maximum 15 ranked fighters.
        non_champions = non_champions[:15]

        current_without_user = []

        if champion:
            current_without_user.append(champion)

        current_without_user.extend(non_champions)

    # --------------------------------------------------------
    # Rebuild the ranking numbers.
    # --------------------------------------------------------

    rebuilt = []

    champion_exists = any(
        item["rank"] == 0
        for item in current_without_user
    )

    ranked_position = 1

    for item in current_without_user:

        if item["rank"] == 0 and champion_exists:
            final_rank = 0

        else:
            final_rank = ranked_position
            ranked_position += 1

        if item["discord_user_id"] == discord_user_id:

            if old_rank is None:
                movement = 0
            elif old_rank == 0:
                movement = 0
            elif final_rank == 0:
                movement = 0
            else:
                movement = old_rank - final_rank

        else:
            movement = item.get("movement", 0)

        rebuilt.append({
            "discord_user_id": item["discord_user_id"],
            "rank": final_rank,
            "movement": movement
        })

    save_division_rankings(
        guild_id,
        weight,
        rebuilt
    )

    return old_weight


# ============================================================
# REMOVE FIGHTER FROM RANKING
# ============================================================

def remove_from_rankings(
    guild_id: int,
    discord_user_id: int,
    weight: str
):
    """
    Removes a fighter from ONLY the selected ranking.

    This deliberately does not remove them from another
    division.

    Example:

        Lightweight #1
        P4P #3

    /rankingsr Lightweight USER_ID

    removes them from Lightweight but leaves P4P #3 intact.
    """

    current = get_division_rankings(
        guild_id,
        weight
    )

    if not current:
        return False

    found = False

    rebuilt = []

    for item in current:

        if item["discord_user_id"] == discord_user_id:
            found = True
            continue

        rebuilt.append({
            "discord_user_id": item["discord_user_id"],
            "rank": item["rank"],
            "movement": item["movement"]
        })

    if not found:
        return False

    # Rebuild ranking positions.
    if weight == P4P_WEIGHT:

        final = []

        for index, item in enumerate(
            rebuilt[:15],
            start=1
        ):
            final.append({
                "discord_user_id": item["discord_user_id"],
                "rank": index,
                "movement": item["movement"]
            })

    else:

        # Find champion.
        champion = None
        ranked = []

        for item in rebuilt:

            if item["rank"] == 0:
                champion = item
            else:
                ranked.append(item)

        final = []

        if champion:
            final.append({
                "discord_user_id": champion["discord_user_id"],
                "rank": 0,
                "movement": champion["movement"]
            })

        for index, item in enumerate(
            ranked[:15],
            start=1
        ):
            final.append({
                "discord_user_id": item["discord_user_id"],
                "rank": index,
                "movement": item["movement"]
            })

    save_division_rankings(
        guild_id,
        weight,
        final
    )

    return True


# ============================================================
# RANKING DISPLAY HELPERS
# ============================================================

def movement_icon(movement: int):

    if movement > 0:
        return f"🟢 ↑{movement}"

    if movement < 0:
        return f"🔴 ↓{abs(movement)}"

    return "⚪ —"


def ranking_mention(discord_user_id: int):
    return f"<@{discord_user_id}>"


def build_p4p_embed(
    guild_id: int
):
    rankings = get_division_rankings(
        guild_id,
        P4P_WEIGHT
    )

    embed = discord.Embed(
        title="🏆 PFO P4P RANKINGS",
        color=discord.Color.gold()
    )

    lines = []

    ranking_dict = {
        row["rank"]: row
        for row in rankings
    }

    for rank in range(1, 16):

        fighter = ranking_dict.get(rank)

        if fighter:

            lines.append(
                f"**#{rank}** "
                f"{ranking_mention(fighter['discord_user_id'])} "
                f"{movement_icon(fighter['movement'])}"
            )

        else:

            lines.append(
                f"**#{rank}** —"
            )

    embed.description = "\n".join(lines)

    embed.set_footer(
        text="PFO UFC 6 League • Pound-for-Pound Rankings"
    )

    return embed


def build_weight_embed(
    guild_id: int,
    weight: str
):
    rankings = get_division_rankings(
        guild_id,
        weight
    )

    display_name = weight.upper()

    embed = discord.Embed(
        title=f"🏆 PFO UFC RANKINGS — {display_name}",
        color=discord.Color.red()
    )

    ranking_dict = {
        row["rank"]: row
        for row in rankings
    }

    # --------------------------------------------------------
    # CHAMPION
    # --------------------------------------------------------

    champion = ranking_dict.get(0)

    if champion:

        champion_text = (
            f"{ranking_mention(champion['discord_user_id'])} "
            f"{movement_icon(champion['movement'])}"
        )

    else:

        champion_text = "—"

    embed.add_field(
        name="👑 CHAMPION",
        value=champion_text,
        inline=False
    )

    # --------------------------------------------------------
    # #1 - #15
    # --------------------------------------------------------

    lines = []

    for rank in range(1, 16):

        fighter = ranking_dict.get(rank)

        if fighter:

            lines.append(
                f"**#{rank}** "
                f"{ranking_mention(fighter['discord_user_id'])} "
                f"{movement_icon(fighter['movement'])}"
            )

        else:

            lines.append(
                f"**#{rank}** —"
            )

    embed.add_field(
        name="RANKINGS",
        value="\n".join(lines),
        inline=False
    )

    embed.set_footer(
        text="PFO UFC 6 League • Official Rankings"
    )

    return embed


# ============================================================
# RANKING MESSAGE DATABASE
# ============================================================

def save_ranking_message(
    guild_id: int,
    weight: str,
    channel_id: int,
    message_id: int
):
    conn = get_db()

    conn.execute("""
        INSERT INTO ranking_messages
        (
            guild_id,
            weight,
            channel_id,
            message_id
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(guild_id, weight)
        DO UPDATE SET
            channel_id = excluded.channel_id,
            message_id = excluded.message_id
    """, (
        guild_id,
        weight,
        channel_id,
        message_id
    ))

    conn.commit()
    conn.close()


def get_ranking_message(
    guild_id: int,
    weight: str
):
    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM ranking_messages
        WHERE guild_id = ?
          AND weight = ?
        LIMIT 1
    """, (
        guild_id,
        weight
    )).fetchone()

    conn.close()

    return row


# ============================================================
# FIND EXISTING RANKING MESSAGE IN CHANNEL
# ============================================================

async def find_existing_ranking_message(
    channel: discord.TextChannel,
    weight: str
):
    if weight == P4P_WEIGHT:
        expected_title = "🏆 PFO P4P RANKINGS"
    else:
        expected_title = (
            f"🏆 PFO UFC RANKINGS — {weight.upper()}"
        )

    try:

        async for message in channel.history(limit=100):

            if not message.author == bot.user:
                continue

            if not message.embeds:
                continue

            embed = message.embeds[0]

            if embed.title == expected_title:
                return message

    except Exception as error:
        print(
            f"Could not search channel for ranking: {error}"
        )

    return None


# ============================================================
# REFRESH RANKING MESSAGE
# ============================================================

async def refresh_ranking_message(
    guild: discord.Guild,
    weight: str,
    preferred_channel: discord.TextChannel = None
):
    saved = get_ranking_message(
        guild.id,
        weight
    )

    message = None

    # --------------------------------------------------------
    # Try saved message first.
    # --------------------------------------------------------

    if saved:

        try:

            channel = guild.get_channel(
                saved["channel_id"]
            )

            if channel:

                message = await channel.fetch_message(
                    saved["message_id"]
                )

        except Exception:
            message = None

    # --------------------------------------------------------
    # If saved message doesn't work, search the requested
    # channel for an existing ranking box.
    # --------------------------------------------------------

    if message is None and preferred_channel:

        message = await find_existing_ranking_message(
            preferred_channel,
            weight
        )

    # --------------------------------------------------------
    # Create new message only if one cannot be found.
    # --------------------------------------------------------

    if message is None:

        if preferred_channel is None:
            return None

        if weight == P4P_WEIGHT:
            embed = build_p4p_embed(guild.id)
        else:
            embed = build_weight_embed(
                guild.id,
                weight
            )

        message = await preferred_channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions(
                users=True
            )
        )

    else:

        if weight == P4P_WEIGHT:
            embed = build_p4p_embed(guild.id)
        else:
            embed = build_weight_embed(
                guild.id,
                weight
            )

        await message.edit(
            embed=embed,
            allowed_mentions=discord.AllowedMentions(
                users=True
            )
        )

    save_ranking_message(
        guild.id,
        weight,
        message.channel.id,
        message.id
    )

    return message


# ============================================================
# FIGHT NIGHT SIGNUP COMMAND
# ============================================================

@bot.tree.command(
    name="fnsignup",
    description="Create a PFO Fight Night signup."
)
async def fnsignup(
    interaction: discord.Interaction
):

    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command can only be used in a server.",
            ephemeral=True
        )
        return

    existing = get_active_session(
        interaction.guild.id,
        "Fight Night"
    )

    if existing:
        await interaction.response.send_message(
            "⚠️ There is already an active Fight Night signup.",
            ephemeral=True
        )
        return

    session_id = create_session(
        interaction.guild.id,
        "Fight Night",
        interaction.channel.id
    )

    embed = signup_embed(
        "Fight Night",
        0,
        True
    )

    view = SignupView(session_id)

    await interaction.response.send_message(
        embed=embed,
        view=view
    )

    message = await interaction.original_response()

    set_session_message(
        session_id,
        message.id,
        interaction.channel.id
    )


# ============================================================
# LIVE CARD SIGNUP COMMAND
# ============================================================

@bot.tree.command(
    name="livesignup",
    description="Create a PFO Live Card signup."
)
async def livesignup(
    interaction: discord.Interaction
):

    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command can only be used in a server.",
            ephemeral=True
        )
        return

    existing = get_active_session(
        interaction.guild.id,
        "Live Card"
    )

    if existing:
        await interaction.response.send_message(
            "⚠️ There is already an active Live Card signup.",
            ephemeral=True
        )
        return

    session_id = create_session(
        interaction.guild.id,
        "Live Card",
        interaction.channel.id
    )

    embed = signup_embed(
        "Live Card",
        0,
        True
    )

    view = SignupView(session_id)

    await interaction.response.send_message(
        embed=embed,
        view=view
    )

    message = await interaction.original_response()

    set_session_message(
        session_id,
        message.id,
        interaction.channel.id
    )


# ============================================================
# CLOSE SIGNUP
# ============================================================

@bot.tree.command(
    name="signupclose",
    description="Close an active PFO signup."
)
@app_commands.describe(
    signup_type="Which signup do you want to close?"
)
@app_commands.choices(
    signup_type=[
        app_commands.Choice(
            name="Fight Night",
            value="Fight Night"
        ),
        app_commands.Choice(
            name="Live Card",
            value="Live Card"
        )
    ]
)
async def signupclose(
    interaction: discord.Interaction,
    signup_type: app_commands.Choice[str]
):

    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command can only be used in a server.",
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
            f"❌ There is no active {signup_type_value} signup.",
            ephemeral=True
        )
        return

    close_session(session["id"])

    count = get_signup_count(
        session["id"]
    )

    await interaction.response.send_message(
        f"✅ {signup_type_value} sign-ups have been closed.",
        ephemeral=True
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
                embed=signup_embed(
                    signup_type_value,
                    count,
                    False
                ),
                view=None
            )

    except Exception as error:
        print(
            f"Could not close signup message: {error}"
        )


# ============================================================
# SIGNUP PASTE
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
            value="Fight Night"
        ),
        app_commands.Choice(
            name="Live Card",
            value="Live Card"
        )
    ]
)
async def signuppaste(
    interaction: discord.Interaction,
    signup_type: app_commands.Choice[str]
):

    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command can only be used in a server.",
            ephemeral=True
        )
        return

    signup_type_value = signup_type.value

    # --------------------------------------------------------
    # Get the latest session, not necessarily an active one.
    # --------------------------------------------------------

    conn = get_db()

    session = conn.execute("""
        SELECT *
        FROM signup_sessions
        WHERE guild_id = ?
          AND signup_type = ?
        ORDER BY id DESC
        LIMIT 1
    """, (
        interaction.guild.id,
        signup_type_value
    )).fetchone()

    conn.close()

    if not session:

        await interaction.response.send_message(
            f"❌ No {signup_type_value} signup exists yet.",
            ephemeral=True
        )
        return

    signups = get_signups(
        session["id"]
    )

    if not signups:

        lines = [
            "No fighters have signed up yet."
        ]

    else:

        lines = []

        for number, signup in enumerate(
            signups,
            start=1
        ):

            # ------------------------------------------------
            # THIS IS THE IMPORTANT PART.
            #
            # It uses the actual Discord User ID, so Discord
            # renders the fighter as an @mention.
            # ------------------------------------------------

            lines.append(
                f"**{number}.** <@{signup['discord_user_id']}>"
            )

    if signup_type_value == "Fight Night":
        title = "🥊 PFO Fight Night Sign-Ups"
    else:
        title = "🔴 PFO Live Card Sign-Ups"

    embed = discord.Embed(
        title=title,
        description="\n".join(lines),
        color=discord.Color.red()
    )

    embed.set_footer(
        text=f"Total Sign-Ups: {len(signups)}"
    )

    await interaction.response.send_message(
        embed=embed,
        allowed_mentions=discord.AllowedMentions(
            users=True
        )
    )


# ============================================================
# RANKINGS UPDATE
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

    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command can only be used in a server.",
            ephemeral=True
        )
        return

    if user.bot:
        await interaction.response.send_message(
            "❌ Bots cannot be placed in the rankings.",
            ephemeral=True
        )
        return

    weight_value = weight.value
    rank_value = rank.value

    # --------------------------------------------------------
    # P4P can only have #1-#15.
    # --------------------------------------------------------

    if weight_value == P4P_WEIGHT and rank_value == 0:

        await interaction.response.send_message(
            "❌ P4P does not have a Champion position. "
            "Please choose #1-#15.",
            ephemeral=True
        )
        return

    old_weight = update_ranking(
        interaction.guild.id,
        weight_value,
        user.id,
        rank_value
    )

    # --------------------------------------------------------
    # Refresh the selected ranking.
    # --------------------------------------------------------

    await refresh_ranking_message(
        interaction.guild,
        weight_value,
        interaction.channel
    )

    # --------------------------------------------------------
    # If the fighter moved from one weight class to another,
    # refresh their OLD weight class too.
    #
    # P4P is NOT touched.
    # --------------------------------------------------------

    if (
        old_weight
        and old_weight != weight_value
        and old_weight != P4P_WEIGHT
    ):

        await refresh_ranking_message(
            interaction.guild,
            old_weight,
            interaction.channel
        )

    if weight_value == P4P_WEIGHT:

        position_text = f"#{rank_value}"

    elif rank_value == 0:

        position_text = "Champion"

    else:

        position_text = f"#{rank_value}"

    await interaction.response.send_message(
        f"✅ {user.mention} has been placed at "
        f"**{position_text}** in **{weight_value}**.",
        ephemeral=True
    )


# ============================================================
# REMOVE FROM RANKINGS
# ============================================================

@bot.tree.command(
    name="rankingsr",
    description="Remove a fighter from a specific ranking."
)
@app_commands.describe(
    weight="Which ranking do you want to remove them from?",
    user_id="Enter their Discord User ID. This also works if they left the server."
)
@app_commands.choices(
    weight=WEIGHT_CHOICES
)
async def rankingsr(
    interaction: discord.Interaction,
    weight: app_commands.Choice[str],
    user_id: str
):

    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command can only be used in a server.",
            ephemeral=True
        )
        return

    # --------------------------------------------------------
    # Discord User IDs are too large for a Discord INTEGER
    # slash-command option, so this is deliberately a STRING.
    # --------------------------------------------------------

    user_id = user_id.strip()

    if not user_id.isdigit():

        await interaction.response.send_message(
            "❌ That is not a valid Discord User ID.\n\n"
            "A User ID should look like:\n"
            "`123456789012345678`",
            ephemeral=True
        )
        return

    try:
        discord_user_id = int(user_id)

    except ValueError:

        await interaction.response.send_message(
            "❌ I could not read that User ID.",
            ephemeral=True
        )
        return

    weight_value = weight.value

    removed = remove_from_rankings(
        interaction.guild.id,
        discord_user_id,
        weight_value
    )

    if not removed:

        await interaction.response.send_message(
            f"❌ That User ID is not currently in "
            f"**{weight_value}**.",
            ephemeral=True
        )
        return

    # --------------------------------------------------------
    # Try to get their current Discord member information.
    #
    # If they left the server, this will simply be None.
    # The ranking removal still works.
    # --------------------------------------------------------

    member = interaction.guild.get_member(
        discord_user_id
    )

    if member:

        fighter_display = member.mention

    else:

        fighter_display = f"<@{discord_user_id}>"

    # --------------------------------------------------------
    # Refresh ONLY the selected ranking.
    # --------------------------------------------------------

    await refresh_ranking_message(
        interaction.guild,
        weight_value,
        interaction.channel
    )

    await interaction.response.send_message(
        f"✅ {fighter_display} has been removed from "
        f"**{weight_value}**.",
        ephemeral=True
    )


# ============================================================
# PUBLISH / REFRESH ALL RANKINGS
# ============================================================

@bot.tree.command(
    name="rankingsp",
    description="Publish or refresh all PFO rankings."
)
async def rankingsp(
    interaction: discord.Interaction
):

    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command can only be used in a server.",
            ephemeral=True
        )
        return

    await interaction.response.defer(
        ephemeral=True
    )

    # --------------------------------------------------------
    # Update all ranking boxes.
    # --------------------------------------------------------

    for weight in WEIGHTS.values():

        await refresh_ranking_message(
            interaction.guild,
            weight,
            interaction.channel
        )

    await interaction.followup.send(
        "✅ All PFO rankings have been published/refreshed.",
        ephemeral=True
    )


# ============================================================
# BOT STARTUP
# ============================================================

@bot.event
async def on_ready():

    print("----------------------------------------")
    print(f"Logged in as {bot.user}")
    print(f"Bot ID: {bot.user.id}")
    print("----------------------------------------")


# ============================================================
# BOT SETUP
# ============================================================

@bot.event
async def setup_hook():

    # --------------------------------------------------------
    # Create database tables.
    # --------------------------------------------------------

    init_db()

    # --------------------------------------------------------
    # Re-register active signup buttons after a restart.
    #
    # This is important because the bot needs to know what
    # to do when somebody presses an old signup button.
    # --------------------------------------------------------

    conn = get_db()

    active_sessions = conn.execute("""
        SELECT *
        FROM signup_sessions
        WHERE active = 1
    """).fetchall()

    conn.close()

    for session in active_sessions:

        try:

            bot.add_view(
                SignupView(session["id"])
            )

            print(
                f"Restored signup button for session "
                f"{session['id']}"
            )

        except Exception as error:

            print(
                f"Could not restore signup session "
                f"{session['id']}: {error}"
            )

    # --------------------------------------------------------
    # Sync slash commands.
    #
    # If GUILD_ID is set, commands are synced directly to
    # your Discord server so changes appear quickly.
    # --------------------------------------------------------

    if GUILD_ID:

        try:

            guild_id = int(GUILD_ID)

            guild = discord.Object(
                id=guild_id
            )

            bot.tree.copy_global_to(
                guild=guild
            )

            synced = await bot.tree.sync(
                guild=guild
            )

            print(
                f"Synced {len(synced)} commands "
                f"to guild {guild_id}"
            )

        except Exception as error:

            print(
                f"Could not sync guild commands: {error}"
            )

    else:

        try:

            synced = await bot.tree.sync()

            print(
                f"Synced {len(synced)} global commands"
            )

        except Exception as error:

            print(
                f"Could not sync global commands: {error}"
            )


# ============================================================
# RUN BOT
# ============================================================

if not TOKEN:

    raise RuntimeError(
        "DISCORD_TOKEN is missing. "
        "Add DISCORD_TOKEN to your environment variables."
    )


bot.run(TOKEN)
