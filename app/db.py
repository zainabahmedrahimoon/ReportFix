import sqlite3
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('student','staff','admin')),
    department  TEXT,                       -- staff only
    trust       REAL NOT NULL DEFAULT 50,   -- cached; source of truth is trust_events
    created_at  TEXT NOT NULL
);

-- one row per submission (including ones that merged into an existing issue)
CREATE TABLE IF NOT EXISTS reports (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    raw_text        TEXT NOT NULL,
    state           TEXT NOT NULL CHECK (state IN ('routed','merged','needs_clarification','rejected_spam')),
    issue_id        INTEGER REFERENCES issues(id),
    triage_json     TEXT,           -- final triage + list of code guards that fired
    triage_source   TEXT,           -- 'llm' | 'fallback'
    injection_flag  INTEGER NOT NULL DEFAULT 0,
    clarify_msg     TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issues (
    id              INTEGER PRIMARY KEY,
    title           TEXT NOT NULL,
    location        TEXT,
    department      TEXT,           -- NULL for confidential (admin-only)
    category        TEXT NOT NULL,
    base_priority   TEXT NOT NULL CHECK (base_priority IN ('critical','high','medium','low')),
    is_safety       INTEGER NOT NULL DEFAULT 0,
    is_confidential INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL CHECK (status IN ('open','in_progress','resolved','verified','rejected')),
    reason          TEXT,
    vote_round      INTEGER NOT NULL DEFAULT 1,
    reopen_count    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- who is "affected": original reporter + me-too joiners. Distinct per user.
CREATE TABLE IF NOT EXISTS issue_reporters (
    issue_id    INTEGER NOT NULL REFERENCES issues(id),
    user_id     INTEGER NOT NULL REFERENCES users(id),
    report_id   INTEGER REFERENCES reports(id),
    kind        TEXT NOT NULL CHECK (kind IN ('original','me_too')),
    created_at  TEXT NOT NULL,
    PRIMARY KEY (issue_id, user_id)
);

CREATE TABLE IF NOT EXISTS votes (
    id          INTEGER PRIMARY KEY,
    issue_id    INTEGER NOT NULL REFERENCES issues(id),
    user_id     INTEGER NOT NULL REFERENCES users(id),
    round       INTEGER NOT NULL,
    vote        TEXT NOT NULL CHECK (vote IN ('confirm','dispute')),
    created_at  TEXT NOT NULL,
    UNIQUE (issue_id, user_id, round)
);

CREATE TABLE IF NOT EXISTS trust_events (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    delta       REAL NOT NULL,
    reason      TEXT NOT NULL,
    issue_id    INTEGER,
    event_key   TEXT UNIQUE,        -- idempotency: same award never applied twice
    trust_after REAL NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS status_log (
    id          INTEGER PRIMARY KEY,
    issue_id    INTEGER NOT NULL REFERENCES issues(id),
    actor_id    INTEGER,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS flags (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,      -- 'injection' | 'brigading' | 'spam'
    issue_id    INTEGER,
    report_id   INTEGER,
    detail      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_issues_loc ON issues(location, status);
CREATE INDEX IF NOT EXISTS idx_issues_dept ON issues(department, status);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def drop_all(conn: sqlite3.Connection) -> None:
    for t in ["flags", "status_log", "trust_events", "votes", "issue_reporters", "reports", "issues", "users"]:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()
