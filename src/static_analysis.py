"""
static_analysis.py — parses each .py file into an AST and extracts its
structure: imports, top-level functions, and classes (with their methods).

WHY ast, not regex?
- Regex like `import\\s+(\\w+)` breaks on multi-line imports, on the word
  "import" appearing inside a string/comment, on `from x import (a, b, c)`
  spanning lines, etc. It's pattern-matching text, not understanding code.
- `ast` is what the Python interpreter itself uses to parse source into a
  tree before compiling it. Parsing with `ast` means we get the same
  structural understanding the interpreter has — no false positives,
  no missed multi-line cases.
- This is the difference between "I grepped the code" and "I parsed the
  code" — an interviewer will notice which one you did.

KNOWN LIMITATION (be upfront about this — it's a legitimate scoping call,
not a bug you're hiding):
- ast.walk() finds FunctionDef nodes at every depth, including methods
  inside classes. So a class's methods show up both inside that class's
  entry AND (unless filtered) in the flat "functions" list. We filter
  functions to top-level (module-level) only, and capture methods
  separately under each class, so the two lists don't overlap. If you
  don't do this filtering, be ready to explain why not.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ClassInfo:
    name: str
    methods: list[str] = field(default_factory=list)
    docstring: str | None = None


@dataclass
class FileAnalysis:
    path: str  # relative path
    imports: list[str] = field(default_factory=list)  # module names, e.g. "os", "pandas"
    from_imports: list[str] = field(default_factory=list)  # "package.module" for `from x import y`
    functions: list[str] = field(default_factory=list)  # top-level functions only
    classes: list[ClassInfo] = field(default_factory=list)
    docstring: str | None = None
    parse_error: str | None = None
    has_main_guard: bool = False  # AST-detected `if __name__ == "__main__":`


def analyze_file(path: Path, relative_path: str) -> FileAnalysis:
    result = FileAnalysis(path=relative_path)
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative_path)
    except (SyntaxError, UnicodeDecodeError) as e:
        result.parse_error = str(e)
        return result

    result.docstring = ast.get_docstring(tree)

    for node in ast.iter_child_nodes(tree):
        # Only TOP-LEVEL nodes here (iter_child_nodes, not ast.walk) —
        # this is what keeps functions/classes cleanly separated.
        if isinstance(node, ast.Import):
            for alias in node.names:
                result.imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                result.from_imports.append(node.module)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result.functions.append(node.name)
        elif isinstance(node, ast.ClassDef):
            methods = [
                n.name
                for n in ast.iter_child_nodes(node)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            result.classes.append(
                ClassInfo(name=node.name, methods=methods, docstring=ast.get_docstring(node))
            )
        elif isinstance(node, ast.If) and _is_main_guard(node):
            # Detects the `if __name__ == "__main__":` idiom at the AST
            # level — a real Python convention (the interpreter sets
            # __name__ to "__main__" only when a file is run directly,
            # not when imported) that a generic/TS-oriented tool has no
            # reason to know about. This is the strongest signal a file
            # is meant to be an entry point, not just a module to import.
            result.has_main_guard = True

    return result


def _is_main_guard(node: ast.If) -> bool:
    """True if node.test is (a form of) `__name__ == "__main__"`."""
    test = node.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return False
    left, right = test.left, test.comparators[0]
    names_and_consts = {
        n.id if isinstance(n, ast.Name) else (n.value if isinstance(n, ast.Constant) else None)
        for n in (left, right)
    }
    return {"__name__", "__main__"} <= names_and_consts


def analyze_repo(root: Path, python_files: list[Path]) -> list[FileAnalysis]:
    analyses = []
    for f in python_files:
        rel = str(f.relative_to(root))
        analyses.append(analyze_file(f, rel))
    return analyses


if __name__ == "__main__":
    import sys
    from ingest import ingest

    url = sys.argv[1] if len(sys.argv) > 1 else "https://github.com/octocat/Hello-World"
    repo = ingest(url)
    for analysis in analyze_repo(repo.local_path, repo.python_files):
        print(f"\n=== {analysis.path} ===")
        if analysis.parse_error:
            print(f"  PARSE ERROR: {analysis.parse_error}")
            continue
        print(f"  imports: {analysis.imports}")
        print(f"  from_imports: {analysis.from_imports}")
        print(f"  functions: {analysis.functions}")
        print(f"  classes: {[(c.name, c.methods) for c in analysis.classes]}")