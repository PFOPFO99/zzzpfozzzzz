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
  /event/NAME   One event: the card in order, results, awards, odds
                vs result, fantasy
  /live         The card that's on now (or next): results appear as
                they're logged, the page refreshes itself
  /compare      Head to head: pick two fighters and compare them
  /sportsbook   Live odds for every open card (same as the Discord
                sportsbook post), betting leaderboards, big wins, upsets
  /fantasy      Fantasy PFO: standings with everyone's team, the price
                list, best performers, price movers, most picked

Railway Variables (all optional):
  DISCORD_INVITE   your Discord invite link, e.g. https://discord.gg/abc123
                   (the "Join Discord" buttons are hidden until it's set)
  TWITCH_URL       your Twitch channel (shows a "Watch live" button)
  WEBSITE          set to off to turn the website off
  PORT             Railway sets this itself, leave it alone

In Railway: open the bot's service → Settings → Networking →
Generate Domain. That's the website's address.
"""

import asyncio
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import time
import traceback
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

try:
    from aiohttp import web
except ImportError:  # only when testing without discord.py installed
    web = None


DISCORD_INVITE = os.getenv("DISCORD_INVITE", "").strip()
SITE_URL = os.getenv("SITE_URL", "").strip().rstrip("/")             # e.g. https://prestigefightingorg.com
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "").strip()       # for "Log in with Discord"
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "").strip()
LOGIN_ON = bool(SITE_URL and DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET)
TWITCH_URL = os.getenv("TWITCH_URL", "").strip()
WEBSITE_ON = os.getenv("WEBSITE", "on").strip().lower() not in ("off", "no", "false", "0")
SITE_VERSION = "25"            # bump if the styles change and browsers keep the old ones
CACHE_SECONDS = 60            # each page is rebuilt at most once a minute
MAX_BUILDS_AT_ONCE = 2        # pages built at the same time (keeps the bot responsive)
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

    MEMO = ("title_lineage", "all_title_outcomes", "get_fighter_stats", "recent_events",
            "get_division_rankings", "get_user_rankings", "fight_stat_totals")

    def __getattr__(self, name):
        try:
            fn = self.g[name]
        except KeyError:
            raise AttributeError(name)
        if name in self.MEMO and callable(fn):
            memo = self.__dict__.setdefault("_memo", {})
            def remembered(*args):
                key = (name, args)
                if key not in memo:
                    memo[key] = fn(*args)
                return memo[key]
            self.__dict__[name] = remembered
            return remembered
        return fn

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

TAG_IN_NAME = re.compile(r"\s*\[[^\[\]]*\]")


def clean_name(ctx, name):
    """'Oliveira [C] / Pico (4-0-0)' -> 'Oliveira / Pico'."""
    name = ctx.name_without_record(name or "")
    name = TAG_IN_NAME.sub("", name)
    return " ".join(name.split()) or "Fighter"


def fighter_name(ctx, user_id, saved=None):
    return clean_name(ctx, _fighter_name(ctx, user_id, saved))


def _fighter_name(ctx, user_id, saved=None):
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
    """'Joshua Van' -> ('JOSHUA', 'VAN'); 'Oliveira / Pico' -> ('OLIVEIRA', '/ PICO')."""
    if "/" in (name or ""):
        left, right = name.split("/", 1)
        if left.strip() and right.strip():
            return left.strip().upper(), "/ " + right.strip().upper()
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


def champ_tag_for(ctx, user_id):
    """DOUBLE CHAMP / TRIPLE CHAMP, or None."""
    belts = sum(1 for r in ctx.get_user_rankings(ctx.gid, user_id) if r["rank"] == 0 and r["weight"] != ctx.P4P_WEIGHT)
    return {2: "DOUBLE CHAMP", 3: "TRIPLE CHAMP"}.get(belts, f"{belts}x CHAMP" if belts > 3 else None)


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


def when_html(unix, upper=False):
    """A time that the visitor's browser turns into their own time zone (UK time stays as the fallback)."""
    text = event_when(unix)
    iso = datetime.fromtimestamp(int(unix), timezone.utc).isoformat()
    return (f'<time datetime="{iso}" data-utc="{int(unix)}"{" data-up=1" if upper else ""}>'
            f'{esc(text.upper() if upper else text)}</time>')


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
    ("live", "/live", "Live"),
    ("rankings", "/rankings", "Rankings"),
    ("fighters", "/fighters", "Fighters"),
    ("results", "/results", "Results"),
    ("Bets & Fantasy", [
        ("sportsbook", "/sportsbook", "Sportsbook", "Live odds, bet and cash out"),
        ("fantasy", "/fantasy", "Fantasy", "Your team, leagues and prices"),
        ("shop", "/shop", "Shop", "Spend your PFO Coins"),
    ]),
    ("social", "/social", "Social"),
    ("League", [
        ("teams", "/teams", "Teams", "Fighter teams and team records"),
        ("cards", "/cards", "Cards", "Every fighter's trading card"),
        ("staffpage", "/staff", "Staff", "Who runs PFO and how to reach them"),
    ]),
    ("Stats", [
        ("week", "/week", "The week", "Weekly recap and past weeks"),
        ("records", "/records", "Records", "All-time records and milestones"),
        ("awards", "/awards", "Awards", "FOTN, POTN and KOTN"),
        ("leaderboards", "/leaderboards", "Leaderboards", "Top 10 in every category"),
        ("titles", "/titles", "Titles", "Every champion, every division"),
        ("halloffame", "/hall-of-fame", "Hall of Fame", "PFO legends"),
        ("compare", "/compare", "Head to head", "Compare any two fighters"),
    ]),
]


def nav_html(active, live=False):
    """Main links, two dropdowns, and the phone menu (same links)."""
    out = []
    for item in NAV:
        if isinstance(item[1], list):
            label, children = item
            current = any(key == active for key, *_ in children)
            links = "".join(
                f'<a href="{href}"' + (' aria-current="page"' if key == active else '') +
                f'><span class="nd-t">{esc(text)}</span><span class="nd-s">{esc(sub)}</span></a>'
                for key, href, text, sub in children
            )
            out.append(
                f'<details class="ndrop{" is-current" if current else ""}"><summary>{esc(label)}'
                f'<svg aria-hidden="true" width="10" height="6" viewBox="0 0 10 6"><path d="M1 1l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>'
                f'</summary><div class="ndrop-menu">{links}</div></details>'
            )
        else:
            key, href, label = item
            dot = '<span class="nlive" aria-label="on now"></span>' if key == "live" and live else ""
            out.append(f'<a class="nlink" href="{href}"' + (' aria-current="page"' if key == active else '') + f'>{dot}{esc(label)}</a>')
    return "".join(out)


def card_is_live(ctx):
    try:
        notice = live_notice(ctx)
        if not notice:
            return False
        logged, total = ctx.notice_fights(ctx.gid, notice)
        raw = str(row_get(notice, "event_date" if notice["kind"] == "live_card" else "deadline", ""))
        started = bool(logged) or (raw.isdigit() and int(raw) <= time.time())
        return started and len(logged) < total
    except Exception:
        return False




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

    links = nav_html(active, card_is_live(ctx))

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
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/icon-192.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="PFO">
<link rel="stylesheet" href="/assets/site.css?v={SITE_VERSION}">
<script src="/assets/site.js?v={SITE_VERSION}" defer></script>
</head>
<body class="{'' if solid_nav else 'over-hero'}">
<a class="skip" href="#main">Skip to content</a>
<header class="nav">
  <nav class="wrap nav-in" aria-label="Main">
    <a class="brand" href="/" aria-label="PFO home">{logo_html(ctx, 'logo-sm')}<span>PFO</span></a>
    <div class="nav-links" id="navlinks">{links}</div>
    {discord_button(extra="nav-cta")}
    <div class="nav-user" id="nav-user" data-login="{1 if LOGIN_ON else 0}"></div>
    <button class="nav-burger" type="button" aria-label="Menu" aria-expanded="false" aria-controls="navlinks"><span></span><span></span><span></span></button>
  </nav>
</header>
{twitch_bar(ctx)}
<main id="main">
{body}
</main>
<footer class="foot">
  <div class="wrap foot-in">
    <span class="foot-brand">{logo_html(ctx, 'logo-xs')}© {datetime.now(timezone.utc).year} Prestige Fighting Organisation</span>
    <span class="foot-links"><a href="/app">Get the app</a><button type="button" class="tzbtn" id="tzbtn" hidden>Times: your time</button></span>
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

    # 0) The newest card DMed with /dmlivecard or /dmfightnight that's still to come.
    #    These are typed Main Event first, with Main Card / Prelims / Early Prelims.
    layout_fn = getattr(ctx, "notice_layout", None)

    if layout_fn:
        for notice in ctx.q("SELECT * FROM card_notices WHERE guild_id = ? ORDER BY id DESC LIMIT 3", (ctx.gid,)):
            kind = row_get(notice, "kind")
            raw = str(row_get(notice, "event_date" if kind == "live_card" else "deadline", ""))
            if not raw.isdigit():
                continue
            when = int(raw)
            if kind == "live_card" and when < now - 4 * 3600:
                continue
            if kind != "live_card" and when < now - 12 * 3600:
                continue
            name = row_get(notice, "event") or ("Live Card" if kind == "live_card" else "Fight Night")
            layout = layout_fn(notice)
            gone = card_cancelled(ctx, notice)
            layout = [x for x in layout if frozenset((x[0], x[1])) not in gone]
            if not layout:
                continue
            if row_get(notice, "event") and len(ctx.event_fights(ctx.gid, name)) >= len(layout):
                continue          # every fight's been logged: the event is over
            return {
                "name": name,
                "when": when,
                "kind": kind,
                "main_first": True,
                "bouts": [(a, b, ctx.auto_weight(ctx.gid, a, b)) for a, b, _ in layout],
                "spots": [spot for _, _, spot in layout],
            }

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

        gone = card_cancelled(ctx, notice)
        bouts = [x for x in bouts if frozenset((x[0], x[1])) not in gone]
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
            gone = ctx.cancelled_pairs(ctx.gid, card["event"]) if getattr(ctx, "cancelled_pairs", None) else set()
            bouts = [x for x in bouts if frozenset((x[0], x[1])) not in gone]
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
        chip_text = esc(event["name"].upper()) + (f" · {when_html(event['when'], True)}" if event["when"] else "")
        chip = f'<a href="#event" class="chip l-chip btn"><span class="dot dot-red" aria-hidden="true"></span>{chip_text}</a>'
    elif signups:
        title, closes = signups[0]
        extra = f" · CLOSES {when_html(closes, True)}" if closes else ""
        target = esc(DISCORD_INVITE) if DISCORD_INVITE else "#join"
        chip = f'<a href="{target}" class="chip l-chip btn"><span class="dot" aria-hidden="true"></span>{esc(title.upper())} OPEN{extra}</a>'

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
                     + (f'<span class="tick-dim">{when_html(event["when"])}</span>' if event["when"] else "")
                     + '</span>')

    if results:
        parts.append(head(f"{results_title} results"))
        for fight in results:
            w = fighter_name(ctx, fight["winner_id"], fight["winner_name"])
            l = fighter_name(ctx, fight["loser_id"], fight["loser_name"])
            joiner = "vs" if fight["method"] in ("DRAW", "NC") else "def."
            belt = (f'<span class="tick-belt">{"INTERIM" if row_get(fight, "interim") else "TITLE"}</span>'
                    if fight["title_fight"] else "")
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
        # Main Event first, with each fight's label and card section.
        if event.get("main_first"):
            ordered = []
            for (fa, fb, fw), spot in zip(event["bouts"], event["spots"]):
                label = (spot["slot"] or "").upper().replace("CO-MAIN EVENT", "CO-MAIN") or f"FIGHT {spot['number']}"
                ordered.append((fa, fb, fw, label, ctx.CARD_SECTION_NAMES[spot["section"]].upper()))
        else:
            total = len(event["bouts"])
            ordered = [
                (fa, fb, fw, "MAIN EVENT" if n == 1 else "CO-MAIN" if n == 2 else f"FIGHT {total - n + 1}", None)
                for n, (fa, fb, fw) in enumerate(reversed(event["bouts"]), start=1)
            ]
        a, b, weight = ordered[0][:3]
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

        if event.get("kind") == "fight_night" and event["when"]:
            when_line = f"FIGHT NIGHT · FIGHTS DUE BY {when_html(event['when'], True)}"
        else:
            when_line = f"NEXT EVENT · {when_html(event['when'], True)}" if event["when"] else "NEXT EVENT"
        detail = " · ".join(part for part in [
            f"{weight} championship" if title_fight and weight else (weight or ""),
            "main event",
        ] if part)

        card_rows = ""
        current_section = None
        for number, (fa, fb, fw, label, section) in enumerate(ordered, start=1):
            if section and section != current_section:
                current_section = section
                card_rows += f'<li class="card-sec rv">{esc(section)}</li>'
            ra = rank_label(rank_in(ctx, fa, fw))
            rb = rank_label(rank_in(ctx, fb, fw))
            card_rows += f"""
          <li class="row card-row {stagger(number)}">
            <span class="card-slot{' gold' if label == 'MAIN EVENT' else ''}">{label}</span>
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
          <span class="eyebrow">{when_line}</span>
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
            f'<span class="muted">{("Closes " + when_html(closes)) if closes else "Open now"}</span></li>'
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

    # ---------- spotlight (bought in the /shop) ----------
    spotlight_html = ""
    spot_fn = getattr(ctx, "active_spotlights", None)
    seen_spots = []
    for row in (spot_fn(ctx.gid) if spot_fn else []):
        if row["user_id"] in seen_spots:
            continue
        seen_spots.append(row["user_id"])
    if seen_spots:
        cards = ""
        for i, uid in enumerate(seen_spots[:3]):
            name = fighter_name(ctx, uid)
            stats = stats_for(ctx, uid, cache)
            cos = ctx.get_cosmetics(ctx.gid, uid) if getattr(ctx, "get_cosmetics", None) else {}
            sub = " · ".join(x for x in (f"“{cos['nickname']}”" if cos.get("nickname") else "", cos.get("badge") or "") if x)
            cards += f"""
        <a href="{fighter_url(uid)}" class="spot {stagger(i)}">
          {avatar_html(ctx, uid, name, 'av av-xl')}
          <span class="spot-meta"><span class="eyebrow">SPOTLIGHT</span><span class="d spot-name">{esc(name)}</span>{f'<span class="muted">{esc(sub)}</span>' if sub else ''}</span>
          <span class="d spot-rec">{esc(ctx.stats_record(stats) if stats else '')}</span>
        </a>"""
        spotlight_html = f"""
    <section class="block" aria-label="Spotlight">
      <div style="display:flex;flex-direction:column;gap:18px">{cards}</div>
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
<div id="homepanel"></div>
<div class="wrap">
  <section id="numbers" class="nums" aria-label="PFO in numbers">{numbers_html}</section>
  {event_html}
  {home_replay(ctx)}
  {spotlight_html}
  {champs_html}
  {results_html}
  {titles_html}
</div>
{join}"""

    return page(ctx, "Home", "home", body, solid_nav=False)


def weight_name(ctx, user_id, weight, name):
    """Members with two fighters ("Strickland / Topuria") show the one for that weight."""
    if not user_id or not weight or "/" not in (name or ""):
        return name
    try:
        return ctx.name_for_weight(ctx.gid, user_id, weight, name) or name
    except Exception:
        return name


def result_row(ctx, fight, i=0, show_weight=True):
    w = weight_name(ctx, fight["winner_id"], fight["division"], fighter_name(ctx, fight["winner_id"], fight["winner_name"]))
    l = weight_name(ctx, fight["loser_id"], fight["division"], fighter_name(ctx, fight["loser_id"], fight["loser_name"]))
    joiner = "VS" if fight["method"] in ("DRAW", "NC") else "DEF."
    if fight["title_fight"] and row_get(fight, "interim"):
        belt = '<span class="belt" title="Interim title fight">INTERIM</span>'
    else:
        belt = '<span class="belt" title="Title fight">TITLE</span>' if fight["title_fight"] else ""
    weight = f'<span class="res-w">{esc((fight["division"] or "").upper())}</span>' if show_weight else ""
    award_fn = getattr(ctx, "awards_for_fight", None)
    awards = ""
    if award_fn:
        kinds = []
        for row in award_fn(ctx.gid, fight["id"]):
            if row["award"] not in kinds:
                kinds.append(row["award"])
        titles = {"fotn": "Fight of the Night", "potn": "Performance of the Night", "kotn": "Knockout of the Night"}
        awards = "".join(f'<span class="award award-{k}" title="{titles[k]}">{k.upper()}</span>' for k in kinds)
    return f"""
          <div class="row res {stagger(i)}" data-fight="{fight['id']}">
            <span class="d res-who">{name_link(ctx, fight['winner_id'], w)}<span class="def">{joiner}</span>{name_link(ctx, fight['loser_id'], l, 'loser')}</span>
            {belt}{awards}
            <span class="res-m {method_class(fight['method'])}">{esc(method_short(ctx, fight))}</span>
            {weight}
          </div>"""


# ============================================================
# RANKINGS
# ============================================================

def render_rankings(ctx, selected=None):
    cache = {}
    weights = [ctx.P4P_WEIGHT] + [w for w in ctx.WEIGHTS if w != ctx.P4P_WEIGHT]
    names = {ctx.P4P_WEIGHT: "Pound for Pound"}

    # Default tab: the division asked for, else the first one with anyone ranked.
    by_weight = {w: ctx.get_division_rankings(ctx.gid, w) for w in weights}
    if selected not in by_weight:
        filled = [w for w in weights if w != ctx.P4P_WEIGHT and by_weight[w]]
        if by_weight[ctx.P4P_WEIGHT]:
            selected = ctx.P4P_WEIGHT          # open on pound for pound
        else:
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
        label = "Pound for Pound" if weight == ctx.P4P_WEIGHT else weight
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
      <a href="{fighter_url(uid)}" class="feature rv" data-rank-uid="{uid}" data-rank-w="{esc(weight)}" data-rank="0">
        <span class="eyebrow">{esc(weight.upper())} CHAMPION{(" · " + champ_tag_for(ctx, uid)) if champ_tag_for(ctx, uid) else ""}</span>
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
          <li class="row rank-row {stagger(i, 3)}" data-rank-uid="{uid}" data-rank-w="{esc(weight)}" data-rank="{row['rank']}">
            <span class="d rank-n{' gold' if row['rank'] == 1 else ''}">{row['rank']}</span>
            {avatar_html(ctx, uid, name, 'av av-sm')}
            {name_link(ctx, uid, name, 'd rank-name')}
            <span class="muted tab">{esc(ctx.stats_record(stats) if stats else '')}</span>
            {move(uid, row['rank'], row)}
          </li>"""

        if not list_rows:
            list_rows = '<li class="empty">Nobody ranked here yet.</li>'

        # Interim champion, BMF and WMF (set with /belt in Discord).
        belts_html = ""
        unranked_html = ""

        if weight != ctx.P4P_WEIGHT:
            belt_fn = getattr(ctx, "get_division_belts", None)
            belts = belt_fn(ctx.gid, weight) if belt_fn else {}
            labels = [("interim", "INTERIM CHAMPION"), ("bmf", "BMF · BADDEST MOTHERF*CKER"), ("wmf", "WMF · WORST MOTHERF*CKER")]

            for i, (key, label) in enumerate(labels):
                if key not in belts:
                    continue
                uid = belts[key]
                name = fighter_name(ctx, uid)
                stats = stats_for(ctx, uid, cache)
                belts_html += f"""
        <a href="{fighter_url(uid)}" class="belt belt-{key} {stagger(i, 3)}">
          {avatar_html(ctx, uid, name, 'av av-sm')}
          <span class="belt-text"><span class="belt-label">{esc(label)}</span><span class="d belt-name">{esc(name)}</span></span>
          <span class="muted">{esc(ctx.stats_record(stats) if stats else '')}</span>
        </a>"""

            # Fighters with the division's role who aren't on the board.
            unranked_fn = getattr(ctx, "unranked_in_division", None)
            unranked = unranked_fn(ctx.guild, weight) if unranked_fn else []
            unranked_rows = ""

            for i, uid in enumerate(unranked):
                name = fighter_name(ctx, uid)
                stats = stats_for(ctx, uid, cache)
                unranked_rows += f"""
          <li class="row rank-row {stagger(i, 3)}" data-rank-uid="{uid}" data-rank-w="{esc(weight)}" data-rank="">
            <span class="d rank-n" aria-hidden="true">–</span>
            {avatar_html(ctx, uid, name, 'av av-sm')}
            {name_link(ctx, uid, name, 'd rank-name')}
            <span class="muted tab">{esc(ctx.stats_record(stats) if stats else '')}</span>
          </li>"""

            if unranked_rows:
                unranked_html = f"""
        <div class="unranked">
          <div class="list-head rv"><span>UNRANKED</span><span>RECORD</span></div>
          <ol class="list">{unranked_rows}</ol>
        </div>"""

        if belts_html:
            champ_html = f'<div class="champ-col">{champ_html}<div class="belts">{belts_html}</div></div>'

        note = (f"Movement since the last rankings update on {uk_time(since).day} {uk_time(since):%B}."
                if since else "Arrows show the latest move.")

        panels += f"""
    <div class="rank-panel{' p4p' if weight == ctx.P4P_WEIGHT else ''}" role="tabpanel" data-panel="{esc(weight)}" aria-label="{esc(names.get(weight, weight))}"{'' if is_sel else ' hidden'}>
      {champ_html}
      <section class="rank-list" aria-label="{esc(names.get(weight, weight))} contenders">
        <div class="list-head rv"><span>{'RANKED' if weight == ctx.P4P_WEIGHT else 'CONTENDER'}</span><span>RECORD · MOVE</span></div>
        <ol class="list">{list_rows}</ol>
        <p class="note rv">{esc(note)}</p>{unranked_html}
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
        multi = champ_tag_for(ctx, e["id"])
        if multi:
            badge = f'<span class="badge badge-gold badge-double">{multi}</span>'
        elif rank == 0:
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
    prospects = [e for e in current if getattr(ctx, "is_prospect", None) and ctx.is_prospect(e["stats"])
                 and not any(rank == 0 for w, rank in e["ranks"].items() if w != ctx.P4P_WEIGHT)]
    prospect_html = ""
    if prospects:
        prospect_html = f"""
  <section class="block prospects" aria-labelledby="pwatch">
    <div class="head-t rv"><span class="eyebrow">🌱 UNBEATEN · 3 FIGHTS OR FEWER</span><h2 id="pwatch" class="d h-m">PROSPECT WATCH</h2></div>
    <div class="fgrid">{''.join(card(e, i) for i, e in enumerate(prospects[:8]))}</div>
  </section>"""
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
      <a class="btn btn-ghost btn-sm" href="/compare">Head to head ⇄</a>
      <div class="fchips" role="group" aria-label="Filter by division">{chips}</div>
    </div>"""

    body = page_hero(f"{len(current)} ACTIVE FIGHTERS", "FIGHTERS", "PFO", extra) + f"""
<div class="wrap">
  {prospect_html}
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
    multi = champ_tag_for(ctx, user_id)
    if multi:
        badge_bits.append(multi)
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
    try:
        if ctx.is_prospect(stats) and not any(r == 0 for w, r in ranks.items() if w != ctx.P4P_WEIGHT):
            badge_bits.append("🌱 PROSPECT")
    except Exception:
        pass
    hof_fn = getattr(ctx, "hof_entry", None)
    if hof_fn and hof_fn(ctx.gid, user_id, name)[0]:
        badge_bits.insert(0, "HALL OF FAME")
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

    # Their team
    team_html = ""
    try:
        team = ctx.team_of(ctx.gid, user_id)
        if team:
            team_html = (f'<a class="fteam l-up" href="/team/{team["id"]}"><span class="fformer-l">TEAM</span>'
                         f'{team_name_html(team)}{" · OWNER" if ctx.is_team_owner(team, user_id) else ""}</a>')
    except Exception:
        team_html = ""

    # Former UFC 6 fighters they used
    former_html = ""
    try:
        former = ctx.former_fighters(ctx.gid, user_id, member.display_name if member else name)
        if former:
            former_html = ('<p class="fformer l-up"><span class="fformer-l">FORMER FIGHTERS</span>'
                           + "".join(f'<span class="fformer-n">{esc(ctx.short_fighter(n))}</span>' for n in former[-8:]) + '</p>')
    except Exception:
        former_html = ""

    # Shop items: nickname, badge, quote and website style
    cos_fn = getattr(ctx, "get_cosmetics", None)
    cosmetics = cos_fn(ctx.gid, user_id) if cos_fn else {}
    nick_html = f'<span class="fnick l-up">“{esc(cosmetics["nickname"])}”</span>' if cosmetics.get("nickname") else ""
    quote_html = f'<blockquote class="fquote l-up">“{esc(cosmetics["quote"])}”</blockquote>' if cosmetics.get("quote") else ""
    badge_html = f'<span class="fshopbadge">{esc(cosmetics["badge"].upper())}</span>' if cosmetics.get("badge") else ""
    style_cls = f' fstyle-{esc(cosmetics["style"])}' if cosmetics.get("style") else ""

    hero = f"""
<section class="fhero{style_cls}" aria-label="{esc(name)}">
  <div class="fhero-mark s-mark" aria-hidden="true">{esc(last or first)}</div>
  <div class="wrap fhero-in s-fhero">
    <div class="fhero-main">
      <div class="fhero-top l-badge">{avatar_html(ctx, user_id, name, 'av av-hero')}<span class="fbadge">{esc(badge)}</span>{badge_html}</div>
      <h1 class="d fhero-name l-title">{esc(first)}{'<br>' + esc(last) if last else ''}</h1>
      {nick_html}
      {handle_html}
      {team_html}
      {former_html}
      {quote_html}
      <p class="muted l-up">{esc(' · '.join(meta))}</p>
      {leaderboard_badges(ctx, user_id)}
      <div class="btns l-up"><button class="btn btn-ghost btn-sm" type="button" data-follow="{user_id}" hidden>★ FOLLOW</button><a class="btn btn-ghost btn-sm" href="/compare?a={user_id}">Compare ⇄</a></div>
      <div class="staffbar" data-staff-fighter="{user_id}" hidden></div>
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
    rival_fn_rows = getattr(ctx, "rivalries_for", None)
    rival_ids_for_rows = {r["opponent_id"] for r in rival_fn_rows(ctx.gid, user_id)} if rival_fn_rows else set()
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
        method = method_short(ctx, fight) + ((" · INTERIM" if row_get(fight, "interim") else " · TITLE") if fight["title_fight"] else "")
        if rival_ids_for_rows and opp_id in rival_ids_for_rows:
            method += " · RIVALRY"
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
            opp = fighter_name(ctx, won["loser_id"], won["loser_name"]) if won else None
            days = ctx.reign_days(reign)
            current = reign["end"] is None
            d = reign["defences"]
            reign_tiles += f"""
        <div class="reign{' reign-now' if current else ''} rv2">
          <span class="reign-tag">{esc(weight.upper())} · {'CURRENT' if current else 'FORMER'}</span>
          <span class="d reign-t">{f"Won vs {esc(opp)}" if opp else "Became champion"}</span>
          <span class="muted">{esc(short_day(reign['start']))} · {d} defence{'s' if d != 1 else ''}{f' · {days} days' if days is not None else ''}</span>
        </div>"""

    # Rivalries and awards
    side_blocks = ""
    rival_fn = getattr(ctx, "rivalries_for", None)
    rival_tiles = ""
    for rival in (rival_fn(ctx.gid, user_id) if rival_fn else [])[:3]:
        opp = fighter_name(ctx, rival["opponent_id"], rival["opponent_name"])
        rec = f"{rival['wins']}-{rival['losses']}" + (f"-{rival['draws']}" if rival["draws"] else "")
        state = "LEADS" if rival["wins"] > rival["losses"] else "TRAILS" if rival["wins"] < rival["losses"] else "LEVEL"
        rival_tiles += f"""
        <div class="reign rv2">
          <span class="reign-tag">RIVALRY · {state}</span>
          <span class="d reign-t">vs {name_link(ctx, rival['opponent_id'], opp)}</span>
          <span class="muted">{esc(rec)} · {rival['fights']} fights · last: {esc(rival['last']['event'] or nice_day(fight_day(rival['last'])))}</span>
        </div>"""
    if rival_tiles:
        side_blocks += f'<h2 id="rivals" class="d h-m rv">RIVALRIES</h2>{rival_tiles}'

    award_fn = getattr(ctx, "awards_for_member", None)
    award_rows = award_fn(ctx.gid, user_id) if award_fn else []
    if award_rows:
        counts = {"fotn": 0, "potn": 0, "kotn": 0}
        for row in award_rows:
            counts[row["award"]] += 1
        labels = {"fotn": "FIGHT OF THE NIGHT", "potn": "PERFORMANCE OF THE NIGHT", "kotn": "KNOCKOUT OF THE NIGHT"}
        award_tiles = "".join(
            f'<div class="reign rv2"><span class="reign-tag">{labels[k]}</span><span class="d reign-t">×{v}</span></div>'
            for k, v in counts.items() if v
        )
        side_blocks += f'<h2 id="awards" class="d h-m rv">AWARDS</h2>{award_tiles}'

    aside = ""
    if reign_tiles or side_blocks:
        aside = f"""
    <aside class="reigns" aria-labelledby="{'titles' if reign_tiles else 'rivals'}">
      {f'<h2 id="titles" class="d h-m rv">TITLE REIGNS</h2>{reign_tiles}' if reign_tiles else ''}
      {side_blocks}
    </aside>"""

    like_html = fights_like_block(ctx, user_id, stats) if member else ""
    has_card = bool(member) and bool(getattr(ctx, "card_eligible", lambda m: bool(fights))(member))
    card_html = ""
    card_info_now = None
    if has_card:
        try:
            card_info_now = ctx.fighter_card_data(ctx.guild, user_id, member)
        except Exception:
            card_info_now = None
        card_html = f"""
  <section class="block fcardsec" aria-label="Fighter card">
    <div class="fcard-in">{fighter_card_img(user_id, name, "cardimg cardimg-lg", lazy=True, info=card_info_now)}
      <div class="fcard-t"><span class="eyebrow">FIGHTER CARD</span><h2 class="d h-m">{esc(name.upper())}</h2>
        <p class="muted">The tier and numbers update as they fight. Pull their card from packs in the shop.</p>
        <div class="btns"><a class="btn btn-gold btn-sm" href="/cardimg/fighter/{user_id}.png?download=1" download="{esc(re.sub(r'[^A-Za-z0-9]+', '-', name).strip('-').lower() or 'pfo')}-card.png">DOWNLOAD</a>
        <a class="btn btn-ghost btn-sm" href="/cards">ALL CARDS</a></div></div></div>
  </section>"""
    stats_panel = numbers or '<section class="block"><p class="empty">No fight stats yet. Staff add them with /addstrikes after a fight.</p></section>'
    tabs = [("overview", "OVERVIEW"), ("stats", "STATS"), ("career", "CAREER")] + ([("card", "CARD")] if has_card else [])
    tab_nav = ('<nav class="ftabs" data-ftabs role="tablist" aria-label="Fighter sections" hidden>'
               + "".join(f'<button type="button" role="tab" data-ftab="{k}">{label}</button>' for k, label in tabs) + '</nav>')
    body = hero + f"""
<div class="wrap">
  {tab_nav}
  <div data-fpanel="overview">
  <section class="tiles" aria-label="Career numbers">{tiles_html}</section>
  {like_html}
  {how}
  </div>
  <div data-fpanel="stats">{stats_panel}</div>
  <div data-fpanel="career"><div class="block bt hist-wrap">{history}{aside}</div></div>
  {f'<div data-fpanel="card">{card_html}</div>' if has_card else ''}
</div>"""

    description = f"{name}: {ctx.stats_record(stats)} in PFO." + (f" {badge.title()}." if badge_bits else "")
    return page(ctx, name, "fighters", body, description)


# ============================================================
# RESULTS
# ============================================================

def render_results(ctx, page_no=1, filters=None):
    if filters and any(filters.values()):
        return render_filtered_results(ctx, filters, page_no)
    events = ctx.recent_events(ctx.gid)
    pages = max(1, -(-len(events) // RESULTS_PER_PAGE))
    page_no = max(1, min(page_no, pages))
    shown = events[(page_no - 1) * RESULTS_PER_PAGE: page_no * RESULTS_PER_PAGE]

    blocks = ""
    outcomes = ctx.all_title_outcomes(ctx.gid)
    for event in shown:
        fights = ctx.event_fights(ctx.gid, event)
        if not fights:
            continue
        layout, title, notes = ctx.load_recap_layout(ctx.gid, event, fights)
        grouped = ctx.ordered_sections(layout)
        by_id = {f["id"]: f for f in fights}

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
      <div class="head-t"><span class="eyebrow">{esc(nice_day(day).upper())}</span><h2 class="d h-l"><a class="ev-link" href="{event_url(event)}">{esc((title or event).upper())}</a></h2></div>
      <span class="muted ev-sum">{count} fight{'s' if count != 1 else ''} · {finishes} finish{'es' if finishes != 1 else ''} · <a href="{event_url(event)}">Full event →</a></span>
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

    body = page_hero("EVERY RESULT, STRAIGHT FROM THE BOT", "RESULTS", "DEF.", results_filter_form(ctx, {})) + f'<div class="wrap">{blocks}{pager}</div>'
    return page(ctx, "Results", "results", body, "Every PFO result, event by event.")


# ============================================================
# EVENT PAGES  /event/NAME
# ============================================================

def event_url(event):
    return "/event/" + quote(" ".join((event or "").split()), safe="")


def find_event(ctx, slug):
    from urllib.parse import unquote
    wanted = ctx.event_key(unquote(slug or ""))
    for name in ctx.recent_events(ctx.gid):
        if ctx.event_key(name) == wanted:
            return name
    return None


def fight_odds(ctx, fight):
    """(favourite's id, favourite odds, winner odds, upset?) from the sportsbook, or None."""
    try:
        rows = ctx.q("SELECT * FROM bet_fights WHERE fight_id = ? ORDER BY market_id DESC LIMIT 1", (fight["id"],))
    except Exception:
        return None
    if not rows or not fight["winner_id"] or fight["method"] in ("DRAW", "NC"):
        return None
    bout = rows[0]
    odds = ctx.live_odds(bout)
    side = "a" if bout["fighter_a"] == fight["winner_id"] else "b"
    other = "b" if side == "a" else "a"
    fav = bout["fighter_a"] if odds["a"] <= odds["b"] else bout["fighter_b"]
    return fav, min(odds["a"], odds["b"]), odds[side], odds[side] > odds[other]


def chip(label, value, cls=""):
    if len(re.sub(r"<[^>]+>", "", str(value))) > 14:
        cls += " evchip-sm"
    return f'<div class="evchip {cls}"><span class="evchip-l">{esc(label)}</span><span class="d evchip-v">{value}</span></div>'


def render_event(ctx, slug):
    name = find_event(ctx, slug)
    if not name:
        return None
    fights = ctx.event_fights(ctx.gid, name)
    if not fights:
        return None

    layout, title, notes = ctx.load_recap_layout(ctx.gid, name, fights)
    grouped = ctx.ordered_sections(layout)
    by_id = {f["id"]: f for f in fights}
    outcomes = ctx.all_title_outcomes(ctx.gid)
    display = title or name

    finishes = sum(1 for f in fights if f["method"] in ("KO/TKO", "SUB"))
    titles = sum(1 for f in fights if f["title_fight"])
    r1 = sum(1 for f in fights if f["method"] in ("KO/TKO", "SUB") and row_get(f, "round") == 1)
    upsets = 0

    sections = ""
    for section in ctx.RECAP_SECTIONS:
        ids = grouped.get(section, [])
        if not ids:
            continue
        rows = ""
        for i, fid in enumerate(ids):
            fight = by_id[fid]
            rows += result_row(ctx, fight, i)
            extra = []
            outcome = outcomes.get(fid)
            if outcome in ("new", "first"):
                extra.append(f'<span class="gold">NEW {esc((fight["division"] or "").upper())} CHAMPION</span>')
            elif outcome == "defence":
                extra.append(f'Retains the {esc(fight["division"] or "")} title')
            odds = fight_odds(ctx, fight)
            if odds:
                fav, fav_odds, win_odds, upset = odds
                upsets += upset
                if upset:
                    extra.append(f'<span class="tag-upset">UPSET</span> won at <b>{win_odds:.2f}</b>')
                else:
                    extra.append(f'Favourite won at {win_odds:.2f}')
            if extra:
                rows += f'<div class="res-note">{" · ".join(extra)}</div>'
        sections += f'<h3 class="sec-h rv">{esc(section.upper())}</h3><div class="list">{rows}</div>'

    # Awards
    award_fn = getattr(ctx, "awards_for_fight", None)
    award_chips = ""
    if award_fn:
        names = {"fotn": "FIGHT OF THE NIGHT", "potn": "PERFORMANCE OF THE NIGHT", "kotn": "KNOCKOUT OF THE NIGHT"}
        for fight in fights:
            for row in award_fn(ctx.gid, fight["id"]):
                if row["award"] == "fotn":
                    who = f'{esc(fighter_name(ctx, fight["winner_id"], fight["winner_name"]))} vs {esc(fighter_name(ctx, fight["loser_id"], fight["loser_name"]))}'
                else:
                    uid = row_get(row, "user_id") or fight["winner_id"]
                    who = name_link(ctx, uid, fighter_name(ctx, uid))
                award_chips += f'<div class="awcard rv2"><span class="award award-{row["award"]}">{row["award"].upper()}</span><span class="awcard-l">{names[row["award"]]}</span><span class="d awcard-n">{who}</span></div>'

    # Fantasy
    fantasy_html = ""
    try:
        round_id = ctx.round_for_fights(ctx.gid, [f["id"] for f in fights])
        award_rows = ctx.q("SELECT * FROM fantasy_round_awards WHERE round_id = ?", (round_id,)) if round_id else []
        if award_rows:
            import json as _json
            winners = _json.loads(award_rows[0]["winners"])
            players, round_list, _ = ctx.fantasy_round_scores(ctx.gid, round_id)
            team, total = ctx.team_of_the_event(ctx.gid, round_list, ctx.round_prices(round_id))
            left = ""
            if winners:
                left = (f'<div class="fan-box rv"><span class="eyebrow-grey">TOP SCORER</span>'
                        f'<span class="d fan-big">{" & ".join(esc(fighter_name(ctx, w)) for w in winners)}</span>'
                        f'<span class="muted">{award_rows[0]["points"]} points · won {award_rows[0]["amount"]:,} coins</span></div>')
            if team:
                picks = "".join(f'<li><span>{"★ " if i == 0 else ""}{name_link(ctx, fid, fighter_name(ctx, fid))}</span><span class="muted tab">{pts} pts · {price}</span></li>'
                                for i, (fid, pts, price) in enumerate(team))
                left += (f'<div class="fan-box rv2"><span class="eyebrow-grey">TEAM OF THE EVENT</span><ul class="fan-team">{picks}</ul>'
                         f'<span class="muted">{total} pts with ★ captain</span></div>')
            if left:
                fantasy_html = f'<section class="block bt" aria-labelledby="fan"><h2 id="fan" class="d h-m rv">FANTASY</h2><div class="fan-grid">{left}</div></section>'
    except Exception:
        fantasy_html = ""

    # Older / newer event
    events = ctx.recent_events(ctx.gid)
    index = next((i for i, e in enumerate(events) if ctx.event_key(e) == ctx.event_key(name)), None)
    newer = events[index - 1] if index not in (None, 0) else None
    older = events[index + 1] if index is not None and index + 1 < len(events) else None
    pager = (f'<nav class="pager" aria-label="Events">'
             + (f'<a class="btn btn-ghost" href="{event_url(newer)}">← {esc(newer.upper())}</a>' if newer else '<span></span>')
             + '<a class="btn btn-ghost" href="/results">ALL RESULTS</a>'
             + (f'<a class="btn btn-ghost" href="{event_url(older)}">{esc(older.upper())} →</a>' if older else '<span></span>')
             + '</nav>')

    strip = "".join([
        chip("FIGHTS", len(fights)),
        chip("FINISHES", finishes, "gold"),
        chip("ROUND 1 FINISHES", r1),
        chip("TITLE FIGHTS", titles),
    ] + ([chip("UPSETS", upsets, "red")] if upsets else []))

    day = fight_day(fights[-1])
    body = page_hero(nice_day(day).upper() or "PFO EVENT", display.upper(), "PFO",
                     f'<div class="evchips l-up">{strip}</div>') + f"""
<div class="wrap">
  {f'<p class="ev-notes ev-notes-lg rv">{esc(notes)}</p>' if notes else ''}
  {replay_player(ctx, ctx.vod_for_event(ctx.gid, name), "REPLAY", show_results=False) if getattr(ctx, "vod_for_event", None) else ""}
  <section class="block ev-card" aria-label="Results">{sections}</section>
  {f'<section class="block bt" aria-labelledby="aw"><h2 id="aw" class="d h-m rv">AWARDS</h2><div class="awgrid">{award_chips}</div></section>' if award_chips else ''}
  {fantasy_html}
  {pager}
</div>"""
    return page(ctx, display, "results", body, f"{display}: every result, the awards and the upsets.")


# ============================================================
# LIVE CARD  /live
# ============================================================

def card_cancelled(ctx, notice):
    """Fights called off on this card with /cancelfight."""
    by_card = getattr(ctx, "notice_cancelled", None)
    if by_card:
        try:
            return by_card(notice)
        except Exception:
            pass
    fn = getattr(ctx, "cancelled_pairs", None)
    if not fn or not row_get(notice, "event"):
        return set()
    try:
        return fn(ctx.gid, notice["event"])
    except Exception:
        return set()


def live_notice(ctx):
    """The card on now or next: the newest DMed card from the last 3 days that isn't finished
    (or finished less than 6 hours ago)."""
    now = time.time()
    fights_fn = getattr(ctx, "notice_fights", None)
    for notice in ctx.q("SELECT * FROM card_notices WHERE guild_id = ? ORDER BY id DESC LIMIT 3", (ctx.gid,)):
        try:
            created = datetime.fromisoformat(str(notice["created_at"]).replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        if now - created > 4 * 86400:
            continue
        if not fights_fn:
            return notice
        logged, total = fights_fn(ctx.gid, notice)
        if total == 0:
            continue
        if len(logged) < total:
            return notice
        last = max((datetime.fromisoformat(str(f["created_at"]).replace("Z", "+00:00")).timestamp() for f in logged), default=0)
        if now - last < 6 * 3600:
            return notice
    return None


def spot_label(spot):
    """'MAIN EVENT', 'CO-MAIN EVENT', 'MAIN CARD', 'PRELIMS' or 'EARLY PRELIMS'."""
    if isinstance(spot, dict):
        if spot.get("slot"):
            return str(spot["slot"]).upper()
        return {"main": "MAIN CARD", "prelims": "PRELIMS", "early": "EARLY PRELIMS"}.get(spot.get("section"), "MAIN CARD")
    text = str(spot or "").lower()
    if "co-main" in text or "co_main" in text:
        return "CO-MAIN EVENT"
    if "main event" in text or "main_event" in text:
        return "MAIN EVENT"
    if "early" in text:
        return "EARLY PRELIMS"
    if "prelim" in text:
        return "PRELIMS"
    return "MAIN CARD"


def live_side(ctx, user_id, fight, cache):
    name = fighter_name(ctx, user_id)
    stats = stats_for(ctx, user_id, cache)
    record = ctx.stats_record(stats) if stats else ""
    cls = ""
    if fight:
        if fight["method"] in ("DRAW", "NC"):
            cls = " lv-draw"
        else:
            cls = " lv-win" if fight["winner_id"] == user_id else " lv-loss"
    return (f'<div class="lv-side{cls}">{avatar_html(ctx, user_id, name, "av")}'
            f'<span class="lv-n"><span class="d">{name_link(ctx, user_id, name)}</span><span class="muted tab">{esc(record)}</span></span></div>')


def render_live(ctx):
    notice = live_notice(ctx)

    if not notice:
        events = ctx.recent_events(ctx.gid)
        last = (f'<a class="btn btn-gold" href="{event_url(events[0])}">LAST EVENT: {esc(events[0].upper())}</a>' if events else "")
        body = page_hero("NOTHING ON RIGHT NOW", "LIVE", "LIVE") + f"""
<div class="wrap">{twitch_player(ctx) or home_replay(ctx)}<section class="block live-empty rv">
  <p class="muted">There's no PFO card on right now. When a card is sent out, it shows here and results appear as they're logged.</p>
  <div class="btns">{last}<a class="btn btn-ghost" href="/results">ALL RESULTS</a></div>
</section></div>"""
        return page(ctx, "Live", "live", body, "Live PFO results.")

    name = row_get(notice, "event") or ("Live Card" if notice["kind"] == "live_card" else "Fight Night")
    layout = ctx.notice_layout(notice)
    called_off = card_cancelled(ctx, notice)
    logged, total = ctx.notice_fights(ctx.gid, notice) if getattr(ctx, "notice_fights", None) else ([], len(layout))
    by_pair = {}
    for f in logged:
        by_pair.setdefault(frozenset((f["winner_id"], f["loser_id"])), f)     # the first fight logged for each pair

    raw = str(row_get(notice, "event_date" if notice["kind"] == "live_card" else "deadline", ""))
    when = int(raw) if raw.isdigit() else None
    now = time.time()
    on_card = [x for x in layout if frozenset((x[0], x[1])) not in called_off]
    done = len(by_pair) >= len(on_card) and on_card
    started = bool(by_pair) or (when is not None and when <= now)
    status = "FINISHED" if done else ("LIVE NOW" if started else "UP NEXT")

    # The next fight to happen: the lowest one on the card without a result (cards run bottom up).
    pending = [i for i, (a, b, _) in enumerate(layout) if frozenset((a, b)) not in by_pair and frozenset((a, b)) not in called_off]
    up_next = pending[-1] if pending and started and not done else None

    cache = {}
    rows = ""
    last_spot = None
    for i, (a, b, spot) in enumerate(layout):
        label = spot_label(spot)
        group = "MAIN CARD" if label in ("MAIN EVENT", "CO-MAIN EVENT") else label
        if group != last_spot:
            rows += f'<h3 class="sec-h rv">{esc(group)}</h3>'
            last_spot = group
        fight = by_pair.get(frozenset((a, b)))
        cancelled = not fight and frozenset((a, b)) in called_off
        if cancelled:
            middle = '<span class="lv-cancel">CANCELLED</span>'
        elif fight:
            middle = f'<span class="lv-res {method_class(fight["method"])}">{esc(method_short(ctx, fight))}</span>'
        elif i == up_next:
            middle = '<span class="lv-next"><span class="dot dot-red" aria-hidden="true"></span>UP NEXT</span>'
        else:
            middle = '<span class="lv-vs d">VS</span>'
        tag = f'<span class="lv-spot">{esc(label)}</span>' if label in ("MAIN EVENT", "CO-MAIN EVENT") else ""
        rows += f"""
  <div class="lvbout{' lvbout-done' if fight else ''}{' lvbout-cancel' if cancelled else ''}{' lvbout-next' if i == up_next else ''} {stagger(i)}">
    {tag}
    {live_side(ctx, a, fight, cache)}
    <div class="lv-mid">{middle}</div>
    {live_side(ctx, b, fight, cache).replace('lv-side', 'lv-side lv-right', 1)}
  </div>"""

    extra = (f'<div class="evchips l-up">{chip("STATUS", esc(status), "red" if status == "LIVE NOW" else "gold")}'
             f'{chip("RESULTS IN", f"{len(by_pair)}/{len(on_card)}")}'
             + (chip("CANCELLED", str(len(layout) - len(on_card)), "red") if len(on_card) < len(layout) else "")
             + (chip("STARTS" if not started else "STARTED", when_html(when)) if when else "") + '</div>')
    refresh = '' if done else '<script>setTimeout(function(){location.reload()},60000)</script>'
    body = page_hero(status, name.upper(), "LIVE", extra) + f"""
<div class="wrap">
  {twitch_player(ctx)}
  <section class="block live-card" aria-label="The card">{rows}</section>
  {home_replay(ctx)}
  <p class="note">{'This page updates by itself every minute.' if not done else f'All results are in. <a href="{event_url(name)}">See the full event →</a>'}</p>
</div>{refresh}"""
    return page(ctx, f"{name} · Live", "live", body, f"Live results from {name}.")


# ============================================================
# HEAD TO HEAD  /compare?a=ID&b=ID
# ============================================================

def compare_options(ctx, selected):
    people = sorted(((fighter_name(ctx, uid), uid) for uid in all_fighter_ids(ctx)), key=lambda item: item[0].lower())
    return "".join(f'<option value="{uid}"{" selected" if uid == selected else ""}>{esc(n)}</option>' for n, uid in people)


def compare_numbers(ctx, user_id, totals):
    stats = stats_for(ctx, user_id) or {}
    fights = stats.get("wins", 0) + stats.get("losses", 0) + stats.get("draws", 0)
    ranks = [r["rank"] for r in ctx.get_user_rankings(ctx.gid, user_id) if r["weight"] != ctx.P4P_WEIGHT]
    total = totals.get(user_id) or {}
    wins_by = stats.get("wins_by", {})

    def acc(a, b):
        return round(100 * a / b) if b else None

    return {
        "record": ctx.stats_record(stats) if stats else "0-0-0",
        "Fights": fights,
        "Win rate": round(100 * stats.get("wins", 0) / fights) if fights else None,
        "Finish rate": stats.get("finish_rate"),
        "KO / TKO wins": wins_by.get("KO/TKO", 0),
        "Submission wins": wins_by.get("SUB", 0),
        "Decision wins": wins_by.get("DEC", 0),
        "Title fight wins": stats.get("title_wins", 0),
        "Streak": stats.get("streak") or "–",
        "Best rank": min(ranks) if ranks else None,
        "Sig. strike accuracy": acc(total.get("sig_landed", 0), total.get("sig_thrown", 0)) if total else None,
        "Takedowns": total.get("td_landed") if total else None,
        "Knockdowns": total.get("knockdowns") if total else None,
    }


def render_compare(ctx, a=None, b=None):
    ids = all_fighter_ids(ctx)
    a = a if a in ids else None
    b = b if b in ids else None

    form = f"""
<form class="cmp-form rv" method="get" action="/compare">
  <label><span class="eyebrow-grey">FIGHTER</span><select name="a" required><option value="">Pick a fighter</option>{compare_options(ctx, a)}</select></label>
  <span class="d cmp-vs" aria-hidden="true">VS</span>
  <label><span class="eyebrow-grey">FIGHTER</span><select name="b" required><option value="">Pick a fighter</option>{compare_options(ctx, b)}</select></label>
  <button class="btn btn-gold" type="submit">COMPARE</button>
</form>"""

    content = ""
    if a and b and a != b:
        try:
            totals = ctx.fight_stat_totals(ctx.gid)
        except Exception:
            totals = {}
        na, nb = compare_numbers(ctx, a, totals), compare_numbers(ctx, b, totals)
        better_low = {"Best rank"}
        rows = ""
        for i, key in enumerate(k for k in na if k != "record"):
            va, vb = na[key], nb[key]
            ca = cb = ""
            if isinstance(va, (int, float)) and isinstance(vb, (int, float)) and va != vb:
                a_wins = (va < vb) if key in better_low else (va > vb)
                ca, cb = (" win", "") if a_wins else ("", " win")
            elif key == "Streak" and va != vb:
                def streak_score(v):
                    return int(v[1:]) if v.startswith("W") else -int(v[1:]) if v[:1] in ("L",) and v[1:].isdigit() else 0
                sa, sb = streak_score(va), streak_score(vb)
                if sa != sb:
                    ca, cb = (" win", "") if sa > sb else ("", " win")

            def show(v, k=key):
                if v is None:
                    return "–"
                if k in ("Win rate", "Finish rate", "Sig. strike accuracy"):
                    return f"{v}%"
                if k == "Best rank":
                    return "Champion" if v == 0 else f"#{v}"
                return esc(v)

            rows += (f'<div class="cmp-row {stagger(i, 3)}"><span class="d cmp-v{ca}">{show(va)}</span>'
                     f'<span class="cmp-l">{esc(key.upper())}</span><span class="d cmp-v r{cb}">{show(vb)}</span></div>')

        def side(uid, numbers, cls=""):
            name = fighter_name(ctx, uid)
            division = main_division(ctx, uid)
            rank = rank_label(rank_in(ctx, uid, division))
            return (f'<div class="cmp-head{cls}">{avatar_html(ctx, uid, name, "av av-lg")}'
                    f'<span class="d cmp-name">{name_link(ctx, uid, name)}</span>'
                    f'<span class="d cmp-rec">{esc(numbers["record"])}</span>'
                    f'<span class="muted">{esc((division or "").upper())}{" · " + esc(rank.upper()) if rank else ""}</span></div>')

        meetings = ctx.head_to_head(ctx.gid, a, b) if getattr(ctx, "head_to_head", None) else []
        wins_a = sum(1 for f in meetings if f["winner_id"] == a and f["method"] not in ("DRAW", "NC"))
        wins_b = sum(1 for f in meetings if f["winner_id"] == b and f["method"] not in ("DRAW", "NC"))
        meet_rows = "".join(result_row(ctx, f, i) for i, f in enumerate(reversed(meetings)))
        meet_head = (f"{len(meetings)} meeting{'s' if len(meetings) != 1 else ''} · "
                     f"{esc(fighter_name(ctx, a))} {wins_a} – {wins_b} {esc(fighter_name(ctx, b))}") if meetings else "They've never fought"

        content = f"""
  <section class="block bt cmp" aria-label="Comparison">
    <div class="cmp-top rv">{side(a, na)}<span class="d cmp-big" aria-hidden="true">VS</span>{side(b, nb, ' r')}</div>
    <div class="cmp-rows">{rows}</div>
  </section>
  <section class="block bt" aria-labelledby="h2h">
    <div class="head-t rv"><span class="eyebrow">{meet_head.upper()}</span><h2 id="h2h" class="d h-m">HEAD TO HEAD</h2></div>
    {f'<div class="list" style="margin-top:28px">{meet_rows}</div>' if meet_rows else ''}
  </section>"""
    elif a and b and a == b:
        content = '<p class="note">Pick two different fighters.</p>'

    body = page_hero("ANY TWO FIGHTERS, SIDE BY SIDE", "HEAD TO HEAD", "VS") + f'<div class="wrap"><section class="block cmp-pick">{form}</section>{content}</div>'
    title = f"{fighter_name(ctx, a)} vs {fighter_name(ctx, b)}" if a and b and a != b else "Head to head"
    return page(ctx, title, "compare", body, "Compare any two PFO fighters side by side.")


# ============================================================
# BETS & FANTASY  /sportsbook  /fantasy
# ============================================================

def play_tabs(active):
    tabs = [("sportsbook", "/sportsbook", "SPORTSBOOK"), ("fantasy", "/fantasy", "FANTASY"), ("shop", "/shop", "SHOP")]
    return '<nav class="ptabs l-up" aria-label="Bets and fantasy">' + "".join(
        f'<a href="{href}"' + (' aria-current="page"' if key == active else '') + f'>{label}</a>' for key, href, label in tabs
    ) + '</nav>'


def player_name(ctx, user_id):
    member = ctx.guild.get_member(user_id)
    if member:
        return clean_name(ctx, member.display_name)
    return "Former member"


def odds_cell(value, opening):
    move = ""
    if value < opening - 0.01:
        move = '<span class="omove down" title="Shortened from %.2f">▼</span>' % opening
    elif value > opening + 0.01:
        move = '<span class="omove up" title="Drifted from %.2f">▲</span>' % opening
    return f'<span class="d odds">{value:.2f}</span>{move}'


def market_card(ctx, market, cache):
    is_open = ctx.market_is_open(market)
    bouts = ctx.market_bouts(market["id"])
    if not bouts:
        return ""
    bettors = ctx.q("SELECT COUNT(DISTINCT user_id) AS n, COALESCE(SUM(stake), 0) AS total FROM bets WHERE market_id = ?",
                    (market["id"],))[0]

    spots = {}
    for bout in bouts:
        try:
            spots[bout["position"]] = ctx.card_spot_for(ctx.gid, market["event"], bout["fighter_a"], bout["fighter_b"])
        except Exception:
            spots[bout["position"]] = None
    if all(spots.values()):
        bouts = sorted(bouts, key=lambda bout: spots[bout["position"]]["number"])

    rows = ""
    for i, bout in enumerate(bouts):
        spot = spots.get(bout["position"])
        label = spot_label(spot) if spot else ctx.bout_slot(bout["position"], len(bouts)).upper()
        weight = f' · {esc(bout["weight"].upper())}' if row_get(bout, "weight") else ""
        head = f'<span class="sb-spot">{esc(label)}{weight}</span>'

        if bout["result"]:
            if bout["result"] == "void":
                verdict = "FIGHT CANCELLED · REFUNDED" if bout["method"] == "CANCELLED" else "DRAW / NO CONTEST · REFUNDED"
                rows += (f'<div class="sbfight sb-done {stagger(i)}">{head}<div class="sb-result">'
                         f'<span class="d">{esc(bout["name_a"])} vs {esc(bout["name_b"])}</span><span class="muted">{verdict}</span></div></div>')
                continue
            odds = ctx.live_odds(bout)
            winner = bout["result"]
            win_name = bout["name_a"] if winner == "a" else bout["name_b"]
            lose_name = bout["name_b"] if winner == "a" else bout["name_a"]
            upset = odds[winner] > odds["b" if winner == "a" else "a"]
            how = ctx.METHOD_NAMES.get(bout["method"], bout["method"] or "") if getattr(ctx, "METHOD_NAMES", None) else ""
            try:
                if how and bout["method"] in ("KO/TKO", "SUB") and bout["rnd"]:
                    how += f" R{bout['rnd']}"
            except (IndexError, KeyError):
                pass
            rows += (f'<div class="sbfight sb-done {stagger(i)}">{head}<div class="sb-result">'
                     f'<span class="d"><span class="gold">{esc(win_name)}</span> <span class="def">DEF.</span> {esc(lose_name)}</span>'
                     f'<span class="muted">{esc(how.upper())} · WON AT {odds[winner]:.2f}{" · " if upset else ""}'
                     f'{"<span class=tag-upset>UPSET</span>" if upset else ""}</span></div></div>')
            continue

        odds = ctx.live_odds(bout)
        pools = ctx.fight_pools(bout["market_id"], bout["position"])
        pool = odds["pool"]
        share_a = round(100 * pools["a"] / pool) if pool else 50

        def side(pick, fighter_id, name, cls=""):
            tag = ""
            try:
                tag = ctx.short_tag(ctx.ranking_tag(ctx.gid, fighter_id, bout["weight"]))
            except Exception:
                tag = ""
            methods = ""
            pos = bout["position"]
            plain = clean_name(ctx, name)
            try:
                parts = []
                round_picks = list(getattr(ctx, "ROUND_PICKS", {}) or {})
                for method, short in (("KO/TKO", "KO"), ("SUB", "SUB"), ("DEC", "DEC"), *((r, r) for r in round_picks)):
                    value = ctx.pick_odds(bout, pick, method)
                    if is_open:
                        label = (f"{plain} to finish in Round {ctx.ROUND_PICKS[method]}" if method in round_picks
                                 else f"{plain} by {ctx.METHOD_NAMES.get(method, method)}")
                        parts.append(f'<button type="button" class="obtn obtn-m" data-pos="{pos}" data-pick="{pick}" data-method="{method}" '
                                     f'data-o="{pos}:{pick}:{method}" data-label="{esc(label)}">{short} <b>{value:.2f}</b></button>')
                    else:
                        parts.append(f'<span>{short} <b>{value:.2f}</b></span>')
                methods = f'<span class="sb-methods">{"".join(parts)}</span>'
            except Exception:
                pass
            main = odds_cell(odds[pick], odds["open_" + pick])
            if is_open:
                main = (f'<button type="button" class="obtn" data-pos="{pos}" data-pick="{pick}" data-method="" data-o="{pos}:{pick}:" '
                        f'data-label="{esc(plain)} to win">{main}</button>')
            stats = stats_for(ctx, fighter_id, cache)
            record = ctx.stats_record(stats) if stats else ""
            return (f'<div class="sb-side{cls}">{avatar_html(ctx, fighter_id, name, "av")}'
                    f'<span class="sb-who"><span class="d sb-name">{name_link(ctx, fighter_id, clean_name(ctx, name))}'
                    f'{f" <span class=sb-tag>{esc(tag)}</span>" if tag else ""}</span>'
                    f'<span class="muted tab">{esc(record)}</span></span>'
                    f'<span class="sb-price">{main}{methods}</span></div>')

        money = (f'<div class="sb-money"><span class="sb-bar"><span style="width:{share_a}%"></span></span>'
                 f'<span class="muted tab">{share_a}% · {pool:,} coins bet · {100 - share_a}%</span></div>'
                 if pool else '<div class="sb-money"><span class="muted">No bets yet</span></div>')
        rows += (f'<div class="sbfight {stagger(i)}">{head}<div class="sb-grid">'
                 f'{side("a", bout["fighter_a"], bout["name_a"])}<span class="d sb-vs">VS</span>'
                 f'{side("b", bout["fighter_b"], bout["name_b"], " r")}</div>{money}</div>')

    if is_open:
        state = (f'<span class="sb-state open"><span class="dot" aria-hidden="true"></span>OPEN</span>'
                 f'<span class="muted">Closes {when_html(market["closes_at"])}</span>')
    elif market["status"] == "settled":
        state = '<span class="sb-state done">SETTLED</span><span class="muted">Winnings paid out</span>'
    else:
        state = '<span class="sb-state locked">LOCKED</span><span class="muted">Paid out as results are logged</span>'

    return f"""
  <section class="block bt sbmarket" aria-label="{esc(market['event'])}" data-market="{market['id']}" data-event="{esc(market['event'])}"{' data-open="1"' if is_open else ''}>
    <div class="head rv">
      <div class="head-t"><div class="sb-states">{state}</div><h2 class="d h-l">{esc(market['event'].upper())}</h2></div>
      <span class="muted">{bettors['total']:,} coins staked by {bettors['n']} bettor{'s' if bettors['n'] != 1 else ''}</span>
    </div>
    <div class="sbfights">{rows}</div>
  </section>"""


def render_sportsbook(ctx):
    cache = {}
    markets = ctx.q("SELECT * FROM bet_markets WHERE guild_id = ? AND status != 'settled' ORDER BY id DESC LIMIT 3", (ctx.gid,))
    settled = ctx.q("SELECT * FROM bet_markets WHERE guild_id = ? AND status = 'settled' ORDER BY id DESC LIMIT 1", (ctx.gid,))
    cards = "".join(market_card(ctx, market, cache) for market in markets)
    if not cards:
        cards = ('<section class="block"><p class="muted rv">No betting open right now. When staff open betting on a card, '
                 'the odds show here live, the same as the sportsbook post in Discord.</p></section>')
    last = "".join(market_card(ctx, market, cache) for market in settled)

    # Numbers
    g = ctx.gid
    totals = ctx.q("SELECT COUNT(*) AS n, COALESCE(SUM(stake), 0) AS staked FROM bets WHERE guild_id = ?", (g,))[0]
    longest = ctx.q("SELECT MAX(odds) AS o FROM bets WHERE guild_id = ? AND status = 'won'", (g,))[0]["o"]
    paid = ctx.q("SELECT COALESCE(SUM(payout), 0) AS p FROM bets WHERE guild_id = ? AND status = 'won'", (g,))[0]["p"]
    strip = "".join([
        chip("BETS PLACED", f"{totals['n']:,}"),
        chip("COINS STAKED", f"{totals['staked']:,}", "gold"),
        chip("PAID OUT", f"{paid:,}"),
    ] + ([chip("LONGEST ODDS WON", f"{longest:.2f}", "red")] if longest else []))

    # Leaderboards
    def board(title, rows_html, note=""):
        return (f'<div class="pbox rv2"><h3 class="pbox-h">{title}</h3>'
                f'{rows_html or "<p class=empty>Nothing yet.</p>"}{f"<p class=pbox-note>{note}</p>" if note else ""}</div>')

    def place_rows(items):
        return '<ol class="prank">' + "".join(
            f'<li><span class="prank-n">{i}</span><span class="prank-who">{esc(name)}</span><span class="prank-v tab">{value}</span></li>'
            for i, (name, value) in enumerate(items, start=1)) + '</ol>' if items else ""

    profits = ctx.q("""
        SELECT user_id, SUM(CASE WHEN status = 'won' THEN payout - stake WHEN status = 'lost' THEN payout - stake ELSE 0 END) AS profit,
               SUM(CASE WHEN status = 'won' THEN 1 ELSE 0 END) AS wins, COUNT(*) AS n
        FROM bets WHERE guild_id = ? AND status IN ('won', 'lost') GROUP BY user_id HAVING profit > 0 ORDER BY profit DESC LIMIT 10
    """, (g,))
    all_time = place_rows([(player_name(ctx, r["user_id"]), f'+{r["profit"]:,}') for r in profits])

    week_rows = ""
    try:
        winners, _ = ctx.bets_of_the_week(g)
        week_rows = place_rows([(player_name(ctx, uid), f'+{profit:,}') for uid, profit, _ in winners[:10]])
    except Exception:
        week_rows = ""

    big = ctx.q("SELECT * FROM bets WHERE guild_id = ? AND status = 'won' ORDER BY payout - stake DESC LIMIT 6", (g,))
    big_rows = ""
    for bet in big:
        legs = ctx.q("SELECT COUNT(*) AS n FROM bet_legs WHERE bet_id = ?", (bet["id"],))[0]["n"]
        market = ctx.get_market(bet["market_id"])
        big_rows += (f'<li><span class="prank-who">{esc(player_name(ctx, bet["user_id"]))}'
                     f'<span class="muted">{"single" if legs <= 1 else f"{legs}-fold"} @ {bet["odds"]:.2f}'
                     f'{" · " + esc(market["event"]) if market else ""}</span></span>'
                     f'<span class="prank-v tab">{bet["stake"]:,} → <b class="gold">{bet["payout"]:,}</b></span></li>')
    big_html = f'<ol class="prank prank-plain">{big_rows}</ol>' if big_rows else ""

    upsets = ""
    try:
        for row in ctx.q("SELECT * FROM upset_alerts WHERE guild_id = ? ORDER BY posted_at DESC LIMIT 6", (g,)):
            fight = ctx.q("SELECT * FROM fights WHERE id = ?", (row["fight_id"],))
            if not fight:
                continue
            fight = fight[0]
            upsets += (f'<li><span class="prank-who"><span>{name_link(ctx, fight["winner_id"], fighter_name(ctx, fight["winner_id"], fight["winner_name"]))}'
                       f' <span class="muted">def.</span> {esc(fighter_name(ctx, fight["loser_id"], fight["loser_name"]))}</span>'
                       f'<span class="muted">{esc(fight["event"] or "")}</span></span>'
                       f'<span class="prank-v tab"><b class="red">{row["odds"]:.2f}</b></span></li>')
    except Exception:
        upsets = ""
    upsets_html = f'<ol class="prank prank-plain">{upsets}</ol>' if upsets else ""

    boards = f"""
  <section class="block bt" aria-labelledby="sbboards">
    <div class="head-t rv"><span class="eyebrow">WHO'S WINNING</span><h2 id="sbboards" class="d h-m">BETTING BOARDS</h2></div>
    <div class="pgrid">
      {board("THIS WEEK", week_rows, "Profit from bets, Monday to Sunday")}
      {board("ALL TIME", all_time, "Profit from settled bets")}
      {board("BIGGEST WINS", big_html)}
      {board("BIGGEST UPSETS", upsets_html, "Winners at long odds")}
    </div>
  </section>"""

    cta = '<p class="muted l-up sb-how" data-howbet>Tap any odds to add them to your slip. Log in with Discord to bet here, or use the sportsbook post in Discord.</p>'
    hero = page_hero("LIVE ODDS, STRAIGHT FROM THE DISCORD", "SPORTSBOOK", "ODDS",
                     play_tabs("sportsbook") + f'<div class="evchips l-up">{strip}</div>' + cta)
    mine = ('<section class="block bt" id="mybets" aria-labelledby="mybets-h" hidden><div class="head-t rv"><span class="eyebrow">ONLY YOU CAN SEE THIS</span>'
            '<h2 id="mybets-h" class="d h-m">MY BETS</h2></div><div data-mybets></div></section>')
    slip = (f'<aside class="slip" id="slip" hidden aria-label="Bet slip" data-cap="{getattr(ctx, "MAX_BET_ODDS", 0) or 0}"><div class="slip-head"><span class="d">BET SLIP</span><span class="muted" id="slipodds"></span>'
            '<button type="button" class="slip-x" id="slipclear" aria-label="Clear the slip">CLEAR</button></div><ol class="slip-legs" id="sliplegs"></ol>'
            '<div class="slip-foot"><label>Stake <input type="number" id="slipstake" min="1" step="1" inputmode="numeric" placeholder="Coins"></label>'
            '<span class="muted" id="slipret"></span><button type="button" class="btn btn-gold btn-sm" id="slipgo">PLACE BET</button></div>'
            '<p class="slip-note">One pick per fight. Points only, no money value.</p></aside>')
    body = hero + f'<div class="wrap">{cards}{mine}{boards}{last}</div>{slip}'
    return page(ctx, "Sportsbook", "sportsbook", body, "Live PFO betting odds, the biggest winners and the upsets.")


def render_fantasy(ctx):
    g = ctx.gid
    players, fighters, per_pick = ctx.fantasy_scores(g)
    counts = ctx.fantasy_pick_counts(g)
    teams = len({row["user_id"] for row in ctx.q("SELECT DISTINCT user_id FROM fantasy_picks WHERE guild_id = ? AND removed_at IS NULL", (g,))})

    # Round status
    current = ctx.open_fantasy_round(g)
    latest = ctx.q("SELECT * FROM fantasy_rounds WHERE guild_id = ? ORDER BY id DESC LIMIT 1", (g,))
    if current:
        status = chip("SWAPS OPEN", esc(current["event"] or "This card"), "gold")
        status += chip("LOCKS", when_html(current["locks_at"]))
    elif latest and not latest[0]["snapshotted"]:
        status = chip("NEXT CARD", esc(latest[0]["event"] or "Coming up"))
    else:
        status = chip("SWAPS", "Open when the next card is out")
    strip = chip("TEAMS", teams) + status + chip("BUDGET", f"{ctx.FANTASY_BUDGET} for {ctx.FANTASY_TEAM_SIZE}")

    # Standings with teams
    owners = {row["user_id"] for row in ctx.q("SELECT DISTINCT user_id FROM fantasy_picks WHERE guild_id = ? AND removed_at IS NULL", (g,))}
    standings = sorted(owners | set(players), key=lambda uid: (-players.get(uid, 0), player_name(ctx, uid).lower()))
    rows = ""
    for place, uid in enumerate(standings[:30], start=1):
        team = ctx.fantasy_team(g, uid)
        picks = "".join(
            f'<span class="tpick{" cap" if pick["captain"] else ""}">{"★ " if pick["captain"] else ""}'
            f'{esc(fighter_name(ctx, pick["fighter_id"]))}<b>{per_pick.get((uid, pick["fighter_id"]), 0)}</b></span>'
            for pick in team) or '<span class="muted">No fighters right now</span>'
        rows += (f'<div class="trow {stagger(place)}"><span class="d trow-n">{place}</span>'
                 f'<span class="trow-main"><span class="d trow-who">{esc(player_name(ctx, uid))}</span><span class="tpicks">{picks}</span></span>'
                 f'<span class="d trow-pts">{players.get(uid, 0)}<span>PTS</span></span></div>')
    more = f'<p class="note">…and {len(standings) - 30} more teams.</p>' if len(standings) > 30 else ""
    standings_html = f"""
  <section class="block" aria-labelledby="fstand">
    <div class="head-t rv"><span class="eyebrow">★ CAPTAIN SCORES DOUBLE · POINTS PER FIGHTER FOR THAT TEAM</span><h2 id="fstand" class="d h-m">STANDINGS</h2></div>
    <div class="tlist">{rows or '<p class="empty">Nobody has made a team yet. Use /fantasy in Discord.</p>'}</div>{more}
  </section>"""

    # Boards
    def place_rows(items):
        return '<ol class="prank">' + "".join(
            f'<li><span class="prank-n">{i}</span><span class="prank-who">{who}</span><span class="prank-v tab">{value}</span></li>'
            for i, (who, value) in enumerate(items, start=1)) + '</ol>' if items else ""

    best = sorted(((fid, pts) for fid, pts in fighters.items() if pts > 0), key=lambda item: item[1], reverse=True)[:10]
    best_html = place_rows([(name_link(ctx, fid, fighter_name(ctx, fid)), f"{pts} pts") for fid, pts in best])
    picked = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:10]
    picked_html = place_rows([(name_link(ctx, fid, fighter_name(ctx, fid)), f"{round(100 * n / teams) if teams else 0}%") for fid, n in picked])

    movers_html, movers_note = "", ""
    try:
        priced = ctx.q("SELECT DISTINCT round_id FROM fantasy_round_prices ORDER BY round_id DESC LIMIT 1")
        if priced:
            round_id = priced[0]["round_id"]
            event = ctx.q("SELECT event FROM fantasy_rounds WHERE id = ?", (round_id,))
            movers_note = f"Since {event[0]['event']}" if event and event[0]["event"] else "Since the last card"
            moves = ctx.price_changes(g, round_id)[:10]
            movers_html = '<ol class="prank prank-plain">' + "".join(
                f'<li><span class="prank-who">{name_link(ctx, fid, fighter_name(ctx, fid))}</span>'
                f'<span class="prank-v tab">{before} → <b class="{"green" if after > before else "red"}">{after}</b> '
                f'<span class="{"green" if after > before else "red"}">{"▲" if after > before else "▼"}{abs(after - before)}</span></span></li>'
                for fid, before, after in moves) + '</ol>' if moves else ""
    except Exception:
        movers_html = ""

    last_html = ""
    try:
        award = ctx.q("SELECT * FROM fantasy_round_awards WHERE guild_id = ? ORDER BY awarded_at DESC LIMIT 1", (g,))
        if award:
            import json as _json
            round_id = award[0]["round_id"]
            event = ctx.q("SELECT event FROM fantasy_rounds WHERE id = ?", (round_id,))
            _, round_list, _ = ctx.fantasy_round_scores(g, round_id)
            team, total = ctx.team_of_the_event(g, round_list, ctx.round_prices(round_id))
            winners = _json.loads(award[0]["winners"])
            team_rows = "".join(f'<li><span class="prank-who"><span>{"★ " if i == 0 else ""}{name_link(ctx, fid, fighter_name(ctx, fid))}</span></span>'
                                f'<span class="prank-v tab">{pts} pts · {price}</span></li>' for i, (fid, pts, price) in enumerate(team))
            last_html = (f'<div class="pbox pbox-wide rv2"><h3 class="pbox-h">LAST CARD · {esc((event[0]["event"] if event else "") or "").upper()}</h3>'
                         + (f'<p class="fan-top"><span class="eyebrow-grey">TOP SCORER</span><span class="d fan-big">{" & ".join(esc(player_name(ctx, w)) for w in winners)}</span>'
                            f'<span class="muted">{award[0]["points"]} pts · won {award[0]["amount"]:,} coins</span></p>' if winners else "")
                         + (f'<span class="eyebrow-grey">TEAM OF THE EVENT · {total} PTS</span><ol class="prank prank-plain">{team_rows}</ol>' if team_rows else "")
                         + '</div>')
    except Exception:
        last_html = ""

    def board(title, html_rows, note=""):
        return (f'<div class="pbox rv2"><h3 class="pbox-h">{title}</h3>'
                f'{html_rows or "<p class=empty>Nothing yet.</p>"}{f"<p class=pbox-note>{esc(note)}</p>" if note else ""}</div>')

    boards = f"""
  <section class="block bt" aria-labelledby="fboards">
    <div class="head-t rv"><span class="eyebrow">WHO'S DELIVERING</span><h2 id="fboards" class="d h-m">FANTASY BOARDS</h2></div>
    <div class="pgrid pgrid-3">
      {last_html}
      {board("BEST PERFORMERS", best_html, "Fantasy points scored this season")}
      {board("PRICE MOVERS", movers_html, movers_note)}
      {board("MOST PICKED", picked_html, "Share of teams with them right now")}
    </div>
  </section>"""

    # Price list
    price_rows = ""
    for i, (price, name, fid) in enumerate(ctx.fantasy_price_rows(ctx.guild)):
        owned = round(100 * counts.get(fid, 0) / teams) if teams else 0
        division = main_division(ctx, fid) or ""
        price_rows += (f'<tr data-name="{esc(clean_name(ctx, name).lower())}" data-id="{fid}" data-price="{price}"{" class=pmore" if i >= 25 else ""}><td class="d td-price" data-v="{price}">{price}</td>'
                       f'<td class="d td-opp">{name_link(ctx, fid, clean_name(ctx, name))}</td>'
                       f'<td class="td-m">{esc(division.upper())}</td>'
                       f'<td class="tab r" data-v="{fighters.get(fid, 0)}">{fighters.get(fid, 0)}</td>'
                       f'<td class="tab r" data-v="{owned}">{owned}%</td><td class="td-act" data-v="0"></td></tr>')
    prices = f"""
  <section class="block bt" aria-labelledby="fprices">
    <div class="head rv">
      <div class="head-t"><span class="eyebrow">5 TO 30 · FROM RECORD, RANK, STREAK AND FINISHES</span><h2 id="fprices" class="d h-m">PRICE LIST</h2></div>
      <label class="search"><span class="vh">Search the price list</span>
        <svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
        <input type="search" id="psearch" placeholder="Search fighters" autocomplete="off">
      </label>
    </div>
    <div class="tscroll"><table class="ftable ptable" id="ptable">
      <thead><tr><th scope="col" data-sort="0">PRICE</th><th scope="col">FIGHTER</th><th scope="col">DIVISION</th>
      <th scope="col" class="r" data-sort="3">POINTS</th><th scope="col" class="r" data-sort="4">PICKED</th><th scope="col" class="td-act"><span class="vh">Add</span></th></tr></thead>
      <tbody>{price_rows}</tbody></table></div>
    {f'<button class="btn btn-ghost btn-sm pshow" id="pshow" type="button">SHOW ALL {len(ctx.fantasy_price_rows(ctx.guild))} FIGHTERS</button>' if len(ctx.fantasy_price_rows(ctx.guild)) > 25 else ''}
    <p class="note">Click PRICE, POINTS or PICKED to sort.</p>
  </section>
<script>
(function(){{
  var t=document.getElementById('ptable'); if(!t) return;
  var body=t.tBodies[0], s=document.getElementById('psearch'), dir={{}};
  var b=document.getElementById('pshow');
  if(b) b.addEventListener('click',function(){{t.classList.add('pall'); b.remove();}});
  if(s) s.addEventListener('input',function(){{var q=s.value.trim().toLowerCase();
    t.classList.toggle('psearching',!!q);
    Array.prototype.forEach.call(body.rows,function(r){{r.hidden=q&&r.getAttribute('data-name').indexOf(q)<0;}});}});
  Array.prototype.forEach.call(t.querySelectorAll('th[data-sort]'),function(th){{
    th.style.cursor='pointer'; th.addEventListener('click',function(){{
      var c=+th.getAttribute('data-sort'); dir[c]=!dir[c];
      var rows=Array.prototype.slice.call(body.rows);
      rows.sort(function(a,b){{var x=+a.cells[c].getAttribute('data-v'),y=+b.cells[c].getAttribute('data-v');return dir[c]?y-x:x-y;}});
      rows.forEach(function(r){{body.appendChild(r);}});
      t.classList.add('pall'); if(b) b.remove();
    }});
  }});
}})();
</script>"""

    p = ctx.FANTASY_POINTS
    rules = f"""
  <section class="block bt" aria-labelledby="frules">
    <div class="head-t rv"><span class="eyebrow">ONE TEAM FOR THE SEASON · {ctx.FANTASY_SWAPS} FREE SWAPS EVERY CARD</span><h2 id="frules" class="d h-m">HOW IT SCORES</h2></div>
    <div class="evchips rv2" style="margin-top:28px">
      {chip("WIN", f"+{p['win']}")}{chip("KO / SUB", f"+{p['finish']}", "gold")}{chip("DECISION", f"+{p['decision']}")}
      {chip("ROUND 1 FINISH", f"+{p['round1']}")}{chip("UNDERDOG WIN", f"+{p['underdog']}")}{chip("TITLE WIN", f"+{p['title']}")}
      {chip("EACH AWARD", f"+{p['award']}")}{chip("DRAW", f"+{p['draw']}")}{chip("CAPTAIN", "×2", "gold")}
    </div>
    <p class="note">Top scorer on each card wins {ctx.FANTASY_EVENT_BONUS:,} coins. Captain changes: free until your first card locks, then 1 per card. Make your team here or with /fantasy in Discord.</p>
  </section>"""

    hero = page_hero("ONE TEAM, THE WHOLE SEASON", "FANTASY", "PTS", play_tabs("fantasy") + f'<div class="evchips l-up">{strip}</div>')
    myteam = ('<section class="block myteam" id="myteam" aria-labelledby="myteam-h"><div class="head-t rv"><span class="eyebrow">YOUR TEAM</span>'
              '<h2 id="myteam-h" class="d h-m">MY FANTASY</h2></div><div data-myteam>'
              + login_box("Log in with Discord to make and manage your fantasy team here.", "/fantasy") + '</div></section>')
    body = hero + f'<div class="wrap">{myteam}{standings_html}{boards}{prices}{rules}</div>'
    return page(ctx, "Fantasy", "fantasy", body, "Fantasy PFO standings, teams, prices and price movers.")


# ============================================================
# TITLES
# ============================================================

def render_hall_of_fame(ctx):
    rows_fn = getattr(ctx, "hall_of_fame", None)
    rows = rows_fn(ctx.gid) if rows_fn else []

    def card_data(number, row):
        uid = row["user_id"]
        name = fighter_name(ctx, uid, row["name"]) if uid else clean_name(ctx, row["name"])
        career = ctx.hof_career(ctx.gid, uid, row["name"])
        return {
            "number": number, "uid": uid, "name": name, "notes": ctx.hof_notes(row),
            "career": career, "inducted": uk_time(row["inducted_at"]),
        }

    cards = [card_data(n, row) for n, row in enumerate(rows, start=1)]

    if not cards:
        body = page_hero("PFO LEGENDS", "HALL OF FAME", "HOF") + \
            '<div class="wrap hof-wrap"><p class="empty">Nobody has been inducted yet.</p></div>'
        return page(ctx, "Hall of Fame", "halloffame", body, "The PFO Hall of Fame.")

    top, rest = cards[0], cards[1:]

    def link_open(card):
        return f'<a href="{fighter_url(card["uid"])}"' if card["uid"] else '<div'

    def link_close(card):
        return "</a>" if card["uid"] else "</div>"

    first, last = split_name(top["name"])
    c = top["career"]
    belts = "".join(f'<span class="hof-belt">{esc(b.upper())} CHAMPION</span>' for b in c["belts"])
    notes = "".join(f'<li><span aria-hidden="true">◆</span>{esc(n)}</li>' for n in top["notes"])
    feature = f"""
    {link_open(top)} class="hof-feature rv">
      <div class="hof-side">
        <span class="eyebrow">INDUCTEE #1 · {esc(top['inducted'].strftime('%b %Y').upper())}</span>
        {avatar_html(ctx, top['uid'], top['name'], 'av av-xl')}
        <span class="d hof-name">{esc(first)}<br>{esc(last)}</span>
        <span class="feature-stats">
          <span><strong class="d">{esc(c['record'])}</strong>RECORD</span>
          <span><strong class="d">{c['reigns']}</strong>TITLE REIGNS</span>
          <span><strong class="d">{c['defences']}</strong>DEFENCES</span>
        </span>
      </div>
      <div class="hof-main">
        <ul class="hof-notes">{notes}</ul>
        {f'<div class="hof-belts">{belts}</div>' if belts else ''}
      </div>
    {link_close(top)}"""

    grid = ""
    for i, card in enumerate(rest):
        notes = "".join(f'<li><span aria-hidden="true">◆</span>{esc(n)}</li>' for n in card["notes"])
        grid += f"""
      {link_open(card)} class="hof-card {stagger(i, 3)}">
        <span class="hof-head">
          {avatar_html(ctx, card['uid'], card['name'], 'av')}
          <span class="hof-who"><span class="eyebrow">INDUCTEE #{card['number']} · {esc(card['inducted'].strftime('%b %Y').upper())}</span>
          <span class="d hof-cname">{esc(card['name'])}</span></span>
          <span class="d hof-rec">{esc(card['career']['record'])}</span>
        </span>
        <ul class="hof-notes hof-notes-sm">{notes}</ul>
      {link_close(card)}"""

    extra = f'<p class="muted l-up">{len(cards)} inductee{"s" if len(cards) != 1 else ""} · the fighters who defined PFO</p>'
    body = page_hero("PFO LEGENDS", "HALL OF FAME", "HOF", extra) + f"""
<div class="wrap hof-wrap">
  {feature}
  {f'<div class="hof-grid">{grid}</div>' if grid else ''}
</div>"""

    return page(ctx, "Hall of Fame", "halloffame", body, "The PFO Hall of Fame: the fighters who defined the Prestige Fighting Organisation.")


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
            opp = fighter_name(ctx, won["loser_id"], won["loser_name"]) if won else None
            how = (f"Won {esc(short_day(reign['start']))} vs {esc(opp)} · {esc(ctx.fight_method_text(won))}" if won
                   else f"Became champion {esc(short_day(reign['start']))}")
            days = ctx.reign_days(reign)
            current = reign["end"] is None
            d = reign["defences"]
            items += f"""
          <li class="lin{' lin-now' if current else ''} {stagger(i, 3)}">
            <span class="lin-n d">{number}</span>
            {avatar_html(ctx, reign['user_id'], name, 'av av-sm' + (' av-gold' if current else ''))}
            <span class="lin-t">
              {name_link(ctx, reign['user_id'], name, 'd lin-name')}
              <span class="muted">{how}</span>
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



# ============================================================
# LEADERBOARDS (the same boards as the bot's /leaderboard)
# ============================================================

BOARD_GROUPS = [
    ("Record", [("wins", "Most wins"), ("streak", "Win streak"), ("titles", "Title fight wins")]),
    ("Finishing", [("ko", "KO / TKO wins"), ("sub", "Submission wins"), ("finish", "Finish rate")]),
    ("Fight stats", [("strikes", "Strikes landed"), ("sigstrikes", "Significant strikes"), ("sigacc", "Striking accuracy"),
                     ("takedowns", "Takedowns"), ("tdacc", "Takedown accuracy"), ("control", "Control time"),
                     ("knockdowns", "Knockdowns")]),
    ("Odds", [("favwins", "Wins as favourite"), ("dogwins", "Wins as underdog")]),
    ("Community", [("votes", "Correct picks"), ("coins", "Richest")]),
]

BOARD_NOTES = {
    "finish": "KO/TKO and submission wins as a share of all wins. Minimum 3 logged wins.",
    "votes": "Correct fight picks from the community vote, once results are logged.",
    "coins": "PFO Coins. Points only, earned in the Discord.",
    "sigacc": "Significant strikes landed out of thrown, from fight stats.",
    "tdacc": "Takedowns landed out of attempted, from fight stats.",
    "favwins": "Favourite = shorter betting odds when betting closed.",
    "dogwins": "Underdog = longer betting odds when betting closed.",
}

BOARD_UNITS = {
    "wins": "wins", "streak": "in a row", "titles": "title wins", "ko": "KO/TKO", "sub": "subs",
    "finish": "", "strikes": "landed", "sigstrikes": "landed", "sigacc": "", "takedowns": "landed",
    "tdacc": "", "control": "", "knockdowns": "knockdowns", "favwins": "wins", "dogwins": "wins",
    "votes": "correct", "coins": "coins",
}


def board_value(category, value):
    if category in ("finish", "sigacc", "tdacc"):
        return f"{value}%"
    if category == "control":
        minutes, seconds = divmod(int(value), 60)
        return f"{minutes}:{seconds:02d}"
    return f"{value:,}" if isinstance(value, int) else str(value)


def board_detail(category, text):
    """The bot's line under each name, made web-friendly."""
    if category == "coins":
        return ""
    text = text.replace("`", "")
    return re.sub(r"<a?:\w+:\d+>", "", text).strip()


def board_rows(ctx, category, cache):
    try:
        rows = ctx.leaderboard_rows(ctx.guild, category, cache)
    except Exception:
        traceback.print_exc()
        return []
    ranked, place, previous = [], 0, None
    for index, (member, value, text) in enumerate(rows[:10], start=1):
        if value != previous:
            place, previous = index, value
        ranked.append((place, member, value, text))
    return ranked


def render_leaderboards(ctx):
    cache = {}
    boards = []
    for group, items in BOARD_GROUPS:
        for key, label in items:
            rows = board_rows(ctx, key, cache)
            if rows:
                boards.append((group, key, label, rows))

    if not boards:
        body = page_hero("TOP 10 IN EVERY CATEGORY", "LEADERBOARDS", "TOP") + \
            '<div class="wrap"><section class="block"><p class="empty">The leaderboards fill up as fights are logged.</p></section></div>'
        return page(ctx, "Leaderboards", "leaderboards", body)

    tabs = ""
    last_group = None
    for group, key, label, rows in boards:
        if group != last_group:
            tabs += f'<span class="lb-group">{esc(group.upper())}</span>'
            last_group = group
        tabs += (f'<a role="tab" class="lb-tab" href="#board-{key}" data-tab="{key}" '
                 f'aria-selected="{"true" if key == boards[0][1] else "false"}">{esc(label)}</a>')

    panels = ""
    for i, (group, key, label, rows) in enumerate(boards):
        top = rows[0][2] or 1
        unit = BOARD_UNITS.get(key, "")

        podium = ""
        for slot, (place, member, value, text) in enumerate(rows[:3]):
            name = clean_name(ctx, member.display_name)
            podium += f"""
        <a href="{fighter_url(member.id)}" class="pod pod-{min(place, 3)} slot-{slot}">
          <span class="pod-place d">{place}</span>
          {avatar_html(ctx, member.id, name, 'av pod-av')}
          <span class="d pod-name">{esc(name)}</span>
          <span class="pod-val"><b class="d">{esc(board_value(key, value))}</b>{f' <span>{esc(unit)}</span>' if unit else ''}</span>
          <span class="pod-detail">{esc(board_detail(key, text))}</span>
        </a>"""

        rest = ""
        for j, (place, member, value, text) in enumerate(rows[3:]):
            name = clean_name(ctx, member.display_name)
            width = max(4, round(100 * (value / top))) if isinstance(value, (int, float)) and top else 0
            rest += f"""
          <li class="row lb-row {stagger(j, 3)}">
            <span class="d lb-place">{place}</span>
            {avatar_html(ctx, member.id, name, 'av av-sm')}
            <span class="lb-who">{name_link(ctx, member.id, name, 'd lb-name')}<span class="lb-bar"><span class="bar bar-gold" style="width:{width}%"></span></span></span>
            <span class="lb-val"><b class="d">{esc(board_value(key, value))}</b><span>{esc(board_detail(key, text))}</span></span>
          </li>"""

        note = BOARD_NOTES.get(key, "From the PFO fight records.")
        panels += f"""
    <div class="lb-panel" role="tabpanel" data-panel="{key}" id="board-{key}" aria-label="{esc(label)}"{'' if i == 0 else ' hidden'}>
      <div class="head lb-head">
        <div class="head-t"><span class="eyebrow">{esc(group.upper())}</span><h2 class="d h-l">{esc(label.upper())}</h2></div>
        <p class="muted lb-note">{esc(note)}</p>
      </div>
      <div class="podium">{podium}</div>
      {f'<ol class="list lb-list">{rest}</ol>' if rest else ''}
    </div>"""

    extra = f'<div class="lb-tabs l-up" role="tablist" aria-label="Leaderboards">{tabs}</div>'
    body = page_hero("TOP 10 IN EVERY CATEGORY", "LEADERBOARDS", "TOP") + f'<div class="wrap lb-wrap">{panels}</div>'
    body = body.replace("</h1>", "</h1>" + extra, 1).replace("phero-title l-title", "phero-title lb-title l-title", 1)
    return page(ctx, "Leaderboards", "leaderboards", body, "PFO leaderboards: wins, finishes, fight stats, picks and more.")


def leaderboard_badges(ctx, user_id):
    """Top 3 spots for a fighter's profile."""
    try:
        places = ctx.leaderboard_places(ctx.guild, user_id)
    except Exception:
        return ""
    labels = dict(item for _, items in BOARD_GROUPS for item in items)
    chips = "".join(
        f'<a class="lbchip lbchip-{place}" href="/leaderboards#board-{key}"><span class="d">{place}</span>{esc(labels.get(key, key))}</a>'
        for place, key in places if key in labels
    )
    return f'<div class="lbchips l-up2">{chips}</div>' if chips else ""


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
.nav-links{display:flex;align-items:center;gap:2px 26px;flex:1 1 auto;font-size:13px;font-weight:600;letter-spacing:.12em;text-transform:uppercase}
.nlink,.ndrop summary{color:var(--muted);padding:12px 0;white-space:nowrap;transition:color .2s;cursor:pointer}
.nlink:hover,.ndrop summary:hover,.ndrop[open] summary{color:var(--text)}
.nlink[aria-current],.ndrop.is-current summary{color:var(--text);box-shadow:inset 0 -2px 0 var(--gold)}
.nlive{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--redc);margin:0 8px 2px 0;animation:blink 1.4s ease-in-out infinite}
.ndrop{position:relative}
.ndrop summary{list-style:none;display:flex;align-items:center;gap:8px}
.ndrop summary::-webkit-details-marker{display:none}
.ndrop summary svg{transition:transform .2s;opacity:.7}
.ndrop[open] summary svg{transform:rotate(180deg)}
.ndrop-menu{position:absolute;top:calc(100% + 10px);left:-18px;min-width:250px;padding:8px;border:1px solid var(--line2);border-radius:14px;background:rgba(12,12,15,.98);box-shadow:0 20px 50px rgba(0,0,0,.55);display:flex;flex-direction:column;text-transform:none;letter-spacing:0;z-index:40}
.ndrop-menu a{display:flex;flex-direction:column;gap:2px;padding:10px 12px;border-radius:10px;color:var(--text)}
.ndrop-menu a:hover{background:var(--panel2)}
.ndrop-menu a[aria-current]{background:rgba(201,162,74,.1)}
.nd-t{font-size:14px;font-weight:700}
.ndrop-menu a[aria-current] .nd-t{color:var(--gold2)}
.nd-s{font-size:12px;font-weight:500;color:var(--dim)}
.nav-burger{display:none;margin-left:auto;width:44px;height:44px;border:1px solid var(--line2);border-radius:12px;background:transparent;cursor:pointer;flex-direction:column;align-items:center;justify-content:center;gap:5px}
.nav-burger span{display:block;width:18px;height:2px;border-radius:2px;background:var(--text);transition:transform .25s,opacity .2s}
.nav-open .nav-burger span:nth-child(1){transform:translateY(7px) rotate(45deg)}
.nav-open .nav-burger span:nth-child(2){opacity:0}
.nav-open .nav-burger span:nth-child(3){transform:translateY(-7px) rotate(-45deg)}
@media (max-width:1080px){
  .nav-in{flex-wrap:wrap;gap:10px 16px}
  .nav-burger{display:flex}
  .nav-cta{margin-left:auto}
  .nav-cta + .nav-burger{margin-left:0}
  .nav-links{display:none;order:5;flex-basis:100%;flex-direction:column;align-items:stretch;gap:0;padding:6px 0 10px;border-top:1px solid var(--line2);max-height:calc(100vh - 80px);overflow-y:auto}
  .nav-open .nav-links{display:flex}
  .nav-open{background:rgba(8,8,10,.98)!important;border-color:var(--line2)!important}
  .nlink,.ndrop summary{padding:14px 2px;border-bottom:1px solid var(--line);font-size:14px}
  .nlink[aria-current],.ndrop.is-current summary{box-shadow:none;color:var(--gold2)}
  .ndrop summary{justify-content:space-between}
  .ndrop-menu{position:static;min-width:0;border:0;box-shadow:none;background:transparent;padding:4px 0 8px 10px}
  .ndrop-menu a{padding:10px 8px}
}

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
.card-row:first-child .card-slot,.card-slot.gold{color:var(--gold)}
.card-sec{list-style:none;padding:26px 6px 10px;font-size:11px;font-weight:700;letter-spacing:.26em;color:var(--dim);border-bottom:1px solid var(--line)}
.card-sec:first-child{padding-top:6px}
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
.tabs{margin-top:26px;display:flex;flex-wrap:wrap;gap:0 26px;border-bottom:1px solid var(--line)}
.tab[data-tab=P4P]{color:var(--gold2)}
.tab[data-tab=P4P][aria-selected=true]{color:var(--gold2)}
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
.champ-col{flex:1 1 340px;align-self:flex-start;display:flex;flex-direction:column;gap:14px;min-width:0}
.champ-col .feature{flex:none;align-self:stretch}
.belts{display:flex;flex-direction:column;gap:10px}
.belt{display:flex;align-items:center;gap:14px;padding:14px 18px;border:1px solid var(--line2);border-radius:14px;background:var(--panel);color:var(--text);transition:border-color .3s,transform .3s var(--ease)}
.belt:hover{border-color:#4A3D1E;color:var(--text);transform:translateX(3px)}
.belt-text{flex:1;min-width:0;display:flex;flex-direction:column;gap:4px}
.belt-label{font-size:11px;font-weight:700;letter-spacing:.2em;color:var(--gold)}
.belt-wmf .belt-label{color:var(--muted)}
.belt-name{font-size:22px;line-height:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.unranked{margin-top:48px}
.unranked .rank-n{color:#4A4A52}
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
.reigns h2+.reign,.reigns .reign+h2{margin-top:0}
.reigns .reign+h2{margin-top:18px}
.award{display:inline-block;margin-left:8px;padding:3px 8px;border-radius:999px;font:700 10px var(--ui,Inter);letter-spacing:.14em;border:1px solid rgba(201,162,74,.5);color:var(--gold2);background:rgba(201,162,74,.08)}
.award-kotn{border-color:rgba(240,122,126,.5);color:#F5A3A6;background:rgba(240,122,126,.08)}
.award-potn{border-color:rgba(127,176,255,.45);color:#A9C8FF;background:rgba(127,176,255,.08)}
.fnick{display:block;margin-top:6px;font:700 italic 24px var(--d);letter-spacing:.04em;color:var(--gold2)}
.fquote{margin:14px 0 0;padding-left:16px;border-left:3px solid var(--gold);font-size:17px;font-style:italic;color:#D7D7DC;max-width:560px}
.fshopbadge{margin-left:10px;border:1px solid rgba(201,162,74,.5);background:rgba(201,162,74,.1);color:var(--gold2);padding:6px 12px;border-radius:999px;font-size:11px;font-weight:700;letter-spacing:.18em}
.fstyle-gold .av-hero{box-shadow:0 0 0 3px var(--gold),0 0 0 9px rgba(201,162,74,.18)}
.fstyle-glow .fhero-name{text-shadow:0 0 28px rgba(226,194,122,.55)}
.fstyle-glow .av-hero{box-shadow:0 0 40px rgba(226,194,122,.45)}
.fstyle-crimson{background:radial-gradient(70% 90% at 15% 30%,rgba(229,72,77,.18),rgba(8,8,10,0) 60%)}
.fstyle-crimson .fhero-name{color:#FFE4E5}
.fstyle-ice{background:radial-gradient(70% 90% at 15% 30%,rgba(127,176,255,.18),rgba(8,8,10,0) 60%)}
.fstyle-ice .fhero-name{color:#E6F0FF}
.spot{display:flex;flex-wrap:wrap;align-items:center;gap:22px 32px;padding:30px 34px;border:1px solid #3A311B;border-radius:20px;color:var(--text);background:radial-gradient(120% 120% at 0% 0%,rgba(201,162,74,.16),rgba(8,8,10,0) 55%),var(--panel);transition:border-color .35s,transform .35s var(--ease)}
.spot:hover{color:var(--text);border-color:#5A4A22;transform:translateY(-3px)}
.spot-name{font-size:48px;line-height:.9}
.spot-meta{display:flex;flex-direction:column;gap:8px;flex:1;min-width:200px}
.spot-rec{font-size:34px;color:var(--muted)}
.hof-wrap{padding-top:64px;padding-bottom:120px;display:flex;flex-direction:column;gap:28px}
.hof-feature{position:relative;overflow:hidden;display:flex;flex-wrap:wrap;gap:40px;padding:48px;border:1px solid #3A311B;border-radius:22px;color:var(--text);background:radial-gradient(120% 120% at 0% 0%,rgba(201,162,74,.18),rgba(8,8,10,0) 55%),var(--panel);transition:border-color .35s,transform .35s var(--ease)}
.hof-feature:hover{color:var(--text);border-color:#5A4A22;transform:translateY(-3px)}
.hof-side{flex:1 1 280px;display:flex;flex-direction:column;gap:22px}
.hof-name{font-size:84px;line-height:.85}
.hof-side .feature-stats{gap:22px;flex-wrap:wrap}
.hof-side .feature-stats strong{white-space:nowrap}
.hof-main{flex:999 1 380px;display:flex;flex-direction:column;justify-content:center;gap:22px}
.hof-notes{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:14px}
.hof-notes li{display:flex;gap:16px;align-items:baseline;font:700 32px/1.05 var(--d)}
.hof-notes li span{color:var(--gold);font-size:16px}
.hof-notes-sm{padding-top:18px;border-top:1px solid var(--line);gap:10px}
.hof-notes-sm li{font:500 16px/1.4 var(--ui,Inter);color:#D7D7DC}
.hof-notes-sm li span{font-size:11px}
.hof-belts{display:flex;flex-wrap:wrap;gap:10px}
.hof-belt{border:1px solid rgba(201,162,74,.45);background:rgba(201,162,74,.08);color:var(--gold2);padding:8px 14px;border-radius:999px;font-size:12px;font-weight:700;letter-spacing:.16em}
.hof-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:28px}
.hof-card{display:flex;flex-direction:column;gap:18px;padding:30px;border:1px solid var(--line2);border-radius:18px;background:var(--panel);color:var(--text);transition:border-color .35s,transform .35s var(--ease)}
.hof-card:hover{color:var(--text);border-color:#4A3D1E;transform:translateY(-3px)}
.hof-head{display:flex;align-items:center;gap:16px}
.hof-who{display:flex;flex-direction:column;gap:6px;min-width:0}
.hof-who .eyebrow{font-size:11px;letter-spacing:.2em}
.hof-cname{font-size:36px;line-height:.9}
.hof-rec{margin-left:auto;font-size:24px;color:var(--muted)}
@media (max-width:640px){.hof-feature{padding:30px 22px}.hof-name{font-size:56px}.hof-notes li{font-size:24px}}
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
  .tabs{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;-webkit-mask:linear-gradient(90deg,#000 85%,transparent);mask:linear-gradient(90deg,#000 85%,transparent);padding-right:40px}
  .tabs::-webkit-scrollbar{display:none}
  .wrap{padding-left:16px;padding-right:16px}
  .nav .btn{padding:9px 14px;min-height:40px;font-size:12px}
  .over-hero main{margin-top:-68px}
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

/* leaderboards */
.lb-tabs{margin-top:26px;display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.lb-group{font-size:10px;font-weight:700;letter-spacing:.22em;color:var(--dim);margin:0 4px 0 12px}
.lb-group:first-child{margin-left:0}
.lb-tab{border:1px solid var(--line2);color:var(--muted);font:600 13px Inter,sans-serif;padding:9px 14px;border-radius:999px;min-height:40px;display:inline-flex;align-items:center;transition:all .2s}
.lb-tab:hover{color:var(--text);border-color:#3A3A42}
.lb-tab[aria-selected=true]{background:var(--gold);border-color:var(--gold);color:var(--bg)}
.lb-wrap{padding-top:64px;padding-bottom:120px}
.lb-panel{display:flex;flex-direction:column;gap:40px}
.lb-panel[hidden]{display:none}
.lb-title{font-size:clamp(46px,9vw,140px)}
.lb-panel.show{animation:panelIn .6s var(--ease) both}
.lb-note{margin:0;max-width:420px;font-size:14px}
.podium{display:grid;grid-template-columns:1fr 1.15fr 1fr;align-items:end;gap:14px}
.pod{position:relative;overflow:hidden;display:flex;flex-direction:column;align-items:center;text-align:center;gap:10px;padding:30px 20px 26px;border:1px solid var(--line2);border-radius:18px;background:var(--panel);color:var(--text);transition:border-color .3s,transform .35s var(--ease)}
.pod:hover{color:var(--text);transform:translateY(-4px);border-color:#4A3D1E}
.pod-1{padding-top:44px;background:radial-gradient(120% 90% at 50% 0%,rgba(201,162,74,.20),rgba(8,8,10,0) 60%),var(--panel);border-color:rgba(201,162,74,.45)}
.slot-0{order:2}.slot-1{order:1}.slot-2{order:3}
.pod-place{position:absolute;top:10px;left:16px;font-size:44px;line-height:1;color:#3A3A42}
.pod-1 .pod-place{color:var(--gold)}.pod-2 .pod-place{color:#BFC3CA}.pod-3 .pod-place{color:#C08A5B}
.pod-av{width:76px;height:76px;font-size:26px;border-width:2px}
.pod-1 .pod-av{width:96px;height:96px;font-size:32px;border-color:var(--gold);animation:ringPulse 2.8s ease-out infinite}
.pod-2 .pod-av{border-color:#BFC3CA}.pod-3 .pod-av{border-color:#C08A5B}
.pod-name{font-size:26px;font-weight:700;line-height:1;max-width:100%;overflow-wrap:anywhere}
.pod-1 .pod-name{font-size:32px}
.pod-val{color:var(--muted);font-size:13px}
.pod-val b{font-size:40px;color:var(--text);line-height:1}
.pod-1 .pod-val b{font-size:52px;color:var(--gold2)}
.pod-detail{font-size:12px;color:var(--dim)}
.lb-row{display:flex;align-items:center;gap:16px;padding:14px 6px}
.lb-place{font-size:28px;width:34px;color:#71717A;font-variant-numeric:tabular-nums}
.lb-who{flex:1;min-width:0;display:flex;flex-direction:column;gap:8px}
.lb-name{font-size:22px;font-weight:700;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lb-name:hover{color:var(--gold2)}
.lb-bar{height:4px;border-radius:2px;background:#17171B;overflow:hidden;max-width:420px}
.lb-val{display:flex;flex-direction:column;align-items:flex-end;text-align:right;gap:2px}
.lb-val b{font-size:26px;font-variant-numeric:tabular-nums}
.lb-val span{font-size:12px;color:var(--dim)}
.lbchips{display:flex;flex-wrap:wrap;gap:8px}
.lbchip{display:inline-flex;align-items:center;gap:8px;border:1px solid var(--line2);background:var(--panel);color:var(--muted);font-size:12px;font-weight:600;padding:5px 12px 5px 6px;border-radius:999px}
.lbchip:hover{color:var(--text);border-color:#4A3D1E}
.lbchip span{display:inline-grid;place-items:center;width:22px;height:22px;border-radius:50%;font-size:14px;color:var(--bg);background:#71717A}
.lbchip-1 span{background:var(--gold)}.lbchip-2 span{background:#BFC3CA}.lbchip-3 span{background:#C08A5B}
@media (max-width:700px){
  .podium{grid-template-columns:1fr}
  .slot-0,.slot-1,.slot-2{order:0}
  .pod{flex-direction:row;text-align:left;padding:18px 18px 18px 54px;gap:14px}
  .pod-1{padding-top:18px}
  .pod-place{top:50%;transform:translateY(-50%);left:16px;font-size:32px}
  .pod-av,.pod-1 .pod-av{width:56px;height:56px;font-size:20px}
  .pod-name,.pod-1 .pod-name{font-size:22px;flex:1;min-width:0}
  .pod-val{display:flex;flex-direction:column;align-items:flex-end}
  .pod-val b,.pod-1 .pod-val b{font-size:30px}
  .pod-detail{display:none}
  .lb-row .av{display:none}
  .lb-val span{display:none}
}

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

/* ---------- event pages, live card, head to head ---------- */
.res:has(+ .res-note){border-bottom-color:transparent}
.res-note b{color:var(--text)}
.btn-sm{min-height:38px;padding:9px 16px;font-size:12px}
.fhero-main .btns{margin-top:18px}
.ev-link{color:inherit}.ev-link:hover{color:var(--gold2)}
.evchips{display:flex;flex-wrap:wrap;gap:10px;margin-top:26px}
.evchip{display:flex;flex-direction:column;gap:2px;padding:12px 18px;border:1px solid var(--line2);border-radius:14px;background:rgba(17,17,21,.7);min-width:120px}
.evchip-l{font-size:10px;font-weight:700;letter-spacing:.22em;color:var(--dim)}
.evchip-v{font-size:30px;line-height:1}
.evchip.gold .evchip-v{color:var(--gold)}
.evchip-sm .evchip-v{font-size:20px;line-height:1.15;padding-top:4px}
.evchip.red .evchip-v{color:var(--red)}
.ev-notes-lg{font-size:16px;max-width:760px;margin:48px 0 0;color:var(--muted)}
.ev-card{padding-top:56px}
.tag-upset{display:inline-block;font-size:10px;font-weight:800;letter-spacing:.18em;color:#fff;background:var(--redc);border-radius:4px;padding:2px 6px;margin-right:4px}
.awgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px;margin-top:28px}
.awcard{display:flex;flex-direction:column;gap:8px;padding:22px;border:1px solid var(--line2);border-radius:16px;background:var(--panel)}
.awcard-l{font-size:11px;font-weight:700;letter-spacing:.2em;color:var(--dim)}
.awcard-n{font-size:24px;line-height:1.05}
.awcard-n a{color:var(--text)}
.fan-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;margin-top:28px}
.fan-box{display:flex;flex-direction:column;gap:10px;padding:24px;border:1px solid var(--line2);border-radius:16px;background:var(--panel)}
.fan-big{font-size:38px;line-height:1;color:var(--gold)}
.fan-team{list-style:none;margin:0;padding:0;display:flex;flex-direction:column}
.fan-team li{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:9px 0;border-bottom:1px solid var(--line);font-weight:600}
.fan-team li a{color:var(--text)}

.live-card{padding-top:48px}
.lvbout{position:relative;display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:16px;padding:20px 18px;border:1px solid var(--line);border-radius:16px;background:var(--panel);margin-bottom:10px;transition:border-color .3s}
.lvbout-next{border-color:rgba(229,72,77,.55);box-shadow:0 0 0 1px rgba(229,72,77,.18) inset}
.lvbout-done{background:var(--panel2)}
.lv-spot{position:absolute;top:-9px;left:18px;font-size:10px;font-weight:800;letter-spacing:.22em;color:var(--bg);background:var(--gold);border-radius:4px;padding:2px 8px}
.lv-side{display:flex;align-items:center;gap:12px;min-width:0}
.lv-right{flex-direction:row-reverse;text-align:right}
.lv-n{display:flex;flex-direction:column;min-width:0}
.lv-n .d{font-size:22px;line-height:1.05;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lv-n .d a{color:var(--text)}
.lv-n .muted{font-size:12px}
.lv-loss{opacity:.45}
.lv-win .lv-n .d a{color:var(--gold2)}
.lv-mid{display:flex;justify-content:center;min-width:110px}
.lv-vs{font-size:20px;color:var(--dim)}
.lv-res{font-size:11px;font-weight:800;letter-spacing:.16em;padding:6px 10px;border-radius:999px;border:1px solid var(--line2);white-space:nowrap}
.lv-next{display:flex;align-items:center;gap:8px;font-size:11px;font-weight:800;letter-spacing:.2em;color:var(--red)}
.live-empty{text-align:left}
.live-empty p{max-width:560px;margin:0 0 22px}

.cmp-pick{padding-top:48px;padding-bottom:0}
.cmp-form{display:grid;grid-template-columns:1fr auto 1fr auto;align-items:end;gap:14px}
.cmp-form label{display:flex;flex-direction:column;gap:8px}
.cmp-form select{appearance:none;background:var(--panel) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8'%3E%3Cpath d='M1 1l5 5 5-5' fill='none' stroke='%23A1A1AA' stroke-width='2'/%3E%3C/svg%3E") no-repeat right 16px center;color:var(--text);border:1px solid var(--line2);border-radius:12px;padding:14px 40px 14px 16px;font:600 15px Inter,system-ui,sans-serif;min-height:50px}
.cmp-form select:focus{border-color:var(--gold);outline:none}
.cmp-vs{font-size:22px;color:var(--dim);padding-bottom:12px}
.cmp{padding-top:64px}
.cmp-top{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:20px;margin-bottom:34px}
.cmp-head{display:flex;flex-direction:column;align-items:flex-start;gap:8px}
.cmp-head.r{align-items:flex-end;text-align:right}
.av-lg{width:84px;height:84px;font-size:26px}
.cmp-name{font-size:clamp(30px,4vw,48px);line-height:.95}
.cmp-name a{color:var(--text)}
.cmp-rec{font-size:28px;color:var(--gold)}
.cmp-big{font-size:clamp(40px,6vw,72px);color:var(--line2)}
.cmp-rows{border-top:1px solid var(--line)}
.cmp-row{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:16px;padding:14px 6px;border-bottom:1px solid var(--line)}
.cmp-l{font-size:11px;font-weight:700;letter-spacing:.22em;color:var(--dim);text-align:center;min-width:180px}
.cmp-v{font-size:26px;color:var(--muted)}
.cmp-v.r{text-align:right}
.cmp-v.win{color:var(--text)}
.cmp-v.win::after{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--gold);margin:0 0 6px 8px}
.cmp-v.r.win::after{display:none}
.cmp-v.r.win::before{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--gold);margin:0 8px 6px 0}
@media (max-width:720px){
  .lvbout{grid-template-columns:1fr;gap:10px;padding:22px 16px 16px}
  .lv-right{flex-direction:row;text-align:left}
  .lv-mid{justify-content:flex-start;min-width:0}
  .cmp-form{grid-template-columns:1fr}
  .cmp-vs{display:none}
  .cmp-top{grid-template-columns:1fr 1fr}
  .cmp-big{display:none}
  .cmp-l{min-width:0;font-size:10px;letter-spacing:.14em}
  .cmp-v{font-size:20px}
}

/* ---------- bets & fantasy ---------- */
.ptabs{display:inline-flex;align-self:flex-start;width:max-content;gap:4px;margin-top:26px;padding:4px;border:1px solid var(--line2);border-radius:999px;background:rgba(17,17,21,.7)}
.ptabs a{font-size:12px;font-weight:700;letter-spacing:.18em;color:var(--muted);padding:10px 18px;border-radius:999px}
.ptabs a[aria-current]{background:var(--gold);color:var(--bg)}
.red{color:var(--red)!important}.green{color:var(--green)!important}
.sbmarket{padding-top:72px;padding-bottom:72px}
.sb-states{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.sb-state{display:inline-flex;align-items:center;gap:8px;font-size:11px;font-weight:800;letter-spacing:.22em;padding:5px 10px;border-radius:999px;border:1px solid var(--line2)}
.sb-state.open{color:var(--green);border-color:rgba(61,214,140,.35)}
.sb-state.locked{color:var(--gold)}
.sb-state.done{color:var(--muted)}
.sbfights{display:flex;flex-direction:column;gap:12px;margin-top:30px}
.sbfight{border:1px solid var(--line);border-radius:16px;background:var(--panel);padding:16px 18px}
.sb-done{background:var(--panel2)}
.sb-spot{display:block;font-size:10px;font-weight:800;letter-spacing:.22em;color:var(--dim);margin-bottom:12px}
.sb-grid{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:16px}
.sb-side{display:grid;grid-template-columns:auto 1fr auto;align-items:center;gap:12px;min-width:0}
.sb-side.r{grid-template-columns:auto 1fr auto;direction:rtl}
.sb-side.r > *{direction:ltr}
.sb-side.r .sb-who{text-align:right}
.sb-who{display:flex;flex-direction:column;min-width:0}
.sb-name{font-size:22px;line-height:1.05;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sb-name a{color:var(--text)}
.sb-tag{font-size:12px;color:var(--gold);letter-spacing:.06em}
.sb-who .muted{font-size:12px}
.sb-price{display:flex;flex-direction:column;align-items:center;gap:4px}
.odds{font-size:30px;line-height:1;color:var(--gold2);background:rgba(201,162,74,.08);border:1px solid rgba(201,162,74,.28);border-radius:10px;padding:6px 10px;min-width:74px;text-align:center}
.omove{font-size:10px;margin-left:4px}.omove.down{color:var(--green)}.omove.up{color:var(--red)}
.sb-price{flex-direction:row;flex-wrap:wrap;justify-content:center;max-width:150px}
.sb-methods{display:flex;flex-wrap:wrap;gap:6px 8px;font-size:10px;letter-spacing:.08em;color:var(--dim);width:100%;justify-content:center}
.sb-methods b{color:var(--muted);font-weight:700}
.sb-vs{font-size:18px;color:var(--dim)}
.sb-money{display:flex;flex-direction:column;gap:6px;margin-top:14px}
.sb-money .muted{font-size:12px;text-align:center}
.sb-bar{display:block;height:6px;border-radius:999px;background:rgba(127,176,255,.35);overflow:hidden}
.sb-bar span{display:block;height:100%;background:var(--gold)}
.sb-result{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:6px 16px}
.sb-result .d{font-size:22px}
.sb-result .muted{font-size:11px;font-weight:700;letter-spacing:.16em}
.sb-result .def{font-size:12px;color:var(--dim);margin:0 4px}
.pgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:14px;margin-top:30px}
.pgrid-3{grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
@media (max-width:520px){.phero-title{font-size:min(13vw,72px)!important}}
.pbox{border:1px solid var(--line2);border-radius:16px;background:var(--panel);padding:22px;display:flex;flex-direction:column;gap:10px}
.pbox-wide{grid-column:1/-1}
.pbox-h{margin:0;font-size:11px;font-weight:800;letter-spacing:.24em;color:var(--gold)}
.pbox-note{margin:4px 0 0;font-size:12px;color:var(--dim)}
.prank{list-style:none;margin:0;padding:0}
.prank li{display:flex;align-items:center;gap:12px;padding:9px 0;border-bottom:1px solid var(--line)}
.prank li:last-child{border-bottom:0}
.prank-n{width:22px;font:800 16px var(--d);color:var(--dim)}
.prank li:first-child .prank-n{color:var(--gold)}
.prank-who{flex:1;display:flex;flex-direction:column;min-width:0;font-weight:600}
.prank-who a{color:var(--text)}
.prank-who .muted{font-size:12px;font-weight:500}
.prank-v{font-weight:700;white-space:nowrap}
.fan-top{display:flex;flex-direction:column;gap:6px;margin:0 0 10px}
.tlist{display:flex;flex-direction:column;margin-top:28px;border-top:1px solid var(--line)}
.trow{display:grid;grid-template-columns:44px 1fr auto;align-items:center;gap:16px;padding:16px 6px;border-bottom:1px solid var(--line)}
.trow:hover{background:var(--panel2)}
.trow-n{font-size:26px;color:var(--dim)}
.trow:first-child .trow-n{color:var(--gold)}
.trow-main{display:flex;flex-direction:column;gap:8px;min-width:0}
.trow-who{font-size:24px;line-height:1}
.tpicks{display:flex;flex-wrap:wrap;gap:6px}
.tpick{display:inline-flex;align-items:center;gap:8px;font-size:12px;font-weight:600;padding:5px 10px;border-radius:999px;border:1px solid var(--line2);color:var(--muted)}
.tpick b{color:var(--text);font-weight:700}
.tpick.cap{border-color:rgba(201,162,74,.5);color:var(--gold2)}
.trow-pts{font-size:34px;line-height:1;text-align:right}
.trow-pts span{display:block;font:700 10px Inter,sans-serif;letter-spacing:.2em;color:var(--dim)}
.ptable .td-price{font-size:22px;color:var(--gold)}
.ptable tr[hidden]{display:none}
.ptable:not(.pall):not(.psearching) tr.pmore{display:none}
.pshow{margin-top:18px}
@media (max-width:720px){
  .sb-grid{grid-template-columns:1fr;gap:12px}
  .sb-vs{display:none}
  .sb-side.r{direction:ltr}
  .sb-side.r .sb-who{text-align:left}
  .trow{grid-template-columns:30px 1fr auto;gap:10px}
  .trow-who{font-size:20px}
  .trow-pts{font-size:26px}
}

/* ---------- account chip ---------- */
.nav-user{position:relative;display:flex;align-items:center}
.nav-user:empty{display:none}
.nu-login{display:inline-flex;align-items:center;gap:8px}
.nu-chip{display:flex;align-items:center;gap:8px;padding:5px 14px 5px 5px;border:1px solid var(--line2);border-radius:999px;background:transparent;color:var(--text);font:700 13px Inter,sans-serif;cursor:pointer;min-height:40px;max-width:220px;white-space:nowrap}
.nu-name{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nu-chip img,.nu-av{flex-shrink:0}
.nu-head{display:flex;flex-direction:column;gap:2px;padding:10px 12px 12px;border-bottom:1px solid var(--line2);margin-bottom:6px}
.nu-head b{font-size:15px;line-height:1.25;word-break:break-word}
.nu-head span{font-size:12px;color:var(--muted)}
.logged-in .nav-cta{display:none}
.tmgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px;margin-top:10px}
.tmcard{display:flex;flex-direction:column;gap:12px;padding:22px;border:1px solid var(--line2);border-radius:18px;background:var(--panel);color:var(--text);transition:border-color .2s}
.tmcard:hover{border-color:var(--gold);color:var(--text)}
.tmcard-name{font-size:30px;line-height:1}
.tm-tag{display:inline-block;font:700 12px Inter,sans-serif;letter-spacing:.14em;color:var(--bg);background:var(--gold);padding:3px 8px;border-radius:6px;margin-right:10px;vertical-align:6px}
.tmcard-faces{display:flex;gap:4px;flex-wrap:wrap}
.tmcard-stats{display:flex;gap:22px;border-top:1px solid var(--line);padding-top:12px}
.tmcard-stats span{display:flex;flex-direction:column;font-size:10px;font-weight:700;letter-spacing:.14em;color:var(--dim)}
.tmcard-stats b{font-size:26px;color:var(--text);letter-spacing:0}
.tm-who{flex:1;display:flex;flex-direction:column;min-width:0}
.fteam{display:inline-flex;align-items:center;gap:8px;margin-top:12px;padding:6px 12px 6px 10px;border:1px solid rgba(201,162,74,.5);border-radius:999px;color:var(--text);font-weight:700;font-size:14px}
.fteam .tm-tag{vertical-align:0;margin-right:0}
.stgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px;margin-top:10px}
.stcard{display:flex;align-items:center;gap:14px;padding:14px;border:1px solid var(--line2);border-radius:16px;background:var(--panel)}
.stcard-t{display:flex;flex-direction:column;min-width:0}.stcard-n{font-size:22px;line-height:1.1}
.stcontact{max-width:none}
.lv-cancel{font:700 11px Inter,sans-serif;letter-spacing:.16em;color:var(--red);border:1px solid rgba(229,72,77,.5);padding:4px 10px;border-radius:999px}
.lvbout-cancel .lv-side{opacity:.4;text-decoration:line-through;text-decoration-color:rgba(229,72,77,.6)}
.fformer{display:flex;flex-wrap:wrap;align-items:center;gap:6px 8px;margin:12px 0 0;font-size:13px}
.fformer-l{font-weight:700;letter-spacing:.16em;font-size:11px;color:var(--dim);margin-right:4px}
.fformer-n{padding:4px 10px;border:1px solid var(--line2);border-radius:999px;color:var(--muted);font-weight:600}
@media (max-width:1080px){
  .nav-user{margin-left:auto;position:static}
  .nav-cta + .nav-user{margin-left:0}
  .logged-in .nav-user{margin-left:auto}
  .nav-user + .nav-burger{margin-left:0}
}
@media (max-width:1320px){.nu-chip{max-width:160px}}
.badge-double{background:linear-gradient(90deg,var(--gold),var(--gold2));color:var(--bg)}
.brand{flex-shrink:0}
.nav-links{min-width:0}
.nu-chip[aria-expanded="true"]{border-color:var(--gold)}
.nu-chip img,.nu-av{width:30px;height:30px;border-radius:50%;object-fit:cover;background:var(--panel2);display:grid;place-items:center;font-size:11px;color:var(--gold)}
.nu-menu{position:absolute;right:0;top:calc(100% + 10px);min-width:240px;max-width:min(320px,calc(100vw - 32px));padding:8px;border:1px solid var(--line2);border-radius:14px;background:rgba(12,12,15,.98);box-shadow:0 20px 50px rgba(0,0,0,.55);display:flex;flex-direction:column;z-index:45}
.nu-menu[hidden]{display:none}
.nu-menu a,.nu-menu button{display:flex;flex-direction:column;gap:2px;padding:10px 12px;border-radius:10px;color:var(--text);background:none;border:0;text-align:left;font:inherit;cursor:pointer}
.nu-menu a:hover,.nu-menu button:hover{background:var(--panel2)}
.nu-menu .nd-t{font-size:14px;font-weight:700}
.nu-sep{height:1px;background:var(--line2);margin:6px 4px}
.nu-menu{background:#0E0E12!important;z-index:60;isolation:isolate}
@media (max-width:1080px){
  .nu-menu{left:max(16px,env(safe-area-inset-left,0px));right:max(16px,env(safe-area-inset-right,0px));top:calc(100% + 6px);
    min-width:0;max-width:none;max-height:calc(100vh - 110px);overflow-y:auto}
}
@media (max-width:600px){.nu-name{display:none}.nu-chip{padding-right:8px}}
.pfo-toast{position:fixed;left:50%;bottom:max(20px,env(safe-area-inset-bottom,0px));transform:translateX(-50%);z-index:80;max-width:min(92vw,520px);padding:12px 18px;border-radius:12px;background:#16161B;border:1px solid var(--line2);color:var(--text);font-weight:600;box-shadow:0 14px 40px rgba(0,0,0,.5)}
.pfo-toast.bad{border-color:rgba(229,72,77,.6)}
.pfo-toast.good{border-color:rgba(61,214,140,.6)}
"""

V2_CSS = r"""
/* ===== added with logins ===== */
.btn-ghost{background:transparent}
button.btn{cursor:pointer;font-family:inherit}
button.btn:disabled{opacity:.45;cursor:not-allowed}
.btn.is-on{border-color:var(--gold);color:var(--gold2)}
.h-s{margin:0;font-size:clamp(28px,3.4vw,40px);line-height:.95}
.green{color:var(--green)!important}.red{color:var(--red)!important}
.is-busy{opacity:.55;pointer-events:none}
a.pfo-toast{display:block;color:var(--text)}
.pfo-toast.ok{border-color:rgba(61,214,140,.6)}.pfo-toast.warn{border-color:rgba(201,162,74,.7)}
.backlink{display:inline-block;margin:18px 0 0 max(28px,calc((100vw - 1240px)/2 + 28px));font-size:13px;font-weight:600;letter-spacing:.06em;color:var(--muted)}
.backlink:hover{color:var(--gold2)}
.foot-links{display:flex;gap:16px;align-items:center;font-size:13px}
.foot-links a{color:var(--muted)}.foot-links a:hover{color:var(--gold2)}
.tzbtn{background:none;border:1px solid var(--line2);color:var(--muted);border-radius:999px;padding:6px 12px;font:600 12px Inter,sans-serif;cursor:pointer}
time[title]{text-decoration:underline dotted rgba(161,161,170,.45);text-underline-offset:3px}

/* live bar */
.livebar{display:flex;align-items:center;gap:12px;padding:10px 28px;background:linear-gradient(90deg,rgba(229,72,77,.22),rgba(229,72,77,.06));border-bottom:1px solid rgba(229,72,77,.4);color:var(--text);font-size:13px}
.livebar b{letter-spacing:.14em;color:#FF8F92}.livebar-t{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--muted)}
.livebar-go{font-weight:700;letter-spacing:.12em;color:var(--text)}
.watch-frame{position:relative;aspect-ratio:16/9;margin-top:26px;border:1px solid var(--line2);border-radius:16px;overflow:hidden;background:#000}
.watch-frame iframe{position:absolute;inset:0;width:100%;height:100%;border:0}

/* login box */
.loginbox{border:1px solid var(--line2);border-radius:18px;background:var(--panel);padding:28px;display:flex;flex-direction:column;align-items:flex-start;gap:14px;max-width:560px}
.loginbox p{margin:0}

/* my pfo */
.mehero{padding:56px 0 20px;border-bottom:1px solid var(--line)}
.mehero-in{display:flex;gap:28px;align-items:center;flex-wrap:wrap}
.mehero-t{display:flex;flex-direction:column;gap:6px}
.mejump{display:flex;flex-wrap:wrap;gap:6px 18px;padding:22px 0;font-size:12px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;border-bottom:1px solid var(--line)}
.mejump a{color:var(--muted)}.mejump a:hover{color:var(--gold2)}
.mesec{scroll-margin-top:90px}
.spinbox{display:grid;grid-template-columns:minmax(0,320px) minmax(0,1fr);gap:24px;align-items:center}
@media (max-width:700px){.spinbox{grid-template-columns:minmax(0,1fr)}}
.spinwheel{position:relative;max-width:320px;width:100%;margin:0 auto}
.spinwheel canvas{width:100%;height:auto;aspect-ratio:1;display:block}
.spinptr{position:absolute;top:-6px;left:50%;transform:translateX(-50%);width:0;height:0;border-left:13px solid transparent;border-right:13px solid transparent;border-top:24px solid var(--gold);z-index:2}
.spinside{display:flex;flex-direction:column;gap:10px;align-items:flex-start;min-width:0}
.upglist li{align-items:center}
.mesec .head-t{margin-bottom:20px}
.nextfight{border:1px solid rgba(201,162,74,.5);border-radius:18px;background:linear-gradient(135deg,rgba(201,162,74,.12),transparent 60%),var(--panel);padding:24px;display:flex;flex-direction:column;gap:12px;align-items:flex-start;margin-bottom:16px}
.nf-row{display:flex;align-items:center;gap:12px;flex-wrap:wrap;font-size:26px}
.nf-vs{color:var(--gold);font-size:18px}
.sugrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.sucard{border:1px solid var(--line2);border-radius:16px;background:var(--panel);padding:20px;display:flex;flex-direction:column;gap:8px;align-items:flex-start}
.sucard-on{border-color:rgba(61,214,140,.5)}
.sucard-t{font-size:26px;line-height:1}
.mefighter{display:flex;align-items:center;gap:20px;flex-wrap:wrap}
.ftiles-4{display:grid;grid-template-columns:repeat(4,1fr);border-top:1px solid var(--line);border-bottom:1px solid var(--line);margin-top:18px}
.ftiles-4 .ftile{padding:22px 6px}.ftiles-4 .ftile-v{font-size:40px}
.mefan{display:flex;align-items:baseline;gap:14px;margin-bottom:14px}
.tasks{list-style:none;margin:16px 0;padding:0;display:flex;flex-direction:column;gap:10px}
.task{display:flex;align-items:center;gap:14px;padding:12px 14px;border:1px solid var(--line2);border-radius:12px;background:var(--panel)}
.task-done{border-color:rgba(61,214,140,.4)}
.task-e{font-size:20px}.task-t{flex:1;display:flex;flex-direction:column;gap:6px;font-weight:600}
.task-bar,.ach-bar,.sbar-h{display:block;height:4px;border-radius:4px;background:var(--line2);overflow:hidden}
.task-bar span,.ach-bar span,.sbar-h span{display:block;height:100%;background:var(--gold);border-radius:4px}
.task-done .task-bar span{background:var(--green)}
.task-v{font-size:13px;color:var(--muted);white-space:nowrap}
.follist{list-style:none;margin:0;padding:0}
.folrow{display:flex;align-items:center;gap:14px;padding:12px 0;border-bottom:1px solid var(--line)}
.folrow-t{flex:1;display:flex;flex-direction:column;min-width:0}
.achgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px}
.ach{display:flex;gap:12px;align-items:center;padding:14px;border:1px solid var(--line2);border-radius:14px;background:var(--panel);opacity:.62}
.ach-on{opacity:1}
.ach-e{font-size:26px;filter:grayscale(1)}.ach-on .ach-e{filter:none}
.ach-t{flex:1;display:flex;flex-direction:column;gap:3px;font-size:13px;color:var(--muted);min-width:0}.ach-t b{color:var(--text);font-size:14px}
.ach-r{font:700 10px Inter,sans-serif;letter-spacing:.14em;text-align:right;color:var(--dim)}
.ach-rare.ach-on{border-color:rgba(59,130,246,.55)}.ach-epic.ach-on{border-color:rgba(168,85,247,.6)}.ach-legendary.ach-on{border-color:rgba(212,168,76,.8);box-shadow:0 0 24px rgba(212,168,76,.15)}
.ach-rare .ach-r{color:#7FB0FF}.ach-epic .ach-r{color:#C99BFF}.ach-legendary .ach-r{color:var(--gold2)}
.prank-plain li{gap:14px}
.prank-v{white-space:nowrap}
.prank li.is-me{background:rgba(201,162,74,.08)}

/* home panel */
.homepanel{border-bottom:1px solid var(--line);background:linear-gradient(180deg,rgba(201,162,74,.06),transparent);padding:30px 0}
.hp-head{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:8px 20px;margin-bottom:16px}
.hp-hi{font-size:28px}
.hp-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}
.hp-card{border:1px solid var(--line2);border-radius:16px;background:var(--panel);padding:18px;display:flex;flex-direction:column;gap:6px;align-items:flex-start;color:var(--text);transition:border-color .2s}
a.hp-card:hover{border-color:var(--gold);color:var(--text)}
.hp-fight{border-color:rgba(201,162,74,.55)}
.hp-big{font-size:24px;line-height:1}
.hp-cd{font:800 18px var(--d);color:var(--gold2)}

/* sportsbook betting */
.obtn{background:none;border:1px solid transparent;border-radius:10px;padding:4px 8px;margin:-4px -8px;color:inherit;font:inherit;cursor:pointer;transition:border-color .2s,background .2s}
.obtn:hover{border-color:rgba(201,162,74,.6);background:rgba(201,162,74,.06)}
.obtn.is-picked{border-color:var(--gold);background:rgba(201,162,74,.18)}
.obtn:disabled{cursor:default;opacity:.6}
.obtn-m{margin:0;padding:2px 6px}
.flash-up{animation:flashUp 1.6s ease}.flash-down{animation:flashDown 1.6s ease}
@keyframes flashUp{0%{color:var(--green);text-shadow:0 0 12px rgba(61,214,140,.6)}100%{color:inherit}}
@keyframes flashDown{0%{color:var(--red);text-shadow:0 0 12px rgba(229,72,77,.6)}100%{color:inherit}}
.sb-how{margin-top:18px}
.slip{position:fixed;right:20px;bottom:20px;z-index:60;width:min(380px,calc(100vw - 32px));max-height:min(70vh,560px);display:flex;flex-direction:column;background:#111116;border:1px solid var(--gold);border-radius:18px;box-shadow:0 20px 60px rgba(0,0,0,.6);overflow:hidden}
.slip-head{display:flex;align-items:center;gap:10px;padding:14px 16px;border-bottom:1px solid var(--line2)}
.slip-head .d{font-size:20px;letter-spacing:.06em}.slip-head .muted{flex:1;font-size:12px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.slip-x{background:none;border:0;color:var(--muted);font:700 11px Inter,sans-serif;letter-spacing:.12em;cursor:pointer}
.slip-legs{list-style:none;margin:0;padding:6px 16px;overflow:auto;flex:1}
.slip-legs li{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--line)}
.slip-legs li span:first-child{flex:1;font-weight:600}
.slip-legs button{background:none;border:0;color:var(--dim);cursor:pointer;font-size:14px;padding:4px}
.slip-foot{display:flex;flex-wrap:wrap;align-items:center;gap:10px;padding:14px 16px;border-top:1px solid var(--line2)}
.slip-foot label{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--muted)}
.slip-foot input,.mt-form input,.pfo-dialog input,.pfo-dialog select,.pfo-dialog textarea,.rfilters input,.rfilters select,.soc-compose textarea,.soc-tools select,#aevent{background:#0A0A0D;border:1px solid var(--line2);border-radius:10px;color:var(--text);padding:10px 12px;font:15px Inter,sans-serif;min-height:42px}
.slip-foot input{width:110px}
.slip-foot .muted{flex:1;font-size:13px}
.slip-note{margin:0;padding:0 16px 12px;font-size:11px;color:var(--dim)}
.mybets{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.mybet{border:1px solid var(--line2);border-radius:14px;background:var(--panel);padding:16px;display:flex;flex-direction:column;gap:10px}
.mybet-won{border-color:rgba(61,214,140,.45)}.mybet-lost{opacity:.7}.mybet-cashed{border-color:rgba(201,162,74,.5)}
.mybet-h{display:flex;justify-content:space-between;gap:10px;font-size:12px;font-weight:700;letter-spacing:.1em;color:var(--muted)}
.mybet-legs{list-style:none;margin:0;padding:0}
.leg{display:flex;justify-content:space-between;gap:10px;padding:6px 0;border-bottom:1px solid var(--line);font-size:14px}
.leg-won span:first-child::before{content:"✓ ";color:var(--green)}.leg-lost span:first-child::before{content:"✕ ";color:var(--red)}
.mybet-f{display:flex;justify-content:space-between;align-items:center;gap:10px;font-size:14px}

/* fantasy manager */
.myteam .head-t{margin-bottom:20px}
.mt-top{display:flex;flex-wrap:wrap;align-items:center;gap:16px 40px;margin-bottom:18px}
.mt-num{display:flex;flex-direction:column}
.mt-rules{flex:1 1 280px;margin:0;color:var(--muted);font-size:14px}
.mypicks{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px}
.mypick{display:flex;flex-wrap:wrap;align-items:center;gap:10px;padding:16px;border:1px solid var(--line2);border-radius:14px;background:var(--panel)}
.mypick.is-cap{border-color:var(--gold)}
.mypick-empty{border-style:dashed;justify-content:center;min-height:84px}
.mypick-n{flex:1 1 100%;display:flex;flex-direction:column;gap:2px;font:800 22px var(--d)}
.mypick-n .muted{font:500 13px Inter,sans-serif}
.cap{font:700 11px Inter,sans-serif;letter-spacing:.12em;color:var(--gold)}
.mt-form{display:flex;gap:8px}.mt-form input{flex:1;min-width:0}
.td-act{text-align:right;white-space:nowrap}
.ptable .btn-sm{min-height:32px;padding:6px 12px}

/* shop */
.shopgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px;margin-top:26px}
.shopitem{display:flex;gap:16px;padding:20px;border:1px solid var(--line2);border-radius:16px;background:var(--panel);align-items:flex-start}
.shopitem.r-rare{border-color:rgba(59,130,246,.4)}.shopitem.r-epic{border-color:rgba(168,85,247,.45)}.shopitem.r-legendary{border-color:rgba(212,168,76,.6)}
.shop-e{font-size:30px;line-height:1}
.shop-t{flex:1;min-width:0}.shop-t h3{margin:0;font-size:22px;line-height:1}.shop-t p{margin:6px 0 8px;font-size:13px;color:var(--muted)}
.shop-tags{display:flex;gap:6px;flex-wrap:wrap}
.rar,.staffchk{font:700 10px Inter,sans-serif;letter-spacing:.14em;padding:3px 8px;border-radius:999px;border:1px solid var(--line2);color:var(--muted)}
.r-rare .rar{color:#7FB0FF;border-color:rgba(59,130,246,.5)}.r-epic .rar{color:#C99BFF;border-color:rgba(168,85,247,.5)}.r-legendary .rar{color:var(--gold2);border-color:rgba(212,168,76,.6)}
.shop-buy{display:flex;flex-direction:column;align-items:flex-end;gap:8px}
.shop-p{font-size:22px;color:var(--gold2);white-space:nowrap}

/* dialogs */
.pfo-dialog{border:1px solid var(--line2);border-radius:20px;background:#111116;color:var(--text);padding:0;width:min(480px,calc(100vw - 32px));box-shadow:0 30px 80px rgba(0,0,0,.6)}
.pfo-dialog::backdrop{background:rgba(0,0,0,.65);backdrop-filter:blur(3px)}
.dlg-in{padding:26px;display:flex;flex-direction:column;gap:14px}
.dlg-in p{margin:0}
.dlg-field{display:flex;flex-direction:column;gap:6px}.dlg-field[hidden]{display:none}
.dlg-field label{font-size:12px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
.dlg-results{display:flex;flex-wrap:wrap;gap:6px}
.dlg-results button{background:var(--panel2);border:1px solid var(--line2);color:var(--text);border-radius:999px;padding:8px 12px;cursor:pointer;font:600 13px Inter,sans-serif}
.dlg-results button:hover{border-color:var(--gold)}
.dlg-total{font-weight:700;color:var(--gold2)}
.fbox{display:flex;align-items:center;gap:8px;font-size:14px;color:var(--muted);cursor:pointer}
.fbox input{width:18px;height:18px;accent-color:var(--gold);min-height:0}

/* social */
.soc-wrap{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:30px;align-items:start}
.soc-side{display:flex;flex-direction:column;gap:14px;position:sticky;top:96px;margin-top:64px}
.soc-compose{border:1px solid var(--line2);border-radius:18px;background:var(--panel);padding:16px;display:flex;flex-direction:column;gap:10px;margin-bottom:22px}
.soc-compose textarea{width:100%;resize:vertical}
.soc-tools{display:flex;flex-wrap:wrap;align-items:center;gap:10px}.soc-tools .muted{flex:1;font-size:12px;text-align:right}
#socpreview img{max-height:200px;border-radius:12px;display:block;margin-bottom:6px}
.soc-sort{margin:0 0 16px}
.feed{display:flex;flex-direction:column;gap:12px}
.post{border:1px solid var(--line2);border-radius:18px;background:var(--panel);padding:18px;display:flex;flex-direction:column;gap:10px;scroll-margin-top:100px}
.post.is-target{border-color:var(--gold)}
.post-h{display:flex;align-items:center;gap:12px}
.post-who{flex:1;min-width:0;display:flex;flex-wrap:wrap;align-items:center;gap:6px}.post-who a{font-size:20px;color:var(--text)}
.post-t{font-size:12px}
.sbadge{font:700 10px Inter,sans-serif;letter-spacing:.14em;padding:2px 7px;border-radius:999px;border:1px solid var(--line2);color:var(--muted)}
.sbadge-staff{color:var(--gold2);border-color:rgba(201,162,74,.6)}.sbadge-fighter{color:#7FB0FF;border-color:rgba(59,130,246,.5)}
.post-tag{font-size:12px;font-weight:700;color:var(--gold2);letter-spacing:.06em}
.post-text{margin:0;white-space:pre-wrap;word-wrap:break-word;font-size:16px}
.post-img{border-radius:12px;max-height:520px;object-fit:cover;width:100%}
.post-acts{display:flex;flex-wrap:wrap;gap:6px}
.pbtn{background:none;border:1px solid var(--line2);color:var(--muted);border-radius:999px;padding:6px 12px;font:600 13px Inter,sans-serif;cursor:pointer;min-height:36px}
.pbtn:hover{border-color:#3A3A42;color:var(--text)}.pbtn.is-on{border-color:var(--gold);color:var(--gold2)}
.post-c{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:6px;font-size:14px}
.post-c:empty{display:none}.post-c li{padding:8px 12px;background:var(--panel2);border-radius:10px}
.post-cf{display:flex;gap:8px}.post-cf[hidden]{display:none}.post-cf input{flex:1;background:#0A0A0D;border:1px solid var(--line2);border-radius:10px;color:var(--text);padding:8px 12px;font:14px Inter,sans-serif}
.linkbtn{background:none;border:0;color:var(--dim);font:12px Inter,sans-serif;text-decoration:underline;cursor:pointer;padding:0}

/* records, awards, week */
.regrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:12px;margin-top:28px}
.recard{border:1px solid var(--line2);border-radius:16px;background:var(--panel);padding:20px;display:flex;flex-direction:column;gap:10px}
.recard-top{display:flex;gap:14px;align-items:center}
.recard-top>span{display:flex;flex-direction:column}
.recard-n{font-size:22px}.recard-v{font-size:40px;line-height:.95}.recard-v small{font-size:14px;letter-spacing:.1em;color:var(--muted)}
.prank-mini{list-style:none;margin:0;padding:0;font-size:13px;color:var(--muted)}
.prank-mini li{display:flex;justify-content:space-between;padding:4px 0;border-top:1px solid var(--line)}
.prank-mini .prank-who{font-weight:500}
.miles{list-style:none;margin:20px 0 0;padding:0}
.mile{display:flex;gap:14px;align-items:center;padding:12px 0;border-bottom:1px solid var(--line)}
.mile-e{font-size:20px}.mile-t{flex:1;font-weight:600}
.awrow[hidden]{display:none}
.rfilters{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:26px}
.rfilters input[type=search]{min-width:180px}
.weekchips .fchip[aria-current]{border-color:var(--gold);color:var(--text)}
.steps{margin:0;padding-left:20px;display:flex;flex-direction:column;gap:6px;color:var(--muted)}.steps b{color:var(--text)}

/* staff */
.checklist{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:6px;font-size:14px;color:var(--muted)}
.checklist li{display:flex;gap:8px}.checklist li.ok{color:var(--text)}
.sreq{display:flex;flex-wrap:wrap;gap:12px;justify-content:space-between;align-items:center;padding:16px;border:1px solid var(--line2);border-radius:14px;background:var(--panel);margin-bottom:10px}
.sreq-t{display:flex;flex-direction:column;gap:4px;font-size:14px}
.sreq-done{opacity:.4;pointer-events:none}
.sreport{font-size:14px;line-height:1.75;border:1px solid var(--line2);border-radius:14px;background:var(--panel);padding:20px}
.staffbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-top:14px;padding:12px 14px;border:1px dashed rgba(201,162,74,.5);border-radius:12px;font-size:13px}
.staffbar[hidden]{display:none}
.stools{display:inline-flex;gap:4px;margin-left:auto}
.stool{background:rgba(201,162,74,.1);border:1px solid rgba(201,162,74,.45);color:var(--gold2);border-radius:8px;padding:3px 8px;font:700 11px Inter,sans-serif;letter-spacing:.08em;cursor:pointer;min-height:28px}
.stool:hover{background:rgba(201,162,74,.25)}
.res .stool{margin-left:auto}
.feature .stools{position:absolute;top:14px;right:14px}
.sbars{display:flex;align-items:flex-end;gap:4px;height:180px;margin-top:26px;padding-bottom:18px;border-bottom:1px solid var(--line)}
.sbars-h{height:120px;margin-top:0}
.sbar{flex:1;height:100%;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;position:relative}
.sbar span{display:block;width:100%;background:#3A3A42;border-radius:4px 4px 0 0;min-height:2px}
.sbar-fight span{background:var(--gold)}
.sbar small{position:absolute;bottom:-18px;font-size:10px;color:var(--dim)}
.sbar-h{flex:0 0 90px}

@media (max-width:900px){
  .soc-wrap{grid-template-columns:1fr}.soc-side{position:static;margin-top:0}
}
@media (max-width:700px){
  .ftiles-4{grid-template-columns:repeat(2,1fr)}
  .slip{right:8px;left:8px;bottom:8px;width:auto}
  .shopgrid,.mybets,.mypicks{grid-template-columns:1fr}
  .livebar{padding:10px 16px}.livebar-t{display:none}
  .backlink{margin-left:16px}
  .hp-hi{font-size:22px}
  .mehero{padding-top:36px}
}
"""


ACCOUNT_JS = r"""
(function () {
  // ---- who's logged in (cached pages stay the same for everyone) ----
  var P = window.PFO = window.PFO || { me: null, waiting: [] };
  P.onMe = function (fn) { if (P.me) fn(P.me); else P.waiting.push(fn); };
  P.toast = function (text, kind) {
    var t = document.createElement('div'); t.className = 'pfo-toast ' + (kind || ''); t.setAttribute('role', 'status');
    t.textContent = text; document.body.appendChild(t); setTimeout(function () { t.remove(); }, 4200);
  };
  P.post = function (url, body) {
    return fetch(url, { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRF': (P.me && P.me.csrf) || '' },
      body: JSON.stringify(body || {}) })
      .then(function (r) { return r.json().catch(function () { return { ok: false, message: 'Something went wrong.' }; }); })
      .catch(function () { return { ok: false, message: 'No connection. Try again.' }; });
  };
  P.esc = function (s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); };

  var slot = document.getElementById('nav-user');
  fetch('/api/me', { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (me) {
    P.me = me;
    if (slot) {
      if (me.logged_in) {
        var av = me.avatar ? '<img src="' + P.esc(me.avatar) + '" alt="">' : '<span class="nu-av">' + P.esc(me.name.slice(0, 1)) + '</span>';
        var links = '<a href="/me"><span class="nd-t">My PFO</span><span class="nd-s">Your fights, bets, fantasy and more</span></a>'
          + '<a href="/me/cards"><span class="nd-t">My cards</span><span class="nd-s">Your collection and trades</span></a>';
        if (me.levels.staff) links += '<a href="/dashboard"><span class="nd-t">Staff dashboard</span><span class="nd-s">Helper and above</span></a>';
        if (me.levels.senior) links += '<a href="/stats"><span class="nd-t">Site stats</span><span class="nd-s">Head Moderator+</span></a>';
        links += '<div class="nu-sep"></div><button type="button" data-out="0"><span class="nd-t">Log out</span></button><button type="button" data-out="1"><span class="nd-t">Log out everywhere</span><span class="nd-s">Every phone and computer</span></button>';
        var head = '<div class="nu-head"><b>' + P.esc(me.name) + '</b><span>' + Number(me.coins || 0).toLocaleString('en-GB') + ' coins</span></div>';
        document.body.classList.add('logged-in');
        slot.innerHTML = '<button type="button" class="nu-chip" aria-expanded="false" aria-haspopup="true" title="' + P.esc(me.name) + '">' + av + '<span class="nu-name">' + P.esc(me.name) + '</span></button><div class="nu-menu" hidden>' + head + links + '</div>';
        var chip = slot.querySelector('.nu-chip'), menu = slot.querySelector('.nu-menu');
        chip.addEventListener('click', function (e) {
          e.stopPropagation();
          menu.hidden = !menu.hidden;
          chip.setAttribute('aria-expanded', !menu.hidden);
          if (!menu.hidden) {
            var navEl = document.querySelector('.nav'), burgerEl = document.querySelector('.nav-burger');
            if (navEl && navEl.classList.contains('nav-open')) { navEl.classList.remove('nav-open'); if (burgerEl) burgerEl.setAttribute('aria-expanded', 'false'); }
          }
        });
        var burgerBtn = document.querySelector('.nav-burger');
        if (burgerBtn) burgerBtn.addEventListener('click', function () { menu.hidden = true; chip.setAttribute('aria-expanded', 'false'); });
        document.addEventListener('click', function (e) { if (!slot.contains(e.target)) { menu.hidden = true; chip.setAttribute('aria-expanded', 'false'); } });
        slot.querySelectorAll('[data-out]').forEach(function (b) {
          b.addEventListener('click', function () {
            fetch('/logout' + (b.getAttribute('data-out') === '1' ? '?all=1' : ''), { method: 'POST', credentials: 'same-origin', headers: { 'X-CSRF': me.csrf } })
              .then(function () { location.href = '/'; });
          });
        });
      } else if (me.login) {
        slot.innerHTML = '<a class="btn btn-ghost nu-login" href="/login?next=' + encodeURIComponent(location.pathname + location.search) + '">Log in</a>';
      }
    }
    P.waiting.forEach(function (fn) { try { fn(me); } catch (e) { console.error(e); } });
    P.waiting = [];
  }).catch(function () {});
})();
"""


NAV_JS = r"""
(function () {
  var nav = document.querySelector('.nav');
  var burger = document.querySelector('.nav-burger');
  if (burger && nav) burger.addEventListener('click', function () {
    var open = nav.classList.toggle('nav-open');
    burger.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  var drops = [].slice.call(document.querySelectorAll('.ndrop'));
  drops.forEach(function (d) {
    d.addEventListener('toggle', function () {
      if (d.open && window.innerWidth > 1080) drops.forEach(function (o) { if (o !== d) o.open = false; });
    });
  });
  document.addEventListener('click', function (e) {
    if (window.innerWidth <= 1080) return;
    drops.forEach(function (d) { if (d.open && !d.contains(e.target)) d.open = false; });
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') drops.forEach(function (d) { d.open = false; });
  });
})();
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
      var group = tab.closest('[role=tablist]');
      var scope = tab.closest('.view') || document;
      group.querySelectorAll('[data-tab]').forEach(function (t) { t.setAttribute('aria-selected', t === tab ? 'true' : 'false'); });
      var kind = tab.classList.contains('lb-tab') ? '.lb-panel' : '.rank-panel';
      scope.querySelectorAll(kind + '[data-panel]').forEach(function (p) {
        var on = p.getAttribute('data-panel') === name;
        p.hidden = !on;
        if (on) { p.classList.remove('show'); void p.offsetWidth; p.classList.add('show'); }
      });
      if (tab.classList.contains('lb-tab')) { history.replaceState(null, '', '#board-' + name); }
      else { history.replaceState(null, '', '?d=' + encodeURIComponent(name)); }
    });
  });

  // Open a leaderboard from a link like /leaderboards#board-ko
  var want = location.hash.indexOf('#board-') === 0 ? location.hash.slice(7) : null;
  if (want) { var t = document.querySelector('.lb-tab[data-tab="' + want + '"]'); if (t) t.click(); }

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


V2_JS = r"""
(function () {
  var P = window.PFO = window.PFO || { me: null, waiting: [] };
  function $(s, r) { return (r || document).querySelector(s); }
  function $$(s, r) { return [].slice.call((r || document).querySelectorAll(s)); }
  var esc = function (s) { return P.esc ? P.esc(s) : String(s); };
  function store(key, value) { try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); } catch (e) { return null; } }
  function num(n) { return Number(n || 0).toLocaleString('en-GB'); }
  function loginUrl() { return '/login?next=' + encodeURIComponent(location.pathname + location.search + location.hash); }
  function needLogin(text) {
    if (P.me && P.me.login) P.toast((text || 'Log in with Discord first.') + ' Tap here to log in.', 'warn', loginUrl());
    else P.toast('Logging in isn\'t switched on yet. Use Discord for now.', 'warn');
  }
  P.toast = function (text, kind, href) {
    var t = document.createElement(href ? 'a' : 'div');
    t.className = 'pfo-toast ' + (kind || ''); t.setAttribute('role', 'status'); t.textContent = text;
    if (href) t.href = href;
    document.body.appendChild(t); setTimeout(function () { t.remove(); }, 4800);
  };
  function busy(btn, on) { if (!btn) return; btn.disabled = !!on; btn.classList.toggle('is-busy', !!on); }

  // ============ Times in the visitor's own time zone ============
  var here = 'Europe/London';
  try { here = Intl.DateTimeFormat().resolvedOptions().timeZone || here; } catch (e) {}
  var away = here !== 'Europe/London';
  var mode = store('pfo_tz') || 'local';
  function times(root) {
    $$('time[data-utc]', root).forEach(function (t) {
      if (!t.dataset.uk) t.dataset.uk = t.textContent;
      if (!away || mode === 'uk') { t.textContent = t.dataset.uk; t.removeAttribute('title'); return; }
      var d = new Date(+t.dataset.utc * 1000);
      var txt = d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' }).replace(',', '') + ' · ' +
        d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }) + ' your time';
      t.textContent = t.dataset.up ? txt.toUpperCase() : txt;
      t.title = t.dataset.uk;
    });
  }
  P.times = times;
  times();
  var tzb = $('#tzbtn');
  if (tzb && away) {
    tzb.hidden = false;
    var label = function () { tzb.textContent = mode === 'uk' ? 'Times: UK' : 'Times: your time'; };
    label();
    tzb.addEventListener('click', function () { mode = mode === 'uk' ? 'local' : 'uk'; store('pfo_tz', mode); label(); times(); });
  }

  // Small countdowns ("2d 4h")
  function countdowns() {
    $$('[data-countdown-small]').forEach(function (el) {
      var left = Math.max(0, Math.floor(+el.getAttribute('data-countdown-small') - Date.now() / 1000));
      var d = Math.floor(left / 86400), h = Math.floor(left % 86400 / 3600), m = Math.floor(left % 3600 / 60);
      el.textContent = left <= 0 ? 'now' : (d ? d + 'd ' : '') + (d || h ? h + 'h ' : '') + m + 'm';
    });
  }
  countdowns(); setInterval(countdowns, 30000);

  // ============ Back links ============
  (function () {
    var ref = document.referrer;
    if (!ref || ref.indexOf(location.origin) !== 0) return;
    var from = new URL(ref), path = from.pathname;
    if (path === location.pathname && from.search === location.search) return;
    var label = null;
    if (path === '/rankings') label = from.searchParams.get('d') ? decodeURIComponent(from.searchParams.get('d')) + ' rankings' : 'Rankings';
    else if (path.indexOf('/event/') === 0) label = decodeURIComponent(path.slice(7));
    else if (path.indexOf('/fighter/') === 0 && location.pathname !== path) label = 'Back to fighter';
    else {
      var names = { '/live': 'Live card', '/results': 'Results', '/fighters': 'Fighters', '/sportsbook': 'Sportsbook', '/fantasy': 'Fantasy',
        '/titles': 'Titles', '/leaderboards': 'Leaderboards', '/compare': 'Head to head', '/me': 'My PFO', '/staff': 'Staff', '/dashboard': 'Staff dashboard', '/teams': 'Teams', '/social': 'Social',
        '/records': 'Records', '/awards': 'Awards', '/week': 'The week', '/hall-of-fame': 'Hall of Fame', '/': 'Home' };
      label = names[path] || null;
    }
    if (!label || location.pathname === '/') return;
    var a = document.createElement('a');
    a.className = 'backlink'; a.href = ref; a.textContent = '← ' + label;
    a.addEventListener('click', function (e) { if (history.length > 1) { e.preventDefault(); history.back(); } });
    var main = $('#main');
    if (main) main.insertBefore(a, main.firstChild);
  })();

  // ============ Home page panel ============
  P.onMe(function (me) {
    var slot = $('#homepanel');
    if (!slot || !me.logged_in) return;
    fetch('/api/home', { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (d) {
      slot.innerHTML = d.html || ''; times(slot); countdowns();
    }).catch(function () {});
  });

  // ============ Coins shown on the page ============
  function showCoins(n) { $$('[data-my-coins]').forEach(function (el) { el.textContent = num(n); }); if (P.me) P.me.coins = n; }
  P.onMe(function (me) { if (me.logged_in) showCoins(me.coins); else $$('[data-my-coins]').forEach(function (el) { el.textContent = 'Log in'; }); });

  // ============ Sign-ups ============
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-signup]');
    if (!b) return;
    if (!P.me || !P.me.logged_in) { needLogin('Log in to sign up.'); return; }
    var leaving = b.getAttribute('data-signed') === '1';
    if (leaving && !confirm('Take yourself off the sign-up list?')) return;
    busy(b, true);
    P.post('/api/signup', { session_id: b.getAttribute('data-signup'), action: leaving ? 'withdraw' : 'join' }).then(function (d) {
      busy(b, false);
      P.toast(d.message, d.ok ? 'ok' : 'bad');
      if (!d.ok) return;
      b.setAttribute('data-signed', d.signed ? '1' : '0');
      b.textContent = d.signed ? 'WITHDRAW' : 'SIGN UP';
      b.classList.toggle('btn-gold', !d.signed); b.classList.toggle('btn-ghost', !!d.signed);
      $$('[data-count-for="' + b.getAttribute('data-signup') + '"]').forEach(function (el) { el.textContent = d.count; });
      var card = b.closest('.sucard'); if (card) card.classList.toggle('sucard-on', !!d.signed);
    });
  });

  // ============ Follow ============
  var follows = $$('[data-follow]');
  if (follows.length) P.onMe(function (me) {
    var paint = function (b, on) {
      b.setAttribute('data-on', on ? '1' : '0');
      b.textContent = on ? '★ FOLLOWING' : '☆ FOLLOW';
      b.classList.toggle('is-on', on);
    };
    var setup = function (ids) {
      follows.forEach(function (b) {
        if (me.logged_in && b.getAttribute('data-follow') === me.id) return;
        b.hidden = false;
        paint(b, ids.indexOf(b.getAttribute('data-follow')) >= 0 || b.getAttribute('data-on') === '1');
        b.addEventListener('click', function () {
          if (!me.logged_in) { needLogin('Log in to follow fighters.'); return; }
          busy(b, true);
          P.post('/api/follow', { fighter_id: b.getAttribute('data-follow') }).then(function (d) {
            busy(b, false);
            if (!d.ok) { P.toast(d.message, 'bad'); return; }
            paint(b, d.following); P.toast(d.following ? 'Following. Their fights show on your home page and in My PFO.' : 'Unfollowed.', 'ok');
          });
        });
      });
    };
    if (!me.logged_in) { setup([]); return; }
    fetch('/api/following', { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (d) { setup(d.ids || []); });
  });

  // ============ Weekly tasks ============
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-claim-tasks]');
    if (!b) return;
    busy(b, true);
    P.post('/api/tasks/claim', {}).then(function (d) {
      busy(b, false); P.toast(d.message, d.ok ? 'ok' : 'bad');
      if (d.ok) { showCoins(d.balance); b.outerHTML = '<span class="muted">Claimed. Nice work.</span>'; }
    });
  });

  // ============ My bets (with cash out) ============
  function loadMyBets() {
    var slots = $$('[data-mybets]');
    if (!slots.length) return;
    fetch('/api/bets/me', { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (d) {
      if (!d.ok) return;
      var section = $('#mybets'); if (section) section.hidden = !d.bets.length;
      var icons = { open: '⏳', won: '✅', lost: '❌', void: '↩️', cashed: '💸' };
      var html = d.bets.length ? d.bets.map(function (b) {
        var legs = b.legs.map(function (l) {
          return '<li class="leg leg-' + esc(l.status) + '"><span>' + esc(l.label) + '</span><span class="tab">' + Number(l.odds).toFixed(2) + '</span></li>';
        }).join('');
        var right = b.status === 'open' ? 'Returns <b>' + num(b.returns) + '</b>' : b.status === 'won' || b.status === 'cashed' ? 'Paid <b class="gold">' + num(b.payout) + '</b>' : '';
        var co = b.cashout ? '<button type="button" class="btn btn-gold btn-sm" data-cashout="' + b.id + '" data-amount="' + b.cashout + '">CASH OUT ' + num(b.cashout) + '</button>' : '';
        if (b.refund !== null && b.refund !== undefined) co += '<button type="button" class="btn btn-ghost btn-sm" data-betrefund="' + b.id + '" data-stake="' + b.stake + '" title="' + b.refund + ' min left">REFUND</button>';
        return '<article class="mybet mybet-' + esc(b.status) + '"><div class="mybet-h"><span>' + (icons[b.status] || '') + ' ' +
          (b.legs.length > 1 ? b.legs.length + '-FOLD' : 'SINGLE') + ' · ' + esc(b.event) + '</span><span class="tab">' + num(b.stake) + ' @ ' +
          Number(b.odds).toFixed(2) + '</span></div><ol class="mybet-legs">' + legs + '</ol><div class="mybet-f"><span>' + right + '</span>' + co + '</div></article>';
      }).join('') : '<p class="muted">No bets yet.</p>';
      slots.forEach(function (s) { s.innerHTML = '<div class="mybets">' + html + '</div>'; });
    }).catch(function () {});
  }
  P.loadMyBets = loadMyBets;
  P.onMe(function (me) { if (me.logged_in) loadMyBets(); else $$('[data-mybets]').forEach(function (s) { s.innerHTML = ''; }); });
  document.addEventListener('click', function (e) {
    var r = e.target.closest && e.target.closest('[data-betrefund]');
    if (!r) return;
    if (!confirm('Refund this bet? You get ' + num(r.getAttribute('data-stake')) + ' coins back and the bet is cancelled.')) return;
    busy(r, true);
    P.post('/api/bets/refund', { bet_id: r.getAttribute('data-betrefund') }).then(function (d) {
      busy(r, false); P.toast(d.message, d.ok ? 'ok' : 'bad');
      if (d.ok) showCoins(d.balance);
      loadMyBets();
    });
  });
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-cashout]');
    if (!b) return;
    var amount = b.getAttribute('data-amount');
    if (!confirm('Cash out now for ' + num(amount) + ' coins? The bet ends and the rest of the legs no longer count.')) return;
    busy(b, true);
    P.post('/api/bets/cashout', { bet_id: b.getAttribute('data-cashout'), expected: amount }).then(function (d) {
      busy(b, false); P.toast(d.message, d.ok ? 'ok' : 'bad');
      if (d.ok) showCoins(d.balance);
      loadMyBets();
    });
  });

  // ============ Sportsbook: slip and live odds ============
  var markets = $$('.sbmarket[data-open]');
  if (markets.length) {
    var slip = { market: null, event: '', legs: {} };
    var box = $('#slip');
    var paint = function () {
      var keys = Object.keys(slip.legs);
      $$('.obtn').forEach(function (b) {
        var leg = slip.legs[b.getAttribute('data-pos')];
        var m = b.closest('.sbmarket').getAttribute('data-market');
        b.classList.toggle('is-picked', !!leg && m === slip.market && leg.pick === b.getAttribute('data-pick') && (leg.method || '') === b.getAttribute('data-method'));
      });
      if (!box) return;
      box.hidden = !keys.length;
      var total = 1;
      $('#sliplegs').innerHTML = keys.map(function (k) {
        var l = slip.legs[k]; var o = currentOdds(slip.market, k, l.pick, l.method); total *= o;
        return '<li><span>' + esc(l.label) + '</span><span class="tab">' + o.toFixed(2) + '</span><button type="button" data-unpick="' + k + '" aria-label="Remove">✕</button></li>';
      }).join('');
      var cap = parseFloat(box.getAttribute('data-cap')) || 0, capped = cap && total > cap;
      if (capped) total = cap;
      $('#slipodds').textContent = keys.length ? (keys.length > 1 ? keys.length + '-fold @ ' : 'Single @ ') + total.toFixed(2) + (capped ? ' (max)' : '') + ' · ' + slip.event : '';
      var stake = parseInt($('#slipstake').value, 10) || 0;
      $('#slipret').textContent = stake ? 'Returns ' + num(Math.round(stake * total)) : '';
      document.body.classList.toggle('has-slip', !!keys.length);
    };
    var currentOdds = function (market, pos, pick, method) {
      var b = $('.sbmarket[data-market="' + market + '"] [data-o="' + pos + ':' + pick + ':' + (method || '') + '"]');
      if (!b) return 1;
      var el = b.querySelector('.odds') || b.querySelector('b');
      return parseFloat(el ? el.textContent : '1') || 1;
    };
    document.addEventListener('click', function (e) {
      var b = e.target.closest && e.target.closest('.obtn');
      if (b) {
        if (!P.me || !P.me.logged_in) { needLogin('Log in to bet.'); return; }
        var section = b.closest('.sbmarket'), m = section.getAttribute('data-market'), pos = b.getAttribute('data-pos');
        if (slip.market && slip.market !== m) { slip.legs = {}; P.toast('Your slip can only have one card. Started a new slip.', 'warn'); }
        slip.market = m; slip.event = section.getAttribute('data-event');
        var same = slip.legs[pos] && slip.legs[pos].pick === b.getAttribute('data-pick') && (slip.legs[pos].method || '') === b.getAttribute('data-method');
        if (same) delete slip.legs[pos];
        else slip.legs[pos] = { pick: b.getAttribute('data-pick'), method: b.getAttribute('data-method') || null, label: b.getAttribute('data-label') };
        if (!Object.keys(slip.legs).length) slip.market = null;
        paint(); return;
      }
      var x = e.target.closest && e.target.closest('[data-unpick]');
      if (x) { delete slip.legs[x.getAttribute('data-unpick')]; if (!Object.keys(slip.legs).length) slip.market = null; paint(); }
    });
    if (box) {
      $('#slipstake').addEventListener('input', paint);
      $('#slipclear').addEventListener('click', function () { slip.legs = {}; slip.market = null; paint(); });
      $('#slipgo').addEventListener('click', function () {
        var stake = parseInt($('#slipstake').value, 10) || 0;
        if (stake < 1) { P.toast('Put in a stake first.', 'warn'); return; }
        var legs = Object.keys(slip.legs).map(function (k) { return { pos: +k, pick: slip.legs[k].pick, method: slip.legs[k].method }; });
        var btn = this; busy(btn, true);
        P.post('/api/bet', { market_id: slip.market, legs: legs, stake: stake }).then(function (d) {
          busy(btn, false); P.toast(d.message, d.ok ? 'ok' : 'bad');
          if (!d.ok) return;
          showCoins(d.balance); slip.legs = {}; slip.market = null; $('#slipstake').value = ''; paint(); loadMyBets();
        });
      });
    }
    var how = $('[data-howbet]');
    P.onMe(function (me) { if (how && me.logged_in) how.textContent = 'Tap any odds to add them to your slip. ' + num(me.coins) + ' coins to play with.'; });

    // Live odds every 10 seconds
    var refresh = function () {
      if (document.hidden) return;
      markets.forEach(function (section) {
        var id = section.getAttribute('data-market');
        fetch('/api/odds?market=' + id, { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (d) {
          if (!d.ok) return;
          if (!d.open) { $$('.obtn', section).forEach(function (b) { b.disabled = true; }); return; }
          Object.keys(d.odds).forEach(function (pos) {
            var o = d.odds[pos];
            ['a', 'b'].forEach(function (pick) {
              var main = $('[data-o="' + pos + ':' + pick + ':"] .odds', section);
              if (main) flash(main, o[pick].toFixed(2), +main.textContent);
              ['KO/TKO', 'SUB', 'DEC', 'R1', 'R2', 'R3'].forEach(function (m) {
                var el = $('[data-o="' + pos + ':' + pick + ':' + m + '"] b', section);
                if (el && o.methods[pick + m]) flash(el, o.methods[pick + m].toFixed(2), +el.textContent);
              });
            });
          });
          if (slip.market === id) paint();
        }).catch(function () {});
      });
    };
    var flash = function (el, text, before) {
      if (el.textContent === text) return;
      var cls = parseFloat(text) < before ? 'flash-down' : 'flash-up';
      el.textContent = text; el.classList.remove('flash-up', 'flash-down'); void el.offsetWidth; el.classList.add(cls);
    };
    setInterval(refresh, 10000);
  }

  // ============ Fantasy manager ============
  var teamSlot = $('[data-myteam]');
  if (teamSlot) {
    var F = null;
    var act = function (body, btn) {
      busy(btn, true);
      return P.post('/api/fantasy', body).then(function (d) { busy(btn, false); P.toast(d.message, d.ok ? 'ok' : 'bad'); if (d.ok) loadTeam(); return d; });
    };
    var loadTeam = function () {
      fetch('/api/fantasy/me', { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (d) {
        if (!d.ok) return;
        F = d; drawTeam(); drawPrices();
      });
    };
    var drawTeam = function () {
      var d = F, spent = d.team.reduce(function (a, p) { return a + p.paid; }, 0);
      var rules = d.round ? 'Swaps open for <b>' + esc(d.round.event || 'this card') + '</b> · ' + d.swaps_left + ' swap' + (d.swaps_left === 1 ? '' : 's') + ' left · ' +
        d.captain_left + ' captain change' + (d.captain_left === 1 ? '' : 's') + ' left · locks <time data-utc="' + d.round.locks_at + '">' + esc(d.round.locks_text) + '</time>'
        : d.free_changes ? 'Changes are free until your first card locks.' : 'Swaps open when the next card is announced.';
      var picks = d.team.map(function (p) {
        return '<li class="mypick' + (p.captain ? ' is-cap' : '') + '"><span class="mypick-n"><a href="/fighter/' + p.id + '">' + esc(p.name) + '</a>' +
          (p.captain ? ' <span class="cap">★ CAPTAIN</span>' : '') + '<span class="muted">' + esc(p.division) + ' · paid ' + p.paid + ' · ' + p.points + ' pts</span></span>' +
          (p.captain ? '' : '<button type="button" class="btn btn-ghost btn-sm" data-fcap="' + p.pick_id + '">MAKE CAPTAIN</button>') +
          '<button type="button" class="btn btn-ghost btn-sm" data-fdrop="' + p.pick_id + '">REMOVE</button></li>';
      }).join('');
      for (var i = d.team.length; i < d.size; i++) picks += '<li class="mypick mypick-empty"><span class="muted">Empty slot · add from the price list below</span></li>';
      var leagues = d.leagues.map(function (l) {
        return '<div class="pbox"><h3 class="pbox-h">' + esc(l.name.toUpperCase()) + '</h3><p class="muted">Code <b class="tab">' + esc(l.code) + '</b> · ' + l.members + ' in it</p><ol class="prank">' +
          l.table.map(function (t, n) { return '<li' + (t.me ? ' class="is-me"' : '') + '><span class="prank-n">' + (n + 1) + '</span><span class="prank-who">' + esc(t.name) + '</span><span class="prank-v tab">' + t.points + '</span></li>'; }).join('') +
          '</ol><button type="button" class="btn btn-ghost btn-sm" data-fleave="' + l.id + '">LEAVE</button></div>';
      }).join('');
      var history = d.history.length ? '<div class="tscroll"><table class="ftable"><thead><tr><th>CARD</th><th class="r">POINTS</th><th>CAPTAIN</th><th>BEST PICK</th><th class="r">RANK AFTER</th></tr></thead><tbody>' +
        d.history.slice().reverse().map(function (h) { return '<tr><td>' + esc(h.event) + '</td><td class="tab r">' + h.points + '</td><td>' + esc(h.captain) + '</td><td>' + esc(h.best) + ' (' + h.best_points + ')</td><td class="tab r">' + (h.rank ? '#' + h.rank + ' of ' + h.of : '–') + '</td></tr>'; }).join('') +
        '</tbody></table></div>' : '<p class="muted">Your points for each card show here once a card you played is scored.</p>';
      teamSlot.innerHTML =
        '<div class="mt-top"><div class="mt-num"><span class="d fan-big">' + d.points + '</span><span class="muted">POINTS' + (d.rank ? ' · #' + d.rank + ' of ' + d.of : '') + '</span></div>' +
        '<div class="mt-num"><span class="d fan-big">' + d.bank + '</span><span class="muted">BANK · ' + spent + '/' + d.budget + ' SPENT</span></div><p class="mt-rules">' + rules + '</p></div>' +
        '<ol class="mypicks">' + picks + '</ol>' +
        '<h3 class="sec-h">MINI LEAGUES</h3><div class="pgrid">' + leagues +
        '<div class="pbox"><h3 class="pbox-h">START OR JOIN</h3><div class="mt-form"><input id="lgname" maxlength="40" placeholder="League name"><button type="button" class="btn btn-gold btn-sm" id="lgmake">START</button></div>' +
        '<div class="mt-form"><input id="lgcode" maxlength="12" placeholder="Code, e.g. ABC-1234"><button type="button" class="btn btn-ghost btn-sm" id="lgjoin">JOIN</button></div></div></div>' +
        '<h3 class="sec-h">MY HISTORY</h3>' + history;
      times(teamSlot);
      $('#lgmake').addEventListener('click', function () { act({ action: 'league_create', name: $('#lgname').value }, this); });
      $('#lgjoin').addEventListener('click', function () { act({ action: 'league_join', code: $('#lgcode').value }, this); });
    };
    var drawPrices = function () {
      var mine = {}; F.team.forEach(function (p) { mine[p.id] = p; });
      $$('#ptable tbody tr').forEach(function (tr) {
        var id = tr.getAttribute('data-id'), cell = tr.querySelector('.td-act');
        if (!cell) return;
        if (mine[id]) { cell.innerHTML = '<span class="muted">IN TEAM</span>'; return; }
        var full = F.team.length >= F.size, price = +tr.getAttribute('data-price');
        cell.innerHTML = '<button type="button" class="btn btn-ghost btn-sm" data-fadd="' + id + '"' + (full || price > F.bank ? ' disabled title="' + (full ? 'Team full: remove someone first' : 'Not enough in the bank') + '"' : '') + '>ADD</button>';
      });
    };
    teamSlot.parentNode.addEventListener('click', function (e) {
      var t = e.target.closest('button'); if (!t) return;
      if (t.hasAttribute('data-fcap')) act({ action: 'captain', pick_id: t.getAttribute('data-fcap') }, t);
      if (t.hasAttribute('data-fdrop') && confirm('Remove them from your team? Once a card is announced this uses a swap.')) act({ action: 'remove', pick_id: t.getAttribute('data-fdrop') }, t);
      if (t.hasAttribute('data-fleave') && confirm('Leave this league?')) act({ action: 'league_leave', league_id: t.getAttribute('data-fleave') }, t);
    });
    var ptable = $('#ptable');
    if (ptable) ptable.addEventListener('click', function (e) {
      var t = e.target.closest('[data-fadd]'); if (t) act({ action: 'add', fighter_id: t.getAttribute('data-fadd') }, t);
    });
    P.onMe(function (me) { if (me.logged_in) loadTeam(); });
  }

  // ============ Shop ============
  var dlg = $('#buydialog');
  if (dlg) {
    var item = null, target = null;
    var field = function (name, on) { var f = $('[data-f="' + name + '"]', dlg); if (f) f.hidden = !on; };
    var total = function () {
      var amount = parseInt($('#buyamount').value, 10) || 0, cost = item.price;
      if (item.key === 'gift') cost = amount ? amount + Math.max(1, Math.round(amount * item.fee)) : 0;
      if (item.key === 'bounty') cost = amount;
      $('#buyfee').textContent = item.key === 'gift' && amount ? 'Fee ' + num(cost - amount) : item.key === 'bounty' ? 'At least ' + num(item.min) : '';
      $('#buytotal').textContent = cost ? 'Costs ' + num(cost) + ' coins' + (P.me && P.me.logged_in ? ' · you have ' + num(P.me.coins) : '') : '';
    };
    document.addEventListener('click', function (e) {
      var b = e.target.closest && e.target.closest('[data-buy]');
      if (!b) return;
      if (!P.me || !P.me.logged_in) { needLogin('Log in to buy from the shop.'); return; }
      item = JSON.parse(b.getAttribute('data-buy')); target = null;
      $('#buyname').textContent = item.name.toUpperCase();
      $('#buyabout').textContent = item.staff ? 'Staff check this one first. If they say no, you get your coins back.' : '';
      field('target', item.target); field('amount', item.amount); field('choices', !!item.choices); field('text', !!item.text);
      $('#buytarget').value = ''; $('#buyresults').innerHTML = ''; $('#buyamount').value = item.min || ''; $('#buytext').value = '';
      if (item.text) { $('#buytextl').textContent = item.text[0]; $('#buytext').maxLength = item.text[1]; }
      if (item.choices) $('#buychoice').innerHTML = Object.keys(item.choices).map(function (k) { return '<option value="' + esc(k) + '">' + esc(item.choices[k]) + '</option>'; }).join('');
      $('#buygo').textContent = item.key === 'gift' ? 'SEND' : item.key === 'bounty' ? 'PLACE BOUNTY' : item.key === 'mystery_box' ? 'OPEN A BOX' : 'BUY';
      total();
      if (dlg.showModal) dlg.showModal(); else dlg.setAttribute('open', '');
    });
    $('#buyamount').addEventListener('input', total);
    var timer = null;
    $('#buytarget').addEventListener('input', function () {
      var q = this.value.trim(); target = null; clearTimeout(timer);
      if (q.length < 2) { $('#buyresults').innerHTML = ''; return; }
      timer = setTimeout(function () {
        fetch('/api/members?q=' + encodeURIComponent(q) + (item.key === 'gift' ? '' : '&fighters=1'), { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (d) {
          $('#buyresults').innerHTML = (d.results || []).map(function (m) { return '<button type="button" data-pickm="' + m.id + '">' + esc(m.name) + '</button>'; }).join('') || '<span class="muted">Nobody found.</span>';
        });
      }, 250);
    });
    $('#buyresults').addEventListener('click', function (e) {
      var b = e.target.closest('[data-pickm]'); if (!b) return;
      target = b.getAttribute('data-pickm'); $('#buytarget').value = b.textContent; $('#buyresults').innerHTML = '';
    });
    $('#buygo').addEventListener('click', function () {
      if (item.target && !target) { P.toast('Pick who first.', 'warn'); return; }
      var btn = this; busy(btn, true);
      P.post('/api/shop/buy', { item: item.key, target_id: target, amount: $('#buyamount').value, choice: item.choices ? $('#buychoice').value : null, text: $('#buytext').value })
        .then(function (d) {
          busy(btn, false);
          if (!d.ok) { P.toast(d.message, 'bad'); return; }
          showCoins(d.balance); dlg.close ? dlg.close() : dlg.removeAttribute('open');
          P.toast(d.message, 'ok');
          if (d.pack_id) { location.href = '/me/cards?pack=' + d.pack_id; }
        });
    });
  }

  // ============ Social ============
  var feed = $('#feed');
  if (feed) {
    var sort = 'new', oldest = null, me = null;
    var ago = function (t) {
      var s = Math.floor(Date.now() / 1000 - t);
      if (s < 60) return 'now'; if (s < 3600) return Math.floor(s / 60) + 'm'; if (s < 86400) return Math.floor(s / 3600) + 'h';
      return new Date(t * 1000).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
    };
    var card = function (p, staff) {
      var av = p.avatar ? '<img class="av av-sm" src="' + esc(p.avatar) + '" alt="" loading="lazy">' : '<span class="av av-sm">' + esc(p.name.slice(0, 1)) + '</span>';
      var comments = p.comments.map(function (c) {
        return '<li><b>' + esc(c.name) + '</b>' + (c.fighter ? ' <span class="sbadge">FIGHTER</span>' : '') + ' ' + esc(c.text) +
          (staff ? ' <button type="button" class="linkbtn" data-sremove="' + p.id + '" data-comment="' + c.id + '">remove</button>' : '') + '</li>';
      }).join('');
      return '<article class="post" id="post-' + p.id + '" data-post="' + p.id + '"><header class="post-h">' + av + '<span class="post-who"><a href="/fighter/' + p.user_id + '" class="d">' + esc(p.name) + '</a>' +
        (p.badge ? ' <span class="sbadge sbadge-' + p.badge.toLowerCase() + '">' + p.badge + '</span>' : '') + (p.rank ? ' <span class="muted">' + esc(p.rank) + '</span>' : '') +
        '</span><span class="muted post-t">' + ago(p.at) + '</span></header>' +
        (p.event ? '<span class="post-tag">#' + esc(p.event) + '</span>' : '') + '<p class="post-text">' + esc(p.text) + '</p>' +
        (p.image ? '<img class="post-img" src="' + esc(p.image) + '" alt="Photo from ' + esc(p.name) + '" loading="lazy">' : '') +
        '<div class="post-acts"><button type="button" class="pbtn' + (p.mine === 1 ? ' is-on' : '') + '" data-vote="1">▲ <span>' + p.likes + '</span></button>' +
        '<button type="button" class="pbtn' + (p.mine === -1 ? ' is-on' : '') + '" data-vote="-1">▼ <span>' + p.dislikes + '</span></button>' +
        '<button type="button" class="pbtn" data-copen>💬 ' + p.comment_count + '</button>' +
        (p.own || staff ? '<button type="button" class="pbtn" data-sremove="' + p.id + '">Remove</button>' : '<button type="button" class="pbtn" data-sreport>Report</button>') +
        '</div><ul class="post-c">' + comments + '</ul><div class="post-cf" hidden><input maxlength="300" placeholder="Write a comment"><button type="button" class="btn btn-ghost btn-sm" data-csend>SEND</button></div></article>';
    };
    var load = function (more) {
      var url = '/api/social/feed?sort=' + sort + (more && oldest ? '&before=' + oldest : '');
      fetch(url, { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (d) {
        var html = d.posts.map(function (p) { return card(p, d.staff); }).join('');
        if (more) feed.insertAdjacentHTML('beforeend', html);
        else feed.innerHTML = html || '<p class="muted">' + esc(d.empty || 'No posts yet. Be the first.') + '</p>';
        if (d.posts.length) oldest = d.posts[d.posts.length - 1].id;
        $('#feedmore').hidden = d.posts.length < 20 || sort === 'top';
        if (location.hash.indexOf('#post-') === 0 && !more) { var t = $(location.hash); if (t) { t.classList.add('is-target'); t.scrollIntoView({ block: 'center' }); } }
      });
    };
    $$('[data-sort]').forEach(function (b) {
      b.addEventListener('click', function () {
        sort = b.getAttribute('data-sort'); oldest = null;
        $$('[data-sort]').forEach(function (o) { o.setAttribute('aria-selected', o === b ? 'true' : 'false'); });
        load(false);
      });
    });
    $('#feedmore').addEventListener('click', function () { load(true); });
    P.onMe(function (m) {
      me = m; $('#compose').hidden = !m.logged_in; $('#soclogin').hidden = !!m.logged_in; load(false);
    });
    var text = $('#soctext'), img = $('#socimg');
    if (text) text.addEventListener('input', function () { $('#soccount').textContent = text.value.length + '/500'; });
    if (img) img.addEventListener('change', function () {
      var f = img.files[0]; $('#socpreview').innerHTML = '';
      if (!f) return;
      if (f.size > 5 * 1024 * 1024) { P.toast('Images can be up to 5 MB.', 'bad'); img.value = ''; return; }
      var u = URL.createObjectURL(f); $('#socpreview').innerHTML = '<img src="' + u + '" alt="Your photo"><button type="button" class="linkbtn" id="socimgx">remove photo</button>';
      $('#socimgx').addEventListener('click', function () { img.value = ''; $('#socpreview').innerHTML = ''; });
    });
    var postBtn = $('#socpost');
    if (postBtn) postBtn.addEventListener('click', function () {
      if (!text.value.trim() && !(img.files && img.files[0])) { P.toast('Write something first.', 'warn'); return; }
      var fd = new FormData(); fd.append('text', text.value); fd.append('event', $('#socevent').value);
      if (img.files && img.files[0]) fd.append('image', img.files[0]);
      busy(postBtn, true);
      fetch('/api/social/post', { method: 'POST', credentials: 'same-origin', headers: { 'X-CSRF': me.csrf }, body: fd })
        .then(function (r) { return r.json(); }).then(function (d) {
          busy(postBtn, false); P.toast(d.message, d.ok ? 'ok' : 'bad');
          if (d.ok) { text.value = ''; img.value = ''; $('#socpreview').innerHTML = ''; $('#soccount').textContent = '0/500'; sort = 'new'; load(false); }
        }).catch(function () { busy(postBtn, false); P.toast('Couldn\'t post. Try again.', 'bad'); });
    });
    feed.addEventListener('click', function (e) {
      var post = e.target.closest('[data-post]'); if (!post) return;
      var id = post.getAttribute('data-post'), b;
      if ((b = e.target.closest('[data-vote]'))) {
        if (!me || !me.logged_in) { needLogin('Log in to vote.'); return; }
        P.post('/api/social/vote', { post_id: id, value: b.getAttribute('data-vote') }).then(function (d) {
          if (!d.ok) { P.toast(d.message, 'bad'); return; }
          var btns = $$('[data-vote]', post);
          btns[0].querySelector('span').textContent = d.likes; btns[1].querySelector('span').textContent = d.dislikes;
          btns[0].classList.toggle('is-on', d.mine === 1); btns[1].classList.toggle('is-on', d.mine === -1);
        });
      } else if (e.target.closest('[data-copen]')) {
        if (!me || !me.logged_in) { needLogin('Log in to comment.'); return; }
        var f = $('.post-cf', post); f.hidden = !f.hidden; if (!f.hidden) $('input', f).focus();
      } else if ((b = e.target.closest('[data-csend]'))) {
        var input = $('.post-cf input', post);
        if (!input.value.trim()) return;
        busy(b, true);
        P.post('/api/social/comment', { post_id: id, text: input.value }).then(function (d) {
          busy(b, false);
          if (!d.ok) { P.toast(d.message, 'bad'); return; }
          $('.post-c', post).insertAdjacentHTML('beforeend', '<li><b>' + esc(d.name) + '</b> ' + esc(input.value) + '</li>'); input.value = '';
        });
      } else if (e.target.closest('[data-sreport]')) {
        if (!me || !me.logged_in) { needLogin('Log in to report posts.'); return; }
        if (!confirm('Report this post to staff?')) return;
        P.post('/api/social/report', { post_id: id }).then(function (d) { P.toast(d.message, d.ok ? 'ok' : 'bad'); });
      } else if ((b = e.target.closest('[data-sremove]'))) {
        if (!confirm('Remove this ' + (b.hasAttribute('data-comment') ? 'comment' : 'post') + '? It goes from Discord too.')) return;
        P.post('/api/social/remove', { post_id: id, comment_id: b.getAttribute('data-comment') }).then(function (d) {
          P.toast(d.message, d.ok ? 'ok' : 'bad');
          if (d.ok) { if (b.hasAttribute('data-comment')) b.closest('li').remove(); else post.remove(); }
        });
      }
    });
  }

  // ============ Awards filters ============
  var alist = $('#alist');
  if (alist) {
    var kind = '', ev = $('#aevent');
    var apply = function () {
      var shown = 0;
      $$('.awrow', alist).forEach(function (r) {
        var ok = (!kind || r.getAttribute('data-kind') === kind) && (!ev.value || r.getAttribute('data-event') === ev.value);
        r.hidden = !ok; if (ok) shown++;
      });
      $('#anone').hidden = shown > 0;
    };
    $$('[data-akind]').forEach(function (b) {
      b.addEventListener('click', function () {
        kind = b.getAttribute('data-akind');
        $$('[data-akind]').forEach(function (o) { o.setAttribute('aria-pressed', o === b ? 'true' : 'false'); });
        apply();
      });
    });
    ev.addEventListener('change', apply);
  }

  // ============ Staff dashboard buttons ============
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-shopreq]');
    if (!b) return;
    var ok = b.getAttribute('data-ok') === '1';
    if (!confirm(ok ? 'Approve this request?' : 'Refund this request? The coins go back to them.')) return;
    busy(b, true);
    P.post('/api/staff/shop', { item_id: b.getAttribute('data-shopreq'), approve: ok }).then(function (d) {
      busy(b, false); P.toast(d.message, d.ok ? 'ok' : 'bad');
      if (d.ok) b.closest('.sreq').classList.add('sreq-done');
    });
  });

  // ============ Staff tools on pages ============
  P.onMe(function (me) {
    if (!me.logged_in || !me.levels.staff) return;
    document.body.classList.add('is-staff');
    $$('[data-staff-only]').forEach(function (el) { el.hidden = false; });
    // Results: edit / swap winner / remove
    var rows = $$('.res[data-fight]');
    rows.forEach(function (r) {
      var t = document.createElement('button');
      t.type = 'button'; t.className = 'stool'; t.textContent = 'EDIT'; t.setAttribute('aria-label', 'Edit this fight (staff)');
      t.addEventListener('click', function (e) { e.preventDefault(); fightEditor(r.getAttribute('data-fight')); });
      r.appendChild(t);
    });
    // Rankings: up / down / remove / place
    $$('[data-rank-uid]').forEach(function (r) {
      var rank = r.getAttribute('data-rank'), w = r.getAttribute('data-rank-w'), uid = r.getAttribute('data-rank-uid');
      var bar = document.createElement('span'); bar.className = 'stools';
      var add = function (label, title, value) {
        var b = document.createElement('button'); b.type = 'button'; b.className = 'stool'; b.textContent = label; b.title = title;
        b.addEventListener('click', function (e) {
          e.preventDefault(); e.stopPropagation();
          var v = value;
          if (v === 'ask') { v = prompt('Place at which rank? (0 = champion, 1 to 15)'); if (v === null || v === '') return; }
          if (v === 'remove' && !confirm('Take them out of the ' + w + ' rankings?')) return;
          busy(b, true);
          P.post('/api/staff/rank', { weight: w, user_id: uid, rank: v }).then(function (d) {
            busy(b, false); P.toast(d.message, d.ok ? 'ok' : 'bad'); if (d.ok) setTimeout(function () { location.reload(); }, 700);
          });
        });
        bar.appendChild(b);
      };
      if (rank === '') add('+ RANK', 'Place in the rankings', 'ask');
      else {
        var n = +rank;
        if (n > 1) add('▲', 'Move up one', n - 1);
        if (n >= 1 && n < 15) add('▼', 'Move down one', n + 1);
        add('#', 'Place at a rank', 'ask');
        add('✕', 'Remove from this ranking', 'remove');
      }
      r.appendChild(bar);
    });
    // Fighter page bar
    var fb = $('[data-staff-fighter]');
    if (fb) {
      fb.hidden = false;
      fb.innerHTML = '<span class="eyebrow-grey">STAFF</span><span class="muted">Edit fights from the list below. Records, stats, belts, transfers and warnings: ' +
        '/editstats, /belt, /transfer and /warn in Discord.</span>' + (me.levels.mod ? '' : '');
    }
  });

  function fightEditor(id) {
    fetch('/api/staff/fight?id=' + id, { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (f) {
      if (!f.ok) { P.toast('Couldn\'t load that fight.', 'bad'); return; }
      var d = document.createElement('dialog'); d.className = 'pfo-dialog';
      var methods = [['KO/TKO', 'KO / TKO'], ['SUB', 'Submission'], ['DEC', 'Decision'], ['DQ', 'DQ'], ['DRAW', 'Draw'], ['NC', 'No contest']];
      d.innerHTML = '<form method="dialog" class="dlg-in"><h2 class="d h-s">FIGHT #' + f.id + '</h2><p class="muted">' + esc(f.winner) + ' def. ' + esc(f.loser) + '</p>' +
        '<div class="dlg-field"><label>Method</label><select name="method">' + methods.map(function (m) { return '<option value="' + m[0] + '"' + (m[0] === f.method ? ' selected' : '') + '>' + m[1] + '</option>'; }).join('') + '</select></div>' +
        '<div class="dlg-field"><label>Round (0 = none)</label><input name="round" type="number" min="0" max="5" value="' + (f.round || 0) + '"></div>' +
        '<div class="dlg-field"><label>Event</label><input name="event" maxlength="80" value="' + esc(f.event) + '"></div>' +
        '<label class="fbox"><input type="checkbox" name="title"' + (f.title_fight ? ' checked' : '') + '> Title fight</label>' +
        '<label class="fbox"><input type="checkbox" name="swap"> Swap the winner and loser</label>' +
        '<div class="btns"><button type="button" class="btn btn-gold" data-save>SAVE</button><button type="button" class="btn btn-ghost" data-del>REMOVE FIGHT</button><button class="btn btn-ghost" value="cancel">CANCEL</button></div>' +
        '<p class="note">Same as /editfight and /removefight. Logged in pfo-bot.</p></form>';
      document.body.appendChild(d); d.showModal();
      d.addEventListener('close', function () { d.remove(); });
      var form = d.querySelector('form');
      d.querySelector('[data-save]').addEventListener('click', function () {
        var body = { fight_id: f.id, method: form.method.value, round: form.round.value, event: form.event.value || 'none', title_fight: form.title.checked, swap: form.swap.checked };
        var b = this; busy(b, true);
        P.post('/api/staff/fight', body).then(function (r) { busy(b, false); P.toast(r.message, r.ok ? 'ok' : 'bad'); if (r.ok) { d.close(); setTimeout(function () { location.reload(); }, 700); } });
      });
      d.querySelector('[data-del]').addEventListener('click', function () {
        if (!confirm('Remove fight #' + f.id + '? This can\'t be undone.')) return;
        var b = this; busy(b, true);
        P.post('/api/staff/fight', { fight_id: f.id, action: 'remove' }).then(function (r) { busy(b, false); P.toast(r.message, r.ok ? 'ok' : 'bad'); if (r.ok) { d.close(); setTimeout(function () { location.reload(); }, 700); } });
      });
    });
  }

  // ============ Add to Home Screen ============
  if ('serviceWorker' in navigator && location.protocol === 'https:') {
    addEventListener('load', function () { navigator.serviceWorker.register('/sw.js').catch(function () {}); });
  }
  var deferred = null;
  addEventListener('beforeinstallprompt', function (e) {
    e.preventDefault(); deferred = e;
    var b = $('#installbtn'); if (b) b.hidden = false;
  });
  var ib = $('#installbtn');
  if (ib) ib.addEventListener('click', function () { if (deferred) { deferred.prompt(); deferred = null; ib.hidden = true; } });
})();
"""


CARDS_JS = r"""
(function () {
  var P = window.PFO = window.PFO || { me: null, waiting: [] };
  function $(q, el) { return (el || document).querySelector(q); }
  function $$(q, el) { return Array.prototype.slice.call((el || document).querySelectorAll(q)); }
  function esc(t) { var d = document.createElement('div'); d.textContent = t == null ? '' : String(t); return d.innerHTML; }
  function busy(btn, on) { if (!btn) return; btn.disabled = !!on; btn.classList.toggle('is-busy', !!on); }
  function post(url, body, btn, reload) {
    busy(btn, true);
    return P.post(url, body).then(function (d) {
      busy(btn, false);
      if (!d.ok) { P.toast(d.message, 'bad'); return d; }
      P.toast(d.message, 'ok');
      if (reload) setTimeout(function () { location.reload(); }, 700);
      return d;
    });
  }

  // ---- fighter page tabs ----
  $$('[data-ftabs]').forEach(function (nav) {
    var keys = $$('[data-ftab]', nav).map(function (b) { return b.getAttribute('data-ftab'); });
    function show(k) {
      $$('[data-fpanel]').forEach(function (p) { p.hidden = p.getAttribute('data-fpanel') !== k; });
      $$('[data-ftab]', nav).forEach(function (b) {
        var on = b.getAttribute('data-ftab') === k; b.classList.toggle('on', on); b.setAttribute('aria-selected', on ? 'true' : 'false');
      });
    }
    nav.addEventListener('click', function (e) {
      var b = e.target.closest('[data-ftab]'); if (!b) return;
      var k = b.getAttribute('data-ftab'); show(k);
      try { history.replaceState(null, '', '#' + k); } catch (err) {}
    });
    var h = (location.hash || '').slice(1);
    show(keys.indexOf(h) >= 0 ? h : 'overview');
    nav.hidden = false;
  });

  // ---- /cards filter ----
  $$('[data-cardfilter]').forEach(function (bar) {
    bar.addEventListener('click', function (e) {
      var b = e.target.closest('[data-ct]'); if (!b) return;
      var t = b.getAttribute('data-ct');
      $$('[data-ct]', bar).forEach(function (x) { x.classList.toggle('on', x === b); });
      $$('.cardgrid [data-tier]').forEach(function (c) { c.hidden = !!t && c.getAttribute('data-tier') !== t; });
    });
  });

  // ---- my cards ----
  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-packdecide]');
    if (b) { post('/api/cards/pack', { pack_id: b.getAttribute('data-packdecide'), decision: b.getAttribute('data-d') }, b, true); return; }
    b = e.target.closest('[data-sellspares]');
    if (b) {
      if (b.getAttribute('data-sure') !== '1') { b.setAttribute('data-sure', '1'); b.textContent = 'TAP AGAIN TO SELL'; return; }
      post('/api/cards/sell', {}, b, true); return;
    }
    b = e.target.closest('[data-trade]');
    if (b) { post('/api/cards/trade/respond', { trade_id: b.getAttribute('data-trade'), action: b.getAttribute('data-a') }, b, true); return; }
    b = e.target.closest('[data-upgrade]');
    if (b) {
      if (b.getAttribute('data-sure') !== '1') { b.setAttribute('data-sure', '1'); b.textContent = 'TAP AGAIN · ' + (+b.getAttribute('data-price')).toLocaleString() + ' COINS'; return; }
      post('/api/cards/upgrade', { card_id: b.getAttribute('data-upgrade') }, b, true);
    }
  });

  // ---- daily spin ----
  var wheel = $('[data-spinwheel]');
  if (wheel) {
    var prizes = []; try { prizes = JSON.parse(wheel.getAttribute('data-spinwheel')); } catch (err) {}
    var cx = wheel.getContext('2d'), N = prizes.length, SEG = 2 * Math.PI / Math.max(N, 1), rot = 0;
    var draw = function () {
      var r = 270; cx.clearRect(0, 0, 560, 560); cx.save(); cx.translate(280, 280); cx.rotate(rot);
      prizes.forEach(function (p, i) {
        cx.beginPath(); cx.moveTo(0, 0); cx.arc(0, 0, r, i * SEG, (i + 1) * SEG); cx.closePath();
        cx.fillStyle = p[1]; cx.fill(); cx.strokeStyle = '#08080a'; cx.lineWidth = 3; cx.stroke();
        cx.save(); cx.rotate(i * SEG + SEG / 2); cx.textAlign = 'right'; cx.fillStyle = '#f4f4f5';
        cx.font = '600 22px Inter, system-ui, sans-serif'; cx.fillText(p[0], r - 18, 8); cx.restore();
      });
      cx.beginPath(); cx.arc(0, 0, r, 0, 2 * Math.PI); cx.strokeStyle = '#c9a24a'; cx.lineWidth = 9; cx.stroke();
      cx.rotate(-rot); cx.beginPath(); cx.arc(0, 0, 44, 0, 2 * Math.PI); cx.fillStyle = '#08080a'; cx.fill();
      cx.strokeStyle = '#c9a24a'; cx.lineWidth = 4; cx.stroke(); cx.fillStyle = '#c9a24a'; cx.textAlign = 'center';
      cx.font = '800 30px Inter, system-ui, sans-serif'; cx.fillText('PFO', 0, 11); cx.restore();
    };
    draw();
    var spinBtn = $('[data-spin]');
    if (spinBtn) spinBtn.addEventListener('click', function () {
      if (spinBtn.disabled) return;
      spinBtn.disabled = true;
      P.post('/api/spin', {}).then(function (d) {
        if (!d.ok) { P.toast(d.message, 'bad'); spinBtn.disabled = false; return; }
        var target = -Math.PI / 2 - (d.index * SEG + SEG / 2) + (Math.random() - 0.5) * SEG * 0.6, end = target;
        while (end < rot + 2 * Math.PI * 5) end += 2 * Math.PI;
        var start = rot, t0 = performance.now(), quick = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
        var dur = quick ? 1 : 3400;
        (function step(now) {
          var k = Math.min(1, (now - t0) / dur), ease = 1 - Math.pow(1 - k, 4);
          rot = start + (end - start) * ease; draw();
          if (k < 1) { requestAnimationFrame(step); return; }
          rot = rot % (2 * Math.PI);
          P.toast(d.message, 'ok');
          var st = $('[data-spinstate]'); if (st) st.textContent = d.message;
          if (d.again) { spinBtn.disabled = false; spinBtn.textContent = 'SPIN AGAIN'; }
          else { spinBtn.textContent = 'COME BACK TOMORROW'; }
          if (d.pack_id) setTimeout(function () { location.href = '/me/cards?pack=' + d.pack_id; }, 1600);
        })(t0);
      }).catch(function () { spinBtn.disabled = false; P.toast('Something went wrong. Try again.', 'bad'); });
    });
  }

  var form = $('[data-tradeform]');
  if (form) {
    var who = null, timer = null;
    $('#trwho').addEventListener('input', function () {
      var q = this.value.trim(); clearTimeout(timer); who = null;
      if (q.length < 2) { $('#trwhores').innerHTML = ''; return; }
      timer = setTimeout(function () {
        fetch('/api/members?q=' + encodeURIComponent(q), { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (d) {
          $('#trwhores').innerHTML = (d.results || []).map(function (m) { return '<button type="button" data-trwho="' + m.id + '">' + esc(m.name) + '</button>'; }).join('') || '<span class="muted">Nobody found.</span>';
        });
      }, 250);
    });
    $('#trwhores').addEventListener('click', function (e) {
      var b = e.target.closest('[data-trwho]'); if (!b) return;
      who = b.getAttribute('data-trwho'); $('#trwho').value = b.textContent; $('#trwhores').innerHTML = '';
      $('#trtheirs').innerHTML = '<p class="muted">Loading…</p>';
      fetch('/api/cards/of?user=' + who, { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (d) {
        $('#trtheirs').innerHTML = (d.cards || []).map(function (c) {
          return '<label class="tr-pick"><input type="checkbox" name="want" value="' + c.id + '"> ' + esc(c.label) + '</label>';
        }).join('') || '<p class="muted">They have no cards free to trade.</p>';
      });
    });
    $('#trsend').addEventListener('click', function () {
      if (!who) { P.toast('Pick who you are trading with.', 'warn'); return; }
      var give = $$('input[name=give]:checked', form).map(function (i) { return i.value; });
      var want = $$('input[name=want]:checked', form).map(function (i) { return i.value; });
      if (give.length > 3 || want.length > 3) { P.toast('Up to 3 cards a side.', 'warn'); return; }
      post('/api/cards/trade', { to: who, give: give, want: want, give_coins: $('#trgivec').value, want_coins: $('#trwantc').value }, this, true);
    });
  }

  // ---- replays: loop the video ----
  var frames = $$('[data-replay]').filter(function (f) { return f.getAttribute('data-replay'); });
  if (frames.length) {
    var s = document.createElement('script');
    s.src = 'https://player.twitch.tv/js/embed/v1.js';
    s.onload = function () {
      if (!window.Twitch || !window.Twitch.Player) return;
      frames.forEach(function (f, i) {
        var id = 'replay-' + i; var box = document.createElement('div'); box.id = id; box.className = 'replay-player';
        f.innerHTML = ''; f.appendChild(box);
        var player = new window.Twitch.Player(id, { video: 'v' + f.getAttribute('data-replay'), parent: [f.getAttribute('data-host'), location.hostname],
                                                  autoplay: true, muted: true, width: '100%', height: '100%' });
        player.addEventListener(window.Twitch.Player.ENDED, function () { try { player.seek(0); player.play(); } catch (err) {} });
      });
    };
    document.head.appendChild(s);
  }
})();
"""

CARDS_CSS = r"""
/* fighter tabs */
.ftabs[hidden],[data-fpanel][hidden],.cardtile[hidden]{display:none!important}
.ftabs{display:flex;gap:6px;flex-wrap:wrap;margin:28px 0 4px;border-bottom:1px solid var(--line)}
.ftabs button{background:none;border:0;border-bottom:2px solid transparent;color:var(--muted);font:600 12px/1 Inter,system-ui,sans-serif;letter-spacing:.16em;padding:12px 14px;cursor:pointer}
.ftabs button.on{color:var(--text);border-bottom-color:var(--gold)}
.ftabs button:focus-visible{outline:2px solid var(--gold);outline-offset:2px}
/* fights like */
.like{display:flex;gap:20px;align-items:center;flex-wrap:wrap}
.like-ring{--p:50;width:92px;height:92px;border-radius:50%;display:grid;place-items:center;background:conic-gradient(var(--gold) calc(var(--p)*1%),#26262d 0);position:relative;flex:none}
.like-ring::after{content:"";position:absolute;inset:7px;border-radius:50%;background:var(--bg,#08080A)}
.like-ring span{position:relative;z-index:1;font-size:26px}
.like-t{display:grid;gap:4px;min-width:0}
.like-name{font-size:clamp(28px,5vw,40px);line-height:1}
.lk-legend{display:flex;gap:16px;font-size:12px;color:var(--muted);margin:18px 0 10px}
.lk-dot{display:inline-block;width:9px;height:9px;border-radius:2px;background:var(--gold);margin-right:6px}
.lk-dot-u{background:#c9c6be}
.lk-bars{display:grid;gap:9px;max-width:620px}
.lk-bar{display:grid;grid-template-columns:62px 1fr 1fr;gap:10px;align-items:center;font-size:12px}
.lk-k{color:var(--muted);letter-spacing:.12em;font-weight:600}
.lk-t{height:8px;border-radius:4px;background:#1f1f25;overflow:hidden;position:relative}
.lk-t i{position:absolute;inset:0 auto 0 0;border-radius:4px;background:var(--gold)}
.lk-u i{background:#c9c6be}
/* cards */
.cardimg{display:block;width:100%;height:auto;aspect-ratio:5/7;border-radius:14px;background:#111}
.cardgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:18px;margin-top:18px}
.cardtile{position:relative;display:block;transition:transform .2s}
.cardtile:hover{transform:translateY(-4px)}
.cardcount,.cardheld{position:absolute;top:8px;right:8px;background:rgba(0,0,0,.75);color:#fff;font:700 12px/1 Inter,system-ui,sans-serif;padding:5px 8px;border-radius:6px}
.cardheld{top:auto;bottom:8px;left:8px;right:auto;color:var(--gold)}
.cardfilter{display:flex;gap:6px;flex-wrap:wrap}
.cardfilter button{background:none;border:1px solid var(--line);color:var(--muted);border-radius:999px;padding:7px 12px;font:600 11px/1 Inter,system-ui,sans-serif;letter-spacing:.12em;cursor:pointer}
.cardfilter button.on{border-color:var(--gold);color:var(--text)}
.fcard-in{display:grid;grid-template-columns:minmax(0,320px) 1fr;gap:28px;align-items:center}
.fcard-t{display:grid;gap:10px;min-width:0}
.packrow{display:grid;grid-template-columns:repeat(3,minmax(0,240px));gap:18px;margin:18px 0}
.packcard{display:grid;gap:8px;text-align:center}
.trade{border:1px solid var(--line);border-radius:12px;padding:16px;display:grid;gap:12px;margin-top:12px}
.trade-sides{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.trade-sides>div{display:flex;flex-wrap:wrap;gap:6px;align-content:flex-start}
.trade-sides .num-l{width:100%}
.tr-card{border:1px solid var(--line);border-radius:6px;padding:5px 9px;font-size:13px}
.tr-card.tier-champion,.tr-card.tier-double,.tr-card.tier-goat,.tr-card.tier-former{border-color:var(--gold)}
.tradeform{display:grid;gap:10px;max-width:760px}
.tr-in{background:#0A0A0D;border:1px solid var(--line2);border-radius:10px;color:var(--text);padding:10px 12px;font:14px Inter,sans-serif;width:100%;box-sizing:border-box}
.tr-cols{display:grid;grid-template-columns:1fr 1fr;gap:18px}
.tr-list{display:grid;gap:4px;max-height:260px;overflow:auto;border:1px solid var(--line);border-radius:10px;padding:8px;margin-bottom:8px}
.tr-pick{display:flex;gap:8px;align-items:center;font-size:13px;padding:4px}
.pickres{display:flex;gap:6px;flex-wrap:wrap}
.pickres button{background:var(--panel,#121216);border:1px solid var(--line);color:inherit;border-radius:6px;padding:6px 10px;cursor:pointer}
/* replay */
.replay-grid{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(0,1fr);gap:20px;align-items:start}
.replay-solo{grid-template-columns:1fr}
.replay-res{display:grid;gap:10px;min-width:0}
.replay-res .btn{justify-self:start}
.replay-player,.replay-player iframe{width:100%;height:100%}
@media (max-width:760px){
  .replay-grid,.fcard-in,.tr-cols,.trade-sides{grid-template-columns:1fr}
  .packrow{grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}
  .cardgrid{grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
  .fcard-in{max-width:340px}
}
"""

SITE_JS += EXTRA_JS
SITE_JS += NAV_JS
SITE_JS += ACCOUNT_JS
SITE_JS += V2_JS
SITE_JS += CARDS_JS
SITE_CSS += V2_CSS
SITE_CSS += CARDS_CSS


# ============================================================
# Web server
# ============================================================

# ============================================================
# ACCOUNTS, SESSIONS AND SECURITY
#
# Log in with Discord (only asks for the username and picture).
# A random session token is kept in an HttpOnly, Secure cookie and
# only its hash is stored. Every action that changes something needs
# the session's CSRF token, and is rate limited. Staff pages check the
# member's Discord roles on every request.
#
# Railway Variables needed for logging in:
#   SITE_URL               https://prestigefightingorg.com
#   DISCORD_CLIENT_ID      from the Discord Developer Portal (OAuth2)
#   DISCORD_CLIENT_SECRET  from the same page
# ============================================================

SESSION_COOKIE = "pfo_s"
STATE_COOKIE = "pfo_state"
SESSION_DAYS = 30


def setup_web_tables(g):
    db = g["get_db"]()
    db.execute("""
        CREATE TABLE IF NOT EXISTS web_sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            csrf TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            last_seen INTEGER NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS web_accounts (
            user_id INTEGER PRIMARY KEY,
            first_login INTEGER NOT NULL,
            last_login INTEGER NOT NULL,
            logins INTEGER NOT NULL DEFAULT 1,
            username TEXT
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS site_views (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day TEXT NOT NULL,
            at INTEGER NOT NULL,
            visitor TEXT NOT NULL,
            path TEXT NOT NULL,
            device TEXT,
            browser TEXT,
            system TEXT,
            source TEXT,
            country TEXT,
            logged_in INTEGER NOT NULL DEFAULT 0,
            installed INTEGER NOT NULL DEFAULT 0
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS site_views_day ON site_views(day)")
    db.commit()
    db.close()


def _hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def create_session(g, user_id):
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    now = int(time.time())
    g["discipline_query"](
        "INSERT INTO web_sessions (token_hash, user_id, csrf, created_at, expires_at, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
        (_hash(token), user_id, csrf, now, now + SESSION_DAYS * 86400, now), write=True
    )
    g["discipline_query"]("DELETE FROM web_sessions WHERE expires_at < ?", (now,), write=True)
    return token


def read_session(g, request):
    token = request.cookies.get(SESSION_COOKIE)
    if not token or len(token) > 100:
        return None
    rows = g["discipline_query"]("SELECT * FROM web_sessions WHERE token_hash = ?", (_hash(token),))
    if not rows:
        return None
    row = rows[0]
    now = int(time.time())
    if row["expires_at"] < now:
        return None
    if now - row["last_seen"] > 600:
        g["discipline_query"]("UPDATE web_sessions SET last_seen = ? WHERE token_hash = ?", (now, row["token_hash"]), write=True)
    return {"user_id": row["user_id"], "csrf": row["csrf"], "token_hash": row["token_hash"]}


def member_levels(g, guild, member):
    """What this member can see on the site."""
    if member is None:
        return {"fighter": False, "staff": False, "mod": False, "senior": False}
    owner = getattr(guild, "owner_id", None) == member.id
    has = g["has_any_role"]
    senior = owner or has(member, g["SENIOR_ROLE_NAMES"])
    mod = senior or has(member, g["MODERATOR_ROLE_NAMES"])
    staff = mod or has(member, g["STAFF_ROLE_NAMES"])
    fighter = has(member, g["FIGHTER_ROLE_NAMES"])
    return {"fighter": fighter, "staff": staff, "mod": mod, "senior": senior}


class Viewer:
    """The logged-in member for one request (or nobody)."""

    def __init__(self, g, guild, session):
        self.session = session
        self.user_id = session["user_id"] if session else None
        self.member = guild.get_member(self.user_id) if (guild and self.user_id) else None
        self.levels = member_levels(g, guild, self.member)
        self.csrf = session["csrf"] if session else None

    @property
    def ok(self):
        return self.member is not None


_rate = {}


def rate_ok(key, limit, window):
    """True if this key has done fewer than `limit` actions in `window` seconds."""
    now = time.time()
    hits = [t for t in _rate.get(key, []) if now - t < window]
    if len(hits) >= limit:
        _rate[key] = hits
        return False
    hits.append(now)
    _rate[key] = hits
    if len(_rate) > 5000:
        for k in list(_rate)[:2500]:
            _rate.pop(k, None)
    return True


def client_ip(request):
    return (request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote or "?")


def secure_cookie_flag():
    return SITE_URL.startswith("https://")


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' https: data: blob:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; script-src 'self' 'unsafe-inline' https://player.twitch.tv; connect-src 'self'; "
        "frame-src https://player.twitch.tv https://www.twitch.tv https://clips.twitch.tv; frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://discord.com"
    ),
}


# ------------------------------------------------------------
# Visitor counting (no cookies, no IPs stored)
# ------------------------------------------------------------

_salt = {"day": None, "value": None}
BOT_WORDS = ("bot", "crawl", "spider", "slurp", "preview", "facebookexternalhit", "discordbot", "monitor", "curl", "python", "headless")


def _daily_salt():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _salt["day"] != today:
        _salt["day"], _salt["value"] = today, secrets.token_hex(16)
    return _salt["value"]


def describe_agent(agent):
    a = agent.lower()
    device = "Tablet" if ("ipad" in a or ("android" in a and "mobile" not in a)) else "Phone" if ("mobile" in a or "iphone" in a) else "Desktop"
    system = ("iPhone" if "iphone" in a else "iPad" if "ipad" in a else "Android" if "android" in a else "Windows" if "windows" in a
              else "Mac" if "mac os" in a else "Linux" if "linux" in a else "Other")
    browser = ("Edge" if "edg/" in a else "Opera" if "opr/" in a else "Samsung" if "samsungbrowser" in a else "Chrome" if "chrome/" in a
               else "Firefox" if "firefox/" in a else "Safari" if "safari/" in a else "Other")
    return device, browser, system


def traffic_source(referer, host):
    if not referer:
        return "Direct / typed"
    try:
        from urllib.parse import urlparse
        ref = urlparse(referer).netloc.lower()
    except Exception:
        return "Other"
    if not ref or ref == host:
        return None          # moving around the site, not a new arrival
    for key, label in (("discord", "Discord"), ("twitch", "Twitch"), ("google", "Google"), ("bing", "Bing"), ("t.co", "Twitter / X"),
                       ("twitter", "Twitter / X"), ("x.com", "Twitter / X"), ("tiktok", "TikTok"), ("youtube", "YouTube"),
                       ("instagram", "Instagram"), ("reddit", "Reddit")):
        if key in ref:
            return label
    return "Other sites"


def record_task_visit(bot, g, request):
    """Weekly tasks: a logged-in visit counts as a website day, and opening the installed app counts once."""
    try:
        session = read_session(g, request)
        if not session or "log_task_event" not in g:
            return
        gd = pick_guild(bot, g)
        if gd is None:
            return
        uid = session["user_id"]
        g["log_task_event"](gd.id, uid, "web_day", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        if request.query.get("source") == "app" or "standalone" in request.headers.get("Sec-CH-UA-Mode", ""):
            g["log_task_event"](gd.id, uid, "app", "")
    except Exception as error:
        print(f"Task visit failed: {error}")


def record_view(g, request, logged_in):
    agent = request.headers.get("User-Agent", "")
    if not agent or any(word in agent.lower() for word in BOT_WORDS):
        return
    device, browser, system = describe_agent(agent)
    visitor = _hash(client_ip(request) + "|" + agent + "|" + _daily_salt())[:24]
    source = traffic_source(request.headers.get("Referer"), request.host)
    installed = 1 if request.query.get("source") == "app" or request.headers.get("Sec-Fetch-Dest") == "document" and "standalone" in request.headers.get("Sec-CH-UA-Mode", "") else 0
    try:
        g["discipline_query"](
            "INSERT INTO site_views (day, at, visitor, path, device, browser, system, source, country, logged_in, installed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).strftime("%Y-%m-%d"), int(time.time()), visitor, request.path[:120], device, browser,
             system, source, (request.headers.get("CF-IPCountry") or "")[:2] or None, 1 if logged_in else 0, installed), write=True
        )
    except Exception as error:
        print(f"Could not record a visit: {error}")


_cache = {}


_locks = {}
_build_slots = None


def _store(key, result):
    _cache[key] = (time.time(), result)
    if len(_cache) > 400:
        for old in sorted(_cache, key=lambda k: _cache[k][0])[:200]:
            _cache.pop(old, None)
            _locks.pop(old, None)


async def build(bot, g, key, maker):
    """
    Returns a page. Busy moments are handled like this:
      - a page built in the last minute is reused for everyone;
      - if lots of people ask for the same page at once, it's built ONCE
        and they all get that copy;
      - while a page is being refreshed, visitors get the previous copy
        straight away instead of waiting;
      - pages are built away from the bot's own work, so the Discord bot
        never freezes, however many people visit.
    """

    global _build_slots
    if _build_slots is None:
        _build_slots = asyncio.Semaphore(MAX_BUILDS_AT_ONCE)

    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]

    lock = _locks.setdefault(key, asyncio.Lock())

    if hit and lock.locked():
        return hit[1]

    async with lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]

        guild = pick_guild(bot, g)
        if guild is None:
            return None

        async with _build_slots:
            result = await asyncio.to_thread(maker, Ctx(g, guild))

        _store(key, result)
        return result


# ============================================================
# LOGIN ROUTES AND ACTION GUARD
# ============================================================

class ActionError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def redirect(url):
    return web.Response(status=302, headers={"Location": url, "Cache-Control": "no-store"})


def jres(data, status=200):
    return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})


def viewer_for(bot, g, request):
    return Viewer(g, pick_guild(bot, g), read_session(g, request))


async def guard(bot, g, request, need="member", bucket="action", limit=30, window=60):
    """Checks an action request. Returns (viewer, body). Raises ActionError."""

    if request.method != "POST":
        raise ActionError("Use POST.", 405)
    viewer = viewer_for(bot, g, request)
    if not viewer.session:
        raise ActionError("Log in with Discord first.", 401)
    sent = request.headers.get("X-CSRF", "")
    if not viewer.csrf or not hmac.compare_digest(sent, viewer.csrf):
        raise ActionError("Your session expired. Refresh the page and try again.", 403)
    if not viewer.ok:
        raise ActionError("You need to be in the PFO Discord to do this.", 403)
    if need != "member" and not viewer.levels.get(need):
        raise ActionError("You don't have permission to do that.", 403)
    if not rate_ok(f"{bucket}:{viewer.user_id}", limit, window) or not rate_ok(f"ip:{client_ip(request)}", 120, 60):
        raise ActionError("Slow down a little and try again in a minute.", 429)
    try:
        body = await request.json() if request.can_read_body else {}
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return viewer, body


def action_route(bot, g, need="member", bucket="action", limit=30, window=60):
    """Decorator: wraps a handler(viewer, body, request) -> dict into a guarded JSON endpoint."""

    def wrap(fn):
        async def handler(request):
            try:
                viewer, body = await guard(bot, g, request, need, bucket, limit, window)
                result = await fn(viewer, body, request)
                return jres(result if isinstance(result, dict) else {"ok": True, "message": str(result)})
            except ActionError as error:
                return jres({"ok": False, "message": error.message}, error.status)
            except Exception as error:
                traceback.print_exc()
                note = g.get("note_error")
                if note:
                    note(f"Website action: {request.path}", error)
                return jres({"ok": False, "message": "Something went wrong. Staff have been told."}, 500)
        return handler
    return wrap


def add_account_routes(app, bot, g):

    async def login(request):
        if not LOGIN_ON:
            return web.Response(text="Logging in isn't set up yet.", status=503)
        if not rate_ok(f"login:{client_ip(request)}", 20, 600):
            return web.Response(text="Too many login attempts. Try again in a few minutes.", status=429)
        nxt = request.query.get("next", "/me")
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/me"
        state = secrets.token_urlsafe(16)
        from urllib.parse import urlencode as _ue
        url = "https://discord.com/oauth2/authorize?" + _ue({
            "client_id": DISCORD_CLIENT_ID, "redirect_uri": f"{SITE_URL}/auth/callback",
            "response_type": "code", "scope": "identify", "state": state, "prompt": "none",
        })
        response = redirect(url)
        response.set_cookie(STATE_COOKIE, f"{state}|{nxt}", max_age=600, httponly=True, secure=secure_cookie_flag(), samesite="Lax", path="/")
        return response

    async def callback(request):
        if not LOGIN_ON:
            return web.Response(text="Logging in isn't set up yet.", status=503)
        saved = request.cookies.get(STATE_COOKIE, "")
        state, _, nxt = saved.partition("|")
        if not state or not hmac.compare_digest(state, request.query.get("state", "")):
            return redirect("/login-problem?why=state")
        code = request.query.get("code")
        if not code:
            return redirect("/")
        import aiohttp
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
                async with http.post("https://discord.com/api/oauth2/token", data={
                    "client_id": DISCORD_CLIENT_ID, "client_secret": DISCORD_CLIENT_SECRET, "grant_type": "authorization_code",
                    "code": code, "redirect_uri": f"{SITE_URL}/auth/callback",
                }, headers={"Content-Type": "application/x-www-form-urlencoded"}) as r:
                    token = await r.json()
                access = token.get("access_token")
                if not access:
                    return redirect("/login-problem?why=discord")
                async with http.get("https://discord.com/api/users/@me", headers={"Authorization": f"Bearer {access}"}) as r:
                    user = await r.json()
        except Exception as error:
            note = g.get("note_error")
            if note:
                note("Website login", error)
            return redirect("/login-problem?why=discord")

        try:
            user_id = int(user["id"])
        except Exception:
            return redirect("/login-problem?why=discord")
        guild = pick_guild(bot, g)
        if not guild or not guild.get_member(user_id):
            return redirect("/login-problem?why=member")

        now = int(time.time())
        g["discipline_query"](
            "INSERT INTO web_accounts (user_id, first_login, last_login, logins, username) VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_login = excluded.last_login, logins = logins + 1, username = excluded.username",
            (user_id, now, now, str(user.get("username", ""))[:40]), write=True
        )
        session = create_session(g, user_id)
        response = redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/me")
        response.set_cookie(SESSION_COOKIE, session, max_age=SESSION_DAYS * 86400, httponly=True, secure=secure_cookie_flag(),
                            samesite="Lax", path="/")
        response.del_cookie(STATE_COOKIE, path="/")
        return response

    async def login_problem(request):
        why = request.query.get("why", "")
        text = {
            "member": "You need to be in the PFO Discord to log in. Join the server, then try again.",
            "state": "That login link expired. Please try again.",
        }.get(why, "Discord didn't let us log you in. Please try again in a minute.")
        guild = pick_guild(bot, g)
        if guild is None:
            return web.Response(text=text)
        body = page_hero("LOGGING IN", "COULDN'T LOG IN", "?", f'<p class="muted l-up">{esc(text)}</p><div class="btns l-up"><a class="btn btn-gold" href="/login">TRY AGAIN</a>{discord_button()}</div>')
        return web.Response(text=page(Ctx(g, guild), "Log in", "", body), content_type="text/html")

    async def logout(request):
        viewer = viewer_for(bot, g, request)
        everywhere = request.query.get("all") == "1"
        if request.method == "POST" and viewer.session and hmac.compare_digest(request.headers.get("X-CSRF", ""), viewer.csrf or ""):
            if everywhere:
                g["discipline_query"]("DELETE FROM web_sessions WHERE user_id = ?", (viewer.user_id,), write=True)
            else:
                g["discipline_query"]("DELETE FROM web_sessions WHERE token_hash = ?", (viewer.session["token_hash"],), write=True)
        response = jres({"ok": True})
        response.del_cookie(SESSION_COOKIE, path="/")
        return response

    async def me(request):
        viewer = viewer_for(bot, g, request)
        if not viewer.ok:
            return jres({"logged_in": False, "login": LOGIN_ON})
        member = viewer.member
        return jres({
            "logged_in": True, "login": LOGIN_ON, "id": str(viewer.user_id), "csrf": viewer.csrf,
            "name": clean_name(Ctx(g, pick_guild(bot, g)), member.display_name),
            "avatar": str(getattr(getattr(member, "display_avatar", None), "url", "") or ""),
            "levels": viewer.levels,
            "coins": g["get_wallet"](member.guild.id if getattr(member, "guild", None) else pick_guild(bot, g).id, viewer.user_id)["balance"],
        })

    app.router.add_get("/login", login)
    app.router.add_get("/auth/callback", callback)
    app.router.add_get("/login-problem", login_problem)
    app.router.add_post("/logout", logout)
    app.router.add_get("/api/me", me)


# ============================================================
# LOGGED-IN ACTIONS (JSON)
# Every action is checked by guard(): logged in, CSRF token, role,
# rate limit. The bot's own functions do the work, so the rules are
# exactly the same as in Discord.
# ============================================================

class _Shim:
    """Looks enough like a Discord interaction for the bot's shop code."""
    def __init__(self, guild, member):
        self.guild = guild
        self.user = member
        self.channel = None


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def media_dir(g):
    folder = os.path.join(os.path.dirname(os.path.abspath(g["DATABASE_FILE"])), "uploads")
    os.makedirs(folder, exist_ok=True)
    return folder


def save_upload(g, data: bytes):
    """Checks and stores an image. Returns the file name or raises ActionError."""
    if len(data) > 5 * 1024 * 1024:
        raise ActionError("Images can be up to 5 MB.")
    kind = ("jpg" if data[:3] == b"\xff\xd8\xff" else "png" if data[:8] == b"\x89PNG\r\n\x1a\n"
            else "gif" if data[:4] == b"GIF8" else "webp" if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else None)
    if not kind:
        raise ActionError("Only JPG, PNG, GIF or WEBP images.")
    try:
        from PIL import Image
        import io as _io
        image = Image.open(_io.BytesIO(data))
        if kind != "gif":
            image.thumbnail((1600, 1600))
            out = _io.BytesIO()
            if image.mode in ("RGBA", "LA", "P"):
                image = image.convert("RGBA")
                image.save(out, "PNG", optimize=True)
                kind = "png"
            else:
                image.convert("RGB").save(out, "JPEG", quality=85, optimize=True)   # re-saved: removes hidden data like location
                kind = "jpg"
            data = out.getvalue()
    except ImportError:
        pass
    except Exception:
        raise ActionError("That image couldn't be read.")
    name = secrets.token_hex(12) + "." + kind
    folder = media_dir(g)
    with open(os.path.join(folder, name), "wb") as file:
        file.write(data)
    try:
        cutoff = time.time() - 120 * 86400
        for old in os.listdir(folder):
            path = os.path.join(folder, old)
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
    except Exception:
        pass
    return name


def add_action_routes(app, bot, g):

    def guild():
        return pick_guild(bot, g)

    def ctx():
        return Ctx(g, guild())

    # ---------------- sign-ups ----------------
    @action_route(bot, g, bucket="signup", limit=10, window=60)
    async def signup(viewer, body, request):
        if not viewer.levels["fighter"]:
            raise ActionError("You need the Fighter role to sign up. Verify in the Discord first, then pick your fighter.", 403)
        session = g["get_session"](_int(body.get("session_id"), 0))
        gd = guild()
        if not session or session["guild_id"] != gd.id or session["active"] != 1:
            raise ActionError("Those sign-ups are closed.")
        if body.get("action") == "withdraw":
            if not g["is_signed_up"](session["id"], viewer.user_id):
                raise ActionError("You're not on that list.")
            g["remove_signup"](session["id"], viewer.user_id)
            message = "You've been taken off the list."
        else:
            suspension = g["get_active_suspension"](gd.id, viewer.user_id)
            if suspension:
                raise ActionError("You're suspended, so you can't sign up right now.")
            if not g["add_signup"](session["id"], viewer.user_id, viewer.member.display_name):
                raise ActionError("You're already signed up.")
            message = "You're signed up! Signing up doesn't guarantee a fight. Staff pick the card."
        try:
            await g["refresh_signup_message"](gd, g["get_session"](session["id"]), session["signup_type"])
        except Exception:
            pass
        return {"ok": True, "message": message, "count": g["get_signup_count"](session["id"]),
                "signed": g["is_signed_up"](session["id"], viewer.user_id)}

    # ---------------- follow ----------------
    @action_route(bot, g, bucket="follow", limit=30, window=60)
    async def follow(viewer, body, request):
        fighter_id = _int(body.get("fighter_id"))
        if not fighter_id or fighter_id == viewer.user_id:
            raise ActionError("Pick a fighter.")
        result = g["toggle_follow"](guild().id, viewer.user_id, fighter_id)
        if result is None:
            raise ActionError("You can follow up to 50 fighters.")
        if result and "log_task_event" in g:
            g["log_task_event"](guild().id, viewer.user_id, "web_follow", fighter_id)
        return {"ok": True, "following": result, "message": "Following" if result else "Unfollowed"}

    async def following(request):
        viewer = viewer_for(bot, g, request)
        if not viewer.ok:
            return jres({"ids": []})
        return jres({"ids": [str(i) for i in g["followed_ids"](guild().id, viewer.user_id)]})

    # ---------------- betting ----------------
    async def odds(request):
        market_id = _int(request.query.get("market"))
        market = g["get_market"](market_id) if market_id else None
        if not market or market["guild_id"] != guild().id:
            return jres({"ok": False}, 404)
        out = {}
        for bout in g["market_bouts"](market_id):
            if bout["result"]:
                continue
            live = g["live_odds"](bout)
            methods = {}
            for pick in ("a", "b"):
                for method in ("KO/TKO", "SUB", "DEC", *(g.get("ROUND_PICKS") or {})):
                    try:
                        methods[f"{pick}{method}"] = g["pick_odds"](bout, pick, method)
                    except Exception:
                        pass
            pools = g["fight_pools"](market_id, bout["position"])
            out[str(bout["position"])] = {"a": live["a"], "b": live["b"], "open_a": live["open_a"], "open_b": live["open_b"],
                                          "pool": live["pool"], "pool_a": pools["a"], "methods": methods}
        return jres({"ok": True, "open": bool(g["market_is_open"](market)), "odds": out, "at": int(time.time())})

    @action_route(bot, g, bucket="bet", limit=20, window=60)
    async def bet(viewer, body, request):
        legs = {}
        for leg in (body.get("legs") or [])[:25]:
            position = _int(leg.get("pos"))
            pick = leg.get("pick")
            method = leg.get("method") or None
            if position is None or pick not in ("a", "b"):
                continue
            if method not in g["METHOD_NAMES"] and method not in (g.get("ROUND_PICKS") or {}):
                method = None
            legs[position] = (pick, method)
        stake = _int(body.get("stake"), 0)
        ok, message, bet_id = g["place_bet"](guild(), viewer.user_id, _int(body.get("market_id"), 0), legs, stake)
        if not ok:
            raise ActionError(message.replace("❌ ", "").replace("🔒 ", ""))
        if "log_task_event" in g:
            g["log_task_event"](guild().id, viewer.user_id, "web_bet", bet_id)
        return {"ok": True, "message": message.replace("✅ ", ""), "balance": g["get_wallet"](guild().id, viewer.user_id)["balance"]}

    async def my_bets(request):
        viewer = viewer_for(bot, g, request)
        if not viewer.ok:
            return jres({"ok": False, "bets": []}, 401)
        gd = guild()
        rows = g["discipline_query"]("SELECT bets.*, bet_markets.event FROM bets JOIN bet_markets ON bet_markets.id = bets.market_id "
                                     "WHERE bets.guild_id = ? AND bets.user_id = ? ORDER BY bets.id DESC LIMIT 12", (gd.id, viewer.user_id))
        out = []
        for bet_row in rows:
            legs = g["discipline_query"]("""
                SELECT bet_legs.*, bet_fights.name_a, bet_fights.name_b FROM bet_legs
                JOIN bet_fights ON bet_fights.market_id = ? AND bet_fights.position = bet_legs.position
                WHERE bet_legs.bet_id = ? ORDER BY bet_legs.position""", (bet_row["market_id"], bet_row["id"]))
            offer = None
            if bet_row["status"] == "open" and len(legs) > 1:
                offer, _ = g["cashout_offer"](bet_row["id"])
            can_refund = bet_row["status"] == "open" and len(legs) == 1 and g["refund_check"](bet_row["id"])[0]
            out.append({
                "id": bet_row["id"], "event": bet_row["event"], "stake": bet_row["stake"], "odds": bet_row["odds"],
                "status": bet_row["status"], "payout": bet_row["payout"], "returns": round(bet_row["stake"] * bet_row["odds"]),
                "cashout": offer,
                "refund": g["refund_minutes_left"](bet_row) if can_refund else None,
                "legs": [{"label": g["pick_label"](leg, leg["pick"], leg["method"]), "odds": leg["odds"], "status": leg["status"]} for leg in legs],
            })
        return jres({"ok": True, "bets": out, "balance": g["get_wallet"](gd.id, viewer.user_id)["balance"]})

    @action_route(bot, g, bucket="cashout", limit=10, window=60)
    async def bet_refund(viewer, body, request):
        ok, message = g["refund_bet"](guild().id, viewer.user_id, _int(body.get("bet_id"), 0))
        if not ok:
            raise ActionError(message.replace("❌ ", ""))
        return {"ok": True, "message": message.replace("↩️ ", ""), "balance": g["get_wallet"](guild().id, viewer.user_id)["balance"]}

    @action_route(bot, g, bucket="cashout", limit=10, window=60)
    async def cashout(viewer, body, request):
        ok, message = g["cash_out"](guild().id, viewer.user_id, _int(body.get("bet_id"), 0), _int(body.get("expected")))
        if not ok:
            raise ActionError(message.replace("❌ ", ""))
        return {"ok": True, "message": message.replace("💸 ", ""), "balance": g["get_wallet"](guild().id, viewer.user_id)["balance"]}

    # ---------------- fantasy ----------------
    async def fantasy_me(request):
        viewer = viewer_for(bot, g, request)
        if not viewer.ok:
            return jres({"ok": False}, 401)
        gd, c = guild(), ctx()
        team = g["fantasy_team"](gd.id, viewer.user_id)
        players, fighters, per_pick = g["fantasy_scores"](gd.id)
        order = sorted(players.items(), key=lambda item: -item[1])
        rank = next((i for i, (uid, _) in enumerate(order, start=1) if uid == viewer.user_id), None)
        current = g["open_fantasy_round"](gd.id)
        played = g["fantasy_has_played"](gd.id, viewer.user_id)
        pool = []
        for price, name, fid in g["fantasy_price_rows"](gd):
            pool.append({"id": str(fid), "name": clean_name(c, name), "price": price, "points": fighters.get(fid, 0),
                         "division": main_division(c, fid) or ""})
        leagues = []
        for league in g["fantasy_user_leagues"](gd.id, viewer.user_id):
            table = g["fantasy_league_table"](gd.id, league["id"], players)
            leagues.append({"id": league["id"], "name": league["name"], "code": league["code"], "members": league["members"],
                            "table": [{"name": player_name(c, uid), "points": pts, "me": uid == viewer.user_id} for uid, pts in table[:30]]})
        history = []
        for row in g["fantasy_history"](gd.id, viewer.user_id):
            history.append({"event": row["event"], "points": row["points"], "rank": row["rank"], "of": row["of"],
                            "captain": fighter_name(c, row["captain_id"]) if row["captain_id"] else "",
                            "best": fighter_name(c, row["best_id"]) if row["best_id"] else "", "best_points": row["best_points"]})
        return jres({
            "ok": True, "budget": g["FANTASY_BUDGET"], "size": g["FANTASY_TEAM_SIZE"],
            "bank": g["fantasy_bank"](gd.id, viewer.user_id), "points": players.get(viewer.user_id, 0), "rank": rank, "of": len(order),
            "round": {"event": current["event"], "locks_at": current["locks_at"], "locks_text": event_when(current["locks_at"])} if current else None,
            "swaps_left": g["swaps_left"](gd.id, viewer.user_id, current) if current else None,
            "captain_left": g["captain_changes_left"](gd.id, viewer.user_id, current) if current else None,
            "free_changes": (not current and not played),
            "team": [{"pick_id": pick["id"], "id": str(pick["fighter_id"]), "name": fighter_name(c, pick["fighter_id"]),
                      "paid": pick["paid"], "captain": bool(pick["captain"]), "points": per_pick.get((viewer.user_id, pick["fighter_id"]), 0),
                      "division": main_division(c, pick["fighter_id"]) or ""} for pick in team],
            "pool": pool, "leagues": leagues, "history": history,
        })

    @action_route(bot, g, bucket="fantasy", limit=30, window=60)
    async def fantasy_action(viewer, body, request):
        gd = guild()
        what = body.get("action")
        if what == "add":
            member = gd.get_member(_int(body.get("fighter_id"), 0))
            if not member:
                raise ActionError("That fighter isn't in the server.")
            message = g["fantasy_add"](gd, viewer.user_id, member)
        elif what == "remove":
            message = g["fantasy_remove"](gd, viewer.user_id, _int(body.get("pick_id"), 0))
        elif what == "captain":
            message = g["fantasy_change_captain"](gd.id, viewer.user_id, _int(body.get("pick_id"), 0))
        elif what == "league_create":
            ok, message, _ = g["fantasy_league_create"](gd.id, viewer.user_id, str(body.get("name", "")))
        elif what == "league_join":
            ok, message, _ = g["fantasy_league_join"](gd.id, viewer.user_id, str(body.get("code", "")))
        elif what == "league_leave":
            message = g["fantasy_league_leave"](gd.id, viewer.user_id, _int(body.get("league_id"), 0))
        else:
            raise ActionError("Unknown action.")
        bad = message.startswith("❌")
        clean = re.sub(r"^[^\w\d(]+", "", message).replace("**", "")
        if bad:
            raise ActionError(clean)
        if what in ("add", "remove", "captain") and "log_task_event" in g:
            g["log_task_event"](gd.id, viewer.user_id, "web_fantasy", f"{what}:{int(time.time())}")
        return {"ok": True, "message": clean}

    # ---------------- shop ----------------
    @action_route(bot, g, bucket="shop", limit=12, window=60)
    async def shop_buy(viewer, body, request):
        gd = guild()
        item = str(body.get("item", ""))
        if item not in g["SHOP_ITEMS"]:
            raise ActionError("That item doesn't exist.")
        target = gd.get_member(_int(body.get("target_id"), 0)) if body.get("target_id") else None
        if item == "mystery_box" and g["boxes_today"](gd.id, viewer.user_id) >= g["BOX_PER_DAY"]:
            raise ActionError(f"You can open {g['BOX_PER_DAY']} mystery boxes a day.")
        message = await g["buy_item"](_Shim(gd, viewer.member), item, target=target, choice=body.get("choice") or None,
                                      text=(str(body.get("text") or "")[:300] or None), amount=_int(body.get("amount")))
        if message.startswith("❌"):
            raise ActionError(re.sub(r"<@!?(\d+)>", "them", message[2:].replace("**", "")))
        clean = re.sub(r"<@!?(\d+)>", lambda m: "@" + player_name(ctx(), int(m.group(1))), message).replace("**", "")
        clean = clean.replace(g["COIN"], "").replace("  ", " ")
        out = {"ok": True, "message": clean, "balance": g["get_wallet"](gd.id, viewer.user_id)["balance"]}
        if "log_task_event" in g:
            g["log_task_event"](gd.id, viewer.user_id, "web_shop", f"{item}:{int(time.time())}")
        if item in g.get("PACK_ITEMS", {}):
            out["pack_id"] = g["_last_pack"].pop((gd.id, viewer.user_id), None)
        return out

    async def members_search(request):
        viewer = viewer_for(bot, g, request)
        if not viewer.ok:
            return jres({"results": []}, 401)
        q = (request.query.get("q") or "").strip().lower()[:30]
        only_fighters = request.query.get("fighters") == "1"
        gd, c = guild(), ctx()
        results = []
        if len(q) >= 2:
            for member in getattr(gd, "members", []):
                if member.bot:
                    continue
                if only_fighters and not g["has_any_role"](member, g["FIGHTER_ROLE_NAMES"]):
                    continue
                name = clean_name(c, member.display_name)
                if q in name.lower() or q in (member.name or "").lower():
                    results.append({"id": str(member.id), "name": name})
                if len(results) >= 10:
                    break
        return jres({"results": results})

    # ---------------- social ----------------
    async def social_feed(request):
        viewer = viewer_for(bot, g, request)
        gd, c = guild(), ctx()
        sort = request.query.get("sort", "new")
        before = _int(request.query.get("before"))
        where = "guild_id = ? AND deleted = 0 AND hidden = 0"
        params = [gd.id]
        if before:
            where += " AND id < ?"
            params.append(before)
        if sort == "following" and viewer.ok:
            ids = g["followed_ids"](gd.id, viewer.user_id)
            if not ids:
                return jres({"posts": [], "empty": "Follow fighters to see their posts here."})
            where += f" AND user_id IN ({','.join('?' * len(ids))})"
            params += ids
        order = "id DESC"
        if sort == "top":
            where += " AND created_at > ?"
            params.append(int(time.time()) - 86400)
            order = "(SELECT COALESCE(SUM(value), 0) FROM social_votes v WHERE v.post_id = social_posts.id) DESC, id DESC"
        rows = g["discipline_query"](f"SELECT * FROM social_posts WHERE {where} ORDER BY {order} LIMIT 20", tuple(params))
        if sort == "fighters":
            rows = [r for r in rows if (m := gd.get_member(r["user_id"])) and g["has_any_role"](m, g["FIGHTER_ROLE_NAMES"])]
        posts = []
        for row in rows:
            member = gd.get_member(row["user_id"])
            likes, dislikes, comments = g["social_counts"](row["id"])
            mine = 0
            if viewer.ok:
                vote = g["discipline_query"]("SELECT value FROM social_votes WHERE post_id = ? AND user_id = ?", (row["id"], viewer.user_id))
                mine = vote[0]["value"] if vote else 0
            levels = member_levels(g, gd, member)
            rank = None
            if member:
                division = main_division(c, member.id)
                r = rank_in(c, member.id, division) if division else None
                rank = f"{division} {rank_label(r)}" if r is not None else None
            com = g["discipline_query"]("SELECT * FROM social_comments WHERE post_id = ? AND deleted = 0 ORDER BY id LIMIT 30", (row["id"],))
            posts.append({
                "id": row["id"], "user_id": str(row["user_id"]), "name": player_name(c, row["user_id"]),
                "avatar": str(getattr(getattr(member, "display_avatar", None), "url", "") or "") if member else "",
                "badge": "STAFF" if levels["staff"] else "FIGHTER" if levels["fighter"] else "", "rank": rank,
                "text": row["text"], "image": f"/media/{row['image']}" if row["image"] else None, "event": row["event"],
                "at": row["created_at"], "likes": likes, "dislikes": dislikes, "mine": mine,
                "own": viewer.ok and row["user_id"] == viewer.user_id,
                "comments": [{"id": cm["id"], "name": player_name(c, cm["user_id"]), "text": cm["text"],
                              "fighter": bool((mm := gd.get_member(cm["user_id"])) and g["has_any_role"](mm, g["FIGHTER_ROLE_NAMES"]))} for cm in com],
                "comment_count": comments,
            })
        return jres({"posts": posts, "staff": viewer.levels.get("staff", False)})

    async def social_post(request):
        try:
            if request.method != "POST":
                raise ActionError("Use POST.", 405)
            viewer = viewer_for(bot, g, request)
            if not viewer.session:
                raise ActionError("Log in with Discord to post.", 401)
            if not hmac.compare_digest(request.headers.get("X-CSRF", ""), viewer.csrf or ""):
                raise ActionError("Your session expired. Refresh the page and try again.", 403)
            if not viewer.ok:
                raise ActionError("You need to be in the PFO Discord to post.", 403)
            if not rate_ok(f"social:{viewer.user_id}", 6, 3600):
                raise ActionError("You can post 5 times an hour. Try again a bit later.", 429)
            form = await request.post()
            text = str(form.get("text") or "")
            event = str(form.get("event") or "")[:80] or None
            image = None
            upload = form.get("image")
            if upload is not None and getattr(upload, "file", None):
                image = save_upload(g, upload.file.read(5 * 1024 * 1024 + 1))
            gd = guild()
            ok, message, post_id = g["social_create"](gd, viewer.user_id, text, image, event)
            if ok and "log_task_event" in g:
                g["log_task_event"](gd.id, viewer.user_id, "web_social", post_id)
            if not ok:
                raise ActionError(message)
            await g["post_social_to_discord"](gd, post_id)
            return jres({"ok": True, "id": post_id, "message": "Posted, and shared in #social-media."})
        except ActionError as error:
            return jres({"ok": False, "message": error.message}, error.status)

    @action_route(bot, g, bucket="vote", limit=60, window=60)
    async def social_vote(viewer, body, request):
        value = 1 if _int(body.get("value")) == 1 else -1
        post_id = _int(body.get("post_id"), 0)
        if not g["discipline_query"]("SELECT 1 FROM social_posts WHERE id = ? AND deleted = 0", (post_id,)):
            raise ActionError("That post was removed.")
        now = g["social_vote"](post_id, viewer.user_id, value)
        likes, dislikes, _ = g["social_counts"](post_id)
        return {"ok": True, "mine": now, "likes": likes, "dislikes": dislikes}

    @action_route(bot, g, bucket="comment", limit=10, window=60)
    async def social_comment(viewer, body, request):
        ok, message = g["social_comment"](guild(), _int(body.get("post_id"), 0), viewer.user_id, str(body.get("text", "")))
        if not ok:
            raise ActionError(message)
        return {"ok": True, "message": message, "name": viewer.member.display_name}

    @action_route(bot, g, bucket="report", limit=10, window=600)
    async def social_report(viewer, body, request):
        g["social_report"](_int(body.get("post_id"), 0), viewer.user_id)
        return {"ok": True, "message": "Thanks. Staff will take a look."}

    @action_route(bot, g, bucket="remove", limit=30, window=60)
    async def social_remove(viewer, body, request):
        post_id = _int(body.get("post_id"), 0)
        comment_id = _int(body.get("comment_id"))
        rows = g["discipline_query"]("SELECT * FROM social_posts WHERE id = ?", (post_id,))
        if not rows:
            raise ActionError("Post not found.")
        own = rows[0]["user_id"] == viewer.user_id
        if comment_id:
            com = g["discipline_query"]("SELECT * FROM social_comments WHERE id = ?", (comment_id,))
            own = bool(com) and com[0]["user_id"] == viewer.user_id
        if not (own or viewer.levels["staff"]):
            raise ActionError("Only staff or the person who posted it can remove it.")
        await g["social_remove"](guild(), post_id, comment_id)
        return {"ok": True, "message": "Removed."}

    async def media(request):
        name = request.match_info.get("name", "")
        if not re.fullmatch(r"[0-9a-f]{24}\.(jpg|png|gif|webp)", name):
            raise web.HTTPNotFound()
        path = os.path.join(media_dir(g), name)
        if not os.path.exists(path):
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "public, max-age=604800", "X-Content-Type-Options": "nosniff"})

    for path, handler in (("/api/signup", signup), ("/api/follow", follow), ("/api/bet", bet), ("/api/bets/cashout", cashout), ("/api/bets/refund", bet_refund),
                          ("/api/fantasy", fantasy_action), ("/api/shop/buy", shop_buy), ("/api/social/post", social_post),
                          ("/api/social/vote", social_vote), ("/api/social/comment", social_comment),
                          ("/api/social/report", social_report), ("/api/social/remove", social_remove)):
        app.router.add_post(path, handler)
    for path, handler in (("/api/following", following), ("/api/odds", odds), ("/api/bets/me", my_bets),
                          ("/api/fantasy/me", fantasy_me), ("/api/members", members_search), ("/api/social/feed", social_feed),
                          ("/media/{name}", media)):
        app.router.add_get(path, handler)


# ============================================================
# PAGES ADDED WITH LOGINS
#   Public (cached, the same for everyone): shop, social, records,
#   awards, the week, get the app.
#   Personal (built for each visitor, never cached): My PFO, staff
#   dashboard, site stats.
# ============================================================

def twitch_bar(ctx):
    """The red LIVE bar on every page while PFO is streaming."""
    try:
        rows = ctx.q("SELECT * FROM twitch_state WHERE id = 1")
    except Exception:
        return ""
    if not rows or not rows[0]["live"] or not TWITCH_URL:
        return ""
    row = rows[0]
    viewers = f' · {row["viewers"]:,} watching' if row["viewers"] else ""
    return (f'<a class="livebar" href="/live#watch"><span class="dot dot-red" aria-hidden="true"></span>'
            f'<b>PFO IS LIVE</b><span class="livebar-t">{esc(row["title"] or "Watch on Twitch")}{viewers}</span>'
            f'<span class="livebar-go">WATCH →</span></a>')


def twitch_channel():
    match = re.search(r"twitch\.tv/([A-Za-z0-9_]+)", TWITCH_URL or "")
    return match.group(1) if match else None


def twitch_player(ctx):
    try:
        rows = ctx.q("SELECT * FROM twitch_state WHERE id = 1")
    except Exception:
        return ""
    channel = twitch_channel()
    if not rows or not rows[0]["live"] or not channel:
        return ""
    host = re.sub(r"^https?://", "", SITE_URL or "prestigefightingorg.com").split("/")[0].split(":")[0]
    return f"""
  <section class="block watch" id="watch" aria-label="Watch live">
    <div class="head-t rv"><span class="eyebrow"><span class="dot dot-red" aria-hidden="true"></span> ON TWITCH NOW</span><h2 class="d h-m">WATCH LIVE</h2></div>
    <div class="watch-frame"><iframe src="https://player.twitch.tv/?channel={esc(channel)}&parent={esc(host)}&muted=true"
      title="PFO on Twitch" allowfullscreen loading="lazy"></iframe></div>
    <p class="note"><a href="{esc(TWITCH_URL)}" rel="noopener" target="_blank">Open on Twitch →</a></p>
  </section>"""


def login_box(text, path):
    if not LOGIN_ON:
        return f'<p class="muted">{esc(text)} Logging in isn\'t switched on yet.</p>'
    return (f'<div class="loginbox rv"><p>{esc(text)}</p>'
            f'<a class="btn btn-gold" href="/login?next={quote(path, safe="")}">LOG IN WITH DISCORD</a>'
            f'<p class="note">Only asks Discord for your username and picture.</p></div>')


def pts(n):
    return f"{n:,}"


def coin_reason(reason):
    """A friendly label for a coin ledger line."""
    r = reason or ""
    table = [
        ("daily", "Daily spin"), ("cards:upgrade", "Card upgrade"), ("cards:sell", "Sold cards"), ("task:", "Weekly task"), ("bet:", "Bet paid out"), ("stake:", "Bet placed"),
        ("shoprefund:", "Shop refund"), ("shop:", "Shop"), ("loot:", "Loot drop"), ("fight:", "Fight pay"),
        ("ach:", "Achievement"), ("fantasy", "Fantasy prize"), ("gift", "Gift"), ("bounty", "Bounty"),
        ("jackpot", "Jackpot"), ("staff:", "From staff"), ("admin:", "From staff"), ("box", "Mystery box"),
    ]
    for start, label in table:
        if r.startswith(start) or (start.endswith(":") is False and start in r):
            return label
    return r.split(":")[0].replace("_", " ").capitalize() or "Coins"


def find_next_fight(ctx, user_id):
    """(opponent_id, event name, when unix or None, spot) for a card they're on that isn't finished."""
    try:
        for notice in ctx.q("SELECT * FROM card_notices WHERE guild_id = ? ORDER BY id DESC LIMIT 4", (ctx.gid,)):
            created = datetime.fromisoformat(str(notice["created_at"]).replace("Z", "+00:00")).timestamp()
            if time.time() - created > 10 * 86400:
                continue
            logged, _ = ctx.notice_fights(ctx.gid, notice)
            done = {frozenset((f["winner_id"], f["loser_id"])) for f in logged}
            for a, b, spot in ctx.active_layout(notice):
                if user_id in (a, b) and frozenset((a, b)) not in done:
                    raw = str(row_get(notice, "event_date" if notice["kind"] == "live_card" else "deadline", ""))
                    name = row_get(notice, "event") or ("Live Card" if notice["kind"] == "live_card" else "Fight Night")
                    return (b if a == user_id else a), name, (int(raw) if raw.isdigit() else None), spot
    except Exception:
        traceback.print_exc()
    return None


def signup_cards(ctx, viewer):
    out = ""
    for signup_type in ("live_card", "fight_night"):
        session = ctx.get_active_session(ctx.gid, signup_type)
        if not session:
            continue
        try:
            title = ctx.signup_title(signup_type, session["id"])
        except Exception:
            title = ctx.SIGNUP_INFO[signup_type]["title"]
        signed = ctx.is_signed_up(session["id"], viewer.user_id)
        count = ctx.get_signup_count(session["id"])
        closes = row_get(session, "closes_at")
        if viewer.levels["fighter"]:
            button = (f'<button class="btn {"btn-ghost" if signed else "btn-gold"} btn-sm" type="button" data-signup="{session["id"]}" '
                      f'data-signed="{1 if signed else 0}">{"WITHDRAW" if signed else "SIGN UP"}</button>')
        else:
            button = '<span class="muted">Get the Fighter role in Discord to sign up.</span>'
        out += (f'<div class="sucard{" sucard-on" if signed else ""}"><span class="eyebrow-grey">{"YOU\'RE SIGNED UP" if signed else "SIGN-UPS OPEN"}</span>'
                f'<span class="d sucard-t">{esc(title)}</span>'
                f'<span class="muted"><span data-count-for="{session["id"]}">{count}</span> signed up'
                f'{" · closes " + when_html(closes) if closes else ""}</span>{button}</div>')
    return out


# ------------------------------------------------------------
# My PFO
# ------------------------------------------------------------

def render_me(ctx, viewer, query):
    uid = viewer.user_id
    gid = ctx.gid
    member = viewer.member
    name = clean_name(ctx, member.display_name)
    wallet = ctx.get_wallet(gid, uid)
    stats = stats_for(ctx, uid)
    fights = stats["fights"] if stats else []
    division = main_division(ctx, uid)
    rank = rank_in(ctx, uid, division) if division else None

    # ---------- hero ----------
    chips = chip("COINS", f'{wallet["balance"]:,}', "gold")
    if stats and fights:
        chips += chip("RECORD", esc(ctx.stats_record(stats)))
    if division:
        chips += chip(division.upper(), esc(rank_label(rank)) if rank is not None else "UNRANKED")
    try:
        if ctx.is_prospect(stats) and not ctx.is_any_champion(gid, uid):
            chips += chip("TAG", "🌱 PROSPECT")
    except Exception:
        pass
    links = ""
    if fights or ctx.get_user_rankings(gid, uid):
        links += f'<a class="btn btn-ghost btn-sm" href="{fighter_url(uid)}">MY FIGHTER PAGE</a>'
    links += '<a class="btn btn-ghost btn-sm" href="/shop">SHOP</a><a class="btn btn-ghost btn-sm" href="/me/cards">MY CARDS</a><a class="btn btn-ghost btn-sm" href="/app">GET THE APP</a>'
    hero = f"""
<section class="mehero">
  <div class="wrap mehero-in">
    {avatar_html(ctx, uid, name, 'av av-hero')}
    <div class="mehero-t">
      <span class="eyebrow">MY PFO</span>
      <h1 class="d h-l">{esc(name.upper())}</h1>
      <div class="evchips l-up">{chips}</div>
      <div class="btns">{links}</div>
    </div>
  </div>
</section>"""

    blocks = []

    # ---------- daily spin ----------
    if "do_spin" in ctx.g:
        try:
            can, _, streak = ctx.spin_status(gid, uid)
            prizes = json.dumps([[p[2], "#%02x%02x%02x" % c] for p, c in zip(ctx.SPIN_PRIZES, ctx.SPIN_COLOURS)])
            odds = " · ".join(f"{p[2]} {p[3]:g}%" for p in ctx.SPIN_PRIZES)
            state = ("Your free spin is ready." if can else f"You've spun today. Next spin {when_html(ctx.next_spin_unix())}.")
            blocks.append(("DAILY SPIN", "spin",
                           f'<div class="spinbox"><div class="spinwheel"><span class="spinptr" aria-hidden="true"></span>'
                           f'<canvas data-spinwheel=\'{esc(prizes)}\' width="560" height="560" aria-label="Daily spin wheel"></canvas></div>'
                           f'<div class="spinside"><p class="muted" data-spinstate>{state}</p>'
                           f'<p class="muted">Spin streak <b class="gold">{streak}</b> · every {ctx.SPIN_STREAK_DAYS}th day in a row, '
                           f'nothing below {ctx.SPIN_STREAK_MIN} coins.</p>'
                           f'<button class="btn btn-gold" type="button" data-spin{"" if can else " disabled"}>{"SPIN" if can else "COME BACK TOMORROW"}</button>'
                           f'<p class="note">{esc(odds)}</p></div></div>'))
        except Exception:
            traceback.print_exc()

    # ---------- next fight + sign-ups ----------
    nxt = find_next_fight(ctx, uid)
    fight_html = ""
    if nxt:
        opp, event, when, spot = nxt
        fight_html = (f'<div class="nextfight"><span class="eyebrow-grey">YOUR NEXT FIGHT · {esc(event.upper())}'
                      f'{" · " + esc(spot_label(spot)) if spot else ""}</span>'
                      f'<div class="nf-row">{avatar_html(ctx, uid, name, "av")}<span class="d">{esc(name)}</span><span class="d nf-vs">VS</span>'
                      f'<span class="d">{name_link(ctx, opp, fighter_name(ctx, opp))}</span>{avatar_html(ctx, opp, fighter_name(ctx, opp), "av")}</div>'
                      + (f'<span class="muted">{when_html(when)} · <span data-countdown-small="{when}"></span></span>' if when else "")
                      + f'<a class="btn btn-ghost btn-sm" href="/compare?a={uid}&b={opp}">TALE OF THE TAPE</a></div>')
    signups = signup_cards(ctx, viewer)
    if fight_html or signups:
        blocks.append(("FIGHTING", "fight", fight_html + (f'<div class="sugrid">{signups}</div>' if signups else "")))

    # ---------- my fighter ----------
    if stats and fights:
        form = ""
        for fight in fights[-5:]:
            if fight["method"] == "NC":
                r, cls = "NC", "pill-nc"
            elif fight["method"] == "DRAW":
                r, cls = "D", "pill-d"
            elif ctx.fight_is_win_for(fight, uid):
                r, cls = "W", "pill-w"
            else:
                r, cls = "L", "pill-l"
            form += f'<span class="pill {cls}">{r}</span>'
        last = "".join(result_row(ctx, f, i) for i, f in enumerate(reversed(fights[-3:])))
        wins_by = stats["wins_by"]
        tiles = "".join(f'<div class="ftile"><span class="d ftile-v">{esc(v)}</span><span class="num-l">{label}</span></div>' for label, v in (
            ("KO / TKO", wins_by.get("KO/TKO", 0)), ("SUBS", wins_by.get("SUB", 0)), ("DECISIONS", wins_by.get("DEC", 0)),
            ("STREAK", stats["streak"] or "–")))
        blocks.append(("MY FIGHTER", "fighter",
                       f'<div class="mefighter"><span class="d fhero-record">{esc(ctx.stats_record(stats))}</span>'
                       f'<span class="form">{form}</span></div><div class="ftiles ftiles-4">{tiles}</div>'
                       f'<h3 class="sec-h">LAST FIGHTS</h3><div class="list">{last}</div>'))

    # ---------- bets ----------
    blocks.append(("MY BETS", "bets", '<div data-mybets><p class="muted">Loading your bets…</p></div>'
                   '<p class="note"><a href="/sportsbook">Go to the sportsbook →</a> Coins are points only and have no money value.</p>'))

    # ---------- bet stats ----------
    if "bet_stats" in ctx.g:
        try:
            bs = ctx.bet_stats(gid, uid)
            if bs["won"] + bs["lost"] + bs["cashed"]:
                place, total = ctx.bet_profit_rank(gid, uid)
                sign = "+" if bs["profit"] >= 0 else ""
                tiles = "".join(f'<div class="ftile"><span class="d ftile-v">{esc(v)}</span><span class="num-l">{label}</span></div>' for label, v in (
                    ("PROFIT", f"{sign}{bs['profit']:,}"), ("HIT RATE", f"{bs['hit']}%"),
                    ("RETURN", f"{'+' if (bs['roi'] or 0) >= 0 else ''}{bs['roi']}%" if bs["roi"] is not None else "–"),
                    ("BEST RUN", bs["best_run"])))

                def rate(won, lost):
                    return f"{won}-{lost} · {round(100 * won / (won + lost))}%" if won + lost else "–"
                rows_html = "".join(f'<li><span class="prank-who">{esc(k)}</span><span class="prank-v tab">{rate(*v)}</span></li>'
                                    for k, v in list(bs["kinds"].items()) + list(bs["picks"].items()))
                high = []
                if bs["biggest"]:
                    b = bs["biggest"]
                    high.append(f'Biggest win <b class="gold">+{b["payout"] - b["stake"]:,}</b> ({b["stake"]:,} @ {b["odds"]:.2f})')
                if bs["best_odds"]:
                    high.append(f'Best odds landed <b class="gold">{bs["best_odds"]["odds"]:.2f}</b>')
                if bs["favourite"]:
                    f = bs["favourite"]
                    high.append(f'Backs most <b>{esc(clean_name(ctx, f["name"]))}</b> ({f["times"]}×, {f["won"]}-{f["lost"]})')
                cashed_text = f", cashed out {bs['cashed']}" if bs["cashed"] else ""
                blocks.append(("BET STATS", "betstats",
                               f'<div class="ftiles ftiles-4">{tiles}</div>'
                               f'<p class="muted" style="margin-top:12px">{" · ".join(high)}</p>'
                               f'<ol class="prank prank-plain">{rows_html}</ol>'
                               f'<p class="note">{f"#{place} of {total} for profit in PFO. " if place else ""}'
                               f'Won {bs["won"]}, lost {bs["lost"]}{cashed_text}. '
                               'Settled bets only, refunds don\'t count. Same as /betstats in Discord.</p>'))
        except Exception:
            traceback.print_exc()

    # ---------- fantasy ----------
    team = ctx.fantasy_team(gid, uid)
    players, _, per_pick = ctx.fantasy_scores(gid)
    order = sorted(players.items(), key=lambda item: -item[1])
    place = next((i for i, (u, _) in enumerate(order, start=1) if u == uid), None)
    picks = "".join(f'<span class="tpick{" cap" if p["captain"] else ""}">{"★ " if p["captain"] else ""}{esc(fighter_name(ctx, p["fighter_id"]))}'
                    f'<b>{per_pick.get((uid, p["fighter_id"]), 0)}</b></span>' for p in team)
    fantasy_html = (f'<div class="mefan"><span class="d fan-big">{players.get(uid, 0)} PTS</span>'
                    f'<span class="muted">{("#" + str(place) + " of " + str(len(order))) if place else "Not ranked yet"}</span></div>'
                    f'<div class="tpicks">{picks or "<span class=muted>No team yet.</span>"}</div>'
                    f'<div class="btns"><a class="btn btn-gold btn-sm" href="/fantasy#myteam">{"MANAGE MY TEAM" if team else "MAKE A TEAM"}</a></div>')
    blocks.append(("MY FANTASY", "fantasy", fantasy_html))

    # ---------- tasks ----------
    try:
        week, end, rows, bonus_ready, bonus_claimed = ctx.task_status(gid, uid)
        items = ""
        claimable = 0
        for key, emoji, text, done, needed, reward, finished, claimed in rows:
            pct = round(100 * done / needed) if needed else 0
            claimable += reward if finished and not claimed else 0
            items += (f'<li class="task{" task-done" if finished else ""}"><span class="task-e" aria-hidden="true">{emoji}</span>'
                      f'<span class="task-t">{esc(text)}<span class="task-bar"><span style="width:{pct}%"></span></span></span>'
                      f'<span class="task-v tab">{"✓ " if claimed else ""}{done}/{needed} · {reward:,}</span></li>')
        streak = ctx.task_streak(gid, uid, week) if getattr(ctx, "task_streak", None) else 0
        if bonus_ready and not bonus_claimed:
            claimable += ctx.WEEKLY_BONUS
            if streak + 1 >= ctx.TASK_STREAK_WEEKS:
                claimable += ctx.TASK_STREAK_BONUS
        starter_html = ""
        for key, emoji, text, done, needed, reward, finished, _ in (ctx.starter_status(gid, uid) if getattr(ctx, "starter_status", None) else []):
            claimable += reward if finished else 0
            starter_html += (f'<li class="task{" task-done" if finished else ""}"><span class="task-e" aria-hidden="true">{emoji}</span>'
                             f'<span class="task-t">{esc(text)}<span class="task-bar"><span style="width:{100 if finished else 0}%"></span></span></span>'
                             f'<span class="task-v tab">{"ready" if finished else "not yet"} · {reward:,}</span></li>')
        button = (f'<button class="btn btn-gold btn-sm" type="button" data-claim-tasks>CLAIM {claimable:,} COINS</button>' if claimable
                  else '<span class="muted">Nothing to claim right now.</span>')
        streak_note = (f" You're on a {streak}-week streak: get this week's bonus for +{ctx.TASK_STREAK_BONUS:,} more." if streak + 1 >= ctx.TASK_STREAK_WEEKS
                       else f" Get the bonus {ctx.TASK_STREAK_WEEKS} weeks running for +{ctx.TASK_STREAK_BONUS:,} each week.")
        blocks.append(("WEEKLY TASKS", "tasks", f'<p class="muted">New tasks {when_html(end)}. Finish {ctx.WEEKLY_BONUS_TASKS} for a '
                                                 f'{ctx.WEEKLY_BONUS:,} coin bonus.{streak_note}</p><ul class="tasks">{items}</ul>'
                                                 + (f'<p class="num-l" style="margin-top:14px">STARTER TASKS (ONCE ONLY)</p><ul class="tasks">{starter_html}</ul>' if starter_html else "")
                                                 + f'<div class="btns">{button}</div>'))
    except Exception:
        traceback.print_exc()

    # ---------- following ----------
    followed = ctx.followed_ids(gid, uid)
    follow_rows = ""
    for fid in followed:
        fstats = stats_for(ctx, fid)
        upcoming = find_next_fight(ctx, fid)
        sub = (f'Next: vs {esc(fighter_name(ctx, upcoming[0]))} · {esc(upcoming[1])}' if upcoming else
               esc(ctx.stats_record(fstats)) if fstats else "")
        follow_rows += (f'<li class="folrow">{avatar_html(ctx, fid, fighter_name(ctx, fid), "av")}'
                        f'<span class="folrow-t"><span class="d">{name_link(ctx, fid, fighter_name(ctx, fid))}</span><span class="muted">{sub}</span></span>'
                        f'<button class="btn btn-ghost btn-sm" type="button" data-follow="{fid}" data-on="1">FOLLOWING</button></li>')
    blocks.append(("FOLLOWING", "following", f'<ul class="follist">{follow_rows}</ul>' if follow_rows else
                   '<p class="muted">Follow fighters with the ★ Follow button on their page. Their next fights show up here and on your home page.</p>'))

    # ---------- achievements ----------
    try:
        achievements = ctx.check_achievements(gid, uid)
        got = sum(1 for _, a in achievements if a["unlocked_at"])
        cards = ""
        for key, a in sorted(achievements, key=lambda item: (not item[1]["unlocked_at"], item[0])):
            pct = round(100 * a["have"] / a["need"]) if a["need"] else 0
            cards += (f'<div class="ach ach-{a["rarity"]}{" ach-on" if a["unlocked_at"] else ""}"><span class="ach-e" aria-hidden="true">{a["emoji"]}</span>'
                      f'<span class="ach-t"><b>{esc(a["name"])}</b><span>{esc(a["how"])}</span>'
                      f'<span class="ach-bar"><span style="width:{pct}%"></span></span></span>'
                      f'<span class="ach-r">{esc(a["rarity"].upper())}<br>{ctx.ACHIEVEMENT_REWARDS[a["rarity"]]:,}</span></div>')
        blocks.append((f"MY ACHIEVEMENTS · {got}/{len(achievements)}", "achievements", f'<div class="achgrid">{cards}</div>'
                       '<p class="note">Only you can see these. Coins are paid once when you unlock one.</p>'))
    except Exception:
        traceback.print_exc()

    # ---------- items ----------
    try:
        owned = ctx.owned_items(gid, uid)
        states = {"unused": "Ready to use", "pending": "Waiting for staff", "approved": "Approved, staff are arranging it", "active": "Active"}
        item_rows = "".join(
            f'<li><span class="prank-who">{ctx.SHOP_ITEMS[r["item"]][1]} {esc(ctx.SHOP_ITEMS[r["item"]][0])}</span>'
            f'<span class="prank-v">{esc(states.get(r["status"], r["status"]))}{(" until " + when_html(r["expires_at"])) if r["expires_at"] else ""}</span></li>'
            for r in owned if r["item"] in ctx.SHOP_ITEMS)
        cos = ctx.get_cosmetics(gid, uid) or {}
        extras = "".join(f'<span class="tpick">{esc(label)}: {esc(cos[key])}</span>' for key, label in
                         (("nickname", "Nickname"), ("badge", "Badge"), ("quote", "Quote"), ("style", "Style")) if cos.get(key))
        blocks.append(("MY ITEMS", "items", (f'<ol class="prank prank-plain">{item_rows}</ol>' if item_rows else '<p class="muted">No items right now.</p>')
                       + (f'<div class="tpicks">{extras}</div>' if extras else "") + '<p class="note"><a href="/shop">Visit the shop →</a></p>'))
    except Exception:
        traceback.print_exc()

    # ---------- awards ----------
    try:
        awards = ctx.awards_for_member(gid, uid)
        titles = {"fotn": "Fight of the Night", "potn": "Performance of the Night", "kotn": "Knockout of the Night"}
        if awards:
            rows_html = "".join(f'<li><span class="prank-who"><span class="award award-{a["award"]}">{a["award"].upper()}</span> {esc(titles.get(a["award"], a["award"]))}</span>'
                                f'<span class="prank-v">{esc(a["event"])}</span></li>' for a in awards[:20])
            blocks.append((f"MY AWARDS · {len(awards)}", "awards", f'<ol class="prank prank-plain">{rows_html}</ol>'))
    except Exception:
        traceback.print_exc()

    # ---------- coin history ----------
    ledger = ctx.q("SELECT * FROM coin_ledger WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT 25", (gid, uid))
    if ledger:
        rows_html = "".join(f'<li><span class="prank-who">{esc(coin_reason(r["reason"]))}<span class="muted">{esc(short_day(r["created_at"]))}</span></span>'
                            f'<span class="prank-v tab {"green" if r["amount"] > 0 else "red"}">{"+" if r["amount"] > 0 else ""}{r["amount"]:,}</span></li>'
                            for r in ledger)
        blocks.append(("COIN HISTORY", "coins", f'<ol class="prank prank-plain">{rows_html}</ol>'
                                                '<p class="note">PFO Coins are points only. They can\'t be bought, sold or swapped for money.</p>'))

    nav = "".join(f'<a href="#me-{key}">{esc(title.split(" · ")[0])}</a>' for title, key, _ in blocks)
    sections = "".join(f'<section class="block bt mesec" id="me-{key}"><div class="head-t rv"><h2 class="d h-m">{esc(title)}</h2></div>{html_}</section>'
                       for title, key, html_ in blocks)
    body = hero + f'<div class="wrap"><nav class="mejump" aria-label="Sections">{nav}</nav>{sections}</div>'
    return page(ctx, "My PFO", "me", body, "Your PFO: fights, bets, fantasy, tasks and more.")


def render_home_panel(ctx, viewer):
    """The personal strip at the top of the home page (loaded by JS)."""
    uid = viewer.user_id
    cards = []
    nxt = find_next_fight(ctx, uid)
    if nxt:
        opp, event, when, _ = nxt
        cards.append(f'<a class="hp-card hp-fight" href="/compare?a={uid}&b={opp}"><span class="eyebrow-grey">YOUR NEXT FIGHT</span>'
                     f'<span class="d hp-big">VS {esc(fighter_name(ctx, opp).upper())}</span>'
                     f'<span class="muted">{esc(event)}{" · " + when_html(when) if when else ""}</span>'
                     + (f'<span class="hp-cd" data-countdown-small="{when}"></span>' if when else "") + '</a>')
    for signup_type in ("live_card", "fight_night"):
        session = ctx.get_active_session(ctx.gid, signup_type)
        if session and viewer.levels["fighter"] and not ctx.is_signed_up(session["id"], uid):
            try:
                title = ctx.signup_title(signup_type, session["id"])
            except Exception:
                title = ctx.SIGNUP_INFO[signup_type]["title"]
            cards.append(f'<div class="hp-card"><span class="eyebrow-grey">SIGN-UPS OPEN</span><span class="d hp-big">{esc(title.upper())}</span>'
                         f'<button class="btn btn-gold btn-sm" type="button" data-signup="{session["id"]}" data-signed="0">SIGN UP</button></div>')
    open_bets = ctx.q("SELECT COUNT(*) AS n, COALESCE(SUM(stake), 0) AS s FROM bets WHERE guild_id = ? AND user_id = ? AND status = 'open'",
                      (ctx.gid, uid))[0]
    if open_bets["n"]:
        cards.append(f'<a class="hp-card" href="/me#me-bets"><span class="eyebrow-grey">OPEN BETS</span>'
                     f'<span class="d hp-big">{open_bets["n"]} BET{"S" if open_bets["n"] != 1 else ""}</span>'
                     f'<span class="muted">{open_bets["s"]:,} coins riding · cash out from My PFO</span></a>')
    players, _, _ = ctx.fantasy_scores(ctx.gid)
    if ctx.fantasy_team(ctx.gid, uid):
        current = ctx.open_fantasy_round(ctx.gid)
        cards.append(f'<a class="hp-card" href="/fantasy#myteam"><span class="eyebrow-grey">MY FANTASY</span>'
                     f'<span class="d hp-big">{players.get(uid, 0)} PTS</span>'
                     f'<span class="muted">{("Swaps open · locks " + when_html(current["locks_at"])) if current else "Team set"}</span></a>')
    for fid in ctx.followed_ids(ctx.gid, uid)[:30]:
        upcoming = find_next_fight(ctx, fid)
        if upcoming:
            cards.append(f'<a class="hp-card" href="{fighter_url(fid)}"><span class="eyebrow-grey">★ FOLLOWING</span>'
                         f'<span class="d hp-big">{esc(fighter_name(ctx, fid).upper())}</span>'
                         f'<span class="muted">vs {esc(fighter_name(ctx, upcoming[0]))} · {esc(upcoming[1])}</span></a>')
        if len(cards) >= 6:
            break
    wallet = ctx.get_wallet(ctx.gid, uid)
    name = clean_name(ctx, viewer.member.display_name)
    return (f'<section class="homepanel" aria-label="Your PFO"><div class="wrap"><div class="hp-head">'
            f'<span class="d hp-hi">WELCOME BACK, {esc(name.upper())}</span><span class="muted">{wallet["balance"]:,} coins · <a href="/me">My PFO →</a></span></div>'
            f'<div class="hp-grid">{"".join(cards) or "<p class=muted>Nothing on for you right now. Follow fighters or make a fantasy team to see more here.</p>"}</div></div></section>')


# ------------------------------------------------------------
# Shop
# ------------------------------------------------------------

SHOP_TARGET = {"fight_request", "ranking_challenge", "callout", "bounty", "gift"}
SHOP_TEXT = {"quote": ("Your quote", 150), "nickname": ("Nickname", 32), "callout": ("Your message", 300),
             "early_swap": ("Who do you want to swap to?", 100), "interview": ("Anything staff should know? (optional)", 200)}
SHOP_AMOUNT = {"bounty", "gift"}
SHOP_ORDER = ["Fighter", "Profile & looks", "Media", "Betting", "Games"]
SHOP_RARITY = {"badge": "rare", "name_colour": "epic", "profile_style": "legendary", "spotlight": "legendary", "nickname": "epic",
               "quote": "rare", "bet_limit": "epic", "parlay_boost": "rare", "bet_insurance": "rare"}


def render_shop(ctx):
    groups = {}
    for key, (name, emoji, _, category, needs_staff, about) in ctx.SHOP_ITEMS.items():
        price, enabled = ctx.item_setting(ctx.gid, key)
        if not enabled:
            continue
        groups.setdefault(category, []).append((key, name, emoji, price, needs_staff, about))
    choices = {"badge": {b: b for b in ctx.BADGES}, "profile_style": dict(ctx.PROFILE_STYLES), "name_colour": {c: c for c in ctx.NAME_COLOURS}}
    sections = ""
    for category in SHOP_ORDER + [c for c in groups if c not in SHOP_ORDER]:
        items = groups.get(category)
        if not items:
            continue
        cards = ""
        for i, (key, name, emoji, price, needs_staff, about) in enumerate(items):
            rarity = SHOP_RARITY.get(key)
            config = {"key": key, "name": name, "price": price, "target": key in SHOP_TARGET, "amount": key in SHOP_AMOUNT,
                      "text": SHOP_TEXT.get(key), "choices": choices.get(key), "staff": bool(needs_staff),
                      "min": price if key == "bounty" else 50 if key == "gift" else None, "fee": ctx.GIFT_FEE if key == "gift" else None}
            price_text = "Any amount" if key == "gift" else (f"From {price:,}" if key == "bounty" else f"{price:,}")
            cards += (f'<article class="shopitem {stagger(i, 3)}{" r-" + rarity if rarity else ""}">'
                      f'<span class="shop-e" aria-hidden="true">{emoji}</span>'
                      f'<div class="shop-t"><h3 class="d">{esc(name.upper())}</h3><p>{esc(about)}</p>'
                      f'<div class="shop-tags">{f"<span class=rar>{rarity.upper()}</span>" if rarity else ""}'
                      f'{"<span class=staffchk>STAFF CHECK</span>" if needs_staff else ""}</div></div>'
                      f'<div class="shop-buy"><span class="d shop-p">{price_text}</span>'
                      f'<button class="btn btn-gold btn-sm" type="button" data-buy=\'{esc(json.dumps(config))}\'>BUY</button></div></article>')
        sections += (f'<section class="block bt" aria-label="{esc(category)}"><div class="head-t rv"><h2 class="d h-m">{esc(category.upper())}</h2></div>'
                     f'<div class="shopgrid">{cards}</div></section>')
    hero = page_hero("SPEND YOUR PFO COINS", "SHOP", "SHOP",
                     play_tabs("shop") + '<div class="evchips l-up"><div class="evchip gold"><span class="evchip-l">YOUR COINS</span>'
                     '<span class="d evchip-v" data-my-coins>–</span></div></div>')
    note = ('<p class="note">Items marked STAFF CHECK go to staff first. If they say no, you get your coins back. '
            'Coins are points only and have no money value. You can also use /shop in Discord.</p>')
    body = hero + f'<div class="wrap">{sections}{note}</div>' + SHOP_DIALOG
    return page(ctx, "Shop", "shop", body, "The PFO shop: fighter perks, profile items, betting boosts and games.")


SHOP_DIALOG = """
<dialog class="pfo-dialog" id="buydialog" aria-labelledby="buyname">
  <form method="dialog" class="dlg-in" id="buyform">
    <h2 class="d h-s" id="buyname">BUY</h2>
    <p class="muted" id="buyabout"></p>
    <div class="dlg-field" data-f="target"><label for="buytarget">Who?</label>
      <input id="buytarget" type="search" autocomplete="off" placeholder="Type a name"><div class="dlg-results" id="buyresults"></div></div>
    <div class="dlg-field" data-f="amount"><label for="buyamount">How many coins?</label><input id="buyamount" type="number" inputmode="numeric" min="1" step="1"><span class="muted" id="buyfee"></span></div>
    <div class="dlg-field" data-f="choices"><label for="buychoice">Choose one</label><select id="buychoice"></select></div>
    <div class="dlg-field" data-f="text"><label for="buytext" id="buytextl">Text</label><textarea id="buytext" rows="3"></textarea></div>
    <p class="dlg-total" id="buytotal"></p>
    <div class="btns"><button class="btn btn-gold" type="button" id="buygo">BUY</button><button class="btn btn-ghost" value="cancel">CANCEL</button></div>
  </form>
</dialog>"""


# ------------------------------------------------------------
# Social
# ------------------------------------------------------------

def render_social(ctx):
    trending = ""
    try:
        rows = ctx.q("SELECT event, COUNT(*) AS n FROM social_posts WHERE guild_id = ? AND deleted = 0 AND hidden = 0 AND event IS NOT NULL "
                     "AND created_at > ? GROUP BY event ORDER BY n DESC LIMIT 6", (ctx.gid, int(time.time()) - 14 * 86400))
        trending = "".join(f'<span class="tpick">#{esc(r["event"])}<b>{r["n"]}</b></span>' for r in rows)
    except Exception:
        pass
    events = ctx.recent_events(ctx.gid)[:6]
    options = "".join(f'<option value="{esc(e)}">{esc(e)}</option>' for e in events)
    body = page_hero("POSTS FROM THE PFO COMMUNITY", "SOCIAL", "POST") + f"""
<div class="wrap soc-wrap">
  <section class="block soc-main" aria-label="Feed">
    <div class="soc-compose" id="compose" hidden>
      <textarea id="soctext" maxlength="500" rows="3" placeholder="What's on your mind? Predictions, callouts, reactions…"></textarea>
      <div class="soc-tools">
        <label class="btn btn-ghost btn-sm soc-photo">PHOTO<input type="file" id="socimg" accept="image/png,image/jpeg,image/gif,image/webp" hidden></label>
        <select id="socevent" aria-label="Tag a card"><option value="">Tag a card (optional)</option>{options}</select>
        <span class="muted" id="soccount">0/500</span>
        <button class="btn btn-gold btn-sm" type="button" id="socpost">POST</button>
      </div>
      <div id="socpreview"></div>
      <p class="note">Posts are shared in #social-media on the Discord too. Be decent: reported posts are hidden until staff check them.</p>
    </div>
    <div class="soc-login" id="soclogin" hidden>{login_box("Log in with Discord to post, like and comment.", "/social")}</div>
    <div class="ptabs soc-sort" role="tablist" aria-label="Sort posts">
      <button role="tab" aria-selected="true" data-sort="new">NEW</button><button role="tab" aria-selected="false" data-sort="top">TOP TODAY</button>
      <button role="tab" aria-selected="false" data-sort="fighters">FIGHTERS</button><button role="tab" aria-selected="false" data-sort="following">FOLLOWING</button>
    </div>
    <div id="feed" class="feed" aria-live="polite"><p class="muted">Loading posts…</p></div>
    <button class="btn btn-ghost" type="button" id="feedmore" hidden>LOAD MORE</button>
  </section>
  <aside class="soc-side">
    <div class="pbox"><h3 class="pbox-h">TRENDING CARDS</h3>{f'<div class="tpicks">{trending}</div>' if trending else '<p class="empty">Nothing tagged yet.</p>'}</div>
    <div class="pbox"><h3 class="pbox-h">ON DISCORD</h3><p class="muted">Every post shows up in #social-media with Like, Dislike and Comment buttons. Votes and comments from either place count in both.</p></div>
  </aside>
</div>"""
    return page(ctx, "Social", "social", body, "PFO Social: posts, reactions and callouts from the PFO community.")


# ------------------------------------------------------------
# Records & milestones
# ------------------------------------------------------------

def render_records(ctx):
    fights = ctx.q("SELECT * FROM fights WHERE guild_id = ? ORDER BY COALESCE(fight_date, substr(created_at,1,10)), id", (ctx.gid,))
    per = {}
    for fight in fights:
        for side, other in (("winner", "loser"), ("loser", "winner")):
            uid = fight[f"{side}_id"]
            if not uid:
                continue
            per.setdefault(uid, []).append(fight)

    def best(score, minimum=1):
        top = []
        for uid, items in per.items():
            value = score(uid, items)
            if value is not None and value >= minimum:
                top.append((value, uid))
        top.sort(key=lambda item: (-item[0], fighter_name(ctx, item[1]).lower()))
        return top[:5]

    def wins(uid, items, method=None):
        return sum(1 for f in items if f["winner_id"] == uid and f["method"] not in ("DRAW", "NC") and (method is None or f["method"] == method))

    def longest_streak(uid, items):
        run = top = 0
        for f in items:
            if f["method"] == "NC":
                continue
            if f["winner_id"] == uid and f["method"] != "DRAW":
                run += 1
                top = max(top, run)
            else:
                run = 0
        return top

    outcomes = ctx.all_title_outcomes(ctx.gid)
    defences = {}
    for f in fights:
        if outcomes.get(f["id"]) == "defence":
            defences[f["winner_id"]] = defences.get(f["winner_id"], 0) + 1

    records = [
        ("MOST WINS", best(lambda u, i: wins(u, i)), "wins"),
        ("LONGEST WIN STREAK", best(longest_streak, 2), "in a row"),
        ("MOST KO / TKO WINS", best(lambda u, i: wins(u, i, "KO/TKO")), "KOs"),
        ("MOST SUBMISSIONS", best(lambda u, i: wins(u, i, "SUB")), "subs"),
        ("MOST TITLE DEFENCES", sorted(((n, u) for u, n in defences.items()), key=lambda x: -x[0])[:5], "defences"),
        ("MOST ROUND 1 FINISHES", best(lambda u, i: sum(1 for f in i if f["winner_id"] == u and f["method"] in ("KO/TKO", "SUB") and f["round"] == 1)), "R1"),
        ("MOST FIGHTS", best(lambda u, i: sum(1 for f in i if f["method"] != "NC")), "fights"),
        ("MOST TITLE FIGHT WINS", best(lambda u, i: sum(1 for f in i if f["winner_id"] == u and f["title_fight"] and f["method"] not in ("DRAW", "NC"))), "title wins"),
    ]
    try:
        award_counts = {}
        for row in ctx.q("SELECT fight_awards.award, fight_awards.user_id, fights.winner_id FROM fight_awards JOIN fights ON fights.id = fight_awards.fight_id "
                         "WHERE fight_awards.guild_id = ?", (ctx.gid,)):
            uid = row["user_id"] or row["winner_id"]
            if uid:
                award_counts[uid] = award_counts.get(uid, 0) + 1
        records.append(("MOST AWARDS", sorted(((n, u) for u, n in award_counts.items()), key=lambda x: -x[0])[:5], "awards"))
    except Exception:
        pass

    cards = ""
    for i, (title, top, unit) in enumerate(records):
        if not top:
            continue
        value, holder = top[0]
        rest = "".join(f'<li><span class="prank-who">{name_link(ctx, u, fighter_name(ctx, u))}</span><span class="prank-v tab">{v}</span></li>' for v, u in top[1:])
        cards += (f'<article class="recard {stagger(i, 3)}"><span class="eyebrow-grey">{title}</span>'
                  f'<div class="recard-top">{avatar_html(ctx, holder, fighter_name(ctx, holder), "av av-md")}'
                  f'<span><span class="d recard-n">{name_link(ctx, holder, fighter_name(ctx, holder))}</span>'
                  f'<span class="d recard-v gold">{value} <small>{esc(unit.upper())}</small></span></span></div>'
                  f'{f"<ol class=prank-mini>{rest}</ol>" if rest else ""}</article>')

    # Bets and odds records
    money = ""
    try:
        upset = ctx.q("SELECT upset_alerts.odds, fights.* FROM upset_alerts JOIN fights ON fights.id = upset_alerts.fight_id "
                      "WHERE upset_alerts.guild_id = ? ORDER BY upset_alerts.odds DESC LIMIT 1", (ctx.gid,))
        if upset:
            u = upset[0]
            money += (f'<article class="recard"><span class="eyebrow-grey">BIGGEST UPSET</span><span class="d recard-v red">{u["odds"]:.2f}</span>'
                      f'<span>{name_link(ctx, u["winner_id"], fighter_name(ctx, u["winner_id"], u["winner_name"]))} def. '
                      f'{esc(fighter_name(ctx, u["loser_id"], u["loser_name"]))}</span><span class="muted">{esc(u["event"] or "")}</span></article>')
        bet = ctx.q("SELECT * FROM bets WHERE guild_id = ? AND status = 'won' ORDER BY payout - stake DESC LIMIT 1", (ctx.gid,))
        if bet:
            b = bet[0]
            money += (f'<article class="recard"><span class="eyebrow-grey">BIGGEST BET WIN</span><span class="d recard-v gold">+{b["payout"] - b["stake"]:,}</span>'
                      f'<span>{esc(player_name(ctx, b["user_id"]))}</span><span class="muted">{b["stake"]:,} at {b["odds"]:.2f}</span></article>')
        odds = ctx.q("SELECT * FROM bets WHERE guild_id = ? AND status = 'won' ORDER BY odds DESC LIMIT 1", (ctx.gid,))
        if odds:
            b = odds[0]
            money += (f'<article class="recard"><span class="eyebrow-grey">LONGEST ODDS WON</span><span class="d recard-v gold">{b["odds"]:.2f}</span>'
                      f'<span>{esc(player_name(ctx, b["user_id"]))}</span><span class="muted">{b["stake"]:,} → {b["payout"]:,}</span></article>')
    except Exception:
        traceback.print_exc()

    total = len(fights)
    finishes = sum(1 for f in fights if f["method"] in ("KO/TKO", "SUB"))
    strip = chip("FIGHTS LOGGED", f"{total:,}") + chip("FINISHES", f"{finishes:,}", "gold") + \
        chip("FINISH RATE", f"{round(100 * finishes / total) if total else 0}%") + chip("FIGHTERS", f"{len(per):,}")

    mile_rows = ""
    try:
        for m in ctx.q("SELECT * FROM milestones WHERE guild_id = ? ORDER BY at DESC LIMIT 40", (ctx.gid,)):
            text = re.sub(r"\*\*", "", m["text"])
            text = re.sub(r"<@!?(\d+)>", lambda x: player_name(ctx, int(x.group(1))), text)
            day = datetime.fromtimestamp(m["at"], timezone.utc)
            mile_rows += (f'<li class="mile {stagger(0)}"><span class="mile-e" aria-hidden="true">{esc(m["emoji"])}</span>'
                          f'<span class="mile-t">{esc(text)}</span><span class="muted">{day.day} {day:%b %Y}</span></li>')
    except Exception:
        pass

    body = page_hero("THE BEST EVER IN THE PFO", "RECORDS", "REC", f'<div class="evchips l-up">{strip}</div>') + f"""
<div class="wrap">
  <section class="block" aria-labelledby="rec-h"><div class="head-t rv"><span class="eyebrow">ALL TIME · LOGGED FIGHTS</span><h2 id="rec-h" class="d h-m">PFO RECORDS</h2></div>
    <div class="regrid">{cards or '<p class="empty">No fights logged yet.</p>'}</div></section>
  {f'<section class="block bt"><div class="head-t rv"><h2 class="d h-m">BETTING RECORDS</h2></div><div class="regrid">{money}</div></section>' if money else ''}
  <section class="block bt" aria-labelledby="mile-h"><div class="head-t rv"><span class="eyebrow">AS THEY HAPPEN</span><h2 id="mile-h" class="d h-m">MILESTONES</h2></div>
    {f'<ol class="miles">{mile_rows}</ol>' if mile_rows else '<p class="empty">Milestones show here as they happen: 10th wins, win streaks, title defence records and every 100th fight.</p>'}</section>
</div>"""
    return page(ctx, "Records", "records", body, "PFO all-time records and milestones.")


# ------------------------------------------------------------
# Awards
# ------------------------------------------------------------

AWARD_TITLES = {"fotn": "Fight of the Night", "potn": "Performance of the Night", "kotn": "Knockout of the Night"}


def render_awards(ctx):
    rows = ctx.q("""
        SELECT fight_awards.*, fights.winner_id, fights.loser_id, fights.winner_name, fights.loser_name, fights.method, fights.round,
               fights.division, fights.fight_date, fights.created_at AS fought_at
        FROM fight_awards JOIN fights ON fights.id = fight_awards.fight_id
        WHERE fight_awards.guild_id = ? ORDER BY fight_awards.id DESC""", (ctx.gid,))
    counts = {}
    by_kind = {k: {} for k in AWARD_TITLES}
    for r in rows:
        uids = [r["user_id"]] if r["user_id"] else ([r["winner_id"], r["loser_id"]] if r["award"] == "fotn" else [r["winner_id"]])
        for uid in uids:
            if uid:
                counts[uid] = counts.get(uid, 0) + 1
                if r["award"] in by_kind:
                    by_kind[r["award"]][uid] = by_kind[r["award"]].get(uid, 0) + 1

    def top(table):
        items = sorted(table.items(), key=lambda x: -x[1])[:8]
        return '<ol class="prank">' + "".join(f'<li><span class="prank-n">{i}</span><span class="prank-who">{name_link(ctx, u, fighter_name(ctx, u))}</span>'
                                              f'<span class="prank-v tab">{n}</span></li>' for i, (u, n) in enumerate(items, start=1)) + '</ol>' if items else ""

    boards = "".join(f'<div class="pbox rv2"><h3 class="pbox-h">{t}</h3>{top(table) or "<p class=empty>None yet.</p>"}</div>' for t, table in (
        ("MOST AWARDS", counts), ("MOST FOTN", by_kind["fotn"]), ("MOST POTN", by_kind["potn"]), ("MOST KOTN", by_kind["kotn"])))

    items = ""
    events = []
    for i, r in enumerate(rows):
        if r["event"] not in events:
            events.append(r["event"])
        who = (f'{name_link(ctx, r["winner_id"], fighter_name(ctx, r["winner_id"], r["winner_name"]))} <span class="def">VS</span> '
               f'{name_link(ctx, r["loser_id"], fighter_name(ctx, r["loser_id"], r["loser_name"]))}') if r["award"] == "fotn" else \
            name_link(ctx, r["user_id"] or r["winner_id"], fighter_name(ctx, r["user_id"] or r["winner_id"],
                      r["winner_name"] if not r["user_id"] or r["user_id"] == r["winner_id"] else r["loser_name"]))
        items += (f'<div class="row res awrow" data-kind="{esc(r["award"])}" data-event="{esc(r["event"])}">'
                  f'<span class="award award-{esc(r["award"])}">{esc(r["award"].upper())}</span>'
                  f'<span class="d res-who">{who}</span><span class="res-m {method_class(r["method"])}">{esc(method_short(ctx, r))}</span>'
                  f'<span class="res-w"><a href="{event_url(r["event"])}">{esc(r["event"].upper())}</a></span></div>')
    chips = '<button type="button" class="fchip" data-akind="" aria-pressed="true">ALL</button>' + "".join(
        f'<button type="button" class="fchip" data-akind="{k}" aria-pressed="false">{k.upper()}</button>' for k in AWARD_TITLES)
    event_select = '<select id="aevent" aria-label="Pick a card"><option value="">Every card</option>' + "".join(
        f'<option value="{esc(e)}">{esc(e)}</option>' for e in events) + '</select>'
    body = page_hero("FIGHT, PERFORMANCE AND KNOCKOUT OF THE NIGHT", "AWARDS", "AWARD",
                     f'<div class="filters l-up"><div class="fchips" role="group" aria-label="Award">{chips}</div>{event_select}</div>') + f"""
<div class="wrap">
  <section class="block" aria-label="Awards"><div class="list" id="alist">{items or '<p class="empty">No awards logged yet. Staff add them with /awards.</p>'}</div>
  <p class="empty" id="anone" hidden>No awards match.</p></section>
  <section class="block bt"><div class="head-t rv"><h2 class="d h-m">AWARD BOARDS</h2></div><div class="pgrid">{boards}</div></section>
</div>"""
    return page(ctx, "Awards", "awards", body, "Every PFO Fight, Performance and Knockout of the Night.")


# ------------------------------------------------------------
# Results with filters
# ------------------------------------------------------------

RESULT_FILTERS = ("d", "m", "t", "a", "u", "r1", "q", "p")


def results_filter_form(ctx, f):
    def opt(value, label, current):
        return f'<option value="{esc(value)}"{" selected" if current == value else ""}>{esc(label)}</option>'
    divisions = opt("", "All divisions", f.get("d", "")) + "".join(opt(w, w, f.get("d", "")) for w in ctx.DIVISION_NAMES)
    methods = "".join(opt(v, l, f.get("m", "")) for v, l in (("", "Any method"), ("KO/TKO", "KO / TKO"), ("SUB", "Submission"),
                                                              ("DEC", "Decision"), ("DRAW", "Draw"), ("NC", "No contest")))
    awards = "".join(opt(v, l, f.get("a", "")) for v, l in (("", "Any awards"), ("any", "Won an award"), ("fotn", "FOTN"), ("potn", "POTN"), ("kotn", "KOTN")))
    periods = "".join(opt(v, l, f.get("p", "")) for v, l in (("", "All time"), ("30", "Last 30 days"), ("90", "Last 3 months"), ("365", "Last year")))

    def box(key, label):
        return f'<label class="fbox"><input type="checkbox" name="{key}" value="1"{" checked" if f.get(key) else ""}> {label}</label>'
    return f"""
    <form class="rfilters l-up" method="get" action="/results">
      <input type="search" name="q" value="{esc(f.get('q', ''))}" placeholder="Fighter name" aria-label="Fighter name" maxlength="40">
      <select name="d" aria-label="Division">{divisions}</select>
      <select name="m" aria-label="Method">{methods}</select>
      <select name="a" aria-label="Awards">{awards}</select>
      <select name="p" aria-label="When">{periods}</select>
      {box("t", "Title fights")}{box("u", "Upsets")}{box("r1", "Round 1")}
      <button class="btn btn-gold btn-sm" type="submit">FILTER</button>
      {'<a class="btn btn-ghost btn-sm" href="/results">CLEAR</a>' if any(f.values()) else ''}
    </form>"""


def render_filtered_results(ctx, f, page_no=1):
    where = ["guild_id = ?"]
    params = [ctx.gid]
    if f.get("d") in ctx.DIVISION_NAMES:
        where.append("division = ?")
        params.append(f["d"])
    if f.get("m") in ("KO/TKO", "SUB", "DEC", "DRAW", "NC", "DQ"):
        where.append("method = ?")
        params.append(f["m"])
    if f.get("t"):
        where.append("title_fight = 1")
    if f.get("r1"):
        where.append("round = 1")
    if f.get("u"):
        where.append("id IN (SELECT fight_id FROM upset_alerts WHERE guild_id = ?)")
        params.append(ctx.gid)
    if f.get("a") in AWARD_TITLES:
        where.append("id IN (SELECT fight_id FROM fight_awards WHERE guild_id = ? AND award = ?)")
        params += [ctx.gid, f["a"]]
    elif f.get("a") == "any":
        where.append("id IN (SELECT fight_id FROM fight_awards WHERE guild_id = ?)")
        params.append(ctx.gid)
    if (f.get("p") or "").isdigit():
        since = (datetime.now(timezone.utc) - timedelta(days=int(f["p"]))).strftime("%Y-%m-%d")
        where.append("COALESCE(fight_date, substr(created_at,1,10)) >= ?")
        params.append(since)
    fights = ctx.q(f"SELECT * FROM fights WHERE {' AND '.join(where)} ORDER BY COALESCE(fight_date, substr(created_at,1,10)) DESC, id DESC",
                   tuple(params))
    q = (f.get("q") or "").strip().lower()
    if q:
        fights = [x for x in fights if q in fighter_name(ctx, x["winner_id"], x["winner_name"]).lower()
                  or q in fighter_name(ctx, x["loser_id"], x["loser_name"]).lower()]
    per = 40
    pages = max(1, -(-len(fights) // per))
    page_no = max(1, min(page_no, pages))
    shown = fights[(page_no - 1) * per: page_no * per]
    rows = ""
    last_event = None
    for i, fight in enumerate(shown):
        event = fight["event"] or nice_day(fight_day(fight))
        if event != last_event:
            rows += f'<h3 class="sec-h"><a href="{event_url(fight["event"])}">{esc(event.upper())}</a></h3>' if fight["event"] else f'<h3 class="sec-h">{esc(event.upper())}</h3>'
            last_event = event
        rows += result_row(ctx, fight, i)
    pager = ""
    if pages > 1:
        base = {k: v for k, v in f.items() if v}
        prev_link = f'<a class="btn btn-ghost" href="?{urlencode({**base, "page": page_no - 1})}">← NEWER</a>' if page_no > 1 else "<span></span>"
        next_link = f'<a class="btn btn-ghost" href="?{urlencode({**base, "page": page_no + 1})}">OLDER →</a>' if page_no < pages else "<span></span>"
        pager = f'<nav class="pager" aria-label="Pages">{prev_link}<span class="muted">Page {page_no} of {pages}</span>{next_link}</nav>'
    finishes = sum(1 for x in fights if x["method"] in ("KO/TKO", "SUB"))
    summary = f'<p class="muted rv">{len(fights)} fight{"s" if len(fights) != 1 else ""} · {finishes} finish{"es" if finishes != 1 else ""}</p>'
    body = page_hero("FILTERED RESULTS", "RESULTS", "DEF.", results_filter_form(ctx, f)) + \
        f'<div class="wrap"><section class="block">{summary}<div class="list">{rows or "<p class=empty>No fights match those filters.</p>"}</div></section>{pager}</div>'
    return page(ctx, "Results", "results", body, "PFO results, filtered.")


# ------------------------------------------------------------
# The week (weekly recap) /week?w=YYYY-MM-DD
# ------------------------------------------------------------

def week_bounds(ctx, monday_text=None):
    """(monday date, start unix, end unix) in UK time. Defaults to last week."""
    today = ctx.uk_now().date()
    this_monday = today - timedelta(days=today.weekday())
    monday = this_monday - timedelta(days=7)
    if monday_text:
        try:
            wanted = datetime.strptime(monday_text[:10], "%Y-%m-%d").date()
            monday = wanted - timedelta(days=wanted.weekday())
        except ValueError:
            pass
    start = int(ctx.to_utc(datetime.combine(monday, datetime.min.time())).timestamp())
    end = int(ctx.to_utc(datetime.combine(monday + timedelta(days=7), datetime.min.time())).timestamp())
    return monday, start, end, this_monday


def render_week(ctx, monday_text=None):
    monday, start, end, this_monday = week_bounds(ctx, monday_text)
    start_iso = datetime.fromtimestamp(start, timezone.utc).isoformat()
    end_iso = datetime.fromtimestamp(end, timezone.utc).isoformat()
    sunday = monday + timedelta(days=6)
    label = f"{monday.day} {monday:%b} – {sunday.day} {sunday:%b %Y}"
    current = monday >= this_monday

    fights = ctx.q("SELECT * FROM fights WHERE guild_id = ? AND created_at >= ? AND created_at < ? ORDER BY id", (ctx.gid, start_iso, end_iso))
    finishes = sum(1 for f in fights if f["method"] in ("KO/TKO", "SUB"))
    titles = sum(1 for f in fights if f["title_fight"])
    outcomes = ctx.all_title_outcomes(ctx.gid)

    # Results by event
    by_event = {}
    for f in fights:
        by_event.setdefault(f["event"] or "Other fights", []).append(f)
    results = ""
    for event, items in by_event.items():
        rows = ""
        for i, f in enumerate(items):
            rows += result_row(ctx, f, i)
            if outcomes.get(f["id"]) in ("new", "first"):
                rows += f'<div class="res-note gold">NEW {esc((f["division"] or "").upper())} CHAMPION</div>'
        head = f'<a href="{event_url(event)}">{esc(event.upper())}</a>' if event != "Other fights" else "OTHER FIGHTS"
        results += f'<h3 class="sec-h">{head}</h3><div class="list">{rows}</div>'

    # Title changes and milestones
    milestones = ""
    for m in ctx.q("SELECT * FROM milestones WHERE guild_id = ? AND at >= ? AND at < ? ORDER BY at", (ctx.gid, start, end)):
        text = re.sub(r"<@!?(\d+)>", lambda x: player_name(ctx, int(x.group(1))), m["text"].replace("**", ""))
        milestones += f'<li class="mile"><span class="mile-e" aria-hidden="true">{esc(m["emoji"])}</span><span class="mile-t">{esc(text)}</span></li>'
    for f in fights:
        if outcomes.get(f["id"]) in ("new", "first"):
            milestones += (f'<li class="mile"><span class="mile-e" aria-hidden="true">👑</span><span class="mile-t">'
                           f'{esc(fighter_name(ctx, f["winner_id"], f["winner_name"]))} is the new {esc(f["division"] or "")} champion</span></li>')

    # Awards
    awards = ""
    if fights:
        ids = [f["id"] for f in fights]
        for r in ctx.q(f"SELECT * FROM fight_awards WHERE guild_id = ? AND fight_id IN ({','.join('?' * len(ids))})", (ctx.gid, *ids)):
            fight = next(f for f in fights if f["id"] == r["fight_id"])
            who = (f'{esc(fighter_name(ctx, fight["winner_id"], fight["winner_name"]))} vs {esc(fighter_name(ctx, fight["loser_id"], fight["loser_name"]))}'
                   if r["award"] == "fotn" else esc(player_name(ctx, r["user_id"] or fight["winner_id"])))
            awards += f'<li><span class="prank-who"><span class="award award-{esc(r["award"])}">{esc(r["award"].upper())}</span> {who}</span></li>'

    # Bets of the week
    bets = ""
    for r in ctx.q("SELECT user_id, SUM(amount) AS profit FROM coin_ledger WHERE guild_id = ? AND (reason LIKE 'bet:%' OR reason LIKE 'stake:%') "
                   "AND created_at >= ? AND created_at < ? GROUP BY user_id HAVING SUM(amount) > 0 ORDER BY profit DESC LIMIT 5",
                   (ctx.gid, start_iso, end_iso)):
        bets += f'<li><span class="prank-who">{esc(player_name(ctx, r["user_id"]))}</span><span class="prank-v tab green">+{r["profit"]:,}</span></li>'

    # Fantasy
    fantasy = ""
    try:
        for a in ctx.q("SELECT * FROM fantasy_round_awards WHERE guild_id = ? AND awarded_at >= ? AND awarded_at < ?", (ctx.gid, start_iso, end_iso)):
            event = ctx.q("SELECT event FROM fantasy_rounds WHERE id = ?", (a["round_id"],))
            winners = json.loads(a["winners"] or "[]")
            fantasy += (f'<li><span class="prank-who">{" & ".join(esc(player_name(ctx, w)) for w in winners) or "–"}'
                        f'<span class="muted">{esc((event[0]["event"] if event else "") or "")}</span></span><span class="prank-v tab">{a["points"]} pts</span></li>')
    except Exception:
        pass

    # Next week
    nxt = ""
    try:
        event = next_event(ctx)
        if event:
            nxt = f'<p><span class="d h-s">{esc(event["name"].upper())}</span><br><span class="muted">{when_html(event["when"]) if event.get("when") else ""}</span></p>'
    except Exception:
        pass

    # Archive: every week with fights
    weeks = []
    for r in ctx.q("SELECT DISTINCT substr(created_at, 1, 10) AS d FROM fights WHERE guild_id = ? ORDER BY d DESC LIMIT 400", (ctx.gid,)):
        try:
            day = datetime.strptime(r["d"], "%Y-%m-%d").date()
        except ValueError:
            continue
        mon = day - timedelta(days=day.weekday())
        if mon not in weeks and mon < this_monday:
            weeks.append(mon)
    archive = "".join(f'<a class="fchip" href="/week?w={w.isoformat()}"{" aria-current=page" if w == monday else ""}>{w.day} {w:%b %y}</a>' for w in weeks[:30])

    def box(title, inner, wide=False):
        return f'<div class="pbox rv2{" pbox-wide" if wide else ""}"><h3 class="pbox-h">{title}</h3>{inner}</div>'

    strip = chip("FIGHTS", len(fights)) + chip("FINISHES", finishes, "gold") + (chip("TITLE FIGHTS", titles) if titles else "")
    body = page_hero("THE WEEK IN THE PFO" if not current else "THIS WEEK SO FAR", label.upper(), "WEEK", f'<div class="evchips l-up">{strip}</div>') + f"""
<div class="wrap">
  <section class="block" aria-label="Results"><div class="head-t rv"><h2 class="d h-m">RESULTS</h2></div>{results or '<p class="empty">No fights logged this week.</p>'}</section>
  <section class="block bt"><div class="pgrid">
    {box("MILESTONES & TITLES", f'<ol class="miles">{milestones}</ol>' if milestones else '<p class="empty">None this week.</p>')}
    {box("AWARDS", f'<ol class="prank prank-plain">{awards}</ol>' if awards else '<p class="empty">None this week.</p>')}
    {box("BETS OF THE WEEK", f'<ol class="prank prank-plain">{bets}</ol>' if bets else '<p class="empty">No winning bettors.</p>')}
    {box("FANTASY TOP SCORER", f'<ol class="prank prank-plain">{fantasy}</ol>' if fantasy else '<p class="empty">No fantasy round finished.</p>')}
    {box("NEXT UP", nxt or '<p class="empty">Nothing announced yet.</p>')}
  </div></section>
  <section class="block bt"><div class="head-t rv"><h2 class="d h-m">PAST WEEKS</h2></div><div class="fchips weekchips">{archive or '<p class="empty">No past weeks yet.</p>'}</div></section>
</div>"""
    return page(ctx, f"The week · {label}", "week", body, f"The PFO week, {label}: results, milestones, awards, bets and fantasy.")


# ------------------------------------------------------------
# Get the app (Add to Home Screen)
# ------------------------------------------------------------

def render_app(ctx):
    body = page_hero("PFO ON YOUR HOME SCREEN", "GET THE APP", "APP") + """
<div class="wrap">
  <section class="block"><div class="pgrid">
    <div class="pbox"><h3 class="pbox-h">IPHONE / IPAD</h3><ol class="steps"><li>Open this site in <b>Safari</b>.</li><li>Tap the <b>Share</b> button (the square with an arrow).</li>
      <li>Scroll down and tap <b>Add to Home Screen</b>.</li><li>Tap <b>Add</b>. PFO is now an app on your home screen.</li></ol></div>
    <div class="pbox"><h3 class="pbox-h">ANDROID</h3><ol class="steps"><li>Open this site in <b>Chrome</b>.</li><li>Tap the <b>⋮</b> menu.</li>
      <li>Tap <b>Install app</b> or <b>Add to Home screen</b>.</li><li>Confirm. Done.</li></ol>
      <button class="btn btn-gold btn-sm" type="button" id="installbtn" hidden>INSTALL NOW</button></div>
    <div class="pbox"><h3 class="pbox-h">COMPUTER</h3><ol class="steps"><li>In Chrome or Edge, look for the <b>install</b> icon at the end of the address bar.</li><li>Click it, then <b>Install</b>.</li></ol></div>
  </div><p class="note">It's the same website, it just opens full screen and loads faster. Nothing to download from an app store.</p></section>
</div>"""
    return page(ctx, "Get the app", "app", body, "Add PFO to your home screen.")


# ------------------------------------------------------------
# Staff dashboard /staff
# ------------------------------------------------------------

def discord_text_html(ctx, text):
    """Bot embed text → safe HTML (bold, small lines, mentions)."""
    out = []
    for line in (text or "").split("\n"):
        small = line.startswith("-# ")
        line = line[3:] if small else line
        line = re.sub(r"<@!?(\d+)>", lambda m: "@" + player_name(ctx, int(m.group(1))), line)
        line = re.sub(r"<a?:\w+:\d+>", "", line)
        safe = esc(line)
        safe = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", safe)
        safe = safe.replace("`", "")
        out.append(f'<span class="muted">{safe}</span>' if small else safe)
    return "<br>".join(out)


def render_staff(ctx, viewer, query):
    gid = ctx.gid
    blocks = []

    # Checklists
    lists = ""
    try:
        for notice, steps, finished in ctx.recent_checklists(ctx.guild):
            name = row_get(notice, "event") or ("Live Card" if notice["kind"] == "live_card" else "Fight Night")
            done = sum(1 for ok, _ in steps if ok)
            lis = "".join(f'<li class="{"ok" if ok else ""}"><span aria-hidden="true">{"✅" if ok else "⬜"}</span>{esc(text)}</li>' for ok, text in steps)
            lists += (f'<div class="pbox"><h3 class="pbox-h">{esc(name.upper())} · {done}/{len(steps)}</h3><ul class="checklist">{lis}</ul>'
                      f'{"<p class=green>All done.</p>" if finished else ""}</div>')
    except Exception:
        traceback.print_exc()
    blocks.append(("EVENT CHECKLIST", f'<div class="pgrid">{lists}</div>' if lists else '<p class="muted">No cards in the last 10 days.</p>'))

    # Shop requests
    reqs = ""
    for r in ctx.q("SELECT * FROM shop_items WHERE guild_id = ? AND (status = 'pending' OR (status = 'approved' AND item IN ({}))) ORDER BY id".format(
            ",".join("?" * len(ctx.REFUNDABLE_AFTER_APPROVAL))), (gid, *sorted(ctx.REFUNDABLE_AFTER_APPROVAL))):
        if r["item"] not in ctx.SHOP_ITEMS:
            continue
        lines = discord_text_html(ctx, "\n".join(ctx.request_lines(ctx.guild, r)))
        buttons = ""
        if r["status"] == "pending":
            buttons += f'<button class="btn btn-gold btn-sm" type="button" data-shopreq="{r["id"]}" data-ok="1">APPROVE</button>'
        buttons += f'<button class="btn btn-ghost btn-sm" type="button" data-shopreq="{r["id"]}" data-ok="0">REFUND</button>'
        reqs += (f'<div class="sreq"><div class="sreq-t">{lines}<span class="muted">{"Waiting" if r["status"] == "pending" else "Approved, being arranged"}'
                 f' · {when_html(r["created_at"])}</span></div><div class="btns">{buttons}</div></div>')
    blocks.append(("SHOP REQUESTS", reqs or '<p class="muted">Nothing waiting. Requests show in pfo-bot too, and both stay in sync.</p>'))

    # Loot today
    day_start = int(ctx.to_utc(datetime.combine(ctx.uk_now().date(), datetime.min.time())).timestamp())
    loot = ""
    for d in ctx.q("SELECT * FROM loot_drops WHERE guild_id = ? AND created_at >= ? ORDER BY created_at", (gid, day_start)):
        state = (f'grabbed by {esc(player_name(ctx, d["claimed_by"]))}' if d["claimed_by"] else d["status"])
        loot += f'<li><span class="prank-who">{when_html(d["created_at"])}</span><span class="prank-v">{esc(state)}</span></li>'
    try:
        planned = ctx.q("SELECT * FROM loot_schedule WHERE guild_id = ? AND done = 0 AND at >= ? ORDER BY at", (gid, int(time.time())))
        for p_ in planned:
            loot += f'<li><span class="prank-who">{when_html(p_["at"])}</span><span class="prank-v muted">planned</span></li>'
    except Exception:
        pass
    blocks.append(("LOOT TODAY", f'<ol class="prank prank-plain">{loot}</ol>' if loot else '<p class="muted">No drops yet today.</p>'))

    # Missing sign-ups
    missing = ""
    try:
        for member, missed, last_fight, last_signup in ctx.find_inactive_fighters(ctx.guild, minimum_missed=2)[:15]:
            missing += (f'<li><span class="prank-who">{esc(clean_name(ctx, member.display_name))}</span>'
                        f'<span class="prank-v tab">{missed} missed</span></li>')
    except Exception:
        traceback.print_exc()
    blocks.append(("MISSING SIGN-UPS", f'<ol class="prank prank-plain">{missing}</ol>' if missing else '<p class="muted">Everyone is signing up.</p>'))

    # Ratings
    try:
        table = ctx.compute_ratings(gid)
        top = sorted((e for e in table.values() if e.get("user_id") and ctx.guild.get_member(e["user_id"])), key=lambda e: -e["rating"])[:20]
        rows = "".join(f'<li><span class="prank-n">{i}</span><span class="prank-who">{name_link(ctx, e["user_id"], fighter_name(ctx, e["user_id"]))}'
                       f'<span class="muted">{e["fights"]} fights · peak {round(e["peak"])}</span></span><span class="prank-v tab">{round(e["rating"])}</span></li>'
                       for i, e in enumerate(top, start=1))
        blocks.append(("SKILL RATINGS · STAFF ONLY", f'<ol class="prank">{rows}</ol><p class="note">Elo from logged fights only (earlier /setrecord records are left out). '
                                                     'Use /rating in Discord for win chances between two fighters.</p>'))
    except Exception:
        traceback.print_exc()

    # Staff report (last week)
    try:
        embed = ctx.staff_report_embed(ctx.guild)
        text = (embed.description or "") + "".join(f"\n**{f.name}**\n{f.value}" for f in getattr(embed, "fields", []))
        blocks.append((esc((embed.title or "STAFF REPORT").upper()), f'<div class="sreport">{discord_text_html(ctx, text)}</div>'))
    except Exception:
        traceback.print_exc()

    # Mod+: errors
    if viewer.levels["mod"]:
        try:
            errs = ctx.q("SELECT place, kind, COUNT(*) AS n, MAX(at) AS last FROM error_log WHERE at > ? GROUP BY place, kind ORDER BY last DESC LIMIT 12",
                         (int(time.time()) - 86400,))
            rows = "".join(f'<li><span class="prank-who">{esc(e["place"])}<span class="muted">{esc(e["kind"])}</span></span>'
                           f'<span class="prank-v tab">{e["n"]}× · {when_html(e["last"])}</span></li>' for e in errs)
            blocks.append(("ERRORS · LAST 24 HOURS", f'<ol class="prank prank-plain">{rows}</ol>' if rows else '<p class="green">No errors in the last 24 hours.</p>'))
        except Exception:
            pass

    links = '<a class="btn btn-ghost btn-sm" href="/results">RESULTS (EDIT)</a><a class="btn btn-ghost btn-sm" href="/rankings">RANKINGS (EDIT)</a>'
    if viewer.levels["senior"]:
        links += '<a class="btn btn-ghost btn-sm" href="/stats">SITE STATS</a>'
    level = "HEAD MODERATOR+" if viewer.levels["senior"] else "MODERATOR" if viewer.levels["mod"] else "HELPER"
    sections = "".join(f'<section class="block bt"><div class="head-t rv"><h2 class="d h-m">{title}</h2></div>{inner}</section>' for title, inner in blocks)
    body = page_hero(f"STAFF · {level}", "STAFF", "STAFF", f'<div class="btns l-up">{links}</div>'
                     '<p class="muted l-up">Staff buttons also show on results, rankings and fighter pages while you\'re logged in. Everything you do is logged in pfo-bot.</p>') + \
        f'<div class="wrap">{sections}</div>'
    return page(ctx, "Staff", "staff", body, "PFO staff dashboard.")


# ------------------------------------------------------------
# Site stats /stats (Head Moderator+)
# ------------------------------------------------------------

def render_stats(ctx, viewer, query):
    now = int(time.time())
    day = 86400
    q = ctx.q

    def one(sql, params=()):
        try:
            return q(sql, params)[0][0] or 0
        except Exception:
            return 0

    accounts = one("SELECT COUNT(*) FROM web_accounts")
    new_week = one("SELECT COUNT(*) FROM web_accounts WHERE first_login > ?", (now - 7 * day,))
    active_week = one("SELECT COUNT(DISTINCT user_id) FROM web_sessions WHERE last_seen > ?", (now - 7 * day,))
    kinds = {"Fighters": 0, "Staff": 0, "Fans": 0, "Left the server": 0}
    for row in q("SELECT user_id FROM web_accounts"):
        member = ctx.guild.get_member(row["user_id"])
        levels = member_levels(ctx.g, ctx.guild, member) if member else None
        if not member:
            kinds["Left the server"] += 1
        elif levels["staff"]:
            kinds["Staff"] += 1
        elif levels["fighter"]:
            kinds["Fighters"] += 1
        else:
            kinds["Fans"] += 1
    posted = one("SELECT COUNT(DISTINCT user_id) FROM social_posts WHERE guild_id = ?", (ctx.gid,))

    since30 = (datetime.now(timezone.utc) - timedelta(days=29)).strftime("%Y-%m-%d")
    since7 = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m-%d")
    views30 = one("SELECT COUNT(*) FROM site_views WHERE day >= ?", (since30,))
    views7 = one("SELECT COUNT(*) FROM site_views WHERE day >= ?", (since7,))
    visitors30 = one("SELECT COUNT(DISTINCT visitor || day) FROM site_views WHERE day >= ?", (since30,))
    visitors7 = one("SELECT COUNT(DISTINCT visitor || day) FROM site_views WHERE day >= ?", (since7,))
    logged_share = one("SELECT ROUND(100.0 * SUM(logged_in) / COUNT(*)) FROM site_views WHERE day >= ?", (since30,))
    installs = one("SELECT COUNT(DISTINCT visitor || day) FROM site_views WHERE installed = 1 AND day >= ?", (since30,))

    daily = q("SELECT day, COUNT(*) AS views, COUNT(DISTINCT visitor) AS people FROM site_views WHERE day >= ? GROUP BY day ORDER BY day", (since30,))
    fight_days = {r["d"] for r in q("SELECT DISTINCT substr(created_at, 1, 10) AS d FROM fights WHERE guild_id = ? AND created_at >= ?", (ctx.gid, since30))}
    peak = max([r["people"] for r in daily] or [1])
    bars = "".join(f'<div class="sbar{" sbar-fight" if r["day"] in fight_days else ""}" title="{esc(r["day"])}: {r["people"]} visitors, {r["views"]} views">'
                   f'<span style="height:{max(3, round(100 * r["people"] / peak))}%"></span><small>{int(r["day"][8:])}</small></div>' for r in daily)

    def split(column, limit=8):
        rows = q(f"SELECT COALESCE({column}, 'Unknown') AS k, COUNT(DISTINCT visitor || day) AS n FROM site_views WHERE day >= ? "
                 f"GROUP BY k ORDER BY n DESC LIMIT {limit}", (since30,))
        total = sum(r["n"] for r in rows) or 1
        return "".join(f'<li><span class="prank-who">{esc(r["k"])}</span><span class="sbar-h"><span style="width:{round(100 * r["n"] / total)}%"></span></span>'
                       f'<span class="prank-v tab">{round(100 * r["n"] / total)}%</span></li>' for r in rows)

    top_pages = "".join(f'<li><span class="prank-who">{esc(r["path"])}</span><span class="prank-v tab">{r["n"]:,}</span></li>' for r in
                        q("SELECT path, COUNT(*) AS n FROM site_views WHERE day >= ? GROUP BY path ORDER BY n DESC LIMIT 12", (since30,)))
    returning = one("SELECT COUNT(*) FROM (SELECT visitor FROM site_views WHERE day >= ? GROUP BY visitor HAVING COUNT(DISTINCT day) > 1)", (since30,))
    per_visit = round(views30 / visitors30, 1) if visitors30 else 0
    hours = {}
    for r in q("SELECT at FROM site_views WHERE day >= ?", (since30,)):
        h = uk_time(r["at"]).hour
        hours[h] = hours.get(h, 0) + 1
    hpeak = max(hours.values() or [1])
    hour_bars = "".join(f'<div class="sbar" title="{h:02d}:00 UK: {hours.get(h, 0)} views"><span style="height:{max(2, round(100 * hours.get(h, 0) / hpeak))}%"></span>'
                        f'<small>{h if h % 3 == 0 else ""}</small></div>' for h in range(24))

    def box(title, inner, note=""):
        return f'<div class="pbox"><h3 class="pbox-h">{title}</h3>{inner}{f"<p class=pbox-note>{note}</p>" if note else ""}</div>'

    strip = (chip("ACCOUNTS", f"{accounts:,}", "gold") + chip("NEW THIS WEEK", f"{new_week:,}") + chip("ACTIVE THIS WEEK", f"{active_week:,}")
             + chip("VISITORS · 7 DAYS", f"{visitors7:,}") + chip("PAGE VIEWS · 7 DAYS", f"{views7:,}"))
    body = page_hero("HEAD MODERATOR+ ONLY", "SITE STATS", "STATS", f'<div class="evchips l-up">{strip}</div>') + f"""
<div class="wrap">
  <section class="block"><div class="head-t rv"><span class="eyebrow">LAST 30 DAYS · GOLD = FIGHT NIGHTS</span><h2 class="d h-m">VISITORS PER DAY</h2></div>
    <div class="sbars">{bars or '<p class="empty">No visits counted yet.</p>'}</div>
    <p class="note">{visitors30:,} visitors · {views30:,} page views · {per_visit} pages per visit · {logged_share}% of views logged in · {returning:,} came back on another day</p></section>
  <section class="block bt"><div class="pgrid pgrid-3">
    {box("ACCOUNTS", "".join(f'<li><span class="prank-who">{k}</span><span class="prank-v tab">{v:,}</span></li>' for k, v in kinds.items()).join(['<ol class="prank prank-plain">', '</ol>']),
         f"{posted} have posted on Social")}
    {box("PHONE VS DESKTOP", f'<ol class="prank prank-plain">{split("device")}</ol>', f"{installs} visits from the home-screen app")}
    {box("WHERE THEY COME FROM", f'<ol class="prank prank-plain">{split("source")}</ol>', "New arrivals only, not clicks around the site")}
    {box("COUNTRIES", f'<ol class="prank prank-plain">{split("country")}</ol>', "From Cloudflare, once the proxy is on")}
    {box("BROWSERS", f'<ol class="prank prank-plain">{split("browser")}</ol>')}
    {box("SYSTEMS", f'<ol class="prank prank-plain">{split("system")}</ol>')}
    {box("TOP PAGES", f'<ol class="prank prank-plain">{top_pages}</ol>')}
    {box("BUSIEST HOURS (UK)", f'<div class="sbars sbars-h">{hour_bars}</div>')}
  </div>
  <p class="note">Counted by the site itself: no cookies, no outside trackers, no IP addresses kept. A visitor is an anonymous code that changes every day, so the same person on two days counts twice.</p></section>
</div>"""
    return page(ctx, "Site stats", "stats", body, "PFO site stats.")


def render_need_login(ctx, title, path, reason="Log in with Discord to see this page."):
    body = page_hero("LOG IN", title.upper(), "PFO") + f'<div class="wrap"><section class="block">{login_box(reason, path)}</section></div>'
    return page(ctx, title, "me", body)


def render_no_access(ctx, title, text):
    body = page_hero("NO ACCESS", title.upper(), "PFO") + f'<div class="wrap"><section class="block"><p class="muted">{esc(text)}</p>' \
        f'<div class="btns"><a class="btn btn-ghost" href="/">HOME</a></div></section></div>'
    return page(ctx, title, "me", body)


# ------------------------------------------------------------
# PWA files
# ------------------------------------------------------------

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="96" fill="#08080A"/>
<rect x="24" y="24" width="464" height="464" rx="80" fill="none" stroke="#C9A24A" stroke-width="10"/>
<text x="256" y="318" font-family="Arial Black, Arial, sans-serif" font-weight="900" font-size="190" fill="#E2C27A" text-anchor="middle" letter-spacing="-6">PFO</text></svg>"""

_icon_cache = {}


def icon_png(size):
    if size in _icon_cache:
        return _icon_cache[size]
    try:
        from PIL import Image, ImageDraw, ImageFont
        import io as _io
        image = Image.new("RGB", (size, size), (8, 8, 10))
        draw = ImageDraw.Draw(image)
        pad = size // 20
        draw.rounded_rectangle([pad, pad, size - pad, size - pad], radius=size // 6, outline=(201, 162, 74), width=max(2, size // 50))
        font = None
        for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf", "arialbd.ttf"):
            try:
                font = ImageFont.truetype(path, size // 3)
                break
            except Exception:
                continue
        font = font or ImageFont.load_default()
        box = draw.textbbox((0, 0), "PFO", font=font)
        draw.text(((size - (box[2] - box[0])) / 2 - box[0], (size - (box[3] - box[1])) / 2 - box[1]), "PFO", font=font, fill=(226, 194, 122))
        out = _io.BytesIO()
        image.save(out, "PNG")
        _icon_cache[size] = out.getvalue()
    except Exception:
        _icon_cache[size] = None
    return _icon_cache[size]


def manifest_json():
    icons = [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}]
    if icon_png(192):
        icons = [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                 {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
                 {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}] + icons
    return json.dumps({
        "name": "Prestige Fighting Organisation", "short_name": "PFO", "start_url": "/?source=app", "scope": "/",
        "display": "standalone", "background_color": "#08080A", "theme_color": "#08080A",
        "description": "PFO rankings, results, live cards, betting and fantasy.", "icons": icons,
    })


SERVICE_WORKER = r"""
// PFO: keeps the styles and script on the phone so pages open fast,
// and shows the last copy of a page when there's no signal.
var CACHE = 'pfo-v__VERSION__';
self.addEventListener('install', function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(['/assets/site.css?v=__VERSION__', '/assets/site.js?v=__VERSION__', '/offline']); }).catch(function () {}));
  self.skipWaiting();
});
self.addEventListener('activate', function (e) {
  e.waitUntil(caches.keys().then(function (keys) { return Promise.all(keys.filter(function (k) { return k !== CACHE; }).map(function (k) { return caches.delete(k); })); }));
  self.clients.claim();
});
self.addEventListener('fetch', function (e) {
  var req = e.request, url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin) return;
  if (url.pathname.indexOf('/api/') === 0 || url.pathname.indexOf('/auth/') === 0 || url.pathname === '/login') return;
  if (url.pathname.indexOf('/assets/') === 0 || url.pathname.indexOf('/media/') === 0) {
    e.respondWith(caches.match(req).then(function (hit) { return hit || fetch(req).then(function (res) {
      var copy = res.clone(); caches.open(CACHE).then(function (c) { c.put(req, copy); }); return res; }); }));
    return;
  }
  if (req.mode === 'navigate') {
    e.respondWith(fetch(req).catch(function () { return caches.match('/offline'); }));
  }
});
"""


# ------------------------------------------------------------
# Teams /teams and /team/ID
# ------------------------------------------------------------

def team_name_html(team):
    tag = f'<span class="tm-tag">{esc(team["tag"])}</span>' if row_get(team, "tag") else ""
    return f'{tag}{esc(team["name"])}'


def team_card(ctx, team, i=0):
    members = ctx.team_members(team["id"])
    rec = ctx.team_record(ctx.gid, team["id"])
    faces = "".join(avatar_html(ctx, uid, fighter_name(ctx, uid), "av av-sm") for uid, _ in members[:8])
    champs = sum(1 for uid, _ in members if ctx.is_any_champion(ctx.gid, uid))
    owners = ctx.team_owner_ids(team)
    owner = ", ".join(fighter_name(ctx, uid) for uid in owners)
    return f"""
      <a class="tmcard {stagger(i, 3)}" href="/team/{team['id']}">
        <span class="d tmcard-name">{team_name_html(team)}</span>
        <span class="muted">Owner{'s' if len(owners) > 1 else ''}: {esc(owner)} · {len(members)} member{'s' if len(members) != 1 else ''}</span>
        <span class="tmcard-faces">{faces}</span>
        <span class="tmcard-stats">
          <span><b class="d">{rec['wins']}-{rec['losses']}-{rec['draws']}</b>TEAM RECORD</span>
          <span><b class="d">{rec['finishes']}</b>FINISHES</span>
          <span><b class="d{' gold' if champs else ''}">{champs}</b>CHAMPS</span>
        </span>
      </a>"""


def render_teams(ctx):
    teams = list(ctx.all_teams(ctx.gid))
    scored = []
    for t in teams:
        rec = ctx.team_record(ctx.gid, t["id"])
        scored.append((rec["wins"] - rec["losses"], rec["wins"], t))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]["name"].lower()))
    cards = "".join(team_card(ctx, t, i) for i, (_, _, t) in enumerate(scored))
    total = sum(len(ctx.team_members(t["id"])) for t in teams)
    strip = chip("TEAMS", len(teams)) + chip("FIGHTERS ON TEAMS", total, "gold")
    how = """
  <section class="block bt"><div class="head-t rv"><span class="eyebrow">IN DISCORD</span><h2 class="d h-m">JOINING A TEAM</h2></div>
    <div class="pgrid">
      <div class="pbox"><h3 class="pbox-h">1 · SET UP BY STAFF</h3><p class="muted">Staff create each team and its Discord role, and pick the owners.</p></div>
      <div class="pbox"><h3 class="pbox-h">2 · GET THE ROLE</h3><p class="muted">Anyone with a team's role is on that team. Want in? Speak to the team's owner or a staff member.</p></div>
      <div class="pbox"><h3 class="pbox-h">3 · FIGHT</h3><p class="muted">Every fight members have while on the team counts towards the team record.</p></div>
    </div>
    <p class="note">See any team in Discord with <code>/team</code>.</p>
  </section>"""
    body = page_hero("FIGHT CAMPS OF THE PFO", "TEAMS", "TEAM", f'<div class="evchips l-up">{strip}</div>') + f"""
<div class="wrap">
  <section class="block"><div class="tmgrid">{cards or '<p class="empty">No teams yet. Staff set teams up in the Discord.</p>'}</div></section>
  {how}
</div>"""
    return page(ctx, "Teams", "teams", body, "PFO fighter teams, members and team records.")


def render_team(ctx, team_id):
    team = ctx.get_team(team_id)
    if not team or team["guild_id"] != ctx.gid:
        return None
    members = ctx.team_members(team["id"])
    owners = ctx.team_owner_ids(team)
    rec = ctx.team_record(ctx.gid, team["id"])
    rows = ""
    cache = {}
    for i, (uid, joined) in enumerate(members):
        name = fighter_name(ctx, uid)
        stats = stats_for(ctx, uid, cache)
        division = main_division(ctx, uid)
        rank = rank_in(ctx, uid, division) if division else None
        role = '<span class="badge badge-gold">OWNER</span>' if uid in owners else ""
        rows += f"""
          <li class="row rank-row {stagger(i, 3)}">
            {avatar_html(ctx, uid, name, 'av av-sm')}
            <span class="tm-who">{name_link(ctx, uid, name, 'd rank-name')}<span class="muted">{esc((division or '').upper())}{(' · ' + esc(rank_label(rank))) if rank is not None else ''}</span></span>
            {role}
            <span class="muted tab">{esc(ctx.stats_record(stats) if stats else '')}</span>
          </li>"""
    # Recent team fights
    fights = []
    for uid, joined in members:
        start = datetime.fromtimestamp(joined, timezone.utc).isoformat()
        fights += ctx.q("SELECT * FROM fights WHERE guild_id = ? AND (winner_id = ? OR loser_id = ?) AND created_at >= ?",
                        (ctx.gid, uid, uid, start))
    seen, recent = set(), []
    for f in sorted(fights, key=lambda f: f["created_at"], reverse=True):
        if f["id"] not in seen:
            seen.add(f["id"])
            recent.append(f)
    results = "".join(result_row(ctx, f, i) for i, f in enumerate(recent[:12]))
    created = datetime.fromtimestamp(team["created_at"], timezone.utc)
    strip = (chip("TEAM RECORD", f"{rec['wins']}-{rec['losses']}-{rec['draws']}", "gold") + chip("FINISHES", rec["finishes"])
             + chip("TITLE WINS", rec["titles"]) + chip("MEMBERS", len(members)))
    label = (f"[{team['tag']}] " if row_get(team, "tag") else "") + team["name"]
    body = page_hero(f"TEAM · SINCE {created.day} {created:%b %Y}".upper(), label.upper(), row_get(team, "tag") or "TEAM",
                     f'<div class="evchips l-up">{strip}</div>') + f"""
<div class="wrap">
  <section class="block"><div class="head-t rv"><h2 class="d h-m">ROSTER</h2></div><ol class="list">{rows}</ol></section>
  <section class="block bt"><div class="head-t rv"><span class="eyebrow">WHILE ON THE TEAM</span><h2 class="d h-m">TEAM FIGHTS</h2></div>
    <div class="list">{results or '<p class="empty">No fights since the team started.</p>'}</div></section>
  <p class="note"><a href="/teams">← All teams</a></p>
</div>"""
    return page(ctx, label, "teams", body, f"{label}: PFO team roster and record.")


# ------------------------------------------------------------
# Staff & contacts /staff (public)
# ------------------------------------------------------------

STAFF_CONTACT = os.getenv("STAFF_CONTACT", "").strip()
STAFF_ORDER = ["Owner", "Vice President", "Head Moderator", "Moderator", "Helper"]
STAFF_ABOUT = {
    "Owner": "Runs the league",
    "Vice President": "Runs the league with the owner",
    "Head Moderator": "Leads the staff team, betting and the economy",
    "Moderator": "Rankings, stats, transfers and discipline",
    "Helper": "Sign-ups, cards, results and recaps",
}


def render_staff_public(ctx):
    guild = ctx.guild
    has = ctx.has_any_role
    groups = {name: [] for name in STAFF_ORDER}
    for member in getattr(guild, "members", []):
        if member.bot:
            continue
        group = None
        if member.id == guild.owner_id:
            group = "Owner"
        else:
            for name in STAFF_ORDER:
                if has(member, [name]):
                    group = name
                    break
        if group:
            groups[group].append(member)
    blocks = ""
    count = 0
    for name in STAFF_ORDER:
        people = sorted(groups[name], key=lambda m: clean_name(ctx, m.display_name).lower())
        if not people:
            continue
        count += len(people)
        cards = "".join(
            f'<div class="stcard {stagger(i, 3)}">{avatar_html(ctx, m.id, clean_name(ctx, m.display_name), "av av-md")}'
            f'<span class="stcard-t"><span class="d stcard-n">{esc(clean_name(ctx, m.display_name))}</span>'
            f'<span class="muted">@{esc(m.name)}</span></span></div>' for i, m in enumerate(people))
        blocks += (f'<section class="block bt"><div class="head-t rv"><span class="eyebrow">{esc(STAFF_ABOUT.get(name, "").upper())}</span>'
                   f'<h2 class="d h-m">{esc(name.upper())}{"S" if len(people) > 1 and not name.endswith("s") else ""}</h2></div>'
                   f'<div class="stgrid">{cards}</div></section>')
    contact = STAFF_CONTACT or "Got a question or a problem? Message any Moderator in the Discord. For anything about a fight result, include the event and your opponent."
    discord_btn = discord_button("Open the Discord", "btn-gold-solid") if DISCORD_INVITE else ""
    body = page_hero("WHO RUNS THE PFO", "STAFF", "STAFF",
                     f'<div class="evchips l-up">{chip("STAFF", count, "gold")}</div><div class="btns l-up">{discord_btn}'
                     '<a class="btn btn-ghost" href="/dashboard" data-staff-only hidden>STAFF DASHBOARD</a></div>') + f"""
<div class="wrap">
  <section class="block"><div class="loginbox stcontact rv"><span class="eyebrow">CONTACT</span><p>{esc(contact)}</p></div></section>
  {blocks or '<section class="block"><p class="empty">No staff roles found.</p></section>'}
</div>"""
    return page(ctx, "Staff", "staffpage", body, "The PFO staff team and how to contact them.")


# ------------------------------------------------------------
# Fighter cards  /cards, /me/cards and the card images
# ------------------------------------------------------------

CARD_TIER_ORDER = ["goat", "double", "champion", "former", "contender", "ranked", "prospect"]
CARD_TIER_TEXT = {"goat": "GOAT", "former": "FORMER CHAMP", "double": "DOUBLE CHAMP", "champion": "CHAMPION", "contender": "CONTENDER", "ranked": "RANKED", "prospect": "PROSPECT"}


def card_bust():
    """Changes every 10 minutes so card pictures refresh after results."""
    return int(time.time() // 600)


def card_version(info):
    """Changes whenever anything on the card changes, so browsers never show an old copy."""
    if not info:
        return str(card_bust())
    raw = json.dumps({k: v for k, v in info.items() if k != "made"}, sort_keys=True, default=str) + SITE_VERSION
    return hashlib.sha1(raw.encode()).hexdigest()[:10]


def fighter_card_img(user_id, name="", cls="cardimg", lazy=True, info=None):
    return (f'<img class="{cls}" src="/cardimg/fighter/{user_id}.png?v={card_version(info)}" width="500" height="700" '
            f'alt="{esc(name)} fighter card"{" loading=lazy" if lazy else ""}>')


def pull_card_img(pull_id, name="", cls="cardimg", info=None):
    version = f"?v={card_version(info)}" if info else ""   # an upgraded card gets a new picture straight away
    return (f'<img class="{cls}" src="/cardimg/pull/{pull_id}.png{version}" width="500" height="700" '
            f'alt="{esc(name)} card" loading="lazy">')


def card_sell_value(ctx, row):
    """Spare value: the bot's card_sell (upgrades don't raise it), or the tier price on an older bot."""
    sell = getattr(ctx, "card_sell", None) if "card_sell" in ctx.g else None
    return sell(row) if sell else ctx.CARD_SELL[row["tier"]]


def fights_like_block(ctx, user_id, stats):
    fn = getattr(ctx, "fights_like", None)
    if not fn or not stats:
        return ""
    try:
        matches = fn(stats, ctx.fighter_division(ctx.gid, user_id), top=3)
    except Exception:
        return ""
    if not matches:
        return ""
    name, score = matches[0]
    style = ctx.fights_like_style(name) if getattr(ctx, "fights_like_style", None) else ""
    values = ctx.ufc_styles().get(name)
    wins = stats["wins"]
    by = stats["wins_by"]
    total = wins + stats["losses"]
    mine = [round(100 * wins / total) if total else 0, round(100 * by.get("KO/TKO", 0) / wins) if wins else 0,
            round(100 * by.get("SUB", 0) / wins) if wins else 0, round(100 * by.get("DEC", 0) / wins) if wins else 0]
    theirs = [0, 0, 0, 0]
    if values:
        _, w, l, ko, sub, dec = values[:6]
        theirs = [round(100 * w / (w + l)) if w + l else 0, round(100 * ko / w) if w else 0,
                  round(100 * sub / w) if w else 0, round(100 * dec / w) if w else 0]
    bars = "".join(
        f'<div class="lk-bar"><span class="lk-k">{label}</span>'
        f'<span class="lk-t"><i style="width:{a}%"></i></span><span class="lk-t lk-u"><i style="width:{b}%"></i></span></div>'
        for label, a, b in zip(["WIN %", "KO", "SUB", "DEC"], mine, theirs))
    others = " · ".join(f"{esc(n)} {s}%" for n, s in matches[1:])
    record = f"{values[1]}-{values[2]}" if values else ""
    return f"""
  <section class="block bt likeblock" aria-labelledby="flike">
    <div class="like">
      <div class="like-ring" style="--p:{score}"><span class="d">{score}%</span></div>
      <div class="like-t">
        <span class="eyebrow" id="flike">FIGHTS LIKE</span>
        <span class="d like-name">{esc(name.upper())}</span>
        <span class="muted">{esc(style)}{(' · UFC record ' + esc(record)) if record else ''}{(' · next closest: ' + others) if others else ''}</span>
      </div>
    </div>
    <div class="lk-legend"><span><i class="lk-dot"></i>PFO</span><span><i class="lk-dot lk-dot-u"></i>{esc(name)}</span></div>
    <div class="lk-bars">{bars}</div>
    <p class="note">Matched on win rate and how they win. Just for fun.</p>
  </section>"""


def render_cards(ctx):
    tiers, cards = ctx.pool_by_tier(ctx.guild)
    order = sorted(cards.values(), key=lambda c: (CARD_TIER_ORDER.index(c["tier"]), -c["ovr"], c["name"].lower()))
    tiles = ""
    for i, info in enumerate(order):
        tiles += (f'<a class="cardtile {stagger(i, 4)}" href="{fighter_url(info["user_id"])}#card" data-tier="{info["tier"]}">'
                  f'{fighter_card_img(info["user_id"], info["name"], info=info)}</a>')
    counts = "".join(chip(CARD_TIER_TEXT[t], len(tiers.get(t, [])), "gold" if t in ("goat", "double", "champion") else "")
                     for t in CARD_TIER_ORDER if tiers.get(t))
    filters = '<div class="cardfilter" data-cardfilter><button type="button" class="on" data-ct="">ALL</button>' + "".join(
        f'<button type="button" data-ct="{t}">{CARD_TIER_TEXT[t]}</button>' for t in CARD_TIER_ORDER if tiers.get(t)) + '</div>'
    packs = "".join(
        f'<div class="pbox"><h3 class="pbox-h">{esc(ctx.SHOP_ITEMS[k][1])} {esc(ctx.SHOP_ITEMS[k][0].upper())} · {ctx.item_price(ctx.gid, k):,}</h3>'
        f'<p class="muted">{esc(ctx.SHOP_ITEMS[k][5])}</p></div>' for k in ("pack_common", "pack_rare", "pack_champ"))
    body = page_hero("COLLECT THE LEAGUE", "CARDS", "CARD", f'<div class="evchips l-up">{chip("FIGHTERS", len(order))}{counts}</div>'
                     '<div class="btns l-up"><a class="btn btn-gold" href="/shop">GET PACKS</a><a class="btn btn-ghost" href="/me/cards">MY CARDS</a></div>') + f"""
<div class="wrap">
  <section class="block">{filters}<div class="cardgrid">{tiles or '<p class="empty">Cards appear for fighters with a weight class role or a UFC fighter name.</p>'}</div></section>
  <section class="block bt"><div class="head-t rv"><span class="eyebrow">IN THE SHOP</span><h2 class="d h-m">PACKS</h2></div>
    <div class="pgrid">{packs}</div>
    <p class="note">Each card's tier comes from where the fighter stands: GOAT, Double Champ, Champion, Former Champ, Contender (#1 to #5), Ranked, Prospect.
    Cards you pull keep the tier they had that day. Duplicates sell for {', '.join(f'{CARD_TIER_TEXT[t].title()} {ctx.CARD_SELL[t]:,}' for t in reversed(CARD_TIER_ORDER))}.</p>
  </section>
</div>"""
    return page(ctx, "Cards", "cards", body, "Every PFO fighter's trading card. Collect them from packs in the shop.")


def render_my_cards(ctx, viewer, query):
    uid = viewer.user_id
    gid = ctx.gid
    have, total = ctx.collection_progress(ctx.guild, uid)
    spares = ctx.spare_cards(gid, uid)
    worth = sum(card_sell_value(ctx, r) for r in spares)

    # Pack just opened
    pack_html = ""
    raw = str(query.get("pack", "") or "")
    if raw.isdigit():
        rows = ctx.q("SELECT * FROM card_packs WHERE id = ? AND user_id = ? AND guild_id = ?", (int(raw), uid, gid))
        if rows:
            pack = rows[0]
            pulls = ctx.pack_cards(pack["id"])
            dupes = ctx.duplicates_in_pack(gid, uid, pack["id"])
            dupe_ids = {r["id"] for r in dupes}
            cards_html = "".join(f'<div class="packcard {stagger(i, 3)}">{pull_card_img(r["id"], ctx.card_info(r)["name"])}'
                                 f'<span class="muted">{CARD_TIER_TEXT[r["tier"]].title()}{" · duplicate" if r["id"] in dupe_ids else ""}{" · sold" if r["status"] == "sold" else ""}</span></div>'
                                 for i, r in enumerate(pulls))
            buttons = ""
            if dupes and not pack["decided"]:
                value = sum(card_sell_value(ctx, r) for r in dupes)
                buttons = (f'<div class="btns"><button class="btn btn-ghost" type="button" data-packdecide="{pack["id"]}" data-d="keep">KEEP DUPLICATES</button>'
                           f'<button class="btn btn-gold" type="button" data-packdecide="{pack["id"]}" data-d="sell">SELL DUPLICATES (+{value:,})</button></div>')
            pack_html = f"""
  <section class="block packopen" aria-label="Pack opened">
    <div class="head-t rv"><span class="eyebrow">{esc(ctx.PACK_NAMES[pack['pack']].upper())} OPENED</span><h2 class="d h-m">YOU PULLED</h2></div>
    <div class="packrow">{cards_html}</div>
    {buttons}
  </section>"""

    # Trades
    trades = ctx.q("SELECT * FROM card_trades WHERE guild_id = ? AND status = 'open' AND (from_id = ? OR to_id = ?) ORDER BY id DESC",
                   (gid, uid, uid))
    trade_rows = ""
    for t in trades:
        give = ctx.trade_card_rows(json.loads(t["give_cards"]))
        want = ctx.trade_card_rows(json.loads(t["want_cards"]))
        incoming = t["to_id"] == uid
        other = t["from_id"] if incoming else t["to_id"]

        def side(rows, coins):
            bits = [f'<span class="tr-card tier-{r["tier"]}">{esc(ctx.card_info(r)["name"])} <b>{ctx.card_info(r)["ovr"]}</b></span>' for r in rows]
            if coins:
                bits.append(f'<span class="tr-card">{coins:,} coins</span>')
            return "".join(bits) or '<span class="muted">Nothing</span>'
        you_give = side(want, t["want_coins"]) if incoming else side(give, t["give_coins"])
        you_get = side(give, t["give_coins"]) if incoming else side(want, t["want_coins"])
        actions = (f'<button class="btn btn-gold btn-sm" type="button" data-trade="{t["id"]}" data-a="accept">ACCEPT</button>'
                   f'<button class="btn btn-ghost btn-sm" type="button" data-trade="{t["id"]}" data-a="decline">DECLINE</button>') if incoming else \
                  f'<button class="btn btn-ghost btn-sm" type="button" data-trade="{t["id"]}" data-a="decline">WITHDRAW</button>'
        trade_rows += f"""
      <div class="trade">
        <span class="eyebrow-grey">{'FROM' if incoming else 'TO'} {esc(player_name(ctx, other).upper())} · ENDS {when_html(t['created_at'] + ctx.TRADE_HOURS * 3600, True)}</span>
        <div class="trade-sides"><div><span class="num-l">YOU GIVE</span>{you_give}</div><div><span class="num-l">YOU GET</span>{you_get}</div></div>
        <div class="btns">{actions}</div>
      </div>"""

    # Collection
    groups = {}
    for row in ctx.owned_cards(gid, uid, include_held=True):
        groups.setdefault((row["fighter_id"], row["tier"]), []).append(row)
    ordered = sorted(groups.values(), key=lambda rows: (CARD_TIER_ORDER.index(rows[0]["tier"]), -ctx.card_info(rows[-1])["ovr"]))
    grid = ""
    for i, rows in enumerate(ordered):
        info = ctx.card_info(rows[-1])
        held = any(r["status"] == "held" for r in rows)
        grid += (f'<div class="cardtile {stagger(i, 4)}" data-tier="{rows[0]["tier"]}">{pull_card_img(rows[-1]["id"], info["name"], info=info)}'
                 + (f'<span class="cardcount">×{len(rows)}</span>' if len(rows) > 1 else "")
                 + ('<span class="cardheld">IN A TRADE</span>' if held else "") + '</div>')
    upgrade_html = ""
    if "card_upgrades" in ctx.g:
        try:
            for row, now, price in ctx.card_upgrades(ctx.guild, uid)[:30]:
                old = ctx.card_info(row)
                tier_note = f" · becomes {CARD_TIER_TEXT[now['tier']].title()}" if now["tier"] != row["tier"] else ""
                upgrade_html += (f'<li><span class="prank-who"><b>{esc(now["name"])}</b> <span class="tab">{old.get("ovr")} → {now["ovr"]}</span>'
                                 f'<span class="muted">Card No. {row["id"]:04d}{esc(tier_note)}</span></span>'
                                 f'<button class="btn btn-gold btn-sm" type="button" data-upgrade="{row["id"]}" data-price="{price}">UPGRADE · {price:,}</button></li>')
        except Exception:
            traceback.print_exc()
    upgrade_section = (f'<section class="block bt" id="upgrades"><div class="head-t rv"><span class="eyebrow">FIGHTERS WHO HAVE IMPROVED</span>'
                       f'<h2 class="d h-m">UPGRADE CARDS</h2></div><ol class="prank prank-plain upglist">{upgrade_html}</ol>'
                       '<p class="note">Upgrading brings a card up to the fighter\'s current rating. The price depends on the new overall. '
                       'An upgraded card still sells as a spare for what it was pulled as.</p></section>') if upgrade_html else ""
    mine_options = "".join(
        f'<label class="tr-pick"><input type="checkbox" name="give" value="{r["id"]}"> {esc(ctx.card_label(r))}</label>'
        for r in reversed(ctx.owned_cards(gid, uid)))
    sell_btn = (f'<button class="btn btn-ghost" type="button" data-sellspares>SELL {len(spares)} SPARE{"S" if len(spares) != 1 else ""} (+{worth:,})</button>'
                if spares else "")
    body = page_hero("MY PFO", "MY CARDS", "CARD", f'<div class="evchips l-up">{chip("COLLECTION", f"{have}/{total}", "gold")}'
                     f'{chip("CARDS", sum(len(g) for g in groups.values()))}{chip("SPARES", len(spares))}</div>'
                     f'<div class="btns l-up"><a class="btn btn-gold" href="/shop">GET PACKS</a><a class="btn btn-ghost" href="/cards">ALL CARDS</a>{sell_btn}</div>') + f"""
<div class="wrap">
  {pack_html}
  {f'<section class="block bt"><div class="head-t rv"><h2 class="d h-m">OPEN TRADES</h2></div>{trade_rows}</section>' if trade_rows else ''}
  {upgrade_section}
  <section class="block bt"><div class="head-t rv"><span class="eyebrow">{have} OF {total} FIGHTERS</span><h2 class="d h-m">COLLECTION</h2></div>
    <div class="cardgrid">{grid or '<p class="empty">No cards yet. Packs are in the shop.</p>'}</div></section>
  <section class="block bt" id="newtrade"><div class="head-t rv"><span class="eyebrow">UP TO {ctx.TRADE_MAX_CARDS} CARDS A SIDE</span><h2 class="d h-m">MAKE A TRADE</h2></div>
    <div class="tradeform" data-tradeform>
      <label class="num-l" for="trwho">TRADE WITH</label>
      <input id="trwho" class="tr-in" type="search" placeholder="Start typing a name" autocomplete="off">
      <div id="trwhores" class="pickres"></div>
      <div class="tr-cols">
        <div><span class="num-l">YOUR CARDS</span><div class="tr-list" id="trmine">{mine_options or '<p class="muted">No cards free to trade.</p>'}</div>
          <label class="num-l" for="trgivec">ADD COINS</label><input id="trgivec" class="tr-in" type="number" min="0" step="50" value="0"></div>
        <div><span class="num-l">THEIR CARDS</span><div class="tr-list" id="trtheirs"><p class="muted">Pick who first.</p></div>
          <label class="num-l" for="trwantc">ASK FOR COINS</label><input id="trwantc" class="tr-in" type="number" min="0" step="50" value="0"></div>
      </div>
      <button class="btn btn-gold" type="button" id="trsend">SEND OFFER</button>
      <p class="note">The offer is posted in the Discord for them to accept. Your cards and coins are held until they answer, or for {ctx.TRADE_HOURS} hours.</p>
    </div>
  </section>
</div>"""
    return page(ctx, "My cards", "me", body, "Your PFO fighter card collection.")


# ------------------------------------------------------------
# Replays
# ------------------------------------------------------------

def site_host():
    return re.sub(r"^https?://", "", SITE_URL or "prestigefightingorg.com").split("/")[0].split(":")[0]


def replay_player(ctx, vod, eyebrow="LAST EVENT · REPLAY", show_results=True):
    if not vod:
        return ""
    video, clip = vod["video_id"], vod["clip_id"]
    host = site_host()
    if video:
        src = f"https://player.twitch.tv/?video=v{esc(video)}&parent={esc(host)}&autoplay=true&muted=true"
    else:
        src = f"https://clips.twitch.tv/embed?clip={esc(clip)}&parent={esc(host)}&autoplay=true&muted=true"
    frame = (f'<div class="watch-frame" data-replay="{esc(video or "")}" data-host="{esc(host)}">'
             f'<iframe src="{src}" title="{esc(vod["event"])} replay" allowfullscreen allow="autoplay; fullscreen"></iframe></div>')
    results = ""
    if show_results:
        fights = ctx.event_fights(ctx.gid, vod["event"]) if getattr(ctx, "event_fights", None) else []
        rows = "".join(result_row(ctx, f, i) for i, f in enumerate(list(reversed(fights))[:6]))
        if rows:
            results = (f'<div class="replay-res"><span class="num-l">RESULTS</span><div class="list">{rows}</div>'
                       f'<a class="btn btn-gold btn-sm" href="{event_url(vod["event"])}">FULL RESULTS</a></div>')
    url = vod["url"] if vod["url"].startswith("http") else "https://" + vod["url"]
    return f"""
  <section class="block bt replay" id="replay" aria-label="{esc(vod['event'])} replay">
    <div class="head rv"><div class="head-t"><span class="eyebrow">{esc(eyebrow)}</span><h2 class="d h-m">{esc(vod['event'].upper())}</h2></div>
      <a class="btn btn-ghost btn-sm" href="{esc(url)}" target="_blank" rel="noopener">WATCH ON TWITCH ↗</a></div>
    <div class="replay-grid{' replay-solo' if not results else ''}">{frame}{results}</div>
    <p class="note">Plays muted on a loop. Tap the player for sound.</p>
  </section>"""


def is_live_now(ctx):
    try:
        rows = ctx.q("SELECT live FROM twitch_state WHERE id = 1")
        return bool(rows and rows[0]["live"])
    except Exception:
        return False


def home_replay(ctx):
    fn = getattr(ctx, "latest_vod", None)
    if not fn or is_live_now(ctx):
        return ""
    try:
        return replay_player(ctx, fn(ctx.gid))
    except Exception:
        return ""


def add_page_routes(app, bot, g, serve, html_response):
    """Routes for the pages added with logins. Personal pages are built per visitor and never cached."""

    def guild():
        return pick_guild(bot, g)

    def private_response(text, status=200):
        return web.Response(text=text, status=status, content_type="text/html", charset="utf-8",
                            headers={"Cache-Control": "private, no-store"})

    async def personal(request, title, maker, need=None, need_text="You don't have access to this page."):
        if not bot.is_ready():
            return html_response(render_starting(), 503)
        gd = guild()
        if gd is None:
            return html_response(render_starting(), 503)
        ctx = Ctx(g, gd)
        viewer = Viewer(g, gd, read_session(g, request))
        try:
            if not viewer.session:
                html = await asyncio.to_thread(render_need_login, ctx, title, request.path_qs)
                return private_response(html)
            if not viewer.ok:
                html = await asyncio.to_thread(render_no_access, ctx, title, "You need to be in the PFO Discord to use this.")
                return private_response(html, 403)
            if need and not viewer.levels.get(need):
                html = await asyncio.to_thread(render_no_access, ctx, title, need_text)
                return private_response(html, 403)
            html = await asyncio.to_thread(maker, ctx, viewer, request.query)
            return private_response(html)
        except Exception as error:
            traceback.print_exc()
            note = g.get("note_error")
            if note:
                note(f"Website page: {request.path}", error, viewer.user_id)
            return private_response("<!doctype html><meta charset=utf-8><body style='background:#08080A;color:#eee;font:16px system-ui;padding:40px'>"
                                    "<p>Something went wrong loading this page. Staff have been told.</p><p><a style='color:#E2C27A' href='/'>Home</a></p>", 500)

    async def me_page(request):
        return await personal(request, "My PFO", render_me)

    async def my_cards_page(request):
        return await personal(request, "My cards", render_my_cards)

    async def cards_page(request):
        return await serve(request, "cards", render_cards)

    _card_png = {}

    async def card_image(request):
        kind = request.match_info.get("kind", "")
        raw = request.match_info.get("ident", "")
        if kind not in ("fighter", "pull") or not raw.isdigit():
            raise web.HTTPNotFound()
        gd = guild()
        if gd is None:
            raise web.HTTPServiceUnavailable()
        key = (kind, int(raw), request.query.get("v", ""))
        hit = _card_png.get(key)
        if hit and time.time() - hit[0] < 600:
            data = hit[1]
        else:
            if kind == "fighter":
                member = gd.get_member(int(raw))
                if not member:
                    raise web.HTTPNotFound()
                data = await g["card_png_for_fighter"](gd, member)
            else:
                rows = g["discipline_query"]("SELECT * FROM card_pulls WHERE id = ? AND guild_id = ?", (int(raw), gd.id))
                if not rows:
                    raise web.HTTPNotFound()
                data = await g["card_png_for_pull"](gd, rows[0])
            if not data:
                raise web.HTTPNotFound()
            if len(_card_png) > 300:
                _card_png.clear()
            _card_png[key] = (time.time(), data)
        headers = {"Cache-Control": "public, max-age=600"}
        if request.query.get("download"):
            headers["Content-Disposition"] = f'attachment; filename="pfo-card-{raw}.png"'
        return web.Response(body=data, content_type="image/png", headers=headers)

    @action_route(bot, g, bucket="cards", limit=20, window=60)
    async def cards_pack(viewer, body, request):
        gd = guild()
        pack_id = _int(body.get("pack_id"), 0)
        rows = g["discipline_query"]("SELECT * FROM card_packs WHERE id = ? AND user_id = ? AND guild_id = ?", (pack_id, viewer.user_id, gd.id))
        if not rows:
            raise ActionError("That pack isn't yours.")
        if rows[0]["decided"]:
            raise ActionError("Already done.")
        if body.get("decision") == "sell":
            total = g["sell_cards"](gd.id, viewer.user_id, g["duplicates_in_pack"](gd.id, viewer.user_id, pack_id))
            message = f"Sold for {total:,} coins."
        else:
            total, message = 0, "Kept. You can sell spares any time."
        g["discipline_query"]("UPDATE card_packs SET decided = ? WHERE id = ?", ("sell" if body.get("decision") == "sell" else "keep", pack_id), write=True)
        return {"ok": True, "message": message, "balance": g["get_wallet"](gd.id, viewer.user_id)["balance"]}

    @action_route(bot, g, bucket="cards", limit=20, window=60)
    async def cards_upgrade(viewer, body, request):
        gd = guild()
        if "upgrade_card" not in g:
            raise ActionError("Card upgrades aren't switched on yet.")
        ok, message = g["upgrade_card"](gd, viewer.user_id, _int(body.get("card_id"), 0))
        if not ok:
            raise ActionError(message.replace("❌ ", ""))
        return {"ok": True, "message": message.replace("⬆️ ", "").replace("**", "").replace(g.get("COIN", ""), "").replace("  ", " "),
                "balance": g["get_wallet"](gd.id, viewer.user_id)["balance"]}

    @action_route(bot, g, bucket="spin", limit=10, window=60)
    async def daily_spin(viewer, body, request):
        gd = guild()
        if "do_spin" not in g:
            raise ActionError("The daily spin isn't switched on yet.")
        blocked = g["member_too_new"](gd, viewer.user_id) if "member_too_new" in g else None
        if blocked:
            raise ActionError(re.sub(r"[*_`]", "", blocked))
        result = g["do_spin"](gd.id, viewer.user_id)
        if not result["claimed"]:
            raise ActionError("You've already spun today. The wheel resets at midnight UK time.")
        key, emoji, label, _, reward = result["prize"]
        if key == "again":
            message = "Spin again! You get one more spin today."
        else:
            message = f"You won {label}!"
            if result.get("pack_id"):
                message += " Your cards are on My cards."
            elif not isinstance(reward, int) and reward not in g.get("PACK_ITEMS", {}):
                message += " It's in My items and is used on your next bet."
        if result.get("top_up"):
            message += f" You were nearly out, so you got topped up to {g['BROKE_TOP_UP']:,}."
        return {"ok": True, "message": message, "index": result["index"], "again": key == "again",
                "pack_id": result.get("pack_id"), "streak": result["streak"],
                "balance": g["get_wallet"](gd.id, viewer.user_id)["balance"]}

    @action_route(bot, g, bucket="cards", limit=20, window=60)
    async def cards_sell_spares(viewer, body, request):
        gd = guild()
        spares = g["spare_cards"](gd.id, viewer.user_id)
        if not spares:
            raise ActionError("No spare cards to sell.")
        total = g["sell_cards"](gd.id, viewer.user_id, spares)
        return {"ok": True, "message": f"Sold {len(spares)} spare card{'s' if len(spares) != 1 else ''} for {total:,} coins.",
                "balance": g["get_wallet"](gd.id, viewer.user_id)["balance"]}

    @action_route(bot, g, bucket="trade", limit=10, window=60)
    async def cards_trade(viewer, body, request):
        gd = guild()
        to = gd.get_member(_int(body.get("to"), 0))
        if not to or to.bot:
            raise ActionError("Pick who you're trading with.")
        give = [_int(x, 0) for x in (body.get("give") or [])][:5]
        want = [_int(x, 0) for x in (body.get("want") or [])][:5]
        trade_id, error = g["create_trade"](gd.id, viewer.user_id, to.id, [x for x in give if x], [x for x in want if x],
                                            max(0, _int(body.get("give_coins"), 0) or 0), max(0, _int(body.get("want_coins"), 0) or 0))
        if error:
            raise ActionError(re.sub(r"^[^\w]+", "", error).replace("**", ""))
        posted = await g["post_web_trade"](gd, trade_id)
        await g["trade_log"](gd, f"<@{viewer.user_id}> offered <@{to.id}> a trade from the website (#{trade_id}).")
        return {"ok": True, "message": "Offer sent." + ("" if posted else " (It couldn't be posted in the Discord, but they can see it on My cards.)")}

    @action_route(bot, g, bucket="trade", limit=20, window=60)
    async def cards_trade_respond(viewer, body, request):
        gd = guild()
        trade = g["get_trade"](_int(body.get("trade_id"), 0))
        if not trade or trade["guild_id"] != gd.id or viewer.user_id not in (trade["from_id"], trade["to_id"]):
            raise ActionError("That trade isn't yours.")
        if trade["status"] != "open":
            raise ActionError("That trade is closed.")
        if body.get("action") == "accept":
            if viewer.user_id != trade["to_id"]:
                raise ActionError("Only the person it's for can accept.")
            error = g["complete_trade"](trade, viewer.user_id)
            if error and g["get_trade"](trade["id"])["status"] == "open":
                raise ActionError(re.sub(r"^[^\w]+", "", error).replace("**", ""))
            message = error or "Trade done. The cards are yours."
            if not error:
                await g["trade_log"](gd, f"<@{trade['from_id']}> and <@{trade['to_id']}> traded on the website (#{trade['id']}).")
        else:
            g["release_trade"](trade, "withdrawn" if viewer.user_id == trade["from_id"] else "declined", viewer.user_id)
            message = "Trade withdrawn." if viewer.user_id == trade["from_id"] else "Trade declined."
        await g["refresh_trade_post"](trade["id"])
        return {"ok": True, "message": message, "balance": g["get_wallet"](gd.id, viewer.user_id)["balance"]}

    async def cards_of(request):
        viewer = viewer_for(bot, g, request)
        if not viewer.ok:
            return jres({"cards": []}, 401)
        gd = guild()
        uid = _int(request.query.get("user"), 0)
        rows = g["owned_cards"](gd.id, uid) if uid and uid != viewer.user_id else []
        return jres({"cards": [{"id": r["id"], "label": re.sub(r"^\S+\s", "", g["card_label"](r)), "tier": r["tier"]} for r in reversed(rows)]})

    async def teams_page(request):
        return await serve(request, "teams", render_teams)

    async def team_page(request):
        raw = request.match_info.get("team_id", "")
        if not raw.isdigit():
            return await serve(request, "404", render_404, 404)
        return await serve(request, f"team:{raw}", lambda ctx: render_team(ctx, int(raw)))

    async def staff_public(request):
        return await serve(request, "staffpublic", render_staff_public)

    async def staff_page(request):
        return await personal(request, "Staff", render_staff, "staff", "The staff dashboard is for Helpers and above.")

    async def stats_page(request):
        return await personal(request, "Site stats", render_stats, "senior", "Site stats are for Head Moderators and above.")

    async def shop(request):
        return await serve(request, "shop", render_shop)

    async def social(request):
        return await serve(request, "social", render_social)

    async def records(request):
        return await serve(request, "records", render_records)

    async def awards(request):
        return await serve(request, "awards", render_awards)

    async def week(request):
        wanted = (request.query.get("w") or "")[:10]
        if wanted and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", wanted):
            wanted = ""
        return await serve(request, f"week:{wanted}", lambda ctx: render_week(ctx, wanted or None))

    async def app_page(request):
        return await serve(request, "app", render_app)

    async def offline(request):
        return await serve(request, "offline", lambda ctx: page(ctx, "Offline", "home", page_hero("NO CONNECTION", "OFFLINE", "PFO") +
                                                                  '<div class="wrap"><section class="block"><p class="muted">You\'re offline. Pages you\'ve opened before will '
                                                                  'still load. Try again when you have signal.</p></section></div>'))

    async def manifest(request):
        return web.Response(text=manifest_json(), content_type="application/manifest+json", headers={"Cache-Control": "public, max-age=86400"})

    async def service_worker(request):
        return web.Response(text=SERVICE_WORKER.replace("__VERSION__", SITE_VERSION), content_type="application/javascript",
                            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    async def icon_svg(request):
        return web.Response(text=ICON_SVG, content_type="image/svg+xml", headers={"Cache-Control": "public, max-age=604800"})

    async def icon(request):
        size = 512 if "512" in request.path else 192
        data = await asyncio.to_thread(icon_png, size)
        if not data:
            return redirect("/icon.svg")
        return web.Response(body=data, content_type="image/png", headers={"Cache-Control": "public, max-age=604800"})

    # ---------------- personal fragments ----------------
    async def home_panel(request):
        viewer = viewer_for(bot, g, request)
        if not viewer.ok:
            return jres({"html": ""})
        try:
            html = await asyncio.to_thread(render_home_panel, Ctx(g, guild()), viewer)
        except Exception as error:
            traceback.print_exc()
            note = g.get("note_error")
            if note:
                note("Website: home panel", error, viewer.user_id)
            html = ""
        return jres({"html": html})

    @action_route(bot, g, bucket="tasks", limit=6, window=60)
    async def claim_tasks(viewer, body, request):
        gd = guild()
        blocked = g["member_too_new"](gd, viewer.user_id)
        if blocked:
            raise ActionError(re.sub(r"^[^\w]+", "", blocked).replace("**", ""))
        total, names = g["claim_tasks"](gd.id, viewer.user_id)
        if not total:
            raise ActionError("Nothing to claim yet.")
        return {"ok": True, "message": f"Claimed {total:,} coins.", "balance": g["get_wallet"](gd.id, viewer.user_id)["balance"]}

    # ---------------- staff actions ----------------
    @action_route(bot, g, need="staff", bucket="staff", limit=40, window=60)
    async def staff_fight(viewer, body, request):
        gd = guild()
        fight_id = _int(body.get("fight_id"), 0)
        if body.get("action") == "remove":
            ok, message = await g["web_remove_fight"](gd, fight_id, viewer.user_id)
        else:
            edits = {}
            for key in ("method", "round", "event"):
                if body.get(key) not in (None, ""):
                    edits[key] = body.get(key)
            if body.get("title_fight") is not None:
                edits["title_fight"] = bool(body.get("title_fight"))
            if body.get("swap"):
                edits["swap"] = True
            ok, message = await g["web_edit_fight"](gd, fight_id, viewer.user_id, edits)
        if not ok:
            raise ActionError(message)
        _cache.clear()
        return {"ok": True, "message": message}

    @action_route(bot, g, need="staff", bucket="staff", limit=40, window=60)
    async def staff_rank(viewer, body, request):
        gd = guild()
        rank = body.get("rank")
        rank = None if rank in (None, "", "remove") else _int(rank)
        if body.get("rank") not in (None, "", "remove") and rank is None:
            raise ActionError("Pick a rank.")
        ok, message = await g["web_set_rank"](gd, str(body.get("weight", "")), _int(body.get("user_id"), 0), rank, viewer.user_id)
        if not ok:
            raise ActionError(message)
        _cache.clear()
        return {"ok": True, "message": message}

    @action_route(bot, g, need="staff", bucket="staff", limit=40, window=60)
    async def staff_shop(viewer, body, request):
        ok, message = await g["web_shop_decision"](guild(), _int(body.get("item_id"), 0), viewer.user_id, bool(body.get("approve")))
        if not ok:
            raise ActionError(message)
        return {"ok": True, "message": message}

    async def staff_fight_info(request):
        viewer = viewer_for(bot, g, request)
        if not viewer.ok or not viewer.levels["staff"]:
            return jres({"ok": False}, 403)
        fight = g["get_fight"](guild().id, _int(request.query.get("id"), 0))
        if not fight:
            return jres({"ok": False}, 404)
        return jres({"ok": True, "id": fight["id"], "method": fight["method"], "round": fight["round"], "event": fight["event"] or "",
                     "title_fight": bool(fight["title_fight"]), "winner": fighter_name(Ctx(g, guild()), fight["winner_id"], fight["winner_name"]),
                     "loser": fighter_name(Ctx(g, guild()), fight["loser_id"], fight["loser_name"])})

    for path, handler in (("/me/cards", my_cards_page), ("/cards", cards_page), ("/cardimg/{kind}/{ident}.png", card_image),
                          ("/api/cards/of", cards_of)):
        app.router.add_get(path, handler)
    for path, handler in (("/api/cards/pack", cards_pack), ("/api/cards/sell", cards_sell_spares), ("/api/cards/upgrade", cards_upgrade),
                          ("/api/spin", daily_spin), ("/api/cards/trade", cards_trade),
                          ("/api/cards/trade/respond", cards_trade_respond)):
        app.router.add_post(path, handler)
    for path, handler in (("/me", me_page), ("/dashboard", staff_page), ("/staff", staff_public), ("/teams", teams_page),
                          ("/team/{team_id}", team_page), ("/stats", stats_page), ("/shop", shop), ("/social", social),
                          ("/records", records), ("/awards", awards), ("/week", week), ("/app", app_page), ("/offline", offline),
                          ("/manifest.webmanifest", manifest), ("/sw.js", service_worker), ("/icon.svg", icon_svg),
                          ("/icon-192.png", icon), ("/icon-512.png", icon), ("/api/home", home_panel), ("/api/staff/fight", staff_fight_info)):
        app.router.add_get(path, handler)
    for path, handler in (("/api/tasks/claim", claim_tasks), ("/api/staff/fight", staff_fight), ("/api/staff/rank", staff_rank),
                          ("/api/staff/shop", staff_shop)):
        app.router.add_post(path, handler)


def make_app(bot, g):

    def html_response(text, status=200):
        return web.Response(text=text, status=status, content_type="text/html", charset="utf-8",
                            headers={"Cache-Control": "public, max-age=30, stale-while-revalidate=120"})

    async def serve(request, key, maker, status=200):
        if not bot.is_ready():
            return html_response(render_starting(), 503)
        try:
            text = await build(bot, g, key, maker)
        except Exception as error:
            traceback.print_exc()
            note = g.get("note_error")
            if note:
                note(f"Website page: {request.path}", error)
            return html_response("<!doctype html><meta charset=utf-8><body style='background:#08080A;color:#eee;font:16px system-ui;padding:40px'>"
                                 "<p>Something went wrong loading this page. Try again in a moment.</p><p><a style='color:#E2C27A' href='/'>Home</a></p>", 500)
        if text is None:
            return html_response(await build(bot, g, "404", render_404), 404)
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
        filters = {key: (request.query.get(key) or "").strip()[:40] for key in RESULT_FILTERS}
        filters = {key: value for key, value in filters.items() if value}
        key = "results:" + str(page_no) + ":" + urlencode(sorted(filters.items()))
        return await serve(request, key, lambda ctx: render_results(ctx, page_no, filters))

    async def titles(request):
        return await serve(request, "titles", render_titles)

    async def leaderboards(request):
        return await serve(request, "leaderboards", render_leaderboards)

    async def hall(request):
        return await serve(request, "halloffame", render_hall_of_fame)

    async def event_page(request):
        slug = request.match_info.get("name", "")
        return await serve(request, f"event:{slug.lower()}", lambda ctx: render_event(ctx, slug))

    async def live(request):
        return await serve(request, "live", render_live)

    async def sportsbook(request):
        return await serve(request, "sportsbook", render_sportsbook)

    async def fantasy(request):
        return await serve(request, "fantasy", render_fantasy)

    async def compare(request):
        def number(key):
            raw = request.query.get(key, "")
            return int(raw) if raw.isdigit() else None
        a, b = number("a"), number("b")
        return await serve(request, f"compare:{a}:{b}", lambda ctx: render_compare(ctx, a, b))

    async def css(request):
        return web.Response(text=SITE_CSS, content_type="text/css", headers={"Cache-Control": "public, max-age=86400"})

    async def js(request):
        return web.Response(text=SITE_JS, content_type="application/javascript", headers={"Cache-Control": "public, max-age=86400"})

    async def health(request):
        return web.Response(text="ok")

    async def not_found(request):
        return await serve(request, "404", render_404, 404)

    @web.middleware
    async def security(request, handler):
        try:
            response = await handler(request)
        except web.HTTPException as http_error:
            response = http_error
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if secure_cookie_flag():
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        try:
            if (request.method == "GET" and response.status == 200 and (response.content_type or "") == "text/html"
                    and not request.path.startswith(("/api/", "/assets/", "/media/", "/auth/"))):
                logged_in = SESSION_COOKIE in request.cookies
                asyncio.get_running_loop().run_in_executor(None, record_view, g, request, logged_in)
                if logged_in:
                    asyncio.get_running_loop().run_in_executor(None, record_task_visit, bot, g, request)
        except Exception:
            pass
        if isinstance(response, web.HTTPException):
            raise response
        return response

    try:
        setup_web_tables(g)
    except Exception as error:
        print(f"Website tables: {error}")

    app = web.Application(middlewares=[security], client_max_size=6 * 1024 * 1024)
    add_account_routes(app, bot, g)
    add_action_routes(app, bot, g)
    app.router.add_get("/", home)
    app.router.add_get("/rankings", rankings)
    app.router.add_get("/fighters", fighters)
    app.router.add_get("/fighter/{user_id}", fighter)
    app.router.add_get("/results", results)
    app.router.add_get("/titles", titles)
    app.router.add_get("/leaderboards", leaderboards)
    app.router.add_get("/hall-of-fame", hall)
    app.router.add_get("/event/{name}", event_page)
    app.router.add_get("/live", live)
    app.router.add_get("/sportsbook", sportsbook)
    app.router.add_get("/bets", sportsbook)
    app.router.add_get("/fantasy", fantasy)
    app.router.add_get("/compare", compare)
    app.router.add_get("/assets/site.css", css)
    app.router.add_get("/assets/site.js", js)
    app.router.add_get("/health", health)
    add_page_routes(app, bot, g, serve, html_response)
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