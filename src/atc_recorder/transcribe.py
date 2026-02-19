"""Transcription functionality using NVIDIA Whisper ASR via Riva."""

import json
import os
import re
import subprocess
import tempfile
import time
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional, Callable

from .config import Config
from .logging import get_logger

logger = get_logger(__name__)

_ATC_ROLE_PATTERNS = [
    re.compile(r"\bcleared\b", re.IGNORECASE),
    re.compile(r"\bcontact\b", re.IGNORECASE),
    re.compile(r"\brunway\b", re.IGNORECASE),
    re.compile(r"\bwind\b", re.IGNORECASE),
    re.compile(r"\bline up(?: and)? wait\b", re.IGNORECASE),
    re.compile(r"\bhold short\b", re.IGNORECASE),
    re.compile(r"\btaxi\b", re.IGNORECASE),
    re.compile(r"\bmaintain\b", re.IGNORECASE),
]

_PILOT_ROLE_PATTERNS = [
    re.compile(r"\bwith you\b", re.IGNORECASE),
    re.compile(r"\bready\b", re.IGNORECASE),
    re.compile(r"\brequest\b", re.IGNORECASE),
    re.compile(r"\bchecking in\b", re.IGNORECASE),
    re.compile(r"\bvisual\b", re.IGNORECASE),
    re.compile(r"\bcopy\b", re.IGNORECASE),
    re.compile(r"\broger\b", re.IGNORECASE),
]


def _stable_stitch_group_id(left_audio: str, right_audio: str, left_idx: int, right_idx: int) -> str:
    raw = f"{left_audio}:{left_idx}->{right_audio}:{right_idx}"
    return "stitch_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _coerce_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().split())


def _classify_speaker_role(text: str) -> tuple[str, float]:
    """Classify ATC role label for a transcript segment."""
    cleaned = _clean_text(text)
    if not cleaned:
        return "UNKNOWN", 0.0

    atc_hits = sum(1 for p in _ATC_ROLE_PATTERNS if p.search(cleaned))
    pilot_hits = sum(1 for p in _PILOT_ROLE_PATTERNS if p.search(cleaned))

    if atc_hits == 0 and pilot_hits == 0:
        return "UNKNOWN", 0.35
    if atc_hits > pilot_hits:
        return "ATC", min(0.95, 0.60 + 0.10 * atc_hits)
    if pilot_hits > atc_hits:
        return "PILOT", min(0.95, 0.60 + 0.10 * pilot_hits)
    return "UNKNOWN", 0.45


def apply_role_diarization(
    segments: list[dict],
    enabled: bool = False,
    mode: str = "role-heuristic",
) -> list[dict]:
    """Annotate segments with role-level diarization labels."""
    if not enabled or mode != "role-heuristic":
        return segments

    for segment in segments:
        role, confidence = _classify_speaker_role(segment.get("text", ""))
        segment["speaker_role"] = role
        segment["speaker_confidence"] = round(confidence, 3)
        if role == "ATC":
            segment["speaker_id"] = "spk_atc"
        elif role == "PILOT":
            segment["speaker_id"] = "spk_pilot"
        else:
            segment["speaker_id"] = "spk_unknown"
    return segments


def _merge_boundary_text(previous_text: str, current_text: str, min_overlap_chars: int) -> tuple[str, int]:
    """Merge boundary text while removing duplicated overlap suffix/prefix."""
    left = _clean_text(previous_text)
    right = _clean_text(current_text)
    if not left:
        return right, 0
    if not right:
        return left, 0

    max_overlap = min(len(left), len(right), 120)
    overlap_chars = 0
    for n in range(max_overlap, min_overlap_chars - 1, -1):
        if left[-n:].lower() == right[:n].lower():
            overlap_chars = n
            break

    if overlap_chars > 0:
        merged = f"{left} {right[overlap_chars:].lstrip()}".strip()
        return merged, overlap_chars
    return f"{left} {right}".strip(), 0


def _find_metadata_entry(metadata_path: Path, audio_file_name: str) -> Optional[dict]:
    if not metadata_path.exists():
        return None
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return None
    for item in data:
        if isinstance(item, dict) and item.get("file") == audio_file_name:
            return item
    return None


def _resolve_audio_start_time(transcript_path: Path, audio_file_name: str) -> Optional[datetime]:
    metadata_entry = _find_metadata_entry(transcript_path.parent / "metadata.json", audio_file_name)
    if metadata_entry and metadata_entry.get("start_time"):
        try:
            return datetime.fromisoformat(str(metadata_entry["start_time"]).replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            pass

    stem = Path(audio_file_name).stem
    pieces = stem.split("_")
    if len(pieces) >= 3:
        date_part = pieces[-2]
        time_part = pieces[-1].rstrip("Z")
        try:
            return datetime.strptime(f"{date_part} {time_part}", "%Y-%m-%d %H%M").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _ordered_transcript_audio_pairs(directory: Path) -> list[tuple[Path, str]]:
    pairs: list[tuple[Path, str, datetime]] = []
    for transcript_path in sorted(directory.glob("*.json")):
        if transcript_path.name == "metadata.json":
            continue
        try:
            payload = json.loads(transcript_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        audio_file = payload.get("audio_file")
        if not isinstance(audio_file, str) or not audio_file:
            continue
        start_dt = _resolve_audio_start_time(transcript_path, audio_file)
        if start_dt is None:
            continue
        pairs.append((transcript_path, audio_file, start_dt))
    pairs.sort(key=lambda item: item[2])
    return [(p[0], p[1]) for p in pairs]


class AudioPreprocess(Enum):
    """Audio preprocessing methods for noise reduction."""
    
    NONE = "none"           # No preprocessing
    FFMPEG = "ffmpeg"       # FFmpeg filters (highpass, lowpass, afftdn, dynaudnorm)
    FFMPEG_VAD = "ffmpeg_vad"  # FFmpeg with VAD to remove static/silence
    SOX = "sox"             # Sox noisered with auto noise profile


def preprocess_audio_ffmpeg(input_path: Path, output_path: Path) -> bool:
    """Apply ffmpeg-based audio preprocessing for ATC radio.
    
    Uses bandpass filtering (300-3400Hz for voice), FFT-based noise reduction,
    and dynamic normalization.
    
    Args:
        input_path: Input audio file
        output_path: Output WAV file path
        
    Returns:
        True if successful, False otherwise
    """
    # ATC radio voice band: 300Hz - 3400Hz
    # afftdn: FFT-based denoiser, nf=-25 is noise floor in dB
    # dynaudnorm: Dynamic audio normalizer for consistent levels
    filters = [
        "highpass=f=300",           # Remove low-frequency rumble/hum
        "lowpass=f=3400",           # Remove high-frequency hiss  
        "afftdn=nf=-25",            # FFT-based noise reduction
        "dynaudnorm=p=0.9:s=5",     # Normalize with 90% peak, 5s smoothing
    ]
    
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(input_path),
        "-af", ",".join(filters),
        "-ac", "1",
        "-ar", "16000",
        "-sample_fmt", "s16",
        "-f", "wav",
        str(output_path),
    ]
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
        
        if result.returncode != 0:
            logger.error(f"ffmpeg preprocessing failed: {result.stderr[:500]}")
            return False
        return True
        
    except Exception as e:
        logger.error(f"ffmpeg preprocessing error: {e}")
        return False


def preprocess_audio_ffmpeg_vad(input_path: Path, output_path: Path) -> bool:
    """Apply ffmpeg preprocessing with Voice Activity Detection.
    
    Applies noise reduction, then uses silenceremove to strip static/silence
    sections that cause Whisper to hallucinate. Keeps only segments with
    actual speech above the threshold.
    
    Args:
        input_path: Input audio file
        output_path: Output WAV file path
        
    Returns:
        True if successful, False otherwise
    """
    # Filter chain:
    # 1. Bandpass filter for voice frequencies
    # 2. Noise reduction
    # 3. silenceremove to strip static sections:
    #    - stop_periods=-1: remove all silence, not just at edges
    #    - stop_duration=0.3: min 0.3s of silence to trigger removal
    #    - stop_threshold=-30dB: audio below -30dB is considered silence/static
    #    - leave_silence=0.1: leave 0.1s of silence between speech for natural gaps
    # 4. Dynamic normalization
    filters = [
        "highpass=f=300",
        "lowpass=f=3400",
        "afftdn=nf=-20",  # Slightly more aggressive noise reduction
        "silenceremove=stop_periods=-1:stop_duration=0.3:stop_threshold=-30dB:leave_silence=0.1",
        "dynaudnorm=p=0.9:s=3",
    ]
    
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(input_path),
        "-af", ",".join(filters),
        "-ac", "1",
        "-ar", "16000",
        "-sample_fmt", "s16",
        "-f", "wav",
        str(output_path),
    ]
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
        
        if result.returncode != 0:
            logger.error(f"ffmpeg VAD preprocessing failed: {result.stderr[:500]}")
            return False
        
        # Check if output file has content (VAD might strip everything if too aggressive)
        if output_path.stat().st_size < 1000:
            logger.warning("VAD removed most audio - file may be mostly static")
        
        return True
        
    except Exception as e:
        logger.error(f"ffmpeg VAD preprocessing error: {e}")
        return False


def preprocess_audio_sox(input_path: Path, output_path: Path) -> bool:
    """Apply sox-based audio preprocessing with automatic noise profiling.
    
    Uses the first 0.5s of audio to build a noise profile, then applies
    noise reduction along with bandpass filtering.
    
    Args:
        input_path: Input audio file
        output_path: Output WAV file path
        
    Returns:
        True if successful, False otherwise
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        noise_sample = tmpdir / "noise_sample.wav"
        noise_profile = tmpdir / "noise.prof"
        intermediate = tmpdir / "intermediate.wav"
        
        try:
            # Step 1: Extract first 0.5s for noise profile (usually dead air/static)
            cmd_extract = [
                "sox",
                str(input_path),
                str(noise_sample),
                "trim", "0", "0.5",
                "rate", "16000",
                "channels", "1",
            ]
            result = subprocess.run(cmd_extract, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                logger.error(f"Sox noise sample extraction failed: {result.stderr[:300]}")
                return False
            
            # Step 2: Generate noise profile
            cmd_profile = [
                "sox",
                str(noise_sample),
                "-n",  # null output
                "noiseprof",
                str(noise_profile),
            ]
            result = subprocess.run(cmd_profile, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                logger.error(f"Sox noise profile failed: {result.stderr[:300]}")
                return False
            
            # Step 3: Apply noise reduction + bandpass filter
            # noisered 0.21 = 21% noise reduction (conservative to avoid artifacts)
            cmd_reduce = [
                "sox",
                str(input_path),
                str(intermediate),
                "rate", "16000",
                "channels", "1",
                "noisered", str(noise_profile), "0.21",
                "highpass", "300",
                "lowpass", "3400",
                "norm",  # Normalize audio levels
            ]
            result = subprocess.run(cmd_reduce, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                logger.error(f"Sox noise reduction failed: {result.stderr[:300]}")
                return False
            
            # Step 4: Convert to 16-bit WAV (sox output might be different bit depth)
            cmd_convert = [
                "ffmpeg",
                "-y",
                "-i", str(intermediate),
                "-ac", "1",
                "-ar", "16000",
                "-sample_fmt", "s16",
                "-f", "wav",
                str(output_path),
            ]
            result = subprocess.run(cmd_convert, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                logger.error(f"Final conversion failed: {result.stderr[:300]}")
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"Sox preprocessing error: {e}")
            return False


def detect_silence_intervals(
    wav_path: Path,
    min_silence_duration: float = 0.5,
    silence_threshold_dB: float = -30.0,
) -> list[tuple[float, float]]:
    """Detect silence intervals in a WAV file using ffmpeg silencedetect.

    Parses ffmpeg stderr for silence_start / silence_end and returns
    a list of (start_sec, end_sec) silence intervals.

    Args:
        wav_path: Path to the WAV file
        min_silence_duration: Minimum silence duration in seconds to report
        silence_threshold_dB: Audio below this dB level is considered silence

    Returns:
        List of (start_sec, end_sec) tuples for each silence interval
    """
    # silencedetect=n=-30dB:d=0.5 -> noise threshold -30dB, min duration 0.5s
    cmd = [
        "ffmpeg",
        "-i", str(wav_path),
        "-af", f"silencedetect=n={silence_threshold_dB}dB:d={min_silence_duration}",
        "-f", "null",
        "-",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
        )
        # silencedetect writes to stderr
        stderr = result.stderr or ""
    except Exception as e:
        logger.error(f"silencedetect failed: {e}")
        return []

    # Parse lines like:
    # [silencedetect @ 0x...] silence_start: 1.234
    # [silencedetect @ 0x...] silence_end: 2.345 | silence_duration: 1.111
    intervals = []
    start_re = re.compile(r"silence_start:\s*([\d.]+)")
    end_re = re.compile(r"silence_end:\s*([\d.]+)")
    pending_start = None

    for line in stderr.splitlines():
        m = start_re.search(line)
        if m:
            pending_start = float(m.group(1))
            continue
        m = end_re.search(line)
        if m and pending_start is not None:
            end = float(m.group(1))
            intervals.append((pending_start, end))
            pending_start = None

    return intervals


def get_speech_intervals(
    duration_seconds: float,
    silence_intervals: list[tuple[float, float]],
    min_speech_duration: float = 0.3,
    merge_gap_seconds: float = 0.5,
) -> list[tuple[float, float]]:
    """Convert silence intervals to speech intervals (gaps between silences).

    Args:
        duration_seconds: Total duration of the audio file
        silence_intervals: List of (start_sec, end_sec) silence intervals
        min_speech_duration: Drop speech intervals shorter than this
        merge_gap_seconds: Merge speech intervals separated by silence shorter than this

    Returns:
        List of (start_sec, end_sec) speech intervals
    """
    if duration_seconds <= 0:
        return []

    # Sort silences by start
    silences = sorted(silence_intervals, key=lambda x: x[0])

    # Build speech intervals: [0, s0_start], [s0_end, s1_start], ..., [sN_end, duration]
    speech = []
    prev_end = 0.0

    for s_start, s_end in silences:
        if s_start > prev_end:
            seg = (prev_end, s_start)
            if seg[1] - seg[0] >= min_speech_duration:
                if speech and (seg[0] - speech[-1][1]) < merge_gap_seconds:
                    # Merge with previous
                    speech[-1] = (speech[-1][0], seg[1])
                else:
                    speech.append(seg)
        prev_end = max(prev_end, s_end)

    if prev_end < duration_seconds and (duration_seconds - prev_end) >= min_speech_duration:
        if speech and (prev_end - speech[-1][1]) < merge_gap_seconds:
            speech[-1] = (speech[-1][0], duration_seconds)
        else:
            speech.append((prev_end, duration_seconds))

    return speech


def _get_wav_duration(wav_path: Path) -> float:
    """Get duration in seconds of a WAV file using ffprobe."""
    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(wav_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return float(result.stdout.strip())
    except Exception as e:
        logger.error(f"Failed to get audio duration: {e}")
        return 0.0


# Try to import optional dependencies for transcription
try:
    import riva.client
    from riva.client.auth import Auth
    RIVA_AVAILABLE = True
except ImportError:
    RIVA_AVAILABLE = False
    logger.warning("nvidia-riva-client not installed. Transcription features unavailable.")

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileCreatedEvent
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False
    logger.warning("watchdog not installed. File watching features unavailable.")


@dataclass
class TranscriptionResult:
    """Result of a transcription operation."""
    
    success: bool
    audio_file: Path
    transcript_file: Optional[Path] = None
    text: str = ""
    language: str = ""
    duration_seconds: float = 0.0
    segments: list = field(default_factory=list)
    error: Optional[str] = None
    transcribed_at: Optional[datetime] = None


class WhisperClient:
    """Client for NVIDIA Whisper ASR via Riva gRPC."""
    
    def __init__(
        self,
        grpc_host: str = "localhost",
        grpc_port: int = 50051,
        language_code: str = "en-US",
    ):
        """Initialize the Whisper client.
        
        Args:
            grpc_host: Hostname of the Whisper gRPC service
            grpc_port: Port of the Whisper gRPC service
            language_code: BCP-47 language code for transcription
        """
        if not RIVA_AVAILABLE:
            raise RuntimeError(
                "nvidia-riva-client is not installed. "
                "Install with: pip install nvidia-riva-client"
            )
        
        self.grpc_host = grpc_host
        self.grpc_port = grpc_port
        self.language_code = language_code
        self._auth = None
        self._asr_service = None
    
    @property
    def server_uri(self) -> str:
        """Get the server URI."""
        return f"{self.grpc_host}:{self.grpc_port}"
    
    def _get_auth(self) -> "Auth":
        """Get or create the authentication object."""
        if self._auth is None:
            self._auth = Auth(uri=self.server_uri, use_ssl=False)
        return self._auth
    
    def _get_asr_service(self) -> "riva.client.ASRService":
        """Get or create the ASR service client."""
        if self._asr_service is None:
            self._asr_service = riva.client.ASRService(self._get_auth())
        return self._asr_service
    
    def check_connection(self) -> bool:
        """Check if the Whisper service is available.
        
        Returns:
            True if the service is healthy, False otherwise
        """
        import grpc
        
        try:
            # Try to create a connection
            channel = grpc.insecure_channel(self.server_uri)
            # Use a short timeout for the health check
            grpc.channel_ready_future(channel).result(timeout=5)
            channel.close()
            return True
        except Exception as e:
            logger.debug(f"Connection check failed: {e}")
            return False
    
    def transcribe_file(self, audio_path: Path) -> TranscriptionResult:
        """Transcribe an audio file.
        
        The file must be in WAV format (mono, 16-bit).
        For MP3 files, use convert_and_transcribe() instead.
        
        Args:
            audio_path: Path to the audio file
            
        Returns:
            TranscriptionResult object
        """
        audio_path = Path(audio_path)
        
        if not audio_path.exists():
            return TranscriptionResult(
                success=False,
                audio_file=audio_path,
                error=f"Audio file not found: {audio_path}",
            )
        
        try:
            # Read the audio file
            with open(audio_path, 'rb') as f:
                audio_data = f.read()
            
            # Get ASR service
            asr_service = self._get_asr_service()
            
            # Configure recognition. NIM Whisper may not populate alt.words; segment-by-pauses
            # path does not depend on word timings.
            config = riva.client.RecognitionConfig(
                language_code=self.language_code,
                max_alternatives=1,
                enable_automatic_punctuation=True,
                enable_word_time_offsets=True,
            )
            
            # Perform offline recognition
            response = asr_service.offline_recognize(audio_data, config)
            
            # Extract transcript
            full_text = ""
            segments = []
            
            for result in response.results:
                if result.alternatives:
                    alt = result.alternatives[0]
                    full_text += alt.transcript + " "
                    
                    # Extract word-level timing if available (may be empty with NIM Whisper)
                    words = []
                    for word_info in alt.words:
                        words.append({
                            "word": word_info.word,
                            "start_time": word_info.start_time,
                            "end_time": word_info.end_time,
                            "confidence": word_info.confidence,
                        })
                    
                    seg = {
                        "text": alt.transcript,
                        "confidence": alt.confidence,
                    }
                    if words:
                        seg["words"] = words
                        seg["start_time"] = words[0]["start_time"]
                        seg["end_time"] = words[-1]["end_time"]
                    segments.append(seg)
            
            return TranscriptionResult(
                success=True,
                audio_file=audio_path,
                text=full_text.strip(),
                language=self.language_code,
                segments=segments,
                transcribed_at=datetime.now(timezone.utc),
            )
            
        except Exception as e:
            logger.error(f"Transcription failed: {e}")
            return TranscriptionResult(
                success=False,
                audio_file=audio_path,
                error=str(e),
            )
    
    def convert_and_transcribe(
        self,
        audio_path: Path,
        preprocess: AudioPreprocess = AudioPreprocess.NONE,
        segment_by_pauses: bool = False,
        min_silence_duration: float = 0.5,
        silence_threshold_dB: float = -30.0,
        min_speech_duration: float = 0.3,
        merge_gap_seconds: float = 0.5,
        diarization_enabled: bool = False,
        diarization_mode: str = "role-heuristic",
    ) -> TranscriptionResult:
        """Convert audio to WAV and transcribe.

        Handles MP3 and other formats by converting to mono 16-bit WAV.
        For long audio files, automatically chunks to avoid gRPC size limits.
        When segment_by_pauses is True, segments by silence and timestamps each segment.

        Args:
            audio_path: Path to the audio file (MP3, WAV, etc.)
            preprocess: Audio preprocessing method for noise reduction
            segment_by_pauses: If True, segment by silence and timestamp each segment
            min_silence_duration: Min silence duration (s) for pause detection
            silence_threshold_dB: dB level below which audio is considered silence
            min_speech_duration: Drop speech intervals shorter than this (s)
            merge_gap_seconds: Merge speech intervals separated by silence shorter than this

        Returns:
            TranscriptionResult object
        """
        audio_path = Path(audio_path)
        
        if not audio_path.exists():
            return TranscriptionResult(
                success=False,
                audio_file=audio_path,
                error=f"Audio file not found: {audio_path}",
            )
        
        # Convert to WAV using ffmpeg (with optional preprocessing)
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp:
            wav_path = Path(tmp.name)
        
        try:
            # Apply preprocessing if requested
            if preprocess == AudioPreprocess.FFMPEG:
                logger.info(f"Preprocessing with ffmpeg filters: {audio_path.name}")
                success = preprocess_audio_ffmpeg(audio_path, wav_path)
                if not success:
                    return TranscriptionResult(
                        success=False,
                        audio_file=audio_path,
                        error="ffmpeg preprocessing failed",
                    )
            elif preprocess == AudioPreprocess.FFMPEG_VAD:
                logger.info(f"Preprocessing with ffmpeg VAD: {audio_path.name}")
                success = preprocess_audio_ffmpeg_vad(audio_path, wav_path)
                if not success:
                    return TranscriptionResult(
                        success=False,
                        audio_file=audio_path,
                        error="ffmpeg VAD preprocessing failed",
                    )
            elif preprocess == AudioPreprocess.SOX:
                logger.info(f"Preprocessing with sox noisered: {audio_path.name}")
                success = preprocess_audio_sox(audio_path, wav_path)
                if not success:
                    return TranscriptionResult(
                        success=False,
                        audio_file=audio_path,
                        error="sox preprocessing failed",
                    )
            else:
                # No preprocessing - just convert to WAV
                cmd = [
                    "ffmpeg",
                    "-y",  # Overwrite output
                    "-i", str(audio_path),
                    "-ac", "1",  # Mono
                    "-ar", "16000",  # 16kHz sample rate
                    "-sample_fmt", "s16",  # 16-bit
                    "-f", "wav",
                    str(wav_path),
                ]
                
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=300,  # 5 minute timeout
                )
                
                if result.returncode != 0:
                    return TranscriptionResult(
                        success=False,
                        audio_file=audio_path,
                        error=f"ffmpeg conversion failed: {result.stderr[:500]}",
                    )
            
            if segment_by_pauses:
                logger.info(f"Segmenting by pauses: {audio_path.name}")
                return self._transcribe_by_pauses(
                    wav_path,
                    audio_path,
                    min_silence_duration=min_silence_duration,
                    silence_threshold_dB=silence_threshold_dB,
                    min_speech_duration=min_speech_duration,
                    merge_gap_seconds=merge_gap_seconds,
                    diarization_enabled=diarization_enabled,
                    diarization_mode=diarization_mode,
                )
            
            # Check file size - gRPC has a 4MB limit
            # At 16kHz 16-bit mono = 32KB/sec, so 4MB = ~120 seconds
            # Use 90 seconds per chunk to be safe
            wav_size = wav_path.stat().st_size
            max_chunk_size = 3 * 1024 * 1024  # 3MB to stay safely under 4MB limit
            
            if wav_size <= max_chunk_size:
                # Small file - transcribe directly
                transcription = self.transcribe_file(wav_path)
                transcription.audio_file = audio_path
                apply_role_diarization(
                    transcription.segments,
                    enabled=diarization_enabled,
                    mode=diarization_mode,
                )
                return transcription
            else:
                # Large file - transcribe in chunks
                logger.info(f"Large file ({wav_size / 1024 / 1024:.1f}MB), transcribing in chunks")
                transcription = self._transcribe_chunked(wav_path, audio_path)
                apply_role_diarization(
                    transcription.segments,
                    enabled=diarization_enabled,
                    mode=diarization_mode,
                )
                return transcription
            
        finally:
            # Clean up temp file
            if wav_path.exists():
                wav_path.unlink()
    
    def _transcribe_chunked(self, wav_path: Path, original_path: Path) -> TranscriptionResult:
        """Transcribe a large WAV file by splitting into chunks.
        
        Args:
            wav_path: Path to the WAV file to transcribe
            original_path: Original audio file path (for result metadata)
            
        Returns:
            TranscriptionResult with combined transcription
        """
        # Calculate chunk duration: 3MB at 32KB/sec = ~94 seconds
        # Use 90 seconds per chunk for safety
        chunk_duration_sec = 90
        
        # Get audio duration using ffprobe
        try:
            cmd = [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(wav_path),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            total_duration = float(result.stdout.strip())
        except Exception as e:
            logger.error(f"Failed to get audio duration: {e}")
            # Estimate from file size (32KB/sec for 16kHz 16-bit mono)
            total_duration = wav_path.stat().st_size / 32000
        
        logger.info(f"Audio duration: {total_duration:.1f}s, chunking into {chunk_duration_sec}s segments")
        
        all_text = []
        all_segments = []
        chunk_num = 0
        start_time = 0.0
        
        while start_time < total_duration:
            chunk_num += 1
            end_time = min(start_time + chunk_duration_sec, total_duration)
            
            # Extract chunk using ffmpeg
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp:
                chunk_path = Path(tmp.name)
            
            try:
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-i", str(wav_path),
                    "-ss", str(start_time),
                    "-t", str(chunk_duration_sec),
                    "-c", "copy",  # No re-encoding needed
                    str(chunk_path),
                ]
                
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                
                if result.returncode != 0:
                    logger.error(f"Failed to extract chunk {chunk_num}: {result.stderr[:200]}")
                    start_time = end_time
                    continue
                
                # Transcribe chunk
                logger.debug(f"Transcribing chunk {chunk_num} ({start_time:.1f}s - {end_time:.1f}s)")
                chunk_result = self.transcribe_file(chunk_path)
                
                if chunk_result.success and chunk_result.text:
                    all_text.append(chunk_result.text)
                    
                    # Adjust segment timestamps to be relative to original file
                    for segment in chunk_result.segments:
                        adjusted_segment = segment.copy()
                        if 'words' in adjusted_segment:
                            for word in adjusted_segment['words']:
                                if 'start_time' in word:
                                    word['start_time'] += start_time
                                if 'end_time' in word:
                                    word['end_time'] += start_time
                        all_segments.append(adjusted_segment)
                elif not chunk_result.success:
                    logger.warning(f"Chunk {chunk_num} transcription failed: {chunk_result.error}")
                
            finally:
                if chunk_path.exists():
                    chunk_path.unlink()
            
            start_time = end_time
        
        # Combine results
        combined_text = " ".join(all_text)
        
        return TranscriptionResult(
            success=True,
            audio_file=original_path,
            text=combined_text,
            language=self.language_code,
            duration_seconds=total_duration,
            segments=all_segments,
            transcribed_at=datetime.now(timezone.utc),
        )

    def _transcribe_by_pauses(
        self,
        wav_path: Path,
        original_path: Path,
        min_silence_duration: float = 0.5,
        silence_threshold_dB: float = -30.0,
        min_speech_duration: float = 0.3,
        merge_gap_seconds: float = 0.5,
        diarization_enabled: bool = False,
        diarization_mode: str = "role-heuristic",
    ) -> TranscriptionResult:
        """Transcribe by segmenting on silence, then transcribing each speech interval.

        Each segment gets start_time/end_time from silence detection, so timestamps
        are available even when ASR does not return word timings.
        """
        duration = _get_wav_duration(wav_path)
        if duration <= 0:
            return TranscriptionResult(
                success=False,
                audio_file=original_path,
                error="Could not get audio duration",
            )

        silence_intervals = detect_silence_intervals(
            wav_path,
            min_silence_duration=min_silence_duration,
            silence_threshold_dB=silence_threshold_dB,
        )
        speech_intervals = get_speech_intervals(
            duration,
            silence_intervals,
            min_speech_duration=min_speech_duration,
            merge_gap_seconds=merge_gap_seconds,
        )

        if not speech_intervals:
            # No speech detected - transcribe whole file as one segment
            speech_intervals = [(0.0, duration)]

        max_chunk_sec = 90
        all_segments = []
        all_text = []

        for seg_start, seg_end in speech_intervals:
            seg_duration = seg_end - seg_start
            if seg_duration <= 0:
                continue

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                slice_path = Path(tmp.name)

            try:
                segment_words = None
                if seg_duration > max_chunk_sec:
                    # Chunk this segment and transcribe each part (no word-level timings)
                    chunk_texts = []
                    t = seg_start
                    while t < seg_end:
                        chunk_len = min(max_chunk_sec, seg_end - t)
                        cmd = [
                            "ffmpeg", "-y",
                            "-i", str(wav_path),
                            "-ss", str(t),
                            "-t", str(chunk_len),
                            "-ac", "1", "-ar", "16000", "-sample_fmt", "s16",
                            "-f", "wav", str(slice_path),
                        ]
                        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                        if result.returncode != 0:
                            logger.warning(f"Failed to extract slice at {t}: {result.stderr[:200]}")
                            t += chunk_len
                            continue
                        chunk_result = self.transcribe_file(slice_path)
                        if chunk_result.success and chunk_result.text:
                            chunk_texts.append(chunk_result.text)
                        t += chunk_len
                    seg_text = " ".join(chunk_texts).strip()
                else:
                    cmd = [
                        "ffmpeg", "-y",
                        "-i", str(wav_path),
                        "-ss", str(seg_start),
                        "-t", str(seg_duration),
                        "-ac", "1", "-ar", "16000", "-sample_fmt", "s16",
                        "-f", "wav", str(slice_path),
                    ]
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                    if result.returncode != 0:
                        logger.warning(f"Failed to extract segment {seg_start:.1f}-{seg_end:.1f}")
                        continue
                    chunk_result = self.transcribe_file(slice_path)
                    seg_text = chunk_result.text.strip() if chunk_result.success else ""
                    if chunk_result.success and chunk_result.segments:
                        first = chunk_result.segments[0]
                        if "words" in first:
                            segment_words = first["words"]
                            for w in segment_words:
                                w["start_time"] = w.get("start_time", 0) + seg_start
                                w["end_time"] = w.get("end_time", 0) + seg_start

                all_text.append(seg_text)
                segment_record = {
                    "start_time": seg_start,
                    "end_time": seg_end,
                    "text": seg_text,
                }
                if segment_words:
                    segment_record["words"] = segment_words
                all_segments.append(segment_record)
            finally:
                if slice_path.exists():
                    slice_path.unlink()

        visible_segments = [
            s for s in all_segments
            if _clean_text(s.get("text", "")) and _clean_text(s.get("text", "")) != "..."
        ]
        if not visible_segments:
            # Some ASR backends can return empty strings on short VAD slices.
            # Fall back to whole-file/chunked transcription so we still return usable text.
            logger.warning(
                "Pause-segmented transcription produced no text for %s; "
                "falling back to whole-audio transcription",
                original_path.name,
            )
            max_chunk_size = 3 * 1024 * 1024
            wav_size = wav_path.stat().st_size
            if wav_size <= max_chunk_size:
                fallback = self.transcribe_file(wav_path)
                fallback.audio_file = original_path
            else:
                fallback = self._transcribe_chunked(wav_path, original_path)
            apply_role_diarization(
                fallback.segments,
                enabled=diarization_enabled,
                mode=diarization_mode,
            )
            return fallback

        return TranscriptionResult(
            success=True,
            audio_file=original_path,
            text=" ".join(all_text).strip(),
            language=self.language_code,
            duration_seconds=duration,
            segments=apply_role_diarization(
                all_segments,
                enabled=diarization_enabled,
                mode=diarization_mode,
            ),
            transcribed_at=datetime.now(timezone.utc),
        )


def convert_mp3_to_wav(mp3_path: Path, wav_path: Optional[Path] = None) -> Optional[Path]:
    """Convert an MP3 file to WAV format suitable for Whisper.
    
    Args:
        mp3_path: Path to the MP3 file
        wav_path: Optional output path. If None, uses same name with .wav extension.
        
    Returns:
        Path to the WAV file, or None if conversion failed
    """
    mp3_path = Path(mp3_path)
    
    if wav_path is None:
        wav_path = mp3_path.with_suffix('.wav')
    else:
        wav_path = Path(wav_path)
    
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(mp3_path),
        "-ac", "1",  # Mono
        "-ar", "16000",  # 16kHz
        "-sample_fmt", "s16",  # 16-bit
        "-f", "wav",
        str(wav_path),
    ]
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
        
        if result.returncode == 0 and wav_path.exists():
            return wav_path
        else:
            logger.error(f"ffmpeg conversion failed: {result.stderr[:500]}")
            return None
            
    except Exception as e:
        logger.error(f"Conversion error: {e}")
        return None


def _format_timestamp_txt(seconds: float) -> str:
    """Format seconds as [MM:SS.mmm] for timestamped text."""
    m = int(seconds // 60)
    s = seconds % 60
    return f"[{m:02d}:{s:06.3f}]"


def _format_srt_time(seconds: float) -> str:
    """Format seconds as HH:MM:SS,mmm for SRT."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    sec_int = int(s)
    millis = int((s - sec_int) * 1000)
    return f"{h:02d}:{m:02d}:{sec_int:02d},{millis:03d}"


def export_timestamped_txt(
    result: TranscriptionResult,
    output_path: Optional[Path] = None,
    periodic_interval_sec: float = 0,
) -> Optional[Path]:
    """Export transcript as timestamped text: [MM:SS.mmm - MM:SS.mmm] text.

    Only segments with start_time and end_time are included. If periodic_interval_sec
    is set (e.g. 30 or 60), inserts marker lines at that interval for easier scanning.
    """
    if output_path is None:
        output_path = result.audio_file.with_suffix(".txt")
    else:
        output_path = Path(output_path)

    timed_segments = [
        s for s in result.segments
        if isinstance(s.get("start_time"), (int, float)) and isinstance(s.get("end_time"), (int, float))
    ]
    if not timed_segments:
        logger.warning("No segments with start_time/end_time for timestamped export")
        return None

    lines = []
    last_marker = -1
    for seg in timed_segments:
        start = float(seg["start_time"])
        end = float(seg["end_time"])
        text = seg.get("text", "").strip()
        if periodic_interval_sec > 0:
            t = int(start // periodic_interval_sec) * periodic_interval_sec
            while t > last_marker and t <= end:
                if t >= start:
                    lines.append(f"{_format_timestamp_txt(t)} ---")
                last_marker = t
                t += periodic_interval_sec
        lines.append(f"{_format_timestamp_txt(start)} - {_format_timestamp_txt(end)} {text}")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return output_path


def export_srt(result: TranscriptionResult, output_path: Optional[Path] = None) -> Optional[Path]:
    """Export transcript as SRT subtitle format."""
    if output_path is None:
        output_path = result.audio_file.with_suffix(".srt")
    else:
        output_path = Path(output_path)

    timed_segments = [
        s for s in result.segments
        if isinstance(s.get("start_time"), (int, float)) and isinstance(s.get("end_time"), (int, float))
    ]
    if not timed_segments:
        logger.warning("No segments with start_time/end_time for SRT export")
        return None

    blocks = []
    for i, seg in enumerate(timed_segments, 1):
        start = float(seg["start_time"])
        end = float(seg["end_time"])
        text = seg.get("text", "").strip().replace("\n", " ")
        blocks.append(f"{i}\n{_format_srt_time(start)} --> {_format_srt_time(end)}\n{text}\n")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(blocks))
    return output_path


def save_transcript(result: TranscriptionResult, output_path: Optional[Path] = None) -> Path:
    """Save a transcription result to a JSON file.
    
    Args:
        result: TranscriptionResult object
        output_path: Optional output path. If None, saves alongside audio file.
        
    Returns:
        Path to the saved transcript file
    """
    if output_path is None:
        output_path = result.audio_file.with_suffix('.json')
    else:
        output_path = Path(output_path)
    
    transcript_data = {
        "audio_file": result.audio_file.name,
        "language": result.language,
        "text": result.text,
        "segments": result.segments,
        "transcribed_at": result.transcribed_at.isoformat() if result.transcribed_at else None,
        "success": result.success,
    }
    
    if result.error:
        transcript_data["error"] = result.error
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(transcript_data, f, indent=2, ensure_ascii=False)
    
    result.transcript_file = output_path
    return output_path


def stitch_transcript_boundary_with_previous(
    transcript_path: Path,
    max_gap_seconds: float = 2.0,
    min_text_overlap_chars: int = 12,
) -> bool:
    """Stitch boundary text between previous transcript and current transcript.

    Returns True when a boundary stitch was applied.
    """
    transcript_path = Path(transcript_path)
    if not transcript_path.exists():
        return False

    try:
        current_data = json.loads(transcript_path.read_text(encoding="utf-8"))
    except Exception:
        return False

    current_audio = current_data.get("audio_file")
    current_segments = current_data.get("segments")
    if not isinstance(current_audio, str) or not isinstance(current_segments, list) or not current_segments:
        return False

    ordered_pairs = _ordered_transcript_audio_pairs(transcript_path.parent)
    idx = next((i for i, (p, _) in enumerate(ordered_pairs) if p == transcript_path), -1)
    if idx <= 0:
        return False
    prev_path, prev_audio = ordered_pairs[idx - 1]

    try:
        prev_data = json.loads(prev_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    prev_segments = prev_data.get("segments")
    if not isinstance(prev_segments, list) or not prev_segments:
        return False

    prev_start = _resolve_audio_start_time(prev_path, prev_audio)
    curr_start = _resolve_audio_start_time(transcript_path, current_audio)
    if prev_start is None or curr_start is None:
        return False

    prev_last_idx = len(prev_segments) - 1
    prev_last = prev_segments[prev_last_idx]
    curr_first_idx = 0
    curr_first = current_segments[curr_first_idx]
    prev_last_end = _coerce_float(prev_last.get("end_time"), 0.0)
    prev_last_abs_end = prev_start + timedelta(seconds=max(0.0, prev_last_end))
    gap_seconds = (curr_start - prev_last_abs_end).total_seconds()
    if gap_seconds < -max_gap_seconds or gap_seconds > max_gap_seconds:
        return False

    merged_text, overlap_chars = _merge_boundary_text(
        previous_text=str(prev_last.get("text", "")),
        current_text=str(curr_first.get("text", "")),
        min_overlap_chars=min_text_overlap_chars,
    )
    if not merged_text:
        return False

    stitch_id = _stable_stitch_group_id(prev_audio, current_audio, prev_last_idx, curr_first_idx)

    prev_last["stitch_next"] = {
        "stitch_group_id": stitch_id,
        "audio_file": current_audio,
        "segment_index": curr_first_idx,
        "gap_seconds": round(gap_seconds, 3),
    }
    prev_last["stitched_canonical_text"] = merged_text
    prev_last["skip_for_ingest"] = True

    curr_first["text"] = merged_text
    curr_first["stitched_with_previous"] = {
        "stitch_group_id": stitch_id,
        "audio_file": prev_audio,
        "segment_index": prev_last_idx,
        "gap_seconds": round(gap_seconds, 3),
        "overlap_chars": overlap_chars,
    }
    curr_first["source_audio_files"] = [prev_audio, current_audio]
    curr_first["source_segment_refs"] = [
        {"audio_file": prev_audio, "segment_index": prev_last_idx},
        {"audio_file": current_audio, "segment_index": curr_first_idx},
    ]

    prev_data["segments"] = prev_segments
    current_data["segments"] = current_segments
    prev_data["text"] = " ".join(_clean_text(seg.get("text", "")) for seg in prev_segments).strip()
    current_data["text"] = " ".join(_clean_text(seg.get("text", "")) for seg in current_segments).strip()

    prev_path.write_text(json.dumps(prev_data, indent=2, ensure_ascii=False), encoding="utf-8")
    transcript_path.write_text(json.dumps(current_data, indent=2, ensure_ascii=False), encoding="utf-8")
    return True


def stitch_transcripts_in_directory(
    directory: Path,
    max_gap_seconds: float = 2.0,
    min_text_overlap_chars: int = 12,
) -> int:
    """Stitch adjacent transcript boundaries for one feed/date directory."""
    directory = Path(directory)
    if not directory.exists():
        return 0
    stitched_count = 0
    ordered_pairs = _ordered_transcript_audio_pairs(directory)
    for transcript_path, _audio_file in ordered_pairs[1:]:
        if stitch_transcript_boundary_with_previous(
            transcript_path,
            max_gap_seconds=max_gap_seconds,
            min_text_overlap_chars=min_text_overlap_chars,
        ):
            stitched_count += 1
    return stitched_count


def refresh_result_from_saved_transcript(result: TranscriptionResult) -> None:
    """Reload text/segments from saved transcript JSON into an in-memory result."""
    if not result.transcript_file or not result.transcript_file.exists():
        return
    try:
        payload = json.loads(result.transcript_file.read_text(encoding="utf-8"))
    except Exception:
        return
    segments = payload.get("segments")
    text = payload.get("text")
    if isinstance(segments, list):
        result.segments = segments
    if isinstance(text, str):
        result.text = text


class TranscriptionWatcher:
    """Watch for new audio files and transcribe them automatically."""

    def __init__(
        self,
        watch_dir: Path,
        client: WhisperClient,
        on_transcription: Optional[Callable[[TranscriptionResult], None]] = None,
        file_patterns: list[str] = None,
        preprocess: AudioPreprocess = AudioPreprocess.NONE,
        segment_by_pauses: bool = False,
        min_silence_duration: float = 0.5,
        silence_threshold_dB: float = -30.0,
        min_speech_duration: float = 0.3,
        merge_gap_seconds: float = 0.5,
        output_format: str = "json",
        on_transcript_saved: Optional[Callable[[Path], None]] = None,
        diarization_enabled: bool = False,
        diarization_mode: str = "role-heuristic",
        stitch_across_files: bool = False,
        stitch_max_gap_seconds: float = 2.0,
        stitch_min_text_overlap_chars: int = 12,
    ):
        """Initialize the watcher.

        Args:
            watch_dir: Directory to watch for new audio files
            client: WhisperClient for transcription
            on_transcription: Optional callback when transcription completes
            file_patterns: File extensions to watch (default: ['.mp3'])
            preprocess: Audio preprocessing method to apply before ASR
            segment_by_pauses: If True, segment by silence and timestamp each segment
            min_silence_duration: Min silence duration (s) for pause detection
            silence_threshold_dB: dB level for silence detection
            min_speech_duration: Min speech interval length (s)
            merge_gap_seconds: Merge speech intervals separated by shorter silence
            output_format: json, timestamped-txt, or srt (additional file when not json)
        """
        if not WATCHDOG_AVAILABLE:
            raise RuntimeError(
                "watchdog is not installed. "
                "Install with: pip install watchdog"
            )

        self.watch_dir = Path(watch_dir)
        self.client = client
        self.on_transcription = on_transcription
        self.file_patterns = file_patterns or ['.mp3']
        self._preprocess = preprocess
        self._segment_by_pauses = segment_by_pauses
        self._min_silence_duration = min_silence_duration
        self._silence_threshold_dB = silence_threshold_dB
        self._min_speech_duration = min_speech_duration
        self._merge_gap_seconds = merge_gap_seconds
        self._output_format = output_format
        self._on_transcript_saved = on_transcript_saved
        self._diarization_enabled = diarization_enabled
        self._diarization_mode = diarization_mode
        self._stitch_across_files = stitch_across_files
        self._stitch_max_gap_seconds = stitch_max_gap_seconds
        self._stitch_min_text_overlap_chars = stitch_min_text_overlap_chars
        self._observer = None
        self._running = False
    
    def _should_transcribe(self, path: Path) -> bool:
        """Check if a file should be transcribed.
        
        Args:
            path: Path to check
            
        Returns:
            True if the file should be transcribed
        """
        # Check extension
        if path.suffix.lower() not in self.file_patterns:
            return False
        
        # Check if transcript already exists
        transcript_path = path.with_suffix('.json')
        if transcript_path.exists():
            return False
        
        return True
    
    def _handle_new_file(self, path: Path) -> None:
        """Handle a new audio file.
        
        Args:
            path: Path to the new file
        """
        if not self._should_transcribe(path):
            return
        
        # Wait a bit for file to be fully written
        time.sleep(2)
        
        if not path.exists():
            return
        
        logger.info(f"Transcribing: {path}")

        try:
            result = self.client.convert_and_transcribe(
                path,
                preprocess=self._preprocess,
                segment_by_pauses=self._segment_by_pauses,
                min_silence_duration=self._min_silence_duration,
                silence_threshold_dB=self._silence_threshold_dB,
                min_speech_duration=self._min_speech_duration,
                merge_gap_seconds=self._merge_gap_seconds,
                diarization_enabled=self._diarization_enabled,
                diarization_mode=self._diarization_mode,
            )

            if result.success:
                save_transcript(result)
                if result.transcript_file and self._stitch_across_files:
                    try:
                        stitched = stitch_transcript_boundary_with_previous(
                            result.transcript_file,
                            max_gap_seconds=self._stitch_max_gap_seconds,
                            min_text_overlap_chars=self._stitch_min_text_overlap_chars,
                        )
                        if stitched:
                            logger.info(f"Boundary stitched: {result.transcript_file}")
                            refresh_result_from_saved_transcript(result)
                    except Exception as exc:
                        logger.error(f"Boundary stitching failed for {result.transcript_file}: {exc}")
                logger.info(f"Transcription saved: {result.transcript_file}")
                if result.transcript_file and self._on_transcript_saved:
                    try:
                        self._on_transcript_saved(result.transcript_file)
                    except Exception as exc:
                        logger.error(f"Transcript ingestion callback failed: {exc}")
                if self._output_format == "timestamped-txt":
                    out = export_timestamped_txt(result)
                    if out:
                        logger.info(f"Timestamped text: {out}")
                elif self._output_format == "srt":
                    out = export_srt(result)
                    if out:
                        logger.info(f"SRT: {out}")
            else:
                logger.error(f"Transcription failed: {result.error}")

            if self.on_transcription:
                self.on_transcription(result)

        except Exception as e:
            logger.error(f"Error transcribing {path}: {e}")
    
    def start(self) -> None:
        """Start watching for new files."""
        if self._running:
            return
        
        class Handler(FileSystemEventHandler):
            def __init__(handler_self, watcher):
                handler_self.watcher = watcher
            
            def on_created(handler_self, event):
                if isinstance(event, FileCreatedEvent) and not event.is_directory:
                    path = Path(event.src_path)
                    handler_self.watcher._handle_new_file(path)
        
        self._observer = Observer()
        self._observer.schedule(
            Handler(self),
            str(self.watch_dir),
            recursive=True,
        )
        self._observer.start()
        self._running = True
        logger.info(f"Started watching: {self.watch_dir}")
    
    def stop(self) -> None:
        """Stop watching for files."""
        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None
        self._running = False
        logger.info("Stopped watching")
    
    def run_forever(self) -> None:
        """Run the watcher until interrupted."""
        self.start()
        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()


def transcribe_file(
    audio_path: Path,
    grpc_host: str = None,
    grpc_port: int = None,
    language_code: str = "en-US",
    save: bool = True,
    preprocess: AudioPreprocess = AudioPreprocess.NONE,
    segment_by_pauses: bool = False,
    min_silence_duration: float = 0.5,
    silence_threshold_dB: float = -30.0,
    min_speech_duration: float = 0.3,
    merge_gap_seconds: float = 0.5,
    output_format: str = "json",
    periodic_timestamp_interval_sec: float = 0,
    diarization_enabled: bool = False,
    diarization_mode: str = "role-heuristic",
    stitch_across_files: bool = False,
    stitch_max_gap_seconds: float = 2.0,
    stitch_min_text_overlap_chars: int = 12,
) -> TranscriptionResult:
    """Convenience function to transcribe a single file.

    Args:
        audio_path: Path to the audio file
        grpc_host: Whisper gRPC host (default from env WHISPER_GRPC_HOST)
        grpc_port: Whisper gRPC port (default from env WHISPER_GRPC_PORT)
        language_code: Language code for transcription
        save: Whether to save the transcript to a JSON file
        preprocess: Audio preprocessing method
        segment_by_pauses: Segment by silence and timestamp each segment
        min_silence_duration: Min silence duration (s) for pause detection
        silence_threshold_dB: dB level for silence detection
        min_speech_duration: Min speech interval length (s)
        merge_gap_seconds: Merge speech intervals separated by shorter silence
        output_format: json, timestamped-txt, or srt (additional file when not json)
        periodic_timestamp_interval_sec: Insert markers every N seconds in timestamped-txt (0=off)

    Returns:
        TranscriptionResult object
    """
    # Get defaults from environment
    if grpc_host is None:
        grpc_host = os.environ.get("WHISPER_GRPC_HOST", "localhost")
    if grpc_port is None:
        grpc_port = int(os.environ.get("WHISPER_GRPC_PORT", "50051"))

    client = WhisperClient(
        grpc_host=grpc_host,
        grpc_port=grpc_port,
        language_code=language_code,
    )

    result = client.convert_and_transcribe(
        audio_path,
        preprocess=preprocess,
        segment_by_pauses=segment_by_pauses,
        min_silence_duration=min_silence_duration,
        silence_threshold_dB=silence_threshold_dB,
        min_speech_duration=min_speech_duration,
        merge_gap_seconds=merge_gap_seconds,
        diarization_enabled=diarization_enabled,
        diarization_mode=diarization_mode,
    )

    if save and result.success:
        save_transcript(result)
        if result.transcript_file and stitch_across_files:
            stitched = stitch_transcript_boundary_with_previous(
                result.transcript_file,
                max_gap_seconds=stitch_max_gap_seconds,
                min_text_overlap_chars=stitch_min_text_overlap_chars,
            )
            if stitched:
                refresh_result_from_saved_transcript(result)
        if output_format == "timestamped-txt":
            export_timestamped_txt(
                result,
                periodic_interval_sec=periodic_timestamp_interval_sec or 0,
            )
        elif output_format == "srt":
            export_srt(result)

    return result


def watch_and_transcribe(
    watch_dir: Path,
    grpc_host: str = None,
    grpc_port: int = None,
    language_code: str = "en-US",
    preprocess: AudioPreprocess = AudioPreprocess.NONE,
    segment_by_pauses: bool = False,
    min_silence_duration: float = 0.5,
    silence_threshold_dB: float = -30.0,
    min_speech_duration: float = 0.3,
    merge_gap_seconds: float = 0.5,
    output_format: str = "json",
    on_transcript_saved: Optional[Callable[[Path], None]] = None,
    diarization_enabled: bool = False,
    diarization_mode: str = "role-heuristic",
    stitch_across_files: bool = False,
    stitch_max_gap_seconds: float = 2.0,
    stitch_min_text_overlap_chars: int = 12,
) -> None:
    """Watch a directory and transcribe new audio files.

    Args:
        watch_dir: Directory to watch
        grpc_host: Whisper gRPC host (default from env WHISPER_GRPC_HOST)
        grpc_port: Whisper gRPC port (default from env WHISPER_GRPC_PORT)
        language_code: Language code for transcription
        preprocess: Audio preprocessing method to apply before ASR
        segment_by_pauses: If True, segment by silence and timestamp each segment
        min_silence_duration: Min silence duration (s) for pause detection
        silence_threshold_dB: dB level for silence detection
        min_speech_duration: Min speech interval length (s)
        merge_gap_seconds: Merge speech intervals separated by shorter silence
        output_format: json, timestamped-txt, or srt
    """
    # Get defaults from environment
    if grpc_host is None:
        grpc_host = os.environ.get("WHISPER_GRPC_HOST", "localhost")
    if grpc_port is None:
        grpc_port = int(os.environ.get("WHISPER_GRPC_PORT", "50051"))
    
    client = WhisperClient(
        grpc_host=grpc_host,
        grpc_port=grpc_port,
        language_code=language_code,
    )
    
    # Wait for Whisper service to be available
    logger.info(f"Waiting for Whisper service at {client.server_uri}...")
    while not client.check_connection():
        time.sleep(5)
    logger.info("Whisper service is ready")
    
    watcher = TranscriptionWatcher(
        watch_dir=watch_dir,
        client=client,
        preprocess=preprocess,
        segment_by_pauses=segment_by_pauses,
        min_silence_duration=min_silence_duration,
        silence_threshold_dB=silence_threshold_dB,
        min_speech_duration=min_speech_duration,
        merge_gap_seconds=merge_gap_seconds,
        output_format=output_format,
        on_transcript_saved=on_transcript_saved,
        diarization_enabled=diarization_enabled,
        diarization_mode=diarization_mode,
        stitch_across_files=stitch_across_files,
        stitch_max_gap_seconds=stitch_max_gap_seconds,
        stitch_min_text_overlap_chars=stitch_min_text_overlap_chars,
    )

    watcher.run_forever()


def find_untranscribed_files(
    directory: Path,
    extensions: list[str] = None,
) -> list[Path]:
    """Find audio files that don't have corresponding transcript JSON files.

    Args:
        directory: Directory to search recursively
        extensions: File extensions to look for (default: ['.mp3'])

    Returns:
        List of audio file paths without transcripts
    """
    if extensions is None:
        extensions = ['.mp3']

    directory = Path(directory)
    untranscribed = []

    for ext in extensions:
        for audio_file in directory.rglob(f"*{ext}"):
            transcript_file = audio_file.with_suffix('.json')
            if not transcript_file.exists():
                untranscribed.append(audio_file)

    return sorted(untranscribed)


def find_audio_files(
    directory: Path,
    extensions: list[str] = None,
) -> list[Path]:
    """Find all audio files in a directory (optionally with transcripts).

    Args:
        directory: Directory to search recursively
        extensions: File extensions to look for (default: ['.mp3'])

    Returns:
        List of audio file paths
    """
    if extensions is None:
        extensions = ['.mp3']
    directory = Path(directory)
    files = []
    for ext in extensions:
        files.extend(directory.rglob(f"*{ext}"))
    return sorted(set(files))


def transcribe_all(
    directory: Path,
    grpc_host: str = None,
    grpc_port: int = None,
    language_code: str = "en-US",
    on_progress: callable = None,
    force: bool = False,
    preprocess: AudioPreprocess = AudioPreprocess.NONE,
    segment_by_pauses: bool = False,
    min_silence_duration: float = 0.5,
    silence_threshold_dB: float = -30.0,
    min_speech_duration: float = 0.3,
    merge_gap_seconds: float = 0.5,
    output_format: str = "json",
    diarization_enabled: bool = False,
    diarization_mode: str = "role-heuristic",
    stitch_across_files: bool = False,
    stitch_max_gap_seconds: float = 2.0,
    stitch_min_text_overlap_chars: int = 12,
) -> list[TranscriptionResult]:
    """Transcribe audio files in a directory.

    By default only transcribes files that don't have a transcript yet.
    Use force=True to re-transcribe all audio files (overwrites existing JSON).

    Args:
        directory: Directory to search recursively
        grpc_host: Whisper gRPC host (default from env WHISPER_GRPC_HOST)
        grpc_port: Whisper gRPC port (default from env WHISPER_GRPC_PORT)
        language_code: Language code for transcription
        on_progress: Optional callback(current, total, result) for progress updates
        force: If True, transcribe all audio files (including those with existing transcripts)
        preprocess: Audio preprocessing method to apply before ASR
        segment_by_pauses: If True, segment by silence and timestamp each segment
        min_silence_duration: Min silence duration (s) for pause detection
        silence_threshold_dB: dB level for silence detection
        min_speech_duration: Min speech interval length (s)
        merge_gap_seconds: Merge speech intervals separated by shorter silence
        output_format: json, timestamped-txt, or srt (writes extra file when not json)

    Returns:
        List of TranscriptionResult objects
    """
    # Get defaults from environment
    if grpc_host is None:
        grpc_host = os.environ.get("WHISPER_GRPC_HOST", "localhost")
    if grpc_port is None:
        grpc_port = int(os.environ.get("WHISPER_GRPC_PORT", "50051"))

    if force:
        files = find_audio_files(directory)
        logger.info(f"Force mode: found {len(files)} audio files to transcribe")
    else:
        files = find_untranscribed_files(directory)

    if not files:
        logger.info("No files to transcribe")
        return []

    logger.info(f"Found {len(files)} files to transcribe")

    # Create client
    client = WhisperClient(
        grpc_host=grpc_host,
        grpc_port=grpc_port,
        language_code=language_code,
    )

    # Check connection
    if not client.check_connection():
        logger.error(f"Cannot connect to Whisper service at {client.server_uri}")
        return []

    results = []

    for i, audio_file in enumerate(files):
        logger.info(f"[{i+1}/{len(files)}] Transcribing: {audio_file}")

        try:
            result = client.convert_and_transcribe(
                audio_file,
                preprocess=preprocess,
                segment_by_pauses=segment_by_pauses,
                min_silence_duration=min_silence_duration,
                silence_threshold_dB=silence_threshold_dB,
                min_speech_duration=min_speech_duration,
                merge_gap_seconds=merge_gap_seconds,
                diarization_enabled=diarization_enabled,
                diarization_mode=diarization_mode,
            )

            if result.success:
                save_transcript(result)
                if result.transcript_file and stitch_across_files:
                    stitched = stitch_transcript_boundary_with_previous(
                        result.transcript_file,
                        max_gap_seconds=stitch_max_gap_seconds,
                        min_text_overlap_chars=stitch_min_text_overlap_chars,
                    )
                    if stitched:
                        logger.info(f"  Boundary stitched: {result.transcript_file.name}")
                        refresh_result_from_saved_transcript(result)
                logger.info(f"  Saved: {result.transcript_file}")
                if output_format == "timestamped-txt":
                    out = export_timestamped_txt(result)
                    if out:
                        logger.info(f"  Timestamped text: {out}")
                elif output_format == "srt":
                    out = export_srt(result)
                    if out:
                        logger.info(f"  SRT: {out}")
            else:
                logger.error(f"  Failed: {result.error}")

            results.append(result)

            if on_progress:
                on_progress(i + 1, len(files), result)

        except Exception as e:
            logger.error(f"  Error: {e}")
            results.append(TranscriptionResult(
                success=False,
                audio_file=audio_file,
                error=str(e),
            ))
    
    return results


def compare_preprocessing(
    audio_path: Path,
    grpc_host: str = None,
    grpc_port: int = None,
    language_code: str = "en-US",
    output_dir: Optional[Path] = None,
) -> dict[str, TranscriptionResult]:
    """Compare transcription results using different preprocessing methods.
    
    Transcribes the same audio file with no preprocessing, ffmpeg filters,
    and sox noise reduction, then saves comparison results.
    
    Args:
        audio_path: Path to the audio file to test
        grpc_host: Whisper gRPC host (default from env WHISPER_GRPC_HOST)
        grpc_port: Whisper gRPC port (default from env WHISPER_GRPC_PORT)
        language_code: Language code for transcription
        output_dir: Directory to save comparison results (default: same as audio)
        
    Returns:
        Dict mapping preprocessing method name to TranscriptionResult
    """
    import shutil
    
    # Get defaults from environment
    if grpc_host is None:
        grpc_host = os.environ.get("WHISPER_GRPC_HOST", "localhost")
    if grpc_port is None:
        grpc_port = int(os.environ.get("WHISPER_GRPC_PORT", "50051"))
    
    audio_path = Path(audio_path)
    if output_dir is None:
        output_dir = audio_path.parent
    else:
        output_dir = Path(output_dir)
    
    # Check for sox availability
    sox_available = shutil.which("sox") is not None
    if not sox_available:
        logger.warning("sox not found in PATH - sox preprocessing will be skipped")
    
    # Create client
    client = WhisperClient(
        grpc_host=grpc_host,
        grpc_port=grpc_port,
        language_code=language_code,
    )
    
    # Check connection
    if not client.check_connection():
        logger.error(f"Cannot connect to Whisper service at {client.server_uri}")
        return {}
    
    results = {}
    methods = [
        ("none", AudioPreprocess.NONE),
        ("ffmpeg", AudioPreprocess.FFMPEG),
        ("ffmpeg_vad", AudioPreprocess.FFMPEG_VAD),
    ]
    if sox_available:
        methods.append(("sox", AudioPreprocess.SOX))
    
    base_name = audio_path.stem
    
    for method_name, preprocess in methods:
        logger.info(f"\n{'='*60}")
        logger.info(f"Transcribing with preprocessing: {method_name.upper()}")
        logger.info(f"{'='*60}")
        
        start_time = time.time()
        result = client.convert_and_transcribe(audio_path, preprocess=preprocess)
        elapsed = time.time() - start_time
        
        results[method_name] = result
        
        if result.success:
            # Save with method suffix
            output_path = output_dir / f"{base_name}_transcript_{method_name}.json"
            result.transcript_file = output_path
            
            transcript_data = {
                "audio_file": audio_path.name,
                "preprocessing": method_name,
                "language": result.language,
                "text": result.text,
                "segments": result.segments,
                "transcribed_at": result.transcribed_at.isoformat() if result.transcribed_at else None,
                "processing_time_seconds": elapsed,
            }
            
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(transcript_data, f, indent=2, ensure_ascii=False)
            
            logger.info(f"  Time: {elapsed:.1f}s")
            logger.info(f"  Saved: {output_path}")
            logger.info(f"  Text preview: {result.text[:200]}..." if len(result.text) > 200 else f"  Text: {result.text}")
        else:
            logger.error(f"  Failed: {result.error}")
    
    # Print comparison summary
    logger.info(f"\n{'='*60}")
    logger.info("COMPARISON SUMMARY")
    logger.info(f"{'='*60}")
    
    for method_name, result in results.items():
        if result.success:
            word_count = len(result.text.split())
            logger.info(f"\n[{method_name.upper()}] - {word_count} words")
            logger.info(f"  {result.text[:300]}{'...' if len(result.text) > 300 else ''}")
        else:
            logger.info(f"\n[{method_name.upper()}] - FAILED: {result.error}")
    
    return results
