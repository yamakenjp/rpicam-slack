"""One capture run.

    lock -> test shot(s) -> final shot -> post to Slack -> (flush spool) -> save state

The test shots are the heart of it: instead of following the sun, each run measures the
scene. The first test starts from the last exposure when it is recent (usually one test is
enough), otherwise from the camera's own auto exposure. Dark scenes may need several tests.
Every camera timeout is derived from a wall-clock budget, and the final shot is shortened
(with more gain) rather than allowed to run out of time.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .camera import Camera, CameraError, Shot, ShotResult
from .config import DEFAULT_COMMENT, Config
from .exposure import (
    Exposure,
    ImageStats,
    command_timeout_s,
    effective_target,
    final_preview_ms,
    is_converged,
    label,
    max_final_shutter_for,
    measure_image,
    next_total,
    plan_exposure,
    shot_cost_s,
)
from .slack import Uploader, UploadError, UploadFileError
from .state import Spool, SpoolItem, State, StateStore, locked

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_CAMERA = 1
EXIT_CONFIG = 2
EXIT_UPLOAD_FAILED = 3  # upload failed and no durable copy is available for retry
EXIT_UPLOAD_DEFERRED = 75  # EX_TEMPFAIL: the image is spooled and will be retried
MAX_SPOOL_FLUSH = 3
ALERT_MIN_S = 5.0  # a failure alert gets this long even when the failure used up the budget
FLUSH_MIN_S = 5.0  # never start a re-send with less of the budget left than this
MAX_UPLOAD_WAIT_S = 3600.0  # a Retry-After (or a clock that jumped) never holds uploads back for longer
LOCK_WAIT_S = 30.0  # how long the timer waits for a focus-scan shot in progress before it gives up
AE_ASSUMED_SHUTTER_S = 0.07  # auto exposure stays near one frame time


@dataclass
class Deps:
    camera: Camera
    store: StateStore
    spool: Spool
    uploader: Uploader | None = None  # None: capture only, never upload
    comment_template: str = DEFAULT_COMMENT
    measure: Callable[[Path], ImageStats] = measure_image
    clock: Callable[[], float] = time.monotonic
    wall: Callable[[], datetime] = lambda: datetime.now().astimezone()
    embed_exif: Callable[[Path, str], None] | None = None
    keep_path: Path | None = None  # copy the final image here (manual checks)


@dataclass(frozen=True)
class Trial:
    total: float  # equivalent exposure actually used
    stats: ImageStats


def test_shot(cfg: Config, planned: Exposure | None) -> Shot:
    """A small, fast test frame: auto exposure when ``planned`` is None, else a manual exposure."""
    ex = cfg.exposure
    return Shot(
        "test",
        ex.test_width,
        ex.test_height,
        ex.test_quality,
        shutter_us=None if planned is None else planned.shutter_us,
        gain=None if planned is None else planned.gain,
        preview_ms=ex.test_preview_ms if planned is None else None,
        lens_position=cfg.camera.lens_position,
        denoise="cdn_off",  # brightness does not depend on denoising; skip the extra ISP work
    )


def format_shutter(seconds: float) -> str:
    if seconds >= 1.0:
        return f"{seconds:.1f}s"
    return f"1/{round(1 / seconds)}s" if seconds > 0 else "0s"


class CaptureSession:
    def __init__(self, cfg: Config, deps: Deps) -> None:
        self.cfg = cfg
        self.d = deps
        self.state = State()
        self._told_to_wait = False  # Slack answered this very run with a Retry-After
        self._upload_took_s = 0.0  # the slowest successful upload of this run

    # ----------------------------------------------------------------- helpers

    def _frames(self) -> float:
        """Frames a manual-exposure still takes: the configured value, raised by what we observed."""
        base = self.cfg.budget.frames_per_shot
        return max(base, self.state.frame_ratio * 1.15) if self.state.frame_ratio else base

    def _measure(self, image: Path) -> ImageStats:
        try:
            return self.d.measure(image)
        except (OSError, ValueError) as exc:
            raise CameraError(f"unreadable image {image.name}: {exc}") from exc

    def _save_state(self) -> None:
        try:
            self.d.store.save(self.state)
        except OSError as exc:  # a full disk must not turn a good run into a failure
            log.warning("could not save the state: %s", exc)

    def _remaining(self, deadline: float) -> float:
        return deadline - self.d.clock()

    # ------------------------------------------------------------------- tests

    def _converge(self, tmp: Path, deadline: float, now_epoch: float) -> list[Trial]:
        cfg, ex, b = self.cfg, self.cfg.exposure, self.cfg.budget
        trials: list[Trial] = []
        total = self.state.fresh_total(now_epoch, ex.state_max_age_min)
        if total is not None:
            log.info("starting from the last exposure (%.4fs equivalent)", total)
        previous: Exposure | None = None

        for index in range(ex.max_tests):
            if total is None:
                planned = None
                shot = test_shot(cfg, None)
                cost = shot_cost_s(AE_ASSUMED_SHUTTER_S, ex.test_preview_ms, b, self._frames())
            else:
                planned = plan_exposure(total, ex, max_shutter_s=ex.test_max_shutter_s)
                if previous is not None and (planned.shutter_us, planned.gain) == (
                    previous.shutter_us, previous.gain,
                ):  # fmt: skip
                    log.info("test exposure cannot change any further; stopping the tests")
                    break
                shot = test_shot(cfg, planned)
                cost = shot_cost_s(planned.shutter_s, None, b, self._frames())

            need = cost + b.upload_reserve_s
            if trials and total is not None:  # a final shot must still fit afterwards
                final_guess = plan_exposure(total, ex, max_shutter_s=ex.max_shutter_s)
                need += shot_cost_s(
                    final_guess.shutter_s, final_preview_ms(ex, final_guess.shutter_s), b, self._frames()
                )
            if need > self._remaining(deadline):
                log.warning("not enough time budget for another test; using what we have")
                break

            timeout = command_timeout_s(cost, b)
            result = self.d.camera.shoot(shot, tmp / f"test{index + 1}.jpg", timeout_s=timeout)
            stats = self._measure(result.image)
            used = result.total_exposure_s
            if used is None:
                if planned is None:
                    raise CameraError("the camera reported no exposure for the auto test shot")
                used = planned.total
            trials.append(Trial(used, stats))
            log.info(
                "test %d: %s equivalent %.4fs -> luma %.0f (target %.0f)",
                index + 1, "auto" if planned is None else "manual", used, stats.luma,
                effective_target(ex),
            )  # fmt: skip
            if is_converged(stats, ex):
                break
            total = next_total(used, stats, ex)
            previous = planned
        return trials

    # ------------------------------------------------------------------- final

    def _final(
        self, tmp: Path, trials: list[Trial], deadline: float, now_epoch: float
    ) -> tuple[ShotResult, Exposure, bool]:
        cfg, ex, b = self.cfg, self.cfg.exposure, self.cfg.budget
        if trials:
            # At saturation several frames can tie while exposure is still moving toward the target.
            best = min(reversed(trials), key=lambda t: abs(t.stats.luma - effective_target(ex)))
            final_total = next_total(best.total, best.stats, ex)
        else:
            hint = self.state.fresh_total(now_epoch, ex.state_max_age_min)
            if hint is None:
                raise CameraError("no time budget left to measure the scene")
            final_total = hint
            log.warning("no test shot fit into the budget; using the last exposure unchecked")

        planned = plan_exposure(final_total, ex, max_shutter_s=ex.max_shutter_s)
        frames = self._frames()
        usable = self._remaining(deadline) - b.upload_reserve_s
        preview_ms = final_preview_ms(ex, planned.shutter_s)
        cost = shot_cost_s(planned.shutter_s, preview_ms, b, frames)
        degraded = False
        if cost > usable:
            allowed = max_final_shutter_for(usable, ex, b, frames)
            if allowed < ex.min_shutter_s:
                raise CameraError("time budget exhausted before the final shot")
            planned = plan_exposure(final_total, ex, max_shutter_s=allowed)
            preview_ms = final_preview_ms(ex, planned.shutter_s)
            cost = shot_cost_s(planned.shutter_s, preview_ms, b, frames)
            degraded = True
            log.warning(
                "final shot shortened to %s (gain %.2f) to fit the time budget",
                format_shutter(planned.shutter_s), planned.gain,
            )  # fmt: skip
        if planned.limited:
            log.warning("exposure limit reached (%s): the image may be too %s",
                        planned.limited, planned.limited)  # fmt: skip

        shot = Shot(
            "final", cfg.camera.width, cfg.camera.height, cfg.camera.quality,
            shutter_us=planned.shutter_us, gain=planned.gain, preview_ms=preview_ms,
            lens_position=cfg.camera.lens_position,
            autofocus_on_capture=cfg.camera.lens_position is None,
        )  # fmt: skip
        timeout = min(command_timeout_s(cost, b), max(cost, self._remaining(deadline)))
        log.info(
            "final shot: shutter %s gain %.2f (equivalent %.4fs)",
            format_shutter(planned.shutter_s), planned.gain, planned.total,
        )  # fmt: skip
        result = self.d.camera.shoot(shot, tmp / "final.jpg", timeout_s=timeout)
        return result, planned, degraded

    def _learn_frame_ratio(self, result: ShotResult, planned: Exposure) -> None:
        if planned.shutter_s < 1.0:
            return
        preview = final_preview_ms(self.cfg.exposure, planned.shutter_s) / 1000
        ratio = min(max((result.elapsed_s - preview) / planned.shutter_s, 1.0), 30.0)
        old = self.state.frame_ratio
        self.state.frame_ratio = ratio if old == 0 else 0.5 * old + 0.5 * ratio

    # --------------------------------------------------------------- reporting

    def _comment(
        self, now: datetime, total: float, stats: ImageStats, planned: Exposure,
        result: ShotResult, tests: int,
    ) -> str:  # fmt: skip
        fields = {
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S %z"),
            "profile": label(total, self.cfg.exposure),
            "camera": self.cfg.camera_name,
            "shutter": format_shutter(planned.shutter_s),
            "gain": f"{planned.gain:.2f}",
            "luma": f"{stats.luma:.0f}",
            "tests": str(tests),
            "lux": "-" if result.lux is None else f"{result.lux:.2f}",
        }
        return self.d.comment_template.format(**fields)

    def _alert(self, text: str, deadline: float) -> None:
        s = self.cfg.slack
        if self.d.uploader is None:
            return
        epoch = self.d.wall().timestamp()
        if self.state.consecutive_failures < s.alert_after_failures:
            return
        if epoch - self.state.last_alert_epoch < s.alert_min_interval_min * 60:
            return
        if self._upload_wait_s() > 0 and not self._told_to_wait:
            return  # an earlier run was asked to wait and the time is not up: no API calls until then
        # An upload that stalls uses the budget to the end, and that is when the alert matters most.
        deadline = max(deadline, self.d.clock() + ALERT_MIN_S)
        if self.d.uploader.alert(f":warning: {self.cfg.camera_name}: {text}", deadline=deadline):
            self.state.last_alert_epoch = epoch
            self._save_state()

    def _fail(self, kind: str, message: str, deadline: float) -> None:
        self.state.consecutive_failures += 1
        self.state.last_error = f"{kind}: {message}"
        log.error("%s failure (%d in a row): %s", kind, self.state.consecutive_failures, message)
        self._save_state()
        self._alert(
            f"{kind} failure, {self.state.consecutive_failures} run(s) in a row: {message}",
            deadline,
        )

    def _record_retry_after(self, exc: UploadError) -> None:
        if exc.retry_after_s is not None:
            wait = min(exc.retry_after_s, MAX_UPLOAD_WAIT_S)
            self.state.upload_not_before_epoch = self.d.wall().timestamp() + wait
            self._told_to_wait = True
            self._save_state()

    def _upload_wait_s(self) -> float:
        """Seconds Slack still wants us to wait before the next upload (0: nothing to wait for)."""
        wait = self.state.upload_not_before_epoch - self.d.wall().timestamp()
        # More than the cap can only mean that the clock was set back after the wait was recorded.
        return wait if 0 < wait <= MAX_UPLOAD_WAIT_S else 0.0

    def _post(
        self, result: ShotResult, comment: str, filename: str, taken: datetime, deadline: float
    ) -> tuple[int, SpoolItem | None]:
        d = self.d
        assert d.uploader is not None
        item = None
        try:
            # Keep the in-flight frame in addition to the bounded backlog until its outcome is known.
            item = d.spool.add(result.image, comment, filename, taken.timestamp(), prune=False)
        except OSError as exc:
            log.error("could not stage %s for retry: %s", filename, exc)
        try:
            wait = self._upload_wait_s()
            if wait > 0:
                raise UploadError(f"Slack Retry-After has {wait:.0f}s remaining")
            started = d.clock()
            d.uploader.upload(item.image if item else result.image, comment, filename, deadline=deadline)
            self._upload_took_s = d.clock() - started
        except UploadError as exc:
            self._record_retry_after(exc)
            self._fail("upload", str(exc), deadline)
            if item is None:
                return EXIT_UPLOAD_FAILED, None
            try:
                d.spool.prune(keep=item.image)
            except OSError as prune_error:
                log.warning("could not trim spool: %s", prune_error)
            return EXIT_UPLOAD_DEFERRED, None
        self.state.upload_not_before_epoch = 0.0
        return EXIT_OK, item

    def _flush_spool(self, deadline: float) -> None:
        uploader = self.d.uploader
        if uploader is None:
            return
        sent = 0
        for item in self.d.spool.pending():
            # An upload that is cut off at the deadline may already have reached Slack and would be
            # sent again next time, so leave room for one: twice what an upload took in this run.
            room = max(FLUSH_MIN_S, 2 * self._upload_took_s)
            if sent >= MAX_SPOOL_FLUSH or self._remaining(deadline) < room:
                return
            started = self.d.clock()
            try:
                uploader.upload(item.image, f"[late] {item.comment}", item.filename, deadline=deadline)
            except UploadFileError as exc:
                log.warning("skipping spooled image %s: %s", item.filename, exc)
                if exc.permanent:  # never sendable: do not offer it to Slack on every run
                    try:
                        self.d.spool.reject(item)
                    except OSError as reject_error:
                        log.warning("could not set %s aside: %s", item.filename, reject_error)
                continue
            except UploadError as exc:
                self._record_retry_after(exc)
                log.warning("spooled image %s still cannot be uploaded: %s", item.filename, exc)
                return
            log.info("uploaded spooled image %s", item.filename)
            self._upload_took_s = max(self._upload_took_s, self.d.clock() - started)
            self.d.spool.remove(item)
            sent += 1

    # --------------------------------------------------------------------- run

    def run(self) -> int:
        cfg, d = self.cfg, self.d
        deadline = d.clock() + cfg.budget.max_total_s
        now = d.wall()
        self.state = d.store.load()

        if cfg.paths.tmp_dir:
            Path(cfg.paths.tmp_dir).expanduser().mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="rpicam-slack-",
            dir=(cfg.paths.tmp_dir and str(Path(cfg.paths.tmp_dir).expanduser())) or None,
        ) as tmp:
            tmpdir = Path(tmp)
            try:
                trials = self._converge(tmpdir, deadline, now.timestamp())
                result, planned, degraded = self._final(tmpdir, trials, deadline, now.timestamp())
                stats = self._measure(result.image)
            except CameraError as exc:
                self._fail("camera", str(exc), deadline)
                return EXIT_CAMERA

            now = d.wall()  # the moment the picture was taken (the run may have started a minute ago)
            total = result.total_exposure_s or planned.total
            log.info("final image: luma %.0f (target %.0f)%s", stats.luma,
                     effective_target(cfg.exposure), ", time-limited" if degraded else "")  # fmt: skip
            if stats.luma < 2 or stats.luma > 253:
                log.warning("the final image looks %s", "black" if stats.luma < 2 else "white")

            comment = self._comment(now, total, stats, planned, result, len(trials))
            filename = f"{cfg.camera_name}_{now:%Y%m%d_%H%M%S}.jpg"
            if d.embed_exif is not None:
                d.embed_exif(result.image, comment)
            if d.keep_path is not None:
                d.keep_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(result.image, d.keep_path)
            log.info("Slack comment: %s", comment)

            self._learn_frame_ratio(result, planned)
            self.state.last_capture_epoch = now.timestamp()
            self.state.last_total_s = total
            self.state.last_shutter_us = planned.shutter_us
            self.state.last_gain = planned.gain
            self.state.last_luma = stats.luma

            if d.uploader is None:
                # A trial run: remember the exposure, but never claim a post that did not happen
                # and never touch the failure counters of the real service.
                log.info("Slack upload skipped")
                self._save_state()
                return EXIT_OK

            outcome, posted = self._post(result, comment, filename, now, deadline)
            if outcome != EXIT_OK:
                return outcome

        # Uploaded. Housekeeping must never turn a successful run into a failure.
        recovered = self.state.consecutive_failures >= cfg.slack.alert_after_failures
        was_alerted = self.state.last_alert_epoch > 0
        failures = self.state.consecutive_failures
        self.state.consecutive_failures = 0
        self.state.last_error = ""
        self.state.last_success_epoch = now.timestamp()
        try:
            if posted is not None:
                d.spool.remove(posted)
            self._flush_spool(deadline)
            if recovered and was_alerted and cfg.slack.alert_recovery and d.uploader:
                d.uploader.alert(
                    f":white_check_mark: {cfg.camera_name}: recovered after {failures} failed run(s)",
                    deadline=deadline,
                )
        except OSError as exc:
            log.warning("could not finish spool housekeeping: %s", exc)
        finally:
            if recovered:
                self.state.last_alert_epoch = 0.0
            self._save_state()
        return EXIT_OK


def run_locked(cfg: Config, deps: Deps, wait_s: float = 0.0) -> int:
    """Run one capture unless another run is still in progress after ``wait_s`` (then skip quietly)."""
    with locked(cfg.paths.state_path / "lock", wait_s) as acquired:
        if not acquired:
            log.warning("another run is in progress; skipping this one")
            return EXIT_OK
        return CaptureSession(cfg, deps).run()
