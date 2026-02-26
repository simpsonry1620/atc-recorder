"""NeMo LoRA fine-tuning configuration and manifest generation.

Generates NeMo-compatible training YAML configs and exports labeled
data into NeMo manifest format for Parakeet-TDT LoRA fine-tuning.
"""

import json
import random
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import yaml

from .logging import get_logger

logger = get_logger(__name__)


def generate_nemo_config(
    train_manifest: str,
    val_manifest: str,
    base_model: str = "stt_en_parakeet_tdt_1.1b",
    output_dir: str = "./models/lora_adapters",
    *,
    lora_r: int = 16,
    lora_alpha: int = 32,
    lora_dropout: float = 0.05,
    target_modules: Optional[list[str]] = None,
    batch_size: int = 16,
    max_epochs: int = 50,
    learning_rate: float = 0.0001,
) -> dict:
    """Generate a NeMo-compatible training config with LoRA PEFT block."""
    if target_modules is None:
        target_modules = ["linear_q", "linear_v", "ffn"]

    config = {
        "name": f"parakeet_lora_kdca",
        "init_from_pretrained_model": base_model,
        "model": {
            "train_ds": {
                "manifest_filepath": train_manifest,
                "batch_size": batch_size,
                "shuffle": True,
                "num_workers": 4,
                "pin_memory": True,
                "max_duration": 20.0,
                "min_duration": 1.0,
            },
            "validation_ds": {
                "manifest_filepath": val_manifest,
                "batch_size": batch_size,
                "shuffle": False,
                "num_workers": 2,
            },
            "optim": {
                "name": "adamw",
                "lr": learning_rate,
                "weight_decay": 0.01,
                "sched": {
                    "name": "CosineAnnealing",
                    "warmup_steps": 500,
                    "min_lr": 1e-6,
                },
            },
        },
        "peft": {
            "peft_scheme": "lora",
            "restore_core_weights": True,
            "lora_cfg": {
                "r": lora_r,
                "lora_alpha": lora_alpha,
                "lora_dropout": lora_dropout,
                "target_modules": target_modules,
            },
        },
        "trainer": {
            "devices": 1,
            "accelerator": "gpu",
            "max_epochs": max_epochs,
            "precision": "bf16-mixed",
            "val_check_interval": 0.25,
            "log_every_n_steps": 10,
            "enable_checkpointing": True,
            "default_root_dir": output_dir,
        },
        "exp_manager": {
            "exp_dir": output_dir,
            "name": "parakeet_lora_kdca",
            "checkpoint_callback_params": {
                "monitor": "val_wer",
                "mode": "min",
                "save_top_k": 3,
                "save_last": True,
            },
        },
    }
    return config


def save_nemo_config(config: dict, output_path: Path) -> None:
    """Write a NeMo training config to YAML."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    logger.info("Saved NeMo training config to %s", output_path)


def export_manifests(
    labeled_chunks: list,
    output_dir: Path,
    train_ratio: float = 0.9,
    min_duration: float = 2.0,
    max_duration: float = 15.0,
    seed: int = 42,
) -> tuple[Path, Path, dict]:
    """Export labeled chunks to NeMo JSONL manifests with train/val split.

    Args:
        labeled_chunks: List of LabeledChunk objects with spoken_text populated.
        output_dir: Directory for manifest files.
        train_ratio: Fraction of data for training (rest is validation).
        min_duration: Minimum chunk duration to include.
        max_duration: Maximum chunk duration to include.
        seed: Random seed for reproducible splitting.

    Returns:
        (train_manifest_path, val_manifest_path, stats_dict)
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    entries = []
    for chunk in labeled_chunks:
        text = chunk.spoken_text or chunk.verified_text or chunk.consensus_text
        if not text.strip():
            continue
        dur = chunk.duration
        if dur < min_duration or dur > max_duration:
            continue
        entries.append({
            "audio_filepath": chunk.audio_path,
            "duration": round(dur, 2),
            "text": text.strip().lower(),
        })

    if not entries:
        logger.warning("No entries to export")
        empty = output_dir / "train_kdca.json"
        empty.write_text("")
        val = output_dir / "val_kdca.json"
        val.write_text("")
        return empty, val, {"total": 0, "train": 0, "val": 0}

    rng = random.Random(seed)
    rng.shuffle(entries)

    split_idx = int(len(entries) * train_ratio)
    train_entries = entries[:split_idx]
    val_entries = entries[split_idx:]

    train_path = output_dir / "train_kdca.json"
    val_path = output_dir / "val_kdca.json"

    with open(train_path, "w") as f:
        for entry in train_entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    with open(val_path, "w") as f:
        for entry in val_entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    total_dur = sum(e["duration"] for e in entries)
    stats = {
        "total": len(entries),
        "train": len(train_entries),
        "val": len(val_entries),
        "total_duration_hours": round(total_dur / 3600, 2),
        "train_duration_hours": round(sum(e["duration"] for e in train_entries) / 3600, 2),
        "val_duration_hours": round(sum(e["duration"] for e in val_entries) / 3600, 2),
    }

    logger.info(
        "Exported manifests: %d train, %d val (%.1f hours total)",
        stats["train"], stats["val"], stats["total_duration_hours"],
    )
    return train_path, val_path, stats
