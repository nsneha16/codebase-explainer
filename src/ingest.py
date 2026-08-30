"""
ingest.py — clones a GitHub repo locally and returns a filtered list of
Python files to analyze.

WHY clone instead of hitting the GitHub API per-file?
- Unauthenticated GitHub API = 60 requests/hour. One repo can easily have
  50+ files, so you'd burn your rate limit fast.
- Cloning is a single network operation; after that, everything is local
  disk I/O, which is fast and has no rate limit.
- Trade-off: disk space + clone time. Fine for a one-shot analysis tool.

WHY exclude .git, __pycache__, venv/.venv, node_modules?
- These are generated/dependency directories, not "the codebase" a human
  wrote. Including them would distort the file tree and dependency graph
  with noise that has nothing to do with the project's actual structure.
"""
from __future__ import annotations
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from git import Repo

EXCLUDED_DIRS = {".git", "__pycache__", "venv", ".venv", "node_modules", ".mypy_cache", ".pytest_cache"}
INCLUDED_EXTENSIONS = {".py"}


@dataclass
class IngestedRepo:
    repo_url: str
    local_path: Path
    python_files: list[Path]  # absolute paths
    relative_files: list[str]  # paths relative to repo root, for display


def clone_repo(repo_url: str, dest_dir: str | None = None) -> Path:
    """Clone repo_url into a temp dir (or dest_dir if given) and return the local path."""
    target = Path(dest_dir) if dest_dir else Path(tempfile.mkdtemp(prefix="codebase_explainer_"))
    if target.exists() and any(target.iterdir()):
        shutil.rmtree(target)
    Repo.clone_from(repo_url, target)
    return target


def collect_python_files(root: Path) -> list[Path]:
    """Walk root, skip excluded dirs, return absolute paths of .py files."""
    collected: list[Path] = []
    for path in root.rglob("*"):
        if path.is_dir():
            continue
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        if path.suffix in INCLUDED_EXTENSIONS:
            collected.append(path)
    return sorted(collected)


def ingest(repo_url: str, dest_dir: str | None = None) -> IngestedRepo:
    local_path = clone_repo(repo_url, dest_dir)
    py_files = collect_python_files(local_path)
    relative = [str(p.relative_to(local_path)) for p in py_files]
    return IngestedRepo(
        repo_url=repo_url,
        local_path=local_path,
        python_files=py_files,
        relative_files=relative,
    )


if __name__ == "__main__":
    import sys

    url = sys.argv[1] if len(sys.argv) > 1 else "https://github.com/octocat/Hello-World"
    result = ingest(url)
    print(f"Cloned to: {result.local_path}")
    print(f"Found {len(result.python_files)} Python file(s):")
    for f in result.relative_files:
        print(f"  {f}")
