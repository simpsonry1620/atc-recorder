#!/bin/bash
set -euo pipefail

CONFIG_PATH="${NEMO_TRAINING_CONFIG:-/workspace/training/parakeet_lora.yaml}"

if [ ! -f "$CONFIG_PATH" ]; then
    echo "ERROR: Training config not found at $CONFIG_PATH"
    echo "Generate it first: atc-recorder training configure"
    exit 1
fi

echo "=== NeMo LoRA Fine-Tuning ==="
echo "Config: $CONFIG_PATH"
echo "GPU: $(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null || echo 'unknown')"
echo ""

python -c "
import nemo.collections.asr as nemo_asr
from nemo.utils.exp_manager import exp_manager
from omegaconf import OmegaConf
import pytorch_lightning as pl

cfg = OmegaConf.load('$CONFIG_PATH')

# Load base model
model_name = cfg.get('init_from_pretrained_model', 'stt_en_parakeet_tdt_1.1b')
print(f'Loading base model: {model_name}')
model = nemo_asr.models.ASRModel.from_pretrained(model_name)

# Apply PEFT/LoRA config
if 'peft' in cfg:
    model.add_adapter(cfg.peft)
    print(f'LoRA adapter added: r={cfg.peft.lora_cfg.r}, alpha={cfg.peft.lora_cfg.lora_alpha}')

# Configure data
model.setup_training_data(cfg.model.train_ds)
model.setup_validation_data(cfg.model.validation_ds)
model.setup_optimization(cfg.model.optim)

# Setup trainer
trainer = pl.Trainer(**cfg.trainer)
exp_manager(trainer, cfg.get('exp_manager', {}))

# Train
print('Starting training...')
trainer.fit(model)

# Save adapter
output_dir = cfg.trainer.get('default_root_dir', '/workspace/output')
adapter_path = f'{output_dir}/parakeet_lora_kdca.nemo'
model.save_to(adapter_path)
print(f'LoRA adapter saved to: {adapter_path}')
"
