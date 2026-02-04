"""Transcription functionality using NVIDIA Whisper ASR via Riva."""

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Callable

from .config import Config
from .logging import get_logger

logger = get_logger(__name__)

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
            
            # Configure recognition
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
                    
                    # Extract word-level timing if available
                    words = []
                    for word_info in alt.words:
                        words.append({
                            "word": word_info.word,
                            "start_time": word_info.start_time,
                            "end_time": word_info.end_time,
                            "confidence": word_info.confidence,
                        })
                    
                    if words:
                        segments.append({
                            "text": alt.transcript,
                            "words": words,
                            "confidence": alt.confidence,
                        })
            
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
    
    def convert_and_transcribe(self, audio_path: Path) -> TranscriptionResult:
        """Convert audio to WAV and transcribe.
        
        Handles MP3 and other formats by converting to mono 16-bit WAV.
        
        Args:
            audio_path: Path to the audio file (MP3, WAV, etc.)
            
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
        
        # Check if already WAV
        if audio_path.suffix.lower() == '.wav':
            return self.transcribe_file(audio_path)
        
        # Convert to WAV using ffmpeg
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp:
            wav_path = Path(tmp.name)
        
        try:
            # Convert to mono 16-bit 16kHz WAV
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
            
            # Transcribe the WAV file
            transcription = self.transcribe_file(wav_path)
            # Update the audio_file to the original
            transcription.audio_file = audio_path
            return transcription
            
        finally:
            # Clean up temp file
            if wav_path.exists():
                wav_path.unlink()


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


class TranscriptionWatcher:
    """Watch for new audio files and transcribe them automatically."""
    
    def __init__(
        self,
        watch_dir: Path,
        client: WhisperClient,
        on_transcription: Optional[Callable[[TranscriptionResult], None]] = None,
        file_patterns: list[str] = None,
    ):
        """Initialize the watcher.
        
        Args:
            watch_dir: Directory to watch for new audio files
            client: WhisperClient for transcription
            on_transcription: Optional callback when transcription completes
            file_patterns: File extensions to watch (default: ['.mp3'])
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
            result = self.client.convert_and_transcribe(path)
            
            if result.success:
                save_transcript(result)
                logger.info(f"Transcription saved: {result.transcript_file}")
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
) -> TranscriptionResult:
    """Convenience function to transcribe a single file.
    
    Args:
        audio_path: Path to the audio file
        grpc_host: Whisper gRPC host (default from env WHISPER_GRPC_HOST)
        grpc_port: Whisper gRPC port (default from env WHISPER_GRPC_PORT)
        language_code: Language code for transcription
        save: Whether to save the transcript to a JSON file
        
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
    
    result = client.convert_and_transcribe(audio_path)
    
    if save and result.success:
        save_transcript(result)
    
    return result


def watch_and_transcribe(
    watch_dir: Path,
    grpc_host: str = None,
    grpc_port: int = None,
    language_code: str = "en-US",
) -> None:
    """Watch a directory and transcribe new audio files.
    
    Args:
        watch_dir: Directory to watch
        grpc_host: Whisper gRPC host (default from env WHISPER_GRPC_HOST)
        grpc_port: Whisper gRPC port (default from env WHISPER_GRPC_PORT)
        language_code: Language code for transcription
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


def transcribe_all(
    directory: Path,
    grpc_host: str = None,
    grpc_port: int = None,
    language_code: str = "en-US",
    on_progress: callable = None,
) -> list[TranscriptionResult]:
    """Transcribe all audio files in a directory that don't have transcripts.
    
    Args:
        directory: Directory to search recursively
        grpc_host: Whisper gRPC host (default from env WHISPER_GRPC_HOST)
        grpc_port: Whisper gRPC port (default from env WHISPER_GRPC_PORT)
        language_code: Language code for transcription
        on_progress: Optional callback(current, total, result) for progress updates
        
    Returns:
        List of TranscriptionResult objects
    """
    # Get defaults from environment
    if grpc_host is None:
        grpc_host = os.environ.get("WHISPER_GRPC_HOST", "localhost")
    if grpc_port is None:
        grpc_port = int(os.environ.get("WHISPER_GRPC_PORT", "50051"))
    
    # Find files to transcribe
    files = find_untranscribed_files(directory)
    
    if not files:
        logger.info("No untranscribed files found")
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
            result = client.convert_and_transcribe(audio_file)
            
            if result.success:
                save_transcript(result)
                logger.info(f"  Saved: {result.transcript_file}")
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
