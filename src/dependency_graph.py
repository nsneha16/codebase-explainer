"""
dependency_graph.py — turns raw imports into a "file A depends on file B"
graph, using ONLY imports that resolve to files inside the repo.

WHY filter out external packages (pandas, numpy, requests, ...)?
- If we graph every import as an edge, the graph becomes
  "file -> external package" for most edges, which tells you nothing
  about how the repo's OWN files relate to each other. The useful
  signal — "does auth.py get used by main.py?" — gets buried in noise.
- So: build a lookup of "module name -> file in this repo" first, then
  only keep an import as a graph edge if it resolves against that lookup.

HOW resolution works:
- For every .py file in the repo, its "importable name" is its dotted
  path relative to the repo root, minus the .py extension, with an
  __init__.py collapsing to its parent package name.
  e.g. src/utils/helpers.py -> "src.utils.helpers"
       src/utils/__init__.py -> "src.utils"
- For each file's `import x` or `from x import y`, we check if `x`
  (or any prefix of it) matches an internal module name. If yes -> edge.
  If no -> it's external, dropped from the graph (but still shown
  separately as "external dependencies" for context).

SIBLING-FILE RESOLUTION (relative imports without dots):
- `from context import Foo` (no leading dots, bare name) is how Python
  code inside a package commonly imports a same-folder sibling file —
  it works at runtime because the package's own directory is on
  sys.path when it's the thing being executed/tested.
- Our dotted-path lookup alone misses this: "context" doesn't match
  "tests.context" as a full name OR a prefix, so it silently gets
  dropped as "external." That's wrong — it's not an external package,
  we just weren't checking the right candidate.
- Fix: when a bare (dot-free) import name doesn't resolve globally,
  also check whether it matches a file in the *same directory* as the
  importing file. This mirrors how Python actually resolves it at
  runtime for script-style/test-style imports.
- We deliberately do NOT attempt full relative-import resolution
  (`from . import x`, `from ..pkg import y` — leading-dot syntax);
  that requires tracking package depth and is scoped out as a known
  limitation for a good reason (time-boxed MVP), not an oversight.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from static_analysis import FileAnalysis


@dataclass
class DependencyGraph:
    # file path -> set of internal file paths it depends on
    edges: dict[str, set[str]] = field(default_factory=dict)
    # file path -> set of external package names it uses
    external_deps: dict[str, set[str]] = field(default_factory=dict)


def _module_name_for(relative_path: str) -> str:
    """src/utils/helpers.py -> src.utils.helpers ; src/utils/__init__.py -> src.utils"""
    p = Path(relative_path)
    parts = list(p.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = p.stem
    return ".".join(parts)


def build_module_lookup(analyses: list[FileAnalysis]) -> dict[str, str]:
    """module dotted-name -> relative file path, for every file in the repo."""
    lookup = {}
    for a in analyses:
        lookup[_module_name_for(a.path)] = a.path
    return lookup


def _resolve(imported_name: str, lookup: dict[str, str], importer_path: str) -> str | None:
    """Check if imported_name (or a prefix of it, for `from x.y import z` style)
    matches an internal module. Returns the internal file path, or None.

    importer_path is the file doing the importing — needed for the
    sibling-file fallback (see module docstring)."""
    if imported_name in lookup:
        return lookup[imported_name]
    # try progressively shorter prefixes: "src.utils.helpers" -> "src.utils" -> "src"
    parts = imported_name.split(".")
    while len(parts) > 1:
        parts.pop()
        candidate = ".".join(parts)
        if candidate in lookup:
            return lookup[candidate]

    # Sibling-file fallback: only applies to bare, dot-free names
    # ("context", not "tests.context") — those are the ones that could
    # plausibly mean "the file next to me."
    if "." not in imported_name:
        sibling = str(Path(importer_path).parent / f"{imported_name}.py")
        # normalize "./foo.py" -> "foo.py" for files at repo root
        sibling = sibling[2:] if sibling.startswith("./") else sibling
        if sibling in lookup.values():
            return sibling

    return None


def build_dependency_graph(analyses: list[FileAnalysis]) -> DependencyGraph:
    lookup = build_module_lookup(analyses)
    graph = DependencyGraph()

    for a in analyses:
        graph.edges.setdefault(a.path, set())
        graph.external_deps.setdefault(a.path, set())

        all_imports = a.imports + a.from_imports
        for imported in all_imports:
            resolved = _resolve(imported, lookup, a.path)
            if resolved and resolved != a.path:
                graph.edges[a.path].add(resolved)
            elif not resolved:
                # top-level package name only, e.g. "matplotlib.pyplot" -> "matplotlib"
                graph.external_deps[a.path].add(imported.split(".")[0])

    return graph


def to_mermaid(graph: DependencyGraph) -> str:
    """Render internal edges as a Mermaid flowchart (for the README / demo)."""
    lines = ["flowchart LR"]
    safe = lambda s: s.replace("/", "_").replace(".", "_").replace("-", "_")
    for src, targets in graph.edges.items():
        if not targets:
            lines.append(f'    {safe(src)}["{src}"]')
        for t in targets:
            lines.append(f'    {safe(src)}["{src}"] --> {safe(t)}["{t}"]')
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    from ingest import ingest
    from static_analysis import analyze_repo

    url = sys.argv[1] if len(sys.argv) > 1 else "https://github.com/octocat/Hello-World"
    repo = ingest(url)
    analyses = analyze_repo(repo.local_path, repo.python_files)
    graph = build_dependency_graph(analyses)

    for f, deps in graph.edges.items():
        ext = graph.external_deps.get(f, set())
        print(f"{f}")
        print(f"  internal deps: {sorted(deps) if deps else '(none)'}")
        print(f"  external deps: {sorted(ext) if ext else '(none)'}")

    print("\n--- Mermaid diagram ---")
    print(to_mermaid(graph))