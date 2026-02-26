"""Configuration management for ATC Recorder."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class RecordingConfig:
    """Configuration for recording behavior."""

    enabled: bool = True
    reconnect_delay: int = 30  # seconds
    max_retries: int = 5
    segment_duration: int = 1800  # 30 minutes in seconds


@dataclass
class TranscriptionConfig:
    """Configuration for transcription (segment-by-pauses, export format)."""

    preprocess: str = "none"  # none, ffmpeg, ffmpeg_vad, sox, maxine, pipeline
    preprocess_output_dir: str = "./recordings/preprocessed"
    keep_preprocessed_audio: bool = False
    segment_by_pauses: bool = False
    min_silence_duration: float = 0.5
    silence_threshold_dB: float = -30.0
    min_speech_duration: float = 0.3
    merge_gap_seconds: float = 0.5
    output_format: str = "json"  # json, timestamped-txt, srt
    diarization_enabled: bool = False
    diarization_mode: str = "role-heuristic"  # role-heuristic
    stitch_across_files: bool = False
    stitch_max_gap_seconds: float = 2.0
    stitch_min_text_overlap_chars: int = 12
    variant_store_path: str = "./recordings/transcripts.db"
    pipeline_preset: Optional[str] = None
    pipeline_steps: Optional[list] = None


@dataclass
class EmbeddingConfig:
    """Embedding endpoint configuration."""

    provider: str = "nvidia-nim"
    endpoint: str = "http://localhost:9080/v1/embeddings"
    model: str = "nvidia/llama-3.2-nv-embedqa-1b-v2"
    api_key_env: str = "NGC_API_KEY"
    timeout_seconds: float = 15.0
    max_retries: int = 3
    retry_backoff_seconds: float = 1.0


@dataclass
class VectorStoreConfig:
    """Vector store configuration."""

    provider: str = "milvus"
    host: str = "localhost"
    port: int = 19530
    collection_name: str = "atc_transcripts"
    embedding_dim: int = 2048
    metric_type: str = "COSINE"
    index_type: str = "HNSW"
    use_tls: bool = False
    sqlite_metadata_path: str = "./recordings/rag_metadata.db"


@dataclass
class RagApiConfig:
    """Search API configuration."""

    host: str = "0.0.0.0"
    port: int = 8100
    top_k_default: int = 10
    top_k_max: int = 50


@dataclass
class RagConfig:
    """RAG ingestion and retrieval configuration."""

    enabled: bool = False
    ingest_on_transcribe: bool = False
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    vector_store: VectorStoreConfig = field(default_factory=VectorStoreConfig)
    api: RagApiConfig = field(default_factory=RagApiConfig)


@dataclass
class EntityExtractionConfig:
    """Entity extraction configuration."""

    enabled: bool = True
    extract_callsigns: bool = True
    extract_runways: bool = True
    extract_altitudes: bool = True
    extract_frequencies: bool = True
    min_confidence: float = 0.5


@dataclass
class TrackingConfig:
    """Flight entity extraction configuration."""

    entity_extraction: EntityExtractionConfig = field(default_factory=EntityExtractionConfig)


@dataclass
class LoRAConfig:
    """LoRA PEFT hyperparameters."""

    r: int = 16
    alpha: int = 32
    dropout: float = 0.05
    target_modules: list[str] = field(
        default_factory=lambda: ["linear_q", "linear_v", "ffn"]
    )


@dataclass
class LexiconConfig:
    """Custom pronunciation lexicon for text normalization."""

    waypoints: dict[str, str] = field(default_factory=dict)


@dataclass
class TrainingConfig:
    """Configuration for LoRA fine-tuning."""

    base_model: str = "stt_en_parakeet_tdt_1.1b"
    lora: LoRAConfig = field(default_factory=LoRAConfig)
    batch_size: int = 16
    max_epochs: int = 50
    learning_rate: float = 0.0001
    output_dir: str = "./models/lora_adapters"
    chunks_dir: str = "./recordings/chunks"
    labels_db_path: str = "./recordings/labels.db"
    chunks_db_path: str = "./recordings/chunks.db"
    benchmark_db_path: str = "./recordings/benchmarks.db"
    train_ratio: float = 0.9
    min_chunk_duration: float = 2.0
    max_chunk_duration: float = 15.0
    pad_seconds: float = 0.5
    energy_threshold: float = 500.0
    max_cer: float = 0.05
    lexicon: LexiconConfig = field(default_factory=LexiconConfig)
    default_model: str = "whisper"


@dataclass
class Config:
    """Main configuration for ATC Recorder."""

    output_dir: Path = field(default_factory=lambda: Path("./recordings"))
    segment_duration: int = 1800  # 30 minutes in seconds
    feeds: list[str] = field(default_factory=list)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    transcription: Optional[TranscriptionConfig] = None
    rag: Optional[RagConfig] = None
    tracking: Optional[TrackingConfig] = None
    training: Optional[TrainingConfig] = None
    request_delay: float = 1.0  # delay between requests in seconds
    user_agent: str = "ATC-Recorder/0.1.0"

    @classmethod
    def from_yaml(cls, path: Path) -> "Config":
        """Load configuration from a YAML file.

        Args:
            path: Path to the YAML configuration file

        Returns:
            Config object

        Raises:
            FileNotFoundError: If the config file doesn't exist
            yaml.YAMLError: If the YAML is invalid
        """
        with open(path, "r") as f:
            data = yaml.safe_load(f) or {}

        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        """Create a Config from a dictionary.

        Args:
            data: Configuration dictionary

        Returns:
            Config object
        """
        recording_data = data.get("recording", {})
        recording_config = RecordingConfig(
            enabled=recording_data.get("enabled", True),
            reconnect_delay=recording_data.get("reconnect_delay", 30),
            max_retries=recording_data.get("max_retries", 5),
            segment_duration=recording_data.get("segment_duration", 1800),
        )

        transcription_config = None
        if "transcription" in data:
            t = data["transcription"]
            transcription_config = TranscriptionConfig(
                preprocess=t.get("preprocess", "none"),
                preprocess_output_dir=t.get("preprocess_output_dir", "./recordings/preprocessed"),
                keep_preprocessed_audio=t.get("keep_preprocessed_audio", False),
                segment_by_pauses=t.get("segment_by_pauses", False),
                min_silence_duration=t.get("min_silence_duration", 0.5),
                silence_threshold_dB=t.get("silence_threshold_dB", -30.0),
                min_speech_duration=t.get("min_speech_duration", 0.3),
                merge_gap_seconds=t.get("merge_gap_seconds", 0.5),
                output_format=t.get("output_format", "json"),
                diarization_enabled=t.get("diarization_enabled", False),
                diarization_mode=t.get("diarization_mode", "role-heuristic"),
                stitch_across_files=t.get("stitch_across_files", False),
                stitch_max_gap_seconds=t.get("stitch_max_gap_seconds", 2.0),
                stitch_min_text_overlap_chars=t.get("stitch_min_text_overlap_chars", 12),
                variant_store_path=t.get("variant_store_path", "./recordings/transcripts.db"),
                pipeline_preset=t.get("pipeline_preset"),
                pipeline_steps=t.get("pipeline_steps"),
            )

        rag_config = None
        if "rag" in data:
            rag_data = data.get("rag", {})
            emb_data = rag_data.get("embedding", {})
            vs_data = rag_data.get("vector_store", {})
            api_data = rag_data.get("api", {})

            rag_config = RagConfig(
                enabled=rag_data.get("enabled", False),
                ingest_on_transcribe=rag_data.get("ingest_on_transcribe", False),
                embedding=EmbeddingConfig(
                    provider=emb_data.get("provider", "nvidia-nim"),
                    endpoint=emb_data.get("endpoint", "http://localhost:9080/v1/embeddings"),
                    model=emb_data.get("model", "nvidia/llama-3.2-nv-embedqa-1b-v2"),
                    api_key_env=emb_data.get("api_key_env", "NGC_API_KEY"),
                    timeout_seconds=emb_data.get("timeout_seconds", 15.0),
                    max_retries=emb_data.get("max_retries", 3),
                    retry_backoff_seconds=emb_data.get("retry_backoff_seconds", 1.0),
                ),
                vector_store=VectorStoreConfig(
                    provider=vs_data.get("provider", "milvus"),
                    host=vs_data.get("host", "localhost"),
                    port=vs_data.get("port", 19530),
                    collection_name=vs_data.get("collection_name", "atc_transcripts"),
                    embedding_dim=vs_data.get("embedding_dim", 2048),
                    metric_type=vs_data.get("metric_type", "COSINE"),
                    index_type=vs_data.get("index_type", "HNSW"),
                    use_tls=vs_data.get("use_tls", False),
                    sqlite_metadata_path=vs_data.get(
                        "sqlite_metadata_path",
                        "./recordings/rag_metadata.db",
                    ),
                ),
                api=RagApiConfig(
                    host=api_data.get("host", "0.0.0.0"),
                    port=api_data.get("port", 8100),
                    top_k_default=api_data.get("top_k_default", 10),
                    top_k_max=api_data.get("top_k_max", 50),
                ),
            )

        tracking_config = None
        if "tracking" in data:
            tk = data["tracking"]
            ee = tk.get("entity_extraction", {})
            tracking_config = TrackingConfig(
                entity_extraction=EntityExtractionConfig(
                    enabled=ee.get("enabled", True),
                    extract_callsigns=ee.get("extract_callsigns", True),
                    extract_runways=ee.get("extract_runways", True),
                    extract_altitudes=ee.get("extract_altitudes", True),
                    extract_frequencies=ee.get("extract_frequencies", True),
                    min_confidence=ee.get("min_confidence", 0.5),
                ),
            )

        training_config = None
        if "training" in data:
            tr = data["training"]
            lora_data = tr.get("lora", {})
            lex_data = tr.get("lexicon", {})
            training_config = TrainingConfig(
                base_model=tr.get("base_model", "stt_en_parakeet_tdt_1.1b"),
                lora=LoRAConfig(
                    r=lora_data.get("r", 16),
                    alpha=lora_data.get("alpha", 32),
                    dropout=lora_data.get("dropout", 0.05),
                    target_modules=lora_data.get(
                        "target_modules", ["linear_q", "linear_v", "ffn"]
                    ),
                ),
                batch_size=tr.get("batch_size", 16),
                max_epochs=tr.get("max_epochs", 50),
                learning_rate=tr.get("learning_rate", 0.0001),
                output_dir=tr.get("output_dir", "./models/lora_adapters"),
                chunks_dir=tr.get("chunks_dir", "./recordings/chunks"),
                labels_db_path=tr.get("labels_db_path", "./recordings/labels.db"),
                chunks_db_path=tr.get("chunks_db_path", "./recordings/chunks.db"),
                benchmark_db_path=tr.get("benchmark_db_path", "./recordings/benchmarks.db"),
                train_ratio=tr.get("train_ratio", 0.9),
                min_chunk_duration=tr.get("min_chunk_duration", 2.0),
                max_chunk_duration=tr.get("max_chunk_duration", 15.0),
                pad_seconds=tr.get("pad_seconds", 0.5),
                energy_threshold=tr.get("energy_threshold", 500.0),
                max_cer=tr.get("max_cer", 0.05),
                lexicon=LexiconConfig(
                    waypoints=lex_data.get("waypoints", {}),
                ),
                default_model=tr.get("default_model", "whisper"),
            )

        output_dir = data.get("output_dir", "./recordings")
        if isinstance(output_dir, str):
            output_dir = Path(output_dir)

        return cls(
            output_dir=output_dir,
            segment_duration=data.get("segment_duration", 1800),
            feeds=data.get("feeds", []),
            recording=recording_config,
            transcription=transcription_config,
            rag=rag_config,
            tracking=tracking_config,
            training=training_config,
            request_delay=data.get("request_delay", 1.0),
            user_agent=data.get("user_agent", "ATC-Recorder/0.1.0"),
        )

    def to_dict(self) -> dict:
        """Convert the configuration to a dictionary.

        Returns:
            Configuration dictionary
        """
        d = {
            "output_dir": str(self.output_dir),
            "segment_duration": self.segment_duration,
            "feeds": self.feeds,
            "recording": {
                "enabled": self.recording.enabled,
                "reconnect_delay": self.recording.reconnect_delay,
                "max_retries": self.recording.max_retries,
                "segment_duration": self.recording.segment_duration,
            },
            "request_delay": self.request_delay,
            "user_agent": self.user_agent,
        }
        if self.transcription is not None:
            d["transcription"] = {
                "preprocess": self.transcription.preprocess,
                "preprocess_output_dir": self.transcription.preprocess_output_dir,
                "keep_preprocessed_audio": self.transcription.keep_preprocessed_audio,
                "segment_by_pauses": self.transcription.segment_by_pauses,
                "min_silence_duration": self.transcription.min_silence_duration,
                "silence_threshold_dB": self.transcription.silence_threshold_dB,
                "min_speech_duration": self.transcription.min_speech_duration,
                "merge_gap_seconds": self.transcription.merge_gap_seconds,
                "output_format": self.transcription.output_format,
                "diarization_enabled": self.transcription.diarization_enabled,
                "diarization_mode": self.transcription.diarization_mode,
                "stitch_across_files": self.transcription.stitch_across_files,
                "stitch_max_gap_seconds": self.transcription.stitch_max_gap_seconds,
                "stitch_min_text_overlap_chars": self.transcription.stitch_min_text_overlap_chars,
                "variant_store_path": self.transcription.variant_store_path,
            }
            if self.transcription.pipeline_preset is not None:
                d["transcription"]["pipeline_preset"] = self.transcription.pipeline_preset
            if self.transcription.pipeline_steps is not None:
                d["transcription"]["pipeline_steps"] = self.transcription.pipeline_steps
        if self.rag is not None:
            d["rag"] = {
                "enabled": self.rag.enabled,
                "ingest_on_transcribe": self.rag.ingest_on_transcribe,
                "embedding": {
                    "provider": self.rag.embedding.provider,
                    "endpoint": self.rag.embedding.endpoint,
                    "model": self.rag.embedding.model,
                    "api_key_env": self.rag.embedding.api_key_env,
                    "timeout_seconds": self.rag.embedding.timeout_seconds,
                    "max_retries": self.rag.embedding.max_retries,
                    "retry_backoff_seconds": self.rag.embedding.retry_backoff_seconds,
                },
                "vector_store": {
                    "provider": self.rag.vector_store.provider,
                    "host": self.rag.vector_store.host,
                    "port": self.rag.vector_store.port,
                    "collection_name": self.rag.vector_store.collection_name,
                    "embedding_dim": self.rag.vector_store.embedding_dim,
                    "metric_type": self.rag.vector_store.metric_type,
                    "index_type": self.rag.vector_store.index_type,
                    "use_tls": self.rag.vector_store.use_tls,
                    "sqlite_metadata_path": self.rag.vector_store.sqlite_metadata_path,
                },
                "api": {
                    "host": self.rag.api.host,
                    "port": self.rag.api.port,
                    "top_k_default": self.rag.api.top_k_default,
                    "top_k_max": self.rag.api.top_k_max,
                },
            }
        if self.tracking is not None:
            d["tracking"] = {
                "entity_extraction": {
                    "enabled": self.tracking.entity_extraction.enabled,
                    "extract_callsigns": self.tracking.entity_extraction.extract_callsigns,
                    "extract_runways": self.tracking.entity_extraction.extract_runways,
                    "extract_altitudes": self.tracking.entity_extraction.extract_altitudes,
                    "extract_frequencies": self.tracking.entity_extraction.extract_frequencies,
                    "min_confidence": self.tracking.entity_extraction.min_confidence,
                },
            }
        if self.training is not None:
            d["training"] = {
                "base_model": self.training.base_model,
                "lora": {
                    "r": self.training.lora.r,
                    "alpha": self.training.lora.alpha,
                    "dropout": self.training.lora.dropout,
                    "target_modules": self.training.lora.target_modules,
                },
                "batch_size": self.training.batch_size,
                "max_epochs": self.training.max_epochs,
                "learning_rate": self.training.learning_rate,
                "output_dir": self.training.output_dir,
                "chunks_dir": self.training.chunks_dir,
                "labels_db_path": self.training.labels_db_path,
                "chunks_db_path": self.training.chunks_db_path,
                "benchmark_db_path": self.training.benchmark_db_path,
                "train_ratio": self.training.train_ratio,
                "min_chunk_duration": self.training.min_chunk_duration,
                "max_chunk_duration": self.training.max_chunk_duration,
                "pad_seconds": self.training.pad_seconds,
                "energy_threshold": self.training.energy_threshold,
                "max_cer": self.training.max_cer,
                "default_model": self.training.default_model,
                "lexicon": {
                    "waypoints": self.training.lexicon.waypoints,
                },
            }
        return d

    def save(self, path: Path) -> None:
        """Save configuration to a YAML file.

        Args:
            path: Path to save the configuration
        """
        with open(path, "w") as f:
            yaml.dump(self.to_dict(), f, default_flow_style=False, sort_keys=False)


def load_config(path: Optional[Path] = None) -> Config:
    """Load configuration from file or return defaults.

    Args:
        path: Optional path to config file. If None, looks for config.yaml
              in the current directory.

    Returns:
        Config object
    """
    if path is None:
        path = Path("config.yaml")

    if path.exists():
        return Config.from_yaml(path)

    return Config()


# Default DCA feeds for quick access
DCA_FEEDS = [
    "kdca1_gnd",  # Ground (121.700)
    "kdca2_twr",  # Tower 1 (119.100)
    "kdca1_twr",  # Tower 2 (119.100)
    "kdca1_heli",  # Tower Helicopters (134.350)
    "kdca1_dep",  # Potomac Departure (118.950)
    "kdca1_app_final",  # Approach DCA Final (124.700)
    "kdca1_app_ensue",  # Approach ENSUE Sector (124.200)
    "kdca1_app_ojaay",  # Approach OJAAY Sector (119.850)
    "kmrb1_app_luray",  # Approach LURAY (118.675)
    "kdca1_dep_121050",  # App/Dep FLUKY (121.050)
    "kdca1_dep_e",  # App/Dep KRANT (125.650)
    "kdca1_sfra_s",  # SFRA South (125.125)
    "kdca",  # Tower/Approach Combined
]
