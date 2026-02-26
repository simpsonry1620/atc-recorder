"""CER/WER calculation and ASR benchmarking.

Provides Levenshtein-based error rate functions and a benchmark runner
that evaluates model accuracy against ground-truth test manifests.
"""

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .logging import get_logger

logger = get_logger(__name__)


def _levenshtein(ref: list, hyp: list) -> tuple[int, int, int, int]:
    """Compute Levenshtein edit-distance components.

    Returns (substitutions, deletions, insertions, ref_length).
    """
    n, m = len(ref), len(hyp)
    # dp[i][j] = (cost, subs, dels, ins)
    dp = [[(0, 0, 0, 0)] * (m + 1) for _ in range(n + 1)]

    for i in range(1, n + 1):
        dp[i][0] = (i, 0, i, 0)
    for j in range(1, m + 1):
        dp[0][j] = (j, 0, 0, j)

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if ref[i - 1] == hyp[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                sub = dp[i - 1][j - 1]
                del_ = dp[i - 1][j]
                ins = dp[i][j - 1]

                candidates = [
                    (sub[0] + 1, sub[1] + 1, sub[2], sub[3]),
                    (del_[0] + 1, del_[1], del_[2] + 1, del_[3]),
                    (ins[0] + 1, ins[1], ins[2], ins[3] + 1),
                ]
                dp[i][j] = min(candidates, key=lambda x: x[0])

    _, s, d, i = dp[n][m]
    return s, d, i, n


def character_error_rate(reference: str, hypothesis: str) -> float:
    """Compute Character Error Rate between reference and hypothesis.

    Returns a float in [0, inf) where 0 means perfect match.
    Returns 0.0 if both strings are empty, 1.0 if reference is empty
    but hypothesis is not.
    """
    ref_chars = list(reference.strip().lower())
    hyp_chars = list(hypothesis.strip().lower())

    if not ref_chars and not hyp_chars:
        return 0.0
    if not ref_chars:
        return 1.0

    s, d, i, n = _levenshtein(ref_chars, hyp_chars)
    return (s + d + i) / n


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Compute Word Error Rate between reference and hypothesis.

    Standard WER = (S + D + I) / N where N is the reference word count.
    """
    ref_words = reference.strip().lower().split()
    hyp_words = hypothesis.strip().lower().split()

    if not ref_words and not hyp_words:
        return 0.0
    if not ref_words:
        return 1.0

    s, d, i, n = _levenshtein(ref_words, hyp_words)
    return (s + d + i) / n


@dataclass
class FileResult:
    """Benchmark result for a single audio file."""

    audio_filepath: str
    reference_text: str
    hypothesis_text: str
    wer: float
    cer: float
    duration: float
    inference_time: float = 0.0


@dataclass
class BenchmarkReport:
    """Aggregate benchmark results."""

    model: str
    test_manifest: str
    total_files: int
    aggregate_wer: float
    aggregate_cer: float
    total_duration_sec: float
    total_inference_sec: float
    file_results: list[FileResult] = field(default_factory=list)
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "test_manifest": self.test_manifest,
            "total_files": self.total_files,
            "aggregate_wer": round(self.aggregate_wer, 4),
            "aggregate_cer": round(self.aggregate_cer, 4),
            "total_duration_sec": round(self.total_duration_sec, 2),
            "total_inference_sec": round(self.total_inference_sec, 2),
            "rtf": round(
                self.total_inference_sec / self.total_duration_sec, 3
            ) if self.total_duration_sec > 0 else 0,
            "created_at": self.created_at,
            "files": [
                {
                    "audio": r.audio_filepath,
                    "wer": round(r.wer, 4),
                    "cer": round(r.cer, 4),
                    "duration": round(r.duration, 2),
                    "ref": r.reference_text[:200],
                    "hyp": r.hypothesis_text[:200],
                }
                for r in self.file_results
            ],
        }


class BenchmarkStore:
    """SQLite-backed storage for benchmark results."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_schema(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS benchmark_runs (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    model           TEXT NOT NULL,
                    test_manifest   TEXT NOT NULL,
                    total_files     INTEGER NOT NULL,
                    aggregate_wer   REAL NOT NULL,
                    aggregate_cer   REAL NOT NULL,
                    total_duration  REAL NOT NULL,
                    total_inference REAL NOT NULL,
                    report_json     TEXT NOT NULL,
                    created_at      TEXT NOT NULL
                )
            """)
            conn.commit()

    def save_report(self, report: BenchmarkReport) -> int:
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT INTO benchmark_runs
                   (model, test_manifest, total_files, aggregate_wer,
                    aggregate_cer, total_duration, total_inference,
                    report_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    report.model,
                    report.test_manifest,
                    report.total_files,
                    report.aggregate_wer,
                    report.aggregate_cer,
                    report.total_duration_sec,
                    report.total_inference_sec,
                    json.dumps(report.to_dict()),
                    report.created_at,
                ),
            )
            conn.commit()
            return cursor.lastrowid or 0

    def list_runs(self, model: Optional[str] = None, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            if model:
                rows = conn.execute(
                    "SELECT id, model, total_files, aggregate_wer, aggregate_cer, "
                    "created_at FROM benchmark_runs WHERE model = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (model, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, model, total_files, aggregate_wer, aggregate_cer, "
                    "created_at FROM benchmark_runs "
                    "ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]

    def get_report(self, run_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT report_json FROM benchmark_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["report_json"])


def run_benchmark(
    test_manifest_path: Path,
    transcribe_fn: callable,
    model_name: str = "unknown",
    progress_callback: Optional[callable] = None,
) -> BenchmarkReport:
    """Run a benchmark against a NeMo-format test manifest.

    Args:
        test_manifest_path: Path to a JSONL manifest with audio_filepath, text, duration.
        transcribe_fn: Callable(audio_path) -> str that returns hypothesis text.
        model_name: Name of the model being benchmarked.
        progress_callback: Optional callback(file_idx, total, file_result).
    """
    entries = []
    with open(test_manifest_path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))

    file_results: list[FileResult] = []
    total_wer_num = 0.0
    total_wer_den = 0
    total_cer_num = 0.0
    total_cer_den = 0
    total_dur = 0.0
    total_inf = 0.0

    for idx, entry in enumerate(entries):
        audio_path = entry["audio_filepath"]
        ref_text = entry.get("text", "")
        duration = entry.get("duration", 0.0)

        t0 = time.monotonic()
        try:
            hyp_text = transcribe_fn(Path(audio_path))
        except Exception as exc:
            logger.error("Transcription failed for %s: %s", audio_path, exc)
            hyp_text = ""
        inf_time = time.monotonic() - t0

        wer = word_error_rate(ref_text, hyp_text)
        cer = character_error_rate(ref_text, hyp_text)

        ref_words = ref_text.strip().lower().split()
        ref_chars = list(ref_text.strip().lower())

        s_w, d_w, i_w, n_w = _levenshtein(ref_words, hyp_text.strip().lower().split())
        s_c, d_c, i_c, n_c = _levenshtein(ref_chars, list(hyp_text.strip().lower()))

        total_wer_num += s_w + d_w + i_w
        total_wer_den += n_w
        total_cer_num += s_c + d_c + i_c
        total_cer_den += n_c
        total_dur += duration
        total_inf += inf_time

        fr = FileResult(
            audio_filepath=audio_path,
            reference_text=ref_text,
            hypothesis_text=hyp_text,
            wer=wer,
            cer=cer,
            duration=duration,
            inference_time=inf_time,
        )
        file_results.append(fr)

        if progress_callback:
            progress_callback(idx, len(entries), fr)

    agg_wer = total_wer_num / total_wer_den if total_wer_den > 0 else 0.0
    agg_cer = total_cer_num / total_cer_den if total_cer_den > 0 else 0.0

    return BenchmarkReport(
        model=model_name,
        test_manifest=str(test_manifest_path),
        total_files=len(entries),
        aggregate_wer=agg_wer,
        aggregate_cer=agg_cer,
        total_duration_sec=total_dur,
        total_inference_sec=total_inf,
        file_results=file_results,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
