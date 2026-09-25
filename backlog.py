"""Steam/HowLongToBeat fetching and scoring, ported from the steam_backlog.py
CLI script.

Everything here works on a plain sqlite3 connection so it can run from the
web app's background thread and from `flask --app app refresh` alike.
"""

import json
import math
import re
import sqlite3
import time

import requests

STEAM_DELAY = 0.5        # seconds between Steam review API calls
# appdetails is rate-limited to roughly 200 requests / 5 minutes.
STORE_DELAY = 1.5
HLTB_DELAY = 1.1         # seconds between HowLongToBeat searches
MIN_SIMILARITY = 0.68    # minimum HLTB match quality to trust
DEFAULT_MIN_REVIEWS = 50
# Games with less playtime than this still count as "unplayed" -- the ones
# you launched once and bounced off.
DEFAULT_PLAYED_MINUTES = 60
# Scores treat anything shorter than this as this long, so a party game
# HLTB lists at 10 minutes doesn't outrank everything.
MIN_SCORED_HOURS = 1.0

# Steam reports type "game" even for tools and test builds, so non-games
# are spotted by genre, by name, or by having no store metadata at all.
SOFTWARE_GENRES = {
    "utilities", "software training", "design & illustration", "video production",
    "animation & modeling", "audio production", "photo editing", "web publishing",
    "game development", "education", "accounting",
}
_NON_GAME_NAME = re.compile(
    r"\b(beta|playtest|test server|tech test|closed test|demo|dedicated server|sdk)\b",
    re.IGNORECASE,
)

# Session-length buckets for the "what do I feel like" toggle:
# key -> (label, min hours inclusive, max hours exclusive)
LENGTHS = {
    "any": ("Any length", None, None),
    "short": ("Quick <3h", 0, 3),
    "medium": ("Weekend 3–15h", 3, 15),
    "long": ("Long 15h+", 15, None),
}
# A "running" refresh that hasn't reported progress in this long is presumed
# dead (worker restarted mid-run) and may be taken over by a new one.
STALE_AFTER = 180


class SteamError(Exception):
    pass


def connect(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


# ── Settings / filters ──────────────────────────────────────────────────────

def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_min_reviews(conn):
    try:
        return int(get_setting(conn, "min_reviews", DEFAULT_MIN_REVIEWS))
    except ValueError:
        return DEFAULT_MIN_REVIEWS


def get_played_minutes(conn):
    try:
        return int(get_setting(conn, "played_minutes", DEFAULT_PLAYED_MINUTES))
    except ValueError:
        return DEFAULT_PLAYED_MINUTES


def get_filters(conn):
    return [r["keyword"].lower() for r in conn.execute("SELECT keyword FROM filters")]


def is_filtered(cats, filters):
    cats = [c.lower() for c in cats]
    return any(f in cat for f in filters for cat in cats)


def coop_modes(cats):
    """Ways to play a game together using only the one copy we own: on one
    screen, or streamed to a second machine with Remote Play Together.
    Remote Play Together alone doesn't count -- it's also on versus-only
    games -- so it needs a co-op category alongside it."""
    modes = []
    if "Shared/Split Screen Co-op" in cats:
        modes.append("couch")
    if "Remote Play Together" in cats and any("Co-op" in c for c in cats):
        modes.append("remote play")
    return modes


# ── Steam API ───────────────────────────────────────────────────────────────

def _get_json(url, params, timeout=10):
    """GET a JSON endpoint, backing off when Steam rate-limits us (429).

    Returns None on any failure so the caller skips caching that game and
    simply retries it on the next refresh (the CLI script cached failures
    as "no data" forever).
    """
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
        except requests.RequestException:
            return None
        if resp.status_code == 429:
            time.sleep(30 * (attempt + 1))
            continue
        if not resp.ok:
            return None
        try:
            return resp.json()
        except ValueError:
            return None
    return None


def get_owned_games(api_key, steam_id):
    try:
        resp = requests.get(
            "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/",
            params={
                "key": api_key,
                "steamid": steam_id,
                "include_appinfo": 1,
                "include_played_free_games": 0,
                "format": "json",
            },
            timeout=30,
        )
    except requests.RequestException as e:
        raise SteamError(f"Couldn't reach the Steam API: {e}") from e
    if resp.status_code in (401, 403):
        raise SteamError("Steam rejected the API key -- check it in Settings.")
    if not resp.ok:
        raise SteamError(f"Steam API returned HTTP {resp.status_code}.")
    games = resp.json().get("response", {}).get("games")
    if games is None:
        raise SteamError(
            "Steam returned no games. Check the Steam ID, and that your profile "
            "and 'Game details' are set to Public in Steam's privacy settings."
        )
    return games


def fetch_reviews(appid):
    data = _get_json(
        f"https://store.steampowered.com/appreviews/{appid}",
        {"json": 1, "language": "all", "purchase_type": "all", "num_per_page": 0},
    )
    if not isinstance(data, dict) or not data.get("success"):
        return None
    summary = data.get("query_summary")
    if not isinstance(summary, dict):
        return None
    total = summary.get("total_reviews", 0)
    positive = summary.get("total_positive", 0)
    return {
        "total": total,
        "pct": round(positive / total * 100, 1) if total else None,
        "label": summary.get("review_score_desc", ""),
    }


def fetch_store_details(appid):
    """Genres, categories and header image from the store page.

    found=False means the store has no page for it (delisted/region-locked),
    which is different from a live page with no metadata (usually a beta).
    """
    data = _get_json(
        "https://store.steampowered.com/api/appdetails",
        {"appids": appid, "filters": "basic,genres,categories"},
    )
    if not isinstance(data, dict) or str(appid) not in data:
        return None
    entry = data[str(appid)]
    empty = {"found": False, "genres": [], "cats": [], "header_image": None}
    if not isinstance(entry, dict) or not entry.get("success"):
        return empty
    # Apps with no metadata come back as "data": [] rather than {}.
    details = entry.get("data")
    if not isinstance(details, dict):
        return {**empty, "found": True}
    return {
        "found": True,
        "genres": [g.get("description", "") for g in details.get("genres", [])],
        "cats": [c.get("description", "") for c in details.get("categories", [])],
        "header_image": details.get("header_image"),
    }


def is_non_game(name, found, genres, cats):
    if _NON_GAME_NAME.search(name):
        return True
    if any(g.lower() in SOFTWARE_GENRES for g in genres):
        return True
    # A live store page with no genres or categories at all is almost
    # always a beta/test build rather than a real release.
    return bool(found) and not genres and not cats


# ── HowLongToBeat ───────────────────────────────────────────────────────────

# Symbols and suffixes Steam adds that HLTB doesn't use
_STRIP_SYMBOLS = str.maketrans("", "", "™®©")
_SUFFIX_PATTERN = re.compile(
    r"\s*(\(\d{4}\)|\(Legacy\)|[-–]\s*Gold (Pack|Classic)"
    r"|(Game of the Year|Definitive|Enhanced|Complete|Remastered"
    r"|HD Remaster|Gold|Classic|Director's Cut)\s*(Edition)?"
    r"|:\s*(Gold|Classic|Complete))\s*$",
    re.IGNORECASE,
)


def _clean_name(name):
    """Strip symbols and Steam-specific suffixes to improve HLTB matching."""
    name = name.translate(_STRIP_SYMBOLS).strip()
    # Iteratively remove suffixes (some games stack them)
    for _ in range(3):
        cleaned = _SUFFIX_PATTERN.sub("", name).strip()
        if cleaned == name:
            break
        name = cleaned
    return name


def _hltb_search(query, threshold):
    """Search HLTB for a single query string. Returns hours or None."""
    from howlongtobeatpy import HowLongToBeat
    results = HowLongToBeat(0.0).search(query, similarity_case_sensitive=False)
    if results is None:
        # howlongtobeatpy returns None (not []) when the request itself
        # failed, which happens a lot once HLTB starts throttling us.
        raise RuntimeError("HLTB request failed")
    if not results:
        return None
    best = max(results, key=lambda r: r.similarity)
    if best.similarity >= threshold:
        return best.main_story or best.main_extra or best.completionist
    return None


def fetch_hltb_hours(name):
    """Returns (ok, hours). ok=False means the lookup itself failed (HLTB
    down, throttling, or its API shifted), so the result shouldn't be cached."""
    for attempt in range(3):
        try:
            # Pass 1: cleaned name (symbols + suffixes stripped)
            cleaned = _clean_name(name)
            hours = _hltb_search(cleaned, MIN_SIMILARITY)

            # Pass 2: if still no match, try just the first segment before " - " or ":"
            if hours is None and cleaned != name:
                short = re.split(r"[-–:]", cleaned)[0].strip()
                if short and short != cleaned:
                    hours = _hltb_search(short, MIN_SIMILARITY)
            return True, hours
        except Exception:
            time.sleep(5 * (attempt + 1))
    return False, None


# ── Scoring ─────────────────────────────────────────────────────────────────

def compute_score(review_pct, hltb_hours):
    """
    Priority = review% / (1 + log(hours + 1))

    A 95% game at 2h scores higher than a 95% game at 20h,
    but great long games still beat mediocre short ones.
    Games with unknown length get a neutral playtime factor, and anything
    under MIN_SCORED_HOURS is scored as if it were that long.
    """
    if review_pct is None:
        return 0.0
    if not hltb_hours or hltb_hours <= 0:
        return review_pct * 0.45  # slight penalty for unknown length
    return review_pct / (1 + math.log1p(max(hltb_hours, MIN_SCORED_HOURS)))


def pct_class(pct):
    if pct is None:
        return "pct-none"
    if pct >= 95:
        return "pct-top"
    if pct >= 80:
        return "pct-good"
    if pct >= 70:
        return "pct-mixed"
    if pct >= 40:
        return "pct-low"
    return "pct-bad"


def in_length(hours, length):
    _, lo, hi = LENGTHS.get(length, LENGTHS["any"])
    if lo is None and hi is None:
        return True
    if not hours:
        return False  # unknown length can't be placed in a bucket
    return (lo is None or hours >= lo) and (hi is None or hours < hi)


def ranked_games(conn, length="any", coop=False):
    """Every unplayed (or barely played), unhidden, unfiltered game with
    enough reviews, best first.

    "any" ranks by the review%/length score. A length bucket already fixes
    roughly how long you want to play, so within it games rank by review %.
    coop keeps only games two people can play together without buying a
    second copy (see coop_modes).
    """
    filters = get_filters(conn)
    rows = conn.execute(
        """
        SELECT g.appid, g.name, g.playtime_forever, r.total, r.pct, r.label,
               h.hours, d.found, d.genres, d.cats, d.header_image
        FROM games g
        JOIN reviews r ON r.appid = g.appid
        LEFT JOIN hltb h ON h.appid = g.appid
        LEFT JOIN store_details d ON d.appid = g.appid
        WHERE g.playtime_forever < ?
          AND r.total >= ?
          AND g.appid NOT IN (SELECT appid FROM hidden)
        """,
        (get_played_minutes(conn), get_min_reviews(conn)),
    ).fetchall()

    scored = []
    for row in rows:
        genres = json.loads(row["genres"]) if row["genres"] else []
        cats = json.loads(row["cats"]) if row["cats"] else []
        if row["found"] is not None and is_non_game(row["name"], row["found"], genres, cats):
            continue
        if is_filtered(cats, filters) or not in_length(row["hours"], length):
            continue
        modes = coop_modes(cats)
        if coop and not modes:
            continue
        scored.append(
            {
                "appid": row["appid"],
                "name": row["name"],
                "played_minutes": row["playtime_forever"],
                "pct": row["pct"],
                "pct_class": pct_class(row["pct"]),
                "label": row["label"],
                "total_reviews": row["total"],
                "hours": row["hours"],
                "header_image": row["header_image"],
                "score": compute_score(row["pct"], row["hours"]),
                "coop_modes": modes,
            }
        )
    if length == "any":
        scored.sort(key=lambda g: g["score"], reverse=True)
    else:
        scored.sort(key=lambda g: (g["pct"] or 0, g["score"]), reverse=True)
    return scored


# ── Refresh job ─────────────────────────────────────────────────────────────

def _progress(conn, stage, done=0, total=0, message=""):
    conn.execute(
        "UPDATE refresh_status SET stage = ?, done = ?, total = ?, message = ?, "
        "updated_at = ? WHERE id = 1",
        (stage, done, total, message, time.time()),
    )
    conn.commit()


def _safely(fetch, appid):
    """One game's odd API response shouldn't abort a 20-minute refresh --
    treat it as a failed fetch so that game is retried next time."""
    try:
        return fetch(appid)
    except Exception:
        return None


def run_refresh(db_path, full=False):
    """Pull the library and fill in any missing review/category/HLTB data.

    Returns False without doing anything if another refresh is already
    running (the web app has several workers; the status row is the lock).
    """
    conn = connect(db_path)
    now = time.time()
    claimed = conn.execute(
        "UPDATE refresh_status SET state = 'running', stage = 'Starting', done = 0, "
        "total = 0, message = '', error = NULL, started_at = ?, updated_at = ? "
        "WHERE id = 1 AND (state != 'running' OR updated_at < ?)",
        (now, now, now - STALE_AFTER),
    ).rowcount
    conn.commit()
    if not claimed:
        conn.close()
        return False

    try:
        _refresh(conn, full)
    except Exception as e:
        conn.rollback()
        state, error = "error", str(e)
    else:
        state, error = "done", None
    now = time.time()
    conn.execute(
        "UPDATE refresh_status SET state = ?, error = ?, updated_at = ?, finished_at = ? "
        "WHERE id = 1",
        (state, error, now, now),
    )
    conn.commit()
    conn.close()
    return True


def _refresh(conn, full):
    api_key = get_setting(conn, "api_key")
    steam_id = get_setting(conn, "steam_id")
    if not api_key or not steam_id:
        raise SteamError("Add your Steam API key and Steam ID in Settings first.")

    if full:
        for table in ("reviews", "store_details", "hltb"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()

    # ── 1. Library ──────────────────────────────────────────────────────
    _progress(conn, "Fetching your Steam library")
    games = get_owned_games(api_key, steam_id)
    conn.execute("DELETE FROM games")
    conn.executemany(
        "INSERT INTO games (appid, name, playtime_forever) VALUES (?, ?, ?)",
        [(g["appid"], g.get("name", ""), g.get("playtime_forever", 0)) for g in games],
    )
    conn.commit()

    played_minutes = get_played_minutes(conn)

    # ── 2. Review scores for unplayed / barely played games ─────────────
    need_reviews = conn.execute(
        """
        SELECT appid, name FROM games
        WHERE playtime_forever < ?
          AND appid NOT IN (SELECT appid FROM hidden)
          AND appid NOT IN (SELECT appid FROM reviews)
        """,
        (played_minutes,),
    ).fetchall()
    for i, g in enumerate(need_reviews):
        _progress(conn, "Fetching Steam reviews", i, len(need_reviews), g["name"])
        r = _safely(fetch_reviews, g["appid"])
        if r is not None:
            conn.execute(
                "INSERT OR REPLACE INTO reviews (appid, total, pct, label, fetched_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (g["appid"], r["total"], r["pct"], r["label"], time.time()),
            )
        time.sleep(STEAM_DELAY)

    # Everything with enough reviews gets store details + HLTB -- your
    # category filters are applied at display time, so changing them never
    # needs a refresh.
    qualified = conn.execute(
        """
        SELECT g.appid, g.name
        FROM games g
        JOIN reviews r ON r.appid = g.appid
        WHERE g.playtime_forever < ?
          AND r.total >= ?
          AND g.appid NOT IN (SELECT appid FROM hidden)
        ORDER BY r.pct DESC
        """,
        (played_minutes, get_min_reviews(conn)),
    ).fetchall()

    # ── 3. Store details (genres, categories, header art) ───────────────
    have_details = {r["appid"] for r in conn.execute("SELECT appid FROM store_details")}
    need_details = [g for g in qualified if g["appid"] not in have_details]
    for i, g in enumerate(need_details):
        _progress(conn, "Fetching Steam store details", i, len(need_details), g["name"])
        d = _safely(fetch_store_details, g["appid"])
        if d is not None:
            conn.execute(
                "INSERT OR REPLACE INTO store_details "
                "(appid, found, genres, cats, header_image, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
                (g["appid"], d["found"], json.dumps(d["genres"]), json.dumps(d["cats"]),
                 d["header_image"], time.time()),
            )
        time.sleep(STORE_DELAY)

    # ── 4. HowLongToBeat playtime (skipping tools and betas) ────────────
    details = {
        r["appid"]: r
        for r in conn.execute("SELECT appid, found, genres, cats FROM store_details")
    }

    def looks_like_game(g):
        d = details.get(g["appid"])
        if d is None:
            return not _NON_GAME_NAME.search(g["name"])
        return not is_non_game(g["name"], d["found"], json.loads(d["genres"]), json.loads(d["cats"]))

    have_hltb = {r["appid"] for r in conn.execute("SELECT appid FROM hltb")}
    need_hltb = [g for g in qualified if g["appid"] not in have_hltb and looks_like_game(g)]
    for i, g in enumerate(need_hltb):
        _progress(conn, "Fetching HowLongToBeat playtimes", i, len(need_hltb), g["name"])
        ok, hours = fetch_hltb_hours(g["name"])
        if ok:
            conn.execute(
                "INSERT OR REPLACE INTO hltb (appid, hours, fetched_at) VALUES (?, ?, ?)",
                (g["appid"], hours, time.time()),
            )
        time.sleep(HLTB_DELAY)

    conn.commit()
