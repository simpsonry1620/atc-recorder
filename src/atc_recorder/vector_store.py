"""Vector storage abstractions and Milvus implementation."""

import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from .config import VectorStoreConfig
from .logging import get_logger
from .rag_models import SearchFilters, SearchHit, TranscriptDocument

logger = get_logger(__name__)

try:
    from pymilvus import (
        Collection,
        CollectionSchema,
        DataType,
        FieldSchema,
        connections,
        utility,
    )

    PYMILVUS_AVAILABLE = True
except ImportError:
    PYMILVUS_AVAILABLE = False


def _to_epoch_ms(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def _from_epoch_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


class VectorStoreAdapter(ABC):
    """Provider-agnostic vector store contract."""

    @abstractmethod
    def ensure_schema(self) -> None:
        """Create required collection/index objects if needed."""

    @abstractmethod
    def upsert_documents(
        self, documents: list[TranscriptDocument], vectors: list[list[float]]
    ) -> int:
        """Insert or update documents and vectors."""

    @abstractmethod
    def search(
        self, query_vector: list[float], filters: SearchFilters, top_k: int
    ) -> list[SearchHit]:
        """Search vectors with metadata filters."""

    @abstractmethod
    def check_health(self) -> bool:
        """Return provider connectivity and schema readiness."""


class MilvusVectorStore(VectorStoreAdapter):
    """Milvus-backed vector store."""

    def __init__(self, config: VectorStoreConfig):
        if not PYMILVUS_AVAILABLE:
            raise RuntimeError("pymilvus not installed. Install with: pip install pymilvus")
        self.config = config
        self._connected = False
        self._collection: Collection | None = None

    def _connect(self) -> None:
        if self._connected:
            return
        uri = f"http://{self.config.host}:{self.config.port}"
        connections.connect(alias="default", uri=uri)
        self._connected = True

    def _build_collection(self) -> Collection:
        self._connect()
        name = self.config.collection_name
        if not utility.has_collection(name):
            fields = [
                FieldSchema(name="doc_id", dtype=DataType.VARCHAR, max_length=256, is_primary=True),
                FieldSchema(name="feed_id", dtype=DataType.VARCHAR, max_length=64),
                FieldSchema(name="audio_file", dtype=DataType.VARCHAR, max_length=256),
                FieldSchema(name="segment_index", dtype=DataType.INT64),
                FieldSchema(name="start_time_utc", dtype=DataType.INT64),
                FieldSchema(name="end_time_utc", dtype=DataType.INT64),
                FieldSchema(name="ingested_at", dtype=DataType.INT64),
                FieldSchema(name="quality_flags_json", dtype=DataType.VARCHAR, max_length=512),
                FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=65535),
                FieldSchema(
                    name="embedding", dtype=DataType.FLOAT_VECTOR, dim=self.config.embedding_dim
                ),
            ]
            schema = CollectionSchema(fields=fields, enable_dynamic_field=False)
            collection = Collection(name=name, schema=schema)
            index_params = {
                "metric_type": self.config.metric_type,
                "index_type": self.config.index_type,
                "params": {"M": 16, "efConstruction": 128},
            }
            collection.create_index(field_name="embedding", index_params=index_params)
            collection.load()
            return collection
        collection = Collection(name=name)
        collection.load()
        return collection

    def ensure_schema(self) -> None:
        self._collection = self._build_collection()

    def _collection_obj(self) -> Collection:
        if self._collection is None:
            self.ensure_schema()
        assert self._collection is not None
        return self._collection

    def upsert_documents(
        self, documents: list[TranscriptDocument], vectors: list[list[float]]
    ) -> int:
        if not documents:
            return 0
        if len(documents) != len(vectors):
            raise ValueError("Documents and vectors length mismatch")

        collection = self._collection_obj()
        doc_ids = [d.doc_id for d in documents]
        expr = "doc_id in [" + ", ".join(json.dumps(doc_id) for doc_id in doc_ids) + "]"
        collection.delete(expr)

        rows = []
        for doc, vector in zip(documents, vectors):
            rows.append(
                {
                    "doc_id": doc.doc_id,
                    "feed_id": doc.feed_id,
                    "audio_file": doc.audio_file,
                    "segment_index": doc.segment_index,
                    "start_time_utc": _to_epoch_ms(doc.start_time_utc),
                    "end_time_utc": _to_epoch_ms(doc.end_time_utc),
                    "ingested_at": _to_epoch_ms(doc.ingested_at),
                    "quality_flags_json": json.dumps(doc.quality_flags),
                    "text": doc.text,
                    "embedding": vector,
                }
            )

        collection.insert(rows)
        collection.flush()
        return len(rows)

    def _build_filter_expr(self, filters: SearchFilters) -> str:
        parts = []
        if filters.start_time_utc is not None:
            parts.append(f"end_time_utc >= {_to_epoch_ms(filters.start_time_utc)}")
        if filters.end_time_utc is not None:
            parts.append(f"start_time_utc <= {_to_epoch_ms(filters.end_time_utc)}")
        if filters.feed_ids:
            feed_expr = ", ".join(json.dumps(feed_id) for feed_id in filters.feed_ids)
            parts.append(f"feed_id in [{feed_expr}]")
        if filters.exclude_feed_ids:
            feed_expr = ", ".join(json.dumps(feed_id) for feed_id in filters.exclude_feed_ids)
            parts.append(f"feed_id not in [{feed_expr}]")
        return " and ".join(parts) if parts else ""

    def search(
        self, query_vector: list[float], filters: SearchFilters, top_k: int
    ) -> list[SearchHit]:
        collection = self._collection_obj()
        expr = self._build_filter_expr(filters)
        search_params = {"metric_type": self.config.metric_type, "params": {"ef": 128}}
        results = collection.search(
            data=[query_vector],
            anns_field="embedding",
            param=search_params,
            limit=top_k,
            expr=expr,
            output_fields=[
                "doc_id",
                "feed_id",
                "audio_file",
                "segment_index",
                "start_time_utc",
                "end_time_utc",
                "text",
            ],
        )
        hits: list[SearchHit] = []
        for row in results[0]:
            entity = row.entity
            hits.append(
                SearchHit(
                    doc_id=entity.get("doc_id"),
                    score=float(row.score),
                    feed_id=entity.get("feed_id"),
                    audio_file=entity.get("audio_file"),
                    segment_index=int(entity.get("segment_index")),
                    start_time_utc=_from_epoch_ms(int(entity.get("start_time_utc"))),
                    end_time_utc=_from_epoch_ms(int(entity.get("end_time_utc"))),
                    text=entity.get("text"),
                )
            )
        return hits

    def check_health(self) -> bool:
        try:
            self.ensure_schema()
            return True
        except Exception as exc:
            logger.debug("Milvus health check failed: %s", exc)
            return False


def create_vector_store(config: VectorStoreConfig) -> VectorStoreAdapter:
    """Factory for vector store providers."""
    provider = config.provider.lower()
    if provider in {"milvus"}:
        return MilvusVectorStore(config)
    raise ValueError(f"Unsupported vector store provider: {config.provider}")
