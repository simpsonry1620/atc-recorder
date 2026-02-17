"""Transcript ingestion service for vector and metadata indexing."""

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .config import Config
from .embedding import EmbeddingClient, create_embedding_client
from .logging import get_logger
from .rag_models import SearchFilters, SearchHit, TranscriptDocument
from .vector_store import VectorStoreAdapter, create_vector_store

logger = get_logger(__name__)


def _parse_timestamp(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized).astimezone(timezone.utc)


def _safe_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def _parse_feed_id(audio_file_name: str) -> str:
    # Expected pattern: feedid_YYYY-MM-DD_HHMMZ.mp3
    if "_" not in audio_file_name:
        return "unknown"
    return audio_file_name.split("_", 1)[0]


def _quality_flags_for_text(text: str) -> list[str]:
    flags = []
    clean = text.strip()
    if len(clean) < 4 or clean in {"...", "-"}:
        flags.append("low_content")
    if sum(ch.isdigit() for ch in clean) > max(8, len(clean) // 2):
        flags.append("numeric_noise")
    return flags


def _doc_id(audio_file: str, segment_index: int, start_s: float, end_s: float) -> str:
    raw = f"{audio_file}:{segment_index}:{start_s:.3f}:{end_s:.3f}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    return f"seg_{digest}"


@dataclass
class IngestStats:
    """Ingestion result summary."""

    files_processed: int = 0
    docs_upserted: int = 0
    docs_skipped: int = 0
    errors: int = 0


class MetadataStore:
    """SQLite metadata index for traceability and future Elastic migration."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def ensure_schema(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS transcript_docs (
                    doc_id TEXT PRIMARY KEY,
                    feed_id TEXT NOT NULL,
                    audio_file TEXT NOT NULL,
                    segment_index INTEGER NOT NULL,
                    start_time_utc TEXT NOT NULL,
                    end_time_utc TEXT NOT NULL,
                    text TEXT NOT NULL,
                    quality_flags TEXT NOT NULL,
                    ingested_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_docs_feed_time ON transcript_docs(feed_id, start_time_utc, end_time_utc)"
            )
            conn.commit()

    def upsert_documents(self, docs: list[TranscriptDocument]) -> int:
        if not docs:
            return 0
        with self._conn() as conn:
            conn.executemany(
                """
                INSERT INTO transcript_docs (
                    doc_id, feed_id, audio_file, segment_index,
                    start_time_utc, end_time_utc, text, quality_flags, ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(doc_id) DO UPDATE SET
                    feed_id=excluded.feed_id,
                    audio_file=excluded.audio_file,
                    segment_index=excluded.segment_index,
                    start_time_utc=excluded.start_time_utc,
                    end_time_utc=excluded.end_time_utc,
                    text=excluded.text,
                    quality_flags=excluded.quality_flags,
                    ingested_at=excluded.ingested_at
                """,
                [
                    (
                        d.doc_id,
                        d.feed_id,
                        d.audio_file,
                        d.segment_index,
                        d.start_time_utc.isoformat(),
                        d.end_time_utc.isoformat(),
                        d.text,
                        json.dumps(d.quality_flags),
                        d.ingested_at.isoformat(),
                    )
                    for d in docs
                ],
            )
            conn.commit()
        return len(docs)

    def search_by_filters(self, filters: SearchFilters, limit: int = 100) -> list[dict]:
        where = []
        params: list[object] = []
        if filters.start_time_utc is not None:
            where.append("end_time_utc >= ?")
            params.append(filters.start_time_utc.isoformat())
        if filters.end_time_utc is not None:
            where.append("start_time_utc <= ?")
            params.append(filters.end_time_utc.isoformat())
        if filters.feed_ids:
            placeholders = ",".join("?" for _ in filters.feed_ids)
            where.append(f"feed_id IN ({placeholders})")
            params.extend(filters.feed_ids)
        if filters.exclude_feed_ids:
            placeholders = ",".join("?" for _ in filters.exclude_feed_ids)
            where.append(f"feed_id NOT IN ({placeholders})")
            params.extend(filters.exclude_feed_ids)
        where_sql = " AND ".join(where) if where else "1=1"
        sql = f"""
            SELECT doc_id, feed_id, audio_file, segment_index, start_time_utc, end_time_utc, text
            FROM transcript_docs
            WHERE {where_sql}
            ORDER BY start_time_utc DESC
            LIMIT ?
        """
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


class TranscriptIngestionService:
    """Ingest transcript JSON files and index segment documents."""

    def __init__(
        self,
        config: Config,
        embedding_client: Optional[EmbeddingClient] = None,
        vector_store: Optional[VectorStoreAdapter] = None,
    ):
        if config.rag is None:
            raise ValueError("RAG config not enabled in config.yaml")

        self.config = config
        self.embedding_client = embedding_client or create_embedding_client(config.rag.embedding)
        self.vector_store = vector_store or create_vector_store(config.rag.vector_store)
        self.metadata_store = MetadataStore(Path(config.rag.vector_store.sqlite_metadata_path))

        self.vector_store.ensure_schema()
        self.metadata_store.ensure_schema()

    def _resolve_recording_start_time(self, transcript_path: Path, audio_file: str) -> datetime:
        metadata_file = transcript_path.parent / "metadata.json"
        if metadata_file.exists():
            try:
                entries = json.loads(metadata_file.read_text(encoding="utf-8"))
                if isinstance(entries, dict):
                    entries = [entries]
                for entry in entries:
                    if entry.get("file") == audio_file and entry.get("start_time"):
                        return _parse_timestamp(entry["start_time"])
            except Exception as exc:
                logger.warning("Failed to parse metadata file %s: %s", metadata_file, exc)

        # Fallback: infer from audio filename suffix
        # Example: kdca1_twr_2026-02-12_1709Z.mp3
        stem = Path(audio_file).stem
        pieces = stem.split("_")
        if len(pieces) >= 3:
            date_part = pieces[-2]
            time_part = pieces[-1].rstrip("Z")
            try:
                return datetime.strptime(
                    f"{date_part} {time_part}",
                    "%Y-%m-%d %H%M",
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        return datetime.now(timezone.utc)

    def _build_documents(self, transcript_path: Path, data: dict) -> list[TranscriptDocument]:
        audio_file = data.get("audio_file", transcript_path.with_suffix(".mp3").name)
        feed_id = _parse_feed_id(audio_file)
        recording_start = self._resolve_recording_start_time(transcript_path, audio_file)
        segments = data.get("segments", [])
        docs: list[TranscriptDocument] = []

        if isinstance(segments, list) and segments:
            for idx, seg in enumerate(segments):
                if bool(seg.get("skip_for_ingest", False)):
                    continue
                text = _safe_text(seg.get("stitched_canonical_text") or seg.get("text"))
                if not text:
                    continue
                start_s = float(seg.get("start_time", 0.0))
                end_s = float(seg.get("end_time", start_s))
                start_dt = recording_start + timedelta(seconds=max(0.0, start_s))
                end_dt = recording_start + timedelta(seconds=max(start_s, end_s))
                quality_flags = _quality_flags_for_text(text)
                role = seg.get("speaker_role")
                if isinstance(role, str) and role:
                    quality_flags.append(f"role:{role.lower()}")
                docs.append(
                    TranscriptDocument(
                        doc_id=_doc_id(audio_file, idx, start_s, end_s),
                        feed_id=feed_id,
                        audio_file=audio_file,
                        segment_index=idx,
                        start_time_utc=start_dt,
                        end_time_utc=end_dt,
                        text=text,
                        quality_flags=quality_flags,
                    )
                )
        else:
            text = _safe_text(data.get("text", ""))
            if text:
                docs.append(
                    TranscriptDocument(
                        doc_id=_doc_id(audio_file, 0, 0.0, 0.0),
                        feed_id=feed_id,
                        audio_file=audio_file,
                        segment_index=0,
                        start_time_utc=recording_start,
                        end_time_utc=recording_start,
                        text=text,
                        quality_flags=_quality_flags_for_text(text),
                    )
                )
        return docs

    def ingest_transcript(self, transcript_path: Path) -> IngestStats:
        stats = IngestStats()
        transcript_path = Path(transcript_path)
        if not transcript_path.exists():
            stats.errors += 1
            return stats

        t0 = time.perf_counter()
        try:
            data = json.loads(transcript_path.read_text(encoding="utf-8"))
            docs = self._build_documents(transcript_path, data)
            if not docs:
                stats.files_processed += 1
                stats.docs_skipped += 1
                logger.info("ingest file=%s skipped=true reason=no_docs", transcript_path)
                return stats

            vectors = [self.embedding_client.embed_text(doc.text).vector for doc in docs]
            upserted = self.vector_store.upsert_documents(docs, vectors)
            self.metadata_store.upsert_documents(docs)
            stats.files_processed += 1
            stats.docs_upserted += upserted
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.info(
                "ingest file=%s docs_upserted=%s elapsed_ms=%s",
                transcript_path,
                upserted,
                elapsed_ms,
            )
            return stats
        except Exception as exc:
            logger.error("Failed ingesting transcript %s: %s", transcript_path, exc)
            stats.files_processed += 1
            stats.errors += 1
            return stats

    def backfill(self, recordings_dir: Path) -> IngestStats:
        total = IngestStats()
        for path in sorted(Path(recordings_dir).rglob("*.json")):
            if path.name == "metadata.json":
                continue
            file_stats = self.ingest_transcript(path)
            total.files_processed += file_stats.files_processed
            total.docs_upserted += file_stats.docs_upserted
            total.docs_skipped += file_stats.docs_skipped
            total.errors += file_stats.errors
        return total

    def search(self, query: str, filters: SearchFilters, top_k: int) -> list[SearchHit]:
        t0 = time.perf_counter()
        vector = self.embedding_client.embed_text(query).vector
        hits = self.vector_store.search(vector, filters=filters, top_k=top_k)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.info("search query_len=%s top_k=%s hits=%s elapsed_ms=%s", len(query), top_k, len(hits), elapsed_ms)
        return hits
