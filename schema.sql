-- One SQLite file per tool, per the project convention.
-- Re-running this is safe: CREATE TABLE IF NOT EXISTS never drops data.
-- Everything except settings/hidden/filters is a cache of Steam/HLTB data
-- and can be rebuilt with a full refresh.

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,   -- api_key, steam_id, min_reviews, played_minutes
    value  TEXT NOT NULL
);

-- Your owned games, replaced wholesale on every refresh.
CREATE TABLE IF NOT EXISTS games (
    appid             INTEGER PRIMARY KEY,
    name              TEXT NOT NULL,
    playtime_forever  INTEGER NOT NULL DEFAULT 0   -- minutes
);

-- Steam review summary per game. total = 0 means "checked, no reviews".
CREATE TABLE IF NOT EXISTS reviews (
    appid       INTEGER PRIMARY KEY,
    total       INTEGER NOT NULL,
    pct         REAL,
    label       TEXT,
    fetched_at  REAL NOT NULL
);

-- Store page metadata. genres/cats are JSON lists. found = 0 means the
-- store has no page (delisted); found = 1 with empty genres and cats is
-- how betas/test builds show up.
CREATE TABLE IF NOT EXISTS store_details (
    appid         INTEGER PRIMARY KEY,
    found         INTEGER NOT NULL,
    genres        TEXT NOT NULL,
    cats          TEXT NOT NULL,
    header_image  TEXT,
    fetched_at    REAL NOT NULL
);

-- HowLongToBeat main-story hours. A row with hours NULL means "searched,
-- no confident match"; no row means not looked up yet. Either way the game
-- is ranked with the unknown-length penalty.
CREATE TABLE IF NOT EXISTS hltb (
    appid       INTEGER PRIMARY KEY,
    hours       REAL,
    fetched_at  REAL NOT NULL
);

-- Replaces ignore.txt from the CLI script. Keyed by appid, not name.
CREATE TABLE IF NOT EXISTS hidden (
    appid      INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    hidden_at  REAL NOT NULL
);

-- Replaces filters.txt: games whose categories contain any keyword
-- (case-insensitive, partial match) are left out of the ranking.
CREATE TABLE IF NOT EXISTS filters (
    id       INTEGER PRIMARY KEY,
    keyword  TEXT NOT NULL UNIQUE COLLATE NOCASE
);

-- Single-row status for the background refresh job. The web workers read
-- this to show progress; updated_at doubles as a heartbeat so a refresh
-- killed mid-run (e.g. service restart) doesn't block the next one forever.
CREATE TABLE IF NOT EXISTS refresh_status (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    state        TEXT NOT NULL DEFAULT 'idle',   -- idle | running | done | error
    stage        TEXT,
    done         INTEGER NOT NULL DEFAULT 0,
    total        INTEGER NOT NULL DEFAULT 0,
    message      TEXT,
    error        TEXT,
    started_at   REAL,
    updated_at   REAL,
    finished_at  REAL
);

INSERT OR IGNORE INTO refresh_status (id) VALUES (1);
