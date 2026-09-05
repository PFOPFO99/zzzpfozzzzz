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
# Set DATABASE_FILE=/data/pfo_signups.db
#
# Local computer:
# It will use pfo_signups.db
DATABASE_FILE = os.getenv(
    "DATABASE_FILE",
    "pfo_signups.db"
)


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
            f"{info['message']}\n\n"
            f"**Current Sign-Ups: {count}**"
        ),
        color=discord.Color.red()
    )

    embed.set_footer(
        text="Press the button below to sign up."
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

    return discord.Embed(
        title=info["title"],
        description=(
            f"**Sign-Ups Closed! [{count} SIGN-UPS]!**\n\n"
            "This signup is no longer accepting entries."
        ),
        color=discord.Color.dark_grey()
    )


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
            emoji="🟢",
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

def is_admin(
    interaction: discord.Interaction
):

    if not interaction.guild:
        return False

    return interaction.user.guild_permissions.administrator


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
            "❌ Only server administrators can create signups.",
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
            "❌ Only server administrators can create signups.",
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
            "❌ Only server administrators can close signups.",
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
    # BUILD REAL DISCORD @ MENTIONS
    #
    # <@USER_ID>
    #
    # Discord turns this into:
    #
    # @Username
    #
    # even if their Discord display name changes.
    # --------------------------------------------------------

    lines = []

    for number, signup in enumerate(
        signups,
        start=1
    ):

        lines.append(
            f"**{number}.** <@{signup['discord_user_id']}>"
        )

    embed = discord.Embed(
        title=SIGNUP_INFO[
            signup_type_value
        ]["title"],
        description="\n".join(lines),
        color=discord.Color.red()
    )

    embed.set_footer(
        text=f"Total Sign-Ups: {len(signups)}"
    )

    # --------------------------------------------------------
    # Allow Discord to process the mentions.
    # --------------------------------------------------------

    await interaction.response.send_message(
        embed=embed,
        allowed_mentions=discord.AllowedMentions(
            users=True
        )
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


async def find_existing_ranking_message(
    channel,
    weight: str
):

    if weight == P4P_WEIGHT:

        expected_title = (
            "🏆 PFO P4P RANKINGS"
        )

    else:

        expected_title = (
            f"🏆 PFO UFC RANKINGS — {weight.upper()}"
        )

    try:

        async for message in channel.history(
            limit=100
        ):

            if not message.embeds:
                continue

            title = message.embeds[0].title

            if title == expected_title:

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

def movement_icon(
    movement: int
):

    if movement > 0:

        return "🟢⬆️"

    if movement < 0:

        return "🔴⬇️"

    return "▫️"


def create_ranking_embed(
    guild_id: int,
    weight: str
):

    rankings = get_division_rankings(
        guild_id,
        weight
    )

    ranking_dict = {
        row["rank"]: row
        for row in rankings
    }

    # ========================================================
    # P4P
    # ========================================================

    if weight == P4P_WEIGHT:

        embed = discord.Embed(
            title="🏆 PFO P4P RANKINGS",
            color=discord.Color.red()
        )

        lines = []

        for rank in range(1, 16):

            row = ranking_dict.get(
                rank
            )

            if row:

                lines.append(
                    f"**#{rank}** "
                    f"<@{row['discord_user_id']}> "
                    f"{movement_icon(row['movement'])}"
                )

            else:

                lines.append(
                    f"**#{rank}** Vacant ▫️"
                )

        embed.description = "\n".join(
            lines
        )

        embed.set_footer(
            text=(
                "🟢⬆️ Moved Up    "
                "🔴⬇️ Moved Down"
            )
        )

        return embed

    # ========================================================
    # WEIGHT CLASS
    # ========================================================

    embed = discord.Embed(
        title=(
            f"🏆 PFO UFC RANKINGS — "
            f"{weight.upper()}"
        ),
        color=discord.Color.red()
    )

    champion = ranking_dict.get(
        0
    )

    if champion:

        champion_text = (
            f"🏆 <@{champion['discord_user_id']}>"
        )

    else:

        champion_text = "🏆 Vacant"

    embed.add_field(
        name="Champion",
        value=champion_text,
        inline=False
    )

    lines = []

    for rank in range(1, 16):

        row = ranking_dict.get(
            rank
        )

        if row:

            lines.append(
                f"**#{rank}** "
                f"<@{row['discord_user_id']}> "
                f"{movement_icon(row['movement'])}"
            )

        else:

            lines.append(
                f"**#{rank}** Vacant ▫️"
            )

    embed.add_field(
        name="Rankings",
        value="\n".join(lines),
        inline=False
    )

    embed.set_footer(
        text=(
            "🟢⬆️ Moved Up    "
            "🔴⬇️ Moved Down"
        )
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
    P4P is completely separate from weight classes.

    A fighter can be:

        Lightweight #1
        P4P #4

    simultaneously.

    Updating P4P does NOT remove them from their weight class.

    Updating their weight class does NOT remove them from P4P.
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

        # Remove ONLY from P4P.
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

    # --------------------------------------------------------
    # Find their current weight class.
    #
    # P4P is intentionally ignored.
    # --------------------------------------------------------

    current_weight_row = get_user_weight_class(
        guild_id,
        user_id
    )

    old_weight = None

    if current_weight_row:

        old_weight = current_weight_row["weight"]

    # --------------------------------------------------------
    # Save old positions.
    # --------------------------------------------------------

    old_positions = {}

    if old_weight:

        old_rows = get_division_rankings(
            guild_id,
            old_weight
        )

        old_positions = {
            row["discord_user_id"]: row["rank"]
            for row in old_rows
        }

    # --------------------------------------------------------
    # Save target positions BEFORE changing.
    # --------------------------------------------------------

    target_rows = get_division_rankings(
        guild_id,
        weight
    )

    target_positions = {
        row["discord_user_id"]: row["rank"]
        for row in target_rows
    }

    # --------------------------------------------------------
    # Remove from old weight class.
    #
    # P4P remains untouched.
    # --------------------------------------------------------

    if old_weight:

        delete_user_from_division(
            guild_id,
            old_weight,
            user_id
        )

    # --------------------------------------------------------
    # Remove from target in case they already exist there.
    # --------------------------------------------------------

    delete_user_from_division(
        guild_id,
        weight,
        user_id
    )

    target_rows = get_division_rankings(
        guild_id,
        weight
    )

    # --------------------------------------------------------
    # Find champion.
    # --------------------------------------------------------

    champion_id = None

    for row in target_rows:

        if row["rank"] == 0:

            champion_id = row["discord_user_id"]

            break

    # --------------------------------------------------------
    # Get ranked fighters.
    # --------------------------------------------------------

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
            dict.fromkeys(
                ranked_users
            )
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
            dict.fromkeys(
                ranked_users
            )
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

    # ========================================================
    # REBUILD OLD WEIGHT CLASS
    # ========================================================

    if old_weight and old_weight != weight:

        remaining = get_division_rankings(
            guild_id,
            old_weight
        )

        old_champion = None
        old_ranked = []

        for row in remaining:

            if row["rank"] == 0:

                old_champion = row["discord_user_id"]

            else:

                old_ranked.append(
                    row["discord_user_id"]
                )

        new_old_positions = {
            fighter_id: index + 1
            for index, fighter_id
            in enumerate(old_ranked)
        }

        old_movements = calculate_movements(
            old_positions,
            new_old_positions
        )

        old_fighters = []

        if old_champion is not None:

            old_fighters.append({
                "user_id": old_champion,
                "rank": 0,
                "movement": 0
            })

        for fighter_id, rank in new_old_positions.items():

            old_fighters.append({
                "user_id": fighter_id,
                "rank": rank,
                "movement": old_movements.get(
                    fighter_id,
                    0
                )
            })

        save_division_rankings(
            guild_id,
            old_weight,
            old_fighters
        )

    return old_weight


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
    description="Create or refresh the PFO ranking boards."
)
async def rankingsp(
    interaction: discord.Interaction
):

    if not is_admin(interaction):

        await interaction.response.send_message(
            "❌ Only server administrators can create rankings.",
            ephemeral=True
        )

        return

    await interaction.response.defer()

    created = 0
    updated = 0
    failed = 0

    # Always use the channel where /rankingsp is run.
    # Ranking DATA in SQLite is not deleted by this command.
    for weight in WEIGHTS.keys():

        embed = create_ranking_embed(
            interaction.guild.id,
            weight
        )

        try:

            saved = get_ranking_message(
                interaction.guild.id,
                weight
            )

            # Use the saved message only when it belongs to
            # the current channel.
            if saved and saved["channel_id"] == interaction.channel.id:

                try:

                    message = await interaction.channel.fetch_message(
                        saved["message_id"]
                    )

                    await message.edit(
                        embed=embed,
                        allowed_mentions=discord.AllowedMentions(
                            users=True
                        )
                    )

                    updated += 1
                    continue

                except discord.NotFound:

                    # Old message was deleted; create a replacement.
                    pass

                except discord.Forbidden as error:

                    print(
                        f"Forbidden editing {weight}: {error}"
                    )

                    failed += 1
                    continue

            # Search this channel for an existing board before
            # creating a duplicate.
            existing_message = await find_existing_ranking_message(
                interaction.channel,
                weight
            )

            if existing_message:

                await existing_message.edit(
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions(
                        users=True
                    )
                )

                save_ranking_message(
                    interaction.guild.id,
                    weight,
                    interaction.channel.id,
                    existing_message.id
                )

                updated += 1
                continue

            # No board exists in this channel, so create it.
            message = await interaction.channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    users=True
                )
            )

            save_ranking_message(
                interaction.guild.id,
                weight,
                interaction.channel.id,
                message.id
            )

            created += 1

        except Exception as error:

            print(
                f"/rankingsp error for {weight}: "
                f"{type(error).__name__}: {error}"
            )

            failed += 1

    if failed:

        await interaction.edit_original_response(
            content=(
                "⚠️ **PFO Rankings finished with some errors.**\n\n"
                f"🆕 Created: **{created}**\n"
                f"🔄 Updated: **{updated}**\n"
                f"❌ Failed: **{failed}**\n\n"
                "Check the Railway deploy logs for the exact error."
            )
        )

    else:

        await interaction.edit_original_response(
            content=(
                "✅ **PFO Rankings are ready!**\n\n"
                f"🆕 Created: **{created}**\n"
                f"🔄 Updated: **{updated}**\n\n"
                "All ranking boards are in this channel."
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
            "❌ Only server administrators can update rankings.",
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
            "❌ Only server administrators can remove fighters.",
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
            embed=create_ranking_embed(
                guild.id,
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
# ON READY
# ============================================================

@bot.event
async def on_ready():

    print(
        f"Logged in as {bot.user}"
    )

    print(
        f"Bot ID: {bot.user.id}"
    )

    setup_database()

    # --------------------------------------------------------
    # Sync slash commands.
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Restore active signup buttons after a restart.
    # --------------------------------------------------------

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


# ============================================================
# START BOT
# ============================================================

if not TOKEN:

    raise RuntimeError(
        "DISCORD_TOKEN is missing from your environment variables."
    )


setup_database()

bot.run(TOKEN)
