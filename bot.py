import os
import sqlite3
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

# Optional but HIGHLY recommended.
# Put your Discord server ID here to make slash commands
# appear almost instantly.
GUILD_ID = os.getenv("GUILD_ID")

DATABASE_FILE = "pfo_signups.db"


# ============================================================
# UFC WEIGHTS
# ============================================================

WEIGHTS = {
    "Heavyweight": "HW",
    "Light Heavyweight": "LHW",
    "Middleweight": "MW",
    "Welterweight": "WW",
    "Lightweight": "LW",
    "Featherweight": "FW",
    "Bantamweight": "BW",
    "Flyweight": "FLW"
}


# ============================================================
# DATABASE
# ============================================================

def get_db():
    connection = sqlite3.connect(DATABASE_FILE)
    connection.row_factory = sqlite3.Row
    return connection


def setup_database():
    db = get_db()
    cursor = db.cursor()

    # --------------------------------------------------------
    # SIGNUP TABLES
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
    # RANKINGS TABLE
    # --------------------------------------------------------
    #
    # rank:
    #   0  = Champion
    #   1  = #1
    #   2  = #2
    #   ...
    #   15 = #15
    #
    # movement:
    #   0 = no movement
    #   1 = moved up
    #  -1 = moved down
    #
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

    db.commit()
    db.close()


# ============================================================
# SIGNUP DATABASE FUNCTIONS
# ============================================================

def get_active_session(guild_id: int, signup_type: str):
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
    """, (guild_id, signup_type))

    session = cursor.fetchone()
    db.close()

    return session


def get_session(session_id: int):
    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        SELECT *
        FROM signup_sessions
        WHERE id = ?
    """, (session_id,))

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
        (guild_id, signup_type, message_id, channel_id, active, created_at)
        VALUES (?, ?, ?, ?, 1, ?)
    """, (
        guild_id,
        signup_type,
        message_id,
        channel_id,
        datetime.utcnow().isoformat()
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

    # Prevent the same Discord account signing up twice
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
        (session_id, discord_user_id, player_name, created_at)
        VALUES (?, ?, ?, ?)
    """, (
        session_id,
        discord_user_id,
        player_name,
        datetime.utcnow().isoformat()
    ))

    db.commit()
    db.close()

    return True


def get_signup_count(session_id: int):
    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        SELECT COUNT(*) AS count
        FROM signups
        WHERE session_id = ?
    """, (session_id,))

    count = cursor.fetchone()["count"]

    db.close()

    return count


def get_signups(session_id: int):
    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        SELECT player_name
        FROM signups
        WHERE session_id = ?
        ORDER BY id ASC
    """, (session_id,))

    signups = cursor.fetchall()

    db.close()

    return [row["player_name"] for row in signups]


def close_session(session_id: int):
    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        UPDATE signup_sessions
        SET active = 0,
            closed_at = ?
        WHERE id = ?
    """, (
        datetime.utcnow().isoformat(),
        session_id
    ))

    db.commit()
    db.close()


# ============================================================
# RANKING DATABASE FUNCTIONS
# ============================================================

def get_rankings(guild_id: int, weight: str):
    """
    Returns a dictionary:
        {
            rank: {
                discord_user_id,
                movement
            }
        }
    """

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        SELECT rank, discord_user_id, movement
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

    rankings = {}

    for row in rows:
        rankings[row["rank"]] = {
            "discord_user_id": row["discord_user_id"],
            "movement": row["movement"]
        }

    return rankings


def get_user_ranking(guild_id: int, discord_user_id: int):
    """
    Returns the user's ranking.

    Result:
        {
            weight: "...",
            rank: number
        }

    or None if they aren't ranked.
    """

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        SELECT weight, rank
        FROM rankings
        WHERE guild_id = ?
        AND discord_user_id = ?
        LIMIT 1
    """, (
        guild_id,
        discord_user_id
    ))

    result = cursor.fetchone()

    db.close()

    return result


def clear_movements(guild_id: int):
    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        UPDATE rankings
        SET movement = 0
        WHERE guild_id = ?
    """, (guild_id,))

    db.commit()
    db.close()


def save_rankings(
    guild_id: int,
    weight: str,
    rankings: dict
):
    """
    Completely replaces the rankings for a division.

    rankings format:

        {
            0: {
                "discord_user_id": 123,
                "movement": 0
            },
            1: {
                "discord_user_id": 456,
                "movement": 1
            }
        }
    """

    db = get_db()
    cursor = db.cursor()

    # Delete existing rankings for this division
    cursor.execute("""
        DELETE FROM rankings
        WHERE guild_id = ?
        AND weight = ?
    """, (
        guild_id,
        weight
    ))

    now = datetime.utcnow().isoformat()

    for rank, data in rankings.items():

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
            data["discord_user_id"],
            rank,
            data.get("movement", 0),
            now
        ))

    db.commit()
    db.close()


# ============================================================
# RANKING MOVEMENT LOGIC
# ============================================================

def update_ranking(
    guild_id: int,
    weight: str,
    user_id: int,
    new_rank: int
):
    """
    Updates a ranking using UFC-style movement.

    0 = Champion
    1-15 = ranked positions

    Returns:
        True, message
    """

    # --------------------------------------------------------
    # Check if user is currently ranked
    # --------------------------------------------------------

    old_position = get_user_ranking(
        guild_id,
        user_id
    )

    old_weight = None
    old_rank = None

    if old_position:
        old_weight = old_position["weight"]
        old_rank = old_position["rank"]

    # --------------------------------------------------------
    # If user is already Champion
    # --------------------------------------------------------

    if old_weight == weight and old_rank == 0 and new_rank == 0:
        return False, "❌ That fighter is already the champion."

    # --------------------------------------------------------
    # Get current division rankings
    # --------------------------------------------------------

    current = get_rankings(
        guild_id,
        weight
    )

    # --------------------------------------------------------
    # If moving from another division, remove them there
    # --------------------------------------------------------

    if old_position and old_weight != weight:

        old_rankings = get_rankings(
            guild_id,
            old_weight
        )

        if old_rank in old_rankings:
            del old_rankings[old_rank]

        # Re-number old division
        ordered = sorted(
            old_rankings.values(),
            key=lambda x: list(old_rankings.keys()).index(
                next(
                    k for k, v in old_rankings.items()
                    if v == x
                )
            )
        )

        rebuilt = {}

        for index, fighter in enumerate(ordered):
            rebuilt[index] = fighter

        save_rankings(
            guild_id,
            old_weight,
            rebuilt
        )

    # --------------------------------------------------------
    # If moving within the SAME division
    # --------------------------------------------------------

    if old_position and old_weight == weight:

        old_rank = old_position["rank"]

        # Remove fighter from their old position
        if old_rank in current:
            del current[old_rank]

        # Convert to ordered list
        fighters = [
            data
            for rank, data in sorted(
                current.items(),
                key=lambda x: x[0]
            )
        ]

        # ----------------------------------------------------
        # Insert fighter at new position
        # ----------------------------------------------------

        # Champion
        if new_rank == 0:

            old_champion = None

            if 0 in current:
                old_champion = current[0]

            # Current #1 etc.
            fighters = [
                data
                for rank, data in sorted(
                    current.items(),
                    key=lambda x: x[0]
                )
                if rank != 0
            ]

            new_rankings = {}

            # New champion
            new_rankings[0] = {
                "discord_user_id": user_id,
                "movement": 1
            }

            # Previous champion becomes #1
            if old_champion and old_champion["discord_user_id"] != user_id:
                fighters.insert(0, old_champion)

            # Rebuild #1-#15
            for index, fighter in enumerate(
                fighters[:15],
                start=1
            ):
                new_rankings[index] = fighter

            # Calculate movement
            new_rankings = calculate_movements(
                current,
                new_rankings,
                user_id
            )

            save_rankings(
                guild_id,
                weight,
                new_rankings
            )

            return True, "Ranking updated."

        # ----------------------------------------------------
        # Normal ranked position
        # ----------------------------------------------------

        fighters.insert(
            max(0, new_rank - 1),
            {
                "discord_user_id": user_id,
                "movement": 0
            }
        )

        new_rankings = {}

        # Keep existing champion
        champion = current.get(0)

        if champion:
            new_rankings[0] = champion

        # Rebuild #1-#15
        for index, fighter in enumerate(
            fighters[:15],
            start=1
        ):
            new_rankings[index] = fighter

        # Calculate movements
        new_rankings = calculate_movements(
            current,
            new_rankings,
            user_id
        )

        save_rankings(
            guild_id,
            weight,
            new_rankings
        )

        return True, "Ranking updated."

    # --------------------------------------------------------
    # User is NOT currently in this division
    # --------------------------------------------------------

    if new_rank == 0:

        old_champion = current.get(0)

        fighters = [
            data
            for rank, data in sorted(
                current.items(),
                key=lambda x: x[0]
            )
            if rank != 0
        ]

        new_rankings = {
            0: {
                "discord_user_id": user_id,
                "movement": 1
            }
        }

        if old_champion:
            fighters.insert(
                0,
                old_champion
            )

        for index, fighter in enumerate(
            fighters[:15],
            start=1
        ):
            new_rankings[index] = fighter

        new_rankings = calculate_movements(
            current,
            new_rankings,
            user_id
        )

        save_rankings(
            guild_id,
            weight,
            new_rankings
        )

        return True, "Ranking updated."

    # --------------------------------------------------------
    # Insert a new fighter at #1-#15
    # --------------------------------------------------------

    fighters = [
        data
        for rank, data in sorted(
            current.items(),
            key=lambda x: x[0]
        )
        if rank != 0
    ]

    fighters.insert(
        new_rank - 1,
        {
            "discord_user_id": user_id,
            "movement": 0
        }
    )

    new_rankings = {}

    # Champion remains champion
    if 0 in current:
        new_rankings[0] = current[0]

    # Rebuild rankings
    for index, fighter in enumerate(
        fighters[:15],
        start=1
    ):
        new_rankings[index] = fighter

    # Calculate movement
    new_rankings = calculate_movements(
        current,
        new_rankings,
        user_id
    )

    save_rankings(
        guild_id,
        weight,
        new_rankings
    )

    return True, "Ranking updated."


def calculate_movements(
    old_rankings: dict,
    new_rankings: dict,
    changed_user_id: int
):
    """
    Compares old and new positions.

    If a fighter's number decreases:
        moved UP

    If a fighter's number increases:
        moved DOWN

    Champion -> ranked:
        movement is treated as DOWN

    Ranked -> Champion:
        movement is treated as UP
    """

    old_positions = {}

    for rank, data in old_rankings.items():
        old_positions[data["discord_user_id"]] = rank

    for rank, data in new_rankings.items():

        user_id = data["discord_user_id"]

        old_rank = old_positions.get(user_id)

        # New fighter
        if old_rank is None:
            if user_id == changed_user_id:
                data["movement"] = 1
            else:
                data["movement"] = 0

            continue

        # Same position
        if old_rank == rank:
            data["movement"] = 0

        # Champion -> ranked
        elif old_rank == 0 and rank > 0:
            data["movement"] = -1

        # Ranked -> Champion
        elif old_rank > 0 and rank == 0:
            data["movement"] = 1

        # Moved up
        elif rank < old_rank:
            data["movement"] = 1

        # Moved down
        elif rank > old_rank:
            data["movement"] = -1

        else:
            data["movement"] = 0

    return new_rankings


# ============================================================
# REMOVE FIGHTER FROM RANKINGS
# ============================================================

def remove_from_rankings(
    guild_id: int,
    user_id: int
):
    """
    Removes a fighter from whichever division they are currently in.

    Returns:
        None
        OR
        (weight, old_rank)
    """

    current_position = get_user_ranking(
        guild_id,
        user_id
    )

    if not current_position:
        return None

    weight = current_position["weight"]
    old_rank = current_position["rank"]

    rankings = get_rankings(
        guild_id,
        weight
    )

    if old_rank not in rankings:
        return None

    # Remove fighter
    del rankings[old_rank]

    # Rebuild rankings
    new_rankings = {}

    # Keep champion if one exists
    if 0 in rankings:
        new_rankings[0] = rankings[0]

    # Ranked fighters
    ranked_fighters = [
        data
        for rank, data in sorted(
            rankings.items(),
            key=lambda x: x[0]
        )
        if rank != 0
    ]

    # Everything below the removed fighter moves up
    for index, fighter in enumerate(
        ranked_fighters[:15],
        start=1
    ):
        new_rankings[index] = fighter

    # Mark fighters who moved up
    for rank, data in new_rankings.items():

        if rank == 0:
            continue

        # Fighters below the removed fighter moved up
        if old_rank > 0 and rank >= old_rank:
            data["movement"] = 1
        else:
            data["movement"] = 0

    save_rankings(
        guild_id,
        weight,
        new_rankings
    )

    return weight, old_rank


# ============================================================
# DISCORD BOT
# ============================================================

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# ============================================================
# SIGNUP TYPES
# ============================================================

SIGNUP_INFO = {
    "fight_night": {
        "title": "PFO Fight Night Sign-Ups",
        "message": "PFO Fight Night Sign-Ups, Press The Button Below To Sign Up!",
        "button": "Sign Up For Fight Night"
    },

    "live_card": {
        "title": "PFO Live Card Sign Ups",
        "message": "PFO Live Card Sign Ups, Press The Button Below To Sign Up!",
        "button": "Sign Up For Live Card"
    }
}


# ============================================================
# SIGNUP EMBEDS
# ============================================================

def create_signup_embed(
    signup_type: str,
    session_id: int
):
    info = SIGNUP_INFO[signup_type]
    count = get_signup_count(session_id)

    embed = discord.Embed(
        title=info["title"],
        description=(
            f"{info['message']}\n\n"
            f"**Current Sign-Ups: {count}**"
        ),
        color=discord.Color.red()
    )

    embed.set_footer(
        text="Press the button below to enter your UFC 6 league name."
    )

    return embed


def create_closed_embed(
    signup_type: str,
    session_id: int
):
    info = SIGNUP_INFO[signup_type]
    count = get_signup_count(session_id)

    embed = discord.Embed(
        title=info["title"],
        description=(
            f"**Sign-Ups Closed! [{count} SIGN-UPS]!**\n\n"
            "This signup is no longer accepting entries."
        ),
        color=discord.Color.dark_grey()
    )

    return embed


# ============================================================
# SIGNUP MODAL
# ============================================================

class PlayerNameModal(
    discord.ui.Modal,
    title="PFO UFC 6 Sign-Up"
):

    player_name = discord.ui.TextInput(
        label="Enter your UFC 6 league name",
        placeholder="Example: Conor McGregor",
        min_length=1,
        max_length=50,
        required=True
    )

    def __init__(
        self,
        session_id: int,
        signup_type: str
    ):
        super().__init__()

        self.session_id = session_id
        self.signup_type = signup_type

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        session = get_session(
            self.session_id
        )

        if not session or session["active"] != 1:

            await interaction.response.send_message(
                "❌ This signup has already been closed.",
                ephemeral=True
            )

            return

        name = self.player_name.value.strip()

        success = add_signup(
            self.session_id,
            interaction.user.id,
            name
        )

        if not success:

            await interaction.response.send_message(
                "❌ You are already signed up for this card.",
                ephemeral=True
            )

            return

        count = get_signup_count(
            self.session_id
        )

        await interaction.response.send_message(
            f"✅ **{name}** has been added to the signup!\n\n"
            f"**Current Sign-Ups: {count}**",
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
# SIGNUP BUTTON
# ============================================================

class SignupView(discord.ui.View):

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

    @discord.ui.button(
        label="Sign Up",
        style=discord.ButtonStyle.green,
        custom_id="pfo_signup_button"
    )
    async def signup_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        session = get_session(
            self.session_id
        )

        if not session or session["active"] != 1:

            await interaction.response.send_message(
                "❌ This signup is closed.",
                ephemeral=True
            )

            return

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

        await interaction.response.send_modal(
            PlayerNameModal(
                self.session_id,
                self.signup_type
            )
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
    description="Paste the current signup list into this channel."
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

    names = get_signups(
        session["id"]
    )

    if not names:

        await interaction.response.send_message(
            "❌ Nobody has signed up yet.",
            ephemeral=True
        )

        return

    lines = []

    for number, name in enumerate(
        names,
        start=1
    ):
        lines.append(
            f"{number}. {name}"
        )

    count = len(names)

    embed = discord.Embed(
        title=SIGNUP_INFO[signup_type_value]["title"],
        description="\n".join(lines),
        color=discord.Color.red()
    )

    embed.set_footer(
        text=f"Total Sign-Ups: {count}"
    )

    await interaction.response.send_message(
        embed=embed
    )


# ============================================================
# RANKING DISPLAY HELPERS
# ============================================================

def get_rank_name(rank: int):

    if rank == 0:
        return "CHAMPION"

    return f"#{rank}"


def get_movement_icon(movement: int):

    if movement == 1:
        return "🟢⬆️"

    if movement == -1:
        return "🔴⬇️"

    return "▫️"


def create_ranking_embed(
    guild: discord.Guild,
    weight: str
):

    rankings = get_rankings(
        guild.id,
        weight
    )

    embed = discord.Embed(
        title=f"🏆 PFO UFC RANKINGS — {weight.upper()}",
        color=discord.Color.red()
    )

    # --------------------------------------------------------
    # Champion
    # --------------------------------------------------------

    champion = rankings.get(0)

    if champion:

        champion_mention = f"<@{champion['discord_user_id']}>"

        champion_text = (
            f"🏆 **CHAMPION**\n"
            f"{champion_mention}"
        )

        movement = champion.get(
            "movement",
            0
        )

        if movement == 1:
            champion_text += " 🟢⬆️"

        elif movement == -1:
            champion_text += " 🔴⬇️"

    else:

        champion_text = (
            "🏆 **CHAMPION**\n"
            "Vacant"
        )

    embed.add_field(
        name="Champion",
        value=champion_text,
        inline=False
    )

    # --------------------------------------------------------
    # #1-#15
    # --------------------------------------------------------

    ranking_lines = []

    for rank in range(1, 16):

        fighter = rankings.get(rank)

        if fighter:

            mention = (
                f"<@{fighter['discord_user_id']}>"
            )

            movement_icon = get_movement_icon(
                fighter.get("movement", 0)
            )

            ranking_lines.append(
                f"**#{rank}** {mention} {movement_icon}"
            )

        else:

            ranking_lines.append(
                f"**#{rank}** Vacant"
            )

    embed.add_field(
        name="Rankings",
        value="\n".join(ranking_lines),
        inline=False
    )

    embed.set_footer(
        text="🟢⬆️ Moved Up    🔴⬇️ Moved Down"
    )

    return embed


# ============================================================
# /RANKINGSP
# ============================================================

@bot.tree.command(
    name="rankingsp",
    description="Post the current UFC rankings for all 8 men's divisions."
)
async def rankingsp(
    interaction: discord.Interaction
):

    if not is_admin(interaction):

        await interaction.response.send_message(
            "❌ Only server administrators can post the rankings.",
            ephemeral=True
        )

        return

    await interaction.response.defer(
        ephemeral=True
    )

    # --------------------------------------------------------
    # Send one embed for every division
    # --------------------------------------------------------

    for weight in WEIGHTS.keys():

        embed = create_ranking_embed(
            interaction.guild,
            weight
        )

        await interaction.channel.send(
            embed=embed
        )

    await interaction.followup.send(
        "✅ All 8 UFC men's ranking boxes have been posted.",
        ephemeral=True
    )


# ============================================================
# RANKING WEIGHT CHOICES
# ============================================================

WEIGHT_CHOICES = [
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


# ============================================================
# RANKING POSITION CHOICES
# ============================================================

RANK_CHOICES = [
    app_commands.Choice(
        name="Champion",
        value="0"
    ),

    app_commands.Choice(
        name="#1",
        value="1"
    ),

    app_commands.Choice(
        name="#2",
        value="2"
    ),

    app_commands.Choice(
        name="#3",
        value="3"
    ),

    app_commands.Choice(
        name="#4",
        value="4"
    ),

    app_commands.Choice(
        name="#5",
        value="5"
    ),

    app_commands.Choice(
        name="#6",
        value="6"
    ),

    app_commands.Choice(
        name="#7",
        value="7"
    ),

    app_commands.Choice(
        name="#8",
        value="8"
    ),

    app_commands.Choice(
        name="#9",
        value="9"
    ),

    app_commands.Choice(
        name="#10",
        value="10"
    ),

    app_commands.Choice(
        name="#11",
        value="11"
    ),

    app_commands.Choice(
        name="#12",
        value="12"
    ),

    app_commands.Choice(
        name="#13",
        value="13"
    ),

    app_commands.Choice(
        name="#14",
        value="14"
    ),

    app_commands.Choice(
        name="#15",
        value="15"
    )
]


# ============================================================
# /RANKINGSU
# ============================================================

@bot.tree.command(
    name="rankingsu",
    description="Update a fighter's UFC ranking."
)
@app_commands.describe(
    weight="Which UFC weight division?",
    user="Which Discord user are you ranking?",
    ranking="What position should they be placed at?"
)
@app_commands.choices(
    weight=WEIGHT_CHOICES,
    ranking=RANK_CHOICES
)
async def rankingsu(
    interaction: discord.Interaction,
    weight: app_commands.Choice[str],
    user: discord.Member,
    ranking: app_commands.Choice[str]
):

    # --------------------------------------------------------
    # Admin check
    # --------------------------------------------------------

    if not is_admin(interaction):

        await interaction.response.send_message(
            "❌ Only server administrators can update rankings.",
            ephemeral=True
        )

        return

    weight_value = weight.value
    new_rank = int(ranking.value)

    # --------------------------------------------------------
    # Prevent bots being ranked
    # --------------------------------------------------------

    if user.bot:

        await interaction.response.send_message(
            "❌ Bots cannot be placed in the UFC rankings.",
            ephemeral=True
        )

        return

    # --------------------------------------------------------
    # Check whether user is already ranked elsewhere
    # --------------------------------------------------------

    current_position = get_user_ranking(
        interaction.guild.id,
        user.id
    )

    # --------------------------------------------------------
    # If they are already in another division, tell admin
    # --------------------------------------------------------

    if current_position:

        old_weight = current_position["weight"]
        old_rank = current_position["rank"]

        if old_weight != weight_value:

            old_rank_name = get_rank_name(
                old_rank
            )

            confirmation_text = (
                f"⚠️ {user.mention} is currently ranked "
                f"{old_rank_name} in **{old_weight}**.\n\n"
                f"They will be moved to **{weight_value}** "
                f"at **{get_rank_name(new_rank)}**."
            )

            # We don't need a confirmation button;
            # the admin explicitly selected the command.
            # Continue automatically.

    # --------------------------------------------------------
    # Update rankings
    # --------------------------------------------------------

    success, message = update_ranking(
        interaction.guild.id,
        weight_value,
        user.id,
        new_rank
    )

    if not success:

        await interaction.response.send_message(
            message,
            ephemeral=True
        )

        return

    # --------------------------------------------------------
    # Get resulting ranking
    # --------------------------------------------------------

    updated_rankings = get_rankings(
        interaction.guild.id,
        weight_value
    )

    placed_fighter = updated_rankings.get(
        new_rank
    )

    # --------------------------------------------------------
    # Ranking name
    # --------------------------------------------------------

    rank_name = get_rank_name(
        new_rank
    )

    await interaction.response.send_message(
        f"✅ {user.mention} is now **{rank_name}** "
        f"in **{weight_value}**.\n\n"
        f"The rankings have automatically been adjusted.",
        ephemeral=True
    )

    # --------------------------------------------------------
    # Post updated ranking box
    # --------------------------------------------------------

    await interaction.channel.send(
        embed=create_ranking_embed(
            interaction.guild,
            weight_value
        )
    )


# ============================================================
# /RANKINGSR
# ============================================================

@bot.tree.command(
    name="rankingsr",
    description="Remove a fighter from the UFC rankings."
)
@app_commands.describe(
    user="Which Discord user should be removed?"
)
async def rankingsr(
    interaction: discord.Interaction,
    user: discord.Member
):

    if not is_admin(interaction):

        await interaction.response.send_message(
            "❌ Only server administrators can remove fighters from the rankings.",
            ephemeral=True
        )

        return

    # --------------------------------------------------------
    # Check current ranking
    # --------------------------------------------------------

    current_position = get_user_ranking(
        interaction.guild.id,
        user.id
    )

    if not current_position:

        await interaction.response.send_message(
            f"❌ {user.mention} is not currently ranked.",
            ephemeral=True
        )

        return

    weight = current_position["weight"]
    old_rank = current_position["rank"]

    old_rank_name = get_rank_name(
        old_rank
    )

    # --------------------------------------------------------
    # Remove fighter
    # --------------------------------------------------------

    result = remove_from_rankings(
        interaction.guild.id,
        user.id
    )

    if not result:

        await interaction.response.send_message(
            "❌ Something went wrong while removing that fighter.",
            ephemeral=True
        )

        return

    # --------------------------------------------------------
    # Tell admin
    # --------------------------------------------------------

    await interaction.response.send_message(
        f"✅ {user.mention} has been removed from "
        f"**{weight} {old_rank_name}**.\n\n"
        f"The remaining rankings have automatically moved up.",
        ephemeral=True
    )

    # --------------------------------------------------------
    # Post updated ranking
    # --------------------------------------------------------

    await interaction.channel.send(
        embed=create_ranking_embed(
            interaction.guild,
            weight
        )
    )


# ============================================================
# BOT READY
# ============================================================

@bot.event
async def on_ready():

    print(
        f"Logged in as {bot.user}"
    )

    print(
        f"Bot ID: {bot.user.id}"
    )

    # Setup database
    setup_database()

    # --------------------------------------------------------
    # Register slash commands
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
    # Re-register persistent signup views
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

        bot.add_view(
            SignupView(
                session["id"],
                session["signup_type"]
            )
        )

    print(
        f"Loaded {len(active_sessions)} "
        f"active signup session(s)."
    )

    print(
        "PFO Rankings system loaded."
    )


# ============================================================
# START BOT
# ============================================================

if not TOKEN:

    raise RuntimeError(
        "DISCORD_TOKEN is missing from your environment variables."
    )


bot.run(TOKEN)