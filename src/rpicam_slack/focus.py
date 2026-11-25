"""Focus calibration: shoot at several lens positions and pick the sharpest one.

The sharpness measure is the camera's own focus figure of merit (``FocusFoM`` in the
metadata). It only means something when the scene is bright enough and has texture: in the
dark every lens position scores about the same (noise), so :func:`recommend` refuses to pick
a "winner" from a flat curve. Run it in daylight, pointing at the real scene, and put the
recommended value into ``camera.local.toml`` as ``lens_position``.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable
from pathlib import Path

from .camera import Camera, CameraError, Shot
from .config import Config
from .exposure import ImageStats, command_timeout_s, measure_image, shot_cost_s
from .session import ALERT_MIN_S
from .state import locked

log = logging.getLogger(__name__)

SCAN_WIDTH, SCAN_HEIGHT, SCAN_QUALITY = 1280, 720, 70
LOCK_GAP_S = 0.2  # the lock is left free this long between shots, so a waiting capture gets its turn
LOCK_MARGIN_S = 2.0  # a capture holds the lock a little longer than its budget (start-up, clean-up)
DARK_LUMA = 40.0  # below this the focus figure is mostly noise
MIN_SPREAD = 0.08  # (best - worst) / best must be at least this for the curve to count


def scan_positions(start: float, end: float, step: float) -> list[float]:
    if step <= 0:
        raise ValueError("step must be positive")
    if end < start:
        raise ValueError("end must not be smaller than start")
    count = int(round((end - start) / step))
    return [round(start + i * step, 6) for i in range(count + 1) if start + i * step <= end + 1e-9]


def focus_scan(
    cfg: Config,
    camera: Camera,
    positions: list[float],
    tmp: Path,
    measure: Callable[[Path], ImageStats] = measure_image,
    lock: Path | None = None,
) -> list[tuple[float, float | None]]:
    """Shoot once per lens position. ``lock`` (the capture lock) is held only while shooting, so a
    scheduled capture runs between two shots instead of being skipped for the whole scan."""
    ex, b = cfg.exposure, cfg.budget
    cost = shot_cost_s(0.07, ex.test_preview_ms, b)
    results: list[tuple[float, float | None]] = []
    darkest = 255.0
    for index, position in enumerate(positions):
        shot = Shot(
            "focus", SCAN_WIDTH, SCAN_HEIGHT, SCAN_QUALITY,
            preview_ms=ex.test_preview_ms, lens_position=position,
        )  # fmt: skip
        if lock is not None and index:
            time.sleep(LOCK_GAP_S)
        # Wait for a whole capture; one that has to raise an alert runs ALERT_MIN_S over its budget.
        wait_s = b.max_total_s + ALERT_MIN_S + LOCK_MARGIN_S
        guard = contextlib.nullcontext(True) if lock is None else locked(lock, wait_s)
        try:
            with guard as acquired:
                if not acquired:
                    raise CameraError("the camera stayed busy")
                result = camera.shoot(shot, tmp / f"focus{index}.jpg", timeout_s=command_timeout_s(cost, b))
        except CameraError as exc:
            log.error("lens position %.2f failed: %s", position, exc)
            results.append((position, None))
            continue
        with contextlib.suppress(OSError, ValueError):
            darkest = min(darkest, measure(result.image).luma)
        results.append((position, result.focus_fom))
        log.info("lens position %.2f -> FocusFoM %s", position, result.focus_fom)
    if darkest < DARK_LUMA:
        log.warning(
            "the scene is dark (luma %.0f): the focus figure is unreliable. Run this in daylight.", darkest
        )
    return results


def spread(results: list[tuple[float, float | None]]) -> float | None:
    """How much the focus figure varies over the scan: (best - worst) / best."""
    scores = [fom for _, fom in results if fom is not None]
    if len(scores) < 2 or max(scores) <= 0:
        return None
    return (max(scores) - min(scores)) / max(scores)


def recommend(results: list[tuple[float, float | None]], min_spread: float = MIN_SPREAD) -> float | None:
    """The sharpest lens position, or ``None`` when the curve is too flat to trust."""
    scored = [(fom, position) for position, fom in results if fom is not None]
    variation = spread(results)
    if variation is None or variation < min_spread:
        return None
    return max(scored)[1]
