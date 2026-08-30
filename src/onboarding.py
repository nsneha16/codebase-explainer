"""
onboarding.py — the features that make this an ONBOARDING tool, not just
a codebase explainer: where should a new developer start reading, and
what does this project actually depend on.

WHY this is the differentiator against generic (often TypeScript-heavy)
codebase-explainer tools:
- Entry-point detection here is grounded in real Python conventions: the
  `if __name__ == "__main__":` guard is a language-level idiom (the
  interpreter only sets __name__ to "__main__" for the directly-executed
  file), and framework files like manage.py (Django) or wsgi.py/asgi.py
  (WSGI/ASGI servers) are Python-ecosystem-specific. A generic tool
  built for a JS/TS-first market has no reason to encode this.
- "Suggested reading order" is Python's actual import graph (from
  dependency_graph.py) turned into a ranking: files many other files
  depend on are foundational — read them first. This only works because
  we did AST-based import resolution earlier, not a doc/comment heuristic.
- Manifest parsing (requirements.txt / pyproject.toml) surfaces the
  project's REAL external dependencies, not "we found the word 'import'
  a lot" — package.json-first tools don't parse these formats at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from dependency_graph import DependencyGraph
from static_analysis import FileAnalysis

ENTRY_POINT_FILENAMES = {
    "main.py", "app.py", "manage.py", "wsgi.py", "asgi.py", "run.py", "cli.py", "__main__.py",
}


@dataclass
class EntryPoint:
    path: str
    reason: str  # "filename convention" or "if __name__ == '__main__' guard"


def detect_entry_points(analyses: list[FileAnalysis]) -> list[EntryPoint]:
    found: dict[str, str] = {}
    for a in analyses:
        filename = Path(a.path).name
        if filename in ENTRY_POINT_FILENAMES:
            found[a.path] = "filename convention"
        elif a.has_main_guard:
            found[a.path] = 'if __name__ == "__main__" guard'
    return [EntryPoint(path=p, reason=r) for p, r in sorted(found.items())]


def _is_test_file(path: str) -> bool:
    p = Path(path)
    return p.name.startswith("test_") or p.name.endswith("_test.py") or "tests" in p.parts or "test" in p.parts


def suggested_reading_order(
    analyses: list[FileAnalysis], graph: DependencyGraph, entry_points: list[EntryPoint], limit: int = 15
) -> list[str]:
    """Rank files: non-test entry points first, then non-test files by how
    many OTHER files depend on them (in-degree — foundational code), then
    test files last. This is the dependency graph read in reverse: an
    edge A->B means A imports B, so B's in-degree is "how many files
    import me" — a direct proxy for "how core is this file to the
    project."

    A test file that calls unittest.main() also has an `if __name__ ==
    "__main__":` guard, which makes it a legitimate entry point by our
    detection rule — but "start by reading the tests" is bad onboarding
    advice. So a test file never gets entry-point priority here, even
    though detect_entry_points() correctly (and separately) still lists
    it as one; ranking and detection are allowed to disagree."""
    entry_paths = {e.path for e in entry_points}
    in_degree: dict[str, int] = {a.path: 0 for a in analyses if not a.parse_error}
    for _src, targets in graph.edges.items():
        for t in targets:
            if t in in_degree:
                in_degree[t] += 1

    def sort_key(path: str) -> tuple:
        is_test = _is_test_file(path)
        is_priority_entry = path in entry_paths and not is_test
        group = 0 if is_priority_entry else (2 if is_test else 1)
        return (group, -in_degree.get(path, 0), path)

    ordered = sorted(in_degree.keys(), key=sort_key)
    return ordered[:limit]


def parse_manifest(repo_root: Path) -> tuple[str, list[str]]:
    """Look for requirements.txt or pyproject.toml at the repo root and
    return (manifest_filename, [package_names]). Returns ("", []) if
    neither is found — deliberately root-only, not a recursive search,
    since a manifest anywhere else in the tree is very likely a nested
    example/vendor project's, not the repo's own."""
    req_file = repo_root / "requirements.txt"
    if req_file.exists():
        packages = []
        for line in req_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            name = re.split(r"[=<>!~\[; ]", line, maxsplit=1)[0].strip()
            if name:
                packages.append(name)
        return "requirements.txt", packages

    pyproject = repo_root / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8", errors="replace")
        # Lightweight, not a full TOML parse: pull quoted package names out
        # of a `dependencies = [...]` array (covers PEP 621 / Poetry's
        # common style). Good enough for "what does this depend on" at a
        # glance; a malformed/unusual pyproject.toml just yields [].
        match = re.search(r"dependencies\s*=\s*\[(.*?)\]", text, re.DOTALL)
        if match:
            names = re.findall(r'["\']([\w\-\.]+)', match.group(1))
            return "pyproject.toml", [re.split(r"[=<>!~\[]", n)[0] for n in names]
        return "pyproject.toml", []

    return "", []