"""Live stream recording functionality."""

import json
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .config import Config
from .feeds import FeedDiscovery, Feed, FeedDiscoveryError
from .utils import (
    check_ffmpeg,
    ensure_dir,
    format_duration,
    get_date_string,
    get_timestamp_string,
    parse_duration,
)


@dataclass
class RecordingResult:
    """Result of a recording operation."""
    
    success: bool
    feed_id: str
    output_file: Optional[Path] = None
    duration_seconds: int = 0
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    error: Optional[str] = None
    metadata: dict = field(default_factory=dict)


class StreamRecorder:
    """Records live streams from LiveATC.net using ffmpeg."""
    
    def __init__(self, config: Optional[Config] = None):
        """Initialize the stream recorder.
        
        Args:
            config: Optional configuration object
        """
        self.config = config or Config()
        self.feed_discovery = FeedDiscovery(config)
        self._stop_requested = False
    
    def record(
        self,
        feed_id: str,
        duration_seconds: int,
        output_dir: Optional[Path] = None,
        on_segment_complete: Optional[Callable[[RecordingResult], None]] = None,
    ) -> list[RecordingResult]:
        """Record a live stream for the specified duration.
        
        The recording is split into segments based on config.segment_duration.
        
        Args:
            feed_id: The feed identifier (e.g., 'kdca1_gnd')
            duration_seconds: Total duration to record in seconds
            output_dir: Optional output directory (defaults to config.output_dir)
            on_segment_complete: Optional callback for when each segment completes
            
        Returns:
            List of RecordingResult objects for each segment
        """
        if not check_ffmpeg():
            return [RecordingResult(
                success=False,
                feed_id=feed_id,
                error="ffmpeg is not installed or not found in PATH",
            )]
        
        # Get the stream URL
        try:
            stream_url = self.feed_discovery.get_stream_url(feed_id)
            if not stream_url:
                return [RecordingResult(
                    success=False,
                    feed_id=feed_id,
                    error=f"Could not find stream URL for feed {feed_id}",
                )]
        except FeedDiscoveryError as e:
            return [RecordingResult(
                success=False,
                feed_id=feed_id,
                error=str(e),
            )]
        
        # Get feed info for metadata
        feed_info = self._get_feed_info(feed_id)
        
        # Setup output directory
        if output_dir is None:
            output_dir = self.config.output_dir
        
        output_dir = Path(output_dir)
        
        results = []
        segment_duration = self.config.segment_duration
        remaining_seconds = duration_seconds
        
        self._stop_requested = False
        
        while remaining_seconds > 0 and not self._stop_requested:
            # Calculate this segment's duration
            current_segment_duration = min(segment_duration, remaining_seconds)
            
            # Record the segment
            result = self._record_segment(
                feed_id=feed_id,
                stream_url=stream_url,
                duration_seconds=current_segment_duration,
                output_dir=output_dir,
                feed_info=feed_info,
            )
            
            results.append(result)
            
            if on_segment_complete:
                on_segment_complete(result)
            
            if not result.success:
                # If recording failed, try to reconnect
                retry_count = 0
                while retry_count < self.config.recording.max_retries and not self._stop_requested:
                    retry_count += 1
                    time.sleep(self.config.recording.reconnect_delay)
                    
                    # Try to get stream URL again
                    try:
                        stream_url = self.feed_discovery.get_stream_url(feed_id)
                        if stream_url:
                            break
                    except FeedDiscoveryError:
                        continue
                
                if retry_count >= self.config.recording.max_retries:
                    break
            
            remaining_seconds -= current_segment_duration
        
        return results
    
    def _record_segment(
        self,
        feed_id: str,
        stream_url: str,
        duration_seconds: int,
        output_dir: Path,
        feed_info: Optional[dict] = None,
    ) -> RecordingResult:
        """Record a single segment.
        
        Args:
            feed_id: Feed identifier
            stream_url: URL of the stream
            duration_seconds: Duration to record
            output_dir: Output directory
            feed_info: Optional feed metadata
            
        Returns:
            RecordingResult object
        """
        start_time = datetime.now(timezone.utc)
        date_str = get_date_string(start_time)
        time_str = get_timestamp_string(start_time)
        
        # Create output directory structure: output_dir/feed_id/date/
        segment_dir = ensure_dir(output_dir / feed_id / date_str)
        
        # Generate filename
        filename = f"{feed_id}_{date_str}_{time_str}.mp3"
        output_file = segment_dir / filename
        
        # Build ffmpeg command
        cmd = [
            "ffmpeg",
            "-y",  # Overwrite output file
            "-i", stream_url,
            "-t", str(duration_seconds),
            "-c", "copy",  # Copy codec (no transcoding)
            "-f", "mp3",
            str(output_file),
        ]
        
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=duration_seconds + 60,  # Add buffer for startup
            )
            
            end_time = datetime.now(timezone.utc)
            actual_duration = int((end_time - start_time).total_seconds())
            
            if result.returncode == 0 and output_file.exists():
                # Save metadata
                metadata = self._create_metadata(
                    feed_id=feed_id,
                    feed_info=feed_info,
                    start_time=start_time,
                    duration_seconds=actual_duration,
                    filename=filename,
                    source="live",
                )
                self._save_metadata(segment_dir, metadata)
                
                return RecordingResult(
                    success=True,
                    feed_id=feed_id,
                    output_file=output_file,
                    duration_seconds=actual_duration,
                    start_time=start_time,
                    end_time=end_time,
                    metadata=metadata,
                )
            else:
                return RecordingResult(
                    success=False,
                    feed_id=feed_id,
                    start_time=start_time,
                    end_time=end_time,
                    error=f"ffmpeg failed: {result.stderr[:500] if result.stderr else 'Unknown error'}",
                )
                
        except subprocess.TimeoutExpired:
            return RecordingResult(
                success=False,
                feed_id=feed_id,
                start_time=start_time,
                error="Recording timed out",
            )
        except Exception as e:
            return RecordingResult(
                success=False,
                feed_id=feed_id,
                start_time=start_time,
                error=str(e),
            )
    
    def _get_feed_info(self, feed_id: str) -> Optional[dict]:
        """Get feed information for metadata.
        
        Args:
            feed_id: Feed identifier
            
        Returns:
            Dict with feed info or None
        """
        try:
            feed = self.feed_discovery.get_feed_by_id(feed_id)
            if feed:
                return {
                    "title": feed.title,
                    "frequency": feed.primary_frequency,
                    "frequencies": feed.frequencies,
                    "status": feed.status,
                }
        except FeedDiscoveryError:
            pass
        return None
    
    def _create_metadata(
        self,
        feed_id: str,
        feed_info: Optional[dict],
        start_time: datetime,
        duration_seconds: int,
        filename: str,
        source: str,
    ) -> dict:
        """Create metadata for a recording.
        
        Args:
            feed_id: Feed identifier
            feed_info: Feed information dict
            start_time: Recording start time
            duration_seconds: Duration of recording
            filename: Output filename
            source: Source type ('live' or 'archive')
            
        Returns:
            Metadata dictionary
        """
        metadata = {
            "feed_id": feed_id,
            "file": filename,
            "start_time": start_time.isoformat(),
            "duration_seconds": duration_seconds,
            "source": source,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        
        if feed_info:
            metadata["description"] = feed_info.get("title", "")
            metadata["frequency"] = feed_info.get("frequency", "")
            metadata["frequencies"] = feed_info.get("frequencies", [])
        
        return metadata
    
    def _save_metadata(self, directory: Path, metadata: dict) -> None:
        """Save or update metadata file in directory.
        
        Args:
            directory: Directory containing recordings
            metadata: Metadata to save
        """
        metadata_file = directory / "metadata.json"
        
        # Load existing metadata if present
        existing = []
        if metadata_file.exists():
            try:
                with open(metadata_file, 'r') as f:
                    existing = json.load(f)
                    if not isinstance(existing, list):
                        existing = [existing]
            except (json.JSONDecodeError, IOError):
                existing = []
        
        # Append new metadata
        existing.append(metadata)
        
        # Save updated metadata
        with open(metadata_file, 'w') as f:
            json.dump(existing, f, indent=2)
    
    def stop(self) -> None:
        """Request the recorder to stop after the current segment."""
        self._stop_requested = True


def record_feed(
    feed_id: str,
    duration: str = "30m",
    output_dir: Optional[Path] = None,
    config: Optional[Config] = None,
) -> list[RecordingResult]:
    """Convenience function to record a feed.
    
    Args:
        feed_id: Feed identifier
        duration: Duration string (e.g., '30m', '2h')
        output_dir: Optional output directory
        config: Optional configuration
        
    Returns:
        List of RecordingResult objects
    """
    duration_seconds = parse_duration(duration)
    recorder = StreamRecorder(config)
    return recorder.record(feed_id, duration_seconds, output_dir)
