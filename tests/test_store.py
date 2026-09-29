import json
from pathlib import Path

from bridge.store import ThreadStore


def test_put_then_get_roundtrip(tmp_path):
    store = ThreadStore(tmp_path / "threads.json")
    store.put(111, "sess-a", Path("/tmp/proj"))
    record = store.get(111)
    assert record is not None
    assert record.session_id == "sess-a"
    assert record.cwd == Path("/tmp/proj")


def test_get_unknown_thread_returns_none(tmp_path):
    store = ThreadStore(tmp_path / "threads.json")
    assert store.get(999) is None


def test_survives_reload(tmp_path):
    path = tmp_path / "threads.json"
    ThreadStore(path).put(222, "sess-b", Path("/tmp/x"))
    reloaded = ThreadStore(path).get(222)
    assert reloaded is not None
    assert reloaded.session_id == "sess-b"


def test_null_session_id_is_allowed(tmp_path):
    store = ThreadStore(tmp_path / "threads.json")
    store.put(333, None, Path("/tmp/y"))
    record = store.get(333)
    assert record is not None
    assert record.session_id is None


def test_delete_removes_entry(tmp_path):
    store = ThreadStore(tmp_path / "threads.json")
    store.put(444, "sess-c", Path("/tmp/z"))
    store.delete(444)
    assert store.get(444) is None


def test_creates_parent_directory(tmp_path):
    path = tmp_path / "nested" / "deep" / "threads.json"
    ThreadStore(path).put(555, "sess-d", Path("/tmp/w"))
    assert path.exists()


def test_on_disk_shape_is_stable(tmp_path):
    path = tmp_path / "threads.json"
    ThreadStore(path).put(666, "sess-e", Path("/tmp/v"))
    data = json.loads(path.read_text())
    assert data == {"666": {"session_id": "sess-e", "cwd": "/tmp/v"}}


def test_corrupt_file_is_treated_as_empty(tmp_path):
    path = tmp_path / "threads.json"
    path.write_text("{ this is not json")
    store = ThreadStore(path)
    assert store.all() == {}
    store.put(777, "sess-f", Path("/tmp/u"))
    assert store.get(777) is not None


def test_no_temp_files_left_behind(tmp_path):
    path = tmp_path / "threads.json"
    store = ThreadStore(path)
    store.put(888, "sess-g", Path("/tmp/t"))
    assert [p.name for p in tmp_path.iterdir()] == ["threads.json"]


def test_entry_missing_cwd_is_treated_as_empty(tmp_path):
    path = tmp_path / "threads.json"
    path.write_text(json.dumps({"999": {"session_id": "sess-x"}}))
    store = ThreadStore(path)
    assert store.all() == {}
    store.put(1000, "sess-y", Path("/tmp/s"))
    assert store.get(1000) is not None


def test_non_integer_thread_id_key_is_treated_as_empty(tmp_path):
    path = tmp_path / "threads.json"
    path.write_text(json.dumps({"not-an-int": {"session_id": "sess-z", "cwd": "/tmp/q"}}))
    store = ThreadStore(path)
    assert store.all() == {}
    store.put(1001, "sess-w", Path("/tmp/r"))
    assert store.get(1001) is not None


def test_top_level_list_is_treated_as_empty(tmp_path):
    path = tmp_path / "threads.json"
    path.write_text(json.dumps([1, 2, 3]))
    store = ThreadStore(path)
    assert store.all() == {}
    store.put(1002, "sess-v", Path("/tmp/p"))
    assert store.get(1002) is not None
    assert store.get(1002).session_id == "sess-v"


def test_entry_value_not_a_dict_is_treated_as_empty(tmp_path):
    path = tmp_path / "threads.json"
    path.write_text(json.dumps({"1003": "not-a-dict"}))
    store = ThreadStore(path)
    assert store.all() == {}
    store.put(1004, "sess-u", Path("/tmp/o"))
    assert store.get(1004) is not None
    assert store.get(1004).session_id == "sess-u"


def test_cwd_not_a_string_is_treated_as_empty(tmp_path):
    path = tmp_path / "threads.json"
    path.write_text(json.dumps({"1005": {"session_id": "sess-t", "cwd": 123}}))
    store = ThreadStore(path)
    assert store.all() == {}
    store.put(1006, "sess-s", Path("/tmp/n"))
    assert store.get(1006) is not None
    assert store.get(1006).session_id == "sess-s"


def test_exists_check_raising_is_treated_as_empty(monkeypatch, tmp_path):
    path = tmp_path / "threads.json"

    def raise_permission_error(self):
        raise PermissionError("no traverse permission")

    monkeypatch.setattr(Path, "exists", raise_permission_error)
    store = ThreadStore(path)
    assert store.all() == {}
