---
task_id: probe_axis_tf_jpeg_0502
title: Probe inference material generation
priority: normal
worker: remote_materializer
status: pending
created_at: 2026-05-02T12:00:00+08:00
task_type: probe_inference
allow_code_changes: false
allow_long_running_commands: false
allow_delete: false
allow_overwrite_outputs: false
allow_network: false
---

# Goal

Generate probe inference materials for the TF/JPEG axes.

# Inputs

- checkpoint: runs/probe_axis_tf_jpeg_0502/input/checkpoint.pt
- samples: runs/probe_axis_tf_jpeg_0502/input/samples.csv
- axes: temporal_flicker, jpeg_stress

# Required Outputs

- probe_infer_config.yaml
- sample_manifest.csv
- probe_axes.csv
- commands.md
- expected_tables.md
- output_contract.yaml

