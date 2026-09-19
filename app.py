#!/usr/bin/env python
"""Minimal Flask UI for searching WD14 tags stored by batch_tag.py."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from urllib.parse import urlencode

from flask import Flask, abort, render_template, request, send_file
from flask_httpauth import HTTPBasicAuth
from werkzeug.security import check_password_hash, generate_password_hash

DB_PATH = Path(os.environ.get("TAGS_DB", Path(__file__).parent / "tags.db"))
PAGE_SIZE = 40

# batch_tag.py stores tags down to a low floor (default 0.1) so these display
# thresholds can be tuned here, at query time, without re-running inference.
DEFAULT_GENERAL_THRESHOLD = 0.35
DEFAULT_CHARACTER_THRESHOLD = 0.85

app = Flask(__name__)
auth = HTTPBasicAuth()

AUTH_USERNAME = os.environ.get("AUTH_USERNAME", "admin")
_default_password_hash = generate_password_hash(os.environ.get("AUTH_PASSWORD", "changeme"))
AUTH_PASSWORD_HASH = os.environ.get("AUTH_PASSWORD_HASH", _default_password_hash)

if "AUTH_USERNAME" not in os.environ or (
    "AUTH_PASSWORD" not in os.environ and "AUTH_PASSWORD_HASH" not in os.environ
):
    print(
        "WARNING: using default auth credentials (admin/changeme). "
        "Set AUTH_USERNAME and AUTH_PASSWORD (or AUTH_PASSWORD_HASH) before deploying.",
    )


@auth.verify_password
def verify_password(username: str, password: str) -> str | None:
    if username == AUTH_USERNAME and check_password_hash(AUTH_PASSWORD_HASH, password):
        return username
    return None


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


def search_images(
    conn: sqlite3.Connection, tags: list[str], folder: str, gt: float, ct: float, page: int
):
    offset = (page - 1) * PAGE_SIZE
    threshold_clause = "(category = 'character' AND confidence >= ?) OR (category != 'character' AND confidence >= ?)"

    conditions = ["i.status = 'done'"]
    params: list = []

    if folder:
        conditions.append("i.path LIKE ? ESCAPE '\\'")
        params.append(like_pattern(folder))

    # Each search term must match at least one tag on the image (substring,
    # not exact match) that also clears the current display threshold.
    for term in tags:
        conditions.append(
            f"""EXISTS (
                SELECT 1 FROM tags tg
                WHERE tg.image_id = i.id AND tg.tag LIKE ? ESCAPE '\\' AND ({threshold_clause})
            )"""
        )
        params.extend([like_pattern(term), ct, gt])

    where = " AND ".join(conditions)

    rows = conn.execute(
        f"""
        SELECT i.id, i.path FROM images i
        WHERE {where}
        ORDER BY i.tagged_at DESC
        LIMIT ? OFFSET ?
        """,
        (*params, PAGE_SIZE, offset),
    ).fetchall()

    total = conn.execute(
        f"SELECT COUNT(*) FROM images i WHERE {where}",
        params,
    ).fetchone()[0]
    return rows, total


def build_url(page: int, q: str, folder: str, gt: float, ct: float) -> str:
    params = {"page": page}
    if q:
        params["q"] = q
    if folder:
        params["folder"] = folder
    params["gt"] = gt
    params["ct"] = ct
    return "/?" + urlencode(params)


@app.route("/")
@auth.login_required
def index():
    q = request.args.get("q", "").strip()
    folder = request.args.get("folder", "").strip()
    page = max(1, request.args.get("page", 1, type=int))
    tags = parse_query(q)
    gt, ct = get_thresholds()

    conn = get_conn()
    try:
        rows, total = search_images(conn, tags, folder, gt, ct, page)
    finally:
        conn.close()

    has_next = page * PAGE_SIZE < total
    next_url = build_url(page + 1, q, folder, gt, ct) if has_next else None

    context = dict(
        q=q,
        folder=folder,
        gt=gt,
        ct=ct,
        images=rows,
        total=total,
        page=page,
        has_next=has_next,
        next_url=next_url,
    )

    if request.headers.get("HX-Request"):
        return render_template("_grid.html", **context)
    return render_template("index.html", **context)


@app.route("/image/<int:image_id>")
@auth.login_required
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
@auth.login_required
def detail(image_id: int):
    gt, ct = get_thresholds()
    back_params = {
        k: v
        for k, v in {
            "q": request.args.get("q", ""),
            "folder": request.args.get("folder", ""),
            "page": request.args.get("page", ""),
            "gt": request.args.get("gt", ""),
            "ct": request.args.get("ct", ""),
        }.items()
        if v
    }
    back_url = "/?" + urlencode(back_params) if back_params else "/"

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

    return render_template(
        "detail.html", image=img, tags=tags, gt=gt, ct=ct, passes=passes, back_url=back_url
    )


if __name__ == "__main__":
    app.run(debug=True)
