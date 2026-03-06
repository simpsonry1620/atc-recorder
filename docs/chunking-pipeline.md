# Chunking Pipeline

The chunking pipeline splits raw ATC audio recordings into short (2–15 second)
single-transmission WAV segments suitable for ASR fine-tuning.  Each chunk is
traceable back to its source recording via a SQLite metadata store.

**Source code:** `src/atc_recorder/chunker.py`, `src/atc_recorder/labeling.py`, `src/atc_recorder/trimmer.py`, `src/atc_recorder/dashboard.py`

## Pipeline Overview

```
 Raw audio (.mp3)
       │
       ▼
 ┌─────────────────┐
 │  Preprocessing   │  Optional pipeline preset (noise reduction, etc.)
 └────────┬────────┘
          ▼
 ┌─────────────────┐
 │ WAV Conversion   │  ffmpeg → 16 kHz mono 16-bit PCM
 └────────┬────────┘
          ▼
 ┌─────────────────┐
 │  Speech Detection│  Energy-based VAD (30 ms frames)
 └────────┬────────┘
          ▼
 ┌─────────────────┐
 │ Merge & Filter   │  Merge nearby segments, drop short ones
 └────────┬────────┘
          ▼
 ┌─────────────────┐
 │ Duration Filter   │  Keep only 2–15 s segments
 └────────┬────────┘
          ▼
 ┌─────────────────┐
 │ Extract + Pad    │  Cut WAV with 0.5 s ambient padding
 └────────┬────────┘
          ▼
 ┌─────────────────┐
 │ Metadata Store   │  SQLite ChunkStore (chunk ID, source, offset, …)
 └────────┬────────┘
          ▼
   Training-ready chunks (.wav)
```

## Step-by-Step

### 1. Preprocessing (optional)

If a pipeline preset is provided (via CLI `--preprocess` or the dashboard UI),
the raw audio is run through a `PipelineExecutor` first.  Presets can chain
arbitrary ffmpeg filter steps such as noise gating, high-pass filtering, or
NVIDIA Maxine audio effects.

When no preset is specified the file passes straight to WAV conversion.

### 2. WAV Conversion

The source file is converted to a standardized format using ffmpeg:

| Property    | Value   |
|-------------|---------|
| Sample rate | 16 kHz  |
| Channels    | Mono    |
| Bit depth   | 16-bit  |
| Format      | WAV     |

This normalization ensures consistent downstream processing regardless of
the original recording format.

### 3. Speech Detection (Energy-Based VAD)

`detect_speech_segments()` implements a lightweight Voice Activity Detector
based on per-frame RMS energy — no ML model required.

**Frame analysis:** The audio is divided into 30 ms frames and the RMS energy
of each frame is computed:

```
RMS = sqrt( mean( sample² ) )
```

**Classification:** A frame is classified as *speech* when its RMS energy
exceeds `energy_threshold` (default 500.0).

**Segment identification:** Contiguous runs of speech frames are grouped into
`(start_sec, end_sec)` pairs.

### 4. Segment Post-Processing

Three successive refinements are applied to the raw speech segments:

1. **Merge** — Segments separated by less than `merge_gap_sec` (default 0.3 s)
   are merged into a single segment.  This prevents brief pauses within a
   single radio transmission from splitting it into multiple chunks.

2. **Minimum duration filter** — Segments shorter than `min_speech_sec`
   (0.3 s, hardcoded) are discarded as likely noise or mic clicks.

3. **Boundary expansion** — Each segment is expanded by half of
   `min_silence_sec` (default 0.2 s per side) to capture onset/offset
   transients.  Overlapping expanded segments are merged.

### 5. Duration Filtering

After VAD, only segments within the configured duration window are kept:

- **Minimum:** `min_chunk_duration` (default 2.0 s) — shorter chunks are
  unlikely to contain a meaningful utterance.
- **Maximum:** `max_chunk_duration` (default 15.0 s) — longer segments may
  span multiple transmissions or contain extended noise.

### 6. Chunk Extraction with Padding

Each surviving segment is extracted from the WAV with `pad_seconds`
(default 0.5 s) of ambient audio on each side.  The padding gives ASR models
a natural silence context around each utterance.

Extracted chunks shorter than 1 second of audio data (after padding) are
discarded as a final safety check.

### 7. Metadata Storage

Every chunk is recorded in a SQLite database (`ChunkStore`) with the
following fields:

| Field             | Description                                        |
|-------------------|----------------------------------------------------|
| `chunk_id`        | Deterministic SHA-1 hash of source filename + offset |
| `source_file`     | Original audio filename                            |
| `feed_id`         | ATC feed identifier (e.g. `kdca1_gnd`)             |
| `date`            | Recording date                                     |
| `offset_seconds`  | Start time within the source file                  |
| `duration_seconds` | Actual chunk duration                             |
| `output_path`     | Path to the chunk WAV file                         |
| `created_at`      | UTC timestamp                                      |

Chunk IDs are deterministic: re-running chunking on the same file with the
same parameters produces identical IDs, making the process idempotent.

### Output Structure

Chunks are organized by feed and date:

```
recordings/chunks/
└── kdca1_gnd/
    └── 2026-02-15/
        ├── kdca1_gnd_2026-02-15_1200Z_chunk_12.3s.wav
        ├── kdca1_gnd_2026-02-15_1200Z_chunk_45.7s.wav
        └── ...
```

## Configuration

All chunking parameters are configurable through `TrainingConfig` in
`config.yaml`, CLI flags, or the dashboard UI:

| Parameter          | Config key            | CLI flag             | Default | Description                          |
|--------------------|-----------------------|----------------------|---------|--------------------------------------|
| Min duration       | `min_chunk_duration`  | `--min-duration`     | 2.0 s   | Shortest chunk to keep               |
| Max duration       | `max_chunk_duration`  | `--max-duration`     | 15.0 s  | Longest chunk to keep                |
| Padding            | `pad_seconds`         | `--pad`              | 0.5 s   | Ambient padding on each side         |
| Energy threshold   | `energy_threshold`    | `--energy-threshold` | 500.0   | RMS energy to classify speech        |
| Min silence        | —                     | —                    | 0.4 s   | Silence duration for boundary detect |
| Merge gap          | —                     | —                    | 0.3 s   | Max gap before merging segments      |
| Pipeline preset    | —                     | `--preprocess`       | None    | Preprocessing pipeline to apply      |

## Downstream: Labeling Pipeline

After chunking, segments enter the **labeling pipeline** for quality-filtered
pseudo-labeling:

### Dual-ASR Transcription

Each chunk is transcribed by two independent ASR models:

- **Whisper** (`whisper-large-v3`)
- **Parakeet** (`parakeet-tdt-0.6b-v2`)

### CER-Based Filtering

The Character Error Rate (CER) between the two transcripts is computed.
When the models agree closely, the transcript is likely correct:

| CER         | Action                                     |
|-------------|--------------------------------------------|
| < 0.05      | **Accepted** — Whisper text used as consensus |
| ≥ 0.05      | **Rejected** — flagged for manual review     |

The CER threshold is configurable (`max_cer`, default 0.05).

### Chunk Statuses

Each labeled chunk progresses through one of five states:

| Status     | Meaning                                      |
|------------|----------------------------------------------|
| `pending`  | Labeled but not yet filtered                 |
| `accepted` | CER below threshold, auto-accepted           |
| `rejected` | CER above threshold, auto-rejected           |
| `verified` | Manually reviewed and confirmed via dashboard |
| `needs_retrim` | Audio needs manual boundary adjustment before final acceptance |

### Manual Verification

The web dashboard provides a UI for reviewing chunks:
- Play audio
- Compare Whisper vs. Parakeet transcripts
- Accept, reject, or correct transcripts
- Mark difficult chunks as `needs_retrim`
- Open a waveform-based manual trim panel and apply new boundaries
- Batch operations by CER range

Verified and accepted chunks are exported for ASR fine-tuning manifest
generation.

### Post-Label Audio Trimming

**Source code:** `src/atc_recorder/trimmer.py`

After labeling, chunks often contain leading or trailing audio from
adjacent ATC transmissions ("dialog bleed"). This occurs because:

1. VAD boundary expansion adds 0.2 s per side
2. Ambient padding adds 0.5 s per side during extraction
3. Back-to-back transmissions on the shared radio channel may be merged

The default **trim step** is local and VAD-based. It re-runs energy-based
speech detection on each labeled WAV chunk, finds the first and last speech
regions inside the chunk, adds small onset/offset padding, archives the
original WAV, and overwrites the chunk with the trimmed audio.

**Workflow:**

1. Run energy-based VAD on the chunk to find internal speech bounds.
2. Set trim boundaries to `speech_start - onset_pad` and
   `speech_end + offset_pad`.
3. Skip chunks that have no detected speech, would become too short, or
   would remove less than 50 ms of audio.
4. Archive the original WAV to `recordings/chunks_archive/`.
5. Overwrite the chunk WAV with the trimmed audio.
6. Record trim metadata in the label database.

If a chunk still needs cleanup after auto-trim, the dashboard review UI can
mark it as `needs_retrim` and open a **Manual Trim** waveform panel. Manual
trim uses user-selected boundaries instead of VAD and always trims from the
archived original when one exists, so retrimming remains reversible.

**Archiving:** Original WAVs are copied to `chunks_archive/` before
modification, preserving the same `{feed_id}/{date}/` directory
structure.  This makes trimming fully reversible.

| Parameter             | CLI flag                 | Default | Description                           |
|-----------------------|--------------------------|---------|---------------------------------------|
| Onset padding         | `--onset-pad`            | 0.1 s   | Padding before first detected speech  |
| Offset padding        | `--offset-pad`           | 0.1 s   | Padding after last detected speech    |
| Min trimmed duration  | `--min-trimmed-duration` | 0.5 s   | Skip if result would be too short     |
| Status filter         | `--status`               | all     | Only trim `accepted`, `verified`, or `pending` chunks |
| Feed filter           | `--feed`                 | all     | Only trim a specific feed             |

**Database columns added to `labeled_chunks`:**

| Column               | Type | Description                            |
|----------------------|------|----------------------------------------|
| `trim_start_sec`     | REAL | Trim start within original chunk       |
| `trim_end_sec`       | REAL | Trim end within original chunk         |
| `original_duration`  | REAL | Duration before trimming               |
| `original_audio_path`| TEXT | Path to archived original WAV          |

Already-trimmed chunks (`trim_start_sec IS NOT NULL`) are skipped on
auto-trim re-runs, making the process idempotent.

## Entry Points

| Method    | Command / Endpoint                         | Description                              |
|-----------|--------------------------------------------|------------------------------------------|
| CLI       | `atc-recorder training chunk <dir>`        | Chunk files in a directory               |
| CLI       | `atc-recorder training label`              | Label existing chunks with dual ASR      |
| CLI       | `atc-recorder training filter`             | Auto-accept / reject by CER threshold    |
| CLI       | `atc-recorder training trim`               | Auto-trim labeled chunks with VAD        |
| CLI       | `atc-recorder training normalize`          | Normalize verified / accepted text       |
| CLI       | `atc-recorder training manifest`           | Export NeMo training manifests           |
| Dashboard | `POST /api/labeling/start-chunking`        | Async chunking via web UI                |
| Dashboard | `POST /api/labeling/start-labeling`        | Async dual-ASR labeling via web UI       |
| Dashboard | `POST /api/labeling/start-trimming`        | Async VAD auto-trim via web UI           |
| Dashboard | `POST /api/labeling/manual-trim/{chunk_id}`| Apply waveform-based manual trim         |
| Dashboard | `POST /api/labeling/normalize`             | Normalize reviewed text in place         |
| Dashboard | `POST /api/training/manifest`              | Export train / validation manifests      |

## Design Notes

- **No overlap** between chunks — each speech segment maps to exactly one
  chunk.
- **Energy-based VAD** keeps the pipeline dependency-free (no ML model
  needed for segmentation), though it may be less accurate than ML-based
  detectors on noisy ATC audio.
- **Deterministic IDs** enable safe re-runs without duplicate data.
- **SQLite storage** for both chunk metadata and labels keeps the system
  self-contained with no external database dependency.
- **Reversible trimming** — original WAVs are archived before modification,
  and trim metadata is tracked in the database.
