"""Persistent state (last exposure, failure counters), the upload spool and the run lock."""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
from collections.abc import Iterator
from dataclasses import asdict, dataclass, fields
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class State:
    last_success_epoch: float = 0.0  # last image that reached Slack
    last_capture_epoch: float = 0.0  # last image taken (even if the upload failed)
    last_total_s: float = 0.0  # equivalent exposure of the last good shot
    last_shutter_us: int = 0
    last_gain: float = 0.0
    last_luma: float = 0.0
    consecutive_failures: int = 0
    last_error: str = ""
    last_alert_epoch: float = 0.0
    frame_ratio: float = 0.0  # observed (elapsed / shutter) of long exposures

    def fresh_total(self, now_epoch: float, max_age_min: int) -> float | None:
        """The last exposure as a starting point, if it is recent enough."""
        if self.last_total_s <= 0 or self.last_capture_epoch <= 0:
            return None
        if now_epoch - self.last_capture_epoch > max_age_min * 60:
            return None
        return self.last_total_s


class StateStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / "state.json"

    def load(self) -> State:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return State()
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable state file %s: %s", self.path, exc)
            return State()
        known = {f.name for f in fields(State)}
        return State(**{k: v for k, v in raw.items() if k in known}) if isinstance(raw, dict) else State()

    def save(self, state: State) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(state), indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)


@dataclass(frozen=True)
class SpoolItem:
    image: Path
    meta: Path
    comment: str
    filename: str


class Spool:
    """Images that could not be uploaded; they are retried on the next run."""

    def __init__(self, directory: Path, max_files: int) -> None:
        self.directory = directory
        self.max_files = max_files

    def add(self, image: Path, comment: str, filename: str, taken_epoch: float) -> None:
        if self.max_files <= 0:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        stem = f"{int(taken_epoch)}-{filename}"
        target = self.directory / stem
        target.write_bytes(image.read_bytes())
        target.with_name(stem + ".json").write_text(
            json.dumps({"comment": comment, "filename": filename}), encoding="utf-8"
        )
        self._prune()

    def pending(self) -> list[SpoolItem]:
        if not self.directory.is_dir():
            return []
        items: list[SpoolItem] = []
        for meta in sorted(self.directory.glob("*.json")):
            image = meta.with_suffix("")
            if not image.exists():
                meta.unlink(missing_ok=True)
                continue
            try:
                info = json.loads(meta.read_text(encoding="utf-8"))
                items.append(SpoolItem(image, meta, str(info["comment"]), str(info["filename"])))
            except (OSError, ValueError, KeyError):
                log.warning("dropping corrupt spool entry %s", meta)
                meta.unlink(missing_ok=True)
                image.unlink(missing_ok=True)
        return items

    def remove(self, item: SpoolItem) -> None:
        item.image.unlink(missing_ok=True)
        item.meta.unlink(missing_ok=True)

    def _prune(self) -> None:
        items = self.pending()
        for item in items[: max(0, len(items) - self.max_files)]:
            log.warning("spool full; dropping oldest image %s", item.filename)
            self.remove(item)


@contextlib.contextmanager
def locked(path: Path) -> Iterator[bool]:
    """Non-blocking exclusive lock. Yields ``False`` when another run holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as fp:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        fp.seek(0)
        fp.truncate()
        fp.write(str(os.getpid()))
        fp.flush()
        try:
            yield True
        finally:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
