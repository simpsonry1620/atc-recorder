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
import time as _time
import wave
from dataclasses import dataclass, field
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


@dataclass
class ChunkDiagnostics:
    """Per-file diagnostic stats from the chunking pipeline."""

    source_file: str = ""
    conversion_ok: bool = True
    segments_found: int = 0
    segments_too_short: int = 0
    segments_too_long: int = 0
    segments_in_range: int = 0
    extract_failures: int = 0
    chunks_created: int = 0
    energy_mean: float = 0.0
    energy_p50: float = 0.0
    energy_p95: float = 0.0
    energy_max: float = 0.0
    vad_backend: str = "energy"
    vad_time_sec: float = 0.0
    extract_time_sec: float = 0.0
    total_time_sec: float = 0.0


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


def _energy_stats(energies: list[float]) -> tuple[float, float, float, float]:
    """Return (mean, p50, p95, max) for a list of energy values."""
    if not energies:
        return 0.0, 0.0, 0.0, 0.0
    s = sorted(energies)
    n = len(s)
    return (
        sum(s) / n,
        s[n // 2],
        s[int(n * 0.95)],
        s[-1],
    )


def detect_speech_segments(
    wav_path: Path,
    *,
    frame_duration_ms: int = 30,
    energy_threshold: float = 500.0,
    min_speech_sec: float = 0.3,
    min_silence_sec: float = 0.4,
    merge_gap_sec: float = 0.3,
    return_energy_stats: bool = False,
) -> list[tuple[float, float]] | tuple[list[tuple[float, float]], tuple[float, float, float, float]]:
    """Energy-based VAD returning (start, end) pairs in seconds.

    Uses per-frame RMS energy to classify frames as speech or silence,
    then merges nearby speech regions and filters by minimum duration.

    If *return_energy_stats* is True, returns a 2-tuple of
    (segments, (mean, p50, p95, max)) so callers can diagnose threshold issues.
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

    if return_energy_stats:
        return expanded, _energy_stats(energies)
    return expanded


_SILERO_MODEL_URL = (
    "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.jit"
)
_silero_model = None


def _load_silero_model(device: str = "cuda:0"):
    """Download and cache the Silero VAD JIT model, bypassing torch.hub."""
    global _silero_model
    if _silero_model is not None:
        return _silero_model

    try:
        import torch
    except ImportError:
        raise RuntimeError(
            "PyTorch is required for Silero VAD. "
            "Install with: pip install 'atc-recorder[gpu-vad]'"
        )

    cache_dir = Path.home() / ".cache" / "silero-vad"
    cache_dir.mkdir(parents=True, exist_ok=True)
    model_path = cache_dir / "silero_vad.jit"

    if not model_path.exists():
        logger.info("Downloading Silero VAD model to %s ...", model_path)
        import urllib.request
        urllib.request.urlretrieve(_SILERO_MODEL_URL, model_path)

    model = torch.jit.load(str(model_path), map_location=device)
    model.eval()
    _silero_model = model
    logger.info("Silero VAD model loaded on %s", device)
    return model


def _read_wav_as_tensor(wav_path: Path, device: str = "cuda:0"):
    """Read a 16kHz mono WAV into a float32 torch tensor (no torchaudio needed)."""
    import numpy as np
    import torch

    pcm_data, _rate = _read_wav_samples(wav_path)
    pcm_np = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32) / 32768.0
    return torch.from_numpy(pcm_np).to(device)


def _silero_get_speech_timestamps(
    audio,
    model,
    *,
    sampling_rate: int = 16000,
    threshold: float = 0.5,
    min_speech_duration_ms: int = 250,
    min_silence_duration_ms: int = 100,
    window_size_samples: int = 512,
) -> list[dict]:
    """Extract speech timestamps from audio using the Silero VAD JIT model.

    Reimplements the core logic of silero-vad's get_speech_timestamps
    so we don't need to import any hub/torchaudio/packaging code.
    """
    import torch

    min_speech_samples = sampling_rate * min_speech_duration_ms // 1000
    min_silence_samples = sampling_rate * min_silence_duration_ms // 1000

    model.reset_states()

    n_chunks = (len(audio) + window_size_samples - 1) // window_size_samples
    if len(audio) % window_size_samples != 0:
        padded = torch.nn.functional.pad(
            audio, (0, n_chunks * window_size_samples - len(audio))
        )
    else:
        padded = audio
    chunks = padded.reshape(n_chunks, window_size_samples)

    speech_probs = []
    for chunk in chunks:
        prob = model(chunk.unsqueeze(0), sampling_rate).item()
        speech_probs.append(prob)

    triggered = False
    speeches: list[dict] = []
    current_speech: dict = {}
    neg_threshold = threshold - 0.15

    for i, prob in enumerate(speech_probs):
        if prob >= threshold and not triggered:
            triggered = True
            current_speech["start"] = i * window_size_samples
        elif prob < neg_threshold and triggered:
            current_speech["end"] = i * window_size_samples
            if current_speech["end"] - current_speech["start"] >= min_speech_samples:
                speeches.append(current_speech)
            current_speech = {}
            triggered = False

    if triggered:
        current_speech["end"] = len(audio)
        if current_speech["end"] - current_speech["start"] >= min_speech_samples:
            speeches.append(current_speech)

    if speeches:
        merged = [speeches[0]]
        for s in speeches[1:]:
            if s["start"] - merged[-1]["end"] < min_silence_samples:
                merged[-1]["end"] = s["end"]
            else:
                merged.append(s)
        speeches = merged

    return speeches


def detect_speech_segments_silero(
    wav_path: Path,
    *,
    device: str = "cuda:0",
    min_speech_sec: float = 0.3,
    min_silence_sec: float = 0.4,
    return_energy_stats: bool = False,
) -> (
    list[tuple[float, float]]
    | tuple[list[tuple[float, float]], tuple[float, float, float, float]]
):
    """Silero-VAD-based speech detection returning (start, end) pairs in seconds.

    Loads the Silero VAD JIT model directly (no torch.hub / torchaudio needed).
    Return signature matches ``detect_speech_segments`` for drop-in use.
    """
    model = _load_silero_model(device)
    wav = _read_wav_as_tensor(wav_path, device)

    timestamps = _silero_get_speech_timestamps(
        wav,
        model,
        sampling_rate=SAMPLE_RATE,
        min_speech_duration_ms=int(min_speech_sec * 1000),
        min_silence_duration_ms=int(min_silence_sec * 1000),
    )

    segments = [
        (ts["start"] / SAMPLE_RATE, ts["end"] / SAMPLE_RATE)
        for ts in timestamps
    ]

    if return_energy_stats:
        return segments, (0.0, 0.0, 0.0, 0.0)
    return segments


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

    def stats_by_feed(self) -> list[dict]:
        """Per-feed aggregate stats: count, duration range, date range."""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT feed_id,
                       COUNT(*)              AS count,
                       MIN(duration_seconds) AS min_dur,
                       AVG(duration_seconds) AS avg_dur,
                       MAX(duration_seconds) AS max_dur,
                       SUM(duration_seconds) AS total_dur,
                       MIN(date)             AS earliest,
                       MAX(date)             AS latest
                FROM chunks GROUP BY feed_id ORDER BY feed_id
            """).fetchall()
        return [
            {
                "feed_id": r["feed_id"],
                "count": r["count"],
                "min_dur": round(r["min_dur"], 2),
                "avg_dur": round(r["avg_dur"], 2),
                "max_dur": round(r["max_dur"], 2),
                "total_dur": round(r["total_dur"], 1),
                "earliest": r["earliest"],
                "latest": r["latest"],
            }
            for r in rows
        ]

    def browse(
        self,
        feed_id: Optional[str] = None,
        date: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict]:
        """Paginated chunk listing for the explorer UI."""
        clauses: list[str] = []
        params: list[object] = []
        if feed_id:
            clauses.append("feed_id = ?")
            params.append(feed_id)
        if date:
            clauses.append("date = ?")
            params.append(date)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.extend([limit, offset])

        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM chunks{where} ORDER BY feed_id, source_file, offset_seconds LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [
            {
                "chunk_id": r["chunk_id"],
                "source_file": r["source_file"],
                "feed_id": r["feed_id"],
                "date": r["date"],
                "offset_seconds": round(r["offset_seconds"], 1),
                "duration_seconds": round(r["duration_seconds"], 1),
            }
            for r in rows
        ]


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
    vad_backend: str = "energy",
    vad_device: str = "cuda:0",
    progress_callback: Optional[callable] = None,
    collect_diagnostics: bool = False,
) -> list[ChunkInfo] | tuple[list[ChunkInfo], ChunkDiagnostics]:
    """Chunk a single audio file into training segments.

    Returns list of ChunkInfo for successfully created chunks.
    When *collect_diagnostics* is True, returns ``(chunks, diagnostics)``
    so callers can report why chunks were dropped.

    *vad_backend* selects the speech detection method: ``"energy"`` for
    the CPU-based RMS energy approach, ``"silero"`` for the GPU-accelerated
    Silero VAD neural network.
    """
    t_total_start = _time.perf_counter()
    diag = ChunkDiagnostics(source_file=audio_path.name, vad_backend=vad_backend)
    feed_id, date = _parse_feed_and_date(audio_path.name)
    chunks_dir = output_dir / feed_id / date
    chunks_dir.mkdir(parents=True, exist_ok=True)

    def _result(chunks: list[ChunkInfo]):
        diag.chunks_created = len(chunks)
        diag.total_time_sec = _time.perf_counter() - t_total_start
        if collect_diagnostics:
            return chunks, diag
        return chunks

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        wav_path = tmpdir_path / "source.wav"

        if preprocess_pipeline and preprocess_pipeline.steps:
            preprocessed = tmpdir_path / "preprocessed.wav"
            executor = PipelineExecutor()
            if not executor.run(audio_path, preprocess_pipeline, preprocessed):
                logger.error("Preprocessing failed for %s", audio_path)
                diag.conversion_ok = False
                return _result([])
            wav_path = preprocessed
        else:
            if not _convert_to_wav(audio_path, wav_path):
                logger.error("WAV conversion failed for %s", audio_path)
                diag.conversion_ok = False
                return _result([])

        t_vad_start = _time.perf_counter()
        if vad_backend == "silero":
            segments, e_stats = detect_speech_segments_silero(
                wav_path,
                device=vad_device,
                min_speech_sec=0.3,
                min_silence_sec=min_silence_sec,
                return_energy_stats=True,
            )
        else:
            segments, e_stats = detect_speech_segments(
                wav_path,
                energy_threshold=energy_threshold,
                min_silence_sec=min_silence_sec,
                merge_gap_sec=merge_gap_sec,
                return_energy_stats=True,
            )
        diag.vad_time_sec = _time.perf_counter() - t_vad_start
        diag.energy_mean, diag.energy_p50, diag.energy_p95, diag.energy_max = e_stats
        diag.segments_found = len(segments)

        t_extract_start = _time.perf_counter()
        results: list[ChunkInfo] = []
        for start, end in segments:
            duration = end - start
            if duration < min_duration:
                diag.segments_too_short += 1
                continue
            if duration > max_duration:
                diag.segments_too_long += 1
                continue
            diag.segments_in_range += 1

            cid = _chunk_id(audio_path.name, start)
            chunk_filename = f"{audio_path.stem}_chunk_{start:.1f}s.wav"
            chunk_path = chunks_dir / chunk_filename

            if not extract_chunk(wav_path, start, end, chunk_path, pad_sec=pad_seconds):
                diag.extract_failures += 1
                continue

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

        diag.extract_time_sec = _time.perf_counter() - t_extract_start

    logger.info(
        "Chunked %s [%s]: %d segments found, %d chunks kept (%.1f-%.1fs) in %.2fs",
        audio_path.name,
        vad_backend,
        len(segments),
        len(results),
        min_duration,
        max_duration,
        diag.total_time_sec,
    )
    return _result(results)
