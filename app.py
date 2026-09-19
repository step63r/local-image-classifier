#!/usr/bin/env python
"""Minimal Flask UI for searching WD14 tags stored by batch_tag.py."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from flask import Flask, abort, render_template, request, send_file

DB_PATH = Path(os.environ.get("TAGS_DB", Path(__file__).parent / "tags.db"))
PAGE_SIZE = 40

# batch_tag.py stores tags down to a low floor (default 0.1) so these display
# thresholds can be tuned here, at query time, without re-running inference.
DEFAULT_GENERAL_THRESHOLD = 0.35
DEFAULT_CHARACTER_THRESHOLD = 0.85

app = Flask(__name__)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def parse_query(q: str) -> list[str]:
    # Tags are separated by whitespace; a multi-word tag itself (e.g. from
    # "azur lane") is written with underscores, as on booru-style tag search
    # boxes, and normalized here to match the space form stored in the DB.
    return [t.replace("_", " ").strip() for t in q.replace(",", " ").split() if t.strip()]


def get_thresholds() -> tuple[float, float]:
    gt = request.args.get("gt", DEFAULT_GENERAL_THRESHOLD, type=float)
    ct = request.args.get("ct", DEFAULT_CHARACTER_THRESHOLD, type=float)
    return gt, ct


def like_pattern(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search_images(conn: sqlite3.Connection, tags: list[str], gt: float, ct: float, page: int):
    offset = (page - 1) * PAGE_SIZE
    threshold_clause = "(category = 'character' AND confidence >= ?) OR (category != 'character' AND confidence >= ?)"

    if not tags:
        rows = conn.execute(
            "SELECT id, path FROM images WHERE status = 'done' "
            "ORDER BY tagged_at DESC LIMIT ? OFFSET ?",
            (PAGE_SIZE, offset),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM images WHERE status = 'done'").fetchone()[0]
        return rows, total

    # Each search term must match at least one tag on the image (substring,
    # not exact match) that also clears the current display threshold.
    exists_clauses = []
    params: list = []
    for term in tags:
        exists_clauses.append(
            f"""EXISTS (
                SELECT 1 FROM tags tg
                WHERE tg.image_id = i.id AND tg.tag LIKE ? ESCAPE '\\' AND ({threshold_clause})
            )"""
        )
        params.extend([like_pattern(term), ct, gt])
    where = " AND ".join(exists_clauses)

    rows = conn.execute(
        f"""
        SELECT i.id, i.path FROM images i
        WHERE i.status = 'done' AND {where}
        ORDER BY i.tagged_at DESC
        LIMIT ? OFFSET ?
        """,
        (*params, PAGE_SIZE, offset),
    ).fetchall()

    total = conn.execute(
        f"SELECT COUNT(*) FROM images i WHERE i.status = 'done' AND {where}",
        params,
    ).fetchone()[0]
    return rows, total


@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    page = max(1, request.args.get("page", 1, type=int))
    tags = parse_query(q)
    gt, ct = get_thresholds()

    conn = get_conn()
    try:
        rows, total = search_images(conn, tags, gt, ct, page)
    finally:
        conn.close()

    return render_template(
        "index.html",
        q=q,
        gt=gt,
        ct=ct,
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
    gt, ct = get_thresholds()
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

    def passes(t: sqlite3.Row) -> bool:
        return t["confidence"] >= (ct if t["category"] == "character" else gt)

    return render_template("detail.html", image=img, tags=tags, gt=gt, ct=ct, passes=passes)


if __name__ == "__main__":
    app.run(debug=True)
