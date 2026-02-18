"""Web dashboard for ATC Recorder – FastAPI backend."""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Config, load_config
from .logging import get_logger

logger = get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def _recordings_dir(config: Config) -> Path:
    return Path(config.output_dir)


def _scan_feeds(recordings: Path) -> list[str]:
    if not recordings.is_dir():
        return []
    return sorted(
        d.name for d in recordings.iterdir() if d.is_dir() and not d.name.startswith(".")
    )


def _scan_dates(recordings: Path, feed_id: str) -> list[str]:
    feed_dir = recordings / feed_id
    if not feed_dir.is_dir():
        return []
    return sorted(d.name for d in feed_dir.iterdir() if d.is_dir())


def _scan_recordings(recordings: Path, feed_id: str, date: str) -> list[dict]:
    day_dir = recordings / feed_id / date
    if not day_dir.is_dir():
        return []
    items = []
    for mp3 in sorted(day_dir.glob("*.mp3")):
        transcript = mp3.with_suffix(".json")
        items.append({
            "filename": mp3.name,
            "has_transcript": transcript.exists(),
            "size_bytes": mp3.stat().st_size,
        })
    return items


def _pipeline_stats(recordings: Path) -> dict:
    feed_dirs = [d for d in recordings.iterdir() if d.is_dir()] if recordings.is_dir() else []
    feeds = [d.name for d in feed_dirs if not d.name.startswith(".")]
    mp3_count = 0
    json_count = 0
    total_bytes = 0
    dates: set[str] = set()
    recent: list[dict] = []

    for feed_dir in feed_dirs:
        if feed_dir.name.startswith("."):
            continue
        for date_dir in sorted(feed_dir.iterdir()):
            if not date_dir.is_dir():
                continue
            dates.add(date_dir.name)
            for f in date_dir.iterdir():
                if f.suffix == ".mp3":
                    mp3_count += 1
                    total_bytes += f.stat().st_size
                elif f.suffix == ".json" and f.name != "metadata.json":
                    json_count += 1
                    try:
                        mtime = f.stat().st_mtime
                        recent.append({
                            "file": f.name,
                            "feed_id": feed_dir.name,
                            "date": date_dir.name,
                            "modified": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
                        })
                    except OSError:
                        pass

    recent.sort(key=lambda r: r["modified"], reverse=True)
    sorted_dates = sorted(dates)

    return {
        "feeds": sorted(feeds),
        "feed_count": len(feeds),
        "recording_count": mp3_count,
        "transcript_count": json_count,
        "total_audio_bytes": total_bytes,
        "total_audio_hours": round(total_bytes / (128_000 / 8 * 3600), 1),  # ~128kbps MP3
        "date_range": {
            "earliest": sorted_dates[0] if sorted_dates else None,
            "latest": sorted_dates[-1] if sorted_dates else None,
        },
        "recent_transcriptions": recent[:20],
    }


def _sqlite_doc_count(config: Config) -> Optional[int]:
    if config.rag is None:
        return None
    db_path = Path(config.rag.vector_store.sqlite_metadata_path)
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT COUNT(*) FROM transcript_docs").fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception:
        return None


def _check_service_health(url: str, timeout: float = 3.0) -> bool:
    try:
        import requests as _requests
        resp = _requests.get(url, timeout=timeout)
        return resp.status_code == 200
    except Exception:
        return False


def create_app(config: Config) -> FastAPI:
    """Build the FastAPI dashboard application."""

    app = FastAPI(title="ATC Recorder Dashboard", version="0.1.0")

    recordings = _recordings_dir(config)

    # ── Static files ────────────────────────────────────────────────
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ── HTML shell ──────────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse)
    async def index():
        index_path = STATIC_DIR / "index.html"
        return HTMLResponse(index_path.read_text(encoding="utf-8"))

    # ── Pipeline status ─────────────────────────────────────────────
    @app.get("/api/status")
    async def status():
        stats = _pipeline_stats(recordings)
        stats["ingested_doc_count"] = _sqlite_doc_count(config)

        services: dict[str, bool | None] = {}
        # Whisper ASR
        whisper_host = os.environ.get("WHISPER_GRPC_HOST", "whisper-asr")
        services["whisper_asr"] = _check_service_health(f"http://{whisper_host}:9000/v1/health/ready")

        if config.rag and config.rag.enabled:
            emb_endpoint = config.rag.embedding.endpoint
            base = emb_endpoint.rsplit("/", 1)[0] if "/" in emb_endpoint else emb_endpoint
            services["embedding_nim"] = _check_service_health(f"{base}/health/ready")

            milvus_host = config.rag.vector_store.host
            milvus_port = config.rag.vector_store.port
            services["milvus"] = _check_service_health(
                f"http://{milvus_host}:{9091}/healthz"
            )
        else:
            services["embedding_nim"] = None
            services["milvus"] = None

        stats["services"] = services
        stats["rag_enabled"] = bool(config.rag and config.rag.enabled)
        return stats

    # ── Feed list ───────────────────────────────────────────────────
    @app.get("/api/feeds")
    async def feeds():
        discovered = _scan_feeds(recordings)
        configured = config.feeds or []
        return {
            "configured": configured,
            "discovered": discovered,
        }

    # ── Recordings for a feed / date ────────────────────────────────
    @app.get("/api/recordings")
    async def list_recordings(
        feed_id: str = Query(...),
        date: Optional[str] = Query(None),
    ):
        if date:
            items = _scan_recordings(recordings, feed_id, date)
            return {"feed_id": feed_id, "date": date, "recordings": items}
        dates = _scan_dates(recordings, feed_id)
        return {"feed_id": feed_id, "dates": dates}

    # ── Single transcript JSON ──────────────────────────────────────
    @app.get("/api/transcript/{feed_id}/{date}/{filename}")
    async def get_transcript(feed_id: str, date: str, filename: str):
        if not filename.endswith(".json"):
            filename += ".json"
        path = recordings / feed_id / date / filename
        if not path.exists():
            raise HTTPException(404, "Transcript not found")
        return JSONResponse(json.loads(path.read_text(encoding="utf-8")))

    # ── Audio streaming ─────────────────────────────────────────────
    @app.get("/api/audio/{feed_id}/{date}/{filename}")
    async def get_audio(feed_id: str, date: str, filename: str):
        if not filename.endswith(".mp3"):
            filename += ".mp3"
        path = recordings / feed_id / date / filename
        if not path.exists():
            raise HTTPException(404, "Audio file not found")
        return FileResponse(path, media_type="audio/mpeg", filename=filename)

    # ── Semantic search (proxied) ───────────────────────────────────
    @app.post("/api/search")
    async def search(request: Request):
        if not config.rag or not config.rag.enabled:
            raise HTTPException(503, "RAG is not enabled in configuration")

        body = await request.json()
        query = str(body.get("query", "")).strip()
        if not query:
            raise HTTPException(400, "query is required")

        top_k = min(max(int(body.get("top_k", 10)), 1), 50)

        try:
            from .ingest import TranscriptIngestionService
            from .rag_models import SearchFilters

            service = TranscriptIngestionService(config)

            def _parse_time(v: Optional[str]) -> Optional[datetime]:
                if not v:
                    return None
                return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(timezone.utc)

            filters = SearchFilters(
                start_time_utc=_parse_time(body.get("start_time")),
                end_time_utc=_parse_time(body.get("end_time")),
                feed_ids=body.get("feed_ids"),
                exclude_feed_ids=body.get("exclude_feed_ids"),
            )
            hits = service.search(query=query, filters=filters, top_k=top_k)
            return {
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
            }
        except ImportError as exc:
            raise HTTPException(503, f"RAG dependencies not available: {exc}")
        except Exception as exc:
            logger.exception("Search error")
            raise HTTPException(500, str(exc))

    # ── Embedding explorer ──────────────────────────────────────────
    @app.get("/api/embeddings")
    async def get_embeddings(
        feed_id: str = Query(...),
        date: Optional[str] = Query(None),
        color_by: str = Query("feed"),
        dims: int = Query(2, ge=2, le=3),
    ):
        """Return dimensionality-reduced embeddings for visualization."""
        # Collect transcript segments with their text
        target_dirs: list[Path] = []
        if date:
            target_dirs.append(recordings / feed_id / date)
        else:
            feed_dir = recordings / feed_id
            if feed_dir.is_dir():
                target_dirs = sorted(d for d in feed_dir.iterdir() if d.is_dir())

        segments: list[dict] = []
        for d in target_dirs:
            for jf in sorted(d.glob("*.json")):
                if jf.name == "metadata.json":
                    continue
                try:
                    data = json.loads(jf.read_text(encoding="utf-8"))
                    for seg in data.get("segments", []):
                        text = (seg.get("text") or "").strip()
                        if not text or text == "...":
                            continue
                        segments.append({
                            "text": text,
                            "feed_id": feed_id,
                            "role": (seg.get("speaker_role") or "UNKNOWN").upper(),
                            "start_time": seg.get("start_time", 0),
                            "file": jf.stem,
                            "date": d.name,
                        })
                except Exception:
                    continue

        if not segments:
            return {"points": [], "count": 0}

        # Cap to prevent overload
        MAX_POINTS = 2000
        if len(segments) > MAX_POINTS:
            import random
            random.seed(42)
            segments = random.sample(segments, MAX_POINTS)

        # Embed all texts
        if not config.rag or not config.rag.enabled:
            raise HTTPException(503, "RAG must be enabled for embedding visualization")

        try:
            from .embedding import create_embedding_client
            client = create_embedding_client(config.rag.embedding)
            vectors = []
            for seg in segments:
                emb = client.embed_text(seg["text"], input_type="passage")
                vectors.append(emb.vector)
        except Exception as exc:
            raise HTTPException(503, f"Embedding service error: {exc}")

        # Dimensionality reduction
        try:
            import numpy as np
            X = np.array(vectors, dtype=np.float32)

            if X.shape[0] < 5:
                from sklearn.decomposition import PCA
                reducer = PCA(n_components=dims)
            else:
                try:
                    from sklearn.manifold import TSNE
                    perplexity = min(30, max(5, X.shape[0] - 1))
                    reducer = TSNE(
                        n_components=dims, perplexity=perplexity,
                        random_state=42, max_iter=500,
                    )
                except ImportError:
                    from sklearn.decomposition import PCA
                    reducer = PCA(n_components=dims)

            coords = reducer.fit_transform(X)
        except ImportError:
            raise HTTPException(503, "scikit-learn is required for embedding visualization (pip install scikit-learn)")

        # Build response
        points = []
        for i, seg in enumerate(segments):
            if color_by == "role":
                label = seg["role"]
            elif color_by == "time":
                label = seg["date"]
            else:
                label = seg["feed_id"]

            hover = f"{seg['role']} | {seg['date']} {seg['file']}\n{seg['text'][:100]}"
            point = {
                "x": float(coords[i, 0]),
                "y": float(coords[i, 1]),
                "color_label": label,
                "hover": hover,
            }
            if dims == 3:
                point["z"] = float(coords[i, 2])
            points.append(point)

        return {"points": points, "count": len(points)}

    return app


def run_dashboard(config: Config, host: str = "0.0.0.0", port: int = 8050) -> None:
    """Start the dashboard server."""
    import uvicorn

    app = create_app(config)
    logger.info("Dashboard starting on %s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")
