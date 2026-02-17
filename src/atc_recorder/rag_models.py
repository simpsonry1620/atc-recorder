"""Shared domain models for transcript ingestion and retrieval."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class TranscriptDocument:
    """Normalized transcript document used for ingestion and retrieval."""

    doc_id: str
    feed_id: str
    audio_file: str
    segment_index: int
    start_time_utc: datetime
    end_time_utc: datetime
    text: str
    quality_flags: list[str] = field(default_factory=list)
    ingested_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class SearchFilters:
    """Filter criteria for semantic transcript search."""

    start_time_utc: Optional[datetime] = None
    end_time_utc: Optional[datetime] = None
    feed_ids: Optional[list[str]] = None
    exclude_feed_ids: Optional[list[str]] = None


@dataclass
class SearchHit:
    """Single search result hit."""

    doc_id: str
    score: float
    feed_id: str
    audio_file: str
    segment_index: int
    start_time_utc: datetime
    end_time_utc: datetime
    text: str
