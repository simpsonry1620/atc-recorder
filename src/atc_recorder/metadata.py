"""Thread-safe, atomic metadata.json persistence.

Provides a single read-modify-write helper used by both the live-stream
recorder and the archive downloader so that concurrent writers cannot
corrupt or lose entries.
"""

import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import Union


METADATA_FILENAME = "metadata.json"


def save_metadata_entry(directory: Path, entry: dict) -> None:
    """Atomically append *entry* to the metadata.json inside *directory*.

    Safety guarantees
    -----------------
    * **File locking** – an exclusive ``fcntl.flock`` is held for the
      entire read-modify-write cycle so concurrent callers serialise.
    * **Atomic write** – data is written to a temporary file in the same
      directory, then moved into place with ``os.replace`` (a single
      rename syscall on POSIX) so readers never see a half-written file.
    * **Deduplication** – if an entry with the same ``"file"`` key already
      exists it is replaced rather than duplicated.

    Parameters
    ----------
    directory:
        Folder that contains (or will contain) ``metadata.json``.
    entry:
        A single metadata dict to upsert.
    """
    metadata_file = directory / METADATA_FILENAME
    lock_file = directory / ".metadata.lock"

    directory.mkdir(parents=True, exist_ok=True)

    with open(lock_file, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            existing = _load_entries(metadata_file)
            filename = entry.get("file")
            if filename:
                existing = [m for m in existing if m.get("file") != filename]
            existing.append(entry)
            _atomic_write(metadata_file, existing)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def load_metadata(path: Union[Path, str]) -> list[dict]:
    """Read metadata.json and return its entries.

    Returns an empty list when the file is missing, empty, or corrupt.
    """
    return _load_entries(Path(path))


# -- internal helpers --------------------------------------------------------


def _load_entries(metadata_file: Path) -> list[dict]:
    """Load existing metadata entries, tolerating missing/corrupt files."""
    if not metadata_file.exists():
        return []
    try:
        with open(metadata_file, "r") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            return [data]
    except (json.JSONDecodeError, IOError):
        return []


def _atomic_write(metadata_file: Path, entries: list[dict]) -> None:
    """Write *entries* to *metadata_file* atomically via temp-file + rename."""
    fd, tmp_path = tempfile.mkstemp(
        dir=metadata_file.parent,
        prefix=".metadata_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(entries, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, metadata_file)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
