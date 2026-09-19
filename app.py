#!/usr/bin/env python
"""Minimal Flask UI for searching WD14 tags stored by batch_tag.py."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from flask import Flask, abort, render_template, request, send_file

DB_PATH = Path(os.environ.get("TAGS_DB", "tags.db"))
PAGE_SIZE = 40

app = Flask(__name__)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def parse_query(q: str) -> list[str]:
    return [t.strip() for t in q.replace(",", " ").split() if t.strip()]


def search_images(conn: sqlite3.Connection, tags: list[str], page: int):
    offset = (page - 1) * PAGE_SIZE

    if not tags:
        rows = conn.execute(
            "SELECT id, path FROM images WHERE status = 'done' "
            "ORDER BY tagged_at DESC LIMIT ? OFFSET ?",
            (PAGE_SIZE, offset),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM images WHERE status = 'done'").fetchone()[0]
        return rows, total

    placeholders = ",".join("?" for _ in tags)
    rows = conn.execute(
        f"""
        SELECT i.id, i.path FROM images i
        WHERE i.status = 'done' AND i.id IN (
            SELECT image_id FROM tags
            WHERE tag IN ({placeholders})
            GROUP BY image_id
            HAVING COUNT(DISTINCT tag) = ?
        )
        ORDER BY i.tagged_at DESC
        LIMIT ? OFFSET ?
        """,
        (*tags, len(tags), PAGE_SIZE, offset),
    ).fetchall()

    total = conn.execute(
        f"""
        SELECT COUNT(*) FROM (
            SELECT image_id FROM tags
            WHERE tag IN ({placeholders})
            GROUP BY image_id
            HAVING COUNT(DISTINCT tag) = ?
        )
        """,
        (*tags, len(tags)),
    ).fetchone()[0]
    return rows, total


@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    page = max(1, request.args.get("page", 1, type=int))
    tags = parse_query(q)

    conn = get_conn()
    try:
        rows, total = search_images(conn, tags, page)
    finally:
        conn.close()

    return render_template(
        "index.html",
        q=q,
        images=rows,
        total=total,
        page=page,
        has_next=page * PAGE_SIZE < total,
        has_prev=page > 1,
    )


@app.route("/image/<int:image_id>")
def image(image_id: int):
    conn = get_conn()
    try:
        row = conn.execute("SELECT path FROM images WHERE id = ?", (image_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        abort(404)
    path = Path(row["path"])
    if not path.is_file():
        abort(404)
    return send_file(path)


@app.route("/detail/<int:image_id>")
def detail(image_id: int):
    conn = get_conn()
    try:
        img = conn.execute("SELECT id, path FROM images WHERE id = ?", (image_id,)).fetchone()
        if img is None:
            abort(404)
        tags = conn.execute(
            "SELECT tag, category, confidence FROM tags WHERE image_id = ? ORDER BY confidence DESC",
            (image_id,),
        ).fetchall()
    finally:
        conn.close()
    return render_template("detail.html", image=img, tags=tags)


if __name__ == "__main__":
    app.run(debug=True)
