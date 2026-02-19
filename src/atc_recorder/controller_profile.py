"""ATC controller/position profiling — per-feed analytics from transcript data."""

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .logging import get_logger

logger = get_logger(__name__)

# Standard ATC phrases to count
PHRASEOLOGY_PATTERNS: dict[str, re.Pattern] = {
    "cleared to land": re.compile(r"\bcleared\s+to\s+land\b", re.IGNORECASE),
    "cleared for takeoff": re.compile(r"\bcleared\s+for\s+take\s*off\b", re.IGNORECASE),
    "hold short": re.compile(r"\bhold\s+short\b", re.IGNORECASE),
    "line up and wait": re.compile(r"\bline\s+up\s+(?:and\s+)?wait\b", re.IGNORECASE),
    "contact": re.compile(r"\bcontact\s+\w+", re.IGNORECASE),
    "taxi to": re.compile(r"\btaxi\s+to\b", re.IGNORECASE),
    "cleared ILS": re.compile(r"\bcleared\s+(?:the\s+)?ILS\b", re.IGNORECASE),
    "cleared visual": re.compile(r"\bcleared\s+(?:the\s+)?visual\b", re.IGNORECASE),
    "go around": re.compile(r"\bgo\s+around\b", re.IGNORECASE),
    "maintain": re.compile(r"\bmaintain\s+\w+", re.IGNORECASE),
    "descend and maintain": re.compile(r"\bdescend\s+and\s+maintain\b", re.IGNORECASE),
    "climb and maintain": re.compile(r"\bclimb\s+and\s+maintain\b", re.IGNORECASE),
    "roger": re.compile(r"\broger\b", re.IGNORECASE),
    "wilco": re.compile(r"\bwilco\b", re.IGNORECASE),
    "say again": re.compile(r"\bsay\s+again\b", re.IGNORECASE),
    "squawk": re.compile(r"\bsquawk\b", re.IGNORECASE),
    "ident": re.compile(r"\bident\b", re.IGNORECASE),
    "turn left": re.compile(r"\bturn\s+left\b", re.IGNORECASE),
    "turn right": re.compile(r"\bturn\s+right\b", re.IGNORECASE),
    "fly heading": re.compile(r"\bfly\s+heading\b", re.IGNORECASE),
    "radar contact": re.compile(r"\bradar\s+contact\b", re.IGNORECASE),
    "traffic": re.compile(r"\btraffic\b", re.IGNORECASE),
    "wind": re.compile(r"\bwind\s+\d", re.IGNORECASE),
    "altimeter": re.compile(r"\baltimeter\b", re.IGNORECASE),
}


@dataclass
class PositionProfile:
    """Analytics profile for a single ATC position/feed over a time window."""

    feed_id: str
    time_window_start: Optional[datetime] = None
    time_window_end: Optional[datetime] = None
    total_segments: int = 0
    atc_segments: int = 0
    pilot_segments: int = 0
    unknown_segments: int = 0
    unique_callsigns: int = 0
    avg_segment_duration: float = 0.0
    total_talk_time: float = 0.0
    phrases: dict[str, int] = field(default_factory=dict)
    busiest_hours: list[tuple[int, int]] = field(default_factory=list)
    handoff_destinations: dict[str, int] = field(default_factory=dict)
    callsign_list: list[str] = field(default_factory=list)


def _parse_dt(val: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


class ControllerProfiler:
    """Compute per-position analytics from the metadata store."""

    def __init__(self, db_path: Path):
        self.db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def profile_feed(
        self,
        feed_id: str,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> PositionProfile:
        """Compute a PositionProfile for a feed in a time window."""
        profile = PositionProfile(feed_id=feed_id)

        where = ["feed_id = ?"]
        params: list[object] = [feed_id]
        if start_time:
            where.append("start_time_utc >= ?")
            params.append(start_time)
        if end_time:
            where.append("end_time_utc <= ?")
            params.append(end_time)
        where_sql = " AND ".join(where)

        with self._conn() as conn:
            # Fetch all segments for this feed/window
            rows = conn.execute(
                f"""
                SELECT doc_id, text, quality_flags, start_time_utc, end_time_utc
                FROM transcript_docs
                WHERE {where_sql}
                ORDER BY start_time_utc ASC
                """,
                params,
            ).fetchall()

        if not rows:
            return profile

        # Compute segment stats
        durations = []
        hour_counter: Counter = Counter()
        phrase_counter: Counter = Counter()
        callsigns_seen: set[str] = set()

        for row in rows:
            row_dict = dict(row)
            text = row_dict.get("text", "")
            flags_raw = row_dict.get("quality_flags", "[]")
            start_str = row_dict.get("start_time_utc", "")
            end_str = row_dict.get("end_time_utc", "")

            try:
                flags = json.loads(flags_raw) if flags_raw else []
            except (json.JSONDecodeError, TypeError):
                flags = []

            profile.total_segments += 1

            if "role:atc" in flags:
                profile.atc_segments += 1
            elif "role:pilot" in flags:
                profile.pilot_segments += 1
            else:
                profile.unknown_segments += 1

            start_dt = _parse_dt(start_str)
            end_dt = _parse_dt(end_str)

            if start_dt and end_dt:
                dur = (end_dt - start_dt).total_seconds()
                if 0 < dur < 300:
                    durations.append(dur)
                hour_counter[start_dt.hour] += 1
                if profile.time_window_start is None or start_dt < profile.time_window_start:
                    profile.time_window_start = start_dt
                if profile.time_window_end is None or end_dt > profile.time_window_end:
                    profile.time_window_end = end_dt

            for phrase_name, pattern in PHRASEOLOGY_PATTERNS.items():
                if pattern.search(text):
                    phrase_counter[phrase_name] += 1

        # Compute callsign stats from entity_mentions
        with self._conn() as conn:
            entity_where = ["feed_id = ?", "entity_type = 'callsign'"]
            entity_params: list[object] = [feed_id]
            if start_time:
                entity_where.append("timestamp_utc >= ?")
                entity_params.append(start_time)
            if end_time:
                entity_where.append("timestamp_utc <= ?")
                entity_params.append(end_time)

            try:
                callsign_rows = conn.execute(
                    f"""
                    SELECT DISTINCT normalized
                    FROM entity_mentions
                    WHERE {' AND '.join(entity_where)}
                    """,
                    entity_params,
                ).fetchall()
                callsigns_seen = {r["normalized"] for r in callsign_rows}
            except sqlite3.OperationalError:
                callsigns_seen = set()

            # Handoff destinations
            try:
                handoff_rows = conn.execute(
                    f"""
                    SELECT normalized, MAX(timestamp_utc) as last_ts
                    FROM entity_mentions
                    WHERE entity_type = 'frequency'
                      AND feed_id = ?
                      {('AND timestamp_utc >= ?' if start_time else '')}
                      {('AND timestamp_utc <= ?' if end_time else '')}
                    GROUP BY normalized
                    ORDER BY COUNT(*) DESC
                    LIMIT 20
                    """,
                    [p for p in [feed_id, start_time, end_time] if p is not None],
                ).fetchall()
                for hr in handoff_rows:
                    profile.handoff_destinations[hr["normalized"]] = 1
            except sqlite3.OperationalError:
                pass

        profile.unique_callsigns = len(callsigns_seen)
        profile.callsign_list = sorted(callsigns_seen)
        profile.total_talk_time = sum(durations)
        profile.avg_segment_duration = (sum(durations) / len(durations)) if durations else 0.0
        profile.phrases = dict(phrase_counter.most_common(30))
        profile.busiest_hours = hour_counter.most_common(24)

        return profile

    def summary_all_feeds(
        self,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> list[dict]:
        """Compute summary stats across all feeds."""
        where = []
        params: list[object] = []
        if start_time:
            where.append("start_time_utc >= ?")
            params.append(start_time)
        if end_time:
            where.append("end_time_utc <= ?")
            params.append(end_time)
        where_sql = " AND ".join(where) if where else "1=1"

        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT feed_id,
                       COUNT(*) as total_segments,
                       MIN(start_time_utc) as first_seen,
                       MAX(end_time_utc) as last_seen
                FROM transcript_docs
                WHERE {where_sql}
                GROUP BY feed_id
                ORDER BY total_segments DESC
                """,
                params,
            ).fetchall()

        summaries = []
        for row in rows:
            d = dict(row)
            # Count unique callsigns per feed
            try:
                with self._conn() as conn:
                    entity_where = ["feed_id = ?", "entity_type = 'callsign'"]
                    entity_params: list[object] = [d["feed_id"]]
                    if start_time:
                        entity_where.append("timestamp_utc >= ?")
                        entity_params.append(start_time)
                    if end_time:
                        entity_where.append("timestamp_utc <= ?")
                        entity_params.append(end_time)
                    cs_row = conn.execute(
                        f"""
                        SELECT COUNT(DISTINCT normalized) as unique_callsigns
                        FROM entity_mentions
                        WHERE {' AND '.join(entity_where)}
                        """,
                        entity_params,
                    ).fetchone()
                    d["unique_callsigns"] = cs_row["unique_callsigns"] if cs_row else 0
            except sqlite3.OperationalError:
                d["unique_callsigns"] = 0
            summaries.append(d)

        return summaries


def profile_to_dict(profile: PositionProfile) -> dict:
    """Serialize a PositionProfile to a JSON-safe dict."""
    return {
        "feed_id": profile.feed_id,
        "time_window_start": profile.time_window_start.isoformat() if profile.time_window_start else None,
        "time_window_end": profile.time_window_end.isoformat() if profile.time_window_end else None,
        "total_segments": profile.total_segments,
        "atc_segments": profile.atc_segments,
        "pilot_segments": profile.pilot_segments,
        "unknown_segments": profile.unknown_segments,
        "unique_callsigns": profile.unique_callsigns,
        "avg_segment_duration": round(profile.avg_segment_duration, 2),
        "total_talk_time": round(profile.total_talk_time, 1),
        "phrases": profile.phrases,
        "busiest_hours": [{"hour": h, "count": c} for h, c in profile.busiest_hours],
        "handoff_destinations": profile.handoff_destinations,
        "callsign_list": profile.callsign_list[:50],
    }
