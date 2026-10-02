"""
Stage runners shared by the CLI commands.

Face analysis is the expensive stage, so it always goes through the on-disk
cache: the first run pays for detection, later runs are effectively free. That
is what makes labeling an iterative loop rather than a single shot.
"""

from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor
from typing import Callable, List, Optional, Sequence

from . import config, faces, quality, scanner, store
from .faces import FaceResult
from .quality import QualityMetrics


ProgressFn = Optional[Callable[[int, int], None]]


def _report(progress: ProgressFn, done: int, total: int) -> None:
    if progress is not None:
        progress(done, total)


# ============================================================================
# WORKER SETUP
# ============================================================================

def _init_worker(overrides: Optional[dict]) -> None:
    """
    Apply runtime config overrides inside a freshly spawned worker.

    Must live at module level to be picklable. This exists because each worker
    imports wedding_album.config afresh and therefore starts with the file's
    defaults: rebinding values in the parent process has no effect on them, so
    a UI-adjusted detection threshold would silently be ignored without this.
    """
    if overrides:
        config.apply_overrides(overrides)


def _pool(workers: int, overrides: Optional[dict]) -> ProcessPoolExecutor:
    return ProcessPoolExecutor(
        max_workers=workers,
        initializer=_init_worker,
        initargs=(overrides,),
    )


# ============================================================================
# FACES
# ============================================================================

def analyze_faces(
    source_dir: str,
    images: Sequence[str],
    workers: int = 1,
    use_cache: bool = True,
    progress: ProgressFn = None,
    overrides: Optional[dict] = None,
) -> tuple[List[FaceResult], int]:
    """
    Detect and embed faces for every image, reusing cached work where the file
    is unchanged.

    Returns the results in the same order as `images`, plus how many were
    served from cache. Any overrides must already be applied in this process;
    they are passed along so workers apply them too.
    """
    faces.require_models()

    cache = store.FaceCache(source_dir)
    if use_cache:
        cache.load()

    cached: dict[str, FaceResult] = {}
    todo: List[str] = []

    for path in images:
        hit = cache.get(path) if use_cache else None
        if hit is not None:
            cached[path] = hit
        else:
            todo.append(path)

    total = len(images)
    done = len(cached)
    _report(progress, done, total)

    fresh: dict[str, FaceResult] = {}
    if todo:
        if workers <= 1:
            analyzer = faces.FaceAnalyzer()
            for path in todo:
                fresh[path] = analyzer.analyze(path)
                done += 1
                _report(progress, done, total)
        else:
            with _pool(workers, overrides) as pool:
                for result in pool.map(faces.analyze_one, todo):
                    fresh[result.filepath] = result
                    done += 1
                    _report(progress, done, total)

        for result in fresh.values():
            cache.put(result)
        cache.prune(list(images))
        cache.save()

    merged = {**cached, **fresh}
    return [merged[p] for p in images if p in merged], len(cached)


# ============================================================================
# QUALITY
# ============================================================================

def analyze_quality(
    images: Sequence[str],
    workers: int = 1,
    progress: ProgressFn = None,
    overrides: Optional[dict] = None,
) -> List[QualityMetrics]:
    """Measure photographic quality for every image."""
    metrics: List[QualityMetrics] = []
    total = len(images)

    if workers <= 1:
        for path in images:
            metrics.append(quality.measure(path))
            _report(progress, len(metrics), total)
    else:
        with _pool(workers, overrides) as pool:
            for m in pool.map(quality.measure, images):
                metrics.append(m)
                _report(progress, len(metrics), total)

    return metrics


# ============================================================================
# CONVENIENCE
# ============================================================================

def load_images(source_dir: str, limit: int = 0) -> List[str]:
    images = scanner.find_images(source_dir)
    return images[:limit] if limit else images


def default_workers() -> int:
    return max(1, (os.cpu_count() or 4) // 2)
