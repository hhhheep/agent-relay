from __future__ import annotations

from pathlib import Path

from solution_agent.harness.repo_map import rank_repo_files, render_repo_map


def test_repo_map_ranks_query_symbol_and_shows_dependency_context(tmp_path: Path) -> None:
    package = tmp_path / "solution_agent"
    package.mkdir()
    (package / "retry_controller.py").write_text(
        "class RetryController:\n    pass\n",
        encoding="utf-8",
    )
    (package / "runner.py").write_text(
        "from solution_agent.retry_controller import RetryController\n\ndef run():\n    return RetryController()\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("general notes", encoding="utf-8")
    paths = ["solution_agent/retry_controller.py", "solution_agent/runner.py", "README.md"]
    entries = rank_repo_files(tmp_path, paths, "fix RetryController loop")
    assert entries[0].path == "solution_agent/retry_controller.py"
    rendered = render_repo_map(entries)
    assert "symbols: RetryController" in rendered
    assert "solution_agent.retry_controller" in rendered


def test_repo_map_is_deterministic_and_bounded(tmp_path: Path) -> None:
    for index in range(5):
        (tmp_path / f"f{index}.py").write_text(f"def symbol_{index}():\n    pass\n", encoding="utf-8")
    paths = [f"f{index}.py" for index in range(5)]
    first = rank_repo_files(tmp_path, paths, "symbol_3", limit=2)
    second = rank_repo_files(tmp_path, list(reversed(paths)), "symbol_3", limit=2)
    assert first == second
    assert len(first) == 2
    assert first[0].path == "f3.py"
