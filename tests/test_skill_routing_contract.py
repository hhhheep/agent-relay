from __future__ import annotations

import csv
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


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
    token = _frontmatter(ROOT / "plugins" / "research-token-guard" / "skills" / "research-token-guard" / "SKILL.md")
    writer = _frontmatter(ROOT / "plugins" / "research-report-writer" / "skills" / "research-report-writer" / "SKILL.md")
    assert "ordinary small commands" in token["description"]
    assert "ordinary Markdown edits" in writer["description"]
    assert len(token["description"]) < 400
    assert len(writer["description"]) < 400


def test_undistributed_legacy_route_cannot_be_selected_by_packaged_skills() -> None:
    legacy = "example-remote-codex"
    assert not (ROOT / "deploy" / "user-skills" / legacy).exists()
    for manifest in ("overlay-files.txt", "plugin-overlay-files.txt"):
        entries = (ROOT / "deploy" / manifest).read_text(encoding="utf-8").splitlines()
        assert entries
        assert not any(legacy in entry for entry in entries)
    skills = list((ROOT / "plugins").glob("*/skills/*/SKILL.md"))
    assert skills
    assert legacy not in {_frontmatter(path)["name"] for path in skills}
    with (ROOT / "evals" / "skill-routing.prompts.csv").open(encoding="utf-8", newline="") as handle:
        assert legacy not in {row["expected_skill"] for row in csv.DictReader(handle)}


def test_routing_eval_has_positive_and_negative_controls() -> None:
    with (ROOT / "evals" / "skill-routing.prompts.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) >= 10
    assert {row["should_trigger"] for row in rows} == {"true", "false"}
    assert "solution-agent-feedback" not in {row["expected_skill"] for row in rows}
    assert {"solution-agent-harness", "research-token-guard", "research-report-writer"} <= {
        row["expected_skill"] for row in rows
    }
