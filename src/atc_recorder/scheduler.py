"""Recording scheduler for continuous operation."""

import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .config import Config
from .stream import StreamRecorder, RecordingResult


class RecordingScheduler:
    """Schedule and manage multiple concurrent recordings."""
    
    def __init__(self, config: Config):
        """Initialize the scheduler.
        
        Args:
            config: Configuration object
        """
        self.config = config
        self.recorders: dict[str, StreamRecorder] = {}
        self.threads: dict[str, threading.Thread] = {}
        self._stop_event = threading.Event()
        self._results: list[RecordingResult] = []
        self._results_lock = threading.Lock()
    
    def start(
        self,
        feed_ids: list[str],
        on_segment_complete: Optional[Callable[[str, RecordingResult], None]] = None,
    ) -> None:
        """Start recording multiple feeds in parallel.
        
        Args:
            feed_ids: List of feed identifiers to record
            on_segment_complete: Optional callback(feed_id, result) for each segment
        """
        self._stop_event.clear()
        
        for feed_id in feed_ids:
            recorder = StreamRecorder(self.config)
            self.recorders[feed_id] = recorder
            
            thread = threading.Thread(
                target=self._recording_loop,
                args=(feed_id, recorder, on_segment_complete),
                name=f"recorder-{feed_id}",
                daemon=True,
            )
            self.threads[feed_id] = thread
            thread.start()
    
    def _recording_loop(
        self,
        feed_id: str,
        recorder: StreamRecorder,
        on_segment_complete: Optional[Callable[[str, RecordingResult], None]],
    ) -> None:
        """Recording loop for a single feed.
        
        Args:
            feed_id: Feed identifier
            recorder: StreamRecorder instance
            on_segment_complete: Optional callback for segment completion
        """
        while not self._stop_event.is_set():
            # Record a single segment
            results = recorder.record(
                feed_id=feed_id,
                duration_seconds=self.config.segment_duration,
                output_dir=self.config.output_dir,
            )
            
            with self._results_lock:
                self._results.extend(results)
            
            for result in results:
                if on_segment_complete:
                    on_segment_complete(feed_id, result)
            
            # Short delay before next segment
            if not self._stop_event.is_set():
                time.sleep(1)
    
    def stop(self, timeout: float = 30.0) -> None:
        """Stop all recordings.
        
        Args:
            timeout: Maximum time to wait for threads to stop
        """
        self._stop_event.set()
        
        # Signal all recorders to stop
        for recorder in self.recorders.values():
            recorder.stop()
        
        # Wait for threads to finish
        for thread in self.threads.values():
            thread.join(timeout=timeout)
        
        self.recorders.clear()
        self.threads.clear()
    
    def is_running(self) -> bool:
        """Check if any recordings are running."""
        return any(t.is_alive() for t in self.threads.values())
    
    def get_results(self) -> list[RecordingResult]:
        """Get all recording results.
        
        Returns:
            List of RecordingResult objects
        """
        with self._results_lock:
            return list(self._results)
    
    def get_active_feeds(self) -> list[str]:
        """Get list of currently recording feed IDs.
        
        Returns:
            List of feed IDs that are actively recording
        """
        return [
            feed_id
            for feed_id, thread in self.threads.items()
            if thread.is_alive()
        ]


def run_scheduler(
    feed_ids: list[str],
    config: Optional[Config] = None,
    on_segment_complete: Optional[Callable[[str, RecordingResult], None]] = None,
) -> RecordingScheduler:
    """Convenience function to start the scheduler.
    
    Args:
        feed_ids: List of feed identifiers to record
        config: Optional configuration
        on_segment_complete: Optional callback for segment completion
        
    Returns:
        Running RecordingScheduler instance
    """
    cfg = config or Config()
    scheduler = RecordingScheduler(cfg)
    scheduler.start(feed_ids, on_segment_complete)
    return scheduler
