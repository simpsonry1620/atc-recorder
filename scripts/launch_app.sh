#!/usr/bin/env bash

set -euo pipefail

# Interactive launcher for ATC Recorder services.
# Supports both prompt-driven usage and CLI flags.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

build_images="ask"
start_dashboard="ask"
start_whisper="ask"
start_parakeet="ask"
start_rag="ask"
start_workers="ask"
whisper_gpu="0"
parakeet_gpu="1"
non_interactive="false"

print_help() {
  cat <<'EOF'
Usage: ./scripts/launch_app.sh [options]

Options:
  --build [yes|no]            Build images before launch
  --dashboard [yes|no]        Start dashboard service
  --whisper [yes|no]          Start Whisper ASR service/profile
  --parakeet [yes|no]         Start Parakeet ASR service/profile
  --rag [yes|no]              Start RAG services/profile
  --workers [yes|no]          Start ASR transcription workers
  --whisper-gpu <index>       GPU index for Whisper services (default: 0)
  --parakeet-gpu <index>      GPU index for Parakeet services (default: 1)
  --non-interactive           Require all choices via flags
  --help                      Show this help

Examples:
  ./scripts/launch_app.sh
  ./scripts/launch_app.sh --build yes --dashboard yes --whisper yes --parakeet no --rag no --workers yes --non-interactive
EOF
}

normalize_yes_no() {
  local v="${1,,}"
  case "${v}" in
    y|yes|true|1) echo "yes" ;;
    n|no|false|0) echo "no" ;;
    ask) echo "ask" ;;
    *)
      echo "Invalid yes/no value: ${1}" >&2
      exit 1
      ;;
  esac
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --build)
      build_images="$(normalize_yes_no "${2:-}")"
      shift 2
      ;;
    --dashboard)
      start_dashboard="$(normalize_yes_no "${2:-}")"
      shift 2
      ;;
    --whisper)
      start_whisper="$(normalize_yes_no "${2:-}")"
      shift 2
      ;;
    --parakeet)
      start_parakeet="$(normalize_yes_no "${2:-}")"
      shift 2
      ;;
    --rag)
      start_rag="$(normalize_yes_no "${2:-}")"
      shift 2
      ;;
    --workers)
      start_workers="$(normalize_yes_no "${2:-}")"
      shift 2
      ;;
    --whisper-gpu)
      whisper_gpu="${2:-}"
      shift 2
      ;;
    --parakeet-gpu)
      parakeet_gpu="${2:-}"
      shift 2
      ;;
    --non-interactive)
      non_interactive="true"
      shift
      ;;
    --help|-h)
      print_help
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      print_help
      exit 1
      ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required but not found in PATH." >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "docker compose is required but unavailable." >&2
  exit 1
fi

ask_bool() {
  local prompt="$1"
  local default="$2"
  local var="$3"

  if [[ "${!var}" == "ask" ]]; then
    if [[ "${non_interactive}" == "true" ]]; then
      echo "Missing required option in non-interactive mode: ${var}" >&2
      exit 1
    fi
    local answer
    read -r -p "${prompt} [${default}/$( [[ "${default}" == "yes" ]] && echo "no" || echo "yes" )]: " answer
    answer="${answer:-${default}}"
    printf -v "${var}" '%s' "$(normalize_yes_no "${answer}")"
  fi
}

ask_bool "Build images first?" "yes" build_images
ask_bool "Start dashboard?" "yes" start_dashboard
ask_bool "Start Whisper ASR?" "yes" start_whisper
ask_bool "Start Parakeet ASR?" "no" start_parakeet
ask_bool "Start RAG services?" "no" start_rag
ask_bool "Start transcription workers?" "yes" start_workers

if [[ "${non_interactive}" != "true" ]]; then
  if [[ "${start_whisper}" == "yes" ]]; then
    read -r -p "Whisper GPU index [${whisper_gpu}]: " input
    whisper_gpu="${input:-${whisper_gpu}}"
  fi
  if [[ "${start_parakeet}" == "yes" ]]; then
    read -r -p "Parakeet GPU index [${parakeet_gpu}]: " input
    parakeet_gpu="${input:-${parakeet_gpu}}"
  fi
fi

echo
echo "Launch plan:"
echo "  Build images:         ${build_images}"
echo "  Dashboard:            ${start_dashboard}"
echo "  Whisper ASR:          ${start_whisper} (GPU ${whisper_gpu})"
echo "  Parakeet ASR:         ${start_parakeet} (GPU ${parakeet_gpu})"
echo "  RAG services:         ${start_rag}"
echo "  Transcription workers:${start_workers}"
echo

if [[ "${non_interactive}" != "true" ]]; then
  read -r -p "Proceed? [yes/no]: " confirm
  confirm="$(normalize_yes_no "${confirm:-yes}")"
  if [[ "${confirm}" != "yes" ]]; then
    echo "Cancelled."
    exit 0
  fi
fi

if [[ "${build_images}" == "yes" ]]; then
  docker compose build atc-dashboard atc-recorder
  if [[ "${start_workers}" == "yes" || "${start_whisper}" == "yes" || "${start_parakeet}" == "yes" ]]; then
    docker compose build transcription-worker parakeet-transcription-worker
  fi
fi

if [[ "${start_dashboard}" == "yes" ]]; then
  docker compose up -d atc-dashboard
fi

if [[ "${start_whisper}" == "yes" ]]; then
  NVIDIA_VISIBLE_DEVICES="${whisper_gpu}" docker compose --profile asr up -d whisper-asr
  if [[ "${start_workers}" == "yes" ]]; then
    NVIDIA_VISIBLE_DEVICES="${whisper_gpu}" docker compose --profile asr up -d transcription-worker
  fi
fi

if [[ "${start_parakeet}" == "yes" ]]; then
  NVIDIA_VISIBLE_DEVICES="${parakeet_gpu}" docker compose --profile asr-parakeet up -d parakeet-asr
  if [[ "${start_workers}" == "yes" ]]; then
    NVIDIA_VISIBLE_DEVICES="${parakeet_gpu}" docker compose --profile asr-parakeet up -d parakeet-transcription-worker
  fi
fi

if [[ "${start_rag}" == "yes" ]]; then
  docker compose --profile rag up -d embedding-nim milvus-etcd milvus-minio milvus-standalone rag-api
fi

echo
echo "Done. Quick checks:"
echo "  docker compose ps"
echo "  docker compose logs --tail=100 atc-dashboard"
if [[ "${start_whisper}" == "yes" ]]; then
  echo "  curl -fsS --max-time 10 'http://localhost:9000/v1/health/ready'"
fi
if [[ "${start_parakeet}" == "yes" ]]; then
  echo "  curl -fsS --max-time 10 'http://localhost:9001/v1/health/ready'"
fi
