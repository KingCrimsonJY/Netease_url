import os
import threading
import time

from storage import CacheStore


def make_entry(store: CacheStore, song_id: int, level: str, data: bytes = b"x" * 10, **meta):
    part = store.part_path(song_id, level)
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(data)
    with store.lock(song_id, level):
        return store.commit(song_id, level, part, ".flac", {"source_md5": "m", **meta})


def test_commit_and_find(tmp_path):
    store = CacheStore(tmp_path)
    entry = make_entry(store, 1, "lossless")
    assert entry.path == tmp_path / "1" / "lossless.flac"
    assert not store.part_path(1, "lossless").exists()
    found = store.find(1, "lossless")
    assert found and found.size == 10 and found.meta["source_md5"] == "m"
    assert store.find(1, "jymaster") is None


def test_size_mismatch_means_corrupted(tmp_path):
    store = CacheStore(tmp_path)
    entry = make_entry(store, 1, "lossless")
    entry.path.write_bytes(b"short")
    assert store.find(1, "lossless") is None


def test_lock_is_exclusive_across_threads(tmp_path):
    store = CacheStore(tmp_path)
    held = threading.Event()
    release = threading.Event()

    def holder():
        with store.lock(1, "lossless"):
            held.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    held.wait(5)
    with store.lock(1, "lossless", blocking=False) as locked:
        assert locked is False
    with store.lock(1, "exhigh", blocking=False) as locked:
        assert locked is True  # 不同音质互不影响
    release.set()
    t.join()
    with store.lock(1, "lossless", blocking=False) as locked:
        assert locked is True


def test_evict_least_recently_used(tmp_path):
    store = CacheStore(tmp_path, max_bytes=25)
    old = make_entry(store, 1, "lossless")
    store.update_meta(old, last_access=time.time() - 1000)
    mid = make_entry(store, 2, "lossless")
    store.update_meta(mid, last_access=time.time() - 500)
    new = make_entry(store, 3, "lossless")
    removed = store.evict(protect={new.path})
    assert removed == [old.path]
    assert (
        store.find(1, "lossless") is None
        and store.find(2, "lossless")
        and store.find(3, "lossless")
    )


def test_cleanup_old_parts(tmp_path):
    store = CacheStore(tmp_path)
    part = store.part_path(5, "lossless")
    part.parent.mkdir(parents=True)
    part.write_bytes(b"partial")
    assert store.cleanup_parts() == 0  # 新的临时文件保留（可续传）
    old = time.time() - 2 * 24 * 3600
    os.utime(part, (old, old))
    assert store.cleanup_parts() == 1 and not part.exists()
