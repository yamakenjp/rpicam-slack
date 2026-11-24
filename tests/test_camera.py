from pathlib import Path

import pytest

from rpicam_slack.camera import CameraError, RpicamStill, Shot
from rpicam_slack.config import CameraConfig


def args_for(shot: Shot, **camera) -> list[str]:
    driver = RpicamStill(CameraConfig(**camera))
    return driver.build_args(shot, Path("/t/a.jpg"), Path("/t/a.json"))


def pair(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def test_auto_exposure_test_shot_has_no_manual_exposure_flags():
    args = args_for(Shot("test", 640, 360, 60, preview_ms=2000, lens_position=4.5))
    assert "--shutter" not in args and "--gain" not in args
    assert pair(args, "--timeout") == "2000"
    assert "--immediate" not in args
    assert pair(args, "--lens-position") == "4.500000"
    assert pair(args, "--autofocus-mode") == "manual"


def test_manual_test_shot_is_immediate():
    args = args_for(Shot("test", 640, 360, 60, shutter_us=2500, gain=3.25))
    assert pair(args, "--shutter") == "2500"
    assert pair(args, "--gain") == "3.2500"
    assert "--immediate" in args and "--timeout" not in args


def test_final_shot_without_a_fixed_lens_uses_autofocus_on_capture():
    shot = Shot("final", 2304, 1296, 92, shutter_us=1000, gain=1.0, preview_ms=2000,
                autofocus_on_capture=True)  # fmt: skip
    args = args_for(shot)
    assert "--autofocus-on-capture" in args
    assert "--lens-position" not in args
    assert (pair(args, "--width"), pair(args, "--height"), pair(args, "--quality")) == ("2304", "1296", "92")


def test_configuration_flows_into_the_command():
    args = args_for(
        Shot("final", 100, 100, 50),
        hdr="off", denoise="cdn_fast", metering="spot", awb="daylight", extra_args=("--vflip",),
    )  # fmt: skip
    assert (pair(args, "--hdr"), pair(args, "--denoise")) == ("off", "cdn_fast")
    assert (pair(args, "--metering"), pair(args, "--awb")) == ("spot", "daylight")
    assert args[-1] == "--vflip"


def test_shoot_returns_image_and_metadata(fake_rpicam, tmp_path):
    camera = RpicamStill(CameraConfig(command=str(fake_rpicam)))
    result = camera.shoot(
        Shot("test", 64, 36, 60, shutter_us=2000, gain=3.0), tmp_path / "a.jpg", timeout_s=20
    )
    assert result.image.exists()
    assert result.exposure_us == 2000
    assert result.total_gain == 3.0
    assert result.total_exposure_s == pytest.approx(0.006)
    assert result.lux == 5.0


def test_a_hung_camera_is_killed_at_the_timeout(fake_rpicam, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "10")
    camera = RpicamStill(CameraConfig(command=str(fake_rpicam)))
    with pytest.raises(CameraError, match="timed out after 1s"):
        camera.shoot(Shot("final", 64, 36, 60), tmp_path / "a.jpg", timeout_s=1)


def test_a_failing_command_reports_its_stderr(fake_rpicam, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FAIL", "1")
    camera = RpicamStill(CameraConfig(command=str(fake_rpicam)))
    with pytest.raises(CameraError, match="exited with 3: ERROR: no cameras available"):
        camera.shoot(Shot("final", 64, 36, 60), tmp_path / "a.jpg", timeout_s=20)


def test_a_missing_command_is_a_camera_error(tmp_path):
    camera = RpicamStill(CameraConfig(command=str(tmp_path / "does-not-exist")))
    with pytest.raises(CameraError, match="cannot run"):
        camera.shoot(Shot("final", 64, 36, 60), tmp_path / "a.jpg", timeout_s=5)
