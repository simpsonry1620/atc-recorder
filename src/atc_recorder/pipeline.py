"""Composable audio preprocessing pipeline.

Provides atomic pipeline steps that can be chained together, a pipeline
executor that merges consecutive ffmpeg steps for efficiency, and a
SQLite-backed preset store.
"""

import json
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class PipelineStep:
    """A single atomic preprocessing step."""

    step_type: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineDefinition:
    """An ordered list of preprocessing steps, optionally named."""

    steps: list[PipelineStep] = field(default_factory=list)
    name: Optional[str] = None
    graph_json: Optional[dict] = None  # Litegraph visual layout

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "steps": [{"step": s.step_type, "params": s.params} for s in self.steps],
        }
        if self.name:
            d["name"] = self.name
        if self.graph_json:
            d["graph_json"] = self.graph_json
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "PipelineDefinition":
        steps = [
            PipelineStep(step_type=s["step"], params=s.get("params", {}))
            for s in data.get("steps", [])
        ]
        return cls(
            steps=steps,
            name=data.get("name"),
            graph_json=data.get("graph_json"),
        )


# ---------------------------------------------------------------------------
# Step parameter schemas
# ---------------------------------------------------------------------------

STEP_PARAM_SCHEMA: dict[str, dict[str, dict]] = {
    "maxine": {
        "intensity_ratio": {"type": "float", "min": 0.0, "max": 1.0, "default": 1.0},
        "effect_version": {"type": "int", "min": 1, "max": 2, "default": 1},
        "enable_vad": {"type": "bool", "default": False},
        "effect": {
            "type": "str",
            "options": [
                "denoiser",
                "dereverb_denoiser",
                "superres",
                "studio_voice_high_quality",
            ],
            "default": "denoiser",
        },
    },
    "bandpass": {
        "highpass_freq": {"type": "int", "min": 50, "max": 1000, "default": 300},
        "lowpass_freq": {"type": "int", "min": 1000, "max": 8000, "default": 3400},
    },
    "denoise": {
        "noise_floor_db": {"type": "int", "min": -60, "max": 0, "default": -25},
    },
    "silence_remove": {
        "stop_duration": {"type": "float", "min": 0.05, "max": 5.0, "default": 0.3},
        "threshold_db": {"type": "int", "min": -60, "max": 0, "default": -30},
        "leave_silence": {"type": "float", "min": 0.0, "max": 2.0, "default": 0.1},
    },
    "normalize": {
        "peak": {"type": "float", "min": 0.1, "max": 1.0, "default": 0.9},
        "smoothing": {"type": "int", "min": 1, "max": 30, "default": 5},
    },
    "sox_noisered": {
        "noise_sample_duration": {"type": "float", "min": 0.1, "max": 5.0, "default": 0.5},
        "noise_reduction": {"type": "float", "min": 0.0, "max": 1.0, "default": 0.21},
        "highpass_freq": {"type": "int", "min": 50, "max": 1000, "default": 300},
        "lowpass_freq": {"type": "int", "min": 1000, "max": 8000, "default": 3400},
    },
    "pad": {
        "pad_before": {"type": "float", "min": 0.0, "max": 5.0, "default": 0.5},
        "pad_after": {"type": "float", "min": 0.0, "max": 5.0, "default": 0.5},
    },
}

_TYPE_COERCE = {"float": float, "int": int, "str": str, "bool": bool}


def validate_step_params(step_type: str, raw: dict) -> dict:
    """Validate and coerce parameters against the schema for a step type."""
    schema = STEP_PARAM_SCHEMA.get(step_type, {})
    validated: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in schema:
            continue
        spec = schema[key]

        if spec.get("type") == "bool":
            validated[key] = bool(value)
            continue

        if "options" in spec:
            sval = str(value)
            if sval in spec["options"]:
                validated[key] = sval
            continue

        coerce = _TYPE_COERCE.get(spec["type"], str)
        try:
            coerced = coerce(value)
        except (TypeError, ValueError):
            continue
        if "min" in spec and "max" in spec:
            coerced = max(spec["min"], min(spec["max"], coerced))
        validated[key] = coerced
    return validated


def get_step_defaults(step_type: str) -> dict:
    """Return the default parameter values for a step type."""
    schema = STEP_PARAM_SCHEMA.get(step_type, {})
    return {k: v["default"] for k, v in schema.items()}


# ---------------------------------------------------------------------------
# ffmpeg filter builders (one per atomic step type)
# ---------------------------------------------------------------------------

_FFMPEG_NATIVE_STEPS = frozenset({"bandpass", "denoise", "silence_remove", "normalize", "pad"})


def _ffmpeg_filters_for_step(step: PipelineStep) -> list[str]:
    """Return the ffmpeg -af filter strings for a single atomic step."""
    p = {**get_step_defaults(step.step_type), **step.params}

    if step.step_type == "bandpass":
        return [
            f"highpass=f={p['highpass_freq']}",
            f"lowpass=f={p['lowpass_freq']}",
        ]
    if step.step_type == "denoise":
        return [f"afftdn=nf={p['noise_floor_db']}"]
    if step.step_type == "silence_remove":
        return [
            f"silenceremove=stop_periods=-1"
            f":stop_duration={p['stop_duration']}"
            f":stop_threshold={p['threshold_db']}dB"
            f":leave_silence={p['leave_silence']}"
        ]
    if step.step_type == "normalize":
        return [f"dynaudnorm=p={p['peak']}:s={p['smoothing']}"]
    if step.step_type == "pad":
        before_samples = int(float(p["pad_before"]) * 16000)
        after_samples = int(float(p["pad_after"]) * 16000)
        return [f"apad=pad_len={after_samples}", f"adelay={before_samples}S|{before_samples}S"]
    return []


# ---------------------------------------------------------------------------
# Individual step executors (for non-ffmpeg steps)
# ---------------------------------------------------------------------------


def _run_ffmpeg_filter_chain(
    input_path: Path, output_path: Path, filters: list[str]
) -> bool:
    """Execute an ffmpeg command with the given audio filters."""
    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-af", ",".join(filters),
        "-ac", "1", "-ar", "16000", "-sample_fmt", "s16",
        "-f", "wav", str(output_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            logger.error("ffmpeg pipeline filter failed: %s", result.stderr[:500])
            return False
        return True
    except Exception as e:
        logger.error("ffmpeg pipeline error: %s", e)
        return False


def _run_maxine_step(input_path: Path, output_path: Path, params: dict) -> bool:
    """Execute the Maxine preprocessing step."""
    from .transcribe import preprocess_audio_maxine
    return preprocess_audio_maxine(input_path, output_path, **params)


def _run_sox_step(input_path: Path, output_path: Path, params: dict) -> bool:
    """Execute the Sox noisered preprocessing step."""
    from .transcribe import preprocess_audio_sox
    return preprocess_audio_sox(input_path, output_path, **params)


def _convert_to_wav(input_path: Path, output_path: Path) -> bool:
    """Convert audio to mono 16kHz 16-bit WAV."""
    from .transcribe import _default_wav_convert
    return _default_wav_convert(input_path, output_path)


# ---------------------------------------------------------------------------
# Pipeline executor
# ---------------------------------------------------------------------------


class PipelineExecutor:
    """Execute a PipelineDefinition against an audio file.

    Merges consecutive ffmpeg-native steps into a single filter chain call
    for efficiency.  Non-ffmpeg steps (maxine, sox) break the chain and
    run as separate subprocess calls.
    """

    def run(
        self,
        input_path: Path,
        pipeline: PipelineDefinition,
        output_path: Path,
    ) -> bool:
        if not pipeline.steps:
            return _convert_to_wav(input_path, output_path)

        groups = self._group_steps(pipeline.steps)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            current_input = input_path
            needs_initial_convert = True

            for i, group in enumerate(groups):
                is_last = i == len(groups) - 1
                group_output = output_path if is_last else tmpdir_path / f"stage_{i}.wav"

                if group["kind"] == "ffmpeg":
                    filters: list[str] = []
                    for step in group["steps"]:
                        filters.extend(_ffmpeg_filters_for_step(step))
                    if not filters:
                        if is_last:
                            if current_input != output_path:
                                return _convert_to_wav(current_input, output_path)
                            return True
                        continue

                    if not _run_ffmpeg_filter_chain(current_input, group_output, filters):
                        return False
                    needs_initial_convert = False

                elif group["kind"] == "maxine":
                    step = group["steps"][0]
                    p = {**get_step_defaults("maxine"), **step.params}
                    validated = validate_step_params("maxine", p)
                    if needs_initial_convert:
                        wav_input = tmpdir_path / "pre_maxine.wav"
                        if not _convert_to_wav(current_input, wav_input):
                            return False
                        current_input = wav_input
                        needs_initial_convert = False
                    if not _run_maxine_step(current_input, group_output, validated):
                        return False

                elif group["kind"] == "sox":
                    step = group["steps"][0]
                    p = {**get_step_defaults("sox_noisered"), **step.params}
                    validated = validate_step_params("sox_noisered", p)
                    if not _run_sox_step(current_input, group_output, validated):
                        return False
                    needs_initial_convert = False

                current_input = group_output

            if needs_initial_convert and current_input == input_path:
                return _convert_to_wav(input_path, output_path)

        return True

    @staticmethod
    def _group_steps(steps: list[PipelineStep]) -> list[dict]:
        """Group consecutive ffmpeg-native steps together.

        Non-ffmpeg steps (maxine, sox_noisered) each form their own group.
        """
        groups: list[dict] = []
        ffmpeg_buf: list[PipelineStep] = []

        def flush_ffmpeg():
            if ffmpeg_buf:
                groups.append({"kind": "ffmpeg", "steps": list(ffmpeg_buf)})
                ffmpeg_buf.clear()

        for step in steps:
            if step.step_type in _FFMPEG_NATIVE_STEPS:
                ffmpeg_buf.append(step)
            elif step.step_type == "maxine":
                flush_ffmpeg()
                groups.append({"kind": "maxine", "steps": [step]})
            elif step.step_type == "sox_noisered":
                flush_ffmpeg()
                groups.append({"kind": "sox", "steps": [step]})
            else:
                logger.warning("Unknown pipeline step type: %s", step.step_type)

        flush_ffmpeg()
        return groups


# ---------------------------------------------------------------------------
# Legacy compatibility
# ---------------------------------------------------------------------------


def pipeline_from_legacy(preprocess_value: str) -> PipelineDefinition:
    """Convert a legacy AudioPreprocess enum value to a PipelineDefinition."""
    if preprocess_value == "none":
        return PipelineDefinition(name="none", steps=[])

    if preprocess_value == "ffmpeg":
        return PipelineDefinition(
            name="ffmpeg",
            steps=[
                PipelineStep("bandpass", {"highpass_freq": 300, "lowpass_freq": 3400}),
                PipelineStep("denoise", {"noise_floor_db": -25}),
                PipelineStep("normalize", {"peak": 0.9, "smoothing": 5}),
            ],
        )

    if preprocess_value == "ffmpeg_vad":
        return PipelineDefinition(
            name="ffmpeg-vad",
            steps=[
                PipelineStep("bandpass", {"highpass_freq": 300, "lowpass_freq": 3400}),
                PipelineStep("denoise", {"noise_floor_db": -20}),
                PipelineStep("silence_remove", {
                    "stop_duration": 0.3,
                    "threshold_db": -30,
                    "leave_silence": 0.1,
                }),
                PipelineStep("normalize", {"peak": 0.9, "smoothing": 3}),
            ],
        )

    if preprocess_value == "sox":
        return PipelineDefinition(
            name="sox",
            steps=[
                PipelineStep("sox_noisered", {
                    "noise_sample_duration": 0.5,
                    "noise_reduction": 0.21,
                    "highpass_freq": 300,
                    "lowpass_freq": 3400,
                }),
            ],
        )

    if preprocess_value == "maxine":
        return PipelineDefinition(
            name="maxine",
            steps=[
                PipelineStep("maxine", {
                    "effect": "denoiser",
                    "effect_version": 1,
                    "intensity_ratio": 1.0,
                    "enable_vad": False,
                }),
            ],
        )

    return PipelineDefinition(name="none", steps=[])


# ---------------------------------------------------------------------------
# Built-in presets
# ---------------------------------------------------------------------------

BUILTIN_PRESETS: dict[str, PipelineDefinition] = {
    "none": pipeline_from_legacy("none"),
    "ffmpeg": pipeline_from_legacy("ffmpeg"),
    "ffmpeg-vad": pipeline_from_legacy("ffmpeg_vad"),
    "sox": pipeline_from_legacy("sox"),
    "maxine-v1": pipeline_from_legacy("maxine"),
    "maxine-v2": PipelineDefinition(
        name="maxine-v2",
        steps=[
            PipelineStep("maxine", {
                "effect": "denoiser",
                "effect_version": 2,
                "intensity_ratio": 1.0,
                "enable_vad": False,
            }),
        ],
    ),
    "maxine-v2-vad": PipelineDefinition(
        name="maxine-v2-vad",
        steps=[
            PipelineStep("maxine", {
                "effect": "denoiser",
                "effect_version": 2,
                "intensity_ratio": 1.0,
                "enable_vad": True,
            }),
        ],
    ),
}


# ---------------------------------------------------------------------------
# Preset store (SQLite)
# ---------------------------------------------------------------------------


class PipelinePresetStore:
    """SQLite-backed storage for pipeline presets."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        conn = self._conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pipeline_presets (
                    name       TEXT PRIMARY KEY,
                    definition TEXT NOT NULL,
                    is_builtin INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.commit()
            self._seed_builtins(conn)
        finally:
            conn.close()

    def _seed_builtins(self, conn: sqlite3.Connection) -> None:
        now = datetime.now(timezone.utc).isoformat()
        for name, pipeline in BUILTIN_PRESETS.items():
            conn.execute(
                """INSERT OR IGNORE INTO pipeline_presets
                   (name, definition, is_builtin, created_at, updated_at)
                   VALUES (?, ?, 1, ?, ?)""",
                (name, json.dumps(pipeline.to_dict()), now, now),
            )
        conn.commit()

    def save(self, name: str, pipeline: PipelineDefinition) -> None:
        now = datetime.now(timezone.utc).isoformat()
        conn = self._conn()
        try:
            conn.execute(
                """INSERT INTO pipeline_presets
                   (name, definition, is_builtin, created_at, updated_at)
                   VALUES (?, ?, 0, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     definition = excluded.definition,
                     updated_at = excluded.updated_at
                   WHERE is_builtin = 0""",
                (name, json.dumps(pipeline.to_dict()), now, now),
            )
            conn.commit()
        finally:
            conn.close()

    def load(self, name: str) -> Optional[PipelineDefinition]:
        if name in BUILTIN_PRESETS:
            return BUILTIN_PRESETS[name]
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT definition FROM pipeline_presets WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                return None
            return PipelineDefinition.from_dict(json.loads(row["definition"]))
        finally:
            conn.close()

    def list_all(self) -> list[dict]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT name, is_builtin, created_at, updated_at FROM pipeline_presets ORDER BY name"
            ).fetchall()
            results = []
            for row in rows:
                results.append({
                    "name": row["name"],
                    "is_builtin": bool(row["is_builtin"]),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                })
            return results
        finally:
            conn.close()

    def delete(self, name: str) -> bool:
        conn = self._conn()
        try:
            cursor = conn.execute(
                "DELETE FROM pipeline_presets WHERE name = ? AND is_builtin = 0",
                (name,),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def load_full(self, name: str) -> Optional[dict]:
        """Load preset with full metadata including graph_json."""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT definition, is_builtin, created_at, updated_at "
                "FROM pipeline_presets WHERE name = ?",
                (name,),
            ).fetchone()
            if row is None:
                return None
            defn = json.loads(row["definition"])
            return {
                "name": name,
                "is_builtin": bool(row["is_builtin"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "definition": defn,
            }
        finally:
            conn.close()
