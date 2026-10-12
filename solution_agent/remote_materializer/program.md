# Remote Materializer Program

This worker is a lightweight remote controller for easy task books.

It follows a fixed contract:

- Read one `task.md`.
- Ask a reader model to convert it to `work_order.json`.
- Ask a materializer model to generate only requested YAML, CSV, and Markdown artifacts.
- Run deterministic Python validators.
- Write `result_package.yaml`, `validation/validation_result.json`, raw model outputs, and `run_manifest.json`.

It must not:

- Execute training or evaluation.
- Modify source code.
- Mutate the scheduler registry.
- Change dataset splits.
- Delete files.
- Touch checkpoints or baseline results.

The human-programmed part is the task book and the schemas. The model-programmed part is limited to generated materials under `runs/<task_id>/`.

