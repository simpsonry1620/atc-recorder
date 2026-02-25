"""Tests for metadata.json atomic persistence."""

import json
import threading
from pathlib import Path

import pytest

from atc_recorder.metadata import load_metadata, save_metadata_entry


@pytest.fixture()
def rec_dir(tmp_path: Path) -> Path:
    """Return an empty recording directory."""
    d = tmp_path / "feed1" / "2026-02-25"
    d.mkdir(parents=True)
    return d


def _make_entry(filename: str, source: str = "live") -> dict:
    return {
        "feed_id": "feed1",
        "file": filename,
        "start_time": "2026-02-25T00:00:00+00:00",
        "duration_seconds": 1800,
        "source": source,
    }


class TestSaveMetadataEntry:
    def test_creates_file_when_missing(self, rec_dir: Path) -> None:
        save_metadata_entry(rec_dir, _make_entry("seg1.mp3"))

        entries = load_metadata(rec_dir / "metadata.json")
        assert len(entries) == 1
        assert entries[0]["file"] == "seg1.mp3"

    def test_appends_to_existing(self, rec_dir: Path) -> None:
        save_metadata_entry(rec_dir, _make_entry("seg1.mp3"))
        save_metadata_entry(rec_dir, _make_entry("seg2.mp3"))

        entries = load_metadata(rec_dir / "metadata.json")
        assert len(entries) == 2
        filenames = {e["file"] for e in entries}
        assert filenames == {"seg1.mp3", "seg2.mp3"}

    def test_deduplicates_by_filename(self, rec_dir: Path) -> None:
        save_metadata_entry(rec_dir, _make_entry("seg1.mp3", source="live"))
        save_metadata_entry(rec_dir, _make_entry("seg1.mp3", source="archive"))

        entries = load_metadata(rec_dir / "metadata.json")
        assert len(entries) == 1
        assert entries[0]["source"] == "archive"

    def test_handles_corrupt_json(self, rec_dir: Path) -> None:
        meta_file = rec_dir / "metadata.json"
        meta_file.write_text("{corrupt")

        save_metadata_entry(rec_dir, _make_entry("seg1.mp3"))

        entries = load_metadata(meta_file)
        assert len(entries) == 1

    def test_handles_single_object_instead_of_array(self, rec_dir: Path) -> None:
        meta_file = rec_dir / "metadata.json"
        meta_file.write_text(json.dumps(_make_entry("seg0.mp3")))

        save_metadata_entry(rec_dir, _make_entry("seg1.mp3"))

        entries = load_metadata(meta_file)
        assert len(entries) == 2
        filenames = {e["file"] for e in entries}
        assert filenames == {"seg0.mp3", "seg1.mp3"}

    def test_creates_directory_if_needed(self, tmp_path: Path) -> None:
        new_dir = tmp_path / "a" / "b" / "c"
        save_metadata_entry(new_dir, _make_entry("seg1.mp3"))

        entries = load_metadata(new_dir / "metadata.json")
        assert len(entries) == 1


class TestLoadMetadata:
    def test_returns_empty_for_missing_file(self, tmp_path: Path) -> None:
        assert load_metadata(tmp_path / "nope.json") == []

    def test_returns_empty_for_corrupt_file(self, tmp_path: Path) -> None:
        (tmp_path / "metadata.json").write_text("not json")
        assert load_metadata(tmp_path / "metadata.json") == []


class TestConcurrentWrites:
    """Verify no entries are lost when multiple threads write simultaneously."""

    def test_concurrent_writers_no_lost_entries(self, rec_dir: Path) -> None:
        num_writers = 20
        barrier = threading.Barrier(num_writers)
        errors: list[Exception] = []

        def writer(idx: int) -> None:
            try:
                barrier.wait(timeout=5)
                save_metadata_entry(rec_dir, _make_entry(f"seg{idx}.mp3"))
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=(i,))
            for i in range(num_writers)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors, f"Writer threads raised: {errors}"

        entries = load_metadata(rec_dir / "metadata.json")
        filenames = {e["file"] for e in entries}
        expected = {f"seg{i}.mp3" for i in range(num_writers)}
        assert filenames == expected, (
            f"Lost {expected - filenames}, extra {filenames - expected}"
        )

    def test_concurrent_upserts_no_duplicates(self, rec_dir: Path) -> None:
        """All threads upsert the same filename — result should have exactly one entry."""
        num_writers = 10
        barrier = threading.Barrier(num_writers)
        errors: list[Exception] = []

        def writer(idx: int) -> None:
            try:
                barrier.wait(timeout=5)
                entry = _make_entry("shared.mp3")
                entry["writer"] = idx
                save_metadata_entry(rec_dir, entry)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=(i,))
            for i in range(num_writers)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors
        entries = load_metadata(rec_dir / "metadata.json")
        assert len(entries) == 1
        assert entries[0]["file"] == "shared.mp3"

    def test_file_never_empty_during_writes(self, rec_dir: Path) -> None:
        """A reader thread should never see an empty or corrupt file."""
        num_writers = 15
        stop = threading.Event()
        read_errors: list[str] = []

        save_metadata_entry(rec_dir, _make_entry("seed.mp3"))
        meta_file = rec_dir / "metadata.json"

        def reader() -> None:
            while not stop.is_set():
                try:
                    text = meta_file.read_text()
                    if not text.strip():
                        read_errors.append("empty file")
                        continue
                    data = json.loads(text)
                    if not isinstance(data, list) or len(data) == 0:
                        read_errors.append(f"unexpected content: {text[:80]}")
                except json.JSONDecodeError as exc:
                    read_errors.append(f"corrupt JSON: {exc}")
                except FileNotFoundError:
                    pass

        reader_thread = threading.Thread(target=reader)
        reader_thread.start()

        threads = []
        for i in range(num_writers):
            t = threading.Thread(
                target=save_metadata_entry,
                args=(rec_dir, _make_entry(f"w{i}.mp3")),
            )
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=15)

        stop.set()
        reader_thread.join(timeout=5)

        assert not read_errors, f"Reader saw bad state: {read_errors}"
