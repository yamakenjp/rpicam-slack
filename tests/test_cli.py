import json

import pytest

from rpicam_slack.cli import main


def write_config(tmp_path, fake_command, extra=""):
    path = tmp_path / "camera.toml"
    path.write_text(
        f'name = "cam-cli"\n'
        f"[camera]\ncommand = {json.dumps(str(fake_command))}\n"
        f'[paths]\nstate_dir = "{tmp_path / "state"}"\n'
        f"[output]\nembed_exif = false\n{extra}",
        encoding="utf-8",
    )
    return str(path)


def test_check_reports_a_missing_slack_file(tmp_path, fake_rpicam, capsys):
    config = write_config(tmp_path, fake_rpicam)
    code = main(["check", "--config", config, "--slack-option", str(tmp_path / "nope")])
    out = capsys.readouterr().out
    assert code == 2
    assert "NG  Slack option file not found" in out
    assert "OK  " in out


def test_check_passes_with_a_good_setup(tmp_path, fake_rpicam, capsys):
    option = tmp_path / "slack_option"
    option.write_text("SLACK_TOKEN=xoxb-secret\nCHANNEL=C1\n", encoding="utf-8")
    option.chmod(0o600)
    code = main(["check", "--config", write_config(tmp_path, fake_rpicam), "--slack-option", str(option)])
    out = capsys.readouterr().out
    assert code == 0 and "NG" not in out
    assert "xoxb-secret" not in out  # never echo the token


def test_a_bad_config_exits_with_2(tmp_path, capsys):
    bad = tmp_path / "camera.toml"
    bad.write_text("[exposure]\nmax_tests = 99\n", encoding="utf-8")
    assert main(["show-config", "--config", str(bad)]) == 2


def test_show_config_prints_the_effective_values(tmp_path, fake_rpicam, capsys):
    assert main(["show-config", "--config", write_config(tmp_path, fake_rpicam)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["name"] == "cam-cli" and data["exposure"]["target_luma"] == 115.0


def test_dry_run_prints_the_first_camera_command(tmp_path, fake_rpicam, capsys):
    assert main(["capture", "--dry-run", "--config", write_config(tmp_path, fake_rpicam)]) == 0
    out = capsys.readouterr().out
    assert str(fake_rpicam) in out and "--output" in out and "auto exposure" in out
    assert not (tmp_path / "state" / "state.json").exists()  # a dry run changes nothing


def test_capture_end_to_end_without_upload(tmp_path, fake_rpicam, capsys):
    config = write_config(tmp_path, fake_rpicam)
    keep = tmp_path / "last.jpg"
    code = main(["capture", "--config", config, "--no-upload", "--keep", str(keep)])
    assert code == 0
    assert keep.exists() and keep.stat().st_size > 0
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    assert state["last_capture_epoch"] > 0 and state["last_total_s"] > 0
    assert main(["status", "--config", config]) == 0
    out = capsys.readouterr().out
    assert "last posted:          never" in out and "last capture:         0 min ago" in out


def test_capture_reports_a_camera_failure(tmp_path, fake_rpicam, monkeypatch):
    monkeypatch.setenv("FAKE_FAIL", "1")
    config = write_config(tmp_path, fake_rpicam)
    assert main(["capture", "--config", config, "--no-upload"]) == 1
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    assert state["consecutive_failures"] == 1 and "camera" in state["last_error"]


def test_focus_scan_recommends_the_sharpest_position(tmp_path, fake_rpicam, capsys):
    config = write_config(tmp_path, fake_rpicam)
    assert main(["focus-scan", "--config", config, "--start", "0", "--end", "2", "--step", "0.5"]) == 0
    out = capsys.readouterr().out
    assert "lens_position = 1" in out


def test_focus_scan_rejects_a_bad_range(tmp_path, fake_rpicam):
    config = write_config(tmp_path, fake_rpicam)
    assert main(["focus-scan", "--config", config, "--start", "3", "--end", "1"]) == 2


def test_version(capsys):
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert "0.1.0" in capsys.readouterr().out
