"""Labeled training data store for consensus-based pseudo-labeling.

Tracks audio chunks through a labeling pipeline: dual-ASR inference,
CER-based filtering, manual verification, and text normalization.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .logging import get_logger

logger = get_logger(__name__)


@dataclass
class LabeledChunk:
    """A labeled audio chunk with dual-ASR outputs and verification status."""

    chunk_id: str
    audio_path: str
    feed_id: str
    date: str
    duration: float
    whisper_text: str
    parakeet_text: str
    consensus_text: str
    cer: float
    status: str  # pending, accepted, rejected, verified
    verified_text: str
    spoken_text: str
    verified_by: str
    created_at: str
    updated_at: str
    trim_start_sec: Optional[float] = None
    trim_end_sec: Optional[float] = None
    original_duration: Optional[float] = None
    original_audio_path: Optional[str] = None


class LabelStore:
    """SQLite-backed store for labeled training data."""

    VALID_STATUSES = ("pending", "accepted", "rejected", "verified", "needs_retrim")

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    _TRIM_COLUMNS = [
        ("trim_start_sec", "REAL"),
        ("trim_end_sec", "REAL"),
        ("original_duration", "REAL"),
        ("original_audio_path", "TEXT"),
    ]

    def _ensure_schema(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS labeled_chunks (
                    chunk_id        TEXT PRIMARY KEY,
                    audio_path      TEXT NOT NULL,
                    feed_id         TEXT NOT NULL,
                    date            TEXT NOT NULL,
                    duration        REAL NOT NULL DEFAULT 0,
                    whisper_text    TEXT NOT NULL DEFAULT '',
                    parakeet_text   TEXT NOT NULL DEFAULT '',
                    consensus_text  TEXT NOT NULL DEFAULT '',
                    cer             REAL NOT NULL DEFAULT 1.0,
                    status          TEXT NOT NULL DEFAULT 'pending',
                    verified_text   TEXT NOT NULL DEFAULT '',
                    spoken_text     TEXT NOT NULL DEFAULT '',
                    verified_by     TEXT NOT NULL DEFAULT '',
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL,
                    trim_start_sec  REAL,
                    trim_end_sec    REAL,
                    original_duration REAL,
                    original_audio_path TEXT
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_label_status ON labeled_chunks(status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_label_cer ON labeled_chunks(cer)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_label_feed ON labeled_chunks(feed_id, date)"
            )
            self._migrate_trim_columns(conn)
            conn.commit()

    def _migrate_trim_columns(self, conn: sqlite3.Connection) -> None:
        """Add trim columns to existing databases that lack them."""
        existing = {
            row[1] for row in conn.execute("PRAGMA table_info(labeled_chunks)").fetchall()
        }
        for col_name, col_type in self._TRIM_COLUMNS:
            if col_name not in existing:
                conn.execute(
                    f"ALTER TABLE labeled_chunks ADD COLUMN {col_name} {col_type}"
                )

    def _row_to_labeled(self, row: sqlite3.Row) -> LabeledChunk:
        return LabeledChunk(
            chunk_id=row["chunk_id"],
            audio_path=row["audio_path"],
            feed_id=row["feed_id"],
            date=row["date"],
            duration=row["duration"],
            whisper_text=row["whisper_text"],
            parakeet_text=row["parakeet_text"],
            consensus_text=row["consensus_text"],
            cer=row["cer"],
            status=row["status"],
            verified_text=row["verified_text"],
            spoken_text=row["spoken_text"],
            verified_by=row["verified_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            trim_start_sec=row["trim_start_sec"],
            trim_end_sec=row["trim_end_sec"],
            original_duration=row["original_duration"],
            original_audio_path=row["original_audio_path"],
        )

    def save_label(
        self,
        chunk_id: str,
        audio_path: str,
        feed_id: str,
        date: str,
        duration: float,
        whisper_text: str,
        parakeet_text: str,
        cer: float,
    ) -> None:
        """Store dual-ASR results for a chunk."""
        now = datetime.now(timezone.utc).isoformat()
        consensus = whisper_text if cer < 0.05 else ""

        with self._conn() as conn:
            conn.execute(
                """INSERT INTO labeled_chunks
                   (chunk_id, audio_path, feed_id, date, duration,
                    whisper_text, parakeet_text, consensus_text, cer,
                    status, verified_text, spoken_text, verified_by,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', '', '', '', ?, ?)
                   ON CONFLICT(chunk_id) DO UPDATE SET
                     whisper_text=excluded.whisper_text,
                     parakeet_text=excluded.parakeet_text,
                     consensus_text=excluded.consensus_text,
                     cer=excluded.cer,
                     updated_at=excluded.updated_at""",
                (
                    chunk_id, audio_path, feed_id, date, duration,
                    whisper_text, parakeet_text, consensus, cer,
                    now, now,
                ),
            )
            conn.commit()

    def filter_by_cer(self, max_cer: float = 0.05) -> tuple[int, int]:
        """Apply CER threshold: accept chunks below, reject chunks above.

        Returns (accepted_count, rejected_count).
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            cur_a = conn.execute(
                """UPDATE labeled_chunks
                   SET status = 'accepted',
                       consensus_text = whisper_text,
                       updated_at = ?
                   WHERE status = 'pending' AND cer < ?""",
                (now, max_cer),
            )
            accepted = cur_a.rowcount

            cur_r = conn.execute(
                """UPDATE labeled_chunks
                   SET status = 'rejected', updated_at = ?
                   WHERE status = 'pending' AND cer >= ?""",
                (now, max_cer),
            )
            rejected = cur_r.rowcount
            conn.commit()

        return accepted, rejected

    def update_trim(
        self,
        chunk_id: str,
        trim_start_sec: float,
        trim_end_sec: float,
        original_duration: float,
        original_audio_path: str,
        new_duration: float,
    ) -> bool:
        """Record trim metadata and update duration for a chunk."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                """UPDATE labeled_chunks
                   SET trim_start_sec = ?, trim_end_sec = ?,
                       original_duration = ?, original_audio_path = ?,
                       duration = ?, updated_at = ?
                   WHERE chunk_id = ?""",
                (trim_start_sec, trim_end_sec, original_duration,
                 original_audio_path, new_duration, now, chunk_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def list_untrimmed_chunks(
        self,
        status: Optional[str] = None,
        feed_id: Optional[str] = None,
        limit: int = 100000,
    ) -> list[LabeledChunk]:
        """Return labeled chunks that have not yet been trimmed."""
        clauses = ["trim_start_sec IS NULL"]
        params: list[object] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if feed_id:
            clauses.append("feed_id = ?")
            params.append(feed_id)
        where = " WHERE " + " AND ".join(clauses)
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM labeled_chunks{where} ORDER BY feed_id, date LIMIT ?",
                params,
            ).fetchall()
        return [self._row_to_labeled(r) for r in rows]

    def update_status(
        self,
        chunk_id: str,
        status: str,
        verified_text: str = "",
        verified_by: str = "",
    ) -> bool:
        """Update chunk status and optionally set verified text."""
        if status not in self.VALID_STATUSES:
            return False
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                """UPDATE labeled_chunks
                   SET status = ?, verified_text = ?, verified_by = ?, updated_at = ?
                   WHERE chunk_id = ?""",
                (status, verified_text, verified_by, now, chunk_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def update_spoken_text(self, chunk_id: str, spoken_text: str) -> bool:
        """Set the spoken-form normalized text for a chunk."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE labeled_chunks SET spoken_text = ?, updated_at = ? WHERE chunk_id = ?",
                (spoken_text, now, chunk_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def batch_update_status(
        self,
        chunk_ids: list[str],
        status: str,
        verified_by: str = "",
    ) -> int:
        """Batch update status for multiple chunks."""
        if status not in self.VALID_STATUSES:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        updated = 0
        with self._conn() as conn:
            for cid in chunk_ids:
                cur = conn.execute(
                    """UPDATE labeled_chunks
                       SET status = ?, verified_by = ?, updated_at = ?
                       WHERE chunk_id = ?""",
                    (status, verified_by, now, cid),
                )
                updated += cur.rowcount
            conn.commit()
        return updated

    def batch_accept_by_cer(self, max_cer: float, verified_by: str = "auto") -> int:
        """Accept all pending/rejected chunks below a CER threshold."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                """UPDATE labeled_chunks
                   SET status = 'accepted',
                       consensus_text = whisper_text,
                       verified_by = ?,
                       updated_at = ?
                   WHERE status IN ('pending', 'rejected') AND cer < ?""",
                (verified_by, now, max_cer),
            )
            conn.commit()
            return cur.rowcount

    def batch_reject_by_cer(self, min_cer: float, verified_by: str = "auto") -> int:
        """Reject all pending/accepted chunks above a CER threshold."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                """UPDATE labeled_chunks
                   SET status = 'rejected',
                       verified_by = ?,
                       updated_at = ?
                   WHERE status IN ('pending', 'accepted') AND cer >= ?""",
                (verified_by, now, min_cer),
            )
            conn.commit()
            return cur.rowcount

    def get_chunk(self, chunk_id: str) -> Optional[LabeledChunk]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM labeled_chunks WHERE chunk_id = ?", (chunk_id,)
            ).fetchone()
        return self._row_to_labeled(row) if row else None

    def list_chunks(
        self,
        status: Optional[str] = None,
        feed_id: Optional[str] = None,
        min_cer: Optional[float] = None,
        max_cer: Optional[float] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[LabeledChunk]:
        clauses: list[str] = []
        params: list[object] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if feed_id:
            clauses.append("feed_id = ?")
            params.append(feed_id)
        if min_cer is not None:
            clauses.append("cer >= ?")
            params.append(min_cer)
        if max_cer is not None:
            clauses.append("cer < ?")
            params.append(max_cer)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.extend([limit, offset])

        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM labeled_chunks{where} ORDER BY cer ASC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [self._row_to_labeled(r) for r in rows]

    def summary(self) -> dict:
        """Aggregate labeling statistics."""
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) as cnt FROM labeled_chunks").fetchone()["cnt"]
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM labeled_chunks GROUP BY status"
            ).fetchall()
            avg_cer = conn.execute(
                "SELECT AVG(cer) as avg_cer FROM labeled_chunks"
            ).fetchone()["avg_cer"]
        by_status = {r["status"]: r["cnt"] for r in rows}
        return {
            "total": total,
            "pending": by_status.get("pending", 0),
            "accepted": by_status.get("accepted", 0),
            "rejected": by_status.get("rejected", 0),
            "verified": by_status.get("verified", 0),
            "needs_retrim": by_status.get("needs_retrim", 0),
            "avg_cer": round(avg_cer, 4) if avg_cer is not None else 0.0,
        }

    def get_verified_chunks(self, limit: int = 10000) -> list[LabeledChunk]:
        """Return all verified and accepted chunks for manifest export."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM labeled_chunks
                   WHERE status IN ('verified', 'accepted')
                   ORDER BY feed_id, date LIMIT ?""",
                (limit,),
            ).fetchall()
        return [self._row_to_labeled(r) for r in rows]
