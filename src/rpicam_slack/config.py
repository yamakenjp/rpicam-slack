"""Configuration.

* Camera behaviour is configured with TOML (``camera.toml`` plus an optional
  per-host ``camera.local.toml`` that is merged on top).
* Slack credentials stay in the legacy ``KEY=VALUE`` option file
  (``.slack_option``) so existing files keep working unchanged.

Unknown keys are rejected on purpose: a typo (or a key the program no longer
reads) must not be silently ignored.
"""

from __future__ import annotations

import os
import socket
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from string import Formatter
from typing import Any

DEFAULT_COMMENT = "Photo taken at {timestamp}! profile={profile}"
COMMENT_FIELDS = frozenset({"timestamp", "profile", "camera", "shutter", "gain", "luma", "tests", "lux"})

HDR_MODES = frozenset({"off", "auto", "sensor", "single-exp"})
DENOISE_MODES = frozenset({"auto", "off", "cdn_off", "cdn_fast", "cdn_hq"})
METERING_MODES = frozenset({"centre", "spot", "average", "matrix", "custom"})
AWB_MODES = frozenset(
    {"auto", "incandescent", "tungsten", "fluorescent", "indoor", "daylight", "cloudy", "custom"}
)


class ConfigError(ValueError):
    """The configuration is invalid."""


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


def _one_of(section: str, key: str, value: str, allowed: frozenset[str]) -> None:
    _check(value in allowed, f"[{section}] {key} must be one of {sorted(allowed)}, got {value!r}")


@dataclass(frozen=True)
class CameraConfig:
    command: str = "rpicam-still"
    width: int = 2304
    height: int = 1296
    quality: int = 92
    hdr: str = "auto"
    denoise: str = "cdn_hq"
    metering: str = "average"
    awb: str = "auto"
    # Diopters (0.0 = infinity, 2.0 = about 0.5 m). None -> autofocus on capture.
    lens_position: float | None = None
    extra_args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _check(self.command.strip() != "", "[camera] command must not be empty")
        _check(16 <= self.width <= 9152, "[camera] width must be 16..9152")
        _check(16 <= self.height <= 9152, "[camera] height must be 16..9152")
        _check(1 <= self.quality <= 100, "[camera] quality must be 1..100")
        _one_of("camera", "hdr", self.hdr, HDR_MODES)
        _one_of("camera", "denoise", self.denoise, DENOISE_MODES)
        _one_of("camera", "metering", self.metering, METERING_MODES)
        _one_of("camera", "awb", self.awb, AWB_MODES)
        if self.lens_position is not None:
            _check(0.0 <= self.lens_position <= 32.0, "[camera] lens_position must be 0..32")


@dataclass(frozen=True)
class ExposureConfig:
    # --- what "correctly exposed" means ---
    target_luma: float = 115.0  # mean of the grey image, 0..255
    tolerance: float = 12.0  # |luma - target| <= tolerance counts as converged
    ev: float = 0.0  # bias in stops (+brighter / -darker)
    max_clipped_fraction: float = 0.05  # do not brighten when this many pixels are blown out
    # --- test shots ---
    max_tests: int = 4
    test_width: int = 640
    test_height: int = 360
    test_quality: int = 60
    test_preview_ms: int = 2000  # time given to AE for the first (auto) test
    test_max_shutter_s: float = 0.5  # tests trade shutter for gain to stay fast
    state_max_age_min: int = 60  # reuse the last exposure as the starting point if newer
    # --- limits for the final shot ---
    min_shutter_us: int = 500
    comfortable_shutter_s: float = 0.05  # keep gain at min_gain while the shutter is below this
    max_shutter_s: float = 12.0
    min_gain: float = 1.0
    preferred_gain: float = 2.0  # lengthen the shutter before raising gain above this
    max_gain: float = 16.0
    final_preview_ms: int = 2000
    # --- model ---
    gamma: float = 2.0  # luma ~ exposure ** (1 / gamma)
    step_limit: float = 32.0  # largest exposure change between two shots
    # --- profile label thresholds (equivalent exposure = shutter x gain, in seconds) ---
    day_max_s: float = 0.02
    twilight_max_s: float = 0.5

    def __post_init__(self) -> None:
        _check(1.0 <= self.target_luma <= 254.0, "[exposure] target_luma must be 1..254")
        _check(1.0 <= self.tolerance <= 100.0, "[exposure] tolerance must be 1..100")
        _check(-4.0 <= self.ev <= 4.0, "[exposure] ev must be -4..4")
        _check(0.0 < self.max_clipped_fraction <= 1.0, "[exposure] max_clipped_fraction must be 0..1")
        _check(1 <= self.max_tests <= 10, "[exposure] max_tests must be 1..10")
        _check(self.test_width >= 16 and self.test_height >= 16, "[exposure] test size too small")
        _check(1 <= self.test_quality <= 100, "[exposure] test_quality must be 1..100")
        _check(0 <= self.test_preview_ms <= 30000, "[exposure] test_preview_ms must be 0..30000")
        _check(self.test_max_shutter_s > 0, "[exposure] test_max_shutter_s must be > 0")
        _check(self.state_max_age_min >= 0, "[exposure] state_max_age_min must be >= 0")
        _check(self.min_shutter_us >= 1, "[exposure] min_shutter_us must be >= 1")
        _check(self.comfortable_shutter_s > 0, "[exposure] comfortable_shutter_s must be > 0")
        _check(0 <= self.final_preview_ms <= 30000, "[exposure] final_preview_ms must be 0..30000")
        _check(
            self.min_shutter_s <= self.comfortable_shutter_s <= self.max_shutter_s,
            "[exposure] need min_shutter_us <= comfortable_shutter_s <= max_shutter_s",
        )
        _check(
            1.0 <= self.min_gain <= self.preferred_gain <= self.max_gain,
            "[exposure] need 1 <= min_gain <= preferred_gain <= max_gain",
        )
        _check(self.test_max_shutter_s >= self.min_shutter_s, "[exposure] test_max_shutter_s too small")
        _check(0.5 <= self.gamma <= 4.0, "[exposure] gamma must be 0.5..4")
        _check(1.5 <= self.step_limit <= 1000.0, "[exposure] step_limit must be 1.5..1000")
        _check(0 < self.day_max_s <= self.twilight_max_s, "[exposure] need day_max_s <= twilight_max_s")

    @property
    def min_shutter_s(self) -> float:
        return self.min_shutter_us / 1_000_000


@dataclass(frozen=True)
class BudgetConfig:
    """Wall-clock budget for one run. Every camera timeout is derived from it."""

    max_total_s: float = 110.0  # everything, including the upload
    upload_reserve_s: float = 30.0  # kept free for uploading
    frames_per_shot: float = 5.0  # a manual-exposure still takes about this many frame times
    overhead_s: float = 3.0
    timeout_factor: float = 1.5
    timeout_slack_s: float = 10.0

    def __post_init__(self) -> None:
        _check(10.0 <= self.max_total_s <= 3600.0, "[budget] max_total_s must be 10..3600")
        _check(0.0 <= self.upload_reserve_s < self.max_total_s, "[budget] upload_reserve_s too large")
        _check(1.0 <= self.frames_per_shot <= 30.0, "[budget] frames_per_shot must be 1..30")
        _check(0.0 <= self.overhead_s <= 60.0, "[budget] overhead_s must be 0..60")
        _check(1.0 <= self.timeout_factor <= 10.0, "[budget] timeout_factor must be 1..10")
        _check(0.0 <= self.timeout_slack_s <= 120.0, "[budget] timeout_slack_s must be 0..120")


@dataclass(frozen=True)
class SlackConfig:
    option_file: str = "~/.config/rpicam-slack/slack_option"
    timeout_s: float = 30.0
    retries: int = 3
    retry_wait_s: float = 3.0
    alert_after_failures: int = 2  # consecutive failed runs before an alert is posted
    alert_min_interval_min: int = 60
    alert_recovery: bool = True

    def __post_init__(self) -> None:
        _check(self.option_file.strip() != "", "[slack] option_file must not be empty")
        _check(1.0 <= self.timeout_s <= 300.0, "[slack] timeout_s must be 1..300")
        _check(1 <= self.retries <= 10, "[slack] retries must be 1..10")
        _check(0.0 <= self.retry_wait_s <= 60.0, "[slack] retry_wait_s must be 0..60")
        _check(1 <= self.alert_after_failures <= 100, "[slack] alert_after_failures must be 1..100")
        _check(1 <= self.alert_min_interval_min <= 10080, "[slack] alert_min_interval_min too large")


@dataclass(frozen=True)
class PathsConfig:
    state_dir: str = "~/.local/state/rpicam-slack"
    tmp_dir: str = ""  # empty -> the system temporary directory
    spool_max_files: int = 24

    def __post_init__(self) -> None:
        _check(self.state_dir.strip() != "", "[paths] state_dir must not be empty")
        _check(0 <= self.spool_max_files <= 1000, "[paths] spool_max_files must be 0..1000")

    @property
    def state_path(self) -> Path:
        return Path(self.state_dir).expanduser()


@dataclass(frozen=True)
class OutputConfig:
    embed_exif: bool = True
    exiftool: str = "exiftool"
    exif_timeout_s: float = 30.0

    def __post_init__(self) -> None:
        _check(1.0 <= self.exif_timeout_s <= 300.0, "[output] exif_timeout_s must be 1..300")


@dataclass(frozen=True)
class Config:
    name: str = ""  # shown in Slack; empty -> the hostname
    camera: CameraConfig = field(default_factory=CameraConfig)
    exposure: ExposureConfig = field(default_factory=ExposureConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    slack: SlackConfig = field(default_factory=SlackConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    output: OutputConfig = field(default_factory=OutputConfig)

    @property
    def camera_name(self) -> str:
        return self.name or socket.gethostname()


_SECTIONS: dict[str, type] = {
    "camera": CameraConfig,
    "exposure": ExposureConfig,
    "budget": BudgetConfig,
    "slack": SlackConfig,
    "paths": PathsConfig,
    "output": OutputConfig,
}


def _coerce(section: str, key: str, value: Any, default: Any) -> Any:
    where = f"[{section}] {key}"
    if isinstance(default, bool):
        _check(isinstance(value, bool), f"{where} must be true or false")
        return value
    if isinstance(default, int):
        _check(isinstance(value, int) and not isinstance(value, bool), f"{where} must be an integer")
        return value
    if isinstance(default, float):
        _check(
            isinstance(value, int | float) and not isinstance(value, bool),
            f"{where} must be a number",
        )
        return float(value)
    if isinstance(default, str):
        _check(isinstance(value, str), f"{where} must be a string")
        return value
    if isinstance(default, tuple):
        _check(
            isinstance(value, list) and all(isinstance(v, str) for v in value),
            f"{where} must be a list of strings",
        )
        return tuple(value)
    # Optional number (default None)
    _check(
        isinstance(value, int | float) and not isinstance(value, bool),
        f"{where} must be a number",
    )
    return float(value)


def _build(cls: type, raw: Any, section: str) -> Any:
    _check(isinstance(raw, Mapping), f"[{section}] must be a table")
    known = {f.name: f for f in fields(cls)}
    unknown = sorted(set(raw) - set(known))
    _check(
        not unknown,
        f"[{section}] unknown key(s): {', '.join(unknown)} (valid: {', '.join(sorted(known))})",
    )
    kwargs = {k: _coerce(section, k, v, known[k].default) for k, v in raw.items()}
    return cls(**kwargs)


def config_from_dict(data: Mapping[str, Any]) -> Config:
    allowed = {"name", *_SECTIONS}
    unknown = sorted(set(data) - allowed)
    _check(not unknown, f"unknown top-level key(s): {', '.join(unknown)}")
    kwargs: dict[str, Any] = {}
    if "name" in data:
        _check(isinstance(data["name"], str), "name must be a string")
        kwargs["name"] = data["name"]
    for section, cls in _SECTIONS.items():
        kwargs[section] = _build(cls, data.get(section, {}), section)
    return Config(**kwargs)


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fp:
            return tomllib.load(fp)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc}") from exc


def load_config(files: Iterable[Path]) -> Config:
    """Merge the TOML files in order (later files win) and validate the result."""
    merged: dict[str, Any] = {}
    for path in files:
        merged = _deep_merge(merged, _read_toml(path))
    return config_from_dict(merged)


def default_config_files() -> list[Path]:
    """``$RPICAM_SLACK_CONFIG`` (path list) or the per-user files that exist."""
    env = os.environ.get("RPICAM_SLACK_CONFIG")
    if env:
        return [Path(p).expanduser() for p in env.split(os.pathsep) if p]
    base = Path("~/.config/rpicam-slack").expanduser()
    return [p for p in (base / "camera.toml", base / "camera.local.toml") if p.exists()]


# --- Slack option file (legacy KEY=VALUE format, kept compatible) -----------------------------


@dataclass(frozen=True)
class SlackOptions:
    token: str
    channel: str
    comment_template: str = DEFAULT_COMMENT

    def __repr__(self) -> str:  # never print the token
        return f"SlackOptions(channel={self.channel!r}, token=<hidden>)"


def read_option_file(path: Path) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines (``#`` comments, optional ``export``, quotes stripped)."""
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def validate_comment_template(template: str) -> None:
    for _, field_name, format_spec, _ in Formatter().parse(template):
        if field_name and field_name not in COMMENT_FIELDS:
            raise ConfigError(
                f"COMMENT_TEMPLATE has unsupported field {{{field_name}}} "
                f"(allowed: {', '.join(sorted(COMMENT_FIELDS))})"
            )
        if format_spec and ("{" in format_spec or "}" in format_spec):
            raise ConfigError("COMMENT_TEMPLATE nested replacement fields are not supported")


def read_slack_options(path: Path) -> SlackOptions:
    if not path.exists():
        raise ConfigError(f"Slack option file not found: {path}")
    values = read_option_file(path)

    def pick(*names: str) -> str:
        for name in names:
            if values.get(name):
                return values[name]
        raise ConfigError(f"{path}: missing {' or '.join(names)}")

    template = values.get("COMMENT_TEMPLATE") or DEFAULT_COMMENT
    validate_comment_template(template)
    return SlackOptions(
        token=pick("SLACK_BOT_TOKEN", "SLACK_TOKEN"),
        channel=pick("SLACK_CHANNEL_ID", "CHANNEL"),
        comment_template=template,
    )


def insecure_permissions(path: Path) -> str | None:
    """A warning message when a secrets file is readable by group/others."""
    if not path.exists():
        return None
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        return f"{path} is accessible by group/others (mode {mode:03o}); run: chmod 600 {path}"
    return None
