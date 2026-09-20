#!/usr/bin/env python
"""Resumable migration: local tags.db (SQLite) -> S3 (originals+thumbnails) + Postgres.

Run locally (this machine has the E: drive and tags.db). PostgreSQL on the EC2
instance only listens on localhost, and there is no SSH access (Session
Manager only), so open an SSM port-forward tunnel first:

    aws ssm start-session --target <InstanceId> `
      --document-name AWS-StartPortForwardingSession `
      --parameters '{"portNumber":["5432"],"localPortNumber":["5433"]}'

then, in another terminal:

    $env:PGPASSWORD = "<from /opt/imageapp/app.env on the instance>"
    python migrate_to_aws.py --s3-bucket <bucket-name-from-cdk-output>

Re-running the same command resumes automatically: any image_id already
present in Postgres is skipped without touching S3 again.

Incremental updates: after adding images locally, first re-run batch_tag.py
against the E: drive (it skips files already tagged), then re-run this
script the same way. Files deleted or moved locally are not detected or
cleaned up; stale rows are left in place.
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import boto3
import botocore.exceptions
import psycopg2
import psycopg2.extras
from PIL import Image
from tqdm import tqdm

PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id SERIAL PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    size BIGINT NOT NULL,
    mtime DOUBLE PRECISION NOT NULL,
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
"""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sqlite-db", default=Path("tags.db"), type=Path)
    p.add_argument("--pg-host", default="localhost")
    p.add_argument("--pg-port", type=int, default=5433, help="Default assumes an SSH tunnel (see module docstring)")
    p.add_argument("--pg-user", default="imageapp")
    p.add_argument("--pg-database", default="imagedb")
    p.add_argument("--s3-bucket", required=True)
    p.add_argument("--aws-profile", default=None)
    p.add_argument("--region", default="ap-northeast-1")
    p.add_argument("--thumb-size", type=int, default=400, help="Longest edge, pixels")
    p.add_argument("--thumb-quality", type=int, default=85)
    p.add_argument("--limit", type=int, default=None, help="Process at most N rows (for testing)")
    p.add_argument("--commit-every", type=int, default=20)
    p.add_argument("--log-dir", default=Path("logs"), type=Path)
    return p.parse_args()


def setup_logging(log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"migrate_to_aws_{datetime.now():%Y%m%d_%H%M%S}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )
    return log_path


def ensure_schema(pg_conn) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(PG_SCHEMA)
    pg_conn.commit()


def make_thumbnail(src_path: Path, max_edge: int, quality: int) -> bytes:
    # Same alpha-onto-white flattening as tagger.models.WD14Tagger.preprocess,
    # without the ONNX-specific square-padding/BGR/resize-to-model-input steps.
    with Image.open(src_path) as im:
        im = im.convert("RGBA")
        canvas = Image.new("RGBA", im.size, "WHITE")
        canvas.paste(im, mask=im)
        im = canvas.convert("RGB")
        im.thumbnail((max_edge, max_edge), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()


def s3_object_exists(s3, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except botocore.exceptions.ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise


def guess_content_type(ext: str) -> str:
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".gif": "image/gif",
    }.get(ext.lower(), "application/octet-stream")


def migrate_one(sqlite_conn, pg_conn, s3, bucket: str, row, thumb_size: int, thumb_quality: int) -> None:
    image_id = row["id"]
    src_path = Path(row["path"])
    ext = src_path.suffix.lower() or ".jpg"

    original_key = f"original/{image_id}{ext}"
    thumb_key = f"thumb/{image_id}.jpg"

    if not s3_object_exists(s3, bucket, original_key):
        s3.upload_file(
            str(src_path), bucket, original_key, ExtraArgs={"ContentType": guess_content_type(ext)}
        )

    if not s3_object_exists(s3, bucket, thumb_key):
        thumb_bytes = make_thumbnail(src_path, thumb_size, thumb_quality)
        s3.put_object(Bucket=bucket, Key=thumb_key, Body=thumb_bytes, ContentType="image/jpeg")

    tags = sqlite_conn.execute(
        "SELECT tag, category, confidence FROM tags WHERE image_id = ?", (image_id,)
    ).fetchall()

    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM images WHERE id = %s", (image_id,))
        cur.execute(
            "INSERT INTO images (id, path, size, mtime, model, status, error, tagged_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                image_id,
                row["path"],
                row["size"],
                row["mtime"],
                row["model"],
                row["status"],
                row["error"],
                row["tagged_at"],
            ),
        )
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO tags (image_id, tag, category, confidence) VALUES %s",
            [(image_id, t["tag"], t["category"], t["confidence"]) for t in tags],
        )


def main() -> None:
    args = parse_args()
    log_path = setup_logging(args.log_dir)
    logging.info("Log file: %s", log_path)

    if not args.sqlite_db.is_file():
        logging.error("SQLite DB not found: %s", args.sqlite_db)
        sys.exit(1)

    # Read-only + immutable: safe even if batch_tag.py is still running against
    # the same file in another process.
    import sqlite3

    sqlite_conn = sqlite3.connect(f"file:{args.sqlite_db}?mode=ro", uri=True)
    sqlite_conn.row_factory = sqlite3.Row

    pgpassword = os.environ.get("PGPASSWORD")
    if not pgpassword:
        logging.error("Set the PGPASSWORD environment variable before running this script.")
        sys.exit(1)

    pg_conn = psycopg2.connect(
        host=args.pg_host,
        port=args.pg_port,
        user=args.pg_user,
        password=pgpassword,
        dbname=args.pg_database,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    ensure_schema(pg_conn)

    session = boto3.Session(profile_name=args.aws_profile) if args.aws_profile else boto3.Session()
    s3 = session.client("s3", region_name=args.region)

    rows = sqlite_conn.execute(
        "SELECT id, path, size, mtime, model, status, error, tagged_at "
        "FROM images WHERE status = 'done' ORDER BY id"
    ).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    logging.info("Found %d rows to migrate (already-migrated rows will be skipped)", len(rows))

    done = skipped = errors = 0
    pending_commits = 0
    start = time.monotonic()

    try:
        with tqdm(rows, unit="img") as bar:
            for row in bar:
                bar.set_postfix(done=done, skipped=skipped, errors=errors)

                with pg_conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM images WHERE id = %s", (row["id"],))
                    already_migrated = cur.fetchone() is not None
                if already_migrated:
                    skipped += 1
                    continue

                try:
                    migrate_one(sqlite_conn, pg_conn, s3, args.s3_bucket, row, args.thumb_size, args.thumb_quality)
                except Exception as e:
                    logging.error("Failed image_id=%s path=%s: %s", row["id"], row["path"], e)
                    pg_conn.rollback()
                    errors += 1
                    continue

                done += 1
                pending_commits += 1
                if pending_commits >= args.commit_every:
                    pg_conn.commit()
                    pending_commits = 0
    except KeyboardInterrupt:
        logging.warning("Interrupted by user, committing progress so far...")
    finally:
        pg_conn.commit()
        with pg_conn.cursor() as cur:
            cur.execute("SELECT setval('images_id_seq', COALESCE((SELECT MAX(id) FROM images), 1))")
        pg_conn.commit()
        pg_conn.close()
        sqlite_conn.close()

    elapsed = time.monotonic() - start
    logging.info(
        "Finished in %.1fs: done=%d skipped=%d errors=%d (re-run the same command to resume/retry)",
        elapsed, done, skipped, errors,
    )


if __name__ == "__main__":
    main()
