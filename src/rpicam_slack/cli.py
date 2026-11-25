"""Command line interface: capture, focus-scan, status, check, show-config."""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import shlex
import shutil
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .camera import RpicamStill
from .config import (
    DEFAULT_COMMENT,
    Config,
    ConfigError,
    default_config_files,
    insecure_permissions,
    load_config,
    read_option_file,
    read_slack_options,
    validate_comment_template,
)
from .exif import make_embedder
from .exposure import plan_exposure
from .focus import focus_scan, recommend, scan_positions, spread
from .session import EXIT_CAMERA, EXIT_CONFIG, EXIT_OK, Deps, run_locked, test_shot
from .slack import SlackUploader
from .state import Spool, StateStore, locked

log = logging.getLogger("rpicam_slack")


def _load(args: argparse.Namespace) -> Config:
    files = [Path(p).expanduser() for p in args.config] or default_config_files()
    log.debug("configuration files: %s", [str(f) for f in files] or "(defaults)")
    return load_config(files)


def _option_path(cfg: Config, args: argparse.Namespace) -> Path:
    return Path(args.slack_option or cfg.slack.option_file).expanduser()


def _stores(cfg: Config) -> tuple[StateStore, Spool]:
    state_dir = cfg.paths.state_path
    return StateStore(state_dir), Spool(state_dir / "spool", cfg.paths.spool_max_files)


# --------------------------------------------------------------------------------- capture


def _dry_run(cfg: Config, camera: RpicamStill, store: StateStore) -> int:
    ex = cfg.exposure
    state = store.load()
    hint = state.fresh_total(time.time(), ex.state_max_age_min)
    placeholder = Path("/tmp/rpicam-slack-test1.jpg")
    if hint is None:
        shot = test_shot(cfg, None)
        print("first test: camera auto exposure (no recent exposure on record)")
    else:
        shot = test_shot(cfg, plan_exposure(hint, ex, max_shutter_s=ex.test_max_shutter_s))
        print(f"first test: from the last exposure ({hint:.4f}s equivalent)")
    print(shlex.join(camera.build_args(shot, placeholder, placeholder.with_suffix(".json"))))
    print(
        f"budget: {cfg.budget.max_total_s:.0f}s in total, "
        f"up to {ex.max_tests} test shot(s), then the final shot"
    )
    return EXIT_OK


def cmd_capture(args: argparse.Namespace) -> int:
    cfg = _load(args)
    store, spool = _stores(cfg)
    camera = RpicamStill(cfg.camera)
    if args.dry_run:
        return _dry_run(cfg, camera, store)

    uploader: SlackUploader | None = None
    template = DEFAULT_COMMENT
    path = _option_path(cfg, args)
    if not args.no_upload:
        options = read_slack_options(path)
        warning = insecure_permissions(path)
        if warning:
            log.warning(warning)
        template = options.comment_template
        uploader = SlackUploader(
            options.token,
            options.channel,
            timeout_s=cfg.slack.timeout_s,
            retries=cfg.slack.retries,
            retry_wait_s=cfg.slack.retry_wait_s,
        )
    elif path.exists():
        template = read_option_file(path).get("COMMENT_TEMPLATE") or DEFAULT_COMMENT
        validate_comment_template(template)

    deps = Deps(
        camera=camera,
        store=store,
        spool=spool,
        uploader=uploader,
        comment_template=template,
        embed_exif=make_embedder(cfg.output),
        keep_path=Path(args.keep).expanduser() if args.keep else None,
    )
    return run_locked(cfg, deps)


# ---------------------------------------------------------------------------------- others


def cmd_focus_scan(args: argparse.Namespace) -> int:
    cfg = _load(args)
    try:
        positions = scan_positions(args.start, args.end, args.step)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    camera = RpicamStill(cfg.camera)
    with locked(cfg.paths.state_path / "lock") as acquired:
        if not acquired:
            log.error("camera is busy; retry the focus scan after the current capture")
            return EXIT_CAMERA
        with tempfile.TemporaryDirectory(prefix="rpicam-slack-focus-") as tmp:
            results = focus_scan(cfg, camera, positions, Path(tmp))
    print(f"{'lens_position':>14}  FocusFoM")
    for position, fom in results:
        print(f"{position:>14.2f}  {'-' if fom is None else f'{fom:.1f}'}")
    best = recommend(results)
    if best is None:
        variation = spread(results)
        if variation is None:
            print("no usable result", file=sys.stderr)
        else:
            print(
                f"\nthe focus figure hardly changes with the lens position (spread {variation:.1%}), "
                "so no position can be recommended.\n"
                "In the dark, or on a plain surface, it is mostly noise. Run focus-scan again in "
                "daylight, pointing at the real scene.\n"
                "Keep the current lens_position until then.",
                file=sys.stderr,
            )
        return EXIT_CAMERA
    print(f"\nrecommended: put this in camera.local.toml\n\n[camera]\nlens_position = {best:g}")
    return EXIT_OK


def _age(epoch: float, now: float) -> str:
    if epoch <= 0:
        return "never"
    minutes = (now - epoch) / 60
    return f"{minutes:.0f} min ago" if minutes < 120 else f"{minutes / 60:.1f} h ago"


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _load(args)
    store, spool = _stores(cfg)
    state = store.load()
    now = time.time()
    print(f"camera:               {cfg.camera_name}")
    print(f"last posted:          {_age(state.last_success_epoch, now)}")
    print(f"last capture:         {_age(state.last_capture_epoch, now)}")
    print(f"consecutive failures: {state.consecutive_failures}")
    if state.last_error:
        print(f"last error:           {state.last_error}")
    if state.last_total_s > 0:
        print(
            f"last exposure:        {state.last_shutter_us / 1e6:.4f}s x gain {state.last_gain:.2f}"
            f" (equivalent {state.last_total_s:.4f}s, luma {state.last_luma:.0f})"
        )
    print(f"spooled uploads:      {len(spool.pending())}")
    return EXIT_OK


def cmd_show_config(args: argparse.Namespace) -> int:
    cfg = _load(args)
    data = asdict(cfg)
    data["name"] = cfg.camera_name
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return EXIT_OK


def cmd_check(args: argparse.Namespace) -> int:
    problems = 0

    def report(ok: bool, text: str) -> None:
        nonlocal problems
        problems += 0 if ok else 1
        print(("OK  " if ok else "NG  ") + text)

    cfg = _load(args)
    files = [str(Path(p).expanduser()) for p in args.config] or [str(p) for p in default_config_files()]
    report(True, f"configuration: {', '.join(files) if files else 'built-in defaults'}")
    report(shutil.which(cfg.camera.command) is not None, f"{cfg.camera.command} is installed")
    report(importlib.util.find_spec("slack_sdk") is not None, "slack-sdk is installed")
    if cfg.camera.lens_position is None:
        report(True, "lens_position is not set (autofocus on every capture; run focus-scan to fix it)")
    if cfg.output.embed_exif:
        report(
            shutil.which(cfg.output.exiftool) is not None, f"{cfg.output.exiftool} is installed (EXIF notes)"
        )

    path = _option_path(cfg, args)
    try:
        options = read_slack_options(path)
        report(True, f"Slack option file {path}: channel {options.channel}, token present")
        warning = insecure_permissions(path)
        report(warning is None, warning or f"{path} permissions are private")
    except ConfigError as exc:
        report(False, str(exc))

    state_dir = cfg.paths.state_path
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        probe = state_dir / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        report(True, f"state directory {state_dir} is writable")
    except OSError as exc:
        report(False, f"state directory {state_dir}: {exc}")
    return EXIT_OK if problems == 0 else EXIT_CONFIG


# ------------------------------------------------------------------------------- plumbing


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", action="append", default=[], metavar="FILE",
                        help="TOML configuration file; repeat to layer files (later wins)")  # fmt: skip
    common.add_argument("--slack-option", metavar="FILE",
                        help="Slack option file (KEY=VALUE); default from [slack] option_file")  # fmt: skip
    common.add_argument("--debug", action="store_true", help="verbose logging")

    parser = argparse.ArgumentParser(prog="rpicam-slack", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("capture", parents=[common], help="measure, shoot and post one image")
    p.add_argument("--no-upload", action="store_true", help="shoot but do not post")
    p.add_argument("--dry-run", action="store_true", help="print the first camera command and exit")
    p.add_argument("--keep", metavar="PATH", help="also save the final image to PATH")
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("focus-scan", parents=[common], help="find the sharpest lens position")
    p.add_argument("--start", type=float, default=0.0)
    p.add_argument("--end", type=float, default=8.0)
    p.add_argument("--step", type=float, default=0.5)
    p.set_defaults(func=cmd_focus_scan)

    p = sub.add_parser("status", parents=[common], help="last run, failures, spool")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("check", parents=[common], help="verify configuration and environment")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("show-config", parents=[common], help="print the effective configuration")
    p.set_defaults(func=cmd_show_config)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("PIL", "slack_sdk"):  # they log every EXIF tag / HTTP header at DEBUG
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        return 130
