from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterator

import cv2
import numpy as np
from PIL import Image

from tagger.models import InferResult, Tag

SAMPLE_INTERVAL_SEC = 10.0
MAX_FRAMES = 60

# Least to most severe. app.py's SENSITIVE_RATINGS excludes everything above "general".
RATING_SEVERITY = ("general", "sensitive", "questionable", "explicit")
# A rating counts as "present in the video" once any single frame reaches this.
RATING_PRESENT_MIN = 0.5


@dataclass
class VideoInfo:
    duration_sec: float
    fps: float
    width: int
    height: int


def open_video(path: str) -> tuple[cv2.VideoCapture, VideoInfo]:
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise ValueError("OpenCV could not open the file")
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
    if fps <= 0 or frame_count <= 0:
        cap.release()
        raise ValueError(f"No usable fps/frame count (fps={fps}, frames={frame_count})")
    info = VideoInfo(
        duration_sec=frame_count / fps,
        fps=fps,
        width=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
        height=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
    )
    return cap, info


def sample_times(duration_sec: float, interval_sec: float, max_frames: int) -> list[float]:
    n = min(max_frames, max(1, math.ceil(duration_sec / interval_sec)))
    # Segment midpoints rather than 0..duration endpoints: the first and last
    # frames are disproportionately black frames, title cards or end credits.
    return [duration_sec * (i + 0.5) / n for i in range(n)]


def iter_sample_frames(
    cap: cv2.VideoCapture,
    info: VideoInfo,
    interval_sec: float = SAMPLE_INTERVAL_SEC,
    max_frames: int = MAX_FRAMES,
) -> Iterator[Image.Image]:
    for t in sample_times(info.duration_sec, interval_sec, max_frames):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(t * info.fps))
        ok, frame = cap.read()
        if not ok:
            # A bad seek target in one spot shouldn't discard the whole video;
            # the caller errors out only if no frame at all could be read.
            continue
        yield Image.fromarray(frame[:, :, ::-1])  # BGR -> RGB


def middle_frame(cap: cv2.VideoCapture, info: VideoInfo) -> Image.Image | None:
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(info.duration_sec / 2 * info.fps))
    ok, frame = cap.read()
    return Image.fromarray(frame[:, :, ::-1]) if ok else None


def aggregate(results: list[InferResult]) -> tuple[list[Tag], np.ndarray | None]:
    """Collapse per-frame inference into one tag list + one embedding for the video.

    Non-rating tags take their max confidence over frames, so a tag that shows
    up in a single frame is still searchable. Ratings can't use "the single
    highest rating" the way images do: one explicit frame would lose to the
    general rating of the other frames and slip past `exclude sensitive`. So
    the most severe rating that clears RATING_PRESENT_MIN wins instead.
    """
    best: dict[str, Tag] = {}
    for result in results:
        for tag in result.tags:
            if tag.category == "rating":
                continue
            current = best.get(tag.name)
            if current is None or tag.confidence > current.confidence:
                best[tag.name] = tag

    rating_max: dict[str, float] = {}
    for result in results:
        for tag in result.tags:
            if tag.category == "rating":
                rating_max[tag.name] = max(rating_max.get(tag.name, 0.0), tag.confidence)
    tags = list(best.values())
    if rating_max:
        present = [r for r in RATING_SEVERITY if rating_max.get(r, 0.0) >= RATING_PRESENT_MIN]
        chosen = present[-1] if present else max(rating_max, key=rating_max.__getitem__)
        tags.append(Tag(name=chosen, category="rating", confidence=rating_max[chosen]))

    embeddings = [r.embedding for r in results if r.embedding is not None]
    embedding = np.mean(embeddings, axis=0) if len(embeddings) == len(results) else None
    return tags, embedding
