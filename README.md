# Codebase Explainer

Give it a GitHub repo URL, it clones the repo, statically analyzes every
`.py` file with `ast` (no regex hacks), builds an internal dependency
graph, and uses Claude to generate a plain-English "start here" overview
plus a per-file summary — grounded strictly in facts the parser already
extracted (the LLM never sees raw source, only structured facts, so it
can't hallucinate a function that doesn't exist).

## Setup (VS Code / local)

```bash
git clone <this-repo>
cd codebase-explainer
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # then paste your ANTHROPIC_API_KEY into .env
```

## Run

```bash
python main.py https://github.com/<user>/<repo>
```

Outputs go to `output/README.md` (human-readable report) and
`output/diagram.mmd` (Mermaid dependency graph — paste into
https://mermaid.live to view, or GitHub renders `.mmd` in markdown fences
automatically).

Add `--no-llm` to skip the Claude API calls and just see structural
analysis (useful for testing without burning API credits).

## Architecture

```
GitHub URL
    │
    ▼
ingest.py            clone repo, filter to .py files (skip .git/venv/etc)
    │
    ▼
static_analysis.py   ast.parse() each file -> imports, functions, classes
    │
    ▼
dependency_graph.py  resolve imports to internal repo files vs external
    │                packages -> build "file depends on file" graph
    ▼
llm_summary.py        send STRUCTURED facts (not raw code) to Claude ->
    │                 plain-English summaries
    ▼
main.py               tie it together, write README.md + diagram.mmd
```

## Design decisions

| Decision | Why |
|---|---|
| Clone repo instead of GitHub API per-file | Unauthenticated API = 60 req/hr; cloning is one network op then unlimited local reads |
| `ast` instead of regex for parsing | Regex breaks on multi-line imports, strings containing "import", comments; `ast` parses the way the interpreter does — no false positives |
| Structured facts → LLM, not raw source → LLM | Cheaper (fewer tokens), and the LLM can't invent functions that don't exist since it only sees facts we already verified |
| Only internal imports become graph edges | Every file importing `pandas` would flood the graph with useless edges; the point is to see how *your* files relate to each other |

## Known limitations

- Only Python (`.py`) files are analyzed.
- Relative imports (`from . import x`) and dynamic imports
  (`importlib.import_module(...)`) may not resolve correctly — scoped out
  for MVP under time pressure; noted rather than hidden.
- `ast.iter_child_nodes` (not `ast.walk`) is used for functions/classes so
  class methods don't double-count as top-level functions.
