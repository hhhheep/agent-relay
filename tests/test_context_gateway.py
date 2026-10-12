from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import yaml

from solution_agent.context_gateway import build_context_pack


def _tmp_root() -> Path:
    root = Path(".test_tmp") / f"context_gateway_{uuid.uuid4().hex}"
    root.mkdir(parents=True, exist_ok=False)
    return root


def test_build_context_pack_writes_auditable_manifest_and_scopes():
    root = _tmp_root()
    try:
        (root / "solution_agent" / "execution_port").mkdir(parents=True)
        (root / "solution_agent" / "execution_port" / "script_registry.yaml").write_text(
            "scripts:\n  smoke:\n    entrypoint: [python, smoke.py]\n    args: {}\n",
            encoding="utf-8",
        )
        (root / ".solutionignore").write_text("secret.env\n", encoding="utf-8")
        (root / "config").mkdir()
        (root / "config" / "remote_execution_profiles.yaml").write_text(
            "schema_version: 1\nprofiles:\n  EXAMPLE_WORKER_B:\n    host: EXAMPLE_WORKER_B\n    remote_workdir: /srv/example-project\n    conda:\n      env: py_torch\n    project_assets:\n      checkpoint_roots:\n        - /srv/example-project/checkpoints\n      known_checkpoints:\n        cls_best: /srv/example-project/checkpoints/cls.ckpt\n",
            encoding="utf-8",
        )
        (root / ".solution" / "context").mkdir(parents=True)
        (root / ".solution" / "context" / "project_map.yaml").write_text(
            "schema_version: 1\nproject_id: demo_project\nproject_name: Demo Project\ndatasets:\n  - id: df40\n    path: /data/df40\nscripts:\n  - script_id: smoke\n    purpose: smoke test\nrouting_hints:\n  - if task asks for smoke, use script_id smoke\n",
            encoding="utf-8",
        )
        workplace = root / "state" / "workplaces" / "EXAMPLE_WORKER_B__p22__demo"
        workplace.mkdir(parents=True)
        (workplace / "agent_context.yaml").write_text(
            "schema_version: 1\nkind: solution_agent.workplace_agent_context\nprofile_id: EXAMPLE_WORKER_B__p22__demo\nhost: EXAMPLE_WORKER_B\nremote_workdir: /srv/example-project\nremote_root: /srv/example-project/outputs/lab-runs\nok: true\nconfigured_conda_env: py_torch\nconfigured_conda_env_ready: yes:3.10\nremote_materializer:\n  env_ready: true\n  auth_method: hmac-sha256\n  auth_status: matched\n  auth_ready: true\nreadiness_checks:\n  - code: remote_materializer_auth\n    status: ok\n",
            encoding="utf-8",
        )
        (root / "README.md").write_text("# demo\n", encoding="utf-8")
        (root / "secret.env").write_text("TOKEN=secret\n", encoding="utf-8")
        task_path = root / "tasks" / "pending" / "demo_task" / "task.md"
        task_path.parent.mkdir(parents=True)
        task_path.write_text(
            "---\ntask_id: demo_task\nworker: materialized_execution\n---\nRun the smoke script.\n",
            encoding="utf-8",
        )
        out_dir = root / "worktrees" / "demo_task" / "_worker_out"

        manifest = build_context_pack(
            task_id="demo_task",
            task_path=task_path,
            repo_root=root,
            out_dir=out_dir,
            workspace_profile="EXAMPLE_WORKER_B",
            remote_conda_env="py_torch",
        )

        pack = out_dir / "context_pack"
        assert (pack / "manifest.yaml").exists()
        assert (pack / "task_brief.md").exists()
        assert (pack / "script_registry_excerpt.yaml").exists()
        assert (pack / "workspace_context_excerpt.yaml").exists()
        assert (pack / "project_map_excerpt.yaml").exists()
        assert (pack / "retrieval_context.md").exists()
        assert (pack / "retrieval_context.json").exists()
        assert (pack / "execution_hints.yaml").exists()
        loaded = yaml.safe_load((pack / "manifest.yaml").read_text(encoding="utf-8"))
        assert loaded["schema_version"] == 1
        assert loaded["context_pack_id"] == manifest["context_pack_id"]
        assert loaded["remote_conda_env"] == "py_torch"
        assert loaded["selected"]["registry_entries"] == ["smoke"]
        selected = json.loads((pack / "selected_files.json").read_text(encoding="utf-8"))
        assert any(item["path"].endswith("task.md") and item["sha256"] for item in selected)
        hints = yaml.safe_load((pack / "execution_hints.yaml").read_text(encoding="utf-8"))
        assert hints["api_access_policy"] == "context_pack_only"
        assert "secret_file" in hints["missing_context_policy"]["denied_kinds"]
        assert hints["output_policy"]["mode"] == "workplace_bound"
        assert hints["output_policy"]["default_output_dir"].endswith("/worktrees/demo_task/artifacts")
        assert hints["output_policy"]["workplace_output_root"].endswith("/worktrees")
        workspace_context = yaml.safe_load((pack / "workspace_context_excerpt.yaml").read_text(encoding="utf-8"))
        assert workspace_context["remote_execution_profile"]["selected"]["conda_env"] == "py_torch"
        assert workspace_context["remote_execution_profile"]["selected"]["project_assets"]["known_checkpoints"]["cls_best"].endswith("cls.ckpt")
        assert workspace_context["workplace_init"]["selected"]["remote_materializer"]["auth_status"] == "matched"
        assert workspace_context["workplace_output"]["policy"] == "workplace_bound"
        assert workspace_context["workplace_output"]["default_output_dir"].endswith("/worktrees/demo_task/artifacts")
        workspace_context_text = (pack / "workspace_context_excerpt.yaml").read_text(encoding="utf-8")
        assert "TOKEN=secret" not in workspace_context_text
        assert "secret.env" not in workspace_context_text
        project_map = yaml.safe_load((pack / "project_map_excerpt.yaml").read_text(encoding="utf-8"))
        assert project_map["project_id"] == "demo_project"
        assert project_map["status"] == "present"
        assert loaded["inputs"]["project_map"] == ".solution/context/project_map.yaml"
        retrieval = json.loads((pack / "retrieval_context.json").read_text(encoding="utf-8"))
        assert retrieval["status"] == "skipped"
        assert loaded["retrieval"]["status"] == "skipped"
        capsule = json.loads(
            (root / "state" / "task_capsules" / "demo_task" / "capsule.json").read_text(encoding="utf-8")
        )
        assert len(capsule["authoritative_artifacts"]) == 2
        assert len(capsule["read_receipt_ids"]) == 2
        assert "Repository-wide discovery is disabled" in (pack / "repo_map_excerpt.md").read_text(encoding="utf-8")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_build_context_pack_parses_registry_entries_from_full_yaml_when_excerpt_truncates():
    root = _tmp_root()
    try:
        registry_dir = root / "solution_agent" / "execution_port"
        registry_dir.mkdir(parents=True)
        filler = "\n".join(f"    filler_{index}: value_{index}" for index in range(2500))
        (registry_dir / "script_registry.yaml").write_text(
            "scripts:\n"
            "  smoke:\n"
            "    entrypoint: [python, smoke.py]\n"
            "    args: {}\n"
            "    metadata:\n"
            f"{filler}\n",
            encoding="utf-8",
        )
        task_path = root / "tasks" / "pending" / "demo_task" / "task.md"
        task_path.parent.mkdir(parents=True)
        task_path.write_text(
            "---\ntask_id: demo_task\nworker: materialized_execution\n---\nRun the smoke script.\n",
            encoding="utf-8",
        )

        manifest = build_context_pack(
            task_id="demo_task",
            task_path=task_path,
            repo_root=root,
            out_dir=root / "worktrees" / "demo_task" / "_worker_out",
        )

        excerpt = (root / "worktrees" / "demo_task" / "_worker_out" / "context_pack" / "script_registry_excerpt.yaml").read_text(encoding="utf-8")
        assert "[truncated by context gateway]" in excerpt
        assert manifest["selected"]["registry_entries"] == ["smoke"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_build_context_pack_includes_grounded_artifact_memory_without_rebuilt_index():
    root = _tmp_root()
    try:
        (root / "solution_agent" / "execution_port").mkdir(parents=True)
        (root / "solution_agent" / "execution_port" / "script_registry.yaml").write_text(
            "scripts:\n  gend_maskout_probe:\n    entrypoint: [python, probe.py]\n    args: {}\n",
            encoding="utf-8",
        )
        context_root = root / ".solution" / "context"
        context_root.mkdir(parents=True)
        (context_root / "artifact_index.jsonl").write_text(
            json.dumps(
                {
                    "kind": "collected_run",
                    "task_id": "gend_feature_leakage_004_maskout_clean_token_probe",
                    "run_id": "gend_feature_leakage_004__a01",
                    "status": "completed",
                    "script_id": "gend_maskout_probe",
                    "summary": "Maskout clean-token probe completed using proxy CLIP fallback.",
                    "claim_scope": "bounded_proxy_probe_only",
                    "produced_artifacts": ["artifacts/maskout_clean_token_probe.csv", "artifacts/decision_summary.json"],
                    "decision": {
                        "claim_allowed": "bounded_proxy_probe_only",
                        "requested_model": "GenD_DINOv3_L",
                        "model": "GenD_CLIP_L_14",
                        "model_fallback_reason": "transformers version lacks DINOv3 classes",
                    },
                    "tags": ["maskout", "GenD_DINOv3_L", "GenD_CLIP_L_14"],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        task_path = root / "tasks" / "pending" / "gend_next" / "task.md"
        task_path.parent.mkdir(parents=True)
        task_path.write_text(
            "---\ntask_id: gend_next\nworker: materialized_execution\n---\nRun the next GenD DINOv3 maskout clean token analysis.\n",
            encoding="utf-8",
        )

        manifest = build_context_pack(task_id="gend_next", task_path=task_path, repo_root=root, out_dir=root / "out")

        pack = root / "out" / "context_pack"
        grounded_yaml = yaml.safe_load((pack / "grounded_context.yaml").read_text(encoding="utf-8"))
        grounded_text = (pack / "grounded_context.md").read_text(encoding="utf-8")
        assert grounded_yaml["status"] == "used"
        assert grounded_yaml["selected_count"] == 1
        assert "bounded_proxy_probe_only" in grounded_text
        assert "GenD_CLIP_L_14" in grounded_text
        assert "gend_feature_leakage_004_maskout_clean_token_probe" in grounded_text
        assert manifest["grounding"]["selected_count"] == 1
        assert "grounded_context.yaml" in yaml.safe_load((pack / "execution_hints.yaml").read_text(encoding="utf-8"))["read_scope"]["include"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_build_context_pack_allows_remote_task_outside_repo_root():
    root = _tmp_root()
    remote_root = Path(".test_tmp") / f"context_gateway_remote_{uuid.uuid4().hex}"
    try:
        (root / "solution_agent" / "execution_port").mkdir(parents=True)
        (root / "solution_agent" / "execution_port" / "script_registry.yaml").write_text(
            "scripts:\n  smoke:\n    entrypoint: [python, smoke.py]\n    args: {}\n",
            encoding="utf-8",
        )
        task_path = remote_root / "run_001" / "task.md"
        task_path.parent.mkdir(parents=True)
        task_path.write_text(
            "---\ntask_id: remote_task\nworker: materialized_execution\n---\nRun remote smoke.\n",
            encoding="utf-8",
        )
        out_dir = remote_root / "run_001" / "_worker_out"

        manifest = build_context_pack(
            task_id="remote_task",
            task_path=task_path,
            repo_root=root,
            out_dir=out_dir,
            workspace_profile="EXAMPLE_WORKER_A",
        )

        pack = out_dir / "context_pack"
        loaded = yaml.safe_load((pack / "manifest.yaml").read_text(encoding="utf-8"))
        selected = json.loads((pack / "selected_files.json").read_text(encoding="utf-8"))
        assert loaded["inputs"]["task_md"].startswith("external:")
        assert loaded["inputs"]["default_output_dir"].endswith("/run_001/artifacts")
        assert manifest["inputs"]["task_md"].startswith("external:")
        assert selected[0]["path"].startswith("external:")
        assert selected[1]["path"] == "solution_agent/execution_port/script_registry.yaml"
        hints = yaml.safe_load((pack / "execution_hints.yaml").read_text(encoding="utf-8"))
        assert hints["output_policy"]["default_output_dir"].endswith("/run_001/artifacts")
        assert hints["output_policy"]["workplace_output_root"].endswith(remote_root.name)
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(remote_root, ignore_errors=True)


def test_build_context_pack_falls_back_to_bundled_script_registry():
    root = _tmp_root()
    try:
        task_path = root / "tasks" / "pending" / "demo_task" / "task.md"
        task_path.parent.mkdir(parents=True)
        task_path.write_text(
            "---\ntask_id: demo_task\nworker: materialized_execution\n---\nRun a bounded probe.\n",
            encoding="utf-8",
        )
        out_dir = root / "worktrees" / "demo_task" / "_worker_out"

        manifest = build_context_pack(
            task_id="demo_task",
            task_path=task_path,
            repo_root=root,
            out_dir=out_dir,
            workspace_profile="local",
        )

        pack = out_dir / "context_pack"
        registry = yaml.safe_load((pack / "script_registry_excerpt.yaml").read_text(encoding="utf-8"))
        assert registry.get("scripts")
        assert manifest["selected"]["registry_entries"]
        assert set(registry["scripts"]).issubset(set(manifest["selected"]["registry_entries"]))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_build_context_pack_includes_required_dependency_artifact_pointers():
    root = _tmp_root()
    try:
        task_path = root / "tasks" / "pending" / "child_task" / "task.md"
        task_path.parent.mkdir(parents=True)
        task_path.write_text(
            """---
task_id: child_task
worker: materialized_execution
requires_artifacts_from:
  - parent_task
---
Use the parent metadata artifact.
""",
            encoding="utf-8",
        )
        result_dir = root / "results" / "parent_task" / "latest"
        result_dir.mkdir(parents=True)
        (result_dir / "result.yaml").write_text(
            """status: success
summary_short: Parent completed.
artifacts_found:
  - /remote/runs/parent_task__20260509_120000__a01/artifacts/metadata_all.csv
  - /remote/runs/parent_task__20260509_120000__a01/artifacts/metadata_pool_summary.csv
artifacts_missing: []
""",
            encoding="utf-8",
        )
        out_dir = root / "worktrees" / "child_task" / "_worker_out"

        manifest = build_context_pack(task_id="child_task", task_path=task_path, repo_root=root, out_dir=out_dir)

        pack = out_dir / "context_pack"
        artifact_summary = (pack / "artifact_summary.md").read_text(encoding="utf-8")
        assert "Use these exact artifact pointers" in artifact_summary
        assert "parent_task" in artifact_summary
        assert "/remote/runs/parent_task__20260509_120000__a01/artifacts/metadata_all.csv" in artifact_summary
        assert "Do not invent run ids" in artifact_summary
        assert manifest["selected"]["dependency_artifacts"][0]["task_id"] == "parent_task"
        assert manifest["selected"]["dependency_artifacts"][0]["artifacts_found"][0].endswith("metadata_all.csv")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_build_context_pack_finds_dependency_artifacts_in_remote_run_root():
    root = _tmp_root()
    remote_root = Path(".test_tmp") / f"context_gateway_remote_runs_{uuid.uuid4().hex}"
    try:
        task_path = remote_root / "child_task__20260509_130000__a01" / "task.md"
        task_path.parent.mkdir(parents=True)
        task_path.write_text(
            """---
task_id: child_task
worker: materialized_execution
requires_artifacts_from:
  - parent_task
---
Use the parent metadata artifact.
""",
            encoding="utf-8",
        )
        parent_out = remote_root / "parent_task__20260509_120000__a01" / "_worker_out"
        parent_out.mkdir(parents=True)
        (parent_out / "result.yaml").write_text(
            """status: success
summary_short: Parent completed remotely.
artifacts_found:
  - /remote/runs/parent_task__20260509_120000__a01/artifacts/metadata_all.csv
artifacts_missing: []
""",
            encoding="utf-8",
        )
        out_dir = task_path.parent / "_worker_out"

        manifest = build_context_pack(task_id="child_task", task_path=task_path, repo_root=root, out_dir=out_dir)

        artifact_summary = (out_dir / "context_pack" / "artifact_summary.md").read_text(encoding="utf-8")
        assert "Parent completed remotely" in artifact_summary
        assert "/remote/runs/parent_task__20260509_120000__a01/artifacts/metadata_all.csv" in artifact_summary
        assert manifest["selected"]["dependency_artifacts"][0]["status"] == "success"
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(remote_root, ignore_errors=True)


def test_build_context_pack_finds_dependency_artifacts_from_materializer_attempt_dir():
    root = _tmp_root()
    remote_root = Path(".test_tmp") / f"context_gateway_materializer_runs_{uuid.uuid4().hex}"
    try:
        task_path = remote_root / "child_task__20260509_130000__a01" / "task.md"
        task_path.parent.mkdir(parents=True)
        task_path.write_text(
            """---
task_id: child_task
worker: materialized_execution
requires_artifacts_from:
  - parent_task
---
Use the parent metadata artifact.
""",
            encoding="utf-8",
        )
        parent_out = remote_root / "parent_task__20260509_120000__a01" / "_worker_out"
        parent_out.mkdir(parents=True)
        (parent_out / "result.yaml").write_text(
            """status: success
summary_short: Parent completed remotely.
artifacts_found:
  - /remote/runs/parent_task__20260509_120000__a01/artifacts/metadata_all.csv
artifacts_missing: []
""",
            encoding="utf-8",
        )
        out_dir = task_path.parent / "materializer" / "attempts" / "attempt_001" / "child_task"

        manifest = build_context_pack(task_id="child_task", task_path=task_path, repo_root=root, out_dir=out_dir)

        artifact_summary = (out_dir / "context_pack" / "artifact_summary.md").read_text(encoding="utf-8")
        assert "Parent completed remotely" in artifact_summary
        assert "/remote/runs/parent_task__20260509_120000__a01/artifacts/metadata_all.csv" in artifact_summary
        assert manifest["selected"]["dependency_artifacts"][0]["status"] == "success"
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(remote_root, ignore_errors=True)
