#!/usr/bin/env python
"""One-time push: copy locally-backfilled embeddings into Postgres for images
that were migrated before the `embedding` column existed.

Run locally, after backfill_embeddings.py has populated tags.db's
`embeddings` table, and after migrate_to_aws.py has applied the schema
change (this script errors out early if the `embedding` column is missing).
PostgreSQL on the EC2 instance only listens on localhost, so open an SSM
port-forward tunnel first (see migrate_to_aws.py's docstring for the exact
command), then:

    $env:PGPASSWORD = "<from /opt/imageapp/app.env on the instance>"
    python push_embeddings.py

Re-running the same command resumes automatically: only images present in
the local `embeddings` table AND still NULL in Postgres are updated. This
only ever runs a single-column UPDATE (no DELETE/INSERT), so it's safe to
run while the app is serving live traffic. New images tagged and migrated
after this feature shipped don't need this script -- migrate_to_aws.py
already carries their embedding over in the same pass.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import psycopg2
import psycopg2.extras
from pgvector.psycopg2 import register_vector
from tqdm import tqdm


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sqlite-db", default=Path("tags.db"), type=Path)
    p.add_argument("--pg-host", default="localhost")
    p.add_argument("--pg-port", type=int, default=5433, help="Default assumes an SSH tunnel (see migrate_to_aws.py)")
    p.add_argument("--pg-user", default="imageapp")
    p.add_argument("--pg-database", default="imagedb")
    p.add_argument("--limit", type=int, default=None, help="Process at most N rows (for testing)")
    p.add_argument("--commit-every", type=int, default=20)
    p.add_argument("--log-dir", default=Path("logs"), type=Path)
    return p.parse_args()


def setup_logging(log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"push_embeddings_{datetime.now():%Y%m%d_%H%M%S}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )
    return log_path


def main() -> None:
    args = parse_args()
    log_path = setup_logging(args.log_dir)
    logging.info("Log file: %s", log_path)

    if not args.sqlite_db.is_file():
        logging.error("SQLite DB not found: %s", args.sqlite_db)
        sys.exit(1)

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

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name='images' AND column_name='embedding'"
        )
        if cur.fetchone() is None:
            logging.error(
                "images.embedding column does not exist yet -- run migrate_to_aws.py first "
                "to apply the schema change."
            )
            sys.exit(1)

    register_vector(pg_conn)

    local_embeddings = {
        r["image_id"]: r["vector"]
        for r in sqlite_conn.execute("SELECT image_id, vector FROM embeddings").fetchall()
    }
    logging.info("%d embeddings available locally", len(local_embeddings))

    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM images WHERE embedding IS NULL")
        pending_ids = {r["id"] for r in cur.fetchall()}
    logging.info("%d rows in Postgres still missing an embedding", len(pending_ids))

    work_ids = sorted(set(local_embeddings) & pending_ids)
    if args.limit:
        work_ids = work_ids[: args.limit]
    logging.info("%d rows to push", len(work_ids))

    done = errors = 0
    pending_commits = 0
    start = time.monotonic()

    try:
        with tqdm(work_ids, unit="img") as bar:
            for image_id in bar:
                bar.set_postfix(done=done, errors=errors)
                try:
                    embedding = np.frombuffer(local_embeddings[image_id], dtype=np.float32)
                    with pg_conn.cursor() as cur:
                        cur.execute(
                            "UPDATE images SET embedding = %s WHERE id = %s", (embedding, image_id)
                        )
                except Exception as e:
                    logging.error("Failed image_id=%s: %s", image_id, e)
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
        pg_conn.close()
        sqlite_conn.close()

    elapsed = time.monotonic() - start
    logging.info(
        "Finished in %.1fs: done=%d errors=%d (re-run the same command to resume/retry)",
        elapsed, done, errors,
    )


if __name__ == "__main__":
    main()
