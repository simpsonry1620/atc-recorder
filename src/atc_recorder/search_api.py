"""Minimal HTTP API for semantic transcript search."""

import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .config import Config
from .ingest import TranscriptIngestionService
from .logging import get_logger
from .rag_models import SearchFilters

logger = get_logger(__name__)


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class SearchApiServer:
    """Search API server wrapper."""

    def __init__(self, config: Config):
        if config.rag is None:
            raise ValueError("RAG config is required for API server")
        self.config = config
        self.service = TranscriptIngestionService(config)

    def run(self) -> None:
        host = self.config.rag.api.host
        port = self.config.rag.api.port
        service = self.service
        top_k_default = self.config.rag.api.top_k_default
        top_k_max = self.config.rag.api.top_k_max

        class Handler(BaseHTTPRequestHandler):
            def _json_response(handler_self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode("utf-8")
                handler_self.send_response(status)
                handler_self.send_header("Content-Type", "application/json")
                handler_self.send_header("Content-Length", str(len(body)))
                handler_self.end_headers()
                handler_self.wfile.write(body)

            def do_GET(handler_self) -> None:  # noqa: N802
                if handler_self.path != "/health":
                    handler_self._json_response(404, {"error": "not_found"})
                    return
                healthy = (
                    service.vector_store.check_health() and service.embedding_client.check_health()
                )
                handler_self._json_response(
                    200 if healthy else 503, {"status": "ok" if healthy else "degraded"}
                )

            def do_POST(handler_self) -> None:  # noqa: N802
                if handler_self.path != "/search":
                    handler_self._json_response(404, {"error": "not_found"})
                    return
                try:
                    content_length = int(handler_self.headers.get("Content-Length", "0"))
                    data = json.loads(handler_self.rfile.read(content_length).decode("utf-8"))
                    query = str(data.get("query", "")).strip()
                    if not query:
                        handler_self._json_response(400, {"error": "query is required"})
                        return

                    top_k = int(data.get("top_k", top_k_default))
                    top_k = max(1, min(top_k, top_k_max))

                    filters = SearchFilters(
                        start_time_utc=_parse_time(data.get("start_time")),
                        end_time_utc=_parse_time(data.get("end_time")),
                        feed_ids=data.get("feed_ids"),
                        exclude_feed_ids=data.get("exclude_feed_ids"),
                    )
                    hits = service.search(query=query, filters=filters, top_k=top_k)
                    handler_self._json_response(
                        200,
                        {
                            "query": query,
                            "top_k": top_k,
                            "hits": [
                                {
                                    "doc_id": h.doc_id,
                                    "score": h.score,
                                    "feed_id": h.feed_id,
                                    "audio_file": h.audio_file,
                                    "segment_index": h.segment_index,
                                    "start_time_utc": h.start_time_utc.isoformat(),
                                    "end_time_utc": h.end_time_utc.isoformat(),
                                    "text": h.text,
                                }
                                for h in hits
                            ],
                        },
                    )
                except Exception as exc:
                    logger.exception("Search API error")
                    handler_self._json_response(500, {"error": str(exc)})

            def log_message(self, format: str, *args) -> None:  # noqa: A003
                logger.info("search-api %s - %s", self.address_string(), format % args)

        httpd = ThreadingHTTPServer((host, port), Handler)
        logger.info("Search API listening on %s:%s", host, port)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            logger.info("Search API interrupted")
        finally:
            httpd.server_close()
