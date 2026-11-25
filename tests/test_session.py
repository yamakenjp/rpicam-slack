"""End-to-end behaviour of one run, against the scene simulator."""

import threading
from pathlib import Path

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
        r.clock.advance(deadline - r.clock() - 30)  # it took over a minute, and 30 s are left

    monkeypatch.setattr(r.uploader, "upload", slow)
    assert run(r) == EXIT_OK
    assert len(r.uploader.uploads) == 1  # only the fresh picture
    assert [item.filename for item in r.spool.pending()] == ["old.jpg"]


def test_a_quick_upload_lets_the_backlog_drain_in_a_tight_budget(rig, tmp_path, monkeypatch):
    r = rig(DAY, budget={"max_total_s": 40.0, "upload_reserve_s": 20.0})
    image = tmp_path / "image.jpg"
    image.write_bytes(b"frame")
    r.spool.add(image, "old", "old.jpg", 1)
    upload = r.uploader.upload
    left = []

    def quick(path, comment, filename, *, deadline):
        r.clock.advance(3)  # each upload takes 3 s
        upload(path, comment, filename, deadline=deadline)
        left.append(deadline - r.clock())

    monkeypatch.setattr(r.uploader, "upload", quick)
    assert run(r) == EXIT_OK
    assert left[0] < r.cfg.slack.timeout_s + 5  # less than a whole timeout was left after the fresh one
    assert r.uploader.uploads[-1] == ("old.jpg", "[late] old")
    assert r.spool.pending() == []


@pytest.mark.parametrize(
    "durations, left, resent",
    [
        ([20.0, 1.0], 39.0, 0),  # twice the 20 s upload is 40 s
        ([20.0, 1.0], 41.0, 1),
        ([0.5, 0.5], 4.0, 0),  # never with less than five seconds
        ([0.5, 0.5], 6.0, 1),
        ([1.0, 10.0, 1.0], 25.0, 1),  # the 10 s re-send leaves 15 s: less than twice the slowest upload
        ([10.0, 1.0, 1.0], 20.5, 1),  # a quick re-send does not make the 10 s upload forgotten
    ],
)
def test_a_resend_needs_twice_the_slowest_upload_and_at_least_five_seconds(
    rig, tmp_path, monkeypatch, durations, left, resent
):
    """Upload i takes durations[i] s, and ``left`` s of the budget remain after the fresh one."""
    r = rig(DAY)
    image = tmp_path / "image.jpg"
    image.write_bytes(b"frame")
    for i in range(len(durations) - 1):  # one waiting picture for each re-send that is timed
        r.spool.add(image, f"old{i}", f"old{i}.jpg", i + 1)
    upload, add = r.uploader.upload, r.spool.add
    deadline = r.clock() + r.cfg.budget.max_total_s
    calls = []

    def staged(*args, **kwargs):  # the run stages the fresh picture just before it uploads it
        item = add(*args, **kwargs)
        r.clock.now = deadline - left - durations[0]
        return item

    def timed(path, comment, filename, *, deadline):
        r.clock.advance(durations[len(calls)])
        calls.append(filename)
        upload(path, comment, filename, deadline=deadline)

    monkeypatch.setattr(r.spool, "add", staged)
    monkeypatch.setattr(r.uploader, "upload", timed)
    assert run(r) == EXIT_OK
    assert len(calls) - 1 == resent


def test_repeated_rate_limits_still_raise_the_alert(rig, monkeypatch):
    r = rig(DAY)

    def rate_limit(*args, **kwargs):
        raise UploadError("429", retry_after_s=30)

    monkeypatch.setattr(r.uploader, "upload", rate_limit)
    assert run(r) == EXIT_UPLOAD_DEFERRED
    r.advance(900)  # the 30 s wait is long over, and Slack answers 429 again
    assert run(r) == EXIT_UPLOAD_DEFERRED
    assert len(r.uploader.alerts) == 1 and "2 run(s) in a row" in r.uploader.alerts[0]


def test_a_wait_that_ends_during_the_capture_does_not_silence_the_alert(rig, monkeypatch):
    r = rig(DAY)
    asked = []

    def rate_limit(*args, **kwargs):
        asked.append(r.clock())
        raise UploadError("429", retry_after_s=901)

    monkeypatch.setattr(r.uploader, "upload", rate_limit)
    assert run(r) == EXIT_UPLOAD_DEFERRED
    r.advance(900)  # one second of the wait is left when the next run starts, none when it posts
    assert run(r) == EXIT_UPLOAD_DEFERRED
    assert len(asked) == 2  # Slack was asked again and answered 429 again
    assert len(r.uploader.alerts) == 1 and "2 run(s) in a row" in r.uploader.alerts[0]


def test_a_failed_upload_that_used_up_the_budget_still_raises_the_alert(rig, monkeypatch):
    r = rig(NIGHT)
    room = []

    def stalled(path, comment, filename, *, deadline):
        r.clock.advance(max(0.0, deadline - r.clock()))  # the JPEG does not get through: the budget is gone
        raise UploadError("upload failed after 3 attempts: TimeoutError: timed out")

    def alert(text, *, deadline):
        room.append(deadline - r.clock())
        r.uploader.alerts.append(text)
        return True

    monkeypatch.setattr(r.uploader, "upload", stalled)
    monkeypatch.setattr(r.uploader, "alert", alert)
    for _ in range(3):
        assert run(r) == EXIT_UPLOAD_DEFERRED
        r.advance(900)
    assert len(r.uploader.alerts) == 1 and "2 run(s) in a row" in r.uploader.alerts[0]
    assert room == [pytest.approx(5.0)]  # the short message gets time of its own


def test_an_alert_with_budget_left_keeps_the_deadline_of_the_run(rig, monkeypatch):
    r = rig(DAY)
    r.camera.fail_runs = 1_000
    room = []

    def alert(text, *, deadline):
        room.append(deadline - r.clock())
        return True

    monkeypatch.setattr(r.uploader, "alert", alert)
    assert run(r) == EXIT_CAMERA
    r.advance(900)
    started = r.clock()
    assert run(r) == EXIT_CAMERA
    left = r.cfg.budget.max_total_s - (r.clock() - started)
    assert left > 60 and room == [pytest.approx(left)]  # neither cut to five seconds nor given five more


def test_a_file_that_cannot_be_set_aside_does_not_block_the_queue(rig, tmp_path, monkeypatch):
    r = rig(DAY)
    image = tmp_path / "image.jpg"
    image.write_bytes(b"frame")
    for i, filename in enumerate(["refused.jpg", "ok.jpg"]):
        r.spool.add(image, filename, filename, i)
    upload = r.uploader.upload

    def refuse(path, comment, filename, *, deadline):
        if filename == "refused.jpg":
            raise UploadFileError("file rejected: slack error file_type_not_allowed", permanent=True)
        upload(path, comment, filename, deadline=deadline)

    def read_only(item):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(r.uploader, "upload", refuse)
    monkeypatch.setattr(r.spool, "reject", read_only)
    assert run(r) == EXIT_OK
    assert r.uploader.uploads[-1] == ("ok.jpg", "[late] ok.jpg")


def test_a_cleanup_error_before_the_copy_does_not_cost_the_fresh_picture(rig, monkeypatch):
    r = rig(DAY)

    def broken(**kwargs):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(r.spool, "prune", broken)
    r.uploader.fail_uploads = 1
    assert run(r) == EXIT_UPLOAD_DEFERRED  # the fresh picture is in the spool all the same
    assert [item.filename.startswith("cam-test_") for item in r.spool.pending()] == [True]


def test_a_file_that_cannot_be_deleted_does_not_stop_the_spool_from_being_trimmed(rig, tmp_path, monkeypatch):
    r = rig(DAY, paths={"spool_max_files": 3})
    image = tmp_path / "image.jpg"
    image.write_bytes(b"frame")
    r.spool.reject(r.spool.add(image, "refused", "refused.jpg", 2))
    unlink = Path.unlink

    def stuck(self, missing_ok=False):
        if self.name.startswith("2-"):  # the refused image cannot be deleted
            raise OSError(5, "Input/output error")
        return unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", stuck)
    r.uploader.fail_uploads = 99
    for _ in range(8):
        assert run(r) == EXIT_UPLOAD_DEFERRED
        r.advance(900)
    assert len(r.spool.pending()) <= 3


def test_a_failed_upload_keeps_the_fresh_picture_even_when_the_backlog_looks_newer(rig, tmp_path):
    r = rig(DAY, paths={"spool_max_files": 2})
    image = tmp_path / "image.jpg"
    image.write_bytes(b"frame")
    for i in range(2):  # taken while the clock was wrong: their names sort after today's picture
        r.spool.add(image, f"later{i}", f"later{i}.jpg", 4_000_000_000 + i)
    r.uploader.fail_uploads = 1
    assert run(r) == EXIT_UPLOAD_DEFERRED
    fresh, *backlog = [item.filename for item in r.spool.pending()]
    assert fresh.startswith("cam-test_") and backlog == ["later1.jpg"]


def test_a_long_outage_keeps_the_spool_bounded_and_the_newest_picture(rig):
    r = rig(DAY, paths={"spool_max_files": 3})
    r.uploader.fail_uploads = 99
    for _ in range(8):
        assert run(r) == EXIT_UPLOAD_DEFERRED
        pending = r.spool.pending()
        newest = max(path.name for path in r.spool.directory.glob("*.jpg"))  # names start with the time
        assert len(pending) <= 3 and newest in [item.image.name for item in pending]
        assert len(list(r.spool.directory.iterdir())) <= 6
        r.advance(900)


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
