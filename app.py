#!/usr/bin/env python
"""Minimal Flask UI for searching WD14 tags stored by batch_tag.py / migrate_to_aws.py.

Reads from PostgreSQL (DATABASE_URL) and serves images/thumbnails from S3
(S3_BUCKET), streamed through Flask so Basic Auth stays the single gate for
all content, including images -- see infra/README.md for the CloudFront
cache-key implications of that design.
"""
from __future__ import annotations

import os
import re
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
# ranges can be tuned here, at query time, without re-running inference.
DEFAULT_GENERAL_MIN = 0.3
DEFAULT_CHARACTER_MIN = 0.85
SENSITIVE_RATINGS = ("sensitive", "questionable", "explicit")

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


def parse_query(q: str) -> list[tuple[str, bool]]:
    # Tags are separated by whitespace/commas; a multi-word tag itself (e.g.
    # from "azur lane") is written with underscores, as on booru-style tag
    # search boxes, and normalized here to match the space form stored in
    # the DB. A "double quoted phrase" is kept intact (spaces as typed) and
    # flagged for exact match, since a substring match on a tag containing
    # spaces would also match unrelated tags that merely contain one of the
    # words.
    terms: list[tuple[str, bool]] = []
    for quoted, unquoted in re.findall(r'"([^"]*)"|(\S+)', q):
        if quoted:
            term = quoted.strip()
            if term:
                terms.append((term, True))
        else:
            for t in unquoted.replace(",", " ").split():
                if t.strip():
                    terms.append((t.replace("_", " ").strip(), False))
    return terms


def _float_arg(name: str, absent_default: float, empty_default: float) -> float:
    # Distinguishes "param not in the querystring" (fresh page load -> use
    # the friendly default) from "param present but cleared by the user"
    # (explicit request for an open-ended bound).
    if name not in request.args:
        return absent_default
    raw = request.args.get(name, "").strip()
    if raw == "":
        return empty_default
    try:
        return float(raw)
    except ValueError:
        return absent_default


def get_thresholds() -> tuple[float, float, float, float]:
    gt_min = _float_arg("gt_min", DEFAULT_GENERAL_MIN, 0.0)
    gt_max = _float_arg("gt_max", 1.0, 1.0)
    ct_min = _float_arg("ct_min", DEFAULT_CHARACTER_MIN, 0.0)
    ct_max = _float_arg("ct_max", 1.0, 1.0)
    return gt_min, gt_max, ct_min, ct_max


def get_exclude_sensitive() -> bool:
    # Defaults to on for a fresh page load. Once the form has been submitted
    # (or a link built by this app has been followed), the param is always
    # present as "1"/"0" -- see the hidden-field trick in index.html -- so an
    # explicit uncheck is distinguishable from "never set".
    if "exclude_sensitive" not in request.args:
        return True
    return request.args.get("exclude_sensitive") == "1"


def like_pattern(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search_images(
    conn: psycopg2.extensions.connection,
    tags: list[tuple[str, bool]],
    folder: str,
    gt_min: float,
    gt_max: float,
    ct_min: float,
    ct_max: float,
    exclude_sensitive: bool,
    page: int,
):
    offset = (page - 1) * PAGE_SIZE
    threshold_clause = (
        "(category = 'character' AND confidence BETWEEN %s AND %s) "
        "OR (category != 'character' AND confidence BETWEEN %s AND %s)"
    )

    conditions = ["i.status = 'done'"]
    params: list = []

    if folder:
        conditions.append("i.path LIKE %s ESCAPE '\\'")
        params.append(like_pattern(folder))

    if exclude_sensitive:
        placeholders = ", ".join(["%s"] * len(SENSITIVE_RATINGS))
        conditions.append(
            f"""NOT EXISTS (
                SELECT 1 FROM tags tg
                WHERE tg.image_id = i.id AND tg.category = 'rating' AND tg.tag IN ({placeholders})
            )"""
        )
        params.extend(SENSITIVE_RATINGS)

    # Each search term must match at least one tag on the image that also
    # falls within the current display range. Unquoted terms match by
    # substring; a "quoted phrase" requires an exact tag match instead.
    for term, exact in tags:
        tag_clause = "tg.tag = %s" if exact else "tg.tag LIKE %s ESCAPE '\\'"
        conditions.append(
            f"""EXISTS (
                SELECT 1 FROM tags tg
                WHERE tg.image_id = i.id AND {tag_clause} AND ({threshold_clause})
            )"""
        )
        params.extend([term if exact else like_pattern(term), ct_min, ct_max, gt_min, gt_max])

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


def build_url(
    page: int,
    q: str,
    folder: str,
    gt_min: float,
    gt_max: float,
    ct_min: float,
    ct_max: float,
    exclude_sensitive: bool,
) -> str:
    params = {"page": page}
    if q:
        params["q"] = q
    if folder:
        params["folder"] = folder
    params["gt_min"] = gt_min
    params["gt_max"] = gt_max
    params["ct_min"] = ct_min
    params["ct_max"] = ct_max
    # Always explicit (never omitted) so downstream requests (pagination,
    # detail back-link) don't fall back to the "absent" default -- see
    # get_exclude_sensitive().
    params["exclude_sensitive"] = "1" if exclude_sensitive else "0"
    return "/?" + urlencode(params)


@app.route("/")
@auth.login_required
def index():
    q = request.args.get("q", "").strip()
    folder = request.args.get("folder", "").strip()
    page = max(1, request.args.get("page", 1, type=int))
    tags = parse_query(q)
    gt_min, gt_max, ct_min, ct_max = get_thresholds()
    exclude_sensitive = get_exclude_sensitive()

    conn = get_conn()
    try:
        rows, total = search_images(
            conn, tags, folder, gt_min, gt_max, ct_min, ct_max, exclude_sensitive, page
        )
    finally:
        conn.close()

    has_next = page * PAGE_SIZE < total
    next_url = (
        build_url(page + 1, q, folder, gt_min, gt_max, ct_min, ct_max, exclude_sensitive)
        if has_next
        else None
    )

    context = dict(
        q=q,
        folder=folder,
        gt_min=gt_min,
        gt_max=gt_max,
        ct_min=ct_min,
        ct_max=ct_max,
        exclude_sensitive=exclude_sensitive,
        images=rows,
        total=total,
        page=page,
        has_next=has_next,
        next_url=next_url,
        defaults={
            "gt_min": DEFAULT_GENERAL_MIN,
            "gt_max": 1.0,
            "ct_min": DEFAULT_CHARACTER_MIN,
            "ct_max": 1.0,
            "exclude_sensitive": True,
        },
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
    gt_min, gt_max, ct_min, ct_max = get_thresholds()
    folder = request.args.get("folder", "").strip()
    exclude_sensitive = get_exclude_sensitive()

    def tag_url(tag: str) -> str:
        # Quoted so parse_query() treats it as an exact-match term rather
        # than a substring search -- clicking a tag should search for that
        # exact tag, not any tag containing it as a substring.
        return build_url(1, f'"{tag}"', folder, gt_min, gt_max, ct_min, ct_max, exclude_sensitive)

    back_params = {
        k: v
        for k, v in {
            "q": request.args.get("q", ""),
            "folder": request.args.get("folder", ""),
            "page": request.args.get("page", ""),
            "gt_min": request.args.get("gt_min", ""),
            "gt_max": request.args.get("gt_max", ""),
            "ct_min": request.args.get("ct_min", ""),
            "ct_max": request.args.get("ct_max", ""),
            "exclude_sensitive": request.args.get("exclude_sensitive", ""),
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
        lo, hi = (ct_min, ct_max) if t["category"] == "character" else (gt_min, gt_max)
        return lo <= t["confidence"] <= hi

    return render_template(
        "detail.html",
        image=img,
        tags=tags,
        gt_min=gt_min,
        gt_max=gt_max,
        ct_min=ct_min,
        ct_max=ct_max,
        passes=passes,
        back_url=back_url,
        tag_url=tag_url,
    )


if __name__ == "__main__":
    app.run(debug=True)
