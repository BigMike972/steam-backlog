import os
import random
import re
import sqlite3
import threading
import time
from pathlib import Path

import click
from flask import Flask, g, redirect, render_template, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

import backlog

# Next to app.py by default; the Docker image points this at a volume.
DB_PATH = Path(os.environ.get("STEAM_BACKLOG_DB") or Path(__file__).parent / "steam_backlog.db")
SCHEMA_PATH = Path(__file__).parent / "schema.sql"

app = Flask(__name__)
# Behind nginx at /steam-backlog/ (see README) -- ProxyFix reads the
# X-Forwarded-Prefix header nginx sets so url_for() generates links with
# the right prefix instead of pointing at the domain root.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_prefix=1)


def get_db():
    if "db" not in g:
        g.db = backlog.connect(DB_PATH)
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create tables if they don't exist. Safe to run repeatedly."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Every gunicorn worker runs this at startup. On a brand-new database
    # two of them can collide switching it to WAL, which fails straight away
    # with "database is locked" rather than waiting -- so retry briefly.
    for attempt in range(20):
        try:
            _init_db()
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or attempt == 19:
                raise
            time.sleep(0.25)


def _init_db():
    db = sqlite3.connect(DB_PATH, timeout=30)
    # WAL so page loads aren't blocked while a refresh is writing.
    db.execute("PRAGMA journal_mode=WAL")
    with open(SCHEMA_PATH) as f:
        db.executescript(f.read())
    # Superseded by store_details (it was only a cache, nothing to migrate).
    db.execute("DROP TABLE IF EXISTS categories")
    db.commit()
    db.close()


# No seed data here (unlike household-budget), so it's safe to just make
# sure the schema exists on every startup.
init_db()


@app.cli.command("init-db")
def init_db_command():
    """flask --app app init-db -- creates tables."""
    init_db()
    print(f"Initialized database at {DB_PATH}")


@app.cli.command("refresh")
@click.option("--full", is_flag=True, help="Discard cached reviews/store details/HLTB and re-fetch all of it.")
def refresh_command(full):
    """flask --app app refresh -- same as the Refresh button, in the foreground."""
    if not backlog.run_refresh(DB_PATH, full=full):
        print("A refresh is already running.")
        return
    status = get_db().execute("SELECT state, error FROM refresh_status").fetchone()
    print(status["error"] or "Refresh complete.")


def format_time(ts):
    return time.strftime("%b %-d, %-I:%M %p", time.localtime(ts)) if ts else None


# Opening the page kicks off a refresh if the last one is older than this.
# When nothing's new that's a single Steam API call, so it's nearly free.
AUTO_REFRESH_AFTER = 24 * 60 * 60


def start_refresh(full=False):
    # Runs in a background thread; run_refresh claims the status row first,
    # so a second click (or the other gunicorn worker) is a no-op.
    threading.Thread(
        target=backlog.run_refresh, args=(DB_PATH,), kwargs={"full": full}, daemon=True
    ).start()
    time.sleep(0.3)  # let it claim the row so the page shows "running"


def load_status(db):
    status = dict(db.execute("SELECT * FROM refresh_status WHERE id = 1").fetchone())
    status["running"] = status["state"] == "running" and (
        time.time() - (status["updated_at"] or 0) < backlog.STALE_AFTER
    )
    status["finished"] = format_time(status["finished_at"])
    return status


def parse_top(value, default=25):
    try:
        return max(1, min(int(value), 500))
    except (TypeError, ValueError):
        return default


@app.route("/")
def index():
    db = get_db()
    has_credentials = bool(
        backlog.get_setting(db, "api_key") and backlog.get_setting(db, "steam_id")
    )

    status = load_status(db)
    stale = time.time() - (status["finished_at"] or 0) > AUTO_REFRESH_AFTER
    if has_credentials and not status["running"] and stale:
        start_refresh()
        status = load_status(db)

    top_n = parse_top(request.args.get("top"))
    length = request.args.get("length")
    if length not in backlog.LENGTHS:
        length = "any"
    coop = request.args.get("coop") == "1"
    ranked = backlog.ranked_games(db, length, coop)
    top = ranked[:top_n]

    # "Pick one for me": weighted by priority score, so better picks come
    # up more often but the whole shortlist stays in play.
    pick = None
    if request.args.get("pick") and top:
        pick = random.choices(top, weights=[max(t["score"], 0.1) for t in top])[0]

    played_minutes = backlog.get_played_minutes(db)
    stats = db.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN playtime_forever < ? THEN 1 ELSE 0 END) AS unplayed FROM games",
        (played_minutes,),
    ).fetchone()
    api_key = backlog.get_setting(db, "api_key") or ""

    return render_template(
        "index.html",
        has_credentials=has_credentials,
        status=status,
        top=top,
        top_n=top_n,
        length=length,
        lengths=backlog.LENGTHS,
        coop=coop,
        played_minutes=played_minutes,
        ranked_count=len(ranked),
        pick=pick,
        total_games=stats["total"] or 0,
        unplayed=stats["unplayed"] or 0,
        hidden=db.execute("SELECT * FROM hidden ORDER BY name COLLATE NOCASE").fetchall(),
        filters=db.execute("SELECT * FROM filters ORDER BY keyword COLLATE NOCASE").fetchall(),
        steam_id=backlog.get_setting(db, "steam_id") or "",
        api_key_hint=f"…{api_key[-4:]}" if api_key else "",
        min_reviews=backlog.get_min_reviews(db),
        error=request.args.get("error"),
    )


@app.route("/settings", methods=["POST"])
def save_settings():
    db = get_db()
    api_key = request.form.get("api_key", "").strip()
    steam_id = request.form.get("steam_id", "").strip()
    min_reviews = request.form.get("min_reviews", "").strip()
    played_minutes = request.form.get("played_minutes", "").strip()

    errors = []
    # A blank API key field means "keep the saved one" -- the page never
    # echoes the key back, so the field is always empty on load.
    if api_key:
        if re.fullmatch(r"[0-9A-Fa-f]{32}", api_key):
            backlog.set_setting(db, "api_key", api_key.upper())
        else:
            errors.append("That API key doesn't look right (it should be 32 letters/digits).")
    if steam_id:
        if re.fullmatch(r"\d{17}", steam_id):
            backlog.set_setting(db, "steam_id", steam_id)
        else:
            errors.append("Steam ID should be the 17-digit number, e.g. 7656119xxxxxxxxxx.")
    if min_reviews.isdigit():
        backlog.set_setting(db, "min_reviews", min_reviews)
    if played_minutes.isdigit():
        # Stored as minutes-played-below-which-it-counts-as-unplayed; 0 would
        # hide everything, so the floor is 1 (i.e. "never launched").
        backlog.set_setting(db, "played_minutes", str(max(int(played_minutes), 1)))
    db.commit()
    return redirect(url_for("index", error=" ".join(errors) or None))


@app.route("/refresh", methods=["POST"])
def refresh():
    start_refresh(full=request.form.get("full") == "1")
    return redirect(url_for("index"))


@app.route("/games/<int:appid>/hide", methods=["POST"])
def hide_game(appid):
    db = get_db()
    row = db.execute("SELECT name FROM games WHERE appid = ?", (appid,)).fetchone()
    if row:
        db.execute(
            "INSERT OR IGNORE INTO hidden (appid, name, hidden_at) VALUES (?, ?, ?)",
            (appid, row["name"], time.time()),
        )
        db.commit()
    return redirect(
        url_for(
            "index",
            top=request.form.get("top"),
            length=request.form.get("length"),
            coop=request.form.get("coop") or None,
        )
    )


@app.route("/games/<int:appid>/unhide", methods=["POST"])
def unhide_game(appid):
    db = get_db()
    db.execute("DELETE FROM hidden WHERE appid = ?", (appid,))
    db.commit()
    return redirect(url_for("index"))


@app.route("/filters", methods=["POST"])
def add_filter():
    keyword = request.form.get("keyword", "").strip()
    if keyword:
        db = get_db()
        db.execute("INSERT OR IGNORE INTO filters (keyword) VALUES (?)", (keyword,))
        db.commit()
    return redirect(url_for("index"))


@app.route("/filters/<int:filter_id>/delete", methods=["POST"])
def delete_filter(filter_id):
    db = get_db()
    db.execute("DELETE FROM filters WHERE id = ?", (filter_id,))
    db.commit()
    return redirect(url_for("index"))


if __name__ == "__main__":
    # Dev server only -- production runs under gunicorn (see README.md).
    app.run(host="127.0.0.1", port=5002, debug=True)
