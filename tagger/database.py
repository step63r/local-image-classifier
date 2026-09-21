from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np

from tagger.models import Tag

SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    tagged_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tags (
    image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    category TEXT NOT NULL,
    confidence REAL NOT NULL,
    PRIMARY KEY (image_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);
CREATE INDEX IF NOT EXISTS idx_images_status ON images(status);

-- One row per image, holding the pooled-feature embedding used for
-- similarity search (see tagger.models.EMBEDDING_TAPS). Kept separate from
-- `images` rather than as an extra column there since not every model
-- produces one, and it's write-once/read-rarely unlike the tagging metadata.
CREATE TABLE IF NOT EXISTS embeddings (
    image_id INTEGER PRIMARY KEY REFERENCES images(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def already_done(conn: sqlite3.Connection, path: str, size: int, mtime: float, model: str) -> bool:
    row = conn.execute(
        "SELECT size, mtime, model, status FROM images WHERE path = ?", (path,)
    ).fetchone()
    if row is None:
        return False
    db_size, db_mtime, db_model, status = row
    return status == "done" and db_size == size and db_mtime == mtime and db_model == model


def _replace_image_row(conn: sqlite3.Connection, path: str) -> None:
    # Relies on ON DELETE CASCADE to drop any previously stored tags too.
    conn.execute("DELETE FROM images WHERE path = ?", (path,))


def save_result(
    conn: sqlite3.Connection,
    path: str,
    size: int,
    mtime: float,
    model: str,
    tags: list[Tag],
    tagged_at: str,
) -> int:
    _replace_image_row(conn, path)
    cur = conn.execute(
        "INSERT INTO images (path, size, mtime, model, status, error, tagged_at) "
        "VALUES (?, ?, ?, ?, 'done', NULL, ?)",
        (path, size, mtime, model, tagged_at),
    )
    image_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO tags (image_id, tag, category, confidence) VALUES (?, ?, ?, ?)",
        [(image_id, t.name, t.category, t.confidence) for t in tags],
    )
    return image_id


def save_embedding(conn: sqlite3.Connection, image_id: int, model: str, embedding: np.ndarray) -> None:
    vector = embedding.astype(np.float32)
    conn.execute(
        "INSERT OR REPLACE INTO embeddings (image_id, model, dim, vector) VALUES (?, ?, ?, ?)",
        (image_id, model, vector.shape[0], vector.tobytes()),
    )


def save_error(
    conn: sqlite3.Connection,
    path: str,
    size: int,
    mtime: float,
    model: str,
    error: str,
    tagged_at: str,
) -> None:
    _replace_image_row(conn, path)
    conn.execute(
        "INSERT INTO images (path, size, mtime, model, status, error, tagged_at) "
        "VALUES (?, ?, ?, ?, 'error', ?, ?)",
        (path, size, mtime, model, error, tagged_at),
    )
