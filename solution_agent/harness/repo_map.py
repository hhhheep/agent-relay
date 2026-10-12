from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path


_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]+|[\u3400-\u9fff]{2,}")


@dataclass(frozen=True)
class RepoMapEntry:
    path: str
    score: float
    symbols: tuple[str, ...]
    imports: tuple[str, ...]


def _tokens(value: str) -> set[str]:
    return {match.group(0).lower() for match in _TOKEN.finditer(value)}


def _python_facts(path: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(text)
    except (OSError, SyntaxError, ValueError):
        return (), ()
    symbols: list[str] = []
    imports: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.append(node.name)
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    return tuple(symbols[:24]), tuple(imports[:24])


def rank_repo_files(
    repo_root: Path,
    relative_paths: list[str],
    query: str,
    *,
    limit: int = 80,
) -> list[RepoMapEntry]:
    """Return a bounded Aider-style symbol/dependency ranking.

    This is intentionally static and deterministic: no model call, no source
    mutation, and no content outside the already-filtered candidate paths.
    """

    query_tokens = _tokens(query)
    facts: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    module_to_path: dict[str, str] = {}
    for relative in sorted(set(relative_paths)):
        path = repo_root / relative
        symbols, imports = _python_facts(path) if path.suffix.lower() == ".py" else ((), ())
        facts[relative] = (symbols, imports)
        if path.suffix.lower() == ".py":
            module = relative[:-3].replace("/", ".").replace("\\", ".")
            module_to_path[module] = relative
            module_to_path.setdefault(path.stem, relative)

    incoming: dict[str, int] = {relative: 0 for relative in facts}
    for _, imports in facts.values():
        for imported in imports:
            target = module_to_path.get(imported)
            if target is None:
                matches = [path for module, path in module_to_path.items() if module.endswith("." + imported)]
                target = sorted(matches)[0] if matches else None
            if target is not None:
                incoming[target] += 1

    ranked: list[RepoMapEntry] = []
    for relative, (symbols, imports) in facts.items():
        path_tokens = _tokens(relative)
        symbol_tokens = _tokens(" ".join(symbols))
        overlap = len(query_tokens & path_tokens) * 8 + len(query_tokens & symbol_tokens) * 12
        exact_stem = 15 if Path(relative).stem.lower() in query_tokens else 0
        dependency_score = min(incoming.get(relative, 0), 12) * 1.5
        root_bonus = 2 if relative.startswith("solution_agent/") else 0
        score = float(overlap + exact_stem + dependency_score + root_bonus)
        ranked.append(RepoMapEntry(relative, score, symbols, imports))

    ranked.sort(key=lambda item: (-item.score, item.path))
    return ranked[: max(1, limit)]


def render_repo_map(entries: list[RepoMapEntry]) -> str:
    lines = [
        "# Ranked Repo Map Excerpt",
        "",
        "Static symbol/dependency ranking; scores are retrieval hints, not evidence.",
        "",
    ]
    for entry in entries:
        details: list[str] = []
        if entry.symbols:
            details.append("symbols: " + ", ".join(entry.symbols[:10]))
        if entry.imports:
            details.append("imports: " + ", ".join(entry.imports[:6]))
        suffix = " :: " + " | ".join(details) if details else ""
        lines.append(f"- {entry.path} [score={entry.score:.1f}]{suffix}")
    lines.append("")
    return "\n".join(lines)
