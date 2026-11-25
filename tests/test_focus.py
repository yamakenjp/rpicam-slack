import logging
import threading
from pathlib import Path

import pytest

from rpicam_slack.camera import ShotResult
from rpicam_slack.exposure import ImageStats
from rpicam_slack.focus import LOCK_GAP_S, focus_scan, recommend, scan_positions, spread
from rpicam_slack.session import ALERT_MIN_S
from rpicam_slack.state import LOCK_POLL_S, locked
from tests.conftest import make_config


def test_a_flat_curve_is_not_a_recommendation():
    """Regression: in the dark every position scored ~2140 and 0.5 was 'recommended'."""
    flat = [(0.0, 2135.0), (0.5, 2156.0), (1.0, 2147.0), (4.5, 2133.0), (8.0, 2110.0)]
    assert spread(flat) < 0.03
    assert recommend(flat) is None


def test_a_distinct_peak_is_recommended():
    sharp = [(0.0, 900.0), (1.0, 1500.0), (2.0, 2400.0), (3.0, 1300.0)]
    assert recommend(sharp) == 2.0


def test_unusable_results():
    assert recommend([]) is None
    assert recommend([(0.0, None), (1.0, None)]) is None
    assert spread([(0.0, 5.0)]) is None
    assert recommend([(0.0, 0.0), (4.0, 0.0), (8.0, 0.0)]) is None
    assert recommend([(0.0, None), (4.0, 1200.0)]) is None


def test_scan_positions():
    assert scan_positions(0.0, 2.0, 0.5) == [0.0, 0.5, 1.0, 1.5, 2.0]
    with pytest.raises(ValueError):
        scan_positions(0.0, 1.0, 0)
    with pytest.raises(ValueError):
        scan_positions(3.0, 1.0, 0.5)


class FocusCamera:
    def shoot(self, shot, image: Path, *, timeout_s: float) -> ShotResult:
        image.write_bytes(b"x")
        return ShotResult(image=image, metadata={"FocusFoM": 2000.0 + shot.lens_position}, elapsed_s=1.0)


def test_scanning_in_the_dark_warns(tmp_path, caplog):
    cfg = make_config(tmp_path)
    dark = lambda path: ImageStats(luma=7.0, clipped_high=0.0, clipped_low=0.9)  # noqa: E731
    with caplog.at_level(logging.WARNING, logger="rpicam_slack.focus"):
        results = focus_scan(cfg, FocusCamera(), [0.0, 1.0], tmp_path, measure=dark)
    assert [fom for _, fom in results] == [2000.0, 2001.0]
    assert "dark" in caplog.text and "daylight" in caplog.text


def test_scanning_in_daylight_does_not_warn(tmp_path, caplog):
    cfg = make_config(tmp_path)
    bright = lambda path: ImageStats(luma=120.0, clipped_high=0.0, clipped_low=0.0)  # noqa: E731
    with caplog.at_level(logging.WARNING, logger="rpicam_slack.focus"):
        focus_scan(cfg, FocusCamera(), [0.0, 1.0], tmp_path, measure=bright)
    assert caplog.text == ""


def test_the_lock_is_held_for_each_shot_and_free_in_between(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    lock = tmp_path / "lock"
    seen: list[tuple[str, bool]] = []

    def free() -> bool:
        with locked(lock) as acquired:
            return acquired

    class Camera(FocusCamera):
        def shoot(self, shot, image: Path, *, timeout_s: float) -> ShotResult:
            seen.append(("shot", free()))
            return super().shoot(shot, image, timeout_s=timeout_s)

    monkeypatch.setattr("rpicam_slack.focus.time.sleep", lambda seconds: seen.append(("gap", free())))
    results = focus_scan(cfg, Camera(), [0.0, 1.0, 2.0], tmp_path, lock=lock)
    assert [fom for _, fom in results] == [2000.0, 2001.0, 2002.0]
    assert seen == [("shot", False), ("gap", True), ("shot", False), ("gap", True), ("shot", False)]
    assert LOCK_GAP_S >= 2 * LOCK_POLL_S  # a waiting capture looks at the lock at least twice in the gap


def test_a_shot_is_given_up_when_the_camera_stays_busy(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    lock = tmp_path / "lock"
    shots, waits = [], []

    class Camera(FocusCamera):
        def shoot(self, shot, image: Path, *, timeout_s: float) -> ShotResult:
            shots.append(shot)
            return super().shoot(shot, image, timeout_s=timeout_s)

    def impatient(path, wait_s):
        waits.append(wait_s)
        return locked(path, 0.05)

    monkeypatch.setattr("rpicam_slack.focus.locked", impatient)
    with locked(lock) as acquired:  # a capture that does not finish in time
        assert acquired
        results = focus_scan(cfg, Camera(), [0.0], tmp_path, lock=lock)
    assert results == [(0.0, None)] and shots == []  # never shoot without the lock
    # It would have waited for a whole capture, and one that alerts runs ALERT_MIN_S over its budget.
    assert len(waits) == 1 and waits[0] > cfg.budget.max_total_s + ALERT_MIN_S


def test_a_shot_waits_for_a_capture_that_holds_the_lock(tmp_path):
    cfg = make_config(tmp_path)
    lock = tmp_path / "lock"
    holding, release = threading.Event(), threading.Event()

    def capture():
        with locked(lock):
            holding.set()
            release.wait(5)

    thread = threading.Thread(target=capture)
    thread.start()
    assert holding.wait(5)
    threading.Timer(0.2, release.set).start()
    results = focus_scan(cfg, FocusCamera(), [0.0], tmp_path, lock=lock)
    thread.join()
    assert results == [(0.0, 2000.0)]  # not recorded as a failed position
