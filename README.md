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

#### Output

Transcripts are saved as JSON files alongside the audio files:

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
