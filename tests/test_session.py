"""End-to-end behaviour of one run, against the scene simulator."""

import threading

import pytest

from rpicam_slack.session import (
    EXIT_CAMERA,
    EXIT_OK,
    EXIT_UPLOAD_DEFERRED,
    EXIT_UPLOAD_FAILED,
    CaptureSession,
    run_locked,
)
from rpicam_slack.slack import UploadError, UploadFileError
from rpicam_slack.state import locked

DAY = 100.0  # bright: ~2 ms equivalent exposure
NIGHT = 0.02  # needs ~9 s equivalent exposure
PITCH_BLACK = 0.0005  # beyond what the hardware can do


def run(r) -> int:
    return CaptureSession(r.cfg, r.deps()).run()


def kinds(r) -> list[str]:
    return [s.kind for s in r.camera.shots]


def test_a_bright_scene_needs_one_test_and_lands_on_target(rig):
    r = rig(DAY)
    assert run(r) == EXIT_OK
    assert kinds(r) == ["test", "final"]
    assert r.camera.shots[0].shutter_us is None  # the first test is the camera's auto exposure
    assert abs(r.camera.last_luma - r.cfg.exposure.target_luma) <= r.cfg.exposure.tolerance
    assert len(r.uploader.uploads) == 1


def test_a_dark_scene_takes_a_few_tests_then_converges(rig):
    r = rig(NIGHT)
    assert run(r) == EXIT_OK
    tests = kinds(r).count("test")
    assert 2 <= tests <= r.cfg.exposure.max_tests
    final = r.camera.shots[-1]
    assert final.kind == "final"
    assert final.shutter_us > 1_000_000  # a long exposure, found by measuring (not by a schedule)
    assert abs(r.camera.last_luma - r.cfg.exposure.target_luma) <= r.cfg.exposure.tolerance


def test_the_next_run_starts_from_the_last_exposure(rig):
    r = rig(NIGHT)
    assert run(r) == EXIT_OK
    first_run = len(r.camera.shots)
    r.advance(15 * 60)
    assert run(r) == EXIT_OK
    second = r.camera.shots[first_run:]
    assert second[0].kind == "test" and second[0].shutter_us is not None  # not auto exposure
    assert [s.kind for s in second].count("test") == 1  # one test is enough when it is stable
    assert abs(r.camera.last_luma - r.cfg.exposure.target_luma) <= r.cfg.exposure.tolerance


def test_a_stale_exposure_is_not_trusted(rig):
    r = rig(NIGHT)
    run(r)
    count = len(r.camera.shots)
    r.advance(3 * 3600)  # older than state_max_age_min
    run(r)
    assert r.camera.shots[count].shutter_us is None  # back to the camera's auto exposure


def test_a_scene_beyond_the_hardware_is_still_posted(rig):
    r = rig(PITCH_BLACK)
    assert run(r) == EXIT_OK
    final = r.camera.shots[-1]
    assert final.shutter_us == 12_000_000 and final.gain == pytest.approx(16.0)
    assert kinds(r).count("test") <= r.cfg.exposure.max_tests
    assert len(r.uploader.uploads) == 1
    assert "profile=night" in r.uploader.uploads[0][1]


def test_a_tight_budget_shortens_the_final_shot_instead_of_timing_out(rig):
    r = rig(PITCH_BLACK, budget={"max_total_s": 60, "upload_reserve_s": 20})
    started = r.clock.now
    assert run(r) == EXIT_OK  # FakeCamera fails the test if a shot would exceed its timeout
    assert r.camera.shots[-1].shutter_us < 12_000_000
    assert r.clock.now - started <= 60 - 20
    assert len(r.uploader.uploads) == 1


def test_timeouts_are_derived_from_the_shutter_not_fixed(rig):
    """Regression: a fixed 45 s limit killed 12 s exposures (53 s needed) and stopped all posts."""
    r = rig(PITCH_BLACK)
    assert run(r) == EXIT_OK
    assert r.camera.timeouts[-1] > 60  # the final shot got a timeout fitting a 12 s exposure
    assert r.camera.timeouts[0] < r.camera.timeouts[-1]  # while tests stay on a short leash


def test_it_learns_how_slow_the_camera_really_is(rig):
    r = rig(NIGHT)
    r.camera.frames = 9.0  # much slower than the configured 5 frames per shot
    assert run(r) == EXIT_OK
    assert r.store.load().frame_ratio > 7
    first = r.camera.timeouts[-1]
    r.advance(900)
    assert run(r) == EXIT_OK
    assert r.camera.timeouts[-1] >= first  # the estimate grew with the observation


def test_camera_failures_alert_once_the_threshold_is_reached(rig):
    r = rig(DAY, slack={"alert_after_failures": 2, "alert_min_interval_min": 60})
    r.camera.fail_runs = 1_000
    assert run(r) == EXIT_CAMERA
    assert r.uploader.alerts == []  # one failure is not worth waking anyone
    r.advance(900)
    assert run(r) == EXIT_CAMERA
    assert len(r.uploader.alerts) == 1
    assert "camera failure" in r.uploader.alerts[0] and "cam-test" in r.uploader.alerts[0]
    r.advance(900)
    run(r)
    assert len(r.uploader.alerts) == 1  # rate limited
    r.advance(3600)
    run(r)
    assert len(r.uploader.alerts) == 2
    assert r.store.load().consecutive_failures == 4


def test_recovery_is_announced_once(rig):
    r = rig(DAY, slack={"alert_after_failures": 2})
    r.camera.fail_runs = 1_000
    run(r)
    r.advance(900)
    run(r)
    r.camera.fail_runs = 0
    r.advance(900)
    assert run(r) == EXIT_OK
    assert "recovered after 2" in r.uploader.alerts[-1]
    state = r.store.load()
    assert state.consecutive_failures == 0 and state.last_alert_epoch == 0.0
    alerts = len(r.uploader.alerts)
    r.advance(900)
    run(r)
    assert len(r.uploader.alerts) == alerts


def test_a_failed_upload_is_kept_and_sent_with_the_next_run(rig):
    r = rig(DAY)
    r.uploader.fail_uploads = 1
    assert run(r) == EXIT_UPLOAD_DEFERRED
    assert len(r.spool.pending()) == 1 and r.uploader.uploads == []
    assert r.store.load().consecutive_failures == 1

    r.advance(900)
    assert run(r) == EXIT_OK
    assert len(r.uploader.uploads) == 2  # the new image, then the spooled one
    assert r.uploader.uploads[1][1].startswith("[late] ")
    assert r.spool.pending() == []
    assert r.store.load().consecutive_failures == 0


def test_without_an_uploader_nothing_is_posted_or_spooled(rig, tmp_path):
    r = rig(DAY)
    keep = tmp_path / "out" / "last.jpg"
    assert CaptureSession(r.cfg, r.deps(uploader=None, keep_path=keep)).run() == EXIT_OK
    assert keep.exists()
    assert r.spool.pending() == [] and r.store.load().last_capture_epoch > 0


def test_the_comment_template_fields(rig):
    r = rig(DAY)
    template = "{camera}|{profile}|{shutter}|{gain}|{luma}|{tests}|{lux}|{timestamp}"
    assert CaptureSession(r.cfg, r.deps(comment_template=template)).run() == EXIT_OK
    camera, profile, shutter, gain, luma, tests, lux, timestamp = r.uploader.uploads[0][1].split("|")
    assert (camera, profile, tests, lux) == ("cam-test", "day", "1", "3.00")
    assert shutter.startswith("1/") and float(gain) >= 1.0
    assert timestamp.startswith("2026-11-24 ") and timestamp.endswith("+0900")
    assert r.uploader.uploads[0][0].startswith("cam-test_20261124_")


def test_an_unreadable_final_image_counts_as_a_camera_failure(rig):
    r = rig(DAY)

    def broken(path):
        raise ValueError("not an image")

    assert CaptureSession(r.cfg, r.deps(measure=broken)).run() == EXIT_CAMERA
    assert r.uploader.uploads == []


def test_a_second_run_is_skipped_while_one_is_in_progress(rig):
    r = rig(DAY)
    with locked(r.cfg.paths.state_path / "lock") as acquired:
        assert acquired
        assert run_locked(r.cfg, r.deps()) == EXIT_OK
        assert r.camera.shots == []
    assert run_locked(r.cfg, r.deps()) == EXIT_OK
    assert r.camera.shots


def test_a_failed_upload_does_not_forget_the_exposure(rig):
    r = rig(NIGHT)
    r.uploader.fail_uploads = 1
    assert run(r) == EXIT_UPLOAD_DEFERRED
    count = len(r.camera.shots)
    r.advance(900)
    assert run(r) == EXIT_OK
    assert r.camera.shots[count].shutter_us is not None  # still starts from the known exposure


def test_a_configured_temporary_directory_is_created(rig, tmp_path):
    r = rig(DAY, paths={"tmp_dir": str(tmp_path / "does" / "not" / "exist")})
    assert run(r) == EXIT_OK


def test_a_longer_preview_for_long_exposures_stays_within_the_budget(rig):
    r = rig(NIGHT, exposure={"final_preview_frames": 1.5}, budget={"max_total_s": 150})
    assert run(r) == EXIT_OK  # FakeCamera fails the test if a shot would exceed its timeout
    final = r.camera.shots[-1]
    assert final.preview_ms == round(1.5 * final.shutter_us / 1000)
    assert final.preview_ms > r.cfg.exposure.final_preview_ms


def test_lights_switch_on_after_a_night_capture(rig):
    r = rig(NIGHT)
    assert run(r) == EXIT_OK
    r.advance(900)
    r.camera.k = 20
    start = len(r.camera.shots)
    assert run(r) == EXIT_OK
    trials = r.camera.shots[start:-1]
    totals = [s.shutter_us / 1e6 * s.gain for s in trials]
    assert len(trials) == 4
    assert totals == sorted(totals, reverse=True)
    assert r.store.load().last_total_s < totals[-1]
    assert r.camera.last_luma < 200


def test_interrupted_current_upload_remains_available_to_next_run(rig, monkeypatch):
    r = rig(DAY)

    def stop_after_staging(path, comment, filename, *, deadline):
        [item] = r.spool.pending()
        assert (item.image, item.comment, item.filename) == (path, comment, filename)
        assert item.image.stat().st_size > 0 and deadline > r.clock()
        raise KeyboardInterrupt

    with monkeypatch.context() as m:
        m.setattr(r.uploader, "upload", stop_after_staging)
        with pytest.raises(KeyboardInterrupt):
            run(r)
    [interrupted] = r.spool.pending()
    r.advance(900)
    assert run(r) == EXIT_OK
    assert r.uploader.uploads[-1] == (interrupted.filename, f"[late] {interrupted.comment}")
    assert r.spool.pending() == []


def test_current_success_preserves_a_full_backlog(rig, tmp_path):
    r = rig(DAY, paths={"spool_max_files": 1})
    image = tmp_path / "old.jpg"
    image.write_bytes(b"old")
    r.spool.add(image, "old comment", "old.jpg", 1)
    assert run(r) == EXIT_OK
    assert r.uploader.uploads[-1] == ("old.jpg", "[late] old comment")


def test_upload_failure_status_requires_a_durable_copy(rig, monkeypatch):
    r = rig(DAY, paths={"spool_max_files": 0})
    r.uploader.fail_uploads = 1
    assert run(r) == EXIT_UPLOAD_FAILED
    assert r.spool.pending() == []
    assert r.store.load().consecutive_failures == 1

    def full_disk(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(r.spool, "add", full_disk)
    r.uploader.fail_uploads = 1
    assert run(r) == EXIT_UPLOAD_FAILED
    assert run(r) == EXIT_OK  # the temporary JPEG can still be sent when staging is unavailable


def test_rate_limit_survives_a_restart_and_applies_to_new_captures(rig, monkeypatch):
    r = rig(DAY)

    def rate_limit(*args, **kwargs):
        raise UploadError("429", retry_after_s=1500)

    with monkeypatch.context() as m:
        m.setattr(r.uploader, "upload", rate_limit)
        assert run(r) == EXIT_UPLOAD_DEFERRED
    assert r.store.load().upload_not_before_epoch > 0
    r.advance(900)
    assert run(r) == EXIT_UPLOAD_DEFERRED
    assert r.uploader.uploads == []
    assert len(r.spool.pending()) == 2
    r.advance(900)
    assert run(r) == EXIT_OK
    assert len(r.uploader.uploads) == 3 and r.spool.pending() == []
    assert r.store.load().upload_not_before_epoch == 0


def test_bad_spooled_files_do_not_block_a_later_good_file(rig, tmp_path, monkeypatch):
    r = rig(DAY)
    image = tmp_path / "image.jpg"
    image.write_bytes(b"frame")
    for i, filename in enumerate(["bad1.jpg", "bad2.jpg", "bad3.jpg", "ok.jpg"]):
        r.spool.add(image, filename, filename, i)
    upload = r.uploader.upload

    def reject_bad(path, comment, filename, *, deadline):
        if filename.startswith("bad"):
            raise UploadFileError("unreadable")
        upload(path, comment, filename, deadline=deadline)

    monkeypatch.setattr(r.uploader, "upload", reject_bad)
    assert run(r) == EXIT_OK
    assert r.uploader.uploads[-1] == ("ok.jpg", "[late] ok.jpg")
    assert [it.filename for it in r.spool.pending()] == ["bad1.jpg", "bad2.jpg", "bad3.jpg"]


def test_a_file_that_slack_refuses_is_offered_only_once(rig, tmp_path, monkeypatch):
    r = rig(DAY)
    image = tmp_path / "image.jpg"
    image.write_bytes(b"frame")
    for i, filename in enumerate(["refused.jpg", "ok.jpg"]):
        r.spool.add(image, filename, filename, i)
    offered = []
    upload = r.uploader.upload

    def refuse(path, comment, filename, *, deadline):
        offered.append(filename)
        if filename == "refused.jpg":
            raise UploadFileError("file rejected: slack error file_type_not_allowed", permanent=True)
        upload(path, comment, filename, deadline=deadline)

    monkeypatch.setattr(r.uploader, "upload", refuse)
    assert run(r) == EXIT_OK
    r.advance(900)
    assert run(r) == EXIT_OK
    assert offered.count("refused.jpg") == 1 and offered.count("ok.jpg") == 1
    assert r.spool.pending() == []


def test_no_resend_starts_without_the_time_for_one_upload(rig, tmp_path, monkeypatch):
    r = rig(DAY)
    image = tmp_path / "image.jpg"
    image.write_bytes(b"frame")
    r.spool.add(image, "old", "old.jpg", 1)
    upload = r.uploader.upload

    def slow(path, comment, filename, *, deadline):
        upload(path, comment, filename, deadline=deadline)
        r.clock.advance(deadline - r.clock() - r.cfg.slack.timeout_s)  # less than timeout_s + 5 is left

    monkeypatch.setattr(r.uploader, "upload", slow)
    assert run(r) == EXIT_OK
    assert len(r.uploader.uploads) == 1  # only the fresh picture
    assert [item.filename for item in r.spool.pending()] == ["old.jpg"]


def test_waiting_for_slack_is_capped_and_raises_no_alert(rig, monkeypatch):
    r = rig(DAY)

    def rate_limit(*args, **kwargs):
        raise UploadError("429", retry_after_s=86400)

    with monkeypatch.context() as m:
        m.setattr(r.uploader, "upload", rate_limit)
        assert run(r) == EXIT_UPLOAD_DEFERRED
    state = r.store.load()
    assert 0 < state.upload_not_before_epoch - r.deps().wall().timestamp() <= 3600
    r.advance(900)
    assert run(r) == EXIT_UPLOAD_DEFERRED  # still waiting: counted as a failure, but Slack is left alone
    assert r.store.load().consecutive_failures == 2 and r.uploader.alerts == []

    state = r.store.load()
    state.upload_not_before_epoch += 10 * 86400  # as if the clock had since been set back by ten days
    r.store.save(state)
    assert run(r) == EXIT_OK


def test_a_capture_waits_for_a_focus_scan_shot_instead_of_being_skipped(rig):
    r = rig(DAY)
    path = r.cfg.paths.state_path / "lock"
    holding, release = threading.Event(), threading.Event()

    def scan_shot():
        with locked(path):
            holding.set()
            release.wait(5)

    thread = threading.Thread(target=scan_shot)
    thread.start()
    assert holding.wait(5)
    threading.Timer(0.2, release.set).start()
    assert run_locked(r.cfg, r.deps(), 5) == EXIT_OK
    thread.join()
    assert r.camera.shots  # taken as soon as the shot in progress had finished
