"""VAD-based audio segmentation into training-ready chunks.

Splits 30-minute ATC recordings into 2-15 second single-transmission
WAV files suitable for ASR fine-tuning.  Chunk metadata is tracked in
a SQLite table so each chunk is traceable back to its source recording.
"""

import hashlib
import json
import sqlite3
import struct
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .logging import get_logger
from .pipeline import PipelineDefinition, PipelineExecutor

logger = get_logger(__name__)

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # 16-bit
CHANNELS = 1


@dataclass
class ChunkInfo:
    """Metadata for a single audio chunk."""

    chunk_id: str
    source_file: str
    feed_id: str
    date: str
    offset_seconds: float
    duration_seconds: float
    output_path: str
    created_at: str


def _read_wav_samples(wav_path: Path) -> tuple[bytes, int]:
    """Read raw PCM samples from a 16kHz mono 16-bit WAV."""
    with wave.open(str(wav_path), "rb") as wf:
        n_frames = wf.getnframes()
        rate = wf.getframerate()
        data = wf.readframes(n_frames)
    return data, rate


def _write_wav(path: Path, data: bytes, rate: int = SAMPLE_RATE) -> None:
    """Write raw 16-bit PCM data as a WAV file."""
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(rate)
        wf.writeframes(data)


def _rms_energy(samples: bytes, frame_size: int) -> list[float]:
    """Compute per-frame RMS energy from 16-bit PCM."""
    n_samples = len(samples) // SAMPLE_WIDTH
    fmt = f"<{n_samples}h"
    pcm = struct.unpack(fmt, samples[:n_samples * SAMPLE_WIDTH])
    energies = []
    for i in range(0, len(pcm), frame_size):
        frame = pcm[i : i + frame_size]
        if not frame:
            break
        mean_sq = sum(s * s for s in frame) / len(frame)
        energies.append(mean_sq**0.5)
    return energies


def _convert_to_wav(input_path: Path, output_path: Path) -> bool:
    """Convert any audio to 16kHz mono 16-bit WAV."""
    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-ac", "1", "-ar", str(SAMPLE_RATE), "-sample_fmt", "s16",
        "-f", "wav", str(output_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        return result.returncode == 0
    except Exception as exc:
        logger.error("WAV conversion failed: %s", exc)
        return False


def _chunk_id(source_file: str, offset: float) -> str:
    raw = f"{source_file}|{offset:.3f}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def detect_speech_segments(
    wav_path: Path,
    *,
    frame_duration_ms: int = 30,
    energy_threshold: float = 500.0,
    min_speech_sec: float = 0.3,
    min_silence_sec: float = 0.4,
    merge_gap_sec: float = 0.3,
) -> list[tuple[float, float]]:
    """Energy-based VAD returning (start, end) pairs in seconds.

    Uses per-frame RMS energy to classify frames as speech or silence,
    then merges nearby speech regions and filters by minimum duration.
    """
    pcm_data, rate = _read_wav_samples(wav_path)
    frame_size = int(rate * frame_duration_ms / 1000)
    energies = _rms_energy(pcm_data, frame_size)

    speech_frames: list[bool] = [e > energy_threshold for e in energies]

    frame_sec = frame_duration_ms / 1000.0
    segments: list[tuple[float, float]] = []
    in_speech = False
    seg_start = 0.0

    for i, is_speech in enumerate(speech_frames):
        t = i * frame_sec
        if is_speech and not in_speech:
            seg_start = t
            in_speech = True
        elif not is_speech and in_speech:
            segments.append((seg_start, t))
            in_speech = False
    if in_speech:
        total_dur = len(pcm_data) / (rate * SAMPLE_WIDTH)
        segments.append((seg_start, total_dur))

    # Merge segments separated by less than merge_gap_sec
    merged: list[tuple[float, float]] = []
    for start, end in segments:
        if merged and (start - merged[-1][1]) < merge_gap_sec:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))

    # Filter by minimum speech duration
    filtered = [(s, e) for s, e in merged if (e - s) >= min_speech_sec]

    # Expand to fill small silence gaps from the original audio
    expanded: list[tuple[float, float]] = []
    for start, end in filtered:
        adj_start = max(0, start - min_silence_sec * 0.5)
        adj_end = end + min_silence_sec * 0.5
        if expanded and adj_start <= expanded[-1][1]:
            expanded[-1] = (expanded[-1][0], adj_end)
        else:
            expanded.append((adj_start, adj_end))

    return expanded


def extract_chunk(
    wav_path: Path,
    start_sec: float,
    end_sec: float,
    output_path: Path,
    pad_sec: float = 0.5,
) -> bool:
    """Extract a time range from a WAV file with optional ambient padding."""
    pcm_data, rate = _read_wav_samples(wav_path)
    total_samples = len(pcm_data) // SAMPLE_WIDTH

    pad_samples = int(pad_sec * rate)
    start_sample = max(0, int(start_sec * rate) - pad_samples)
    end_sample = min(total_samples, int(end_sec * rate) + pad_samples)

    byte_start = start_sample * SAMPLE_WIDTH
    byte_end = end_sample * SAMPLE_WIDTH
    chunk_data = pcm_data[byte_start:byte_end]

    if len(chunk_data) < SAMPLE_WIDTH * rate:  # less than 1 second of data
        return False

    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_wav(output_path, chunk_data, rate)
    return True


class ChunkStore:
    """SQLite-backed storage for chunk metadata."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_schema(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id        TEXT PRIMARY KEY,
                    source_file     TEXT NOT NULL,
                    feed_id         TEXT NOT NULL,
                    date            TEXT NOT NULL,
                    offset_seconds  REAL NOT NULL,
                    duration_seconds REAL NOT NULL,
                    output_path     TEXT NOT NULL,
                    created_at      TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_file)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chunks_feed ON chunks(feed_id, date)"
            )
            conn.commit()

    def save_chunk(self, info: ChunkInfo) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO chunks
                   (chunk_id, source_file, feed_id, date,
                    offset_seconds, duration_seconds, output_path, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    info.chunk_id,
                    info.source_file,
                    info.feed_id,
                    info.date,
                    info.offset_seconds,
                    info.duration_seconds,
                    info.output_path,
                    info.created_at,
                ),
            )
            conn.commit()

    def list_chunks(
        self,
        feed_id: Optional[str] = None,
        date: Optional[str] = None,
        source_file: Optional[str] = None,
        limit: int = 500,
    ) -> list[ChunkInfo]:
        clauses: list[str] = []
        params: list[object] = []
        if feed_id:
            clauses.append("feed_id = ?")
            params.append(feed_id)
        if date:
            clauses.append("date = ?")
            params.append(date)
        if source_file:
            clauses.append("source_file = ?")
            params.append(source_file)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)

        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM chunks{where} ORDER BY source_file, offset_seconds LIMIT ?",
                params,
            ).fetchall()
        return [
            ChunkInfo(
                chunk_id=r["chunk_id"],
                source_file=r["source_file"],
                feed_id=r["feed_id"],
                date=r["date"],
                offset_seconds=r["offset_seconds"],
                duration_seconds=r["duration_seconds"],
                output_path=r["output_path"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def get_chunk(self, chunk_id: str) -> Optional[ChunkInfo]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM chunks WHERE chunk_id = ?", (chunk_id,)
            ).fetchone()
        if row is None:
            return None
        return ChunkInfo(
            chunk_id=row["chunk_id"],
            source_file=row["source_file"],
            feed_id=row["feed_id"],
            date=row["date"],
            offset_seconds=row["offset_seconds"],
            duration_seconds=row["duration_seconds"],
            output_path=row["output_path"],
            created_at=row["created_at"],
        )

    def count(self, feed_id: Optional[str] = None) -> int:
        with self._conn() as conn:
            if feed_id:
                row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM chunks WHERE feed_id = ?",
                    (feed_id,),
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) as cnt FROM chunks").fetchone()
        return row["cnt"] if row else 0


def _parse_feed_and_date(filename: str) -> tuple[str, str]:
    """Extract feed_id and date from a standard recording filename.

    Expected format: feedid_YYYY-MM-DD_HHMMz.mp3
    """
    stem = Path(filename).stem
    parts = stem.split("_")
    if len(parts) >= 3:
        date_part = parts[-2]
        feed_part = "_".join(parts[:-2])
        return feed_part, date_part
    return "unknown", "unknown"


def chunk_audio_file(
    audio_path: Path,
    output_dir: Path,
    chunk_store: ChunkStore,
    *,
    preprocess_pipeline: Optional[PipelineDefinition] = None,
    min_duration: float = 2.0,
    max_duration: float = 15.0,
    pad_seconds: float = 0.5,
    energy_threshold: float = 500.0,
    min_silence_sec: float = 0.4,
    merge_gap_sec: float = 0.3,
    progress_callback: Optional[callable] = None,
) -> list[ChunkInfo]:
    """Chunk a single audio file into training segments.

    Returns list of ChunkInfo for successfully created chunks.
    """
    feed_id, date = _parse_feed_and_date(audio_path.name)
    chunks_dir = output_dir / feed_id / date
    chunks_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        wav_path = tmpdir_path / "source.wav"

        # Preprocess then convert to WAV
        if preprocess_pipeline and preprocess_pipeline.steps:
            preprocessed = tmpdir_path / "preprocessed.wav"
            executor = PipelineExecutor()
            if not executor.run(audio_path, preprocess_pipeline, preprocessed):
                logger.error("Preprocessing failed for %s", audio_path)
                return []
            wav_path = preprocessed
        else:
            if not _convert_to_wav(audio_path, wav_path):
                logger.error("WAV conversion failed for %s", audio_path)
                return []

        segments = detect_speech_segments(
            wav_path,
            energy_threshold=energy_threshold,
            min_silence_sec=min_silence_sec,
            merge_gap_sec=merge_gap_sec,
        )

        results: list[ChunkInfo] = []
        for start, end in segments:
            duration = end - start
            if duration < min_duration or duration > max_duration:
                continue

            cid = _chunk_id(audio_path.name, start)
            chunk_filename = f"{audio_path.stem}_chunk_{start:.1f}s.wav"
            chunk_path = chunks_dir / chunk_filename

            if not extract_chunk(wav_path, start, end, chunk_path, pad_sec=pad_seconds):
                continue

            # Verify actual output duration
            try:
                with wave.open(str(chunk_path), "rb") as wf:
                    actual_dur = wf.getnframes() / wf.getframerate()
            except Exception:
                actual_dur = duration

            info = ChunkInfo(
                chunk_id=cid,
                source_file=audio_path.name,
                feed_id=feed_id,
                date=date,
                offset_seconds=round(start, 3),
                duration_seconds=round(actual_dur, 3),
                output_path=str(chunk_path),
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            chunk_store.save_chunk(info)
            results.append(info)

            if progress_callback:
                progress_callback(info)

    logger.info(
        "Chunked %s: %d segments found, %d chunks kept (%.1f-%.1fs)",
        audio_path.name,
        len(segments),
        len(results),
        min_duration,
        max_duration,
    )
    return results
