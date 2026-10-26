"""
PFO WEBSITE

Runs inside the Discord bot on Railway and reads the bot's own
database, so every page is always up to date: log a result, move a
ranking or crown a champion and the website shows it straight away.
Nobody ever has to edit the website.

Pages
  /             Home: next event + countdown, champions, latest results,
                pound for pound, title history
  /rankings     Every division (tabs), with movement since the last
                rankings update
  /fighters     Every fighter, searchable
  /fighter/ID   A fighter's profile (record, how they win, fight
                history, title reigns, fight stats)
  /results      Every event's results, laid out like /recap
  /titles       Title history for every division

Railway Variables (all optional):
  DISCORD_INVITE   your Discord invite link, e.g. https://discord.gg/abc123
                   (the "Join Discord" buttons are hidden until it's set)
  TWITCH_URL       your Twitch channel (shows a "Watch live" button)
  WEBSITE          set to off to turn the website off
  PORT             Railway sets this itself, leave it alone

In Railway: open the bot's service → Settings → Networking →
Generate Domain. That's the website's address.
"""

import html
import os
import re
import time
import traceback
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

try:
    from aiohttp import web
except ImportError:  # only when testing without discord.py installed
    web = None


DISCORD_INVITE = os.getenv("DISCORD_INVITE", "").strip()
TWITCH_URL = os.getenv("TWITCH_URL", "").strip()
WEBSITE_ON = os.getenv("WEBSITE", "on").strip().lower() not in ("off", "no", "false", "0")
SITE_VERSION = "3"            # bump if the styles change and browsers keep the old ones
CACHE_SECONDS = 20            # pages are rebuilt at most every 20 seconds
RESULTS_PER_PAGE = 6          # events per page on /results


def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


# ============================================================
# Talking to the bot
# ============================================================

class Ctx:
    """The bot's functions plus the server the site is about."""

    def __init__(self, g, guild):
        self.g = g
        self.guild = guild
        self.gid = guild.id

    def __getattr__(self, name):
        try:
            return self.g[name]
        except KeyError:
            raise AttributeError(name)

    def q(self, sql, params=()):
        return self.g["discipline_query"](sql, params)


def pick_guild(bot, g):
    wanted = g.get("GUILD_ID")
    guilds = list(getattr(bot, "guilds", []))

    if wanted and str(wanted).isdigit():
        for guild in guilds:
            if guild.id == int(wanted):
                return guild

    return guilds[0] if guilds else None


def row_get(row, key, default=None):
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


# ------------------------------------------------------------
# Fighters
# ------------------------------------------------------------

def fighter_name(ctx, user_id, saved=None):
    member = ctx.guild.get_member(user_id) if user_id else None

    if member:
        return ctx.name_without_record(member.display_name)

    if saved and saved != "Unknown Fighter":
        return ctx.name_without_record(saved)

    if user_id:
        cached = ctx.g.get("DEPARTED_NAME_CACHE", {}).get(user_id)
        if cached:
            return cached
        rows = ctx.q("""
            SELECT winner_name AS name FROM fights WHERE guild_id = ? AND winner_id = ? AND winner_name != 'Unknown Fighter'
            UNION ALL
            SELECT loser_name AS name FROM fights WHERE guild_id = ? AND loser_id = ? AND loser_name != 'Unknown Fighter'
        """, (ctx.gid, user_id, ctx.gid, user_id))
        if rows:
            return ctx.name_without_record(rows[-1]["name"])

    return "Former fighter"


def avatar_url(ctx, user_id, size=256):
    member = ctx.guild.get_member(user_id) if user_id else None
    avatar = getattr(member, "display_avatar", None)

    if not avatar:
        return None

    try:
        return str(avatar.replace(size=size, static_format="png").url)
    except Exception:
        return str(getattr(avatar, "url", "")) or None


def initials(name):
    parts = [part for part in re.split(r"\s+", name or "") if part[:1].isalnum()]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def split_name(name):
    """'Joshua Van' -> ('JOSHUA', 'VAN') for the big stacked names."""
    parts = (name or "").upper().split()
    if len(parts) < 2:
        return (name or "").upper(), ""
    return " ".join(parts[:-1]), parts[-1]


def fighter_url(user_id):
    return f"/fighter/{user_id}" if user_id else None


def stats_for(ctx, user_id, cache=None):
    if cache is not None and user_id in cache:
        return cache[user_id]
    try:
        stats = ctx.get_fighter_stats(ctx.gid, user_id)
    except Exception:
        stats = None
    if cache is not None:
        cache[user_id] = stats
    return stats


def rank_in(ctx, user_id, weight):
    if not weight or not user_id:
        return None
    row = ctx.get_user_ranking_in_division(ctx.gid, user_id, weight)
    return row["rank"] if row else None


def rank_label(rank):
    if rank is None:
        return None
    return "Champion" if rank == 0 else f"#{rank}"


def all_fighter_ids(ctx):
    ids = set()

    for row in ctx.q("SELECT winner_id AS id FROM fights WHERE guild_id = ? AND winner_id IS NOT NULL "
                     "UNION SELECT loser_id FROM fights WHERE guild_id = ? AND loser_id IS NOT NULL",
                     (ctx.gid, ctx.gid)):
        ids.add(row["id"])

    for row in ctx.q("SELECT discord_user_id AS id FROM starting_records WHERE guild_id = ?", (ctx.gid,)):
        ids.add(row["id"])

    for row in ctx.q("SELECT DISTINCT discord_user_id AS id FROM rankings WHERE guild_id = ?", (ctx.gid,)):
        ids.add(row["id"])

    return ids


def main_division(ctx, user_id):
    for row in ctx.get_user_rankings(ctx.gid, user_id):
        if row["weight"] != ctx.P4P_WEIGHT:
            return row["weight"]

    rows = ctx.q("""
        SELECT division, COUNT(*) AS n FROM fights
        WHERE guild_id = ? AND (winner_id = ? OR loser_id = ?) AND division IS NOT NULL
        GROUP BY division ORDER BY n DESC LIMIT 1
    """, (ctx.gid, user_id, user_id))

    return rows[0]["division"] if rows else None


# ------------------------------------------------------------
# Dates
# ------------------------------------------------------------

def uk_time(unix):
    moment = datetime.fromtimestamp(int(unix), timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return moment.astimezone(ZoneInfo("Europe/London"))
    except Exception:
        return moment


def event_when(unix):
    moment = uk_time(unix)
    return f"{moment:%a} {moment.day} {moment:%b} · {moment:%H:%M} UK"


def nice_day(text):
    try:
        day = datetime.fromisoformat((text or "")[:10]).date()
        return f"{day.day} {day:%b %Y}"
    except Exception:
        return ""


def short_day(text):
    try:
        day = datetime.fromisoformat((text or "")[:10]).date()
        return f"{day.day} {day:%b}"
    except Exception:
        return ""


def fight_day(fight):
    return row_get(fight, "fight_date") or (row_get(fight, "created_at", "") or "")[:10]


def method_short(ctx, fight):
    """'KO/TKO · R2', 'SUB · R4', 'DECISION', 'DRAW'."""
    method = fight["method"]
    labels = {"KO/TKO": "KO/TKO", "SUB": "SUB", "DEC": "DECISION", "DQ": "DQ", "DRAW": "DRAW", "NC": "NO CONTEST"}
    text = labels.get(method, method)
    rnd = row_get(fight, "round")
    if rnd and method not in ("DEC", "DRAW", "NC"):
        text += f" · R{rnd}"
    return text


def method_class(method):
    return {"KO/TKO": "m-ko", "SUB": "m-sub", "DQ": "m-ko"}.get(method, "m-dec")


# ============================================================
# Page shell
# ============================================================

NAV = [
    ("home", "/", "Home"),
    ("rankings", "/rankings", "Rankings"),
    ("fighters", "/fighters", "Fighters"),
    ("results", "/results", "Results"),
    ("titles", "/titles", "Titles"),
]


def logo_html(ctx, size_class="logo"):
    url = ctx.asset_url(ctx.LOGO_FILE)
    if url:
        return f'<img class="{size_class}" src="{esc(url)}" alt="">'
    return f'<span class="{size_class} logo-text" aria-hidden="true">PFO</span>'


def discord_button(label="Join Discord", extra=""):
    if not DISCORD_INVITE:
        return ""
    return (f'<a class="btn btn-ghost {extra}" href="{esc(DISCORD_INVITE)}" target="_blank" rel="noopener">'
            f'{esc(label)}</a>')


def page(ctx, title, active, body, description=None, solid_nav=True):
    full_title = "PFO · Prestige Fighting Organisation" if active == "home" else f"{title} · PFO"
    description = description or "Rankings, fighters, results and champions of the Prestige Fighting Organisation, a UFC 6 league on Discord."
    logo = ctx.asset_url(ctx.LOGO_FILE)

    links = "".join(
        '<a href="' + href + '"' + (' aria-current="page"' if key == active else '') + '>' + label + '</a>'
        for key, href, label in NAV
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(full_title)}</title>
<meta name="description" content="{esc(description)}">
<meta name="theme-color" content="#C9A24A">
<meta property="og:site_name" content="Prestige Fighting Organisation">
<meta property="og:title" content="{esc(title if active != 'home' else 'Prestige Fighting Organisation')}">
<meta property="og:description" content="{esc(description)}">
{f'<meta property="og:image" content="{esc(logo)}"><link rel="icon" href="{esc(logo)}">' if logo else ''}
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;600;700;800&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/assets/site.css?v={SITE_VERSION}">
<script src="/assets/site.js?v={SITE_VERSION}" defer></script>
</head>
<body class="{'' if solid_nav else 'over-hero'}">
<a class="skip" href="#main">Skip to content</a>
<header class="nav">
  <nav class="wrap nav-in" aria-label="Main">
    <a class="brand" href="/" aria-label="PFO home">{logo_html(ctx, 'logo-sm')}<span>PFO</span></a>
    <div class="nav-links">{links}</div>
    {discord_button()}
  </nav>
</header>
<main id="main">
{body}
</main>
<footer class="foot">
  <div class="wrap foot-in">
    <span class="foot-brand">{logo_html(ctx, 'logo-xs')}© {datetime.now(timezone.utc).year} Prestige Fighting Organisation</span>
    <span class="live"><span class="dot" aria-hidden="true"></span>Live data from the PFO Discord</span>
  </div>
</footer>
</body>
</html>"""


def page_hero(eyebrow, title, mark, extra=""):
    return f"""
<section class="phero">
  <div class="phero-mark s-mark" aria-hidden="true">{esc(mark)}</div>
  <div class="wrap phero-in">
    <span class="eyebrow l-up"><span class="dot" aria-hidden="true"></span>{esc(eyebrow)}</span>
    <h1 class="d phero-title l-title">{esc(title)}</h1>
    {extra}
  </div>
</section>"""


def avatar_html(ctx, user_id, name, cls="av"):
    url = avatar_url(ctx, user_id)
    if url:
        return f'<span class="{cls}"><img src="{esc(url)}" alt="" loading="lazy"></span>'
    return f'<span class="{cls}" aria-hidden="true">{esc(initials(name))}</span>'


def name_link(ctx, user_id, name, cls=""):
    url = fighter_url(user_id)
    if url:
        return f'<a class="{cls}" href="{url}">{esc(name)}</a>'
    return f'<span class="{cls}">{esc(name)}</span>'


def stagger(i, n=4):
    return ["rv", "rv2", "rv3", "rv4"][i % n]


# ============================================================
# Data for the home page
# ============================================================

def next_event(ctx):
    """{'name', 'when' (unix or None), 'bouts': [(a, b, weight)]} or None."""

    now = time.time()

    def card_for(event_name):
        key = ctx.event_key(event_name or "")
        for card in ctx.q("SELECT * FROM fight_cards WHERE guild_id = ? ORDER BY id DESC LIMIT 15", (ctx.gid,)):
            if ctx.event_key(card["event"]) == key:
                return [(b["fighter_a"], b["fighter_b"], row_get(b, "weight")) for b in ctx.get_fight_card_bouts(card["id"])]
        return None

    # 1) A Live Card that's been DMed with a date that hasn't passed.
    for notice in ctx.q("SELECT * FROM card_notices WHERE guild_id = ? AND kind = 'live_card' ORDER BY id DESC LIMIT 3",
                        (ctx.gid,)):
        when = str(row_get(notice, "event_date", ""))
        if not when.isdigit() or int(when) < now - 4 * 3600:
            continue

        name = row_get(notice, "event") or "Live Card"
        bouts = card_for(name)

        if not bouts:
            fighters = ctx.get_card_fighters(notice["id"])
            seen, bouts = set(), []
            for row in fighters:
                if row["user_id"] in seen:
                    continue
                seen.add(row["user_id"])
                if row["opponent_id"]:
                    seen.add(row["opponent_id"])
                    bouts.append((row["user_id"], row["opponent_id"], None))
            bouts = [(a, b, ctx.auto_weight(ctx.gid, a, b)) for a, b, _ in bouts]

        return {"name": name, "when": int(when), "bouts": bouts}

    # 2) The last saved card, if none of its fights have been logged yet.
    cards = ctx.q("SELECT * FROM fight_cards WHERE guild_id = ? ORDER BY id DESC LIMIT 1", (ctx.gid,))

    if cards:
        card = cards[0]
        try:
            age = now - datetime.fromisoformat(card["created_at"]).timestamp()
        except Exception:
            age = 0
        if age < 21 * 86400 and not ctx.event_fights(ctx.gid, card["event"]):
            when = None
            key = ctx.event_key(card["event"])
            for market in ctx.q("SELECT event, closes_at FROM bet_markets WHERE guild_id = ? ORDER BY id DESC LIMIT 10", (ctx.gid,)):
                if ctx.event_key(market["event"]) == key:
                    when = market["closes_at"]
                    break
            if when is None:
                for poll in ctx.q("SELECT event, deadline FROM vote_polls WHERE guild_id = ? ORDER BY id DESC LIMIT 10", (ctx.gid,)):
                    if ctx.event_key(poll["event"]) == key:
                        when = poll["deadline"]
                        break
            bouts = [(b["fighter_a"], b["fighter_b"], row_get(b, "weight")) for b in ctx.get_fight_card_bouts(card["id"])]
            if bouts:
                return {"name": card["event"], "when": when if when and when > now - 4 * 3600 else None, "bouts": bouts}

    return None


def open_signups(ctx):
    found = []
    for signup_type in ("live_card", "fight_night"):
        session = ctx.get_active_session(ctx.gid, signup_type)
        if session:
            found.append((ctx.SIGNUP_INFO[signup_type]["title"], row_get(session, "closes_at")))
    return found


def champions(ctx, cache):
    rows = []
    for weight in ctx.DIVISION_NAMES:
        champ = next((r for r in ctx.get_division_rankings(ctx.gid, weight) if r["rank"] == 0), None)
        if not champ:
            continue
        user_id = champ["discord_user_id"]
        reigns = ctx.title_lineage(ctx.gid, weight)[0]
        reign = reigns[-1] if reigns and reigns[-1]["user_id"] == user_id else None
        rows.append({
            "weight": weight,
            "user_id": user_id,
            "name": fighter_name(ctx, user_id),
            "stats": stats_for(ctx, user_id, cache),
            "reign": reign,
        })
    return rows


def defences_text(reign):
    if not reign:
        return "Champion"
    if not reign["defences"]:
        return "New champion"
    return f"{reign['defences']} defence{'s' if reign['defences'] != 1 else ''}"


def latest_event_results(ctx):
    events = ctx.recent_events(ctx.gid)
    if events:
        name = events[0]
        fights = ctx.event_fights(ctx.gid, name)
        layout, title, _ = ctx.load_recap_layout(ctx.gid, name, fights)
        grouped = ctx.ordered_sections(layout)
        by_id = {f["id"]: f for f in fights}
        ordered = [by_id[fid] for section in ctx.RECAP_SECTIONS for fid in grouped.get(section, [])]
        when = fight_day(fights[-1]) if fights else ""
        return (title or name), when, ordered[:15]

    fights = ctx.q("SELECT * FROM fights WHERE guild_id = ? ORDER BY COALESCE(fight_date, substr(created_at,1,10)) DESC, id DESC LIMIT 6",
                   (ctx.gid,))
    return "Latest", fight_day(fights[0]) if fights else "", list(fights)


def title_moments(ctx, limit=4):
    """Recent title changes and defences across every division."""
    moments = []
    for weight in ctx.DIVISION_NAMES:
        reigns, outcomes = ctx.title_lineage(ctx.gid, weight)
        if not outcomes:
            continue
        for fight in ctx.q("SELECT * FROM fights WHERE guild_id = ? AND title_fight = 1 AND division = ?", (ctx.gid, weight)):
            outcome = outcomes.get(fight["id"])
            if outcome in ("new", "first", "defence"):
                moments.append((fight_day(fight), fight["id"], weight, outcome, fight))
    moments.sort(key=lambda m: (m[0], m[1]), reverse=True)
    return moments[:limit]


def moment_text(ctx, outcome, fight):
    loser = fighter_name(ctx, fight["loser_id"], fight["loser_name"])
    how = ctx.fight_method_text(fight)
    if outcome == "defence":
        return f"Defended against {loser} · {how}"
    if outcome == "first":
        return f"Won the first title vs {loser} · {how}"
    return f"Took the belt from {loser} · {how}"


# ============================================================
# HOME
# ============================================================

def render_home(ctx):
    cache = {}
    event = next_event(ctx)
    signups = open_signups(ctx)
    champs = champions(ctx, cache)
    results_title, results_day, results = latest_event_results(ctx)

    # ---------- hero chip ----------
    chip = ""
    if event:
        chip_text = event["name"].upper() + (f" · {event_when(event['when']).upper()}" if event["when"] else "")
        chip = f'<a href="#event" class="chip l-chip btn"><span class="dot dot-red" aria-hidden="true"></span>{esc(chip_text)}</a>'
    elif signups:
        title, closes = signups[0]
        extra = f" · CLOSES {event_when(closes).upper()}" if closes else ""
        target = esc(DISCORD_INVITE) if DISCORD_INVITE else "#join"
        chip = f'<a href="{target}" class="chip l-chip btn"><span class="dot" aria-hidden="true"></span>{esc(title.upper())} OPEN{esc(extra)}</a>'

    hero = f"""
<section class="hero" aria-label="Prestige Fighting Organisation">
  <div class="hero-grid" aria-hidden="true"></div>
  <div class="hero-glow s-glow" aria-hidden="true"></div>
  <div class="hero-mark s-mark-par" aria-hidden="true">PFO</div>
  <div class="hero-in">
    <div class="s-hero"><div class="hero-logo l-logo">{logo_html(ctx, 'logo-hero')}</div></div>
    <h1 class="hero-name s-name">
      <span class="d hero-big l-name">PRESTIGE</span>
      <span class="hero-sub l-sub">FIGHTING ORGANISATION</span>
    </h1>
    <div class="hero-rule l-rule" aria-hidden="true"></div>
    {chip}
    <a href="#numbers" class="cue l-cue" aria-label="Scroll down">SCROLL<span aria-hidden="true" class="cue-line"><span class="cue-bar"></span></span></a>
  </div>
</section>"""

    # ---------- ticker: latest card's results, then pound for pound ----------
    parts = []

    def head(text, cls=""):
        return f'<span class="tick tick-head {cls}"><span class="tick-chip">{esc(text)}</span></span>'

    if event:
        parts.append(f'<span class="tick"><span class="tick-tag">NEXT</span>{esc(event["name"])}'
                     + (f'<span class="tick-dim">{esc(event_when(event["when"]))}</span>' if event["when"] else "")
                     + '</span>')

    if results:
        parts.append(head(f"{results_title} results"))
        for fight in results:
            w = fighter_name(ctx, fight["winner_id"], fight["winner_name"])
            l = fighter_name(ctx, fight["loser_id"], fight["loser_name"])
            joiner = "vs" if fight["method"] in ("DRAW", "NC") else "def."
            belt = '<span class="tick-belt">TITLE</span>' if fight["title_fight"] else ""
            parts.append(
                f'<span class="tick">{belt}<b>{esc(w)}</b><span class="tick-dim">{joiner}</span>{esc(l)}'
                f'<span class="tick-m {method_class(fight["method"])}">{esc(method_short(ctx, fight))}</span></span>'
            )

    p4p = ctx.get_division_rankings(ctx.gid, ctx.P4P_WEIGHT)[:10]
    if p4p:
        parts.append(head("Pound for pound", "tick-head-alt"))
        for row in p4p:
            uid = row["discord_user_id"]
            stats = stats_for(ctx, uid, cache)
            parts.append(
                f'<span class="tick"><span class="tick-rank{" gold" if row["rank"] == 1 else ""}">#{row["rank"]}</span>'
                f'<b>{esc(fighter_name(ctx, uid))}</b>'
                + (f'<span class="tick-dim">{esc(ctx.stats_record(stats))}</span>' if stats else "")
                + '</span>'
            )

    for title, closes in signups:
        parts.append(f'<span class="tick"><span class="tick-tag">SIGN-UPS OPEN</span>{esc(title)}</span>')

    ticker = ""
    if parts:
        spans = "".join(parts)
        seconds = max(30, len(parts) * 4)   # same speed however long the card is
        ticker = (f'<div class="ticker" aria-label="Latest results and pound for pound rankings">'
                  f'<span class="tick-live"><span class="dot dot-red" aria-hidden="true"></span>LIVE</span>'
                  f'<div class="tick-win"><div class="track" style="animation-duration:{seconds}s">{spans}'
                  f'<span aria-hidden="true" class="track-copy">{spans}</span></div></div></div>')

    # ---------- numbers ----------
    total = ctx.q("SELECT COUNT(*) AS n FROM fights WHERE guild_id = ?", (ctx.gid,))[0]["n"]
    finishes = ctx.q("SELECT COUNT(*) AS n FROM fights WHERE guild_id = ? AND method IN ('KO/TKO','SUB')", (ctx.gid,))[0]["n"]
    roster = sum(1 for uid in all_fighter_ids(ctx) if ctx.guild.get_member(uid))
    events = len(ctx.recent_events(ctx.gid))
    numbers = [(total, "FIGHTS LOGGED"), (finishes, "FINISHES"), (roster, "FIGHTERS"), (events, "EVENTS")]
    numbers_html = "".join(
        f'<div class="num {stagger(i)}"><span class="d num-v" data-count="{v}">{v:,}</span><span class="num-l">{l}</span></div>'
        for i, (v, l) in enumerate(numbers)
    )

    # ---------- next event ----------
    event_html = ""
    if event and event["bouts"]:
        a, b, weight = event["bouts"][-1]
        title_fight = rank_in(ctx, a, weight) == 0 or rank_in(ctx, b, weight) == 0

        def corner(uid, side):
            name = fighter_name(ctx, uid)
            first, last = split_name(name)
            stats = stats_for(ctx, uid, cache) or {}
            rank = rank_label(rank_in(ctx, uid, weight))
            colour = "RED" if side == "a" else "BLUE"
            tag = f"{colour} CORNER" + (f" · {rank.upper()}" if rank else "")
            stat_items = [
                (ctx.stats_record(stats) if stats else "0-0-0", "RECORD"),
                (stats.get("streak") or "–", "STREAK"),
                (f"{stats['finish_rate']}%" if stats.get("finish_rate") is not None else "–", "FINISHES"),
            ]
            if side == "b":
                stat_items = stat_items[::-1]
            figs = "".join(f'<span><strong class="d">{esc(v)}</strong>{l}</span>' for v, l in stat_items)
            return (f'<a href="{fighter_url(uid)}" class="corner corner-{side} {"sl" if side == "a" else "sr"}">'
                    f'<span class="corner-top">{avatar_html(ctx, uid, name, "av corner-av")}<span class="corner-tag">{esc(tag)}</span></span>'
                    f'<span class="d corner-name">{esc(first)}<br>{esc(last)}</span>'
                    f'<span class="corner-stats">{figs}</span></a>')

        countdown = ""
        if event["when"] and event["when"] > time.time():
            countdown = f"""
        <div class="countdown" role="timer" aria-label="Time until {esc(event['name'])}" data-countdown="{event['when']}">
          <div><b class="d" data-unit="d">--</b><span>DAYS</span></div>
          <div><b class="d" data-unit="h">--</b><span>HRS</span></div>
          <div><b class="d" data-unit="m">--</b><span>MIN</span></div>
          <div><b class="d" data-unit="s">--</b><span>SEC</span></div>
        </div>"""

        when_line = f"NEXT EVENT · {event_when(event['when']).upper()}" if event["when"] else "NEXT EVENT"
        detail = " · ".join(part for part in [
            f"{weight} championship" if title_fight and weight else (weight or ""),
            "main event",
        ] if part)

        card_rows = ""
        for number, (fa, fb, fw) in enumerate(reversed(event["bouts"]), start=1):
            label = "MAIN EVENT" if number == 1 else "CO-MAIN" if number == 2 else f"FIGHT {len(event['bouts']) - number + 1}"
            ra = rank_label(rank_in(ctx, fa, fw))
            rb = rank_label(rank_in(ctx, fb, fw))
            card_rows += f"""
          <li class="row card-row {stagger(number)}">
            <span class="card-slot">{label}</span>
            <span class="card-fight d">{name_link(ctx, fa, fighter_name(ctx, fa))}{f' <small>{esc(ra)}</small>' if ra else ''}<span class="vs-s">VS</span>{name_link(ctx, fb, fighter_name(ctx, fb))}{f' <small>{esc(rb)}</small>' if rb else ''}</span>
            <span class="card-weight">{esc((fw or '').upper())}</span>
          </li>"""

        buttons = '<a class="btn btn-gold" href="#card">FULL FIGHT CARD</a>'
        if TWITCH_URL:
            buttons += (f'<a class="btn btn-ghost" href="{esc(TWITCH_URL)}" target="_blank" rel="noopener">'
                        '<svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><polygon points="6 4 20 12 6 20"/></svg>WATCH LIVE</a>')

        event_html = f"""
    <section id="event" class="block" aria-labelledby="ev-title">
      <div class="drift" aria-hidden="true">MAIN EVENT</div>
      <div class="head rv">
        <div class="head-t">
          <span class="eyebrow">{esc(when_line)}</span>
          <h2 id="ev-title" class="d h-xl">{esc(event['name'].upper())}</h2>
        </div>
        {countdown}
      </div>
      <div class="versus">
        {corner(a, 'a')}
        <div class="vs d" aria-hidden="true"><span class="vs-t">VS</span></div>
        {corner(b, 'b')}
      </div>
      <div class="ev-foot rv2">
        <p class="muted">{esc(detail.capitalize())}</p>
        <div class="btns">{buttons}</div>
      </div>
      <div id="card" class="card-list">
        <h3 class="eyebrow rv">FULL CARD · {len(event['bouts'])} FIGHT{'S' if len(event['bouts']) != 1 else ''}</h3>
        <ol class="list">{card_rows}</ol>
      </div>
    </section>"""
    elif signups:
        lines = "".join(
            f'<li class="row so-row rv2"><span class="d so-name">{esc(title)}</span>'
            f'<span class="muted">{("Closes " + esc(event_when(closes))) if closes else "Open now"}</span></li>'
            for title, closes in signups
        )
        event_html = f"""
    <section id="event" class="block" aria-labelledby="ev-title">
      <div class="head rv"><div class="head-t">
        <span class="eyebrow"><span class="dot" aria-hidden="true"></span>SIGN-UPS OPEN</span>
        <h2 id="ev-title" class="d h-xl">GET ON THE CARD</h2>
      </div></div>
      <ul class="list">{lines}</ul>
      <div class="btns rv3">{discord_button('Sign up in the Discord', 'btn-gold-solid')}</div>
    </section>"""

    # ---------- champions ----------
    champ_cards = ""
    for i, champ in enumerate(champs):
        record = ctx.stats_record(champ["stats"]) if champ["stats"] else ""
        champ_cards += f"""
        <a href="{fighter_url(champ['user_id'])}" class="champ {stagger(i)}">
          <span class="champ-div">{esc(champ['weight'].upper())}</span>
          <span class="champ-who">{avatar_html(ctx, champ['user_id'], champ['name'], 'av av-gold')}<span class="d champ-name">{esc(champ['name'])}</span></span>
          <span class="champ-foot"><span>{esc(record)}</span><span>{esc(defences_text(champ['reign']))}</span></span>
        </a>"""

    if not champ_cards:
        champ_cards = '<p class="empty">No champions crowned yet. Titles are won in the Discord.</p>'

    champs_html = f"""
    <section class="block bt" aria-labelledby="ch-title">
      <div class="drift" aria-hidden="true">CHAMPIONS</div>
      <div class="head rv">
        <div class="head-t"><span class="eyebrow">CURRENT TITLEHOLDERS</span><h2 id="ch-title" class="d h-l">CHAMPIONS</h2></div>
        <a class="more" href="/rankings">ALL RANKINGS →</a>
      </div>
      <div class="champ-grid">{champ_cards}</div>
    </section>"""

    # ---------- results + P4P ----------
    result_rows = "".join(result_row(ctx, fight, i) for i, fight in enumerate(results)) or '<p class="empty">No results logged yet.</p>'
    eyebrow = results_title.upper() + (f" · {short_day(results_day).upper()}" if results_day else "")

    p4p_rows = ""
    for i, row in enumerate(ctx.get_division_rankings(ctx.gid, ctx.P4P_WEIGHT)[:5]):
        uid = row["discord_user_id"]
        stats = stats_for(ctx, uid, cache)
        p4p_rows += f"""
          <li class="row p4p-row {stagger(i)}">
            <span class="d p4p-rank{' gold' if i == 0 else ''}">{row['rank']}</span>
            {avatar_html(ctx, uid, fighter_name(ctx, uid), 'av av-sm')}
            {name_link(ctx, uid, fighter_name(ctx, uid), 'd p4p-name')}
            <span class="muted tab">{esc(ctx.stats_record(stats) if stats else '')}</span>
          </li>"""
    p4p_rows = p4p_rows or '<li class="empty">Pound for pound rankings coming soon.</li>'

    results_html = f"""
    <section class="block bt split" aria-label="Results and pound for pound">
      <div class="drift drift-rev" aria-hidden="true">RESULTS</div>
      <div class="split-main">
        <div class="head-t rv"><span class="eyebrow">{esc(eyebrow)}</span><h2 class="d h-m">LATEST RESULTS</h2></div>
        <div class="list">{result_rows}</div>
        <a class="more rv" href="/results">ALL RESULTS →</a>
      </div>
      <div class="split-side">
        <div class="head-t rv"><span class="eyebrow">BEST IN PFO</span><h2 class="d h-m">POUND FOR POUND</h2></div>
        <ol class="list">{p4p_rows}</ol>
      </div>
    </section>"""

    # ---------- title history ----------
    moments = title_moments(ctx, 4)
    titles_html = ""
    if moments:
        items = ""
        for i, (day, _, weight, outcome, fight) in enumerate(moments):
            tag = {"defence": "DEFENCE", "first": "FIRST CHAMPION", "new": "NEW CHAMPION"}[outcome]
            items += f"""
            <li class="tl-item {stagger(i)}">
              <span class="tl-dot" aria-hidden="true"></span>
              <span class="tl-when">{esc(short_day(day).upper())} · {esc(weight.upper())} · {tag}</span>
              {name_link(ctx, fight['winner_id'], fighter_name(ctx, fight['winner_id'], fight['winner_name']), 'd tl-name')}
              <span class="muted tl-how">{esc(moment_text(ctx, outcome, fight))}</span>
            </li>"""
        titles_html = f"""
    <section class="block bt" aria-labelledby="th-title">
      <div class="drift" aria-hidden="true">LEGACY</div>
      <div class="head rv">
        <div class="head-t"><span class="eyebrow">THE BELT CHANGES HANDS</span><h2 id="th-title" class="d h-l">TITLE HISTORY</h2></div>
        <a class="more" href="/titles">EVERY CHAMPION →</a>
      </div>
      <div class="timeline"><div class="tl-line draw" aria-hidden="true"></div><ol class="tl">{items}</ol></div>
    </section>"""

    join = ""
    if DISCORD_INVITE:
        join = f"""
<section id="join" class="join" aria-label="Join PFO">
  <div class="wrap join-in">
    <h2 class="d join-big big">JOIN <span class="gold">PFO</span></h2>
    <p class="muted join-p rv2">Sign-ups, fight cards, results and betting all happen in the Discord.</p>
    <a class="btn btn-gold btn-lg rv3" href="{esc(DISCORD_INVITE)}" target="_blank" rel="noopener">JOIN THE DISCORD</a>
  </div>
</section>"""

    body = f"""{hero}
{ticker}
<div class="wrap">
  <section id="numbers" class="nums" aria-label="PFO in numbers">{numbers_html}</section>
  {event_html}
  {champs_html}
  {results_html}
  {titles_html}
</div>
{join}"""

    return page(ctx, "Home", "home", body, solid_nav=False)


def result_row(ctx, fight, i=0, show_weight=True):
    w = fighter_name(ctx, fight["winner_id"], fight["winner_name"])
    l = fighter_name(ctx, fight["loser_id"], fight["loser_name"])
    joiner = "VS" if fight["method"] in ("DRAW", "NC") else "DEF."
    belt = '<span class="belt" title="Title fight">TITLE</span>' if fight["title_fight"] else ""
    weight = f'<span class="res-w">{esc((fight["division"] or "").upper())}</span>' if show_weight else ""
    return f"""
          <div class="row res {stagger(i)}">
            <span class="d res-who">{name_link(ctx, fight['winner_id'], w)}<span class="def">{joiner}</span>{name_link(ctx, fight['loser_id'], l, 'loser')}</span>
            {belt}
            <span class="res-m {method_class(fight['method'])}">{esc(method_short(ctx, fight))}</span>
            {weight}
          </div>"""


# ============================================================
# RANKINGS
# ============================================================

def render_rankings(ctx, selected=None):
    cache = {}
    weights = [w for w in ctx.WEIGHTS if w != ctx.P4P_WEIGHT] + [ctx.P4P_WEIGHT]
    names = {ctx.P4P_WEIGHT: "Pound for Pound"}

    # Default tab: the division asked for, else the first one with anyone ranked.
    by_weight = {w: ctx.get_division_rankings(ctx.gid, w) for w in weights}
    if selected not in by_weight:
        filled = [w for w in weights if w != ctx.P4P_WEIGHT and by_weight[w]]
        selected = max(filled, key=lambda w: len(by_weight[w])) if filled else weights[0]

    settings = ctx.update_settings(ctx.gid)
    snapshot = ctx.saved_rankings(ctx.gid) if row_get(settings, "snapshot_at") else {}
    since = row_get(settings, "snapshot_at")

    tabs = ""
    panels = ""

    for weight in weights:
        rows = by_weight[weight]
        is_sel = weight == selected
        slug = quote(weight)
        label = "P4P" if weight == ctx.P4P_WEIGHT else weight
        tabs += (f'<a role="tab" class="tab" href="?d={slug}" data-tab="{esc(weight)}" '
                 f'aria-selected="{"true" if is_sel else "false"}">{esc(label.upper())}</a>')

        before = snapshot.get(weight, {}) if snapshot else None

        def move(uid, rank, row):
            if before is not None and since:
                old = before.get(uid)
                if old is None:
                    return '<span class="mv mv-new">NEW</span>'
                if old == 0 and rank != 0:
                    return f'<span class="mv mv-down">▼ {rank}</span>'
                diff = (old or 0) - rank
                if diff > 0:
                    return f'<span class="mv mv-up" aria-label="up {diff}">▲ {diff}</span>'
                if diff < 0:
                    return f'<span class="mv mv-down" aria-label="down {-diff}">▼ {-diff}</span>'
                return '<span class="mv mv-none" aria-label="no change">—</span>'
            m = row_get(row, "movement", 0)
            if m > 0:
                return '<span class="mv mv-up" aria-label="moved up">▲</span>'
            if m < 0:
                return '<span class="mv mv-down" aria-label="moved down">▼</span>'
            return '<span class="mv mv-none" aria-label="no change">—</span>'

        champ = next((r for r in rows if r["rank"] == 0), None)
        champ_html = ""

        if weight != ctx.P4P_WEIGHT:
            if champ:
                uid = champ["discord_user_id"]
                name = fighter_name(ctx, uid)
                first, last = split_name(name)
                stats = stats_for(ctx, uid, cache)
                reigns = ctx.title_lineage(ctx.gid, weight)[0]
                reign = reigns[-1] if reigns and reigns[-1]["user_id"] == uid else None
                days = ctx.reign_days(reign) if reign else None
                champ_html = f"""
      <a href="{fighter_url(uid)}" class="feature rv">
        <span class="eyebrow">{esc(weight.upper())} CHAMPION</span>
        {avatar_html(ctx, uid, name, 'av av-xl')}
        <span class="d feature-name">{esc(first)}<br>{esc(last)}</span>
        <span class="feature-stats">
          <span><strong class="d">{esc(ctx.stats_record(stats) if stats else '–')}</strong>RECORD</span>
          <span><strong class="d">{reign['defences'] if reign else '–'}</strong>DEFENCES</span>
          {f'<span><strong class="d">{days}</strong>DAYS</span>' if days is not None else ''}
        </span>
      </a>"""
            else:
                champ_html = f"""
      <div class="feature feature-vacant rv">
        <span class="eyebrow">{esc(weight.upper())} CHAMPION</span>
        <span class="av av-xl" aria-hidden="true">?</span>
        <span class="d feature-name">VACANT</span>
        <span class="muted">The belt is up for grabs.</span>
      </div>"""

        list_rows = ""
        for i, row in enumerate(r for r in rows if r["rank"] >= 1):
            uid = row["discord_user_id"]
            name = fighter_name(ctx, uid)
            stats = stats_for(ctx, uid, cache)
            list_rows += f"""
          <li class="row rank-row {stagger(i, 3)}">
            <span class="d rank-n{' gold' if row['rank'] == 1 else ''}">{row['rank']}</span>
            {avatar_html(ctx, uid, name, 'av av-sm')}
            {name_link(ctx, uid, name, 'd rank-name')}
            <span class="muted tab">{esc(ctx.stats_record(stats) if stats else '')}</span>
            {move(uid, row['rank'], row)}
          </li>"""

        if not list_rows:
            list_rows = '<li class="empty">Nobody ranked here yet.</li>'

        note = (f"Movement since the last rankings update on {uk_time(since).day} {uk_time(since):%B}."
                if since else "Arrows show the latest move.")

        panels += f"""
    <div class="rank-panel{' p4p' if weight == ctx.P4P_WEIGHT else ''}" role="tabpanel" data-panel="{esc(weight)}" aria-label="{esc(names.get(weight, weight))}"{'' if is_sel else ' hidden'}>
      {champ_html}
      <section class="rank-list" aria-label="{esc(names.get(weight, weight))} contenders">
        <div class="list-head rv"><span>{'RANKED' if weight == ctx.P4P_WEIGHT else 'CONTENDER'}</span><span>RECORD · MOVE</span></div>
        <ol class="list">{list_rows}</ol>
        <p class="note rv">{esc(note)}</p>
      </section>
    </div>"""

    extra = f'<div class="tabs l-up" role="tablist" aria-label="Weight classes">{tabs}</div>'
    body = page_hero("UPDATED LIVE FROM THE DISCORD", "RANKINGS", "#1", extra) + f'<div class="wrap rank-wrap">{panels}</div>'

    return page(ctx, "Rankings", "rankings", body, "The official PFO rankings for every division and pound for pound.")


# ============================================================
# FIGHTERS
# ============================================================

def render_fighters(ctx):
    cache = {}
    current, former = [], []

    for uid in all_fighter_ids(ctx):
        member = ctx.guild.get_member(uid)
        if member and getattr(member, "bot", False):
            continue
        stats = stats_for(ctx, uid, cache)
        if not stats:
            continue
        entry = {
            "id": uid,
            "name": fighter_name(ctx, uid),
            "stats": stats,
            "division": main_division(ctx, uid),
            "ranks": {r["weight"]: r["rank"] for r in ctx.get_user_rankings(ctx.gid, uid)},
        }
        (current if member else former).append(entry)

    def order(e):
        champ = any(rank == 0 for w, rank in e["ranks"].items() if w != ctx.P4P_WEIGHT)
        return (not champ, -e["stats"]["wins"], e["stats"]["losses"], e["name"].casefold())

    current.sort(key=order)
    former.sort(key=lambda e: e["name"].casefold())

    def card(e, i):
        division = e["division"]
        rank = e["ranks"].get(division) if division else None
        badge = ""
        if rank == 0:
            badge = '<span class="badge badge-gold">CHAMPION</span>'
        elif rank:
            badge = f'<span class="badge">#{rank}</span>'
        streak = e["stats"]["streak"] or ""
        hot = ' <span class="hot">🔥</span>' if streak.startswith("W") and int(streak[1:] or 0) >= 3 else ""
        return f"""
        <a href="{fighter_url(e['id'])}" class="fcard {stagger(i)}" data-name="{esc(e['name'].casefold())}" data-div="{esc(division or '')}">
          {avatar_html(ctx, e['id'], e['name'], 'av av-md')}
          <span class="fcard-t">
            <span class="d fcard-name">{esc(e['name'])}</span>
            <span class="fcard-sub">{esc((division or 'No division').upper())}</span>
          </span>
          <span class="fcard-r"><span class="d fcard-rec">{esc(ctx.stats_record(e['stats']))}</span>{badge}{f'<span class="fcard-streak">{esc(streak)}{hot}</span>' if streak and not badge else ''}</span>
        </a>"""

    chips = '<button type="button" class="fchip" data-filter="" aria-pressed="true">ALL</button>' + "".join(
        f'<button type="button" class="fchip" data-filter="{esc(w)}" aria-pressed="false">{esc(w.upper())}</button>'
        for w in ctx.DIVISION_NAMES if any(e["division"] == w for e in current)
    )

    grid = "".join(card(e, i) for i, e in enumerate(current)) or '<p class="empty">No fighters yet.</p>'
    former_html = ""
    if former:
        former_html = f"""
  <section class="block bt" aria-labelledby="former">
    <div class="head-t rv"><span class="eyebrow">NO LONGER IN THE SERVER</span><h2 id="former" class="d h-m">FORMER FIGHTERS</h2></div>
    <div class="fgrid fgrid-former">{''.join(card(e, i) for i, e in enumerate(former))}</div>
  </section>"""

    extra = f"""
    <div class="filters l-up">
      <label class="search"><span class="vh">Search fighters</span>
        <svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
        <input type="search" id="fsearch" placeholder="Search {len(current)} fighters" autocomplete="off">
      </label>
      <div class="fchips" role="group" aria-label="Filter by division">{chips}</div>
    </div>"""

    body = page_hero(f"{len(current)} ACTIVE FIGHTERS", "FIGHTERS", "PFO", extra) + f"""
<div class="wrap">
  <section class="block fsec" aria-label="Fighters">
    <div class="fgrid" id="fgrid">{grid}</div>
    <p class="empty" id="fnone" hidden>No fighters match that search.</p>
  </section>
  {former_html}
</div>"""

    return page(ctx, "Fighters", "fighters", body, "Every fighter in the Prestige Fighting Organisation.")


# ============================================================
# FIGHTER PROFILE
# ============================================================

def render_fighter(ctx, user_id):
    stats = stats_for(ctx, user_id)

    if not stats or (not stats["fights"] and not ctx.get_starting_record(ctx.gid, user_id)
                     and not ctx.get_user_rankings(ctx.gid, user_id)):
        return None

    name = fighter_name(ctx, user_id)
    first, last = split_name(name)
    member = ctx.guild.get_member(user_id)
    ranks = {r["weight"]: r["rank"] for r in ctx.get_user_rankings(ctx.gid, user_id)}
    division = main_division(ctx, user_id)

    # Badge line
    badge_bits = []
    for weight in ctx.DIVISION_NAMES:
        if ranks.get(weight) == 0:
            badge_bits.append(f"{weight.upper()} CHAMPION")
    if not badge_bits and division and ranks.get(division):
        badge_bits.append(f"{division.upper()} #{ranks[division]}")
    elif not badge_bits and division:
        badge_bits.append(division.upper())
    if ranks.get(ctx.P4P_WEIGHT):
        badge_bits.append(f"P4P #{ranks[ctx.P4P_WEIGHT]}")
    if not member:
        badge_bits.append("FORMER FIGHTER")
    badge = " · ".join(badge_bits) or "PFO FIGHTER"

    fights = stats["fights"]
    debut = fights[0] if fights else None
    latest = fights[-1] if fights else None

    def event_of(fight):
        return fight["event"] or nice_day(fight_day(fight))

    meta = []
    if debut:
        meta.append(f"Debut: {event_of(debut)}")
    if latest and latest is not debut:
        meta.append(f"Last fight: {event_of(latest)}")
    if stats.get("starting_record"):
        meta.append(f"Includes earlier record {stats['starting_record']}")

    # Form (last 5, oldest → newest)
    form = ""
    for fight in fights[-5:]:
        if fight["method"] == "NC":
            r, cls = "NC", "pill-nc"
        elif fight["method"] == "DRAW":
            r, cls = "D", "pill-d"
        elif ctx.fight_is_win_for(fight, user_id):
            r, cls = "W", "pill-w"
        else:
            r, cls = "L", "pill-l"
        form += f'<span class="pill {cls}" title="{esc(event_of(fight))}">{r}</span>'

    # Their Discord username, e.g. @razorreyes
    username = getattr(member, "name", None) if member else None
    handle_html = (f'<span class="handle l-up"><span class="handle-l">DISCORD</span>@{esc(username)}</span>'
                   if username else "")

    hero = f"""
<section class="fhero" aria-label="{esc(name)}">
  <div class="fhero-mark s-mark" aria-hidden="true">{esc(last or first)}</div>
  <div class="wrap fhero-in s-fhero">
    <div class="fhero-main">
      <div class="fhero-top l-badge">{avatar_html(ctx, user_id, name, 'av av-hero')}<span class="fbadge">{esc(badge)}</span></div>
      <h1 class="d fhero-name l-title">{esc(first)}{'<br>' + esc(last) if last else ''}</h1>
      {handle_html}
      <p class="muted l-up">{esc(' · '.join(meta))}</p>
    </div>
    <div class="fhero-rec l-up2">
      <span class="eyebrow-grey">PFO RECORD</span>
      <span class="d fhero-record">{esc(ctx.stats_record(stats))}</span>
      {f'<span class="form" aria-label="Last {len(fights[-5:])} fights">{form}</span>' if form else ''}
    </div>
  </div>
</section>"""

    wins_by = stats["wins_by"]
    streak = stats["streak"] or "–"
    tiles = [
        ("KO / TKO", wins_by.get("KO/TKO", 0)),
        ("SUBMISSIONS", wins_by.get("SUB", 0)),
        ("DECISIONS", wins_by.get("DEC", 0)),
        ("FINISH RATE", f"{stats['finish_rate']}%" if stats["finish_rate"] is not None else "–"),
        ("TITLE WINS", stats["title_wins"]),
        ("STREAK", streak),
    ]
    tiles_html = ""
    for i, (label, v) in enumerate(tiles):
        gold = " gold" if label == "FINISH RATE" else ""
        count = f' data-count="{v}"' if isinstance(v, int) else ""
        tiles_html += (f'<div class="ftile {stagger(i, 3)}"><span class="d ftile-v{gold}"{count}>{esc(v)}</span>'
                       f'<span class="num-l">{label}</span></div>')

    # How they win
    logged_wins = sum(wins_by.values())
    bars = ""
    if logged_wins:
        for i, (key, label, cls) in enumerate([("KO/TKO", "KO / TKO", "bar-gold"), ("SUB", "SUBMISSION", "bar-silver"),
                                               ("DEC", "DECISION", "bar-dim"), ("DQ", "DQ", "bar-dim")]):
            count = wins_by.get(key, 0)
            if key == "DQ" and not count:
                continue
            pct = round(100 * count / logged_wins, 1)
            bars += f"""
        <div class="mbar {stagger(i, 3)}">
          <span class="mbar-l"><span>{label}</span><span>{count}</span></span>
          <span class="mbar-track"><span class="bar {cls}" style="width:{pct}%"></span></span>
        </div>"""
    how = ""
    if bars:
        how = f"""
  <section class="block bt" aria-labelledby="how">
    <div class="drift" aria-hidden="true">{esc((last or first))}</div>
    <h2 id="how" class="d h-m rv">HOW THEY WIN</h2>
    <div class="mbars">{bars}</div>
  </section>"""

    # Fight stats from /addstrikes
    numbers = ""
    total = None
    try:
        total = ctx.fight_stat_totals(ctx.gid).get(user_id)
    except Exception:
        total = None
    if total:
        def acc(a, b):
            return f"{round(100 * a / b)}%" if b else "–"
        mins, secs = divmod(total["control"] or 0, 60)
        stat_items = [
            ("STRIKES LANDED", f"{total['total_landed']:,}", f"of {total['total_thrown']:,} thrown"),
            ("SIG. STRIKES", f"{total['sig_landed']:,}", f"{acc(total['sig_landed'], total['sig_thrown'])} accuracy"),
            ("TAKEDOWNS", f"{total['td_landed']}", f"of {total['td_attempted']} attempts"),
            ("CONTROL TIME", f"{mins}:{secs:02d}", "minutes"),
            ("KNOCKDOWNS", f"{total['knockdowns']}", "landed"),
        ]
        numbers = f"""
  <section class="block bt" aria-labelledby="fstats">
    <div class="head-t rv"><span class="eyebrow">FROM {total['fights']} FIGHT{'S' if total['fights'] != 1 else ''} WITH STATS</span><h2 id="fstats" class="d h-m">FIGHT STATS</h2></div>
    <div class="sgrid">{''.join(f'<div class="stile {stagger(i)}"><span class="num-l">{l}</span><span class="d stile-v">{esc(v)}</span><span class="muted">{esc(s)}</span></div>' for i, (l, v, s) in enumerate(stat_items))}</div>
  </section>"""

    # Fight history
    rows = ""
    for i, fight in enumerate(reversed(fights)):
        if fight["method"] == "NC":
            r, cls = "NC", "pill-nc"
        elif fight["method"] == "DRAW":
            r, cls = "D", "pill-d"
        elif ctx.fight_is_win_for(fight, user_id):
            r, cls = "W", "pill-w"
        else:
            r, cls = "L", "pill-l"
        if fight["winner_id"] == user_id:
            opp_id, opp_saved = fight["loser_id"], fight["loser_name"]
        else:
            opp_id, opp_saved = fight["winner_id"], fight["winner_name"]
        method = method_short(ctx, fight) + (" · TITLE" if fight["title_fight"] else "")
        rows += f"""
          <tr class="row {stagger(i, 3)}">
            <td><span class="pill {cls}">{r}</span></td>
            <td class="d td-opp">{name_link(ctx, opp_id, fighter_name(ctx, opp_id, opp_saved))}</td>
            <td class="td-m">{esc(method)}</td>
            <td class="td-e">{esc(fight['event'] or '')}<span>{esc(nice_day(fight_day(fight)))}</span></td>
          </tr>"""

    history = f"""
    <section class="hist" aria-labelledby="history">
      <h2 id="history" class="d h-m rv">FIGHT HISTORY</h2>
      {f'''<div class="tscroll"><table class="ftable">
        <thead><tr><th scope="col">RESULT</th><th scope="col">OPPONENT</th><th scope="col">METHOD</th><th scope="col" class="r">EVENT</th></tr></thead>
        <tbody>{rows}</tbody></table></div>''' if rows else '<p class="empty">No fights logged yet.</p>'}
    </section>"""

    # Title reigns
    reign_tiles = ""
    for weight in ctx.DIVISION_NAMES:
        for reign in reversed(ctx.title_lineage(ctx.gid, weight)[0]):
            if reign["user_id"] != user_id:
                continue
            won = reign["won_fight"]
            opp = fighter_name(ctx, won["loser_id"], won["loser_name"])
            days = ctx.reign_days(reign)
            current = reign["end"] is None
            d = reign["defences"]
            reign_tiles += f"""
        <div class="reign{' reign-now' if current else ''} rv2">
          <span class="reign-tag">{esc(weight.upper())} · {'CURRENT' if current else 'FORMER'}</span>
          <span class="d reign-t">Won vs {esc(opp)}</span>
          <span class="muted">{esc(short_day(reign['start']))} · {d} defence{'s' if d != 1 else ''}{f' · {days} days' if days is not None else ''}</span>
        </div>"""

    aside = ""
    if reign_tiles:
        aside = f"""
    <aside class="reigns" aria-labelledby="titles">
      <h2 id="titles" class="d h-m rv">TITLE REIGNS</h2>
      {reign_tiles}
    </aside>"""

    body = hero + f"""
<div class="wrap">
  <section class="tiles" aria-label="Career numbers">{tiles_html}</section>
  {how}
  {numbers}
  <div class="block bt hist-wrap">{history}{aside}</div>
</div>"""

    description = f"{name}: {ctx.stats_record(stats)} in PFO." + (f" {badge.title()}." if badge_bits else "")
    return page(ctx, name, "fighters", body, description)


# ============================================================
# RESULTS
# ============================================================

def render_results(ctx, page_no=1):
    events = ctx.recent_events(ctx.gid)
    pages = max(1, -(-len(events) // RESULTS_PER_PAGE))
    page_no = max(1, min(page_no, pages))
    shown = events[(page_no - 1) * RESULTS_PER_PAGE: page_no * RESULTS_PER_PAGE]

    blocks = ""
    for event in shown:
        fights = ctx.event_fights(ctx.gid, event)
        if not fights:
            continue
        layout, title, notes = ctx.load_recap_layout(ctx.gid, event, fights)
        grouped = ctx.ordered_sections(layout)
        by_id = {f["id"]: f for f in fights}
        outcomes = ctx.all_title_outcomes(ctx.gid)

        sections = ""
        count = 0
        finishes = 0
        for section in ctx.RECAP_SECTIONS:
            ids = grouped.get(section, [])
            if not ids:
                continue
            rows = ""
            for i, fid in enumerate(ids):
                fight = by_id[fid]
                count += 1
                finishes += fight["method"] in ("KO/TKO", "SUB")
                rows += result_row(ctx, fight, i)
                outcome = outcomes.get(fid)
                if outcome in ("new", "first"):
                    rows += f'<div class="res-note gold">NEW {esc((fight["division"] or "").upper())} CHAMPION</div>'
                elif outcome == "defence":
                    rows += f'<div class="res-note">Retains the {esc(fight["division"] or "")} title</div>'
            sections += f'<h3 class="sec-h rv">{esc(section.upper())}</h3><div class="list">{rows}</div>'

        day = fight_day(fights[-1])
        blocks += f"""
  <section class="block bt event" aria-label="{esc(title or event)}">
    <div class="drift" aria-hidden="true">{esc((title or event).upper())}</div>
    <div class="head rv">
      <div class="head-t"><span class="eyebrow">{esc(nice_day(day).upper())}</span><h2 class="d h-l">{esc((title or event).upper())}</h2></div>
      <span class="muted ev-sum">{count} fight{'s' if count != 1 else ''} · {finishes} finish{'es' if finishes != 1 else ''}</span>
    </div>
    {f'<p class="ev-notes rv2">{esc(notes)}</p>' if notes else ''}
    {sections}
  </section>"""

    if not blocks:
        fights = ctx.q("SELECT * FROM fights WHERE guild_id = ? ORDER BY COALESCE(fight_date, substr(created_at,1,10)) DESC, id DESC LIMIT 30",
                       (ctx.gid,))
        rows = "".join(result_row(ctx, f, i) for i, f in enumerate(fights))
        blocks = f'<section class="block"><div class="list">{rows or "<p class=empty>No results logged yet.</p>"}</div></section>'

    pager = ""
    if pages > 1:
        prev_link = f'<a class="btn btn-ghost" href="?{urlencode({"page": page_no - 1})}">← NEWER</a>' if page_no > 1 else "<span></span>"
        next_link = f'<a class="btn btn-ghost" href="?{urlencode({"page": page_no + 1})}">OLDER →</a>' if page_no < pages else "<span></span>"
        pager = f'<nav class="pager" aria-label="Pages">{prev_link}<span class="muted">Page {page_no} of {pages}</span>{next_link}</nav>'

    body = page_hero("EVERY RESULT, STRAIGHT FROM THE BOT", "RESULTS", "DEF.") + f'<div class="wrap">{blocks}{pager}</div>'
    return page(ctx, "Results", "results", body, "Every PFO result, event by event.")


# ============================================================
# TITLES
# ============================================================

def render_titles(ctx):
    blocks = ""
    for d_i, weight in enumerate(ctx.DIVISION_NAMES):
        reigns, _ = ctx.title_lineage(ctx.gid, weight)
        champ_row = next((r for r in ctx.get_division_rankings(ctx.gid, weight) if r["rank"] == 0), None)

        if not reigns and not champ_row:
            continue

        items = ""
        for i, reign in enumerate(reversed(reigns)):
            number = len(reigns) - i
            won = reign["won_fight"]
            name = fighter_name(ctx, reign["user_id"], reign["name"])
            opp = fighter_name(ctx, won["loser_id"], won["loser_name"])
            days = ctx.reign_days(reign)
            current = reign["end"] is None
            d = reign["defences"]
            items += f"""
          <li class="lin{' lin-now' if current else ''} {stagger(i, 3)}">
            <span class="lin-n d">{number}</span>
            {avatar_html(ctx, reign['user_id'], name, 'av av-sm' + (' av-gold' if current else ''))}
            <span class="lin-t">
              {name_link(ctx, reign['user_id'], name, 'd lin-name')}
              <span class="muted">Won {esc(short_day(reign['start']))} vs {esc(opp)} · {esc(ctx.fight_method_text(won))}</span>
            </span>
            <span class="lin-r">{'<span class="badge badge-gold">CURRENT</span>' if current else ''}<span class="muted">{d} def. · {days if days is not None else '–'} days</span></span>
          </li>"""

        if not items and champ_row:
            uid = champ_row["discord_user_id"]
            items = f"""
          <li class="lin lin-now rv">
            <span class="lin-n d">1</span>
            {avatar_html(ctx, uid, fighter_name(ctx, uid), 'av av-sm av-gold')}
            <span class="lin-t">{name_link(ctx, uid, fighter_name(ctx, uid), 'd lin-name')}<span class="muted">Title fights not logged yet</span></span>
            <span class="lin-r"><span class="badge badge-gold">CURRENT</span></span>
          </li>"""

        blocks += f"""
  <section class="block bt" aria-labelledby="t-{d_i}">
    <div class="drift{' drift-rev' if d_i % 2 else ''}" aria-hidden="true">{esc(weight.upper())}</div>
    <div class="head rv">
      <div class="head-t"><span class="eyebrow">{len(reigns)} CHAMPION{'S' if len(reigns) != 1 else ''}</span><h2 id="t-{d_i}" class="d h-l">{esc(weight.upper())}</h2></div>
      <a class="more" href="/rankings?d={quote(weight)}">RANKINGS →</a>
    </div>
    <ol class="list">{items}</ol>
  </section>"""

    if not blocks:
        blocks = '<section class="block"><p class="empty">No title fights logged yet.</p></section>'

    body = page_hero("A DRAW OR NO CONTEST KEEPS THE CHAMPION", "TITLES", "GOLD") + f'<div class="wrap">{blocks}</div>'
    return page(ctx, "Title history", "titles", body, "Every PFO champion in every division.")


def render_404(ctx):
    body = page_hero("ERROR 404", "NOT FOUND", "404", '<p class="muted l-up">That page doesn\'t exist. <a href="/">Back to the home page</a>.</p>')
    return page(ctx, "Not found", "", body)


def render_starting():
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            "<meta http-equiv=refresh content=5><title>PFO</title>"
            "<body style='background:#08080A;color:#F4F4F5;font:16px system-ui;display:grid;place-items:center;height:100vh;margin:0'>"
            "<p>PFO is starting up, one moment…</p>")


# ============================================================
# Styles and scripts
# ============================================================

SITE_CSS = r"""
:root{--bg:#08080A;--panel:#0C0C0F;--panel2:#111115;--line:#1C1C21;--line2:#24242A;--text:#F4F4F5;--muted:#A1A1AA;--dim:#868690;
--gold:#C9A24A;--gold2:#E2C27A;--green:#3DD68C;--red:#F07A7E;--blue:#7FB0FF;--redc:#E5484D;--bluec:#3B82F6;
--d:'Barlow Condensed',Impact,sans-serif;--ease:cubic-bezier(.16,1,.3,1)}
*{box-sizing:border-box}
html{scroll-behavior:smooth;-webkit-text-size-adjust:100%;overflow-x:clip}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 Inter,system-ui,sans-serif;overflow-x:clip}
a{color:var(--gold2);text-decoration:none}a:hover{color:#F3DDA6}
::selection{background:var(--gold);color:var(--bg)}
img{max-width:100%}
.d{font-family:var(--d);font-weight:800;letter-spacing:.01em}
.wrap{max-width:1240px;margin:0 auto;padding:0 28px}
.gold{color:var(--gold)!important}
.muted{color:var(--muted)}
.tab{font-variant-numeric:tabular-nums}
.vh{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
.skip{position:absolute;left:-999px;top:8px;z-index:99;background:var(--gold);color:var(--bg);padding:10px 16px;border-radius:8px;font-weight:700}
.skip:focus{left:8px}
:focus-visible{outline:2px solid var(--gold);outline-offset:3px;border-radius:4px}
.empty{color:var(--dim);padding:24px 6px;margin:0;list-style:none}
.note{margin:22px 0 0;font-size:13px;color:var(--dim)}

/* nav */
.nav{position:sticky;top:0;z-index:30;border-bottom:1px solid var(--line2);background:rgba(8,8,10,.92);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);transition:background .3s,border-color .3s}
.over-hero .nav{background:rgba(8,8,10,0);border-color:rgba(36,36,42,0)}
.over-hero .nav.solid{background:rgba(8,8,10,.92);border-color:var(--line2)}
.nav-in{display:flex;align-items:center;gap:10px 36px;padding-top:14px;padding-bottom:14px}
.brand{display:flex;align-items:center;gap:12px;color:var(--text)}
.brand span{font-family:var(--d);font-weight:700;font-size:22px;letter-spacing:.14em}
.logo-sm{width:38px;height:38px;border-radius:50%;display:grid;place-items:center}
.logo-xs{width:28px;height:28px;border-radius:50%;display:inline-grid;place-items:center}
.logo-text{background:#16161A;border:1px solid var(--gold);color:var(--gold);font:700 11px var(--d);letter-spacing:.05em}
.nav-links{display:flex;gap:2px 28px;flex:1 1 auto;font-size:13px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;overflow-x:auto;scrollbar-width:none}
.nav-links::-webkit-scrollbar{display:none}
.nav-links a{color:var(--muted);padding:12px 0;white-space:nowrap;transition:color .2s}
.nav-links a:hover{color:var(--text)}
.nav-links a[aria-current]{color:var(--text);box-shadow:inset 0 -2px 0 var(--gold)}

/* buttons */
.btn{display:inline-flex;align-items:center;gap:10px;font-size:13px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;padding:13px 22px;border-radius:999px;min-height:44px;transition:background .25s,color .25s,border-color .25s,transform .25s var(--ease);white-space:nowrap}
.btn:hover{transform:translateY(-1px)}
.btn-ghost{border:1px solid #3A3A42;color:#D7D7DC}
.btn-ghost:hover{border-color:var(--gold);color:var(--text)}
.btn-gold,.btn-gold-solid{background:var(--gold);color:var(--bg);border:1px solid var(--gold)}
.btn-gold:hover,.btn-gold-solid:hover{background:var(--gold2);color:var(--bg)}
.btn-lg{font-size:14px;padding:17px 34px}
.btns{display:flex;flex-wrap:wrap;gap:10px}
.more{font-size:13px;font-weight:700;letter-spacing:.14em;padding:12px 0;display:inline-block}

/* text */
.eyebrow{display:flex;align-items:center;gap:10px;font-size:12px;font-weight:700;letter-spacing:.26em;color:var(--gold);margin:0}
.eyebrow-grey{font-size:12px;font-weight:700;letter-spacing:.26em;color:var(--dim)}
.h-xl{margin:0;font-size:clamp(52px,8vw,104px);line-height:.86}
.h-l{margin:0;font-size:clamp(44px,6vw,80px);line-height:.88}
.h-m{margin:0;font-size:clamp(40px,5vw,60px);line-height:.9}
.head{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:flex-end;gap:20px 28px}
.head-t{display:flex;flex-direction:column;gap:12px}
.block{padding:100px 0}
.bt{border-top:1px solid var(--line)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--green);flex:none;animation:blink 1.6s ease-in-out infinite}
.dot-red{background:var(--redc);width:8px;height:8px}
.live{display:flex;align-items:center;gap:10px}

/* lists */
.list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;border-top:1px solid var(--line)}
.row{border-bottom:1px solid var(--line);transition:background .25s,padding-left .35s var(--ease)}
.row:hover{background:var(--panel2)}
.list-head{display:flex;justify-content:space-between;font-size:11px;font-weight:700;letter-spacing:.22em;color:var(--dim);padding:0 6px 14px}

/* avatars */
.av{flex:none;width:44px;height:44px;border-radius:50%;overflow:hidden;display:grid;place-items:center;background:#16161A;border:1px solid var(--line2);font:700 16px var(--d);color:var(--gold2);letter-spacing:.04em}
.av img{width:100%;height:100%;object-fit:cover;display:block}
.av-sm{width:36px;height:36px;font-size:14px}
.av-md{width:52px;height:52px;font-size:19px}
.av-gold{border-color:rgba(201,162,74,.6);box-shadow:0 0 0 4px rgba(201,162,74,.06)}
.av-xl{width:96px;height:96px;font-size:34px;border-color:rgba(201,162,74,.6);box-shadow:0 0 0 8px rgba(201,162,74,.05)}
.av-hero{width:76px;height:76px;font-size:28px;border-color:rgba(201,162,74,.6)}

/* hero (home) */
.over-hero main{margin-top:-68px}
.hero{position:relative;min-height:max(720px,100svh);display:flex;align-items:center;justify-content:center;overflow:hidden}
.hero-grid{position:absolute;inset:0;background-image:repeating-linear-gradient(45deg,rgba(201,162,74,.05) 0 1px,transparent 1px 34px),repeating-linear-gradient(-45deg,rgba(201,162,74,.05) 0 1px,transparent 1px 34px);-webkit-mask:radial-gradient(60% 55% at 50% 48%,#000 0%,transparent 100%);mask:radial-gradient(60% 55% at 50% 48%,#000 0%,transparent 100%)}
.hero-glow{position:absolute;width:900px;height:900px;left:50%;top:50%;margin:-470px 0 0 -450px;border-radius:50%;background:radial-gradient(circle,rgba(201,162,74,.2) 0%,rgba(201,162,74,.06) 35%,rgba(201,162,74,0) 65%);animation:breathe 7s ease-in-out infinite}
.hero-mark{position:absolute;left:50%;top:4%;transform:translateX(-50%);font:800 clamp(240px,42vw,560px)/.8 var(--d);letter-spacing:-.02em;color:transparent;-webkit-text-stroke:1px rgba(201,162,74,.1);white-space:nowrap;pointer-events:none}
.hero-in{position:relative;display:flex;flex-direction:column;align-items:center;text-align:center;padding:130px 20px 70px}
.hero-logo{width:clamp(150px,22vw,210px);aspect-ratio:1;padding:14px;border-radius:50%;border:1px solid rgba(201,162,74,.45);box-shadow:0 0 0 10px rgba(201,162,74,.05),0 40px 90px rgba(0,0,0,.65)}
.logo-hero{display:grid;place-items:center;width:100%;height:100%;border-radius:50%;font-size:48px}
.hero-name{margin:44px 0 0;display:flex;flex-direction:column;align-items:center;gap:14px;font-weight:400}
.hero-big{font-size:clamp(60px,11vw,148px);line-height:.82;letter-spacing:.04em;background:linear-gradient(180deg,#FFF 0%,#E9E6DE 45%,#B9B3A4 100%);-webkit-background-clip:text;background-clip:text;color:transparent}
.hero-sub{font:600 clamp(13px,2vw,22px) var(--d);letter-spacing:.62em;margin-right:-.62em;color:var(--gold)}
.hero-rule{width:min(420px,70vw);height:1px;margin-top:34px;background:linear-gradient(90deg,rgba(201,162,74,0),var(--gold),rgba(201,162,74,0))}
.chip{margin-top:30px;border:1px solid #2E2E35;background:rgba(17,17,20,.7);color:#D7D7DC;letter-spacing:.14em;white-space:normal;text-align:left}
.chip:hover{color:var(--text);border-color:var(--gold)}
.cue{margin-top:64px;display:flex;flex-direction:column;align-items:center;gap:12px;color:var(--dim);font-size:11px;font-weight:600;letter-spacing:.3em;padding:6px}
.cue-line{position:relative;width:1px;height:54px;background:var(--line2);overflow:hidden}
.cue-bar{position:absolute;left:0;top:0;width:1px;height:45%;background:var(--gold);animation:cue 2.2s cubic-bezier(.65,0,.35,1) infinite}

/* ticker */
.ticker{border-top:1px solid var(--line);border-bottom:1px solid var(--line);background:var(--panel);overflow:hidden}
.track{display:flex;width:max-content;animation:ticker 48s linear infinite}
.track:hover{animation-play-state:paused}
.track-copy{display:flex}
.tick{display:flex;align-items:center;gap:12px;padding:14px 26px;white-space:nowrap;font-size:13px;color:#BFC3CA}
.tick-tag{font:700 13px var(--d);letter-spacing:.18em;color:var(--gold)}
.tick-sep{width:4px;height:4px;border-radius:50%;background:#3A3A42;margin-left:14px}

/* numbers */
.nums{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));border-bottom:1px solid var(--line)}
.num{padding:56px 8px 52px;display:flex;flex-direction:column;gap:8px}
.num-v{font-size:72px;line-height:.9;font-variant-numeric:tabular-nums}
.num-l{font-size:12px;font-weight:600;letter-spacing:.2em;color:var(--muted)}

/* next event */
#event{display:flex;flex-direction:column;gap:52px;padding-top:110px}
.countdown{display:flex;gap:6px}
.countdown>div{min-width:76px;padding:12px 10px;border:1px solid var(--line2);border-radius:10px;background:#0E0E11;text-align:center}
.countdown b{display:block;font-size:38px;line-height:1;font-variant-numeric:tabular-nums}
.countdown span{display:block;font-size:10px;font-weight:600;letter-spacing:.22em;color:var(--dim);margin-top:6px}
.versus{position:relative;display:flex;flex-wrap:wrap;border:1px solid #1F1F25;border-radius:18px;overflow:hidden;background:linear-gradient(90deg,rgba(229,72,77,.08) 0%,rgba(8,8,10,0) 38%,rgba(8,8,10,0) 62%,rgba(59,130,246,.08) 100%),var(--panel)}
.corner{flex:1 1 300px;display:flex;flex-direction:column;gap:18px;padding:48px 44px;color:var(--text);transition:background .35s}
.corner:hover{color:var(--text);background:rgba(255,255,255,.02)}
.corner-a{border-left:3px solid var(--redc)}
.corner-b{border-right:3px solid var(--bluec);align-items:flex-end;text-align:right}
.corner-tag{font-size:12px;font-weight:700;letter-spacing:.24em}
.corner-a .corner-tag{color:#FF8589}.corner-b .corner-tag{color:#8BB8FF}
.corner-name{font-size:clamp(42px,5.4vw,72px);line-height:.85}
.corner-stats{display:flex;gap:28px;font-size:12px;color:var(--muted);letter-spacing:.08em}
.corner-stats strong{display:block;font-size:28px;color:var(--text);letter-spacing:0}
.vs{flex:0 0 110px;display:grid;place-items:center;font-size:54px;color:var(--gold);letter-spacing:.04em}
.ev-foot{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:18px}
.ev-foot p{margin:0}
.card-list{display:flex;flex-direction:column;gap:18px}
.card-row{display:grid;grid-template-columns:120px 1fr auto;align-items:center;gap:8px 20px;padding:18px 6px}
.card-slot{font-size:11px;font-weight:700;letter-spacing:.2em;color:var(--dim)}
.card-row:first-child .card-slot{color:var(--gold)}
.card-fight{font-size:24px;font-weight:700;display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 10px}
.card-fight a{color:var(--text)}.card-fight a:hover{color:var(--gold2)}
.card-fight small{font:600 12px Inter,sans-serif;color:var(--gold);letter-spacing:.06em}
.vs-s{font:600 12px Inter,sans-serif;color:var(--dim);letter-spacing:.14em}
.card-weight{font-size:12px;letter-spacing:.12em;color:var(--dim);text-align:right}
.so-row{display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px 20px;padding:22px 6px}
.so-name{font-size:30px}

/* champions */
.champ-grid{margin-top:44px;display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:12px}
.champ{display:flex;flex-direction:column;gap:22px;padding:28px 26px;border:1px solid var(--line);border-radius:16px;background:radial-gradient(120% 90% at 0% 0%,rgba(201,162,74,.08),rgba(8,8,10,0) 55%),var(--panel);color:var(--text);transition:background .35s,border-color .35s,transform .35s var(--ease)}
.champ:hover{border-color:#4A3D1E;color:var(--text);transform:translateY(-4px)}
.champ:hover .champ-name{color:var(--gold2)}
.champ-div{font-size:11px;font-weight:700;letter-spacing:.24em;color:var(--muted)}
.champ-who{display:flex;align-items:center;gap:16px}
.champ-who .av{width:54px;height:54px;font-size:20px}
.champ-name{font-size:28px;font-weight:700;line-height:.95;transition:color .25s}
.champ-foot{display:flex;justify-content:space-between;font-size:13px;color:var(--muted);border-top:1px solid var(--line);padding-top:14px}

/* results */
.split{display:flex;flex-wrap:wrap;gap:64px}
.split-main{flex:999 1 560px;min-width:0;display:flex;flex-direction:column;gap:28px}
.split-side{flex:1 1 320px;display:flex;flex-direction:column;gap:28px}
.res{display:flex;flex-wrap:wrap;align-items:center;gap:8px 18px;padding:20px 6px}
.res-who{flex:1 1 280px;font-size:24px;font-weight:700;display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 10px}
.res-who a{color:var(--text)}.res-who a:hover{color:var(--gold2)}
.res-who .loser{color:var(--muted)}
.def{font:500 12px Inter,sans-serif;color:var(--dim);letter-spacing:.12em}
.res-m{font-size:12px;font-weight:700;letter-spacing:.14em}
.m-ko{color:var(--red)}.m-sub{color:var(--blue)}.m-dec{color:#BFC3CA}
.res-w{font-size:12px;letter-spacing:.12em;color:var(--dim);min-width:150px;text-align:right}
.belt{font-size:10px;font-weight:700;letter-spacing:.18em;color:var(--bg);background:var(--gold);padding:3px 8px;border-radius:4px}
.res-note{font-size:12px;font-weight:700;letter-spacing:.16em;color:var(--muted);padding:0 6px 16px;margin-top:-10px;border-bottom:1px solid var(--line)}
.sec-h{margin:44px 0 14px;font-size:12px;font-weight:700;letter-spacing:.24em;color:var(--dim)}
.event .head{margin-bottom:10px}
.ev-notes{color:var(--muted);max-width:720px;margin:14px 0 0}
.pager{display:flex;justify-content:space-between;align-items:center;gap:16px;padding:0 0 100px}

/* P4P + rankings */
.p4p-row,.rank-row{display:flex;align-items:center;gap:16px;padding:14px 6px}
.p4p-row:hover,.rank-row:hover{padding-left:14px}
.p4p-rank{font-size:32px;width:34px;color:#71717A;font-variant-numeric:tabular-nums}
.p4p-name,.rank-name{flex:1;min-width:0;font-size:22px;font-weight:700;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.p4p-name:hover,.rank-name:hover{color:var(--gold2)}
.rank-n{font-size:34px;width:44px;color:#71717A;font-variant-numeric:tabular-nums}
.rank-name{font-size:26px}
.mv{font-size:12px;font-weight:700;letter-spacing:.06em;width:52px;text-align:right}
.mv-up{color:var(--green)}.mv-down{color:var(--red)}.mv-new{color:var(--gold)}.mv-none{color:#5A5A64}

/* page hero */
.phero{position:relative;overflow:hidden;border-bottom:1px solid var(--line)}
.phero-mark{position:absolute;right:-40px;top:-60px;font:800 420px/.8 var(--d);color:transparent;-webkit-text-stroke:1px rgba(201,162,74,.09);pointer-events:none;white-space:nowrap}
.phero-in{position:relative;padding-top:90px;padding-bottom:44px;display:flex;flex-direction:column;gap:18px}
.phero-title{margin:0;font-size:clamp(68px,12vw,160px);line-height:.8}
.tabs{margin-top:26px;display:flex;gap:4px 26px;border-bottom:1px solid var(--line);overflow-x:auto;scrollbar-width:none}
.tabs::-webkit-scrollbar{display:none}
.tab{background:none;border:0;padding:14px 0;min-height:44px;font:600 13px Inter,sans-serif;letter-spacing:.14em;color:var(--dim);white-space:nowrap;cursor:pointer;transition:color .2s,box-shadow .3s}
.tab:hover{color:var(--text)}
.tab[aria-selected=true]{color:var(--text);box-shadow:inset 0 -2px 0 var(--gold)}
.rank-wrap{padding-top:56px;padding-bottom:120px}
.rank-panel{display:flex;flex-wrap:wrap;gap:56px}
.rank-panel[hidden]{display:none}
.rank-panel.show{animation:panelIn .6s var(--ease) both}
.rank-list{flex:999 1 560px;min-width:0;display:flex;flex-direction:column}
.feature{flex:1 1 340px;align-self:flex-start;position:relative;overflow:hidden;display:flex;flex-direction:column;gap:24px;padding:40px 36px;border:1px solid var(--line2);border-radius:18px;background:radial-gradient(120% 80% at 0% 0%,rgba(201,162,74,.15),rgba(8,8,10,0) 60%),var(--panel);color:var(--text);transition:border-color .35s,transform .35s var(--ease)}
.feature:hover{border-color:#4A3D1E;color:var(--text);transform:translateY(-3px)}
.feature::after{content:"";position:absolute;inset:0;background:linear-gradient(115deg,transparent 35%,rgba(226,194,122,.09) 50%,transparent 65%);transform:translateX(-100%);animation:shine 5s var(--ease) 1.5s infinite}
.feature-vacant{background:var(--panel)}
.feature-vacant::after{display:none}
.feature-name{font-size:64px;line-height:.85}
.feature-stats{display:flex;gap:28px;font-size:12px;font-weight:600;letter-spacing:.16em;color:var(--muted);border-top:1px solid var(--line);padding-top:22px}
.feature-stats strong{display:block;font-size:32px;color:var(--text);letter-spacing:0}

/* fighters */
.filters{margin-top:22px;display:flex;flex-direction:column;gap:18px}
.search{display:flex;align-items:center;gap:12px;max-width:460px;border:1px solid var(--line2);border-radius:999px;padding:0 18px;background:var(--panel);color:var(--dim);transition:border-color .25s}
.search:focus-within{border-color:var(--gold)}
.search input{flex:1;background:none;border:0;color:var(--text);font:500 15px Inter,sans-serif;padding:14px 0;outline:none;min-width:0}
.search input::placeholder{color:var(--dim)}
.fchips{display:flex;flex-wrap:wrap;gap:8px}
.fchip{background:none;border:1px solid var(--line2);color:var(--muted);font:600 12px Inter,sans-serif;letter-spacing:.12em;padding:10px 14px;border-radius:999px;min-height:40px;cursor:pointer;transition:all .2s}
.fchip:hover{color:var(--text);border-color:#3A3A42}
.fchip[aria-pressed=true]{background:var(--gold);border-color:var(--gold);color:var(--bg)}
.fsec{padding-top:56px}
.fgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:12px}
.fcard{display:flex;align-items:center;gap:16px;padding:16px 18px;border:1px solid var(--line);border-radius:14px;background:var(--panel);color:var(--text);transition:border-color .3s,transform .3s var(--ease),background .3s}
.fcard:hover{border-color:#4A3D1E;background:#121216;color:var(--text);transform:translateY(-3px)}
.fcard[hidden]{display:none}
.fcard-t{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px}
.fcard-name{font-size:22px;font-weight:700;line-height:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fcard-sub{font-size:11px;font-weight:600;letter-spacing:.16em;color:var(--dim)}
.fcard-r{display:flex;flex-direction:column;align-items:flex-end;gap:4px}
.fcard-rec{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums}
.fcard-streak{font-size:11px;font-weight:700;letter-spacing:.1em;color:var(--muted)}
.fgrid-former .fcard{opacity:.7}
.badge{font-size:10px;font-weight:700;letter-spacing:.14em;color:var(--gold2);border:1px solid rgba(201,162,74,.5);padding:3px 8px;border-radius:999px}
.badge-gold{background:var(--gold);color:var(--bg);border-color:var(--gold)}

/* fighter profile */
.fhero{position:relative;overflow:hidden;border-bottom:1px solid var(--line);background:radial-gradient(70% 90% at 15% 30%,rgba(201,162,74,.13),rgba(8,8,10,0) 60%)}
.fhero-mark{position:absolute;right:-30px;bottom:-110px;font:800 440px/.8 var(--d);color:transparent;-webkit-text-stroke:1px rgba(201,162,74,.08);pointer-events:none;white-space:nowrap}
.fhero-in{position:relative;padding-top:80px;padding-bottom:64px;display:flex;flex-wrap:wrap;align-items:flex-end;gap:40px}
.fhero-main{flex:999 1 520px;min-width:0;display:flex;flex-direction:column;gap:22px}
.fhero-main p{margin:0}
.handle{align-self:flex-start;display:inline-flex;align-items:center;gap:10px;font-size:15px;font-weight:600;color:var(--text);border:1px solid var(--line2);background:var(--panel);padding:7px 14px 7px 8px;border-radius:999px}
.handle-l{font-size:10px;font-weight:700;letter-spacing:.18em;color:var(--bg);background:#8C93F2;padding:3px 8px;border-radius:999px}
.fhero-top{display:flex;align-items:center;gap:18px;flex-wrap:wrap}
.fbadge{display:flex;align-items:center;border:1px solid rgba(201,162,74,.5);background:rgba(201,162,74,.08);color:var(--gold2);padding:9px 16px;border-radius:999px;font-size:12px;font-weight:700;letter-spacing:.2em}
.fhero-name{margin:0;font-size:clamp(72px,13vw,176px);line-height:.8;overflow-wrap:anywhere}
.fhero-rec{flex:0 1 auto;display:flex;flex-direction:column;gap:6px;padding-bottom:8px}
.fhero-record{font-size:clamp(64px,8vw,104px);white-space:nowrap;line-height:.85;font-variant-numeric:tabular-nums}
.form{display:flex;gap:6px;margin-top:10px}
.pill{display:inline-grid;place-items:center;min-width:30px;height:30px;padding:0 6px;border-radius:8px;font-weight:700;font-size:13px}
.pill-w{background:rgba(61,214,140,.12);color:var(--green);border:1px solid rgba(61,214,140,.35)}
.pill-l{background:rgba(240,122,126,.1);color:var(--red);border:1px solid rgba(240,122,126,.35)}
.pill-d{background:rgba(201,162,74,.1);color:var(--gold2);border:1px solid rgba(201,162,74,.35)}
.pill-nc{background:#16161A;color:var(--muted);border:1px solid var(--line2);font-size:11px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));border-bottom:1px solid var(--line)}
.ftile{padding:44px 6px 40px;display:flex;flex-direction:column;gap:8px}
.ftile-v{font-size:60px;line-height:.9}
.mbars{margin-top:30px;display:flex;flex-direction:column;gap:18px;max-width:760px}
.mbar{display:flex;flex-direction:column;gap:8px}
.mbar-l{display:flex;justify-content:space-between;font-size:12px;font-weight:700;letter-spacing:.2em;color:#BFC3CA}
.mbar-track{height:6px;border-radius:3px;background:#17171B;overflow:hidden}
.bar{display:block;height:100%;border-radius:3px;transform-origin:left}
.bar-gold{background:var(--gold)}.bar-silver{background:#BFC3CA}.bar-dim{background:#5A5A64}
.sgrid{margin-top:30px;display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.stile{border:1px solid var(--line);border-radius:14px;background:var(--panel);padding:22px;display:flex;flex-direction:column;gap:6px}
.stile-v{font-size:44px;line-height:1}
.stile .muted{font-size:13px}
.hist-wrap{display:flex;flex-wrap:wrap;gap:56px;padding-bottom:120px}
.hist{flex:999 1 560px;min-width:0;display:flex;flex-direction:column;gap:22px}
.tscroll{overflow-x:auto}
.ftable{width:100%;min-width:560px;border-collapse:collapse}
.ftable th{font-size:11px;letter-spacing:.22em;color:var(--dim);text-align:left;padding:0 6px 14px;font-weight:700;border-bottom:1px solid var(--line)}
.ftable th.r{text-align:right}
.ftable td{padding:16px 6px;border-bottom:1px solid var(--line);vertical-align:middle}
.td-opp{font-size:22px;font-weight:700}
.td-opp a{color:var(--text)}.td-opp a:hover{color:var(--gold2)}
.td-m{font-size:13px;font-weight:600;letter-spacing:.08em;color:#BFC3CA}
.td-e{font-size:13px;color:var(--muted);text-align:right}
.td-e span{display:block;font-size:12px;color:var(--dim)}
.reigns{flex:1 1 300px;display:flex;flex-direction:column;gap:16px}
.reigns h2{margin-bottom:6px}
.reign{border:1px solid var(--line2);border-radius:16px;padding:24px;background:var(--panel);display:flex;flex-direction:column;gap:10px;transition:border-color .35s,transform .35s var(--ease)}
.reign:hover{border-color:#4A3D1E;transform:translateY(-3px)}
.reign-now{background:radial-gradient(120% 100% at 0% 0%,rgba(201,162,74,.12),rgba(8,8,10,0) 60%),var(--panel)}
.reign-tag{font-size:11px;font-weight:700;letter-spacing:.24em;color:var(--dim)}
.reign-now .reign-tag{color:var(--gold)}
.reign-t{font-size:26px;font-weight:700;line-height:1}
.reign .muted{font-size:13px}

/* titles */
.lin{display:flex;align-items:center;gap:16px;padding:16px 6px;border-bottom:1px solid var(--line);transition:background .25s}
.lin:hover{background:var(--panel2)}
.lin-n{font-size:26px;width:36px;color:#71717A}
.lin-now .lin-n{color:var(--gold)}
.lin-t{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px}
.lin-name{font-size:24px;font-weight:700;color:var(--text)}
.lin-name:hover{color:var(--gold2)}
.lin-t .muted{font-size:13px}
.lin-r{display:flex;flex-direction:column;align-items:flex-end;gap:6px;font-size:13px;text-align:right}
.head + .list{margin-top:36px}

/* timeline */
.timeline{position:relative;margin-top:48px}
.tl-line{position:absolute;left:0;right:0;top:7px;height:1px;background:linear-gradient(90deg,var(--gold),#3A3A42)}
.tl{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:32px}
.tl-item{display:flex;flex-direction:column;gap:12px}
.tl-dot{width:15px;height:15px;border-radius:50%;background:var(--bg);border:2px solid var(--gold)}
.tl-when{font-size:11px;font-weight:700;letter-spacing:.2em;color:var(--muted);margin-top:8px}
.tl-name{font-size:32px;line-height:.95;color:var(--text)}
.tl-name:hover{color:var(--gold2)}
.tl-how{font-size:14px}

/* join */
.join{position:relative;overflow:hidden;border-top:1px solid var(--line);background:radial-gradient(60% 120% at 50% 100%,rgba(201,162,74,.15) 0%,rgba(8,8,10,0) 70%),var(--bg)}
.join-in{padding-top:120px;padding-bottom:120px;display:flex;flex-direction:column;align-items:center;gap:30px;text-align:center}
.join-big{margin:0;font-size:clamp(56px,9vw,128px);line-height:.84}
.join-p{margin:0;max-width:520px;font-size:16px;line-height:1.6}

/* footer */
.foot{border-top:1px solid var(--line)}
.foot-in{padding-top:28px;padding-bottom:28px;display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:16px;font-size:13px;color:var(--dim)}
.foot-brand{display:flex;align-items:center;gap:12px}

/* load-in */
@keyframes logoIn{from{opacity:0;transform:scale(.9) rotate(-8deg)}to{opacity:1;transform:none}}
@keyframes trackIn{from{opacity:0;letter-spacing:.42em;filter:blur(8px)}to{opacity:1;filter:none}}
@keyframes trackInSm{from{opacity:0;letter-spacing:.24em;filter:blur(6px)}to{opacity:1;filter:none}}
@keyframes fadeUp{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:none}}
@keyframes scaleIn{from{opacity:0;transform:scale(.92)}to{opacity:1;transform:none}}
@keyframes ruleIn{from{transform:scaleX(0)}to{transform:scaleX(1)}}
@keyframes cue{0%{transform:translateY(-100%)}100%{transform:translateY(220%)}}
@keyframes ticker{from{transform:translateX(0)}to{transform:translateX(-50%)}}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.25}}
@keyframes breathe{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.7;transform:scale(1.06)}}
@keyframes shine{0%{transform:translateX(-100%)}40%,100%{transform:translateX(100%)}}
@keyframes panelIn{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
.l-logo{animation:logoIn 1.4s var(--ease) both}
.l-name{animation:trackIn 1.6s var(--ease) .35s both}
.l-sub{animation:fadeUp 1s var(--ease) .9s both}
.l-rule{transform-origin:center;animation:ruleIn 1.1s var(--ease) 1.1s both}
.l-chip{animation:fadeUp 1s var(--ease) 1.35s both}
.l-cue{animation:fadeUp 1s var(--ease) 1.7s both}
.l-title{animation:trackInSm 1.3s var(--ease) .1s both}
.l-badge{animation:scaleIn 1.1s var(--ease) both}
.l-up{animation:fadeUp .9s var(--ease) .35s both}
.l-up2{animation:fadeUp .9s var(--ease) .55s both}

/* scroll-linked (Chrome / Edge). Other browsers use site.js. */
@keyframes navSolid{from{background:rgba(8,8,10,0);border-color:rgba(36,36,42,0)}to{background:rgba(8,8,10,.92);border-color:rgba(36,36,42,1)}}
@keyframes heroOut{to{opacity:0;transform:translateY(-90px) scale(.82)}}
@keyframes nameOut{to{opacity:0;letter-spacing:.3em;transform:translateY(-40px)}}
@keyframes parallax{to{transform:translate(-50%,280px)}}
@keyframes markUp{to{transform:translateY(-160px)}}
@keyframes glowOut{to{opacity:0;transform:scale(1.25)}}
@keyframes fheroOut{to{opacity:.15;transform:translateY(-60px)}}
@keyframes reveal{from{opacity:0;transform:translateY(56px)}to{opacity:1;transform:none}}
@keyframes fromLeft{from{opacity:0;transform:translateX(-140px)}to{opacity:1;transform:none}}
@keyframes fromRight{from{opacity:0;transform:translateX(140px)}to{opacity:1;transform:none}}
@keyframes vsIn{from{opacity:0;transform:scale(2.6)}to{opacity:1;transform:none}}
@keyframes lineDraw{from{transform:scaleX(0)}to{transform:scaleX(1)}}
@keyframes bigRise{from{transform:translateY(120px);opacity:0}to{transform:none;opacity:1}}
@supports (animation-timeline: view()){
  .over-hero .nav{animation:navSolid linear both;animation-timeline:scroll(root);animation-range:0 420px}
  .s-hero{animation:heroOut linear both;animation-timeline:scroll(root);animation-range:0 620px}
  .s-name{animation:nameOut linear both;animation-timeline:scroll(root);animation-range:60px 640px}
  .s-mark-par{animation:parallax linear both;animation-timeline:scroll(root);animation-range:0 900px}
  .s-glow{animation:glowOut linear both;animation-timeline:scroll(root);animation-range:0 700px}
  .s-mark{animation:markUp linear both;animation-timeline:scroll(root);animation-range:0 800px}
  .s-fhero{animation:fheroOut linear both;animation-timeline:scroll(root);animation-range:140px 660px}
  .rv{animation:reveal linear both;animation-timeline:view();animation-range:entry 0% cover 24%}
  .rv2{animation:reveal linear both;animation-timeline:view();animation-range:entry 8% cover 30%}
  .rv3{animation:reveal linear both;animation-timeline:view();animation-range:entry 16% cover 36%}
  .rv4{animation:reveal linear both;animation-timeline:view();animation-range:entry 24% cover 42%}
  .sl{animation:fromLeft linear both;animation-timeline:view();animation-range:entry 0% cover 40%}
  .sr{animation:fromRight linear both;animation-timeline:view();animation-range:entry 0% cover 40%}
  .vs{animation:vsIn linear both;animation-timeline:view();animation-range:entry 20% cover 45%}
  .draw{transform-origin:left;animation:lineDraw linear both;animation-timeline:view();animation-range:entry 10% cover 40%}
  .bar{animation:lineDraw linear both;animation-timeline:view();animation-range:entry 10% cover 40%}
  .big{animation:bigRise linear both;animation-timeline:view();animation-range:entry 0% cover 35%}
}
/* fallback reveal for browsers without scroll timelines */
.no-sdt .rv,.no-sdt .rv2,.no-sdt .rv3,.no-sdt .rv4,.no-sdt .sl,.no-sdt .sr,.no-sdt .vs,.no-sdt .big{opacity:0;transform:translateY(40px);transition:opacity .9s var(--ease),transform .9s var(--ease)}
.no-sdt .sl{transform:translateX(-80px)}.no-sdt .sr{transform:translateX(80px)}.no-sdt .vs{transform:scale(2)}
.no-sdt .rv2{transition-delay:.08s}.no-sdt .rv3{transition-delay:.16s}.no-sdt .rv4{transition-delay:.24s}
.no-sdt .in{opacity:1!important;transform:none!important}
.no-sdt .bar,.no-sdt .draw{transform:scaleX(0);transition:transform 1.2s var(--ease)}
.no-sdt .bar.in,.no-sdt .draw.in{transform:scaleX(1)!important}

/* phones */
@media (max-width:700px){
  .wrap{padding-left:16px;padding-right:16px}
  .nav-in{gap:6px 18px;flex-wrap:wrap}
  .nav-links{order:3;flex-basis:100%;gap:2px 22px}
  .nav .btn{padding:9px 14px;min-height:40px;font-size:12px;margin-left:auto}
  .over-hero main{margin-top:-116px}
  .block{padding:72px 0}
  #event{padding-top:80px}
  .corner{padding:32px 24px}
  .vs{flex-basis:100%;padding:4px 0;font-size:40px}
  .corner-b{align-items:flex-start;text-align:left;border-right:0;border-left:3px solid var(--bluec)}
  .corner-b .corner-stats{flex-direction:row-reverse;justify-content:flex-end}
  .countdown>div{min-width:64px}
  .card-row{grid-template-columns:1fr}
  .card-weight{text-align:left}
  .res-w{min-width:0;text-align:left}
  .nums{grid-template-columns:1fr 1fr}
  .num{padding:36px 4px}
  .num-v{font-size:56px}
  .feature{padding:30px 24px}
  .feature-name{font-size:52px}
  .fhero-record{font-size:84px}
  .rank-row .av,.p4p-row .av{display:none}
  .rank-row,.p4p-row{gap:12px}
  .rank-name,.p4p-name{font-size:21px;white-space:normal;overflow-wrap:anywhere}
  .rank-n{width:32px;font-size:28px}
  .fhero-in{padding-top:56px}
  .phero-in{padding-top:64px}
  .phero-mark,.fhero-mark{font-size:260px}
  .lin-r .muted{display:none}
}
/* ===== stand-out layer: scroll motion, shine, glare ===== */
.block{position:relative}
.block>*:not(.drift){position:relative;z-index:1}
.drift{position:absolute;z-index:0;top:46px;left:-4%;font:800 clamp(120px,19vw,260px)/.8 var(--d);letter-spacing:-.01em;white-space:nowrap;color:transparent;-webkit-text-stroke:1px rgba(201,162,74,.10);pointer-events:none;user-select:none;will-change:transform}
.drift-rev{left:auto;right:-4%}

/* scroll progress line under the nav */
.nav::after{content:"";position:absolute;left:0;right:0;bottom:-1px;height:2px;background:linear-gradient(90deg,var(--gold),#F3DDA6);transform-origin:left;transform:scaleX(var(--progress,0))}

/* gold shine across the big titles */
@keyframes shimmer{0%{background-position:120% 0}30%,100%{background-position:-20% 0}}
.hero-big{background:linear-gradient(100deg,#FFF 0%,#E9E6DE 38%,#FFF3D1 46%,#E2C27A 50%,#FFF3D1 54%,#B9B3A4 64%,#E9E6DE 100%);background-size:260% 100%;-webkit-background-clip:text;background-clip:text}
.hero-big.l-name{animation:trackIn 1.6s var(--ease) .35s both,shimmer 7s ease-in-out 2.4s infinite}
.phero-title,.fhero-name{background:linear-gradient(100deg,#F4F4F5 0%,#F4F4F5 40%,#FFF3D1 47%,#E2C27A 50%,#FFF3D1 53%,#F4F4F5 60%,#F4F4F5 100%);background-size:260% 100%;-webkit-background-clip:text;background-clip:text;color:transparent}
.phero-title.l-title,.fhero-name.l-title{animation:trackInSm 1.3s var(--ease) .1s both,shimmer 8s ease-in-out 1.6s infinite}

/* headings wipe up out of a mask as they scroll in */
@keyframes wipeUp{from{clip-path:inset(0 0 100% 0);transform:translateY(40%)}to{clip-path:inset(-10% 0 -20% 0);transform:none}}

/* ticker */
.ticker{display:flex;align-items:stretch}
.tick-live{flex:none;display:flex;align-items:center;gap:10px;padding:0 20px 0 max(16px,env(safe-area-inset-left,0px));font:700 13px var(--d);letter-spacing:.2em;color:var(--bg);background:var(--gold);position:relative;z-index:1}
.tick-live .dot{background:var(--bg)}
.tick-win{flex:1;min-width:0;overflow:hidden;-webkit-mask:linear-gradient(90deg,transparent,#000 40px);mask:linear-gradient(90deg,transparent,#000 40px)}

.tick{gap:10px;padding:14px 22px}
.tick b{font:700 17px var(--d);letter-spacing:.02em;color:var(--text)}
.tick-dim{color:var(--dim);font-size:12px;letter-spacing:.06em}
.tick-m{font-size:11px;font-weight:700;letter-spacing:.14em;padding:3px 8px;border-radius:999px;border:1px solid currentColor;opacity:.95}
.tick-belt{font-size:10px;font-weight:700;letter-spacing:.16em;color:var(--bg);background:var(--gold);padding:3px 7px;border-radius:4px}
.tick-rank{font:800 18px var(--d);color:var(--muted);font-variant-numeric:tabular-nums}
.tick-head{padding-left:34px}
.tick-chip{font:700 12px var(--d);letter-spacing:.2em;text-transform:uppercase;color:var(--gold2);border:1px solid rgba(201,162,74,.45);background:rgba(201,162,74,.08);padding:5px 12px;border-radius:999px}
.tick-head-alt .tick-chip{color:var(--bg);background:var(--gold);border-color:var(--gold)}
.tick:not(.tick-head)+.tick:not(.tick-head)::before{content:"";width:4px;height:4px;border-radius:50%;background:#3A3A42;margin-right:12px}
@keyframes tickIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.tick-win .track{animation-name:ticker}
.ticker .tick-m{animation:mGlow 2.4s ease-in-out infinite}
@keyframes mGlow{0%,100%{box-shadow:0 0 0 0 transparent}50%{box-shadow:0 0 12px -2px currentColor}}

/* main event: photos, pulsing VS */
.corner-top{display:flex;align-items:center;gap:16px}
.corner-b .corner-top{flex-direction:row-reverse}
.corner-av{width:64px;height:64px;font-size:22px;border-width:2px}
.corner-a .corner-av{border-color:var(--redc);box-shadow:0 0 0 5px rgba(229,72,77,.12),0 0 28px rgba(229,72,77,.35)}
.corner-b .corner-av{border-color:var(--bluec);box-shadow:0 0 0 5px rgba(59,130,246,.12),0 0 28px rgba(59,130,246,.35)}
.vs{position:relative}
.vs::before{content:"";position:absolute;left:50%;top:50%;width:84px;height:84px;margin:-42px 0 0 -42px;border-radius:50%;border:1px solid rgba(201,162,74,.5);animation:ring 2.4s cubic-bezier(.2,.6,.3,1) infinite}
.vs-t{display:inline-block;animation:vsGlow 3.2s ease-in-out infinite}
@keyframes ring{0%{transform:scale(.7);opacity:.9}100%{transform:scale(1.9);opacity:0}}
@keyframes vsGlow{0%,100%{text-shadow:0 0 18px rgba(201,162,74,.25)}50%{text-shadow:0 0 34px rgba(226,194,122,.7)}}

/* countdown digits flip on change */
.countdown b.flip{animation:flip .45s var(--ease)}
@keyframes flip{from{transform:translateY(-35%) rotateX(70deg);opacity:0}to{transform:none;opacity:1}}

/* a light that follows the cursor on cards */
.champ,.feature,.fcard,.reign,.stile{position:relative;overflow:hidden}
.champ::before,.fcard::before,.reign::before,.stile::before,.feature::before{content:"";position:absolute;inset:0;pointer-events:none;opacity:0;transition:opacity .35s;background:radial-gradient(260px circle at var(--gx,50%) var(--gy,50%),rgba(226,194,122,.13),transparent 65%)}
.champ:hover::before,.fcard:hover::before,.reign:hover::before,.stile:hover::before,.feature:hover::before{opacity:1}
.champ>*,.fcard>*,.reign>*,.stile>*{position:relative}

/* the #1 spot gets a gold wash, champions' rings pulse */
.rank-row:first-child,.p4p-row:first-child{background:linear-gradient(90deg,rgba(201,162,74,.09),transparent 70%)}
.av-xl,.av-hero{animation:ringPulse 2.8s ease-out infinite}
@keyframes ringPulse{0%{box-shadow:0 0 0 0 rgba(201,162,74,.4)}80%,100%{box-shadow:0 0 0 16px rgba(201,162,74,0)}}

/* champion cards and fighter tiles rise and settle as you scroll */
@keyframes riseScale{from{opacity:0;transform:translateY(60px) scale(.94)}to{opacity:1;transform:none}}

@supports (animation-timeline: view()){
  .h-xl,.h-l,.h-m,.join-big{animation:wipeUp linear both;animation-timeline:view();animation-range:entry 5% cover 28%}
  .champ,.fcard,.stile{animation:riseScale linear both;animation-timeline:view();animation-range:entry 0% cover 26%}
  .ftile{animation:riseScale linear both;animation-timeline:view();animation-range:entry 0% cover 30%}
}
.no-sdt .h-xl,.no-sdt .h-l,.no-sdt .h-m{clip-path:inset(0 0 100% 0);transform:translateY(40%);transition:clip-path 1s var(--ease),transform 1s var(--ease)}
.no-sdt .h-xl.in,.no-sdt .h-l.in,.no-sdt .h-m.in{clip-path:inset(-10% 0 -20% 0)}

/* smooth fade between pages (Chrome, Edge, Safari) */
@view-transition{navigation:auto}
::view-transition-old(root){animation:.25s ease both vtOut}
::view-transition-new(root){animation:.45s var(--ease) both vtIn}
@keyframes vtOut{to{opacity:0}}
@keyframes vtIn{from{opacity:0;transform:translateY(14px)}}

@media (max-width:700px){
  .drift{top:30px;font-size:110px}
  .corner-b .corner-top{flex-direction:row}
  .corner-av{width:52px;height:52px}
  .tick-live{padding-right:14px}
}
@media (prefers-reduced-motion:reduce){.drift{transform:none!important}.no-sdt .h-xl,.no-sdt .h-l,.no-sdt .h-m{clip-path:none;transform:none}}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}html{scroll-behavior:auto}.no-sdt .rv,.no-sdt .rv2,.no-sdt .rv3,.no-sdt .rv4,.no-sdt .sl,.no-sdt .sr,.no-sdt .vs,.no-sdt .big,.no-sdt .bar,.no-sdt .draw{opacity:1;transform:none}}
"""

EXTRA_JS = r"""
(function () {
  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var root = document.documentElement;
  var nav = document.querySelector('.nav');
  var drifts = [].slice.call(document.querySelectorAll('.drift'));
  var ticking = false;

  // Scroll progress line + giant words sliding sideways with the scroll
  function frame() {
    ticking = false;
    var max = root.scrollHeight - innerHeight;
    if (nav) nav.style.setProperty('--progress', max > 0 ? Math.min(1, scrollY / max) : 0);
    if (reduce) return;
    drifts.forEach(function (el) {
      var box = el.parentNode.getBoundingClientRect();
      if (box.bottom < -200 || box.top > innerHeight + 200) return;
      var p = (innerHeight - box.top) / (innerHeight + box.height);   // 0 → 1 as the section passes
      var dir = el.classList.contains('drift-rev') ? 1 : -1;
      el.style.transform = 'translate3d(' + (dir * (p - 0.5) * 34) + '%,0,0)';
    });
  }
  function onScroll() { if (!ticking) { ticking = true; requestAnimationFrame(frame); } }
  addEventListener('scroll', onScroll, { passive: true });
  addEventListener('resize', onScroll);
  addEventListener('hashchange', function () { setTimeout(frame, 30); });
  frame();

  // Headings fallback for browsers without scroll timelines
  if (root.classList.contains('no-sdt') && 'IntersectionObserver' in window) {
    var ho = new IntersectionObserver(function (es) { es.forEach(function (e) { if (e.isIntersecting) { e.target.classList.add('in'); ho.unobserve(e.target); } }); }, { rootMargin: '0px 0px -10% 0px' });
    document.querySelectorAll('.h-xl,.h-l,.h-m').forEach(function (h) { ho.observe(h); });
  }

  // Cursor light on cards
  if (!reduce && matchMedia('(hover: hover)').matches) {
    document.addEventListener('pointermove', function (e) {
      var card = e.target.closest && e.target.closest('.champ,.feature,.fcard,.reign,.stile');
      if (!card) return;
      var r = card.getBoundingClientRect();
      card.style.setProperty('--gx', (e.clientX - r.left) + 'px');
      card.style.setProperty('--gy', (e.clientY - r.top) + 'px');
    }, { passive: true });
  }

  // Countdown digits flip when they change
  if (!reduce) {
    document.querySelectorAll('[data-countdown] b').forEach(function (b) {
      new MutationObserver(function () {
        if (b.textContent === b.dataset.last) return;
        b.dataset.last = b.textContent;
        b.classList.remove('flip'); void b.offsetWidth; b.classList.add('flip');
      }).observe(b, { childList: true, characterData: true, subtree: true });
    });
  }
})();
"""

SITE_JS = r"""
(function () {
  var root = document.documentElement;
  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var sdt = window.CSS && CSS.supports && CSS.supports('animation-timeline: view()');

  // Nav goes solid after scrolling (when CSS can't do it)
  var nav = document.querySelector('.nav');
  function onScroll() { if (nav) nav.classList.toggle('solid', window.scrollY > 80); }
  if (!sdt) { window.addEventListener('scroll', onScroll, { passive: true }); onScroll(); }

  // Reveal on scroll for browsers without scroll timelines
  if (!sdt && !reduce && 'IntersectionObserver' in window) {
    root.classList.add('no-sdt');
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); } });
    }, { rootMargin: '0px 0px -8% 0px' });
    document.querySelectorAll('.rv,.rv2,.rv3,.rv4,.sl,.sr,.vs,.big,.bar,.draw').forEach(function (el) { io.observe(el); });
  }

  // Count-up numbers
  function countUp(el) {
    var end = parseInt(el.getAttribute('data-count'), 10);
    if (isNaN(end) || end < 2 || reduce) return;
    var start = null, dur = Math.min(1800, 600 + end * 8);
    function step(t) {
      if (!start) start = t;
      var p = Math.min(1, (t - start) / dur), eased = 1 - Math.pow(1 - p, 3);
      el.textContent = Math.round(end * eased).toLocaleString('en-GB');
      if (p < 1) requestAnimationFrame(step);
    }
    el.textContent = '0';
    requestAnimationFrame(step);
  }
  var counters = document.querySelectorAll('[data-count]');
  if ('IntersectionObserver' in window) {
    var co = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { if (e.isIntersecting) { countUp(e.target); co.unobserve(e.target); } });
    }, { threshold: 0.4 });
    counters.forEach(function (el) { co.observe(el); });
  }

  // Countdown
  var cd = document.querySelector('[data-countdown]');
  if (cd) {
    var target = parseInt(cd.getAttribute('data-countdown'), 10) * 1000;
    var parts = {};
    cd.querySelectorAll('[data-unit]').forEach(function (el) { parts[el.getAttribute('data-unit')] = el; });
    var pad = function (n) { return String(n).padStart(2, '0'); };
    var tick = function () {
      var left = Math.max(0, Math.floor((target - Date.now()) / 1000));
      var d = Math.floor(left / 86400); left -= d * 86400;
      var h = Math.floor(left / 3600); left -= h * 3600;
      var m = Math.floor(left / 60), s = left - m * 60;
      parts.d.textContent = pad(d); parts.h.textContent = pad(h); parts.m.textContent = pad(m); parts.s.textContent = pad(s);
    };
    tick(); setInterval(tick, 1000);
  }

  // Rankings tabs
  var tabs = document.querySelectorAll('[data-tab]');
  tabs.forEach(function (tab) {
    tab.addEventListener('click', function (ev) {
      ev.preventDefault();
      var name = tab.getAttribute('data-tab');
      tabs.forEach(function (t) { t.setAttribute('aria-selected', t === tab ? 'true' : 'false'); });
      document.querySelectorAll('[data-panel]').forEach(function (p) {
        var on = p.getAttribute('data-panel') === name;
        p.hidden = !on;
        if (on) { p.classList.remove('show'); void p.offsetWidth; p.classList.add('show'); }
      });
      history.replaceState(null, '', '?d=' + encodeURIComponent(name));
    });
  });

  // Fighter search + division filter
  var search = document.getElementById('fsearch');
  if (search) {
    var cards = document.querySelectorAll('#fgrid .fcard');
    var chips = document.querySelectorAll('[data-filter]');
    var none = document.getElementById('fnone');
    var div = '';
    var apply = function () {
      var q = search.value.trim().toLowerCase(), shown = 0;
      cards.forEach(function (c) {
        var ok = (!q || c.getAttribute('data-name').indexOf(q) !== -1) && (!div || c.getAttribute('data-div') === div);
        c.hidden = !ok; if (ok) shown++;
      });
      if (none) none.hidden = shown > 0;
    };
    search.addEventListener('input', apply);
    chips.forEach(function (chip) {
      chip.addEventListener('click', function () {
        div = chip.getAttribute('data-filter');
        chips.forEach(function (c) { c.setAttribute('aria-pressed', c === chip ? 'true' : 'false'); });
        apply();
      });
    });
  }
})();
"""


SITE_JS += EXTRA_JS


# ============================================================
# Web server
# ============================================================

_cache = {}


def build(bot, g, key, maker):
    """Builds a page (or uses the copy from the last few seconds)."""

    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]

    guild = pick_guild(bot, g)
    if guild is None:
        return None

    ctx = Ctx(g, guild)
    result = maker(ctx)
    _cache[key] = (time.time(), result)

    if len(_cache) > 400:
        for old in sorted(_cache, key=lambda k: _cache[k][0])[:200]:
            _cache.pop(old, None)

    return result


def make_app(bot, g):

    def html_response(text, status=200):
        return web.Response(text=text, status=status, content_type="text/html", charset="utf-8",
                            headers={"Cache-Control": "public, max-age=15"})

    async def serve(request, key, maker, status=200):
        if not bot.is_ready():
            return html_response(render_starting(), 503)
        try:
            text = build(bot, g, key, maker)
        except Exception:
            traceback.print_exc()
            return html_response("<!doctype html><meta charset=utf-8><body style='background:#08080A;color:#eee;font:16px system-ui;padding:40px'>"
                                 "<p>Something went wrong loading this page. Try again in a moment.</p><p><a style='color:#E2C27A' href='/'>Home</a></p>", 500)
        if text is None:
            return html_response(build(bot, g, "404", render_404), 404)
        return html_response(text, status)

    async def home(request):
        return await serve(request, "home", render_home)

    async def rankings(request):
        division = request.query.get("d")
        return await serve(request, f"rankings:{division}", lambda ctx: render_rankings(ctx, division))

    async def fighters(request):
        return await serve(request, "fighters", render_fighters)

    async def fighter(request):
        raw = request.match_info.get("user_id", "")
        if not raw.isdigit():
            return await serve(request, "404", render_404, 404)
        return await serve(request, f"fighter:{raw}", lambda ctx: render_fighter(ctx, int(raw)))

    async def results(request):
        raw = request.query.get("page", "1")
        page_no = int(raw) if raw.isdigit() else 1
        return await serve(request, f"results:{page_no}", lambda ctx: render_results(ctx, page_no))

    async def titles(request):
        return await serve(request, "titles", render_titles)

    async def css(request):
        return web.Response(text=SITE_CSS, content_type="text/css", headers={"Cache-Control": "public, max-age=86400"})

    async def js(request):
        return web.Response(text=SITE_JS, content_type="application/javascript", headers={"Cache-Control": "public, max-age=86400"})

    async def health(request):
        return web.Response(text="ok")

    async def not_found(request):
        return await serve(request, "404", render_404, 404)

    app = web.Application()
    app.router.add_get("/", home)
    app.router.add_get("/rankings", rankings)
    app.router.add_get("/fighters", fighters)
    app.router.add_get("/fighter/{user_id}", fighter)
    app.router.add_get("/results", results)
    app.router.add_get("/titles", titles)
    app.router.add_get("/assets/site.css", css)
    app.router.add_get("/assets/site.js", js)
    app.router.add_get("/health", health)
    app.router.add_get("/{tail:.*}", not_found)

    return app


_runner = None


async def start(bot, g):
    """Starts the website. Called once from the bot's setup_hook."""

    global _runner

    if not WEBSITE_ON:
        print("Website is turned off (WEBSITE=off).")
        return

    if web is None:
        print("Website: aiohttp isn't installed, so the website can't start.")
        return

    if _runner is not None:
        return

    port = int(os.getenv("PORT", "8080"))
    _runner = web.AppRunner(make_app(bot, g), access_log=None)
    await _runner.setup()
    await web.TCPSite(_runner, "0.0.0.0", port).start()

    print(f"PFO website running on port {port}.")
