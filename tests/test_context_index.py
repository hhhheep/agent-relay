from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from solution_agent.cli import cli
from solution_agent.context_gateway import build_context_pack
from solution_agent.context_index import build_context_index, context_index_status, index_collected_result, query_context_index, snapshot_remote_workplace, write_context_documents


def _tmp_root() -> Path:
    root = Path(".test_tmp") / f"context_index_{uuid.uuid4().hex}"
    root.mkdir(parents=True, exist_ok=False)
    return root


def _write_demo_sources(root: Path) -> None:
    (root / ".solution" / "context").mkdir(parents=True)
    (root / ".solution" / "context" / "project_map.yaml").write_text(
        """schema_version: 1
project_id: demo
project_name: Demo
confidence:
  overall: reviewed
datasets:
  - id: df40
    path: /data/df40
    purpose: Deepfake evaluation dataset
scripts:
  - script_id: run_eval_probe
    path: .codex-orchestrator/runtime/scripts/probes/run_eval_probe.py
    purpose: Run evaluation probe and write metrics
routing_hints:
  - when: task asks for evaluation metrics
    prefer_script_id: run_eval_probe
""",
        encoding="utf-8",
    )
    (root / "solution_agent" / "execution_port").mkdir(parents=True)
    (root / "solution_agent" / "execution_port" / "script_registry.yaml").write_text(
        """scripts:
  run_eval_probe:
    entrypoint: [python, .codex-orchestrator/runtime/scripts/probes/run_eval_probe.py]
    args:
      config:
        flag: --config
        type: path
        required: true
""",
        encoding="utf-8",
    )
    (root / ".solution" / "context" / "artifact_index.jsonl").write_text(
        json.dumps(
            {
                "task_id": "eval_old",
                "status": "completed",
                "summary": "DF40 evaluation completed with metrics.json.",
                "metrics": {"auc": 0.83},
                "important_paths": ["outputs/eval_old/metrics.json"],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def test_context_index_builds_and_queries_bounded_documents():
    root = _tmp_root()
    try:
        _write_demo_sources(root)
        docs = write_context_documents(root)
        assert docs["doc_count"] >= 3
        built = build_context_index(root)
        assert built["doc_count"] == docs["doc_count"]

        result = query_context_index(root, "df40 evaluation metrics", limit=5)

        assert result["ok"] is True
        assert result["results"]
        joined = "\n".join(item["doc_id"] for item in result["results"])
        assert "run_eval_probe" in joined or "df40" in joined or "eval_old" in joined
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_index_collected_result_records_run_artifacts_and_claim_scope():
    root = _tmp_root()
    try:
        result_dir = root / "results" / "gend_004" / "latest"
        artifacts = result_dir / "artifacts"
        artifacts.mkdir(parents=True)
        (result_dir / "result.yaml").write_text(
            """status: success
worker: materialized_execution
script_id: gend_maskout_probe
summary_short: Maskout probe completed with proxy fallback.
run_id: gend_004__a01
remote:
  host: EXAMPLE_WORKER_A
  remote_workdir: /srv/example-workspaces-b/GenD_experiment
judge:
  execution_success: true
  warning_type: model_fallback
""",
            encoding="utf-8",
        )
        (result_dir / "metrics.json").write_text(json.dumps({"rows": 12}), encoding="utf-8")
        (artifacts / "decision_summary.json").write_text(
            json.dumps(
                {
                    "claim_allowed": "bounded_proxy_probe_only",
                    "model_execution_evidence": {
                        "requested_model": "GenD_DINOv3_L",
                        "executed_model": "GenD_CLIP_L_14",
                        "fallback_reason": "Remote transformers lacks DINOv3 classes.",
                        "proxy_only": True,
                        "formal_evidence": False,
                        "evidence_scope": "proxy_probe",
                        "allowed_claims": ["bounded proxy probe completed"],
                        "forbidden_claims": ["DINOv3 formally validated the feature leakage hypothesis"],
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (artifacts / "maskout_clean_token_probe.csv").write_text("group,score\nclean,0.1\n", encoding="utf-8")

        result = index_collected_result(root, task_id="gend_004", result_path=result_dir)

        assert result["ok"] is True
        entry = json.loads((root / ".solution" / "context" / "artifact_index.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        assert entry["kind"] == "collected_run"
        assert entry["task_id"] == "gend_004"
        assert entry["run_id"] == "gend_004__a01"
        assert entry["claim_scope"] == "bounded_proxy_probe_only"
        assert entry["remote"]["host"] == "EXAMPLE_WORKER_A"
        assert "artifacts/maskout_clean_token_probe.csv" in entry["produced_artifacts"]
        assert entry["model_execution_evidence"]["executed_model"] == "GenD_CLIP_L_14"
        assert entry["model_execution_evidence"]["formal_evidence"] is False

        write_context_documents(root)
        build_context_index(root)
        query = query_context_index(root, "GenD_DINOv3_L bounded_proxy_probe_only", limit=3)
        assert query["results"][0]["doc_id"] == "artifact_index:gend_004"
        assert "bounded_proxy_probe_only" in query["results"][0]["text"]
        assert "GenD_CLIP_L_14" in query["results"][0]["text"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_context_index_cli_is_manual_and_local():
    root = _tmp_root()
    try:
        _write_demo_sources(root)
        runner = CliRunner()

        docs_result = runner.invoke(cli, ["context-index", "build-docs", "--repo-root", str(root)])
        assert docs_result.exit_code == 0, docs_result.output
        build_result = runner.invoke(cli, ["context-index", "build", "--repo-root", str(root)])
        assert build_result.exit_code == 0, build_result.output
        query_result = runner.invoke(cli, ["context-index", "query", "--repo-root", str(root), "evaluation metrics"])
        assert query_result.exit_code == 0, query_result.output
        payload = json.loads(query_result.output)
        assert payload["results"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_context_index_cli_build_creates_missing_documents():
    root = _tmp_root()
    try:
        _write_demo_sources(root)
        runner = CliRunner()

        build_result = runner.invoke(cli, ["context-index", "build", "--repo-root", str(root)])

        assert build_result.exit_code == 0, build_result.output
        payload = json.loads(build_result.output)
        assert payload["doc_count"] >= 3
        assert payload["documents"]["doc_count"] == payload["doc_count"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_context_index_cli_build_refreshes_stale_documents():
    root = _tmp_root()
    try:
        _write_demo_sources(root)
        runner = CliRunner()

        first = runner.invoke(cli, ["context-index", "build", "--repo-root", str(root)])
        assert first.exit_code == 0, first.output
        time.sleep(0.02)
        artifact_path = root / ".solution" / "context" / "artifact_index.jsonl"
        artifact_path.write_text(
            json.dumps({"task_id": "new_cli_refresh", "summary": "new result after cli build"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        second = runner.invoke(cli, ["context-index", "build", "--repo-root", str(root)])

        assert second.exit_code == 0, second.output
        payload = json.loads(second.output)
        assert payload["documents"]["doc_count"] == payload["doc_count"]
        assert context_index_status(root)["stale"]["dirty"] is False
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_context_pack_includes_retrieval_results_when_index_exists():
    root = _tmp_root()
    try:
        _write_demo_sources(root)
        write_context_documents(root)
        build_context_index(root)
        task_path = root / "tasks" / "pending" / "eval_task" / "task.md"
        task_path.parent.mkdir(parents=True)
        task_path.write_text(
            "---\ntask_id: eval_task\nworker: materialized_execution\n---\nRun df40 evaluation metrics with the eval probe.\n",
            encoding="utf-8",
        )

        manifest = build_context_pack(task_id="eval_task", task_path=task_path, repo_root=root, out_dir=root / "out")

        retrieval_path = root / "out" / "context_pack" / "retrieval_context.json"
        payload = json.loads(retrieval_path.read_text(encoding="utf-8"))
        assert payload["status"] == "used"
        assert payload["result_count"] >= 1
        assert manifest["retrieval"]["status"] == "used"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_context_index_reranks_exact_script_id_matches_first():
    root = _tmp_root()
    try:
        _write_demo_sources(root)
        write_context_documents(root)
        build_context_index(root)

        result = query_context_index(root, "run_eval_probe evaluation metrics", limit=3)

        assert result["results"][0]["doc_id"] == "script_registry:run_eval_probe"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_context_index_can_retrieve_lineage_and_best_checkpoint_nodes():
    root = _tmp_root()
    try:
        _write_demo_sources(root)
        artifact_path = root / ".solution" / "context" / "artifact_index.jsonl"
        artifact_path.write_text(
            json.dumps(
                {
                    "task_id": "finetune_delta1518_stage3",
                    "status": "completed",
                    "summary": "Stage 3 finetune improved DF40 video AUC.",
                    "conclusion": "Use the stage3 best checkpoint for the next evaluation branch.",
                    "script_ids": ["run_recipe_ablation"],
                    "parent_task_ids": ["finetune_delta1518_stage2"],
                    "best_checkpoint": {
                        "id": "delta1518_stage3_best",
                        "path": "outputs/finetune_delta1518_stage3/checkpoints/best.ckpt",
                        "metric": "video_auc",
                        "value": 0.932,
                    },
                    "config_paths": ["configs/delta1518/stage3.yaml"],
                    "code_paths": ["scripts/train_delta1518.py"],
                    "important_paths": ["outputs/finetune_delta1518_stage3/summary.md"],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        write_context_documents(root)
        build_context_index(root)

        by_child = query_context_index(root, "finetune_delta1518_stage3 best checkpoint video_auc", limit=3)
        by_parent = query_context_index(root, "finetune_delta1518_stage2 next branch best ckpt", limit=3)

        assert by_child["results"][0]["doc_id"] == "artifact_index:finetune_delta1518_stage3"
        assert any(item["doc_id"] == "artifact_index:finetune_delta1518_stage3" for item in by_parent["results"])
        top_text = by_child["results"][0]["text"]
        assert "outputs/finetune_delta1518_stage3/checkpoints/best.ckpt" in top_text
        assert "scripts/train_delta1518.py" in top_text
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_context_index_supports_inference_nodes_without_best_checkpoint():
    root = _tmp_root()
    try:
        _write_demo_sources(root)
        artifact_path = root / ".solution" / "context" / "artifact_index.jsonl"
        artifact_path.write_text(
            json.dumps(
                {
                    "task_id": "infer_delta1518_stage3_df40",
                    "status": "completed",
                    "experiment_kind": "inference",
                    "summary": "Inference on DF40 completed using the stage3 checkpoint.",
                    "conclusion": "Predictions and metrics can be used for threshold analysis.",
                    "script_ids": ["run_eval_probe"],
                    "parent_task_ids": ["finetune_delta1518_stage3"],
                    "consumed_artifacts": [
                        {
                            "id": "delta1518_stage3_best",
                            "type": "checkpoint",
                            "path": "outputs/finetune_delta1518_stage3/checkpoints/best.ckpt",
                            "role": "model_input",
                        }
                    ],
                    "produced_artifacts": [
                        {
                            "id": "df40_predictions",
                            "type": "predictions",
                            "path": "outputs/infer_delta1518_stage3_df40/predictions.parquet",
                            "role": "inference_output",
                        },
                        {
                            "id": "df40_metrics",
                            "type": "metrics",
                            "path": "outputs/infer_delta1518_stage3_df40/metrics.json",
                            "role": "evaluation_summary",
                        },
                    ],
                    "config_paths": ["configs/delta1518/infer_df40.yaml"],
                    "code_paths": ["scripts/eval_delta1518.py"],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        write_context_documents(root)
        build_context_index(root)

        by_prediction = query_context_index(root, "df40_predictions inference output", limit=3)
        by_checkpoint_input = query_context_index(root, "delta1518_stage3_best consumed checkpoint inference", limit=3)

        assert by_prediction["results"][0]["doc_id"] == "artifact_index:infer_delta1518_stage3_df40"
        assert by_checkpoint_input["results"][0]["doc_id"] == "artifact_index:infer_delta1518_stage3_df40"
        assert "predictions.parquet" in by_prediction["results"][0]["text"]
        assert "best.ckpt" in by_checkpoint_input["results"][0]["text"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_context_index_retrieves_data_inventory_and_capability_matrix():
    root = _tmp_root()
    try:
        _write_demo_sources(root)
        context_root = root / ".solution" / "context"
        (context_root / "data_inventory.yaml").write_text(
            """schema_version: 1
project_id: spatial_demo
confidence:
  overall: reviewed
feature_caches:
  - id: dinov3_hs20_tokens
    kind: feature_cache
    path: /features/dinov3_hs20
    status: available
    feature_space: DINOv3 hs20
    reusable_for:
      - spatial_node_type_audit
datasets:
  - id: ddl_i_masks
    kind: image_dataset
    path: /datasets/ddl_i
    contains:
      - images
      - masks
""",
            encoding="utf-8",
        )
        (context_root / "capability_matrix.yaml").write_text(
            """schema_version: 1
project_id: spatial_demo
confidence:
  overall: reviewed
capabilities:
  - id: node_type_audit
    script_id: run_node_type_audit
    path: scripts/probes/run_node_type_audit.py
    can_do:
      - cluster token features
      - render cluster overlay sheet
    can_produce:
      - node_table.parquet
      - cluster_summary.csv
      - cluster_overlay_sheet.jpg
    limitations:
      - does not implement residual fusion
""",
            encoding="utf-8",
        )
        write_context_documents(root)
        build_context_index(root)

        feature_result = query_context_index(root, "DINOv3 hs20 feature cache spatial node audit", limit=5)
        capability_result = query_context_index(root, "node_table parquet cluster overlay residual fusion", limit=5)

        assert any(item["doc_id"] == "data_inventory:feature_caches:dinov3_hs20_tokens" for item in feature_result["results"])
        assert any(item["doc_id"] == "capability_matrix:capabilities:node_type_audit" for item in capability_result["results"])
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_context_index_prefers_recent_artifact_tail_when_capped():
    root = _tmp_root()
    try:
        _write_demo_sources(root)
        artifact_path = root / ".solution" / "context" / "artifact_index.jsonl"
        lines = []
        for idx in range(200):
            lines.append(
                json.dumps(
                    {
                        "task_id": f"noise_{idx:03d}",
                        "status": "completed",
                        "summary": "generic historical noise metrics",
                    },
                    ensure_ascii=False,
                )
            )
        lines.append(
            json.dumps(
                {
                    "task_id": "recent_important_eval",
                    "status": "completed",
                    "summary": "Recent important eval produced rare_metric_x and final report.",
                    "produced_artifacts": [{"id": "rare_metric_x", "type": "metrics", "path": "outputs/recent/metrics.json"}],
                },
                ensure_ascii=False,
            )
        )
        artifact_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        docs = write_context_documents(root, max_docs=20)
        build_context_index(root)
        result = query_context_index(root, "rare_metric_x recent important eval", limit=3)

        assert docs["doc_count"] == 20
        assert result["results"][0]["doc_id"] == "artifact_index:recent_important_eval"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_context_index_keeps_latest_duplicate_artifact_record():
    root = _tmp_root()
    try:
        _write_demo_sources(root)
        artifact_path = root / ".solution" / "context" / "artifact_index.jsonl"
        artifact_path.write_text(
            json.dumps({"task_id": "dup_stage", "summary": "old duplicate", "produced_artifacts": [{"id": "old_ckpt", "path": "outputs/old.ckpt"}]}, ensure_ascii=False)
            + "\n"
            + json.dumps({"task_id": "dup_stage", "summary": "new duplicate", "produced_artifacts": [{"id": "new_ckpt", "path": "outputs/new.ckpt"}]}, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )

        write_context_documents(root)
        build_context_index(root)
        new_result = query_context_index(root, "new_ckpt duplicate", limit=5)
        old_result = query_context_index(root, "old_ckpt duplicate", limit=5)

        assert new_result["results"][0]["doc_id"] == "artifact_index:dup_stage"
        assert "new_ckpt" in new_result["results"][0]["text"]
        assert not old_result["results"] or "old_ckpt" not in old_result["results"][0]["text"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_context_index_status_reports_dirty_after_source_update():
    root = _tmp_root()
    try:
        _write_demo_sources(root)
        write_context_documents(root)
        build_context_index(root)
        clean = context_index_status(root)
        assert clean["stale"]["dirty"] is False

        artifact_path = root / ".solution" / "context" / "artifact_index.jsonl"
        artifact_path.write_text(
            json.dumps({"task_id": "new_after_index", "summary": "new result after index"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.utime(artifact_path, None)
        dirty = context_index_status(root)

        assert dirty["stale"]["dirty"] is True
        assert dirty["stale"]["documents_stale"] is True
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_remote_snapshot_keeps_checkpoint_roles_as_role_to_path():
    root = _tmp_root()
    try:
        known_path = "/remote/runs/cls.ckpt"
        profile = {
            "profile_name": "demo_remote",
            "remote_workdir": "/remote/project",
            "remote_output_root": "/remote/runs",
            "project_assets": {
                "checkpoint_roots": ["/remote/runs"],
                "known_checkpoints": {
                    "cls_debug_best": known_path,
                    "seg_debug_best": "/remote/runs/missing_seg.ckpt",
                },
            },
        }
        stdout = "\n".join(
            [
                "__CHECKPOINTS__",
                f"1000\t123\t{known_path}",
                "__RESULTS__",
            ]
        )

        with patch("solution_agent.context_index._remote_profile", return_value=profile), patch(
            "solution_agent.context_index.subprocess.run",
            return_value=subprocess.CompletedProcess(["ssh"], 0, stdout=stdout, stderr=""),
        ):
            result = snapshot_remote_workplace(root, host="DemoHost", remote_root="/remote/runs", remote_workdir="/remote/project")

        assert result["ok"] is True
        artifact_path = root / ".solution" / "context" / "artifact_index.jsonl"
        entry = json.loads(artifact_path.read_text(encoding="utf-8").splitlines()[-1])
        roles = entry["known_checkpoint_roles"]
        assert {"role": "cls_debug_best", "path": known_path, "source": "remote_execution_profiles.project_assets.known_checkpoints"} in roles
        assert any(item["role"] == "seg_debug_best" and item["path"] == "/remote/runs/missing_seg.ckpt" and item.get("observed") is False for item in roles)
        assert not any(str(item.get("role", "")).startswith("/remote/") for item in roles)
        assert entry["checkpoint_selection_policy"]
    finally:
        shutil.rmtree(root, ignore_errors=True)
