"""
streamlit_app.py — a thin web UI over the same pipeline main.py runs from
the CLI. This file does NOT duplicate the engineering logic (ingest,
static_analysis, dependency_graph, llm_summary) — it just calls the same
functions and renders the result in a browser, so the CLI and the web
demo can never drift out of sync with each other.

WHY Streamlit specifically (over Flask/FastAPI + a hand-built frontend)?
- The core deliverable for this project is the analysis pipeline, not a
  web app — a hand-rolled frontend would spend time on UI plumbing that
  doesn't demonstrate anything the interview cares about. Streamlit
  turns a Python script into a browser UI with no separate frontend
  code, which keeps 100% of the effort on the actual engineering.
- Streamlit Community Cloud deploys directly from a GitHub repo for
  free, with no server to manage — appropriate for a demo link, not
  meant to imply this is how a production service would be hosted.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv
warnings.filterwarnings("ignore", category=FutureWarning, module="google.*")
load_dotenv()
sys.path.insert(0, str(Path(__file__).parent / "src"))

from dependency_graph import build_dependency_graph, to_mermaid  # noqa: E402
from ingest import ingest  # noqa: E402
# from llm_summary import LLMConfigError, summarize_file, summarize_repo  # noqa: E402
from llm_summary import LLMConfigError, friendly_llm_error, summarize_file, summarize_repo  # noqa: E402
from llm_summary import _client as llm_client  # noqa: E402
from onboarding import detect_entry_points, parse_manifest, suggested_reading_order  # noqa: E402
from static_analysis import analyze_repo  # noqa: E402

st.set_page_config(page_title="Codebase Explainer", page_icon="\U0001f4c2", layout="wide")

st.title("Codebase Explainer")
st.caption(
    "A Python-specific onboarding tool. Give it a GitHub repo URL: it statically "
    "analyzes every `.py` file with Python's `ast` module (not regex), detects "
    "entry points and suggests a reading order from the real import graph, reads "
    "`requirements.txt`/`pyproject.toml` for actual dependencies, and optionally "
    "uses Gemini for plain-English summaries grounded in the extracted facts."
)

with st.form("analyze_form"):
    repo_url = st.text_input("GitHub repo URL", placeholder="https://github.com/user/repo")
    use_llm = st.checkbox("Generate LLM summaries (uses Gemini free tier — may be rate-limited)", value=False)
    submitted = st.form_submit_button("Analyze")

if submitted and repo_url:
    progress = st.empty()
    try:
        with st.spinner("Cloning repo..."):
            repo = ingest(repo_url)
        progress.success(f"Found {len(repo.python_files)} Python file(s)")

        if not repo.python_files:
            st.warning("No .py files found — this tool only analyzes Python repos.")
            st.stop()

        with st.spinner("Running static analysis (AST)..."):
            analyses = analyze_repo(repo.local_path, repo.python_files)
        parse_errors = [a for a in analyses if a.parse_error]
        if parse_errors:
            st.warning(f"{len(parse_errors)} file(s) could not be parsed (shown below).")

        with st.spinner("Building dependency graph..."):
            graph = build_dependency_graph(analyses)
        total_edges = sum(len(v) for v in graph.edges.values())
        st.success(f"{total_edges} internal dependency edge(s) found")

        entry_points = detect_entry_points(analyses)
        reading_order = suggested_reading_order(analyses, graph, entry_points)
        manifest_name, manifest_packages = parse_manifest(repo.local_path)

        overview = ""
        file_summaries: dict[str, str] = {}
        if use_llm:
            try:
                client = llm_client()
                llm_progress = st.progress(0.0, text="Generating repo overview...")
                overview = summarize_repo(client, analyses, graph, repo_url)
                summarizable = [a for a in analyses if not a.parse_error]
                for i, a in enumerate(summarizable, 1):
                    llm_progress.progress(
                        i / len(summarizable), text=f"Summarizing {a.path} ({i}/{len(summarizable)})"
                    )
                    file_summaries[a.path] = summarize_file(client, a)
                llm_progress.empty()
            except LLMConfigError:
                st.info(
                    "No GEMINI_API_KEY configured on this deployment — showing structural "
                    "analysis only. (Set GEMINI_API_KEY in Streamlit secrets to enable summaries.)"
                )
            except Exception as e:  # noqa: BLE001 — surfaced to the user, not swallowed
                # st.warning(f"LLM summaries stopped early: {e}")
                st.warning(f"LLM summaries stopped early: {friendly_llm_error(e)}")

        tab_onboard, tab_overview, tab_files, tab_diagram = st.tabs(
            ["Getting started", "Overview", "Files", "Dependency diagram"]
        )

        with tab_onboard:
            st.subheader("Entry points")
            if entry_points:
                for e in entry_points:
                    st.markdown(f"- `{e.path}` — _{e.reason}_")
            else:
                st.info("No entry points detected (no `__main__` guard or conventional filename).")

            st.subheader("Suggested reading order")
            st.caption("Entry points first, then files most other files depend on, tests last.")
            for i, path in enumerate(reading_order, 1):
                st.markdown(f"{i}. `{path}`")

            st.subheader("Key dependencies")
            if manifest_name:
                st.caption(f"From `{manifest_name}`")
                st.write(", ".join(f"`{p}`" for p in manifest_packages) or "_(none listed)_")
            else:
                st.info("No `requirements.txt` or `pyproject.toml` found at the repo root.")

        with tab_overview:
            if overview:
                st.markdown(overview)
            else:
                st.info("Enable the LLM checkbox above to generate a plain-English overview.")

        with tab_files:
            for a in analyses:
                with st.expander(a.path):
                    if a.parse_error:
                        st.error(f"Parse error: {a.parse_error}")
                        continue
                    if a.path in file_summaries:
                        st.markdown(file_summaries[a.path])
                    col1, col2 = st.columns(2)
                    col1.markdown(f"**Functions:** {', '.join(a.functions) or '_none_'}")
                    col1.markdown(f"**Classes:** {', '.join(c.name for c in a.classes) or '_none_'}")
                    deps = sorted(graph.edges.get(a.path, []))
                    col2.markdown(f"**Internal deps:** {', '.join(deps) or '_none_'}")

        with tab_diagram:
            mermaid_code = to_mermaid(graph)
            st.code(mermaid_code, language="text")
            st.caption("Paste this into https://mermaid.live to view it rendered.")

    except Exception as e:  # noqa: BLE001 — top-level catch so the UI never shows a raw crash
        st.error(f"Something went wrong: {e}")

elif submitted:
    st.warning("Enter a GitHub repo URL first.")