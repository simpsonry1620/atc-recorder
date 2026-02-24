#!/usr/bin/env python3
"""Batch preprocess ATC recordings with Maxine denoiser for A/B listening.

Creates a side-by-side comparison folder:
  recordings/preprocessed/<feed>/<date>/<stem>_maxine.wav
  recordings/preprocessed/<feed>/<date>/<stem>_ffmpeg_vad.wav

Usage:
  python scripts/batch_maxine_preprocess.py [--limit N] [--all]

Defaults to 5 files per feed for quick A/B listening.
"""

import argparse
import struct
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

RECORDINGS_ROOT = Path(__file__).resolve().parent.parent / "recordings"
OUTPUT_ROOT = RECORDINGS_ROOT / "preprocessed"
MAXINE_WRAPPER = Path(__file__).resolve().parent.parent.parent / "maxine-afx" / "maxine_denoise.sh"


def convert_to_maxine_wav(input_path: Path, output_path: Path) -> bool:
    with tempfile.NamedTemporaryFile(suffix=".pcm", delete=False) as tmp:
        raw_path = Path(tmp.name)
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(input_path), "-ac", "1", "-ar", "16000", "-f", "f32le", str(raw_path)],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0:
            print(f"  ffmpeg float conv failed: {r.stderr[:200]}", file=sys.stderr)
            return False
        pcm = raw_path.read_bytes()
        with open(output_path, "wb") as out:
            data_size = len(pcm)
            out.write(b"RIFF")
            out.write(struct.pack("<I", 36 + data_size))
            out.write(b"WAVE")
            out.write(b"fmt ")
            out.write(struct.pack("<I", 16))
            out.write(struct.pack("<H", 3))
            out.write(struct.pack("<H", 1))
            out.write(struct.pack("<I", 16000))
            out.write(struct.pack("<I", 64000))
            out.write(struct.pack("<H", 4))
            out.write(struct.pack("<H", 32))
            out.write(b"data")
            out.write(struct.pack("<I", data_size))
            out.write(pcm)
        return True
    finally:
        if raw_path.exists():
            raw_path.unlink()


def run_maxine(input_wav: Path, output_wav: Path) -> bool:
    r = subprocess.run(
        [str(MAXINE_WRAPPER), "--input", str(input_wav), "--output", str(output_wav)],
        capture_output=True, text=True, timeout=600,
    )
    return r.returncode == 0 and output_wav.exists()


def run_ffmpeg_vad(input_mp3: Path, output_wav: Path) -> bool:
    filters = [
        "highpass=f=300",
        "lowpass=f=3400",
        "afftdn=nf=-20",
        "silenceremove=stop_periods=-1:stop_duration=0.3:stop_threshold=-30dB:leave_silence=0.1",
        "dynaudnorm=p=0.9:s=3",
    ]
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(input_mp3), "-af", ",".join(filters),
         "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", "-f", "wav", str(output_wav)],
        capture_output=True, text=True, timeout=300,
    )
    return r.returncode == 0


def main():
    parser = argparse.ArgumentParser(description="Batch Maxine preprocess")
    parser.add_argument("--limit", type=int, default=5, help="Files per feed (default 5, 0=all)")
    parser.add_argument("--all", action="store_true", help="Process all files")
    parser.add_argument("--feed", type=str, default=None, help="Only process this feed")
    args = parser.parse_args()

    limit = 0 if args.all else args.limit

    mp3s = sorted(RECORDINGS_ROOT.rglob("*.mp3"))
    by_feed: dict[str, list[Path]] = defaultdict(list)
    for p in mp3s:
        feed = p.parent.parent.name if p.parent.name != "recordings" else "unknown"
        by_feed[feed].append(p)

    if args.feed:
        by_feed = {k: v for k, v in by_feed.items() if k == args.feed}

    total = 0
    for feed, files in sorted(by_feed.items()):
        subset = files if limit == 0 else files[:limit]
        print(f"\n=== {feed} ({len(subset)}/{len(files)} files) ===")
        for mp3 in subset:
            rel = mp3.parent.relative_to(RECORDINGS_ROOT)
            out_dir = OUTPUT_ROOT / rel
            out_dir.mkdir(parents=True, exist_ok=True)

            stem = mp3.stem
            maxine_out = out_dir / f"{stem}_maxine.wav"
            vad_out = out_dir / f"{stem}_ffmpeg_vad.wav"

            if maxine_out.exists() and vad_out.exists():
                print(f"  [skip] {stem} (already processed)")
                total += 1
                continue

            print(f"  [{total+1}] {stem}")

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                float_wav = Path(tmp.name)

            try:
                if not maxine_out.exists():
                    if convert_to_maxine_wav(mp3, float_wav):
                        ok = run_maxine(float_wav, maxine_out)
                        print(f"       maxine: {'OK' if ok else 'FAIL'}")
                    else:
                        print(f"       maxine: SKIP (conversion failed)")

                if not vad_out.exists():
                    ok = run_ffmpeg_vad(mp3, vad_out)
                    print(f"       ffmpeg_vad: {'OK' if ok else 'FAIL'}")
            finally:
                if float_wav.exists():
                    float_wav.unlink()

            total += 1

    print(f"\nDone. Processed {total} files. Outputs in {OUTPUT_ROOT}")


if __name__ == "__main__":
    main()
