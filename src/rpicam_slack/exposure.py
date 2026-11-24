"""Exposure maths: measure an image, decide the next exposure, fit it to a time budget.

All functions here are pure (except :func:`measure_image`, which reads a file), so the
convergence behaviour can be tested without a camera.

Terminology
-----------
* *total* (equivalent exposure): ``shutter seconds x gain``. Two shots with the same total
  have the same brightness, which is what lets a short, high-gain *test* shot predict a long,
  low-gain *final* shot.
* *luma*: mean of the grey image, 0..255 (display-referred, so roughly ``total ** (1/gamma)``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from .config import BudgetConfig, ExposureConfig


@dataclass(frozen=True)
class ImageStats:
    luma: float
    clipped_high: float  # fraction of pixels >= 250
    clipped_low: float  # fraction of pixels <= 5


@dataclass(frozen=True)
class Exposure:
    shutter_us: int
    gain: float
    limited: str | None = None  # "dark": cannot get brighter, "bright": cannot get darker

    @property
    def shutter_s(self) -> float:
        return self.shutter_us / 1_000_000

    @property
    def total(self) -> float:
        return self.shutter_s * self.gain


def measure_image(path: Path, max_side: int = 320) -> ImageStats:
    """Mean luma and clipping fractions. JPEGs are decoded at reduced size (fast, low memory)."""
    with Image.open(path) as image:
        image.draft("L", (max_side, max_side))
        gray = image.convert("L")
        gray.thumbnail((max_side, max_side))
        histogram = gray.histogram()
    count = sum(histogram)
    if count == 0:
        raise ValueError(f"empty image: {path}")
    mean = sum(level * n for level, n in enumerate(histogram)) / count
    return ImageStats(
        luma=mean,
        clipped_high=sum(histogram[250:]) / count,
        clipped_low=sum(histogram[:6]) / count,
    )


def effective_target(cfg: ExposureConfig) -> float:
    """The luma to aim for after applying the ``ev`` bias."""
    target = cfg.target_luma * 2.0 ** (cfg.ev / cfg.gamma)
    return min(max(target, 1.0), 254.0)


def is_converged(stats: ImageStats, cfg: ExposureConfig) -> bool:
    return abs(stats.luma - effective_target(cfg)) <= cfg.tolerance


def next_total(prev_total: float, stats: ImageStats, cfg: ExposureConfig) -> float:
    """The equivalent exposure that should bring the measured luma to the target."""
    luma = max(stats.luma, 1.0)
    factor = (effective_target(cfg) / luma) ** cfg.gamma
    factor = min(max(factor, 1.0 / cfg.step_limit), cfg.step_limit)
    if stats.clipped_high > cfg.max_clipped_fraction and factor > 1.0:
        factor = 1.0  # blown highlights: never brighten further
    return prev_total * factor


def plan_exposure(total: float, cfg: ExposureConfig, *, max_shutter_s: float) -> Exposure:
    """Split an equivalent exposure into shutter and gain.

    Preference order (lowest noise first):
      1. gain ``min_gain`` while the shutter stays below ``comfortable_shutter_s``;
      2. raise the gain up to ``preferred_gain`` at the comfortable shutter;
      3. lengthen the shutter (gain ``preferred_gain``) up to ``max_shutter_s``;
      4. raise the gain up to ``max_gain``.
    """
    total = max(total, 1e-9)
    min_s = cfg.min_shutter_s
    cap = max(min_s, max_shutter_s)
    comfortable = min(cfg.comfortable_shutter_s, cap)

    if total <= comfortable * cfg.min_gain:
        gain = cfg.min_gain
        shutter = total / gain
    elif total <= comfortable * cfg.preferred_gain:
        shutter = comfortable
        gain = total / shutter
    elif total <= cap * cfg.preferred_gain:
        gain = cfg.preferred_gain
        shutter = total / gain
    else:
        shutter = cap
        gain = total / shutter

    limited: str | None = None
    if shutter < min_s:
        shutter = min_s
        gain = cfg.min_gain
        if shutter * gain > total * 1.05:
            limited = "bright"
    if gain > cfg.max_gain:
        gain = cfg.max_gain
        limited = "dark"
    return Exposure(shutter_us=max(1, round(shutter * 1_000_000)), gain=round(gain, 4), limited=limited)


def label(total: float, cfg: ExposureConfig) -> str:
    """A rough profile name (``day`` / ``twilight`` / ``night``) from the equivalent exposure."""
    if total <= cfg.day_max_s:
        return "day"
    if total <= cfg.twilight_max_s:
        return "twilight"
    return "night"


def shot_cost_s(
    shutter_s: float, preview_ms: int | None, budget: BudgetConfig, frames: float | None = None
) -> float:
    """Estimated wall time of one capture (a manual-exposure still needs several frames)."""
    n = budget.frames_per_shot if frames is None else frames
    return n * max(shutter_s, 0.03) + (preview_ms or 0) / 1000 + budget.overhead_s


def command_timeout_s(cost_s: float, budget: BudgetConfig) -> float:
    return cost_s * budget.timeout_factor + budget.timeout_slack_s


def max_shutter_for(
    remaining_s: float, preview_ms: int | None, budget: BudgetConfig, frames: float | None = None
) -> float:
    """The longest shutter whose estimated cost still fits into ``remaining_s``."""
    n = budget.frames_per_shot if frames is None else frames
    spare = remaining_s - budget.overhead_s - (preview_ms or 0) / 1000
    return spare / n if spare > 0 else 0.0
