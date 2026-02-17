# ATC Recorder

Record and download Air Traffic Control (ATC) audio from LiveATC.net for later transcription.

## Features

- **Live Stream Recording**: Record live ATC streams in 30-minute segments
- **Archive Download**: Download historical recordings from LiveATC archives
- **Feed Discovery**: Automatically discover available feeds for any airport
- **Organized Output**: Files organized by feed and date with metadata
- **Speech-to-Text**: Automatic transcription using NVIDIA Whisper ASR (GPU required)

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

Create a `config.yaml` file:

```yaml
output_dir: ./recordings
segment_duration: 1800  # 30 minutes in seconds

feeds:
  - kdca1_gnd
  - kdca2_twr
  - kdca1_app_final

recording:
  enabled: true
  reconnect_delay: 30
  max_retries: 5

transcription:
  segment_by_pauses: false
  min_silence_duration: 0.5
  silence_threshold_dB: -30
  min_speech_duration: 0.3
  merge_gap_seconds: 0.5
  output_format: json  # json | timestamped-txt | srt
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

The project includes NVIDIA Whisper ASR integration for automatic transcription of recordings.

#### Requirements

- NVIDIA GPU with CUDA support
- [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)
- NGC API Key from [NVIDIA Build](https://build.nvidia.com/openai/whisper-large-v3)
- `sox` (optional, for `transcribe-compare` sox preprocessing method)

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

4. Start the ASR services:
   ```bash
   docker compose --profile asr up -d
   ```

   Note: First startup may take up to 30 minutes while the model downloads.

5. Check if the service is ready:
   ```bash
   curl http://localhost:9000/v1/health/ready
   ```

#### Transcribe a Single File

```bash
# Transcribe an audio file
docker compose run --rm atc-recorder transcribe recordings/kdca1_gnd/2026-02-04/kdca1_gnd_2026-02-04_1200Z.mp3

# Segment by pauses (one segment per utterance) and write timestamped .txt
docker compose run --rm atc-recorder transcribe --segment-by-pauses --output-format timestamped-txt recordings/.../file.mp3

# Also export SRT subtitles
docker compose run --rm atc-recorder transcribe --segment-by-pauses --output-format srt recordings/.../file.mp3

# Check connection to Whisper service
docker compose run --rm atc-recorder transcribe --check recordings/any-file.mp3
```

#### Automatic Transcription

The transcription worker automatically transcribes new recordings as they're created:

```bash
# Start ASR services (includes transcription worker)
docker compose --profile asr up -d

# View transcription worker logs
docker compose logs -f transcription-worker
```

The worker reads optional `transcription` settings from `config.yaml` (pause segmentation thresholds and output format).

#### Transcribe Existing Recordings

Use the batch command to process files already on disk:

```bash
# Transcribe only files that do not have JSON transcripts yet
docker compose run --rm atc-recorder transcribe-all

# Show what would be transcribed, without running ASR
docker compose run --rm atc-recorder transcribe-all --dry-run

# Re-transcribe all audio files, including ones with existing transcripts
docker compose run --rm atc-recorder transcribe-all --force

# Batch mode with pause segmentation and SRT export
docker compose run --rm atc-recorder transcribe-all --segment-by-pauses --output-format srt
```

#### Compare Audio Preprocessing Methods

To evaluate transcript quality with different preprocessing chains:

```bash
docker compose run --rm atc-recorder transcribe-compare recordings/.../file.mp3
```

This creates multiple transcript files (for example `*_transcript_none.json`, `*_transcript_ffmpeg.json`, and `*_transcript_ffmpeg_vad.json`; `*_transcript_sox.json` is included when `sox` is available) so you can compare recognition quality.

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
    endpoint: http://embedding-nim:8000/v1/embeddings
    model: nvidia/llama-3_2-nv-embedqa-1b-v2
    api_key_env: NVIDIA_API_KEY
  vector_store:
    provider: milvus
    host: milvus-standalone
    port: 19530
    collection_name: atc_transcripts
    embedding_dim: 2048
```

#### Start RAG services

```bash
# Start Milvus + API (rag profile)
docker compose --profile rag up -d milvus-etcd milvus-minio milvus-standalone rag-api

# Backfill existing transcript JSON files
docker compose run --rm atc-recorder ingest-transcripts

# Validate embedding + vector connectivity
docker compose run --rm atc-recorder rag-check
```

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
- `NGC_API_KEY`: NVIDIA NGC API key (required for ASR features)
- `WHISPER_GRPC_HOST`: Whisper gRPC host (default: whisper-asr)
- `WHISPER_GRPC_PORT`: Whisper gRPC port (default: 50051)

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
