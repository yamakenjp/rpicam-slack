import os
import stat
import threading
import time
from pathlib import Path

import pytest

from rpicam_slack.state import STALE_S, Spool, State, StateStore, locked


def test_fresh_total_respects_age_and_validity():
    state = State(last_capture_epoch=1000.0, last_total_s=2.5)
    assert state.fresh_total(1000.0 + 59 * 60, 60) == 2.5
    assert state.fresh_total(1000.0 + 61 * 60, 60) is None
    assert State().fresh_total(1000.0, 60) is None


def test_state_round_trips_and_survives_corruption(tmp_path):
    store = StateStore(tmp_path / "state")
    assert store.load() == State()  # nothing yet
    store.save(State(last_total_s=1.5, consecutive_failures=3, last_error="x"))
    loaded = store.load()
    assert (loaded.last_total_s, loaded.consecutive_failures) == (1.5, 3)

    store.path.write_text("{ not json", encoding="utf-8")
    assert store.load() == State()  # a damaged file must never stop the camera
    store.path.write_text('{"last_total_s": 2, "from_a_newer_version": 1}', encoding="utf-8")
    assert store.load().last_total_s == 2


def test_spool_keeps_the_newest_files(tmp_path):
    image = tmp_path / "a.jpg"
    image.write_bytes(b"img")
    spool = Spool(tmp_path / "spool", max_files=2)
    for i in range(3):
        spool.add(image, f"comment {i}", f"f{i}.jpg", 1000 + i)
    pending = spool.pending()
    assert [p.filename for p in pending] == ["f1.jpg", "f2.jpg"]  # oldest dropped, order kept
    assert pending[0].comment == "comment 1"
    spool.remove(pending[0])
    assert [p.filename for p in spool.pending()] == ["f2.jpg"]


def test_spool_ignores_orphans_and_can_be_disabled(tmp_path):
    spool = Spool(tmp_path / "spool", max_files=0)
    image = tmp_path / "a.jpg"
    image.write_bytes(b"img")
    spool.add(image, "c", "f.jpg", 1)
    assert spool.pending() == []  # max_files = 0 turns the spool off

    spool = Spool(tmp_path / "spool2", max_files=5)
    spool.directory.mkdir()
    (spool.directory / "1-lost.jpg.json").write_text('{"comment": "c", "filename": "lost.jpg"}')
    (spool.directory / "2-dir.jpg").mkdir()  # only a regular file is a picture
    (spool.directory / "2-dir.jpg.json").write_text('{"comment": "c", "filename": "dir.jpg"}')
    assert spool.pending() == []
    assert (spool.directory / "1-lost.jpg.json").exists()  # status never changes the spool


def test_spool_only_publishes_complete_pairs(tmp_path, monkeypatch):
    import os

    spool = Spool(tmp_path / "spool", max_files=2)
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"capture")
    original_replace = os.replace

    def inspect_before_publish(src, dst):
        assert spool.pending() == []
        assert dst.with_suffix("").read_bytes() == b"capture"
        original_replace(src, dst)

    monkeypatch.setattr(os, "replace", inspect_before_publish)
    item = spool.add(source, "comment", "photo.jpg", 1000)
    assert spool.pending() == [item]


def test_status_leaves_corrupt_and_legacy_entries_in_place(tmp_path):
    spool = Spool(tmp_path, max_files=2)
    (tmp_path / "1000-old.jpg").write_bytes(b"legacy")
    (tmp_path / "1000-old.jpg.json").write_text('{"comment":"old","filename":"old.jpg"}')
    (tmp_path / "1001-bad.jpg").write_bytes(b"image")
    (tmp_path / "1001-bad.jpg.json").write_text("{")
    assert [item.filename for item in spool.pending()] == ["old.jpg"]
    assert (tmp_path / "1001-bad.jpg").read_bytes() == b"image"
    assert (tmp_path / "1001-bad.jpg.json").exists()


def test_sweep_deletes_only_old_leftovers(tmp_path):
    spool = Spool(tmp_path / "spool", max_files=5)
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"capture")
    kept = spool.add(source, "waiting", "kept.jpg", 1000)
    refused = spool.add(source, "refused", "refused.jpg", 1001)
    spool.reject(refused)
    assert spool.pending() == [kept]  # a rejected image is no longer offered to Slack
    leftovers = [refused.image, refused.meta.with_suffix(".rejected")]
    for name in ["2-orphan.jpg", "3-lost.jpg.json", "4-bad.jpg", "4-bad.jpg.json", "5-half.jpg.tmp"]:
        (spool.directory / name).write_text("{")
        leftovers.append(spool.directory / name)
    now = 1_000_000.0
    for path in [kept.image, kept.meta, *leftovers]:
        os.utime(path, (now - STALE_S - 60, now - STALE_S - 60))
    fresh = spool.directory / "6-fresh.jpg"  # could belong to a run that has only just stopped
    fresh.write_bytes(b"copy")
    os.utime(fresh, (now - 60, now - 60))

    spool.pending()  # what status does: it must leave everything in place
    assert all(path.exists() for path in leftovers)
    assert STALE_S == 24 * 3600  # the README promises one day
    spool.sweep(now)
    assert sorted(p.name for p in spool.directory.iterdir()) == sorted(
        [kept.image.name, kept.meta.name, fresh.name]
    )


def test_add_flushes_the_copy_to_the_disk(tmp_path, monkeypatch):
    synced: list[Path] = []
    monkeypatch.setattr("rpicam_slack.state._fsync", synced.append)
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"capture")
    spool = Spool(tmp_path / "spool", max_files=2)
    spool.add(source, "comment", "photo.jpg", 1000)
    assert [path.suffix for path in synced[:2]] == [".jpg", ".tmp"]  # the JPEG, then the sidecar
    assert synced[2:] == [spool.directory]  # and the directory entry, after the rename


def test_each_flush_really_reaches_the_disk(tmp_path, monkeypatch):
    real_fsync = os.fsync
    directories: list[bool] = []

    def recording(fd):
        directories.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording)
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"capture")
    Spool(tmp_path / "spool", max_files=2).add(source, "comment", "photo.jpg", 1000)
    assert directories == [False, False, True]  # two files, then the directory


def test_a_directory_that_cannot_be_flushed_does_not_cost_the_copy(tmp_path, monkeypatch):
    real_fsync = os.fsync

    def no_directory_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(22, "Invalid argument")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", no_directory_fsync)
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"capture")
    spool = Spool(tmp_path / "spool", max_files=2)
    item = spool.add(source, "comment", "photo.jpg", 1000)
    assert spool.pending() == [item]  # the pair was complete before the flush failed


def test_a_copy_that_fails_half_way_leaves_nothing_behind(tmp_path, monkeypatch):
    def fail_on_the_sidecar(path):
        if path.suffix == ".tmp":
            raise OSError(5, "Input/output error")

    monkeypatch.setattr("rpicam_slack.state._fsync", fail_on_the_sidecar)
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"capture")
    spool = Spool(tmp_path / "spool", max_files=2)
    with pytest.raises(OSError):
        spool.add(source, "comment", "photo.jpg", 1000)
    assert list(spool.directory.iterdir()) == []


def test_only_the_capture_path_sweeps(tmp_path):
    spool = Spool(tmp_path / "spool", max_files=5)
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"capture")
    spool.directory.mkdir()
    orphan = spool.directory / "1-orphan.jpg"
    orphan.write_bytes(b"left behind")
    old = time.time() - STALE_S - 60
    os.utime(orphan, (old, old))
    spool.add(source, "c", "a.jpg", 1000)
    spool.pending()
    assert orphan.exists()  # neither a plain add nor status cleans up
    spool.add(source, "c", "b.jpg", 1001, prune=False)  # what a capture does before it posts
    assert not orphan.exists()


def test_sweep_spares_a_pair_whose_sidecar_cannot_be_read_right_now(tmp_path, monkeypatch):
    spool = Spool(tmp_path / "spool", max_files=5)
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"capture")
    item = spool.add(source, "waiting", "kept.jpg", 1000)
    now = 1_000_000.0
    for path in (item.image, item.meta):
        os.utime(path, (now - STALE_S - 60, now - STALE_S - 60))
    read_text = Path.read_text

    def flaky(self, *args, **kwargs):
        if self == item.meta:
            raise OSError(5, "Input/output error")
        return read_text(self, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(Path, "read_text", flaky)
        spool.sweep(now)
    assert spool.pending() == [item]  # unreadable for a moment is not the same as damaged


def test_sweep_spares_a_pair_whose_jpeg_cannot_be_examined_right_now(tmp_path, monkeypatch):
    spool = Spool(tmp_path / "spool", max_files=5)
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"capture")
    item = spool.add(source, "waiting", "kept.jpg", 1000)
    now = 1_000_000.0
    for path in (item.image, item.meta):
        os.utime(path, (now - STALE_S - 60, now - STALE_S - 60))
    real_stat = Path.stat

    def flaky(self, *args, **kwargs):
        if self == item.image:
            raise OSError(5, "Input/output error")
        return real_stat(self, *args, **kwargs)

    def is_file(self, *, follow_symlinks=True):  # as from Python 3.14: every error reads as "no"
        try:
            return stat.S_ISREG(self.stat(follow_symlinks=follow_symlinks).st_mode)
        except OSError:
            return False

    with monkeypatch.context() as m:
        m.setattr(Path, "stat", flaky)
        m.setattr(Path, "is_file", is_file)
        spool.sweep(now)
    assert spool.pending() == [item]


def test_a_cleanup_error_does_not_stop_the_capture_from_staging_its_picture(tmp_path, monkeypatch):
    spool = Spool(tmp_path / "spool", max_files=2)
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"capture")
    spool.directory.mkdir()

    def broken(self):
        raise OSError(5, "Input/output error")

    with monkeypatch.context() as m:
        m.setattr(Path, "iterdir", broken)
        item = spool.add(source, "c", "a.jpg", 1000, prune=False)
    assert spool.pending() == [item]


def test_the_picture_just_added_is_never_the_one_dropped(tmp_path):
    spool = Spool(tmp_path / "spool", max_files=1)
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"capture")
    spool.add(source, "c", "later.jpg", 4_000_000_000)  # taken while the clock was wrong
    item = spool.add(source, "c", "now.jpg", 1000)
    assert spool.pending() == [item]


def test_reject_works_even_when_the_files_cannot_be_touched(tmp_path, monkeypatch):
    spool = Spool(tmp_path / "spool", max_files=5)
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"capture")
    item = spool.add(source, "refused", "refused.jpg", 1000)

    def denied(path, *args, **kwargs):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "utime", denied)
    spool.reject(item)
    assert spool.pending() == [] and item.meta.with_suffix(".rejected").exists()


def test_a_rejected_image_is_kept_for_a_day_from_the_rejection(tmp_path):
    spool = Spool(tmp_path / "spool", max_files=5)
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"capture")
    item = spool.add(source, "refused", "waited.jpg", 1000)
    long_ago = time.time() - STALE_S - 3600  # it had waited for more than a day before Slack refused it
    for path in (item.image, item.meta):
        os.utime(path, (long_ago, long_ago))
    spool.reject(item)
    spool.sweep()  # what the next capture does
    assert item.image.exists() and item.meta.with_suffix(".rejected").exists()
    spool.sweep(time.time() + STALE_S + 60)
    assert list(spool.directory.iterdir()) == []


def test_rejected_images_give_way_when_the_spool_is_over_its_limit(tmp_path):
    spool = Spool(tmp_path / "spool", max_files=3)
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"capture")
    refused = [spool.add(source, "refused", f"refused{i}.jpg", i) for i in range(2)]
    for item in refused:
        spool.reject(item)
    waiting = [spool.add(source, "waiting", f"waiting{i}.jpg", 10 + i) for i in range(2)]
    assert spool.pending() == waiting
    assert not refused[0].image.exists() and refused[1].image.exists()  # only the excess, oldest first
    waiting.append(spool.add(source, "waiting", "waiting2.jpg", 12))
    assert spool.pending() == waiting
    assert len(list(spool.directory.iterdir())) == 6  # the three waiting pairs and nothing else


def test_lock_can_wait_for_the_holder_to_finish(tmp_path):
    path = tmp_path / "lock"
    holding, release = threading.Event(), threading.Event()

    def hold():
        with locked(path):
            holding.set()
            release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    assert holding.wait(5)
    with locked(path) as acquired:
        assert not acquired  # no waiting unless asked for
    threading.Timer(0.2, release.set).start()
    with locked(path, 5) as acquired:
        assert acquired
    thread.join()
