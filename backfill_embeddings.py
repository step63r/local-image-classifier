#!/usr/bin/env python
"""One-time backfill: compute embeddings for images tagged before embedding
support existed.

Re-running the same command resumes automatically: any image_id that already
has a row in the `embeddings` table is skipped. Safe to interrupt.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from PIL import Image
from tqdm import tqdm

from tagger import database
from tagger.models import WD14Tagger


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=Path("tags.db"), type=Path)
    p.add_argument("--model", default="wd-eva02-large-tagger-v3")
    p.add_argument("--limit", type=int, default=None, help="Process at most N rows (for testing)")
    p.add_argument("--log-dir", default=Path("logs"), type=Path)
    p.add_argument("--commit-every", type=int, default=20, help="SQLite commit batch size")
    return p.parse_args()


def setup_logging(log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"backfill_embeddings_{datetime.now():%Y%m%d_%H%M%S}.log"
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

    if not args.db.is_file():
        logging.error("Database not found: %s", args.db)
        sys.exit(1)

    logging.info("Loading model: %s", args.model)
    tagger = WD14Tagger(args.model)
    logging.info("Providers in use: %s", tagger.session.get_providers())
    if tagger.embedding_output_name is None:
        logging.error(
            "Model '%s' has no known embedding tap point (see tagger.models.EMBEDDING_TAPS) -- "
            "only wd-eva02-large-tagger-v3 is currently supported.",
            args.model,
        )
        sys.exit(1)

    conn = database.connect(args.db)

    rows = conn.execute(
        "SELECT i.id, i.path, i.size, i.mtime FROM images i "
        "LEFT JOIN embeddings e ON e.image_id = i.id "
        "WHERE i.status = 'done' AND i.model = ? AND e.image_id IS NULL "
        "ORDER BY i.id",
        (args.model,),
    ).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    logging.info("Found %d images without an embedding yet", len(rows))

    done = skipped = errors = 0
    pending_commits = 0
    start = time.monotonic()

    try:
        with tqdm(rows, unit="img") as bar:
            for row in bar:
                bar.set_postfix(done=done, skipped=skipped, errors=errors)
                image_id, path, db_size, db_mtime = row

                try:
                    stat = Path(path).stat()
                except OSError as e:
                    logging.warning("Skipping image_id=%s: stat failed for %s: %s", image_id, path, e)
                    skipped += 1
                    continue

                if stat.st_size != db_size or stat.st_mtime != db_mtime:
                    logging.warning(
                        "Skipping image_id=%s: %s has changed since it was tagged "
                        "(re-run batch_tag.py --force to retag and re-embed it)",
                        image_id,
                        path,
                    )
                    skipped += 1
                    continue

                try:
                    with Image.open(path) as im:
                        im.load()
                        embedding = tagger.embed(im)
                except Exception as e:
                    logging.error("Failed to embed image_id=%s path=%s: %s", image_id, path, e)
                    errors += 1
                    continue

                database.save_embedding(conn, image_id, args.model, embedding)
                done += 1

                pending_commits += 1
                if pending_commits >= args.commit_every:
                    conn.commit()
                    pending_commits = 0
    except KeyboardInterrupt:
        logging.warning("Interrupted by user, committing progress so far...")
    finally:
        conn.commit()
        conn.close()

    elapsed = time.monotonic() - start
    logging.info(
        "Finished in %.1fs: done=%d skipped=%d errors=%d "
        "(re-run the same command to resume/retry)",
        elapsed, done, skipped, errors,
    )


if __name__ == "__main__":
    main()
