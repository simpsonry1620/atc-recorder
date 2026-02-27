"""Post-label audio trimming using ASR word-level timestamps.

Trims labeled chunk WAVs so audio aligns precisely with the transcribed
text, removing leading/trailing dialog bleed from adjacent transmissions.
Original files are archived before modification.

See: https://github.com/simpsonry1620/atc-recorder/issues/13
"""

import shutil
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .chunker import SAMPLE_RATE, SAMPLE_WIDTH, _read_wav_samples, _write_wav
from .labeling import LabelStore, LabeledChunk
from .logging import get_logger

logger = get_logger(__name__)


@dataclass
class TrimResult:
    """Outcome of trimming a single chunk."""

    chunk_id: str
    success: bool
    trim_start_sec: float = 0.0
    trim_end_sec: float = 0.0
    original_duration: float = 0.0
    trimmed_duration: float = 0.0
    archived_path: str = ""
    error: str = ""
    skipped: bool = False
    skip_reason: str = ""


def _get_word_timestamps(
    audio_path: Path,
    whisper_client,
) -> Optional[tuple[float, float]]:
    """Run a quick Whisper pass to get first/last word timestamps.

    Returns (first_word_start, last_word_end) in seconds relative to the
    audio file, or None if word timestamps are unavailable.
    """
    result = whisper_client.transcribe_file(audio_path)
    if not result.success or not result.segments:
        return None

    first_start: Optional[float] = None
    last_end: Optional[float] = None

    for seg in result.segments:
        words = seg.get("words", [])
        if not words:
            if "start_time" in seg and "end_time" in seg:
                st = seg["start_time"]
                et = seg["end_time"]
                if first_start is None or st < first_start:
                    first_start = st
                if last_end is None or et > last_end:
                    last_end = et
            continue
        for w in words:
            st = w["start_time"]
            et = w["end_time"]
            if first_start is None or st < first_start:
                first_start = st
            if last_end is None or et > last_end:
                last_end = et

    if first_start is None or last_end is None:
        return None
    return (first_start, last_end)


def _trim_wav(
    input_path: Path,
    output_path: Path,
    start_sec: float,
    end_sec: float,
) -> float:
    """Trim a WAV file to [start_sec, end_sec] and return the new duration."""
    pcm_data, rate = _read_wav_samples(input_path)
    total_samples = len(pcm_data) // SAMPLE_WIDTH

    start_sample = max(0, int(start_sec * rate))
    end_sample = min(total_samples, int(end_sec * rate))

    byte_start = start_sample * SAMPLE_WIDTH
    byte_end = end_sample * SAMPLE_WIDTH
    trimmed_data = pcm_data[byte_start:byte_end]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_wav(output_path, trimmed_data, rate)

    return len(trimmed_data) / (SAMPLE_WIDTH * rate)


def trim_chunk(
    chunk: LabeledChunk,
    whisper_client,
    label_store: LabelStore,
    archive_dir: Path,
    *,
    onset_pad: float = 0.1,
    offset_pad: float = 0.1,
    min_trimmed_duration: float = 0.5,
) -> TrimResult:
    """Trim a single labeled chunk using Whisper word timestamps.

    Steps:
      1. Get word-level timestamps from Whisper
      2. Calculate trim boundaries with padding
      3. Archive the original WAV
      4. Write trimmed WAV to original path
      5. Update label DB with trim metadata
    """
    audio_path = Path(chunk.audio_path)
    result = TrimResult(chunk_id=chunk.chunk_id)

    if chunk.trim_start_sec is not None:
        result.skipped = True
        result.skip_reason = "already trimmed"
        return result

    if not audio_path.exists():
        result.error = f"audio file not found: {audio_path}"
        return result

    try:
        with wave.open(str(audio_path), "rb") as wf:
            original_duration = wf.getnframes() / wf.getframerate()
    except Exception as exc:
        result.error = f"cannot read WAV: {exc}"
        return result

    result.original_duration = original_duration

    timestamps = _get_word_timestamps(audio_path, whisper_client)
    if timestamps is None:
        result.error = "no word timestamps returned by Whisper"
        return result

    first_word_start, last_word_end = timestamps
    trim_start = max(0.0, first_word_start - onset_pad)
    trim_end = min(original_duration, last_word_end + offset_pad)

    if (trim_end - trim_start) < min_trimmed_duration:
        result.skipped = True
        result.skip_reason = (
            f"trimmed duration {trim_end - trim_start:.2f}s "
            f"below minimum {min_trimmed_duration}s"
        )
        return result

    savings = original_duration - (trim_end - trim_start)
    if savings < 0.05:
        result.skipped = True
        result.skip_reason = "trim would remove < 50ms, not worth it"
        return result

    archive_path = archive_dir / audio_path.parent.name / audio_path.name
    if chunk.feed_id and chunk.date:
        archive_path = archive_dir / chunk.feed_id / chunk.date / audio_path.name

    try:
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(audio_path, archive_path)
    except Exception as exc:
        result.error = f"archiving failed: {exc}"
        return result

    try:
        trimmed_duration = _trim_wav(audio_path, audio_path, trim_start, trim_end)
    except Exception as exc:
        # Restore from archive on failure
        shutil.copy2(archive_path, audio_path)
        result.error = f"trim failed: {exc}"
        return result

    label_store.update_trim(
        chunk_id=chunk.chunk_id,
        trim_start_sec=round(trim_start, 4),
        trim_end_sec=round(trim_end, 4),
        original_duration=round(original_duration, 4),
        original_audio_path=str(archive_path),
        new_duration=round(trimmed_duration, 4),
    )

    result.success = True
    result.trim_start_sec = trim_start
    result.trim_end_sec = trim_end
    result.trimmed_duration = trimmed_duration
    result.archived_path = str(archive_path)

    logger.info(
        "Trimmed %s: %.2fs -> %.2fs (removed %.2fs leading, %.2fs trailing)",
        audio_path.name,
        original_duration,
        trimmed_duration,
        trim_start,
        original_duration - trim_end,
    )
    return result


@dataclass
class TrimBatchResult:
    """Summary of a batch trim operation."""

    total: int = 0
    trimmed: int = 0
    skipped: int = 0
    errors: int = 0
    total_saved_sec: float = 0.0
    results: list[TrimResult] | None = None


def trim_labeled_chunks(
    label_store: LabelStore,
    whisper_client,
    archive_dir: Path,
    *,
    onset_pad: float = 0.1,
    offset_pad: float = 0.1,
    min_trimmed_duration: float = 0.5,
    status_filter: Optional[str] = None,
    feed_filter: Optional[str] = None,
    max_chunks: int = 0,
    progress_callback: Optional[callable] = None,
    collect_results: bool = False,
) -> TrimBatchResult:
    """Batch trim all untrimmed labeled chunks.

    Args:
        label_store: Label database
        whisper_client: WhisperClient for word timestamps
        archive_dir: Where to store original WAVs
        onset_pad: Seconds of padding before first word
        offset_pad: Seconds of padding after last word
        min_trimmed_duration: Skip chunks that would be shorter than this
        status_filter: Only trim chunks with this status (e.g. "accepted")
        feed_filter: Only trim chunks from this feed
        max_chunks: Limit number of chunks to process (0=all)
        progress_callback: Called with (index, total, TrimResult) after each chunk
        collect_results: If True, include per-chunk results in TrimBatchResult
    """
    chunks = label_store.list_untrimmed_chunks(
        status=status_filter,
        feed_id=feed_filter,
    )
    if max_chunks > 0:
        chunks = chunks[:max_chunks]

    batch = TrimBatchResult(total=len(chunks))
    if collect_results:
        batch.results = []

    for i, chunk in enumerate(chunks):
        result = trim_chunk(
            chunk,
            whisper_client,
            label_store,
            archive_dir,
            onset_pad=onset_pad,
            offset_pad=offset_pad,
            min_trimmed_duration=min_trimmed_duration,
        )

        if result.success:
            batch.trimmed += 1
            batch.total_saved_sec += result.original_duration - result.trimmed_duration
        elif result.skipped:
            batch.skipped += 1
        else:
            batch.errors += 1
            logger.warning("Trim error for %s: %s", chunk.chunk_id, result.error)

        if collect_results:
            batch.results.append(result)
        if progress_callback:
            progress_callback(i, len(chunks), result)

    return batch
