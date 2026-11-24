from rpicam_slack.state import Spool, State, StateStore


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
    assert spool.pending() == []  # metadata without an image is cleaned up
    assert list(spool.directory.iterdir()) == []
