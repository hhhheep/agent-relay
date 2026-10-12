from __future__ import annotations

import csv
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    _, raw, _ = text.split("---", 2)
    return yaml.safe_load(raw)


def test_harness_plugin_exposes_one_focused_skill() -> None:
    skills_root = ROOT / "plugins" / "solution-agent-harness" / "skills"
    skills = sorted(skills_root.glob("*/SKILL.md"))
    assert [path.parent.name for path in skills] == ["solution-agent-harness"]
    description = _frontmatter(skills[0])["description"]
    assert len(description) < 400
    assert "ordinary local work" in description
    assert (skills[0].parent / "references" / "project-routing.md").is_file()
    assert (skills[0].parent / "references" / "feedback.md").is_file()


def test_complementary_skills_have_negative_activation_boundaries() -> None:
    token = _frontmatter(WORKSPACE / "tools" / "research-token-guard" / "skills" / "research-token-guard" / "SKILL.md")
    writer = _frontmatter(WORKSPACE / "tools" / "research-report-writer-plugin" / "skills" / "research-report-writer" / "SKILL.md")
    assert "ordinary small commands" in token["description"]
    assert "ordinary Markdown edits" in writer["description"]
    assert len(token["description"]) < 400
    assert len(writer["description"]) < 400


def test_legacy_ncku_route_is_explicit_only() -> None:
    policy = yaml.safe_load(
        (ROOT / "deploy" / "user-skills" / "example-remote-codex" / "agents" / "openai.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert policy["policy"]["allow_implicit_invocation"] is False


def test_routing_eval_has_positive_and_negative_controls() -> None:
    with (ROOT / "evals" / "skill-routing.prompts.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) >= 10
    assert {row["should_trigger"] for row in rows} == {"true", "false"}
    assert "solution-agent-feedback" not in {row["expected_skill"] for row in rows}
    assert {"solution-agent-harness", "research-token-guard", "research-report-writer"} <= {
        row["expected_skill"] for row in rows
    }
