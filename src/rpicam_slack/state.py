"""Persistent state (last exposure, failure counters), the upload spool and the run lock."""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import shutil
import stat
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from uuid import uuid4

log = logging.getLogger(__name__)

STALE_S = 24 * 3600  # spool leftovers older than this are deleted by Spool.sweep()
LOCK_POLL_S = 0.05


def _fsync(path: Path) -> None:
    """Flush a file or a directory to the disk, so that it survives a sudden power loss."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _discard(*paths: Path) -> None:
    """Delete what can be deleted: one stuck file must not keep the rest of the spool from being trimmed."""
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not delete %s: %s", path.name, exc)


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
    upload_not_before_epoch: float = 0.0  # Slack Retry-After, shared by fresh and queued uploads
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

    def add(
        self, image: Path, comment: str, filename: str, taken_epoch: float, *, prune: bool = True
    ) -> SpoolItem | None:
        if self.max_files <= 0:
            return None
        self.directory.mkdir(parents=True, exist_ok=True)
        if not prune:
            # A previous process may have stopped with its in-flight slot occupied.
            try:
                self.prune()
            except OSError as exc:  # tidying up must never cost the fresh picture its copy
                log.warning("could not trim the spool: %s", exc)
            self.sweep()
        # The sidecar is the commit marker. Readers see the pair only after the JPEG is complete.
        target = self.directory / f"{int(taken_epoch)}-{uuid4().hex}.jpg"
        meta = target.with_suffix(".jpg.json")
        tmp = meta.with_suffix(".tmp")
        try:
            shutil.copyfile(image, target)
            _fsync(target)
            tmp.write_text(json.dumps({"comment": comment, "filename": filename}), encoding="utf-8")
            _fsync(tmp)
            os.replace(tmp, meta)
        except OSError:
            tmp.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise
        try:
            _fsync(self.directory)  # the rename itself should reach the disk too
        except OSError as exc:  # not every filesystem can do this, and the pair is complete either way
            log.warning("could not flush the spool directory: %s", exc)
        if prune:
            self.prune(keep=target)
        return SpoolItem(target, meta, comment, filename)

    def pending(self) -> list[SpoolItem]:
        return self._scan()[0]

    def _scan(self) -> tuple[list[SpoolItem], set[Path]]:
        """The readable pairs in order, and the files of entries that could not be read just now."""
        items: list[SpoolItem] = []
        unreadable: set[Path] = set()
        if not self.directory.is_dir():
            return items, unreadable
        for meta in sorted(self.directory.glob("*.json")):
            image = meta.with_suffix("")
            try:
                # Not is_file(): from Python 3.14 on it hides every error, and a JPEG that cannot be
                # examined just now must not be taken for a missing one.
                if not stat.S_ISREG(image.stat().st_mode):
                    continue
                info = json.loads(meta.read_text(encoding="utf-8"))
                items.append(SpoolItem(image, meta, str(info["comment"]), str(info["filename"])))
            except FileNotFoundError:
                continue  # no JPEG (or the pair has just been removed): a leftover at most
            except OSError as exc:  # may well be readable next time: this is not a damaged entry
                log.warning("cannot read spool entry %s: %s", meta, exc)
                unreadable.update((image, meta))
            except (ValueError, KeyError, TypeError) as exc:
                log.warning("cannot read spool entry %s: %s", meta, exc)
        return items, unreadable

    def remove(self, item: SpoolItem) -> None:
        item.meta.unlink(missing_ok=True)
        item.image.unlink(missing_ok=True)

    def reject(self, item: SpoolItem) -> None:
        """Take an image that can never be sent out of the queue; sweep() deletes it a day later."""
        with contextlib.suppress(OSError):  # the day counts from now, however long the image had waited
            os.utime(item.meta)
            os.utime(item.image)
        os.replace(item.meta, item.meta.with_suffix(".rejected"))

    def sweep(self, now: float | None = None) -> None:
        """Delete leftovers older than STALE_S: every file that is not part of a readable pair.

        That covers a JPEG without a sidecar (a run stopped inside add() or remove()), a sidecar
        without a JPEG, damaged sidecars with their JPEG, ``*.tmp`` and rejected images. Only the
        capture path calls this; ``status`` must never change the spool.
        """
        if not self.directory.is_dir():
            return
        cutoff = (time.time() if now is None else now) - STALE_S
        try:
            items, keep = self._scan()
            keep.update(path for item in items for path in (item.image, item.meta))
            for path in self.directory.iterdir():
                if path not in keep and path.is_file() and path.stat().st_mtime < cutoff:
                    log.warning("removing stale spool file %s", path.name)
                    path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not clean the spool: %s", exc)

    def prune(self, *, keep: Path | None = None) -> None:
        if self.max_files <= 0:
            return
        items = self.pending()
        # Rejected images only wait for sweep(): they give way first when the directory is over its limit.
        rejected = sorted(self.directory.glob("*.rejected")) if self.directory.is_dir() else []
        for marker in rejected[: max(0, len(items) + len(rejected) - self.max_files)]:
            _discard(marker.with_suffix(""), marker)
        oldest = [item for item in items if item.image != keep]
        for item in oldest[: max(0, len(items) - self.max_files)]:
            log.warning("spool full; dropping oldest image %s", item.filename)
            _discard(item.meta, item.image)


@contextlib.contextmanager
def locked(path: Path, wait_s: float = 0.0) -> Iterator[bool]:
    """Exclusive lock. Yields ``False`` when another run still holds it after ``wait_s`` seconds."""
    path.parent.mkdir(parents=True, exist_ok=True)
    give_up = time.monotonic() + wait_s
    with path.open("a+", encoding="utf-8") as fp:
        while True:
            try:
                fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= give_up:
                    yield False
                    return
                time.sleep(LOCK_POLL_S)
        fp.seek(0)
        fp.truncate()
        fp.write(str(os.getpid()))
        fp.flush()
        try:
            yield True
        finally:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
