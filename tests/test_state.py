import os
import threading

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
    spool.sweep(now)
    assert sorted(p.name for p in spool.directory.iterdir()) == sorted(
        [kept.image.name, kept.meta.name, fresh.name]
    )


def test_add_flushes_the_copy_to_the_disk(tmp_path, monkeypatch):
    synced: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"capture")
    Spool(tmp_path / "spool", max_files=2).add(source, "comment", "photo.jpg", 1000)
    assert len(synced) == 3  # the JPEG, the sidecar and the directory entry


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
