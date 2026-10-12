---
task_id: tv_2026_0502_r6_eval
title: Training validation material generation
priority: normal
worker: remote_materializer
status: pending
created_at: 2026-05-02T12:00:00+08:00
task_type: training_validation
allow_code_changes: false
allow_long_running_commands: false
allow_delete: false
allow_overwrite_outputs: false
allow_network: false
---

# Goal

Generate training validation materials for a prepared checkpoint.

# Inputs

- checkpoint: runs/tv_2026_0502_r6_eval/input/checkpoint.pt
- data list: runs/tv_2026_0502_r6_eval/input/samples.csv

# Required Outputs

- train_config.yaml
- val_config.yaml
- data_manifest.csv
- commands.md
- output_contract.yaml

