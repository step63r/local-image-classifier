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
script the same way. Videos/GIFs go through batch_tag_video.py instead and
are migrated by this same script (use --media video to restrict to them). Files deleted or moved locally are not detected or
cleaned up; stale rows are left in place. Images deleted from the search UI
are recorded in deleted_images and are skipped here.
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
import numpy as np
import psycopg2
import psycopg2.extras
from pgvector.psycopg2 import register_vector
from boto3.s3.transfer import TransferConfig
from PIL import Image
from tqdm import tqdm

# Videos reach >1GB each; a larger part size keeps the request count (and the
# retry granularity on a home uplink) reasonable. Stills stay single-part.
UPLOAD_CONFIG = TransferConfig(multipart_threshold=64 * 1024 * 1024, multipart_chunksize=32 * 1024 * 1024)

PG_SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;

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

-- ALTER (rather than inlining into the CREATE TABLE above) so this is the
-- one code path that's correct both for a fresh deploy and for the
-- already-populated prod table -- see push_embeddings.py for backfilling
-- rows migrated before this column existed. Deliberately no vector index
-- here: ensure_schema() runs on every invocation of this script, including
-- routine --limit smoke tests, and an HNSW build is expensive DDL that must
-- never fire from a routine, frequently re-run code path (see infra/README.md).
ALTER TABLE images ADD COLUMN IF NOT EXISTS embedding vector(1024);

-- Videos/GIFs are rows of `images` too (see batch_tag_video.py). The constant
-- DEFAULT keeps this a metadata-only change on the already-populated table.
ALTER TABLE images ADD COLUMN IF NOT EXISTS media_type TEXT NOT NULL DEFAULT 'image';
ALTER TABLE images ADD COLUMN IF NOT EXISTS duration_sec DOUBLE PRECISION;
ALTER TABLE images ADD COLUMN IF NOT EXISTS width INTEGER;
ALTER TABLE images ADD COLUMN IF NOT EXISTS height INTEGER;

CREATE TABLE IF NOT EXISTS tags (
    image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    category TEXT NOT NULL,
    confidence REAL NOT NULL,
    PRIMARY KEY (image_id, tag)
);

-- Paths deleted from the search UI. The local tags.db still has these rows, so
-- without this list the next incremental run would migrate them back.
CREATE TABLE IF NOT EXISTS deleted_images (
    path TEXT PRIMARY KEY,
    deleted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);
CREATE INDEX IF NOT EXISTS idx_images_status ON images(status);

-- No user_id: single global user, same as every other table here. The
-- UNIQUE constraint doubles as the "is this exact search already saved"
-- lookup key, so no separate hash/key column is needed.
-- media/dur_* were added to the key after the table first shipped. They are
-- deliberately not an ALTER: the old UNIQUE constraint (auto-named, possibly
-- truncated) would have to be dropped and recreated, which can't be written
-- idempotently here. The table was dropped and recreated instead, so this
-- CREATE is the only definition. NULLS NOT DISTINCT (PG15+) is required so that
-- two searches with no duration bound still collide on the key.
CREATE TABLE IF NOT EXISTS saved_searches (
    id SERIAL PRIMARY KEY,
    q TEXT NOT NULL DEFAULT '',
    folder TEXT NOT NULL DEFAULT '',
    gt_min DOUBLE PRECISION NOT NULL,
    gt_max DOUBLE PRECISION NOT NULL,
    ct_min DOUBLE PRECISION NOT NULL,
    ct_max DOUBLE PRECISION NOT NULL,
    exclude_sensitive BOOLEAN NOT NULL,
    media TEXT NOT NULL DEFAULT '',
    dur_min DOUBLE PRECISION,
    dur_max DOUBLE PRECISION,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (
        q, folder, gt_min, gt_max, ct_min, ct_max, exclude_sensitive, media, dur_min, dur_max
    )
);
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
    p.add_argument(
        "--media",
        choices=("all", "image", "video"),
        default="all",
        help="Restrict to one media type. Applied before --limit, so `--media video --limit 5` "
        "smoke-tests the first 5 videos even when all images are already migrated.",
    )
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


def _encode_jpeg(im: Image.Image, max_edge: int, quality: int) -> bytes:
    im.thumbnail((max_edge, max_edge), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def make_thumbnail(src_path: Path, max_edge: int, quality: int) -> bytes:
    # Same alpha-onto-white flattening as tagger.models.WD14Tagger.preprocess,
    # without the ONNX-specific square-padding/BGR/resize-to-model-input steps.
    with Image.open(src_path) as im:
        im = im.convert("RGBA")
        canvas = Image.new("RGBA", im.size, "WHITE")
        canvas.paste(im, mask=im)
        return _encode_jpeg(canvas.convert("RGB"), max_edge, quality)


def make_video_thumbnail(src_path: Path, max_edge: int, quality: int) -> bytes:
    # Imported here so the image-only path doesn't pull in tagger.video (and
    # through it onnxruntime) just to migrate stills.
    from tagger import video

    cap, info = video.open_video(str(src_path))
    try:
        frame = video.middle_frame(cap, info)
    finally:
        cap.release()
    if frame is None:
        raise ValueError("Could not read a frame for the thumbnail")
    return _encode_jpeg(frame, max_edge, quality)


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
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
    }.get(ext.lower(), "application/octet-stream")


def migrate_one(sqlite_conn, pg_conn, s3, bucket: str, row, thumb_size: int, thumb_quality: int) -> None:
    image_id = row["id"]
    src_path = Path(row["path"])
    ext = src_path.suffix.lower() or ".jpg"

    original_key = f"original/{image_id}{ext}"
    thumb_key = f"thumb/{image_id}.jpg"

    if not s3_object_exists(s3, bucket, original_key):
        s3.upload_file(
            str(src_path),
            bucket,
            original_key,
            ExtraArgs={"ContentType": guess_content_type(ext)},
            Config=UPLOAD_CONFIG,
        )

    if not s3_object_exists(s3, bucket, thumb_key):
        make = make_video_thumbnail if row["media_type"] == "video" else make_thumbnail
        thumb_bytes = make(src_path, thumb_size, thumb_quality)
        s3.put_object(Bucket=bucket, Key=thumb_key, Body=thumb_bytes, ContentType="image/jpeg")

    tags = sqlite_conn.execute(
        "SELECT tag, category, confidence FROM tags WHERE image_id = ?", (image_id,)
    ).fetchall()
    embedding_row = sqlite_conn.execute(
        "SELECT vector FROM embeddings WHERE image_id = ?", (image_id,)
    ).fetchone()
    embedding = np.frombuffer(embedding_row["vector"], dtype=np.float32) if embedding_row is not None else None

    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM images WHERE id = %s", (image_id,))
        cur.execute(
            "INSERT INTO images (id, path, size, mtime, model, status, error, tagged_at, embedding, "
            "media_type, duration_sec, width, height) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                image_id,
                row["path"],
                row["size"],
                row["mtime"],
                row["model"],
                row["status"],
                row["error"],
                row["tagged_at"],
                embedding,
                row["media_type"],
                row["duration_sec"],
                row["width"],
                row["height"],
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
    register_vector(pg_conn)  # after ensure_schema(): needs the vector extension/type to already exist

    session = boto3.Session(profile_name=args.aws_profile) if args.aws_profile else boto3.Session()
    s3 = session.client("s3", region_name=args.region)

    media_filter = "" if args.media == "all" else f"AND media_type = '{args.media}' "
    rows = sqlite_conn.execute(
        "SELECT id, path, size, mtime, model, status, error, tagged_at, "
        "media_type, duration_sec, width, height "
        f"FROM images WHERE status = 'done' {media_filter}ORDER BY id"
    ).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    logging.info("Found %d rows to migrate (already-migrated rows will be skipped)", len(rows))

    # Fetched once up front instead of one SELECT per row: on a re-run where
    # most/all rows are already migrated, checking membership in this set is
    # a single round trip total instead of one per row (the latter took
    # 15-30+ minutes of pure tunnel round-trip latency on ~44k rows with
    # nothing new to do).
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM images")
        migrated_ids = {r["id"] for r in cur.fetchall()}
    logging.info("%d rows already present in Postgres", len(migrated_ids))

    with pg_conn.cursor() as cur:
        cur.execute("SELECT path FROM deleted_images")
        deleted_paths = {r["path"] for r in cur.fetchall()}
    logging.info("%d paths excluded (deleted from the search UI)", len(deleted_paths))

    done = skipped = errors = 0
    pending_commits = 0
    start = time.monotonic()

    try:
        with tqdm(rows, unit="img") as bar:
            for row in bar:
                bar.set_postfix(done=done, skipped=skipped, errors=errors)

                if row["id"] in migrated_ids or row["path"] in deleted_paths:
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
