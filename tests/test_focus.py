import logging
from pathlib import Path

import pytest

from rpicam_slack.camera import ShotResult
from rpicam_slack.exposure import ImageStats
from rpicam_slack.focus import focus_scan, recommend, scan_positions, spread
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
