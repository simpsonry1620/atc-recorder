# ATC Recorder

Record and download Air Traffic Control (ATC) audio from LiveATC.net for later transcription.

## Features

- **Live Stream Recording**: Record live ATC streams in 30-minute segments
- **Archive Download**: Download historical recordings from LiveATC archives
- **Feed Discovery**: Automatically discover available feeds for any airport
- **Organized Output**: Files organized by feed and date with metadata
- **Speech-to-Text**: Automatic transcription using NVIDIA Whisper ASR (GPU required)
- **GUI ASR Trigger**: Run ASR from the dashboard with model and preprocessing selection

## Requirements

- Python 3.10+
- ffmpeg (for live stream recording)

### Installing ffmpeg

```bash
# Ubuntu/Debian
sudo apt install ffmpeg

# macOS
brew install ffmpeg

# Windows (with chocolatey)
choco install ffmpeg
```

## Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/atc-recorder.git
cd atc-recorder

# Install in development mode
pip install -e .

# Or install dependencies directly
pip install -r requirements.txt
```

## Usage

### List Available Feeds

```bash
# List all feeds for an airport
atc-recorder feeds list kdca

# Show detailed feed information
atc-recorder feeds list kdca --verbose
```

### Record Live Streams

```bash
# Record a single feed for 30 minutes
atc-recorder record kdca1_gnd --duration 30m

# Record for 2 hours
atc-recorder record kdca2_twr --duration 2h

# Record all feeds for an airport
atc-recorder record-all kdca --duration 1h
```

### Download Archives

```bash
# Download archives for a specific date
atc-recorder download kdca1_gnd --date 2026-02-01

# Download a range of hours
atc-recorder download kdca1_gnd --date 2026-02-01 --start-hour 12 --hours 6
```

### Run as Daemon

```bash
# Run continuous recording with config file
atc-recorder daemon --config config.yaml
```

### System Check

```bash
# Verify ffmpeg, network connectivity, and output directory
atc-recorder check
```

## Configuration

`config.yaml` is optional. If it is not present, the app uses built-in defaults.

Configuration precedence (highest to lowest):
1. CLI `--config` path (for commands that accept it)
2. `config.yaml` in the current working directory
3. Built-in defaults
4. Environment variables for specific runtime overrides (for example gRPC hosts/ports and API keys)

Create a `config.yaml` file:

```yaml
output_dir: ./recordings
segment_duration: 1800  # 30 minutes in seconds
request_delay: 1.0
user_agent: ATC-Recorder/0.1.0

feeds:
  - kdca1_gnd
  - kdca2_twr
  - kdca1_app_final

recording:
  enabled: true
  reconnect_delay: 30
  max_retries: 5

transcription:
  preprocess: ffmpeg_vad
  preprocess_output_dir: ./recordings/preprocessed
  keep_preprocessed_audio: false
  segment_by_pauses: false
  min_silence_duration: 0.5
  silence_threshold_dB: -30
  min_speech_duration: 0.3
  merge_gap_seconds: 0.5
  output_format: json  # json | timestamped-txt | srt
  diarization_enabled: false
  diarization_mode: role-heuristic
  stitch_across_files: false
  stitch_max_gap_seconds: 2.0
  stitch_min_text_overlap_chars: 12
  variant_store_path: ./recordings/transcripts.db

rag:
  enabled: true
  ingest_on_transcribe: true
  embedding:
    endpoint: http://embedding-nim:8000/v1/embeddings
    api_key_env: NGC_API_KEY

tracking:
  entity_extraction:
    enabled: true
    min_confidence: 0.5
```

## Output Structure

```
recordings/
└── kdca1_gnd/
    └── 2026-02-03/
        ├── kdca1_gnd_2026-02-03_1200Z.mp3
        ├── kdca1_gnd_2026-02-03_1230Z.mp3
        └── metadata.json
```

## Docker

The application is fully containerized with health checks, logging, and multiple service profiles.

### Run Tests (Container)

```bash
docker compose run --rm --entrypoint sh -v "$(pwd):/app" atc-recorder -lc \
  "cd /app && python -m pip install --no-cache-dir pytest pytest-cov && python -m pytest -q -o cache_dir=/tmp/pytest-cache"
```

### Quick Start

```bash
# Build the image
docker compose build

# List available feeds
docker compose run --rm atc-recorder feeds list kdca

# Record a single feed for 30 minutes
docker compose run --rm atc-recorder record kdca1_gnd --duration 30m

# Download historical archives
docker compose --profile archive run --rm atc-archiver download kdca1_gnd --date 2026-02-01
```

### One-Command Launcher

Use the interactive launcher script to avoid manually running multiple compose commands:

```bash
./scripts/launch_app.sh
```

It prompts for:
- services to start (dashboard, Whisper, Parakeet, RAG, workers)
- optional image build
- GPU index selection for Whisper/Parakeet

You can also run it non-interactively with flags:

```bash
./scripts/launch_app.sh --build yes --dashboard yes --whisper yes --parakeet no --rag no --workers yes --non-interactive
```

### Dashboard Access

The web dashboard is exposed on port `8050` by default.

Dashboard host/port are controlled by CLI arguments, not `config.yaml`:

```bash
atc-recorder dashboard --host 0.0.0.0 --port 8050
```

In Docker Compose, the dashboard service runs:

```bash
docker compose up -d atc-dashboard
```

- Local machine: `http://localhost:8050`
- External access: `http://<server-ip>:8050`

Quick health check:

```bash
curl -s "http://localhost:8050/api/status"
```

### Running as a Daemon

```bash
# Start continuous recording in background
docker compose up -d atc-daemon

# View logs
docker compose logs -f atc-daemon

# Check health status
docker compose ps

# Stop the daemon
docker compose down
```

### Scheduled Archive Downloads

```bash
# Run daily archive cron job (downloads previous day's recordings)
docker compose --profile cron up -d atc-archive-cron
```

### Automatic Speech Recognition (ASR)

The project supports two NVIDIA NIM ASR workflows for transcription:
- Whisper: `whisper-large-v3` (profile: `asr`)
- Parakeet: `parakeet-tdt-0.6b-v2` (profile: `asr-parakeet`)

#### Requirements

- NVIDIA GPU with CUDA support
- [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)
- NGC API Key from [NVIDIA Build](https://build.nvidia.com/openai/whisper-large-v3)
- `sox` (optional, for `transcribe-compare` sox preprocessing method)
- NVIDIA Maxine Audio Effects runtime (optional, for `--preprocess maxine`)

#### Setup

1. Get an NGC API Key from https://build.nvidia.com/openai/whisper-large-v3

2. Add your API key to `.env`:
   ```bash
   cp .env.example .env
   # Edit .env and set NGC_API_KEY=your-key-here
   ```

3. Login to NVIDIA Container Registry:
   ```bash
   docker login nvcr.io -u '$oauthtoken' -p $NGC_API_KEY
   ```

4. Start the Whisper ASR services:
   ```bash
   docker compose --profile asr up -d
   ```

   Note: First startup may take up to 30 minutes while the model downloads.

5. Check if Whisper is ready:
   ```bash
   curl http://localhost:9000/v1/health/ready
   ```

6. (Optional) Start the Parakeet ASR workflow:
   ```bash
   docker compose --profile asr-parakeet up -d
   ```

   Check Parakeet readiness on host port `9001`:
   ```bash
   curl http://localhost:9001/v1/health/ready
   ```

#### Transcribe a Single File

```bash
# Transcribe an audio file via Whisper backend
docker compose --profile asr run --rm transcription-worker transcribe recordings/kdca1_gnd/2026-02-04/kdca1_gnd_2026-02-04_1200Z.mp3

# Segment by pauses (one segment per utterance) and write timestamped .txt
docker compose --profile asr run --rm transcription-worker transcribe --segment-by-pauses --output-format timestamped-txt recordings/.../file.mp3

# Use NVIDIA Maxine preprocessing (falls back to ffmpeg_vad if unavailable)
docker compose --profile asr run --rm transcription-worker transcribe --preprocess maxine recordings/.../file.mp3

# Add role diarization labels and stitch boundary transmissions
docker compose --profile asr run --rm transcription-worker transcribe --segment-by-pauses --diarization --stitch-across-files recordings/.../file.mp3

# Also export SRT subtitles
docker compose --profile asr run --rm transcription-worker transcribe --segment-by-pauses --output-format srt recordings/.../file.mp3

# Check connection to Whisper service
docker compose --profile asr run --rm transcription-worker transcribe --check recordings/any-file.mp3

# Use Parakeet backend for single-file transcription
docker compose --profile asr-parakeet run --rm parakeet-transcription-worker transcribe recordings/.../file.mp3
```

#### Automatic Transcription

The transcription worker automatically transcribes new recordings as they're created:

```bash
# Start ASR services (includes transcription worker)
docker compose --profile asr up -d

# View transcription worker logs
docker compose logs -f transcription-worker

# Start Parakeet ASR services (includes dedicated worker)
docker compose --profile asr-parakeet up -d

# View Parakeet worker logs
docker compose logs -f parakeet-transcription-worker
```

If you run both Whisper and Parakeet workers, do not have both watchers transcribe the same directory at the same time unless overwriting/skip behavior is acceptable. For clean A/B testing, run one backend at a time with `transcribe-all --force`.

The worker reads optional `transcription` settings from `config.yaml` (audio preprocessing mode, pause segmentation thresholds, output format, role diarization, and cross-file stitching).

#### Transcribe Existing Recordings

Use the batch command to process files already on disk:

```bash
# Transcribe only files that do not have JSON transcripts yet (Whisper)
docker compose --profile asr run --rm transcription-worker transcribe-all

# Show what would be transcribed, without running ASR (Whisper)
docker compose --profile asr run --rm transcription-worker transcribe-all --dry-run

# Re-transcribe all audio files, including ones with existing transcripts (Whisper)
docker compose --profile asr run --rm transcription-worker transcribe-all --force

# Batch mode with pause segmentation and SRT export (Whisper)
docker compose --profile asr run --rm transcription-worker transcribe-all --segment-by-pauses --output-format srt

# Batch mode with role diarization + cross-file stitching (Whisper)
docker compose --profile asr run --rm transcription-worker transcribe-all --segment-by-pauses --diarization --stitch-across-files

# Batch re-transcription using Parakeet backend
docker compose --profile asr-parakeet run --rm parakeet-transcription-worker transcribe-all --force
```

#### Compare Audio Preprocessing Methods

To evaluate transcript quality with different preprocessing chains:

```bash
docker compose --profile asr run --rm transcription-worker transcribe-compare recordings/.../file.mp3
```

To compare Whisper vs Parakeet end-to-end, run the same file through each worker:

```bash
docker compose --profile asr run --rm transcription-worker transcribe recordings/.../file.mp3
docker compose --profile asr-parakeet run --rm parakeet-transcription-worker transcribe recordings/.../file.mp3
```

This creates multiple transcript files (for example `*_transcript_none.json`, `*_transcript_ffmpeg.json`, and `*_transcript_ffmpeg_vad.json`; `*_transcript_sox.json` is included when `sox` is available, and `*_transcript_maxine.json` is included when Maxine runtime is available) so you can compare recognition quality.

To retain preprocessed WAV artifacts, set:

```yaml
transcription:
  preprocess_output_dir: ./recordings/preprocessed
  keep_preprocessed_audio: true
```

Maxine runtime integration uses one of:
- `MAXINE_AUDIO_CMD_TEMPLATE` (recommended), for example: `my-maxine-wrapper --in {input} --out {output}`
- `MAXINE_AUDIO_CLI` (default command name: `maxine_audio_fx`)

#### Output

Transcripts are always saved as JSON files alongside the audio files:

```
recordings/
└── kdca1_gnd/
    └── 2026-02-04/
        ├── kdca1_gnd_2026-02-04_1200Z.mp3
        ├── kdca1_gnd_2026-02-04_1200Z.json  # transcript
        └── metadata.json
```

Transcript JSON format:

```json
{
  "audio_file": "kdca1_gnd_2026-02-04_1200Z.mp3",
  "language": "en-US",
  "text": "Delta 1234 cleared to land runway 19...",
  "segments": [...],
  "transcribed_at": "2026-02-04T12:35:00Z"
}
```

When `transcription.diarization_enabled: true`, each segment is enriched with:
- `speaker_role`: `ATC`, `PILOT`, or `UNKNOWN`
- `speaker_id`: role-level stable label (`spk_atc`, `spk_pilot`, `spk_unknown`)
- `speaker_confidence`: heuristic confidence score

When `transcription.stitch_across_files: true`, adjacent transcripts are stitched at file boundaries when timing and text continuity indicate the same transmission. Stitch metadata is added to boundary segments (for example `stitched_with_previous`, `stitch_next`, `source_audio_files`, and `skip_for_ingest`) while preserving backward-compatible transcript fields.

**Timestamped segments and export formats:** Use `--segment-by-pauses` to split the transcript by silence (e.g. between ATC and pilot) so each segment has `start_time` and `end_time` in seconds. This works even when the ASR service does not return word-level timings. With `--output-format timestamped-txt` or `--output-format srt`, an additional file is written alongside the JSON: a timestamped text file (`[MM:SS.mmm] - [MM:SS.mmm] text`) or SRT subtitles. You can set `transcription` options in `config.yaml` so the transcription worker uses the same behavior automatically.

### Streaming-to-RAG (Phase 1)

The project supports a Phase 1 RAG workflow aligned with NVIDIA's streaming blueprint:

- Real-time ingestion of saved transcript JSON files
- Segment-level embeddings and Milvus indexing
- Time-window and feed/channel filtering during semantic search
- Minimal HTTP search API for downstream tools

#### Enable RAG in config

Add and customize `rag` settings in `config.yaml`:

```yaml
rag:
  enabled: true
  ingest_on_transcribe: true
  embedding:
    provider: nvidia-nim
    # Docker Compose runtime (service-to-service)
    endpoint: http://embedding-nim:8000/v1/embeddings
    # Host CLI runtime alternative:
    # endpoint: http://localhost:9080/v1/embeddings
    model: nvidia/llama-3.2-nv-embedqa-1b-v2
    api_key_env: NGC_API_KEY
  vector_store:
    provider: milvus
    host: milvus-standalone
    port: 19530
    collection_name: atc_transcripts
    embedding_dim: 2048
```

#### Start RAG services

```bash
# Start Retriever + Milvus + API (rag profile)
docker compose --profile rag up -d embedding-nim milvus-etcd milvus-minio milvus-standalone rag-api

# Backfill existing transcript JSON files
docker compose run --rm atc-recorder ingest-transcripts

# Validate embedding + vector connectivity
docker compose run --rm atc-recorder rag-check
```

If you run `atc-recorder` directly on the host (outside Docker), point `rag.embedding.endpoint` to `http://localhost:9080/v1/embeddings`.

#### Local Retriever runbook

```bash
# 1) Bring up Retriever + Milvus + API
docker compose --profile rag up -d embedding-nim milvus-etcd milvus-minio milvus-standalone rag-api

# 2) Check Retriever readiness
docker compose --profile rag ps embedding-nim
docker compose --profile rag logs embedding-nim

# 3) Validate end-to-end embedding + vector store connectivity
docker compose run --rm atc-recorder rag-check

# 4) Backfill transcript JSON files into Milvus
docker compose run --rm atc-recorder ingest-transcripts

# 5) Run a semantic query
docker compose run --rm atc-recorder search "departure congestion" --feed-id kdca1_dep --top-k 5
```

Quick checks when startup fails:
- Retriever unhealthy: verify `NGC_API_KEY` is set and `embedding-nim` logs show model startup completed.
- Wrong endpoint: inside Docker use `http://embedding-nim:8000/v1/embeddings`; host CLI use `http://localhost:9080/v1/embeddings`.
- Auth mismatch: confirm `rag.embedding.api_key_env` matches the env var exported in the running process/container.
- Vector mismatch after model change: set `rag.vector_store.embedding_dim` to the Retriever model output dimension before ingesting.

#### Query by time and channel

```bash
# CLI semantic search
docker compose run --rm atc-recorder search "runway change discussion" --feed-id kdca1_twr --top-k 5

# API semantic search
curl -s http://localhost:8100/search \
  -H "Content-Type: application/json" \
  -d '{
    "query":"summarize departure congestion",
    "feed_ids":["kdca1_dep"],
    "start_time":"2026-02-13T00:00:00Z",
    "end_time":"2026-02-13T23:59:59Z",
    "top_k":10
  }'
```

When `rag.ingest_on_transcribe: true`, the transcription watcher automatically ingests each newly saved transcript into Milvus and the metadata index.

### Environment Configuration

Copy `.env.example` to `.env` to customize:

```bash
cp .env.example .env
```

Available environment variables:
- `TZ`: Timezone (default: UTC)
- `ATC_LOG_LEVEL`: Logging level - DEBUG, INFO, WARNING, ERROR (default: INFO)
- `COMPOSE_PROJECT_NAME`: Optional Docker Compose project name
- `NGC_API_KEY`: NVIDIA NGC API key (used by ASR and Retriever services)
- `WHISPER_GRPC_HOST`: Whisper gRPC host (default: whisper-asr)
- `WHISPER_GRPC_PORT`: Whisper gRPC port (default: 50051)
- `PARAKEET_GRPC_HOST`: Parakeet gRPC host (default: parakeet-asr)
- `PARAKEET_GRPC_PORT`: Parakeet gRPC port (default: 50051)
- `NIM_WHISPER_IMAGE`: Whisper container image tag
- `NIM_WHISPER_TAGS_SELECTOR`: Optional Whisper model selector
- `NIM_PARAKEET_IMAGE`: Parakeet container image tag
- `NIM_PARAKEET_TAGS_SELECTOR`: Optional Parakeet model selector
- `NIM_PARAKEET_HOST_HTTP_PORT`: Parakeet HTTP host port (default: `9001`)
- `NIM_PARAKEET_HOST_GRPC_PORT`: Parakeet gRPC host port (default: `50052`)
- `NIM_RETRIEVER_IMAGE`: Retriever container image tag
- `NIM_RETRIEVER_HTTP_PORT`: Internal Retriever HTTP port (default: `8000`)
- `NIM_RETRIEVER_HOST_PORT`: Host port mapped to Retriever HTTP port (default: `9080`)
- `NIM_RETRIEVER_TAGS_SELECTOR`: Optional Retriever model selector

`rag.embedding.api_key_env` in `config.yaml` controls which environment variable name is read for the embedding API key (default: `NGC_API_KEY`).

### Manual Docker Build

```bash
# Build manually
docker build -t atc-recorder .

# Run with volume mount
docker run -v $(pwd)/recordings:/app/recordings atc-recorder record kdca1_gnd --duration 1h

# Run system check
docker run atc-recorder check
```

## Legal Notice

This tool is for personal and research use only. LiveATC.net's terms of service prohibit use of their streams in third-party products. Please respect their terms and use this tool responsibly.

## License

MIT License
