"""Web dashboard for ATC Recorder – FastAPI backend."""

import json
import os
import re
import shutil
import socket
import sqlite3
import time as _time
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
_ESTIMATED_MP3_BITRATE_BPS = 128_000


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


def _load_duration_seconds_by_file(day_dir: Path) -> dict[str, float]:
    """Load duration_seconds from day-level metadata.json keyed by file name."""
    metadata_path = day_dir / "metadata.json"
    if not metadata_path.exists():
        return {}

    try:
        raw = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    if isinstance(raw, dict):
        entries = [raw]
    elif isinstance(raw, list):
        entries = raw
    else:
        return {}

    durations: dict[str, float] = {}
    for item in entries:
        if not isinstance(item, dict):
            continue
        name = item.get("file")
        duration = item.get("duration_seconds")
        if not isinstance(name, str) or not isinstance(duration, (int, float)):
            continue
        # If duplicate entries exist, keep the latest valid value in the file.
        durations[name] = float(duration)

    return durations


def _resolve_recording_start(recordings: Path, audio_file: str) -> Optional[datetime]:
    """Resolve the actual recording start time from metadata.json or filename."""
    m = re.match(r"^(.+?)_(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})Z?\.mp3$", audio_file)
    if not m:
        return None
    feed_id, date_str, hh, mm = m.group(1), m.group(2), m.group(3), m.group(4)
    meta_path = recordings / feed_id / date_str / "metadata.json"
    if meta_path.exists():
        try:
            raw = json.loads(meta_path.read_text(encoding="utf-8"))
            entries = [raw] if isinstance(raw, dict) else raw if isinstance(raw, list) else []
            for entry in entries:
                if entry.get("file") == audio_file and entry.get("start_time"):
                    ts = entry["start_time"].replace("Z", "+00:00")
                    return datetime.fromisoformat(ts).astimezone(timezone.utc)
        except Exception:
            pass
    try:
        return datetime.strptime(f"{date_str} {hh}{mm}", "%Y-%m-%d %H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _audio_offset_seconds(recordings: Path, audio_file: str, segment_utc: datetime) -> float:
    """Compute the offset in seconds from the start of the audio file to a segment time."""
    rec_start = _resolve_recording_start(recordings, audio_file)
    if rec_start is None:
        return 0.0
    return max(0.0, (segment_utc - rec_start).total_seconds())


def _pipeline_stats(recordings: Path) -> dict:
    feed_dirs = [d for d in recordings.iterdir() if d.is_dir()] if recordings.is_dir() else []
    feeds = [d.name for d in feed_dirs if not d.name.startswith(".")]
    mp3_count = 0
    json_count = 0
    total_bytes = 0
    total_duration_seconds = 0.0
    metadata_covered_recordings = 0
    estimated_fallback_bytes = 0
    dates: set[str] = set()
    recent: list[dict] = []

    for feed_dir in feed_dirs:
        if feed_dir.name.startswith("."):
            continue
        for date_dir in sorted(feed_dir.iterdir()):
            if not date_dir.is_dir():
                continue
            dates.add(date_dir.name)
            duration_by_file = _load_duration_seconds_by_file(date_dir)
            for f in date_dir.iterdir():
                if f.suffix == ".mp3":
                    mp3_count += 1
                    size_bytes = f.stat().st_size
                    total_bytes += size_bytes
                    duration_seconds = duration_by_file.get(f.name)
                    if duration_seconds is not None:
                        total_duration_seconds += duration_seconds
                        metadata_covered_recordings += 1
                    else:
                        estimated_fallback_bytes += size_bytes
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
    estimated_fallback_seconds = estimated_fallback_bytes / (_ESTIMATED_MP3_BITRATE_BPS / 8)
    total_audio_hours = round((total_duration_seconds + estimated_fallback_seconds) / 3600, 1)
    fallback_recordings = mp3_count - metadata_covered_recordings
    coverage_pct = (metadata_covered_recordings / mp3_count * 100.0) if mp3_count else 0.0

    return {
        "feeds": sorted(feeds),
        "feed_count": len(feeds),
        "recording_count": mp3_count,
        "transcript_count": json_count,
        "total_audio_bytes": total_bytes,
        "total_audio_hours": total_audio_hours,
        "audio_hours_source": "metadata_with_size_fallback",
        "audio_hours_metadata_recordings": metadata_covered_recordings,
        "audio_hours_estimated_recordings": fallback_recordings,
        "audio_hours_metadata_coverage_pct": round(coverage_pct, 1),
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


def _check_tcp_port(host: str, port: int, timeout: float = 2.0) -> bool:
    """Best-effort TCP connectivity check for internal service ports."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
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
        whisper_http_ok = _check_service_health(f"http://{whisper_host}:9000/v1/health/ready")
        whisper_grpc_ok = _check_tcp_port(whisper_host, int(os.environ.get("WHISPER_GRPC_PORT", "50051")))
        services["whisper_asr"] = whisper_http_ok or whisper_grpc_ok
        parakeet_host = os.environ.get("PARAKEET_GRPC_HOST", "parakeet-asr")
        parakeet_http_ok = _check_service_health(f"http://{parakeet_host}:9000/v1/health/ready")
        parakeet_grpc_ok = _check_tcp_port(parakeet_host, int(os.environ.get("PARAKEET_GRPC_PORT", "50051")))
        services["parakeet_asr"] = parakeet_http_ok or parakeet_grpc_ok

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

    # ── ASR transcription (GUI trigger) ────────────────────────────
    @app.post("/api/asr/transcribe")
    async def transcribe_from_dashboard(request: Request):
        body = await request.json()
        feed_id = str(body.get("feed_id", "")).strip()
        date = str(body.get("date", "")).strip()
        filename = str(body.get("filename", "")).strip()
        model = str(body.get("model", "whisper")).strip().lower()
        preprocess = str(body.get("preprocess", "none")).strip().lower()

        if not feed_id or not date or not filename:
            raise HTTPException(400, "feed_id, date, and filename are required")

        if not filename.endswith(".mp3"):
            filename = f"{filename}.mp3"
        audio_path = recordings / feed_id / date / filename
        if not audio_path.exists():
            raise HTTPException(404, f"Audio file not found: {filename}")

        host_port_by_model = {
            "whisper": (
                os.environ.get("WHISPER_GRPC_HOST", "whisper-asr"),
                int(os.environ.get("WHISPER_GRPC_PORT", "50051")),
            ),
            "parakeet": (
                os.environ.get("PARAKEET_GRPC_HOST", "parakeet-asr"),
                int(os.environ.get("PARAKEET_GRPC_PORT", "50051")),
            ),
        }
        if model not in host_port_by_model:
            raise HTTPException(400, "model must be one of: whisper, parakeet")
        grpc_host, grpc_port = host_port_by_model[model]

        try:
            from .transcribe import (
                AudioPreprocess,
                RIVA_AVAILABLE,
                WhisperClient,
                save_transcript,
                stitch_transcript_boundary_with_previous,
                refresh_result_from_saved_transcript,
                export_timestamped_txt,
                export_srt,
            )
        except ImportError as exc:
            raise HTTPException(503, f"Transcription dependencies not available: {exc}")

        if not RIVA_AVAILABLE:
            raise HTTPException(
                503,
                "ASR client dependency missing in dashboard container (nvidia-riva-client)",
            )

        try:
            preprocess_mode = AudioPreprocess(preprocess)
        except ValueError:
            valid = ", ".join(m.value for m in AudioPreprocess)
            raise HTTPException(400, f"Invalid preprocess option '{preprocess}'. Valid: {valid}")

        trans_cfg = getattr(config, "transcription", None)
        segment_by_pauses = trans_cfg.segment_by_pauses if trans_cfg else False
        min_silence_duration = trans_cfg.min_silence_duration if trans_cfg else 0.5
        silence_threshold_dB = trans_cfg.silence_threshold_dB if trans_cfg else -30.0
        min_speech_duration = trans_cfg.min_speech_duration if trans_cfg else 0.3
        merge_gap_seconds = trans_cfg.merge_gap_seconds if trans_cfg else 0.5
        output_format = trans_cfg.output_format if trans_cfg else "json"
        diarization_enabled = trans_cfg.diarization_enabled if trans_cfg else False
        diarization_mode = trans_cfg.diarization_mode if trans_cfg else "role-heuristic"
        stitch_across_files = trans_cfg.stitch_across_files if trans_cfg else False
        stitch_max_gap_seconds = trans_cfg.stitch_max_gap_seconds if trans_cfg else 2.0
        stitch_min_text_overlap_chars = trans_cfg.stitch_min_text_overlap_chars if trans_cfg else 12

        def _has_content(candidate) -> bool:
            text = str(candidate.text or "").strip()
            if text:
                return True
            for seg in candidate.segments or []:
                seg_text = str(seg.get("text", "")).strip()
                if seg_text and seg_text != "...":
                    return True
            return False

        try:
            client = WhisperClient(
                grpc_host=grpc_host,
                grpc_port=grpc_port,
                language_code="en-US",
            )
            if not client.check_connection():
                raise HTTPException(503, f"Cannot connect to ASR service at {grpc_host}:{grpc_port}")

            attempts: list[tuple[bool, AudioPreprocess]] = [(segment_by_pauses, preprocess_mode)]
            if segment_by_pauses:
                attempts.append((False, preprocess_mode))
            if preprocess_mode != AudioPreprocess.NONE:
                attempts.append((False, AudioPreprocess.NONE))

            deduped_attempts: list[tuple[bool, AudioPreprocess]] = []
            for attempt in attempts:
                if attempt not in deduped_attempts:
                    deduped_attempts.append(attempt)

            result = None
            used_segment_by_pauses = segment_by_pauses
            used_preprocess = preprocess_mode
            for attempt_segment_by_pauses, attempt_preprocess in deduped_attempts:
                candidate = client.convert_and_transcribe(
                    audio_path=audio_path,
                    preprocess=attempt_preprocess,
                    segment_by_pauses=attempt_segment_by_pauses,
                    min_silence_duration=min_silence_duration,
                    silence_threshold_dB=silence_threshold_dB,
                    min_speech_duration=min_speech_duration,
                    merge_gap_seconds=merge_gap_seconds,
                    diarization_enabled=diarization_enabled,
                    diarization_mode=diarization_mode,
                )
                if candidate.success and _has_content(candidate):
                    result = candidate
                    used_segment_by_pauses = attempt_segment_by_pauses
                    used_preprocess = attempt_preprocess
                    break
                result = candidate
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Dashboard ASR run failed")
            raise HTTPException(500, f"ASR execution failed: {exc}")

        if not result or not result.success:
            raise HTTPException(500, f"ASR transcription failed: {result.error if result else 'unknown error'}")
        if not _has_content(result):
            raise HTTPException(
                422,
                "ASR returned no transcript content for this recording with the selected settings",
            )

        # Only persist when we have content.
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
            export_timestamped_txt(result)
        elif output_format == "srt":
            export_srt(result)

        return {
            "success": True,
            "model": model,
            "preprocess": used_preprocess.value,
            "segment_by_pauses": used_segment_by_pauses,
            "audio_file": filename,
            "transcript_file": str(result.transcript_file) if result.transcript_file else None,
            "segment_count": len(result.segments),
            "text_preview": result.text[:200],
        }

    # ── Audio Lab: preprocessing experiment endpoints ─────────────
    def _preprocessed_dir() -> Path:
        trans_cfg = getattr(config, "transcription", None)
        if trans_cfg and trans_cfg.preprocess_output_dir:
            p = Path(trans_cfg.preprocess_output_dir)
            if not p.is_absolute():
                return recordings / p.name
            return p
        return recordings / "preprocessed"

    _ALL_PREPROCESS_METHODS = ["none", "ffmpeg", "ffmpeg_vad", "sox", "maxine"]

    @app.get("/api/preprocess/status")
    async def preprocess_status():
        sox_ok = shutil.which("sox") is not None
        try:
            from .transcribe import _maxine_available
            maxine_ok = _maxine_available()
        except Exception:
            maxine_ok = False
        available = ["none", "ffmpeg", "ffmpeg_vad"]
        if sox_ok:
            available.append("sox")
        if maxine_ok:
            available.append("maxine")
        return {
            "available_methods": available,
            "all_methods": _ALL_PREPROCESS_METHODS,
            "maxine_available": maxine_ok,
            "sox_available": sox_ok,
        }

    _METHODS_LONGEST_FIRST = sorted(_ALL_PREPROCESS_METHODS, key=len, reverse=True)

    def _parse_artifact(stem: str, filepath: Path) -> Optional[dict]:
        """Parse a preprocessed artifact filename into structured metadata."""
        name = filepath.stem
        if not name.startswith(stem + "_"):
            return None
        remainder = name[len(stem) + 1:]

        method = None
        for m in _METHODS_LONGEST_FIRST:
            if remainder == m or remainder.startswith(m + "_"):
                method = m
                break
        if method is None:
            return None

        after_method = remainder[len(method):]
        param_tag = ""
        if after_method.startswith("_custom_"):
            param_tag = after_method[len("_custom_"):]
        elif after_method.startswith("_"):
            param_tag = after_method[1:]

        try:
            stat = filepath.stat()
            mtime = stat.st_mtime
            size = stat.st_size
        except OSError:
            return None

        return {
            "method": method,
            "filename": filepath.name,
            "size_bytes": size,
            "ext": filepath.suffix,
            "param_tag": param_tag,
            "is_custom": bool(param_tag),
            "mtime": mtime,
            "mtime_iso": datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        }

    @app.get("/api/preprocess/artifacts")
    async def preprocess_artifacts(
        feed_id: str = Query(...),
        date: str = Query(...),
        filename: str = Query(...),
    ):
        stem = Path(filename).stem
        artifact_dir = _preprocessed_dir() / feed_id / date
        artifacts = []
        if artifact_dir.is_dir():
            for f in sorted(artifact_dir.iterdir()):
                if not f.is_file():
                    continue
                if f.suffix not in (".wav", ".mp3"):
                    continue
                parsed = _parse_artifact(stem, f)
                if parsed:
                    artifacts.append(parsed)
        artifacts.sort(key=lambda a: a["mtime"], reverse=True)
        return {"original": filename, "artifacts": artifacts}

    @app.delete("/api/preprocess/artifacts")
    async def delete_preprocess_artifacts(request: Request):
        body = await request.json()
        feed_id = str(body.get("feed_id", "")).strip()
        date = str(body.get("date", "")).strip()
        filename = str(body.get("filename", "")).strip()
        target = str(body.get("target", "all")).strip()

        if not feed_id or not date or not filename:
            raise HTTPException(400, "feed_id, date, and filename are required")

        stem = Path(filename).stem
        artifact_dir = _preprocessed_dir() / feed_id / date
        if not artifact_dir.is_dir():
            return {"deleted": 0}

        deleted = 0
        if target == "all":
            for f in list(artifact_dir.iterdir()):
                if f.is_file() and f.stem.startswith(stem + "_") and f.suffix in (".wav", ".mp3"):
                    f.unlink()
                    deleted += 1
        else:
            single = artifact_dir / target
            if single.exists() and single.is_file():
                single.unlink()
                deleted = 1

        return {"deleted": deleted}

    _PARAM_SCHEMA = {
        "ffmpeg": {
            "highpass_freq":        {"type": int,   "min": 50,   "max": 1000, "default": 300},
            "lowpass_freq":         {"type": int,   "min": 1000, "max": 8000, "default": 3400},
            "noise_floor_db":       {"type": int,   "min": -60,  "max": 0,    "default": -25},
            "dynaudnorm_peak":      {"type": float, "min": 0.1,  "max": 1.0,  "default": 0.9},
            "dynaudnorm_smoothing": {"type": int,   "min": 1,    "max": 30,   "default": 5},
        },
        "ffmpeg_vad": {
            "highpass_freq":        {"type": int,   "min": 50,   "max": 1000, "default": 300},
            "lowpass_freq":         {"type": int,   "min": 1000, "max": 8000, "default": 3400},
            "noise_floor_db":       {"type": int,   "min": -60,  "max": 0,    "default": -20},
            "dynaudnorm_peak":      {"type": float, "min": 0.1,  "max": 1.0,  "default": 0.9},
            "dynaudnorm_smoothing": {"type": int,   "min": 1,    "max": 30,   "default": 3},
            "silence_stop_duration":{"type": float, "min": 0.05, "max": 5.0,  "default": 0.3},
            "silence_threshold_db": {"type": int,   "min": -60,  "max": 0,    "default": -30},
            "leave_silence":        {"type": float, "min": 0.0,  "max": 2.0,  "default": 0.1},
        },
        "sox": {
            "noise_sample_duration":{"type": float, "min": 0.1,  "max": 5.0,  "default": 0.5},
            "noise_reduction":      {"type": float, "min": 0.0,  "max": 1.0,  "default": 0.21},
            "highpass_freq":        {"type": int,   "min": 50,   "max": 1000, "default": 300},
            "lowpass_freq":         {"type": int,   "min": 1000, "max": 8000, "default": 3400},
        },
        "maxine": {
            "intensity_ratio":      {"type": float, "min": 0.0,  "max": 1.0,  "default": 1.0},
            "effect_version":       {"type": int,   "min": 1,    "max": 2,    "default": 1},
            "enable_vad":           {"type": int,   "min": 0,    "max": 1,    "default": 0},
            "effect":               {"type": str,   "options": ["denoiser", "dereverb_denoiser"], "default": "denoiser"},
        },
    }

    @app.get("/api/preprocess/params")
    async def preprocess_params():
        """Return the tunable parameter schema for each method."""
        schema = {}
        for method, params in _PARAM_SCHEMA.items():
            schema[method] = {}
            for k, v in params.items():
                entry: dict = {"type": v["type"].__name__, "default": v["default"]}
                if "options" in v:
                    entry["options"] = v["options"]
                else:
                    entry["min"] = v["min"]
                    entry["max"] = v["max"]
                schema[method][k] = entry
        return {"params": schema}

    def _validate_params(method: str, raw: dict) -> dict:
        """Validate and coerce user-supplied params against the schema."""
        schema = _PARAM_SCHEMA.get(method, {})
        validated = {}
        for key, value in raw.items():
            if key not in schema:
                continue
            spec = schema[key]
            if "options" in spec:
                sval = str(value)
                if sval in spec["options"]:
                    validated[key] = sval
                continue
            try:
                coerced = spec["type"](value)
            except (TypeError, ValueError):
                continue
            coerced = max(spec["min"], min(spec["max"], coerced))
            if key == "enable_vad":
                validated[key] = bool(coerced)
            else:
                validated[key] = coerced
        return validated

    def _build_artifact_suffix(method: str, params: dict) -> str:
        """Build a descriptive filename suffix from method params.

        Maxine always gets a descriptive tag so runs are distinguishable.
        Other methods only get a suffix when params differ from defaults.
        """
        if method == "maxine":
            ir = params.get("intensity_ratio", 1.0)
            ev = params.get("effect_version", 1)
            vad = params.get("enable_vad", False)
            eff = params.get("effect", "denoiser")
            parts = [f"i{ir}"]
            if eff != "denoiser":
                parts.append("derev")
            parts.append(f"v{ev}")
            if vad:
                parts.append("vad")
            return "_" + "_".join(parts)
        if not params:
            return ""
        tag = "_".join(f"{k}{v}" for k, v in sorted(params.items()))
        return f"_custom_{tag}"

    @app.post("/api/preprocess/run")
    async def preprocess_run(request: Request):
        body = await request.json()
        feed_id = str(body.get("feed_id", "")).strip()
        date = str(body.get("date", "")).strip()
        filename = str(body.get("filename", "")).strip()
        methods = body.get("methods", [])
        custom_params: dict = body.get("params", {})

        if not feed_id or not date or not filename:
            raise HTTPException(400, "feed_id, date, and filename are required")
        if not methods:
            raise HTTPException(400, "methods list is required")

        if not filename.endswith(".mp3"):
            filename = f"{filename}.mp3"
        audio_path = recordings / feed_id / date / filename
        if not audio_path.exists():
            raise HTTPException(404, f"Audio file not found: {filename}")

        try:
            from .transcribe import (
                _default_wav_convert,
                preprocess_audio_ffmpeg,
                preprocess_audio_ffmpeg_vad,
                preprocess_audio_sox,
                preprocess_audio_maxine,
            )
        except ImportError as exc:
            raise HTTPException(503, f"Transcription module not available: {exc}")

        dispatch = {
            "none": _default_wav_convert,
            "ffmpeg": preprocess_audio_ffmpeg,
            "ffmpeg_vad": preprocess_audio_ffmpeg_vad,
            "sox": preprocess_audio_sox,
            "maxine": preprocess_audio_maxine,
        }

        stem = Path(filename).stem
        out_dir = _preprocessed_dir() / feed_id / date
        out_dir.mkdir(parents=True, exist_ok=True)

        has_custom = bool(custom_params)

        results = []
        for method in methods:
            if method not in dispatch:
                results.append({"method": method, "success": False, "error": f"Unknown method: {method}"})
                continue

            method_params = _validate_params(method, custom_params.get(method, {})) if has_custom else {}

            suffix = _build_artifact_suffix(method, method_params)
            out_path = out_dir / f"{stem}_{method}{suffix}.wav"

            t0 = _time.monotonic()
            try:
                ok = dispatch[method](audio_path, out_path, **method_params)
                elapsed = round(_time.monotonic() - t0, 2)
                results.append({
                    "method": method,
                    "success": ok,
                    "filename": out_path.name if ok else None,
                    "size_bytes": out_path.stat().st_size if ok and out_path.exists() else 0,
                    "elapsed_seconds": elapsed,
                    "params_applied": method_params or None,
                    "error": None if ok else f"{method} preprocessing failed",
                })
            except Exception as exc:
                elapsed = round(_time.monotonic() - t0, 2)
                results.append({
                    "method": method,
                    "success": False,
                    "error": str(exc),
                    "elapsed_seconds": elapsed,
                })

        return {"audio_file": filename, "results": results}

    @app.get("/api/preprocessed/{feed_id}/{date}/{filename}")
    async def get_preprocessed_audio(feed_id: str, date: str, filename: str):
        path = _preprocessed_dir() / feed_id / date / filename
        if not path.exists():
            raise HTTPException(404, "Preprocessed audio file not found")
        if path.suffix == ".mp3":
            media = "audio/mpeg"
        else:
            media = "audio/wav"
        return FileResponse(path, media_type=media, filename=filename)

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
                        "start_offset_seconds": _audio_offset_seconds(recordings, h.audio_file, h.start_time_utc),
                        "end_offset_seconds": _audio_offset_seconds(recordings, h.audio_file, h.end_time_utc),
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

    # ── Entity search ──────────────────────────────────────────
    def _get_metadata_store():
        if config.rag and config.rag.vector_store:
            db_path = Path(config.rag.vector_store.sqlite_metadata_path)
        else:
            db_path = recordings / "rag_metadata.db"
        if not db_path.exists():
            return None
        from .ingest import MetadataStore
        store = MetadataStore(db_path)
        store.ensure_schema()
        return store

    @app.get("/api/entities/search")
    async def search_entities(
        q: str = Query(""),
        entity_type: Optional[str] = Query(None),
        feed_id: Optional[str] = Query(None),
        start_time: Optional[str] = Query(None),
        end_time: Optional[str] = Query(None),
        limit: int = Query(100, ge=1, le=500),
    ):
        store = _get_metadata_store()
        if not store:
            raise HTTPException(503, "Metadata store not available")
        results = store.search_entities(
            normalized=q.upper() if q else None,
            entity_type=entity_type,
            feed_id=feed_id,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )
        return {"query": q, "results": results, "count": len(results)}

    @app.get("/api/entities/active")
    async def active_callsigns(
        feed_id: Optional[str] = Query(None),
        start_time: Optional[str] = Query(None),
        end_time: Optional[str] = Query(None),
        limit: int = Query(100, ge=1, le=500),
    ):
        store = _get_metadata_store()
        if not store:
            raise HTTPException(503, "Metadata store not available")
        results = store.get_active_callsigns(
            feed_id=feed_id, start_time=start_time, end_time=end_time, limit=limit,
        )
        return {"feed_id": feed_id, "callsigns": results, "count": len(results)}

    # ── Flight tracking ────────────────────────────────────────
    def _get_flight_tracker():
        store = _get_metadata_store()
        if not store:
            return None
        from .flight_tracker import FlightTracker
        return FlightTracker(metadata_store=store)

    @app.get("/api/flights/recent")
    async def recent_flights(limit: int = Query(50, ge=1, le=200)):
        store = _get_metadata_store()
        if not store:
            raise HTTPException(503, "Metadata store not available")
        flights = store.get_recent_flights(limit=limit)
        return {"flights": flights, "count": len(flights)}

    @app.get("/api/flights/{callsign}")
    async def get_flight_track(callsign: str):
        tracker = _get_flight_tracker()
        if not tracker:
            raise HTTPException(503, "Metadata store not available")
        from .flight_tracker import flight_track_to_dict
        track = tracker.track_flight(callsign.upper())
        if not track:
            raise HTTPException(404, f"No data found for callsign {callsign}")
        return flight_track_to_dict(track)

    # ── Controller profiling ───────────────────────────────────
    def _get_profiler():
        if config.rag and config.rag.vector_store:
            db_path = Path(config.rag.vector_store.sqlite_metadata_path)
        else:
            db_path = recordings / "rag_metadata.db"
        if not db_path.exists():
            return None
        from .controller_profile import ControllerProfiler
        return ControllerProfiler(db_path)

    @app.get("/api/profile/{feed_id}")
    async def get_position_profile(
        feed_id: str,
        start_time: Optional[str] = Query(None),
        end_time: Optional[str] = Query(None),
    ):
        profiler = _get_profiler()
        if not profiler:
            raise HTTPException(503, "Metadata store not available")
        from .controller_profile import profile_to_dict
        profile = profiler.profile_feed(feed_id, start_time=start_time, end_time=end_time)
        return profile_to_dict(profile)

    @app.get("/api/profile/summary")
    async def profile_summary(
        start_time: Optional[str] = Query(None),
        end_time: Optional[str] = Query(None),
    ):
        profiler = _get_profiler()
        if not profiler:
            raise HTTPException(503, "Metadata store not available")
        summaries = profiler.summary_all_feeds(start_time=start_time, end_time=end_time)
        return {"feeds": summaries, "count": len(summaries)}

    return app


def run_dashboard(config: Config, host: str = "0.0.0.0", port: int = 8050) -> None:
    """Start the dashboard server."""
    import uvicorn

    app = create_app(config)
    logger.info("Dashboard starting on %s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")
