"""
main.py — CLI entry point. Ties ingest -> static_analysis -> dependency_graph
-> llm_summary together, prints a report, and writes a Mermaid diagram +
a "start here" doc to disk.

Usage:
    python main.py https://github.com/<user>/<repo>

WHY a custom top-level error handler instead of letting exceptions crash
with a raw traceback?
- A raw Python traceback is meant for developers debugging the tool
  itself, not for someone running the tool and hitting an ordinary,
  expected failure (bad URL, no internet, API quota hit). We catch the
  known failure modes and print one clean, actionable line instead.
- We deliberately do NOT swallow *unexpected* errors — anything we don't
  recognize still shows a short message + the option to see the full
  traceback, so real bugs are still debuggable.
"""

from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

# Silence the noisy "Python 3.9 is past end of life" FutureWarning that
# google-auth prints on import — it's a real signal (upgrade Python
# eventually) but it drowns out our own progress output on every run.
warnings.filterwarnings("ignore", category=FutureWarning, module="google.*")

sys.path.insert(0, str(Path(__file__).parent / "src"))

from dotenv import load_dotenv  # noqa: E402

from dependency_graph import build_dependency_graph, to_mermaid  # noqa: E402
from ingest import ingest  # noqa: E402
# from llm_summary import LLMConfigError, summarize_file, summarize_repo  # noqa: E402
from llm_summary import LLMConfigError, friendly_llm_error, summarize_file, summarize_repo  # noqa: E402
from llm_summary import _client as llm_client  # noqa: E402
from onboarding import detect_entry_points, parse_manifest, suggested_reading_order  # noqa: E402
from static_analysis import analyze_repo  # noqa: E402
# NOTE: llm_summary.py talks to Google Gemini (free tier, no credit card
# needed) rather than the Anthropic API — see llm_summary.py docstring
# for the "why isolate the LLM provider in one file" reasoning.

try:
    from git.exc import GitCommandError
except ImportError:  # GitPython not installed yet — ingest() will fail anyway
    GitCommandError = None



def _step(n: int, total: int, label: str) -> None:
    print(f"\n[{n}/{total}] {label}")


def _ok(msg: str) -> None:
    print(f"      \u2713 {msg}")


def _warn(msg: str) -> None:
    print(f"      ! {msg}")


def build(repo_url: str, out_dir: Path, use_llm: bool) -> None:
    total_steps = 4

    _step(1, total_steps, f"Cloning {repo_url}")
    repo = ingest(repo_url)
    _ok(f"Found {len(repo.python_files)} Python file(s)")
    if len(repo.python_files) == 0:
        _warn("No .py files found — is this a Python repo? (this tool only analyzes Python)")

    _step(2, total_steps, "Running static analysis (AST)")
    analyses = analyze_repo(repo.local_path, repo.python_files)
    parse_errors = [a for a in analyses if a.parse_error]
    _ok(f"Parsed {len(analyses) - len(parse_errors)}/{len(analyses)} file(s) successfully")
    for a in parse_errors:
        _warn(f"Could not parse {a.path}: {a.parse_error}")

    _step(3, total_steps, "Building dependency graph")
    graph = build_dependency_graph(analyses)
    (out_dir / "diagram.mmd").write_text(to_mermaid(graph), encoding="utf-8")
    total_edges = sum(len(v) for v in graph.edges.values())
    _ok(f"{total_edges} internal dependency edge(s) -> diagram.mmd")

    entry_points = detect_entry_points(analyses)
    reading_order = suggested_reading_order(analyses, graph, entry_points)
    manifest_name, manifest_packages = parse_manifest(repo.local_path)
    _ok(f"{len(entry_points)} entry point(s) detected" + (f", manifest: {manifest_name}" if manifest_name else ""))

    overview = ""
    file_summaries: dict[str, str] = {}
    if use_llm:
        _step(4, total_steps, "Generating LLM summaries (Gemini)")
        try:
            client = llm_client()
            overview = summarize_repo(client, analyses, graph, repo_url)
            _ok("Repo overview generated")
            summarizable = [a for a in analyses if not a.parse_error]
            for i, a in enumerate(summarizable, 1):
                file_summaries[a.path] = summarize_file(client, a)
                print(f"      \u2713 [{i}/{len(summarizable)}] {a.path}")
        except LLMConfigError as e:
            _warn(f"Skipping LLM summaries — {e}")
        except Exception as e:  # noqa: BLE001 — deliberate: see module docstring
            # _warn(f"LLM summaries stopped early — {_friendly_error(e)}")
            print(f"\nUnexpected error: {friendly_llm_error(e)}")
    else:
        _step(4, total_steps, "Skipped LLM summaries (--no-llm)")

    _write_readme(out_dir, repo_url, analyses, graph, overview, file_summaries, entry_points, reading_order, manifest_name, manifest_packages)
    print(f"\nDone. See {out_dir / 'README.md'} and {out_dir / 'diagram.mmd'}")


def _write_readme(
    out_dir, repo_url, analyses, graph, overview, file_summaries,
    entry_points, reading_order, manifest_name, manifest_packages,
) -> None:
    lines = [f"# Codebase Explainer: {repo_url}\n"]
    if overview:
        lines += ["## Overview\n", overview, ""]

    lines.append("## Getting started\n")
    if entry_points:
        lines.append("**Entry points:**")
        for e in entry_points:
            lines.append(f"- `{e.path}` — {e.reason}")
        lines.append("")
    else:
        lines.append("**Entry points:** none detected (no `if __name__ == \"__main__\":` guard "
                      "or conventional entry filename found).\n")

    if reading_order:
        lines.append("**Suggested reading order** (entry points first, then files most other "
                      "files depend on, tests last):")
        for i, path in enumerate(reading_order, 1):
            lines.append(f"{i}. `{path}`")
        lines.append("")

    if manifest_name:
        lines.append(f"**Key dependencies** (from `{manifest_name}`):")
        lines.append(", ".join(f"`{p}`" for p in manifest_packages) or "_(none listed)_")
        lines.append("")
    else:
        lines.append("**Key dependencies:** no `requirements.txt` or `pyproject.toml` found at repo root.\n")

    lines.append("## Files\n")
    for a in analyses:
        lines.append(f"### `{a.path}`")
        if a.parse_error:
            lines.append(f"_Parse error: {a.parse_error}_\n")
            continue
        if a.path in file_summaries:
            lines.append(file_summaries[a.path] + "\n")
        lines.append(f"- Functions: {', '.join(a.functions) or '(none)'}")
        lines.append(f"- Classes: {', '.join(c.name for c in a.classes) or '(none)'}")
        deps = sorted(graph.edges.get(a.path, []))
        lines.append(f"- Internal deps: {', '.join(deps) or '(none)'}\n")

    lines.append("## Known limitations")
    lines.append(
        "- Only analyzes `.py` files; leading-dot relative imports "
        "(`from . import x`) and dynamic imports (`importlib.import_module`) "
        "are not resolved. Bare-name sibling imports (`from context import y`) are."
    )
    lines.append("- Top-level functions and class methods are tracked separately.")
    lines.append(
        "- Reading order and entry-point detection are heuristics based on "
        "import structure and Python naming conventions — not a guarantee "
        "of the intended architecture.\n"
    )

    (out_dir / "README.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Explain a GitHub Python codebase.")
    parser.add_argument("repo_url", help="GitHub repo URL to analyze")
    parser.add_argument(
        "--no-llm", action="store_true", help="Skip LLM summaries, just show structural analysis"
    )
    parser.add_argument(
        "--out", default="output", help="Directory to write README.md and diagram.mmd into"
    )
    args = parser.parse_args()

    load_dotenv()
    out_dir = Path(args.out)
    out_dir.mkdir(exist_ok=True)

    try:
        build(args.repo_url, out_dir, use_llm=not args.no_llm)
        return 0
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    except (GitCommandError if GitCommandError else ()) as e:
        print(f"\nCould not clone repo — check the URL and your internet connection.\n({e})")
        return 1
    except Exception as e:  # noqa: BLE001 — last-resort catch, see module docstring
        print(f"\nUnexpected error: {_friendly_error(e)}")
        print("Re-run with PYTHONTRACEBACK=1 set, or share this message, for the full trace.")
        import os

        if os.environ.get("PYTHONTRACEBACK"):
            raise
        return 1


if __name__ == "__main__":
    sys.exit(main())