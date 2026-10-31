#!/usr/bin/env python
"""Resumable WD14 batch tagging for videos/GIFs: folder -> sampled frames -> ONNX inference -> SQLite.

Each video becomes one row of `images` (media_type='video') in the same tags.db
as batch_tag.py, with tags aggregated over evenly spaced sampled frames and an
embedding averaged over them. Re-running the same command resumes automatically:
any file already recorded as 'done' (same path, size, mtime and model) is skipped.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from tqdm import tqdm

from batch_tag import select_tags
from tagger import database, video
from tagger.models import MODEL_REGISTRY, WD14Tagger

VIDEO_EXTENSIONS = {".mp4", ".mov", ".gif"}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dir", required=True, type=Path, help="Root folder of videos")
    p.add_argument("--recursive", action="store_true")
    p.add_argument(
        "--model",
        default="wd-eva02-large-tagger-v3",
        help=f"Short name ({', '.join(MODEL_REGISTRY)}) or HF repo id",
    )
    p.add_argument("--db", default=Path("tags.db"), type=Path)
    p.add_argument(
        "--general-threshold",
        type=float,
        default=0.1,
        help="Storage floor for general tags (default 0.1), same rationale as batch_tag.py.",
    )
    p.add_argument(
        "--character-threshold",
        type=float,
        default=0.1,
        help="Storage floor for character tags (default 0.1), same rationale as batch_tag.py.",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=video.SAMPLE_INTERVAL_SEC,
        help="Seconds of video per sampled frame (default %(default)s)",
    )
    p.add_argument(
        "--max-frames",
        type=int,
        default=video.MAX_FRAMES,
        help="Upper bound of sampled frames per video (default %(default)s)",
    )
    p.add_argument("--limit", type=int, default=None, help="Process at most N files (for testing)")
    p.add_argument("--force", action="store_true", help="Reprocess even if already tagged")
    p.add_argument("--log-dir", default=Path("logs"), type=Path)
    p.add_argument("--commit-every", type=int, default=20, help="SQLite commit batch size")
    return p.parse_args()


def setup_logging(log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"batch_tag_video_{datetime.now():%Y%m%d_%H%M%S}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )
    return log_path


def iter_videos(root: Path, recursive: bool):
    pattern = "**/*" if recursive else "*"
    for path in sorted(root.glob(pattern)):
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
            yield path


def tag_video(tagger: WD14Tagger, path: Path, interval: float, max_frames: int):
    cap, info = video.open_video(str(path))
    try:
        results = [tagger.infer(frame) for frame in video.iter_sample_frames(cap, info, interval, max_frames)]
    finally:
        cap.release()
    if not results:
        raise ValueError("No frame could be read")
    tags, embedding = video.aggregate(results)
    return info, tags, embedding


def main() -> None:
    args = parse_args()
    log_path = setup_logging(args.log_dir)
    logging.info("Log file: %s", log_path)

    if not args.dir.is_dir():
        logging.error("Directory not found: %s", args.dir)
        sys.exit(1)

    logging.info("Scanning %s (recursive=%s)...", args.dir, args.recursive)
    files = list(iter_videos(args.dir, args.recursive))
    if args.limit:
        files = files[: args.limit]
    logging.info("Found %d video files", len(files))

    logging.info("Loading model: %s", args.model)
    tagger = WD14Tagger(args.model)
    logging.info("Providers in use: %s", tagger.session.get_providers())

    conn = database.connect(args.db)

    done = skipped = errors = 0
    pending_commits = 0
    start = time.monotonic()

    try:
        with tqdm(files, unit="video") as bar:
            for path in bar:
                bar.set_postfix(done=done, skipped=skipped, errors=errors)
                abs_path = str(path.resolve())
                try:
                    stat = path.stat()
                except OSError as e:
                    logging.error("stat failed for %s: %s", path, e)
                    errors += 1
                    continue

                if not args.force and database.already_done(
                    conn, abs_path, stat.st_size, stat.st_mtime, args.model
                ):
                    skipped += 1
                    continue

                tagged_at = datetime.now(timezone.utc).isoformat()
                try:
                    info, raw_tags, embedding = tag_video(tagger, path, args.interval, args.max_frames)
                except Exception as e:
                    logging.error("Failed to tag %s: %s", path, e)
                    database.save_error(
                        conn, abs_path, stat.st_size, stat.st_mtime, args.model, str(e), tagged_at
                    )
                    errors += 1
                else:
                    selected = select_tags(raw_tags, args.general_threshold, args.character_threshold)
                    image_id = database.save_result(
                        conn,
                        abs_path,
                        stat.st_size,
                        stat.st_mtime,
                        args.model,
                        selected,
                        tagged_at,
                        media_type="video",
                        duration_sec=info.duration_sec,
                        width=info.width,
                        height=info.height,
                    )
                    if embedding is not None:
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
