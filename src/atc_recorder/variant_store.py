"""Transcript variant storage for tracking multiple ASR outputs and edits per audio file."""

import difflib
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .logging import get_logger

logger = get_logger(__name__)

_FEED_ID_RE = re.compile(r"^(.+?)_(\d{4}-\d{2}-\d{2})_\d{4}Z?\.mp3$")


def _parse_feed_id(audio_file_name: str) -> str:
    m = _FEED_ID_RE.match(audio_file_name)
    if m:
        return m.group(1)
    if "_" not in audio_file_name:
        return "unknown"
    return audio_file_name.split("_", 1)[0]


def _variant_id(
    audio_file: str, asr_model: str, preprocess: str, parent_variant_id: Optional[str]
) -> str:
    raw = f"{audio_file}|{asr_model}|{preprocess}|{parent_variant_id or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class TranscriptVariant:
    variant_id: str
    audio_file: str
    audio_path: str
    feed_id: str
    asr_model: str
    preprocess: str
    variant_type: str
    parent_variant_id: Optional[str]
    is_active: bool
    transcript: dict
    word_count: int
    segment_count: int
    created_at: str
    created_by: str
    notes: Optional[str]


@dataclass
class VariantDiff:
    variant_a_id: str
    variant_b_id: str
    text_a: str
    text_b: str
    unified_diff: str
    word_count_a: int
    word_count_b: int
    segment_count_a: int
    segment_count_b: int
    segment_diffs: list = field(default_factory=list)


class TranscriptVariantStore:
    """SQLite-backed store for transcript variants."""

    def __init__(self, db_path: Path, recordings_root: Optional[Path] = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.recordings_root = Path(recordings_root) if recordings_root else self.db_path.parent
        self.ensure_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def ensure_schema(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS transcript_variants (
                    variant_id        TEXT PRIMARY KEY,
                    audio_file        TEXT NOT NULL,
                    audio_path        TEXT NOT NULL,
                    feed_id           TEXT NOT NULL,
                    asr_model         TEXT NOT NULL,
                    preprocess        TEXT NOT NULL,
                    variant_type      TEXT NOT NULL,
                    parent_variant_id TEXT,
                    is_active         INTEGER DEFAULT 0,
                    transcript        TEXT NOT NULL,
                    word_count        INTEGER,
                    segment_count     INTEGER,
                    created_at        TEXT NOT NULL,
                    created_by        TEXT DEFAULT 'system',
                    notes             TEXT,
                    FOREIGN KEY (parent_variant_id) REFERENCES transcript_variants(variant_id)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_variants_audio ON transcript_variants(audio_file)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_variants_feed_time ON transcript_variants(feed_id, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_variants_model ON transcript_variants(asr_model, preprocess)"
            )
            conn.commit()

    def _row_to_variant(self, row: sqlite3.Row) -> TranscriptVariant:
        return TranscriptVariant(
            variant_id=row["variant_id"],
            audio_file=row["audio_file"],
            audio_path=row["audio_path"],
            feed_id=row["feed_id"],
            asr_model=row["asr_model"],
            preprocess=row["preprocess"],
            variant_type=row["variant_type"],
            parent_variant_id=row["parent_variant_id"],
            is_active=bool(row["is_active"]),
            transcript=json.loads(row["transcript"]),
            word_count=row["word_count"],
            segment_count=row["segment_count"],
            created_at=row["created_at"],
            created_by=row["created_by"],
            notes=row["notes"],
        )

    def save_variant(
        self,
        audio_file: str,
        audio_path: str,
        asr_model: str,
        preprocess: str,
        transcript_data: dict,
        variant_type: str = "asr",
        parent_variant_id: Optional[str] = None,
        activate: bool = True,
        created_by: str = "system",
        notes: Optional[str] = None,
    ) -> str:
        """Store a transcript variant and optionally mark it active.

        Returns the variant_id.
        """
        vid = _variant_id(audio_file, asr_model, preprocess, parent_variant_id)
        feed_id = _parse_feed_id(audio_file)
        text = transcript_data.get("text", "")
        word_count = len(text.split()) if text else 0
        segments = transcript_data.get("segments", [])
        segment_count = len(segments)
        now = datetime.now(timezone.utc).isoformat()

        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO transcript_variants (
                    variant_id, audio_file, audio_path, feed_id,
                    asr_model, preprocess, variant_type, parent_variant_id,
                    is_active, transcript, word_count, segment_count,
                    created_at, created_by, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(variant_id) DO UPDATE SET
                    transcript=excluded.transcript,
                    word_count=excluded.word_count,
                    segment_count=excluded.segment_count,
                    created_at=excluded.created_at,
                    is_active=excluded.is_active,
                    notes=excluded.notes
                """,
                (
                    vid,
                    audio_file,
                    audio_path,
                    feed_id,
                    asr_model,
                    preprocess,
                    variant_type,
                    parent_variant_id,
                    1 if activate else 0,
                    json.dumps(transcript_data, ensure_ascii=False),
                    word_count,
                    segment_count,
                    now,
                    created_by,
                    notes,
                ),
            )
            if activate:
                conn.execute(
                    "UPDATE transcript_variants SET is_active = 0 WHERE audio_file = ? AND variant_id != ?",
                    (audio_file, vid),
                )
            conn.commit()

        if activate:
            self._write_active_to_disk(audio_file, audio_path, transcript_data)

        logger.info(
            "Saved variant %s for %s (model=%s, preprocess=%s, active=%s)",
            vid[:8],
            audio_file,
            asr_model,
            preprocess,
            activate,
        )
        return vid

    def save_edit(
        self,
        audio_file: str,
        transcript_data: dict,
        parent_variant_id: str,
        notes: Optional[str] = None,
        created_by: str = "user",
        activate: bool = True,
    ) -> str:
        """Save a human-edited transcript variant."""
        parent = self.get_variant(parent_variant_id)
        if parent is None:
            raise ValueError(f"Parent variant {parent_variant_id} not found")
        return self.save_variant(
            audio_file=audio_file,
            audio_path=parent.audio_path,
            asr_model="manual",
            preprocess="n/a",
            transcript_data=transcript_data,
            variant_type="edit",
            parent_variant_id=parent_variant_id,
            activate=activate,
            created_by=created_by,
            notes=notes,
        )

    def activate_variant(self, variant_id: str) -> bool:
        """Mark a variant as active, writing its content to disk."""
        variant = self.get_variant(variant_id)
        if variant is None:
            return False

        with self._conn() as conn:
            conn.execute(
                "UPDATE transcript_variants SET is_active = 0 WHERE audio_file = ?",
                (variant.audio_file,),
            )
            conn.execute(
                "UPDATE transcript_variants SET is_active = 1 WHERE variant_id = ?",
                (variant_id,),
            )
            conn.commit()

        self._write_active_to_disk(variant.audio_file, variant.audio_path, variant.transcript)
        logger.info("Activated variant %s for %s", variant_id[:8], variant.audio_file)
        return True

    def get_variant(self, variant_id: str) -> Optional[TranscriptVariant]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM transcript_variants WHERE variant_id = ?",
                (variant_id,),
            ).fetchone()
        return self._row_to_variant(row) if row else None

    def get_active_variant(self, audio_file: str) -> Optional[TranscriptVariant]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM transcript_variants WHERE audio_file = ? AND is_active = 1",
                (audio_file,),
            ).fetchone()
        return self._row_to_variant(row) if row else None

    def list_variants(
        self,
        audio_file: Optional[str] = None,
        feed_id: Optional[str] = None,
        asr_model: Optional[str] = None,
        limit: int = 200,
    ) -> list[TranscriptVariant]:
        clauses: list[str] = []
        params: list[object] = []
        if audio_file:
            clauses.append("audio_file = ?")
            params.append(audio_file)
        if feed_id:
            clauses.append("feed_id = ?")
            params.append(feed_id)
        if asr_model:
            clauses.append("asr_model = ?")
            params.append(asr_model)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)

        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM transcript_variants{where} ORDER BY audio_file, created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._row_to_variant(r) for r in rows]

    def compare_variants(self, variant_id_a: str, variant_id_b: str) -> Optional[VariantDiff]:
        a = self.get_variant(variant_id_a)
        b = self.get_variant(variant_id_b)
        if a is None or b is None:
            return None

        text_a = a.transcript.get("text", "")
        text_b = b.transcript.get("text", "")

        diff_lines = list(
            difflib.unified_diff(
                text_a.splitlines(keepends=True),
                text_b.splitlines(keepends=True),
                fromfile=f"{a.asr_model}/{a.preprocess}",
                tofile=f"{b.asr_model}/{b.preprocess}",
                lineterm="",
            )
        )

        segs_a = a.transcript.get("segments", [])
        segs_b = b.transcript.get("segments", [])
        seg_diffs = self._align_segments(segs_a, segs_b)

        return VariantDiff(
            variant_a_id=variant_id_a,
            variant_b_id=variant_id_b,
            text_a=text_a,
            text_b=text_b,
            unified_diff="\n".join(diff_lines),
            word_count_a=a.word_count,
            word_count_b=b.word_count,
            segment_count_a=a.segment_count,
            segment_count_b=b.segment_count,
            segment_diffs=seg_diffs,
        )

    def delete_variant(self, variant_id: str) -> bool:
        variant = self.get_variant(variant_id)
        if variant is None:
            return False
        if variant.is_active:
            raise ValueError("Cannot delete the active variant. Activate another variant first.")
        with self._conn() as conn:
            conn.execute(
                "UPDATE transcript_variants SET parent_variant_id = ? WHERE parent_variant_id = ?",
                (variant.parent_variant_id, variant_id),
            )
            conn.execute("DELETE FROM transcript_variants WHERE variant_id = ?", (variant_id,))
            conn.commit()
        logger.info("Deleted variant %s for %s", variant_id[:8], variant.audio_file)
        return True

    def backfill(
        self, recordings_dir: Path, asr_model: str = "whisper-large-v3", preprocess: str = "unknown"
    ) -> int:
        """Scan existing JSON transcripts and register them as active variants.

        Idempotent: skips audio files that already have a variant registered.
        Returns the number of new variants created.
        """
        recordings_dir = Path(recordings_dir)
        count = 0

        existing: set[str] = set()
        with self._conn() as conn:
            rows = conn.execute("SELECT DISTINCT audio_file FROM transcript_variants").fetchall()
            existing = {r["audio_file"] for r in rows}

        for json_path in sorted(recordings_dir.rglob("*.json")):
            if json_path.name in ("metadata.json",) or json_path.name.startswith("."):
                continue
            audio_name = json_path.stem + ".mp3"
            if audio_name in existing:
                continue
            # Skip comparison files (e.g. _transcript_ffmpeg.json)
            if "_transcript_" in json_path.name:
                continue
            audio_candidate = json_path.with_suffix(".mp3")
            if not audio_candidate.exists():
                continue

            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    transcript_data = json.load(f)
            except (json.JSONDecodeError, OSError):
                logger.warning("Skipping unreadable JSON: %s", json_path)
                continue

            try:
                rel_path = str(json_path.parent.relative_to(recordings_dir))
            except ValueError:
                rel_path = str(json_path.parent)

            self.save_variant(
                audio_file=audio_name,
                audio_path=rel_path,
                asr_model=asr_model,
                preprocess=preprocess,
                transcript_data=transcript_data,
                variant_type="asr",
                activate=True,
                created_by="backfill",
            )
            count += 1

        logger.info("Backfill complete: %d new variants registered", count)
        return count

    def _write_active_to_disk(
        self, audio_file: str, audio_path: str, transcript_data: dict
    ) -> None:
        target_dir = self.recordings_root / audio_path
        if not target_dir.exists():
            logger.warning("Target directory does not exist, skipping disk write: %s", target_dir)
            return
        stem = Path(audio_file).stem
        output_path = target_dir / f"{stem}.json"
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(transcript_data, f, indent=2, ensure_ascii=False)
            logger.debug("Wrote active variant to disk: %s", output_path)
        except PermissionError:
            logger.debug(
                "Permission denied writing %s (file may already exist from another process)",
                output_path,
            )

    @staticmethod
    def _align_segments(segs_a: list[dict], segs_b: list[dict]) -> list[dict]:
        """Align segments by time overlap and return per-segment diffs."""
        diffs = []
        j = 0
        for seg_a in segs_a:
            a_start = float(seg_a.get("start_time", 0))
            a_end = float(seg_a.get("end_time", 0))
            a_text = seg_a.get("text", "").strip()

            best_match = None
            best_overlap = 0.0
            for k in range(j, min(j + 5, len(segs_b))):
                b_start = float(segs_b[k].get("start_time", 0))
                b_end = float(segs_b[k].get("end_time", 0))
                overlap = max(0, min(a_end, b_end) - max(a_start, b_start))
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_match = k

            if best_match is not None:
                b_text = segs_b[best_match].get("text", "").strip()
                if a_text != b_text:
                    diffs.append(
                        {
                            "time_range": f"{a_start:.1f}-{a_end:.1f}",
                            "text_a": a_text,
                            "text_b": b_text,
                        }
                    )
                j = best_match + 1

        return diffs
