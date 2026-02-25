"""Cross-feed flight tracking — reconstruct a flight's journey across ATC frequencies."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .logging import get_logger

logger = get_logger(__name__)

# Map known feed IDs to their frequencies for handoff detection.
# Extends with data from DCA_FEEDS comments in config.py.
FEED_FREQUENCY_MAP: dict[str, str] = {
    "kdca1_gnd": "121.700",
    "kdca2_twr": "119.100",
    "kdca1_twr": "119.100",
    "kdca1_heli": "134.350",
    "kdca1_dep": "118.950",
    "kdca1_app_final": "124.700",
    "kdca1_app_ensue": "124.200",
    "kdca1_app_ojaay": "119.850",
    "kmrb1_app_luray": "118.675",
    "kdca1_dep_121050": "121.050",
    "kdca1_dep_e": "125.650",
    "kdca1_sfra_s": "125.125",
}

# Reverse map: frequency -> list of feed_ids
_FREQ_TO_FEEDS: dict[str, list[str]] = {}
for _fid, _freq in FEED_FREQUENCY_MAP.items():
    _FREQ_TO_FEEDS.setdefault(_freq, []).append(_fid)

# Patterns to detect handoff instructions
_HANDOFF_RE = re.compile(
    r"\bcontact\s+([\w\s]+?)\s+(?:on\s+)?(\d{2,3})\s*(?:point|decimal|\.)?\s*(\d{1,3})\b",
    re.IGNORECASE,
)

# Position name keywords for feed matching
_POSITION_KEYWORDS: dict[str, list[str]] = {
    "ground": ["gnd", "ground"],
    "tower": ["twr", "tower"],
    "departure": ["dep", "departure"],
    "approach": ["app", "approach"],
    "center": ["ctr", "center"],
}


@dataclass
class FlightLeg:
    """A segment of a flight's journey on a single ATC frequency."""

    feed_id: str
    frequency: str
    first_heard: datetime
    last_heard: datetime
    segments: list[dict] = field(default_factory=list)
    handoff_to: Optional[str] = None
    handoff_frequency: Optional[str] = None


@dataclass
class FlightTrack:
    """Complete reconstruction of a flight's journey across ATC frequencies."""

    callsign: str
    normalized: str
    legs: list[FlightLeg] = field(default_factory=list)
    total_duration: timedelta = field(default_factory=timedelta)


def _detect_handoff(text: str) -> Optional[tuple[str, str]]:
    """Detect a frequency handoff instruction in transcript text.

    Returns (position_name, frequency) or None.
    """
    m = _HANDOFF_RE.search(text)
    if not m:
        return None
    position = m.group(1).strip()
    freq = f"{m.group(2)}.{m.group(3)}"
    return (position, freq)


def _frequency_to_feed_ids(frequency: str) -> list[str]:
    """Resolve a frequency to potential feed IDs."""
    return _FREQ_TO_FEEDS.get(frequency, [])


class FlightTracker:
    """Reconstruct flight paths from entity mentions across feeds."""

    def __init__(
        self,
        metadata_store: Any,
    ):
        self.metadata_store = metadata_store

    def track_flight(self, callsign: str) -> Optional[FlightTrack]:
        """Build a FlightTrack for a given callsign."""
        timeline = self.metadata_store.get_flight_timeline(callsign)
        if not timeline:
            return None

        track = FlightTrack(callsign=callsign, normalized=callsign)
        legs = self._build_legs(timeline)
        self._detect_handoffs(legs)
        track.legs = legs

        if legs:
            first = legs[0].first_heard
            last = legs[-1].last_heard
            track.total_duration = last - first

        return track

    def _build_legs(self, timeline: list[dict]) -> list[FlightLeg]:
        """Group timeline mentions into legs by feed, allowing re-entry on same feed."""
        if not timeline:
            return []

        legs: list[FlightLeg] = []
        current_feed = None
        current_leg: Optional[FlightLeg] = None
        gap_threshold = timedelta(minutes=10)

        for row in timeline:
            feed_id = row.get("feed_id", "unknown")
            ts_str = row.get("timestamp_utc") or row.get("start_time_utc", "")
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).astimezone(timezone.utc)
            except (ValueError, AttributeError):
                continue

            freq = FEED_FREQUENCY_MAP.get(feed_id, "")

            if feed_id != current_feed or (
                current_leg and (ts - current_leg.last_heard) > gap_threshold
            ):
                if current_leg:
                    legs.append(current_leg)
                current_leg = FlightLeg(
                    feed_id=feed_id,
                    frequency=freq,
                    first_heard=ts,
                    last_heard=ts,
                    segments=[row],
                )
                current_feed = feed_id
            else:
                assert current_leg is not None
                current_leg.last_heard = ts
                current_leg.segments.append(row)

        if current_leg:
            legs.append(current_leg)

        return legs

    def _detect_handoffs(self, legs: list[FlightLeg]) -> None:
        """Scan each leg's segments for handoff instructions and link to next leg."""
        for i, leg in enumerate(legs):
            for seg in reversed(leg.segments):
                text = seg.get("text", "")
                handoff = _detect_handoff(text)
                if handoff:
                    position, freq = handoff
                    leg.handoff_frequency = freq
                    feed_candidates = _frequency_to_feed_ids(freq)
                    if i + 1 < len(legs) and legs[i + 1].feed_id in feed_candidates:
                        leg.handoff_to = legs[i + 1].feed_id
                    elif feed_candidates:
                        leg.handoff_to = feed_candidates[0]
                    break

    def get_recent_flights(self, limit: int = 50) -> list[dict]:
        """Return recently seen flights with summary info."""
        return self.metadata_store.get_recent_flights(limit=limit)

    def get_active_on_feed(
        self,
        feed_id: str,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """Get callsigns active on a specific feed in a time range."""
        return self.metadata_store.get_active_callsigns(
            feed_id=feed_id, start_time=start_time, end_time=end_time, limit=limit
        )


def flight_track_to_dict(track: FlightTrack) -> dict:
    """Serialize a FlightTrack to a JSON-safe dict."""
    legs = []
    for leg in track.legs:
        legs.append(
            {
                "feed_id": leg.feed_id,
                "frequency": leg.frequency,
                "first_heard": leg.first_heard.isoformat(),
                "last_heard": leg.last_heard.isoformat(),
                "segment_count": len(leg.segments),
                "segments": [
                    {
                        "text": s.get("text", ""),
                        "audio_file": s.get("audio_file", ""),
                        "start_time_utc": s.get("start_time_utc", ""),
                        "end_time_utc": s.get("end_time_utc", ""),
                        "feed_id": s.get("feed_id", ""),
                    }
                    for s in leg.segments
                ],
                "handoff_to": leg.handoff_to,
                "handoff_frequency": leg.handoff_frequency,
            }
        )

    result: dict = {
        "callsign": track.callsign,
        "normalized": track.normalized,
        "legs": legs,
        "total_duration_seconds": track.total_duration.total_seconds(),
        "feed_count": len({leg.feed_id for leg in track.legs}),
    }

    return result
