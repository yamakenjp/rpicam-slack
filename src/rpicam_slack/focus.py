"""Focus calibration: shoot at several lens positions and pick the sharpest one.

The sharpness measure is the camera's own focus figure of merit (``FocusFoM`` in the
metadata). Point the camera at the real scene, run ``rpicam-slack focus-scan``, and put the
recommended value into ``camera.local.toml`` as ``lens_position``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .camera import Camera, CameraError, Shot
from .config import Config
from .exposure import command_timeout_s, shot_cost_s

log = logging.getLogger(__name__)

SCAN_WIDTH, SCAN_HEIGHT, SCAN_QUALITY = 1280, 720, 70


def scan_positions(start: float, end: float, step: float) -> list[float]:
    if step <= 0:
        raise ValueError("step must be positive")
    if end < start:
        raise ValueError("end must not be smaller than start")
    count = int(round((end - start) / step))
    return [round(start + i * step, 6) for i in range(count + 1) if start + i * step <= end + 1e-9]


def focus_scan(
    cfg: Config, camera: Camera, positions: list[float], tmp: Path
) -> list[tuple[float, float | None]]:
    ex, b = cfg.exposure, cfg.budget
    cost = shot_cost_s(0.07, ex.test_preview_ms, b)
    results: list[tuple[float, float | None]] = []
    for index, position in enumerate(positions):
        shot = Shot(
            "focus", SCAN_WIDTH, SCAN_HEIGHT, SCAN_QUALITY,
            preview_ms=ex.test_preview_ms, lens_position=position,
        )  # fmt: skip
        try:
            result = camera.shoot(shot, tmp / f"focus{index}.jpg", timeout_s=command_timeout_s(cost, b))
        except CameraError as exc:
            log.error("lens position %.2f failed: %s", position, exc)
            results.append((position, None))
            continue
        results.append((position, result.focus_fom))
        log.info("lens position %.2f -> FocusFoM %s", position, result.focus_fom)
    return results


def recommend(results: list[tuple[float, float | None]]) -> float | None:
    scored = [(fom, position) for position, fom in results if fom is not None]
    return max(scored)[1] if scored else None
