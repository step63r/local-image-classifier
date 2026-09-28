#!/usr/bin/env python
"""Minimal Flask UI for searching WD14 tags stored by batch_tag.py / migrate_to_aws.py.

Reads from PostgreSQL (DATABASE_URL) and serves images/thumbnails from S3
(S3_BUCKET), streamed through Flask so the login session stays the single
gate for all content, including images -- see infra/README.md for the
CloudFront cache-key implications of that design.
"""
from __future__ import annotations

import hmac
import os
import re
import secrets
import time
from datetime import timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import urlencode

import boto3
import botocore.exceptions
import psycopg2
import psycopg2.extras
from flask import (
    Flask,
    Response,
    abort,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
    url_for,
)
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
app.config.update(
    # gunicorn runs multiple worker processes (no --preload), so this must be
    # a fixed value shared across them -- a per-process random key would make
    # sessions validate on whichever worker issued them and fail on the rest.
    SECRET_KEY=os.environ["SECRET_KEY"],
    SESSION_COOKIE_NAME="imgapp_session",
    SESSION_COOKIE_HTTPONLY=True,
    # Local http:// testing needs this off; CloudFront always terminates TLS
    # in production, so it defaults on.
    SESSION_COOKIE_SECURE=os.environ.get("SECURE_COOKIES", "1") != "0",
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=180),
    SESSION_REFRESH_EACH_REQUEST=False,
)
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


def _safe_next(raw: str) -> str:
    # Restrict redirect targets to same-site absolute paths to avoid an open
    # redirect through the "next" param.
    if raw and raw.startswith("/") and not raw.startswith("//"):
        return raw
    return "/"


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            next_url = request.full_path if request.query_string else request.path
            login_url = url_for("login", next=next_url)
            if request.headers.get("HX-Request"):
                # A plain redirect would just swap the grid partial for the
                # login page's HTML inside the results div -- HX-Redirect
                # tells htmx to navigate the whole browser instead.
                resp = Response(status=200)
                resp.headers["HX-Redirect"] = login_url
                return resp
            return redirect(login_url)
        return view(*args, **kwargs)

    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("authenticated"):
        return redirect(_safe_next(request.args.get("next", "/")))

    error = None
    if request.method == "POST":
        next_url = _safe_next(request.form.get("next", "/"))
        token_ok = hmac.compare_digest(
            request.form.get("csrf_token", ""), session.get("csrf_token", "")
        )
        if (
            token_ok
            and request.form.get("username") == AUTH_USERNAME
            and check_password_hash(AUTH_PASSWORD_HASH, request.form.get("password", ""))
        ):
            session.clear()
            session["authenticated"] = True
            session.permanent = True
            return redirect(next_url)
        time.sleep(1)  # cheap brute-force friction on top of the WAF rate limit
        error = "ユーザー名またはパスワードが違います"

    csrf_token = secrets.token_urlsafe(16)
    session["csrf_token"] = csrf_token
    return render_template(
        "login.html", error=error, next=request.values.get("next", "/"), csrf_token=csrf_token
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


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


def get_related_images(cur, image_id: int, limit: int = 12):
    # Only called when the source image has an embedding (see detail()) --
    # otherwise the subquery below returns NULL and `ORDER BY NULL` would
    # hand back an arbitrary, not actually related, set of rows.
    cur.execute(
        "SELECT id, path FROM images "
        "WHERE id != %s AND embedding IS NOT NULL "
        "ORDER BY embedding <=> (SELECT embedding FROM images WHERE id = %s) "
        "LIMIT %s",
        (image_id, image_id, limit),
    )
    return cur.fetchall()


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
@login_required
def index():
    q = request.args.get("q", "").strip()
    folder = request.args.get("folder", "").strip()
    page = max(1, request.args.get("page", 1, type=int))
    tags = parse_query(q)
    gt_min, gt_max, ct_min, ct_max = get_thresholds()
    exclude_sensitive = get_exclude_sensitive()
    # フォーム送信(検索実行)時は build_url/フォームの全フィールドが必ずクエリに
    # 乗るため、素の "/" アクセス(クエリなし)と確実に区別できる。
    has_searched = bool(request.args)

    if has_searched:
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
    else:
        rows, total, has_next, next_url = [], 0, False, None

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
        has_searched=has_searched,
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
@login_required
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
@login_required
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
@login_required
def detail(image_id: int):
    gt_min, gt_max, ct_min, ct_max = get_thresholds()
    folder = request.args.get("folder", "").strip()
    exclude_sensitive = get_exclude_sensitive()

    def tag_url(tag: str) -> str:
        # Quoted so parse_query() treats it as an exact-match term rather
        # than a substring search -- clicking a tag should search for that
        # exact tag, not any tag containing it as a substring.
        return build_url(1, f'"{tag}"', folder, gt_min, gt_max, ct_min, ct_max, exclude_sensitive)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, path, (embedding IS NOT NULL) AS has_embedding FROM images WHERE id = %s",
                (image_id,),
            )
            img = cur.fetchone()
            if img is None:
                abort(404)
            cur.execute(
                "SELECT tag, category, confidence FROM tags WHERE image_id = %s ORDER BY confidence DESC",
                (image_id,),
            )
            tags = cur.fetchall()
            related = get_related_images(cur, image_id) if img["has_embedding"] else []
    finally:
        conn.close()

    def passes(t) -> bool:
        lo, hi = (ct_min, ct_max) if t["category"] == "character" else (gt_min, gt_max)
        return lo <= t["confidence"] <= hi

    has_extra_tags = any(not passes(t) for t in tags)

    return render_template(
        "detail.html",
        image=img,
        tags=tags,
        related=related,
        # ヘッダーの検索フォームは詳細画面の遷移元クエリを引き継がず、常に空の状態で表示する。
        q="",
        folder="",
        gt_min=gt_min,
        gt_max=gt_max,
        ct_min=ct_min,
        ct_max=ct_max,
        exclude_sensitive=exclude_sensitive,
        passes=passes,
        has_extra_tags=has_extra_tags,
        tag_url=tag_url,
    )


if __name__ == "__main__":
    app.run(debug=True)
