#!/usr/bin/env python
"""Minimal Flask UI for searching WD14 tags stored by batch_tag.py / migrate_to_aws.py.

Reads from PostgreSQL (DATABASE_URL) and serves images/thumbnails from S3
(S3_BUCKET), streamed through Flask so Basic Auth stays the single gate for
all content, including images -- see infra/README.md for the CloudFront
cache-key implications of that design.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlencode

import boto3
import botocore.exceptions
import psycopg2
import psycopg2.extras
from flask import Flask, Response, abort, render_template, request, stream_with_context
from flask_httpauth import HTTPBasicAuth
from werkzeug.security import check_password_hash, generate_password_hash

DATABASE_URL = os.environ["DATABASE_URL"]
S3_BUCKET = os.environ["S3_BUCKET"]
PAGE_SIZE = 40

# batch_tag.py stores tags down to a low floor (default 0.1) so these display
# thresholds can be tuned here, at query time, without re-running inference.
DEFAULT_GENERAL_THRESHOLD = 0.35
DEFAULT_CHARACTER_THRESHOLD = 0.85

app = Flask(__name__)
auth = HTTPBasicAuth()
s3 = boto3.client("s3")  # picks up creds from the EC2 instance role automatically

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


def get_conn() -> psycopg2.extensions.connection:
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True  # app.py is read-only; avoids idle-in-transaction connections
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
    conn: psycopg2.extensions.connection, tags: list[str], folder: str, gt: float, ct: float, page: int
):
    offset = (page - 1) * PAGE_SIZE
    threshold_clause = "(category = 'character' AND confidence >= %s) OR (category != 'character' AND confidence >= %s)"

    conditions = ["i.status = 'done'"]
    params: list = []

    if folder:
        conditions.append("i.path LIKE %s ESCAPE '\\'")
        params.append(like_pattern(folder))

    # Each search term must match at least one tag on the image (substring,
    # not exact match) that also clears the current display threshold.
    for term in tags:
        conditions.append(
            f"""EXISTS (
                SELECT 1 FROM tags tg
                WHERE tg.image_id = i.id AND tg.tag LIKE %s ESCAPE '\\' AND ({threshold_clause})
            )"""
        )
        params.extend([like_pattern(term), ct, gt])

    where = " AND ".join(conditions)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT i.id, i.path FROM images i
            WHERE {where}
            ORDER BY i.tagged_at DESC
            LIMIT %s OFFSET %s
            """,
            (*params, PAGE_SIZE, offset),
        )
        rows = cur.fetchall()

        cur.execute(f"SELECT COUNT(*) AS count FROM images i WHERE {where}", params)
        total = cur.fetchone()["count"]

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


def _stream_s3(key: str, fallback_content_type: str) -> Response:
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
    except botocore.exceptions.ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            abort(404)
        raise
    return Response(
        stream_with_context(obj["Body"].iter_chunks(chunk_size=65536)),
        content_type=obj.get("ContentType") or fallback_content_type,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "Content-Length": str(obj["ContentLength"]),
        },
    )


@app.route("/image/<int:image_id>")
@auth.login_required
def image(image_id: int):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT path FROM images WHERE id = %s", (image_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        abort(404)
    ext = Path(row["path"]).suffix.lower() or ".jpg"
    return _stream_s3(f"original/{image_id}{ext}", "application/octet-stream")


@app.route("/thumb/<int:image_id>")
@auth.login_required
def thumb(image_id: int):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM images WHERE id = %s", (image_id,))
            found = cur.fetchone() is not None
    finally:
        conn.close()
    if not found:
        abort(404)
    return _stream_s3(f"thumb/{image_id}.jpg", "image/jpeg")


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
        with conn.cursor() as cur:
            cur.execute("SELECT id, path FROM images WHERE id = %s", (image_id,))
            img = cur.fetchone()
            if img is None:
                abort(404)
            cur.execute(
                "SELECT tag, category, confidence FROM tags WHERE image_id = %s ORDER BY confidence DESC",
                (image_id,),
            )
            tags = cur.fetchall()
    finally:
        conn.close()

    def passes(t) -> bool:
        return t["confidence"] >= (ct if t["category"] == "character" else gt)

    return render_template(
        "detail.html", image=img, tags=tags, gt=gt, ct=ct, passes=passes, back_url=back_url
    )


if __name__ == "__main__":
    app.run(debug=True)
