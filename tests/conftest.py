"""Shared fakes: a scene simulator standing in for the camera, a fake uploader, a fake clock."""

from __future__ import annotations

import json
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from rpicam_slack.camera import CameraError, Shot, ShotResult
from rpicam_slack.config import Config, config_from_dict
from rpicam_slack.session import Deps
from rpicam_slack.slack import UploadError
from rpicam_slack.state import Spool, StateStore

SCENE_GAMMA = 2.2  # the "true" camera response; the program assumes 2.0, so it must iterate


def scene_luma(k: float, total: float) -> float:
    """Display luma of a scene whose linear brightness is ``k`` per equivalent second."""
    linear = min(1.0, max(0.0, k * total))
    return 255.0 * linear ** (1.0 / SCENE_GAMMA)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeCamera:
    """Simulates exposure: brightness depends only on shutter x gain, like the real thing.

    It also enforces what a real run must satisfy: no shot may take longer than the timeout
    it was given, and the clock advances by the time the shot would really take.
    """

    def __init__(
        self,
        clock: FakeClock,
        k: float,
        *,
        frames: float = 4.5,
        ae_max_total: float = 1.0,
        fail_runs: int = 0,
    ) -> None:
        self.clock = clock
        self.k = k
        self.frames = frames
        self.ae_max_total = ae_max_total
        self.fail_runs = fail_runs
        self.shots: list[Shot] = []
        self.timeouts: list[float] = []
        self.last_luma = 0.0

    def shoot(self, shot: Shot, image: Path, *, timeout_s: float) -> ShotResult:
        if self.fail_runs > 0:
            raise CameraError("camera is not responding")
        self.shots.append(shot)
        self.timeouts.append(timeout_s)
        if shot.shutter_us is None:  # camera auto exposure: as good as its limits allow
            wanted = (118.0 / 255.0) ** SCENE_GAMMA / self.k
            total = min(max(wanted, 0.0005), self.ae_max_total)
            shutter = min(0.066, total)
            gain = max(1.0, total / shutter)
        else:
            shutter = shot.shutter_us / 1_000_000
            gain = shot.gain or 1.0
        total = shutter * gain
        elapsed = self.frames * max(shutter, 0.03) + (shot.preview_ms or 0) / 1000 + 1.0
        assert elapsed <= timeout_s, f"{shot.kind} shot would time out: {elapsed:.1f}s > {timeout_s:.1f}s"
        self.clock.advance(elapsed)
        self.last_luma = round(scene_luma(self.k, total))
        Image.new("RGB", (64, 36), (int(self.last_luma),) * 3).save(image, quality=95)
        metadata = {
            "ExposureTime": round(shutter * 1_000_000),
            "AnalogueGain": gain,
            "DigitalGain": 1.0,
            "Lux": 3.0,
        }
        return ShotResult(image=image, metadata=metadata, elapsed_s=elapsed)


class FakeUploader:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str]] = []  # (filename, comment)
        self.alerts: list[str] = []
        self.fail_uploads = 0

    def upload(self, path: Path, comment: str, filename: str, *, deadline: float | None = None) -> None:
        if self.fail_uploads > 0:
            self.fail_uploads -= 1
            raise UploadError("network is down")
        assert Path(path).exists()
        self.uploads.append((filename, comment))

    def alert(self, text: str, *, deadline: float | None = None) -> bool:
        self.alerts.append(text)
        return True


def make_config(tmp_path: Path, **sections: dict[str, Any]) -> Config:
    (tmp_path / "tmp").mkdir(exist_ok=True)
    data: dict[str, Any] = {
        "name": "cam-test",
        "paths": {"state_dir": str(tmp_path / "state"), "tmp_dir": str(tmp_path / "tmp")},
        "output": {"embed_exif": False},
    }
    for name, values in sections.items():
        data[name] = {**data.get(name, {}), **values}
    return config_from_dict(data)


class Rig:
    """A wired-up session: config, fakes and the pieces tests want to inspect."""

    def __init__(self, tmp_path: Path, k: float, **sections: dict[str, Any]) -> None:
        self.clock = FakeClock()
        self.cfg = make_config(tmp_path, **sections)
        self.camera = FakeCamera(self.clock, k)
        self.uploader = FakeUploader()
        self.store = StateStore(self.cfg.paths.state_path)
        self.spool = Spool(self.cfg.paths.state_path / "spool", self.cfg.paths.spool_max_files)
        self.epoch = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone(timedelta(hours=9)))

    def deps(self, **overrides: Any) -> Deps:
        defaults: dict[str, Any] = {
            "camera": self.camera,
            "store": self.store,
            "spool": self.spool,
            "uploader": self.uploader,
            "clock": self.clock,
            "wall": lambda: self.epoch + timedelta(seconds=self.clock.now),
        }
        return Deps(**{**defaults, **overrides})

    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds)


@pytest.fixture
def rig(tmp_path: Path):
    def build(k: float, **sections: dict[str, Any]) -> Rig:
        return Rig(tmp_path, k, **sections)

    return build


FAKE_RPICAM = """#!{python}
import argparse, json, os, sys, time
from PIL import Image

p = argparse.ArgumentParser()
for opt in ["--output", "--metadata", "--metadata-format", "--width", "--height", "--quality",
            "--hdr", "--denoise", "--metering", "--awb", "--autofocus-mode", "--lens-position",
            "--shutter", "--gain", "--timeout"]:
    p.add_argument(opt)
for flag in ["--nopreview", "--immediate", "--autofocus-on-capture"]:
    p.add_argument(flag, action="store_true")
a = p.parse_args()
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
if os.environ.get("FAKE_FAIL"):
    print("ERROR: no cameras available", file=sys.stderr)
    sys.exit(3)
Image.new("L", (32, 18), int(os.environ.get("FAKE_LEVEL", "115"))).save(a.output, quality=90)
lens = float(a.lens_position) if a.lens_position else 0.0
with open(a.metadata, "w") as fp:
    json.dump({{"ExposureTime": int(a.shutter or 10000), "AnalogueGain": float(a.gain or 1.0),
               "DigitalGain": 1.0, "Lux": 5.0, "FocusFoM": 1000 - 100 * abs(lens - 1.0)}}, fp)
"""


@pytest.fixture
def fake_rpicam(tmp_path: Path) -> Path:
    """An executable that behaves like rpicam-still closely enough for the wrapper."""
    script = tmp_path / "fake-rpicam-still"
    script.write_text(FAKE_RPICAM.format(python=sys.executable), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def write_json(path: Path, data: Any) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path
