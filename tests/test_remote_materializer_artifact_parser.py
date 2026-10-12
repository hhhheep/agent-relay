from __future__ import annotations

from solution_agent.remote_materializer.artifact_parser import parse_fenced_artifacts


def test_parser_infers_execution_plan_from_unnamed_json_fence():
    raw = """Here is the plan:

```json
{
  "schema_version": 1,
  "task_id": "t1",
  "read_paths": [],
  "write_paths": [],
  "data_access": {
    "uses_existing_indices": true,
    "opens_image_files": false,
    "opens_image_pixels": false,
    "full_recursive_dataset_scan": false,
    "copies_or_moves_data": false,
    "modifies_datasets": false
  },
  "execution_strategy": "Use existing manifests only."
}
```
"""

    artifacts = parse_fenced_artifacts(raw)

    assert [artifact.name for artifact in artifacts] == ["execution_plan.json"]


def test_parser_keeps_explicit_file_names_authoritative():
    raw = """```json file="custom.json"
{"hello": "world"}
```

```json
{
  "schema_version": 1,
  "task_id": "t1",
  "read_paths": [],
  "write_paths": [],
  "data_access": {},
  "execution_strategy": "bounded"
}
```
"""

    artifacts = parse_fenced_artifacts(raw)

    assert [artifact.name for artifact in artifacts] == ["custom.json", "execution_plan.json"]


def test_parser_repairs_missing_trailing_json_brace_for_named_artifact():
    raw = """```yaml file="execution_plan.json"
{
  "schema_version": 1,
  "task_id": "t1",
  "read_paths": [],
  "write_paths": [],
  "data_access": {},
  "execution_strategy": "bounded"
```
"""

    artifacts = parse_fenced_artifacts(raw)

    assert [artifact.name for artifact in artifacts] == ["execution_plan.json"]
    assert artifacts[0].text.rstrip().endswith("}")


def test_parser_accepts_accidental_trailing_angle_after_file_attribute():
    raw = """```markdown file="worker_notes.md">
# Notes

The model accidentally left a markdown-ish angle after the file attribute.
```
"""

    artifacts = parse_fenced_artifacts(raw)

    assert [artifact.name for artifact in artifacts] == ["worker_notes.md"]
    assert "accidentally" in artifacts[0].text
