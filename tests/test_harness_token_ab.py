from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "harness" / "run_token_ab.py"
SPEC = importlib.util.spec_from_file_location("run_token_ab_test", SCRIPT)
runner = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(runner)


def _record(variant: str, cached: int, new_input: int) -> dict:
    return {
        "variant": variant,
        "usage": {
            "cache_read_tokens": cached,
            "input_tokens": cached + new_input,
            "output_tokens": 10,
        },
    }


def test_cache_topup_selects_first_balanced_pair_not_lowest_usage() -> None:
    early_baseline = _record("baseline", 0, 1000)
    candidate = _record("candidate", 100, 100)
    expensive_balanced_baseline = _record("baseline", 105, 5000)
    cheaper_later_baseline = _record("baseline", 102, 200)
    pair = runner._first_cache_matched_pair(
        [early_baseline, candidate, expensive_balanced_baseline, cheaper_later_baseline]
    )
    assert pair == (expensive_balanced_baseline, candidate)


def test_event_parser_requires_usage_and_json_outcome() -> None:
    outcome = {"case_id": "x", "execution_status": "succeeded"}
    stream = "\n".join(
        [
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(outcome)}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}}),
        ]
    )
    usage, parsed = runner._parse_events(stream)
    assert usage["input_tokens"] == 10
    assert parsed == outcome


def test_synthetic_fixture_builder_and_validator(tmp_path: Path) -> None:
    manifest = runner.build(tmp_path)
    assert len(manifest["cases"]) == 3
    for item in manifest["cases"]:
        case_dir = tmp_path / item["case_id"]
        compact = json.loads((case_dir / "compact_evidence.json").read_text(encoding="utf-8"))
        runner._validate_synthetic_fixture(case_dir, compact)
