"""Thin wrapper around ``rpicam-still``."""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .config import CameraConfig

log = logging.getLogger(__name__)


class CameraError(RuntimeError):
    """The camera command failed, timed out, or produced no usable image."""


@dataclass(frozen=True)
class Shot:
    """What to capture. ``None`` fields fall back to the configuration."""

    kind: str  # "test" | "final" | "focus"
    width: int
    height: int
    quality: int
    shutter_us: int | None = None  # None -> auto exposure
    gain: float | None = None
    preview_ms: int | None = None  # None or 0 -> --immediate
    lens_position: float | None = None  # None -> not forced (see autofocus_on_capture)
    autofocus_on_capture: bool = False
    denoise: str | None = None
    hdr: str | None = None


@dataclass(frozen=True)
class ShotResult:
    image: Path
    metadata: Mapping[str, Any]
    elapsed_s: float

    def _num(self, key: str) -> float | None:
        value = self.metadata.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return float(value)

    @property
    def exposure_us(self) -> float | None:
        return self._num("ExposureTime")

    @property
    def analogue_gain(self) -> float | None:
        return self._num("AnalogueGain")

    @property
    def digital_gain(self) -> float | None:
        return self._num("DigitalGain")

    @property
    def lux(self) -> float | None:
        return self._num("Lux")

    @property
    def focus_fom(self) -> float | None:
        return self._num("FocusFoM")

    @property
    def total_gain(self) -> float | None:
        analogue = self.analogue_gain
        if analogue is None:
            return None
        return analogue * (self.digital_gain or 1.0)

    @property
    def total_exposure_s(self) -> float | None:
        """Equivalent exposure: shutter seconds multiplied by the total gain."""
        exposure, gain = self.exposure_us, self.total_gain
        if exposure is None or gain is None:
            return None
        return exposure / 1_000_000 * gain


class Camera(Protocol):
    def shoot(self, shot: Shot, image: Path, *, timeout_s: float) -> ShotResult: ...


def read_metadata(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("cannot read camera metadata %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


class RpicamStill:
    def __init__(
        self,
        cfg: CameraConfig,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self._runner = runner
        self._clock = clock

    def build_args(self, shot: Shot, image: Path, metadata: Path) -> list[str]:
        cfg = self.cfg
        args = [
            cfg.command,
            "--nopreview",
            "--output", str(image),
            "--metadata", str(metadata),
            "--metadata-format", "json",
            "--width", str(shot.width),
            "--height", str(shot.height),
            "--quality", str(shot.quality),
            "--hdr", shot.hdr or cfg.hdr,
            "--denoise", shot.denoise or cfg.denoise,
            "--metering", cfg.metering,
            "--awb", cfg.awb,
        ]  # fmt: skip
        if shot.lens_position is not None:
            args += ["--autofocus-mode", "manual", "--lens-position", f"{shot.lens_position:.6f}"]
        elif shot.autofocus_on_capture:
            args += ["--autofocus-mode", "auto", "--autofocus-on-capture"]
        if shot.shutter_us is not None:
            args += ["--shutter", str(int(shot.shutter_us))]
        if shot.gain is not None:
            args += ["--gain", f"{shot.gain:.4f}"]
        if shot.preview_ms is None or shot.preview_ms <= 0:
            args.append("--immediate")
        else:
            args += ["--timeout", str(int(shot.preview_ms))]
        args += list(cfg.extra_args)
        return args

    def shoot(self, shot: Shot, image: Path, *, timeout_s: float) -> ShotResult:
        metadata = image.with_suffix(".json")
        args = self.build_args(shot, image, metadata)
        log.debug("running: %s", shlex.join(args))
        started = self._clock()
        try:
            proc = self._runner(args, capture_output=True, text=True, timeout=timeout_s, check=False)
        except subprocess.TimeoutExpired as exc:
            raise CameraError(f"{self.cfg.command} ({shot.kind}) timed out after {timeout_s:.0f}s") from exc
        except OSError as exc:
            raise CameraError(f"cannot run {self.cfg.command}: {exc}") from exc
        elapsed = self._clock() - started
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip()[-400:]
            raise CameraError(f"{self.cfg.command} exited with {proc.returncode}: {tail}")
        if not image.exists() or image.stat().st_size == 0:
            raise CameraError(f"{self.cfg.command} produced no image ({shot.kind})")
        return ShotResult(image=image, metadata=read_metadata(metadata), elapsed_s=elapsed)
