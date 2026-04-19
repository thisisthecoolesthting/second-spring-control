"""SQLite layer.

Schema is created on first connection. Migrations are append-only — never
drop or rewrite a column without writing a migration step here.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL CHECK (type IN ('product', 'pillar', 'article')),
    asin TEXT,                       -- only for product type
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    category TEXT,
    target_keyword TEXT,
    stage TEXT NOT NULL DEFAULT 'idea',
    score REAL DEFAULT 0,
    priority INTEGER DEFAULT 0,
    skeleton_md TEXT,                -- raw skeleton from generate.py
    draft_md TEXT,                   -- AI-drafted markdown
    edited_md TEXT,                  -- final human-edited markdown
    notes TEXT,
    metadata_json TEXT,              -- frontmatter as JSON
    error TEXT,                      -- last error if any
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_items_stage ON items(stage);
CREATE INDEX IF NOT EXISTS idx_items_type ON items(type);
CREATE INDEX IF NOT EXISTS idx_items_score ON items(score DESC);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    from_stage TEXT,
    to_stage TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'system',  -- 'system' | 'user' | 'ai'
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_item ON events(item_id);

CREATE TABLE IF NOT EXISTS ideas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'product',
    source TEXT,                     -- 'manual' | 'scout' | 'trends' | 'reddit'
    target_keyword TEXT,
    notes TEXT,
    promoted_item_id INTEGER REFERENCES items(id),
    rejected_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ai_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
    purpose TEXT NOT NULL,           -- 'draft' | 'pillar' | 'safety' | 'meta' | 'pins'
    model TEXT NOT NULL,
    system_prompt TEXT,
    user_prompt TEXT,
    response_md TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    cost_usd REAL,
    duration_ms INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | ok | error
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ai_requests_item ON ai_requests(item_id);
"""


def init_db() -> None:
    path = Path(settings.control_db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.control_db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def execute(sql: str, params: tuple | dict = ()) -> int:
    """Run a write query, return lastrowid."""
    with get_conn() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid or 0


def fetch_all(sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
    with get_conn() as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall()


def fetch_one(sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
    with get_conn() as conn:
        cur = conn.execute(sql, params)
        return cur.fetchone()
