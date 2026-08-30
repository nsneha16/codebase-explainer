"""
llm_summary.py — takes the STRUCTURED output of static_analysis.py and
dependency_graph.py and asks an LLM (Google Gemini) to turn it into a
plain-English summary. This is the layer that talks to the LLM —
deliberately the LAST layer, not the first.

WHY structured data -> LLM, instead of raw source code -> LLM?
- Token cost/limits: raw source of a medium repo can be huge; the
  structured summary (imports, function names, class names, edges) is
  a fraction of the size and scales to bigger repos.
- Accuracy: the LLM can't "hallucinate" a function that doesn't exist
  if we only ever show it function names we already extracted via ast.
  The facts are guaranteed correct BEFORE the LLM ever sees them — the
  LLM's job is only to explain, not to extract.
- This is the answer to "why not just feed raw code to the LLM?" —
  cost, accuracy, and structured output you can actually build UI/CLI
  around (a summary string is hard to reason about; a dataclass isn't).

WHY Gemini instead of Claude/OpenAI here specifically?
- Google's Gemini API has a genuinely free tier (no credit card required
  to get an API key), which matters for a student project with no card
  on hand. The architecture doesn't care which LLM sits behind this
  module — that's the point of isolating it in its own file. Swapping
  providers later (e.g. to Claude) means changing only this file.

WHY a custom LLMConfigError instead of letting a bare RuntimeError bubble
up?
- main.py needs to tell "API key missing" (a setup problem the user can
  fix) apart from "the network/API call itself failed" (a runtime
  problem). A dedicated exception type makes that distinction explicit
  and catchable, instead of string-matching an error message.
"""

from __future__ import annotations

import json
import os
import time

from google import genai
from google.genai import errors as genai_errors

from dependency_graph import DependencyGraph
from static_analysis import FileAnalysis

MODEL = "gemini-3.6-flash"
_RATE_LIMIT_DELAY_SECONDS = 13  # free tier: 5 requests/minute -> ~12-13s spacing
_MAX_RETRY_ATTEMPTS = 4


class LLMConfigError(RuntimeError):
    """Raised when the LLM can't be used due to missing/bad setup
    (e.g. no API key) — distinct from a call that fails at runtime."""
    
def friendly_llm_error(e: Exception) -> str:
    """Turn a raw Gemini SDK exception into one short, user-facing
    sentence. Both main.py (CLI) and streamlit_app.py (web) call this —
    kept here, next to the code that raises the errors, so the two UIs
    can't drift into showing different messages for the same failure.

    The free tier has TWO separate quotas that both surface as the same
    429 RESOURCE_EXHAUSTED status, so we inspect the error body's
    quotaId to tell them apart — the fix is different for each:
    - per-day quota (as low as 20 requests/day on this tier): waiting a
      minute does nothing; only a new day (or a new API key) helps.
    - per-minute quota: waiting ~60s and retrying is genuinely enough.
    Showing the raw exception (a multi-line dict dump) in a UI reads as
    an unhandled crash even though it's an entirely expected, recoverable
    condition — this exists so the person sees "quota's out for today,"
    not a wall of JSON.
    """
    text = str(e)
    if "RESOURCE_EXHAUSTED" in text:
        if "PerDay" in text:
            return (
                "Gemini's free-tier daily quota is used up for now. "
                "It resets in ~24h — try again later, or use --no-llm / "
                "uncheck the LLM option to see structural analysis only."
            )
        return "Gemini's free-tier rate limit was hit. Wait about a minute and try again."
    if isinstance(e, genai_errors.ServerError):
        return "Gemini's servers are overloaded right now. Try again shortly."
    return text

def _client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise LLMConfigError(
            "GEMINI_API_KEY not set. Put it in a .env file (see .env.example) "
            "or export it in your shell. Get a free key at "
            "https://aistudio.google.com/apikey"
        )
    return genai.Client(api_key=api_key)


# def _generate_with_retry(client: genai.Client, **kwargs):
#     """Wraps generate_content with retry-with-backoff for transient
#     failures. Two distinct cases, handled differently:

#     - 503 ServerError (Gemini overloaded) -> short backoff (10s, 20s, 30s),
#       not our bug, usually resolves fast.
#     - 429 ClientError RESOURCE_EXHAUSTED (rate limit hit) -> longer wait
#       (65s, past the free tier's per-minute window) since retrying
#       immediately would just hit the same limit again. This is the case
#       we originally missed: ClientError and ServerError are different
#       exception types, so a handler that only caught ServerError let
#       rate-limit hits fail immediately with zero retries — exactly the
#       "stopped early on the very first call" symptom that exposed it."""
#     last_error: Exception | None = None
#     for attempt in range(1, _MAX_RETRY_ATTEMPTS + 1):
#         try:
#             return client.models.generate_content(**kwargs)
#         except genai_errors.ServerError as e:
#             last_error = e
#             if attempt == _MAX_RETRY_ATTEMPTS:
#                 break
#             wait = 10 * attempt  # 10s, 20s, 30s
#             print(f"      ! Gemini busy, retrying in {wait}s (attempt {attempt}/{_MAX_RETRY_ATTEMPTS})")
#             time.sleep(wait)
#         except genai_errors.ClientError as e:
#             if "RESOURCE_EXHAUSTED" not in str(e):
#                 raise  # a different 4xx (bad request, auth, ...) — don't mask it, fail now
#             last_error = e
#             if attempt == _MAX_RETRY_ATTEMPTS:
#                 break
#             wait = 65  # past the free tier's per-minute quota window
#             print(f"      ! Rate limit hit, waiting {wait}s (attempt {attempt}/{_MAX_RETRY_ATTEMPTS})")
#             time.sleep(wait)
#     raise last_error
# # 
def _generate_with_retry(client: genai.Client, **kwargs):
    """Wraps generate_content with retry-with-backoff for 503 ServerError
    (Gemini overloaded) only — that's genuinely transient, usually
    resolves within seconds.

    429 ClientError RESOURCE_EXHAUSTED (rate/quota limit) is deliberately
    NOT retried here. Retrying assumes the wait will help, but the free
    tier has both a per-minute AND a per-day quota — if it's the daily
    one, every retry just re-hits the same wall after wasting the wait
    time (we originally retried this with a 65s backoff x4 attempts,
    which blocked the whole run for ~4+ minutes only to fail anyway).
    Failing fast here and surfacing one clear message is better UX,
    especially for the Streamlit app where this blocks the UI thread."""
    try:
        return client.models.generate_content(**kwargs)
    except genai_errors.ServerError as e:
        last_error = e
        for attempt in range(2, _MAX_RETRY_ATTEMPTS + 1):
            wait = 10 * (attempt - 1)  # 10s, 20s, 30s
            print(f"      ! Gemini busy, retrying in {wait}s (attempt {attempt - 1}/{_MAX_RETRY_ATTEMPTS - 1})")
            time.sleep(wait)
            try:
                return client.models.generate_content(**kwargs)
            except genai_errors.ServerError as e2:
                last_error = e2
        raise last_error

def summarize_file(client: genai.Client, analysis: FileAnalysis) -> str:
    if analysis.parse_error:
        return f"Could not parse this file: {analysis.parse_error}"

    facts = {
        "file": analysis.path,
        "docstring": analysis.docstring,
        "functions": analysis.functions,
        "classes": [{"name": c.name, "methods": c.methods} for c in analysis.classes],
        "imports": analysis.imports + analysis.from_imports,
    }

    response = _generate_with_retry(
        client,
        model=MODEL,
        contents=(
            "Here is the extracted structure of a Python file (from AST parsing, "
            "so these facts are accurate — do not add functions/classes that aren't "
            "listed). Write a 2-3 sentence plain-English summary of what this file "
            f"likely does, for a developer skimming a codebase:\n\n{json.dumps(facts, indent=2)}"
        ),
    )
    time.sleep(_RATE_LIMIT_DELAY_SECONDS)
    return response.text.strip()


def summarize_repo(
    client: genai.Client,
    analyses: list[FileAnalysis],
    graph: DependencyGraph,
    repo_url: str,
) -> str:
    overview_facts = {
        "repo_url": repo_url,
        "files": [
            {
                "path": a.path,
                "functions": a.functions,
                "classes": [c.name for c in a.classes],
                "depends_on": sorted(graph.edges.get(a.path, [])),
            }
            for a in analyses
            if not a.parse_error
        ],
    }

    response = _generate_with_retry(
        client,
        model=MODEL,
        contents=(
            "Here is the structure of an entire codebase, extracted via AST parsing "
            "(file list, their functions/classes, and internal dependency edges). "
            "Write a short 'start here' overview (4-6 sentences) explaining what this "
            "project does, its overall architecture, and which file is the best entry "
            f"point to start reading:\n\n{json.dumps(overview_facts, indent=2)}"
        ),
    )
    time.sleep(_RATE_LIMIT_DELAY_SECONDS)
    return response.text.strip()


if __name__ == "__main__":
    import sys

    from dotenv import load_dotenv

    from dependency_graph import build_dependency_graph
    from ingest import ingest
    from static_analysis import analyze_repo

    load_dotenv()

    url = sys.argv[1] if len(sys.argv) > 1 else "https://github.com/octocat/Hello-World"
    repo = ingest(url)
    analyses = analyze_repo(repo.local_path, repo.python_files)
    graph = build_dependency_graph(analyses)

    client = _client()
    print("=== REPO OVERVIEW ===")
    print(summarize_repo(client, analyses, graph, url))

    for a in analyses:
        print(f"\n=== {a.path} ===")
        print(summarize_file(client, a))