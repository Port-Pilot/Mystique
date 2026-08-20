"""
phase2_generate.py
==================
Phase 2 of the FixMorph/Mystique evaluation.

For every row in `backport_benchmark_results_new` with status='ready' this script:

1.  Reads Main-data-set.xlsx to rebuild the (pb, pe) -> (pa, pc, ref_path,
    target_path) lookup needed by Mystique's bp() function.
2.  Pulls full file content for all four commits (pa, pb, pc, pe) from the
    local SQLite cache built during phase 1 (no new network calls needed).
3.  Calls patchbp.bp() -- Mystique's full Joern-slice + LLM pipeline.
4.  If bp() returns before its own LLM call (JOERN_ERROR, METHOD_NOT_FOUND,
    PDG_NOT_FOUND, SLICE_FAILED), falls back to a direct LLM call using the
    raw diff stored in the DB and the target file content.
5.  Updates the Neon row with:
        generated_patch, method, execution_time_seconds,
        number_of_llm_api_calls, input_tokens, output_tokens,
        reasoning_tokens, total_tokens, api_cost, status

Run from Mystique\\ :
    python phase2_generate.py --main FixMorph-Dataset/Main-data-set.xlsx

or
    python3 phase2_generate.py --main FixMorph-Dataset/Main-data-set.xlsx

Optional flags:
    --limit N      Process at most N rows (smoke-testing)
    --dry-run      Reconstruct patch dict and print, skip LLM and DB writes
    --overwrite    Re-process rows that already have status='done'

Environment variables (loaded from .env):
    NEON_DATABASE_URL   Postgres connection string
    GPT_MODEL           Model name (default: gpt-5.5)
    GPT_API_KEY         Base64-encoded OpenAI API key
    COST_INPUT_PER_1M   Cost per 1M input tokens  (default: 5.00)
    COST_OUTPUT_PER_1M  Cost per 1M output tokens (default: 30.00)
"""

import argparse
import base64
import logging
import os
import sqlite3
import sys
import time
import traceback

import pandas as pd
import psycopg2
from dotenv import load_dotenv

# Use an explicit path so .env is found even after os.chdir() below.
_DOTENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(_DOTENV_PATH, override=True)

# ---------------------------------------------------------------------------
# Bootstrap: add Mystique src/ to sys.path so we can import its modules.
# ---------------------------------------------------------------------------

_SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
_MYSTIQUE_SRC = os.path.join(_SCRIPT_DIR, "mystique-opensource.github.io", "src")
if _MYSTIQUE_SRC not in sys.path:
    sys.path.insert(0, _MYSTIQUE_SRC)

# Mystique uses relative paths for its cache dirs (e.g. cache_bug/).
# Running from src/ keeps those artefacts near the source.
os.chdir(_MYSTIQUE_SRC)

import config   # noqa: E402
import difftools  # noqa: E402
import llm      # noqa: E402
import patchbp  # noqa: E402
from ast_parser import ASTParser  # noqa: E402
from common import ErrorCode, Language  # noqa: E402
# Reuse bp_wrapper's own raw (unformatted) method parsing + diff assembly
# helpers, instead of inventing a second, possibly-misaligned mechanism.
from patchbp import _methods_by_name, _raw_methods, _unified_file_diff  # noqa: E402

# llm.client is created at import time, before dotenv is guaranteed to have
# populated the env.  Re-create it now with the confirmed key/url values.
from openai import OpenAI as _OpenAI  # noqa: E402
llm.client = _OpenAI(
    api_key=config.GPT_API_KEY,
    base_url=config.OPENAI_BASE_URL,
)

# ---------------------------------------------------------------------------
# Config / constants
# ---------------------------------------------------------------------------

NEON_DATABASE_URL  = os.getenv("NEON_DATABASE_URL")
CACHE_DB_PATH      = os.path.join(_SCRIPT_DIR, "github_fetch_cache.sqlite")
COST_INPUT_PER_1M  = float(os.getenv("COST_INPUT_PER_1M",  "5.00"))
COST_OUTPUT_PER_1M = float(os.getenv("COST_OUTPUT_PER_1M", "30.00"))

# When True: only tier-1 (real Mystique -- PDG slice + AST completeness +
# check/refine loop) results are accepted. Any row that can't complete tier 1
# is recorded as skipped rather than being handed to a tree-sitter-scoped or
# whole-file LLM fallback.
TIER1_ONLY = True

# Error codes that mean bp() exited BEFORE calling the LLM.
# In these cases we run a direct LLM fallback.
_PRE_LLM_ERRORS = {
    ErrorCode.EXCEPTION.value,           # unhandled crash (e.g. Joern not installed)
    ErrorCode.JOERN_ERROR.value,
    ErrorCode.METHOD_NOT_FOUND.value,
    ErrorCode.REFERENCE_METHOD_MISMATCH.value,
    ErrorCode.TARGET_METHOD_NOT_FOUND.value,
    ErrorCode.AMBIGUOUS_METHOD.value,
    ErrorCode.CHANGE_OUTSIDE_METHOD.value,
    ErrorCode.PARTIAL_BACKPORT_FAILED.value,
    ErrorCode.INVALID_PATCH.value,
    ErrorCode.PDG_NOT_FOUND.value,
    ErrorCode.SLICE_FAILED.value,
    ErrorCode.GROUNDTRUTH_SLICE_FAILED.value,
    ErrorCode.AST_ERROR.value,
}


def _scoped_llm_fallback(stored_patch: str, target_code: str, target_path: str,
                          language: Language, failed_method: str | None,
                          failed_changes: list[tuple[str, int]] | None,
                          usage: "llm.LLMUsage") -> str | None:
    """Mystique-faithful fallback for when bp()/bp_wrapper() couldn't reach
    its own LLM call. Instead of asking the LLM to localize AND adapt across
    the whole file (gpt_fix_diff's job), do the localization ourselves with
    the same tree-sitter method boundaries Mystique's real path would hand to
    Joern, then call the SAME llm.gpt_fix() Mystique uses internally on just
    that scoped region -- fixed-code-in/fixed-code-out, not a hand-written
    diff. Splicing + diffing reuses bp_wrapper's own raw byte-offset helpers
    so there's no risk of the formatting pass shifting line numbers.
 
    Returns a unified diff string, or None if nothing could be localized
    (caller should then decide whether to fall further back to gpt_fix_diff
    on the whole file as an absolute last resort).
    """
    raw_methods = _raw_methods(target_code, target_path, language)
 
    scope_node = None
    if failed_method:
        candidates = _methods_by_name(raw_methods).get(failed_method, [])
        if len(candidates) == 1:
            scope_node = candidates[0].node
            scope_code = candidates[0].code
 
    if scope_node is None and failed_changes:
        # CHANGE_OUTSIDE_METHOD: there is no method boundary at all (macro,
        # global, struct/enum def, #include, ...). Localize to the smallest
        # enclosing top-level tree-sitter node(s) covering the touched lines
        # instead of the whole file.
        parser = ASTParser(target_code, language)
        touched_lines = {ln for _, ln in failed_changes}
        covering = [
            n for n in parser.root.children
            if any((n.start_point[0] + 1) <= ln <= (n.end_point[0] + 1) for ln in touched_lines)
        ]
        if covering:
            start_byte = min(n.start_byte for n in covering)
            end_byte = max(n.end_byte for n in covering)
            scope_code = target_code.encode("utf-8")[start_byte:end_byte].decode("utf-8")
 
            class _Span:
                pass
            scope_node = _Span()
            scope_node.start_byte = start_byte
            scope_node.end_byte = end_byte
 
    if scope_node is None:
        return None
 
    fixed_scope = llm.gpt_fix(stored_patch, scope_code, language, usage)
    if not fixed_scope:
        return None
 
    full_bytes = target_code.encode("utf-8")
    full_bytes = (full_bytes[:scope_node.start_byte]
                  + fixed_scope.encode("utf-8")
                  + full_bytes[scope_node.end_byte:])
    full_target_code = full_bytes.decode("utf-8")
    return _unified_file_diff(target_code, full_target_code, target_path)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("phase2")


# ---------------------------------------------------------------------------
# SQLite cache helpers (read-only; phase 1 already populated it)
# ---------------------------------------------------------------------------

_cache_conn = sqlite3.connect(CACHE_DB_PATH)


def _cache_get(sha: str, path: str) -> str | None:
    """Return file content from the local SQLite cache, or None if missing."""
    row = _cache_conn.execute(
        "SELECT content, missing FROM files WHERE sha=? AND path=?", (sha, path)
    ).fetchone()
    if row is None:
        return None  # never fetched
    content, missing = row
    return None if missing else (content or None)


# ---------------------------------------------------------------------------
# Excel lookup: (pb_sha, pe_sha) -> {pa, pc, ref_path, target_path}
# ---------------------------------------------------------------------------

def _clean(val) -> str | None:
    if pd.isna(val):
        return None
    v = str(val).strip()
    return v if v else None


def _obj2src(p: str) -> str:
    """Convert FixMorph's .o object path to the corresponding .c source path."""
    return p[:-2] + ".c" if p.endswith(".o") else p


def build_excel_lookup(xlsx_path: str) -> dict[tuple[str, str], dict]:
    """
    Returns a dict keyed by (pb_sha, pe_sha) with values:
        { "pa": str, "pc": str, "ref_path": str, "target_path": str }
    """
    df = pd.read_excel(xlsx_path)
    lookup: dict[tuple[str, str], dict] = {}
    for _, r in df.iterrows():
        pa = _clean(r.get("Commit - Pa"))
        pb = _clean(r.get("Commit - Pb"))
        pc = _clean(r.get("Commit - Pc"))
        pe = _clean(r.get("Commit - Pe"))
        path_ab = _clean(r.get("Program AB"))
        path_ce = _clean(r.get("Program CE"))
        if not all([pa, pb, pc, pe, path_ab, path_ce]):
            continue
        lookup[(pb, pe)] = {
            "pa": pa, "pc": pc,
            "ref_path":    _obj2src(path_ab),
            "target_path": _obj2src(path_ce),
        }
    log.info("Excel lookup built: %d entries", len(lookup))
    return lookup


# ---------------------------------------------------------------------------
# Commit-URL -> SHA extraction
# ---------------------------------------------------------------------------

def sha_from_url(url: str) -> str | None:
    """
    Extract the SHA from a GitHub commit URL of the form:
        https://github.com/{owner}/{repo}/commit/{sha}
    """
    if not url:
        return None
    parts = url.rstrip("/").split("/")
    candidate = parts[-1] if parts else ""
    return candidate if len(candidate) >= 7 else None


# ---------------------------------------------------------------------------
# Neon DB helpers
# ---------------------------------------------------------------------------

def get_conn():
    return psycopg2.connect(NEON_DATABASE_URL)


def fetch_ready_rows(conn, overwrite: bool) -> list[dict]:
    statuses = ("'ready'", "'done'", "'error'", "'running'") if overwrite else ("'ready'",)
    sql = (
        "SELECT id, new_version_patch_commit_url, old_version_patch_commit_url, "
        "       new_version_patch, old_version_patch "
        "FROM backport_benchmark_results_mystique_new "
        f"WHERE status IN ({', '.join(statuses)}) "
        "ORDER BY id"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def mark_running(conn, row_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE backport_benchmark_results_mystique_new "
            "SET status='running', updated_at=NOW() WHERE id=%s",
            (row_id,),
        )
    conn.commit()


def update_row(conn, row_id: int, *, method: str, generated_patch: str | None,
               elapsed: float, usage, api_cost: float, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE backport_benchmark_results_mystique_new SET
                method                   = %s,
                generated_patch          = %s,
                execution_time_seconds   = %s,
                number_of_llm_api_calls  = %s,
                input_tokens             = %s,
                output_tokens            = %s,
                reasoning_tokens         = %s,
                total_tokens             = %s,
                api_cost                 = %s,
                status                   = %s,
                updated_at               = NOW()
            WHERE id = %s
            """,
            (
                method, generated_patch, elapsed,
                usage.calls, usage.input_tokens, usage.output_tokens,
                usage.reasoning_tokens, usage.total_tokens,
                api_cost, status, row_id,
            ),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------

def estimate_cost(usage) -> float:
    return (
        usage.input_tokens  * COST_INPUT_PER_1M  / 1_000_000
        + usage.output_tokens * COST_OUTPUT_PER_1M / 1_000_000
    )


# ---------------------------------------------------------------------------
# Core: process one DB row
# ---------------------------------------------------------------------------

def process_row(row: dict, excel_lookup: dict, dry_run: bool) -> dict:
    """
    Returns a result dict:
        method, generated_patch, elapsed, usage, api_cost, status, error_detail
    """
    t0    = time.time()
    empty = llm.LLMUsage()

    def _err(msg: str) -> dict:
        return dict(method="mystique", generated_patch=None,
                    elapsed=time.time() - t0, usage=empty,
                    api_cost=0.0, status="error", error_detail=msg)

    pb_sha = sha_from_url(row["new_version_patch_commit_url"])
    pe_sha = sha_from_url(row["old_version_patch_commit_url"])

    if not pb_sha or not pe_sha:
        return _err("Could not parse SHAs from commit URLs")

    # Look up pa, pc, file paths from the Excel-derived lookup
    meta = excel_lookup.get((pb_sha, pe_sha))
    if meta is None:
        # Partial-SHA prefix match fallback (Excel might store full 40-char SHAs
        # while the URL could have a shorter form or vice-versa)
        meta = next(
            (v for (kpb, kpe), v in excel_lookup.items()
             if (kpb.startswith(pb_sha) or pb_sha.startswith(kpb))
             and (kpe.startswith(pe_sha) or pe_sha.startswith(kpe))),
            None,
        )
    if meta is None:
        return _err(f"(pb={pb_sha[:8]}, pe={pe_sha[:8]}) not found in Excel lookup")

    pa          = meta["pa"]
    pc          = meta["pc"]
    ref_path    = meta["ref_path"]
    target_path = meta["target_path"]

    # Pull all four file blobs from the local SQLite cache
    c_pa = _cache_get(pa,     ref_path)
    c_pb = _cache_get(pb_sha, ref_path)
    c_pc = _cache_get(pc,     target_path)
    c_pe = _cache_get(pe_sha, target_path)

    missing = [lbl for lbl, c in [("pa", c_pa), ("pb", c_pb),
                                   ("pc", c_pc), ("pe", c_pe)] if c is None]
    if missing:
        return _err(f"SQLite cache miss for commits {missing}")

    if dry_run:
        log.info("[DRY-RUN] id=%s pa=%s pb=%s pc=%s pe=%s ref=%s target=%s",
                 row["id"], pa[:8], pb_sha[:8], pc[:8], pe_sha[:8],
                 ref_path, target_path)
        return dict(method="mystique-dry-run", generated_patch=None,
                    elapsed=time.time() - t0, usage=empty,
                    api_cost=0.0, status="ready", error_detail="dry-run")

    # Build the patch dict bp() expects.
    # We pass full file content; bp() / Joern locate functions from the AST.
    patch_dict = {
        "origin_before_func_code": c_pa,  # reference (mainline) before fix
        "origin_after_func_code":  c_pb,  # reference after fix
        "target_before_func_code": c_pc,  # target branch before backport
        "target_after_func_code":  c_pe,  # target branch after backport (ground truth)
    }

    # Method name: best-effort from the file stem (e.g. "x86" from "x86.c").
    # When Joern is configured it tries f"{filename}#{method_name}" as a signature.
    method_name = os.path.splitext(os.path.basename(ref_path))[0]
    cveid       = f"fixmorph_{row['id']}"

    # ---- Run Mystique's bp() pipeline ----
    bp_result: dict = {}
    try:
        bp_result = patchbp.bp_warper(
            cveid, patch_dict, ref_path, method_name,
            Language.C, overwrite=False, target_file_path=target_path,
        )
    except Exception:
        log.error("Row %s: bp_warper raised:\n%s", row["id"], traceback.format_exc())
        bp_result = {"error": ErrorCode.EXCEPTION.value}

    bp_error    = bp_result.get("error", "")
    fixed_code  = bp_result.get("fixed_code")
    bp_usage    = bp_result.get("usage", llm.LLMUsage())
    method_used = "mystique"

    if fixed_code is not None:
        fixed_code = difftools.normalize_and_validate_unified_diff(
            fixed_code, c_pc, target_path
        )
        if fixed_code is None:
            bp_error = ErrorCode.INVALID_PATCH.value
            log.error("Row %s: Mystique produced an inapplicable diff", row["id"])

    # ---- LLM fallback ----
    # If Mystique's pipeline stopped BEFORE reaching its own LLM call
    # (e.g. no Joern, method not found, etc.) we don't hand the whole file
    # to the LLM. We localize with tree-sitter (the same method boundaries
    # Mystique's real path would hand to Joern) and call Mystique's own
    # llm.gpt_fix() on just that scope. Only if that localization genuinely
    # finds nothing (scope_node is None) do we fall further back to the
    # whole-file gpt_fix_diff as an absolute last resort.
    # if fixed_code is None and bp_error in _PRE_LLM_ERRORS:
    #     log.info(
    #         "Row %s: bp() stopped at '%s' (cause=%s method=%s) — "
    #         "running whole-file LLM fallback",
    #         row["id"], bp_error, bp_result.get("cause", ""),
    #         bp_result.get("failed_method", ""),
    #     )
    #     stored_patch = row.get("new_version_patch") or ""
    #     target_code  = bp_result.get("target") or c_pc
    #     fb_usage = llm.LLMUsage()
    #     try:
    #         candidate = llm.llm_fix_diff(stored_patch, target_code, fb_usage)
    #         fixed_code = difftools.normalize_and_validate_unified_diff(
    #             candidate or "", target_code, target_path
    #         )
    #         if candidate and fixed_code is None:
    #             log.error(
    #                 "Row %s: direct LLM returned a malformed or inapplicable diff",
    #                 row["id"],
    #             )
    #     except Exception:
    #         log.error("Row %s: direct LLM call raised:\n%s",
    #                   row["id"], traceback.format_exc())
    #         fixed_code = None

    #     # Merge token counts (bp may have spent 0 tokens; fallback adds on top)
    #     bp_usage = llm.LLMUsage(
    #         calls            = bp_usage.calls + fb_usage.calls,
    #         input_tokens     = bp_usage.input_tokens + fb_usage.input_tokens,
    #         output_tokens    = bp_usage.output_tokens + fb_usage.output_tokens,
    #         reasoning_tokens = bp_usage.reasoning_tokens + fb_usage.reasoning_tokens,
    #     )
    #     method_used = "mystique-llm-fallback"
    # ---- LLM fallback ----
    # If Mystique's pipeline stopped BEFORE reaching its own LLM call
    # (e.g. no Joern, method not found, etc.) we don't hand the whole file
    # to the LLM. We localize with tree-sitter (the same method boundaries
    # Mystique's real path would hand to Joern) and call Mystique's own
    # llm.gpt_fix() on just that scope. Only if that localization genuinely
    # finds nothing (scope_node is None) do we fall further back to the
    # whole-file gpt_fix_diff as an absolute last resort.
    if fixed_code is None and bp_error in _PRE_LLM_ERRORS and not TIER1_ONLY:
        stored_patch = row.get("new_version_patch") or ""
        target_code  = bp_result.get("target") or c_pc
        failed_method  = bp_result.get("failed_method")
        failed_changes = bp_result.get("failed_changes")
        fb_usage = llm.LLMUsage()
        method_used = "mystique-llm-fallback"

        try:
            candidate = _scoped_llm_fallback(
                stored_patch, target_code, target_path, Language.C,
                failed_method, failed_changes, fb_usage,
            )
            if candidate is not None:
                log.info(
                    "Row %s: bp() stopped at '%s' (cause=%s method=%s) — "
                    "running scoped (tree-sitter localized) LLM fallback",
                    row["id"], bp_error, bp_result.get("cause", ""), failed_method or "",
                )
                fixed_code = difftools.normalize_and_validate_unified_diff(
                    candidate, target_code, target_path
                )
                if fixed_code is None:
                    log.error(
                        "Row %s: scoped LLM fallback produced an inapplicable diff",
                        row["id"],
                    )
                else:
                    method_used = "mystique-llm-fallback-scoped"
        except Exception:
            log.error("Row %s: scoped LLM fallback raised:\n%s",
                      row["id"], traceback.format_exc())
            fixed_code = None

        # if fixed_code is None:
        #     log.info(
        #         "Row %s: could not localize a scope — running whole-file LLM fallback",
        #         row["id"],
        #     )
        #     try:
        #         candidate = llm.llm_fix_diff(stored_patch, target_code, fb_usage)
        #         fixed_code = difftools.normalize_and_validate_unified_diff(
        #             candidate or "", target_code, target_path
        #         )
        #         if candidate and fixed_code is None:
        #             log.error(
        #                 "Row %s: direct LLM returned a malformed or inapplicable diff",
        #                 row["id"],
        #             )
        #     except Exception:
        #         log.error("Row %s: direct LLM call raised:\n%s",
        #                   row["id"], traceback.format_exc())
        #         fixed_code = None
        if fixed_code is None:
            log.info(
                "Row %s: could not localize a scope — running Mystique-style "
                "whole-file LLM fallback",
                row["id"],
            )
            try:
                # Same behavioral contract as tiers 1/2 (llm.gpt_fix):
                # adapt-only, preserve everything else, output fixed code --
                # just scoped to the whole file instead of a slice, and with
                # the diff computed deterministically rather than LLM-authored.
                full_fixed_code = llm.llm_fix_wholefile(
                    stored_patch, target_code, Language.C, fb_usage
                )
                candidate = (
                    _unified_file_diff(target_code, full_fixed_code, target_path)
                    if full_fixed_code else None
                )
                fixed_code = difftools.normalize_and_validate_unified_diff(
                    candidate or "", target_code, target_path
                )
                if candidate and fixed_code is None:
                    log.error(
                        "Row %s: whole-file LLM fix produced an inapplicable diff",
                        row["id"],
                    )
            except Exception:
                log.error("Row %s: whole-file LLM call raised:\n%s",
                          row["id"], traceback.format_exc())
                fixed_code = None

        # Merge token counts (bp may have spent 0 tokens; fallback adds on top)
        bp_usage = llm.LLMUsage(
            calls            = bp_usage.calls + fb_usage.calls,
            input_tokens     = bp_usage.input_tokens + fb_usage.input_tokens,
            output_tokens    = bp_usage.output_tokens + fb_usage.output_tokens,
            reasoning_tokens = bp_usage.reasoning_tokens + fb_usage.reasoning_tokens,
        )

    elapsed = time.time() - t0
    cost    = estimate_cost(bp_usage)
    status = "done" if fixed_code is not None else "error"

    return dict(
        method          = method_used,
        generated_patch = fixed_code,
        elapsed         = elapsed,
        usage           = bp_usage,
        api_cost        = cost,
        status          = status,
        # error_detail    = (
        #     f"{bp_error}: {bp_result.get('cause', '')} "
        #     f"{bp_result.get('failed_method', '')}"
        # ).strip() if fixed_code is None else "",
        error_detail    = (
            f"{bp_error}: {bp_result.get('cause', '')} "
            f"{bp_result.get('failed_method', '')}"
            + (
                f" [{bp_result.get('check_fail_reason')}"
                f", attempts={bp_result.get('refinement_attempts')}]"
                if bp_result.get("check_fail_reason") is not None else ""
            )
        ).strip() if fixed_code is None else "",
        check_fail_reason = bp_result.get("check_fail_reason") if fixed_code is None else None,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Phase 2: generate Mystique backports and update Neon DB"
    )
    parser.add_argument(
        "--main", required=True,
        help="Path to FixMorph-Dataset/Main-data-set.xlsx",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process at most N rows (for smoke-testing)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Reconstruct patch dict and log info; skip LLM calls and DB writes",
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Re-process rows that already have status='done'",
    )
    parser.add_argument(
        "--log-file", type=str, default=None,
        help="Optional file path to save output log to (e.g., phase2.log)",
    )
    args = parser.parse_args()

    if args.log_file:
        class FlushingFileHandler(logging.FileHandler):
            def emit(self, record):
                super().emit(record)
                self.flush()

        fh = FlushingFileHandler(args.log_file, mode="a", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
        logging.getLogger().addHandler(fh)
        log.info("Logging output to file: %s", args.log_file)

    if not NEON_DATABASE_URL:
        log.error("NEON_DATABASE_URL is not set — check your .env file")
        sys.exit(1)

    # Build Excel lookup (using absolute path since we changed cwd above)
    xlsx = (args.main if os.path.isabs(args.main)
            else os.path.join(_SCRIPT_DIR, args.main))
    log.info("Building Excel lookup from %s ...", xlsx)
    excel_lookup = build_excel_lookup(xlsx)

    # Fetch rows from Neon
    conn = get_conn()
    rows = fetch_ready_rows(conn, args.overwrite)
    conn.close()
    log.info("Fetched %d rows to process", len(rows))

    if args.limit:
        rows = rows[: args.limit]
        log.info("--limit applied: processing %d rows", len(rows))

    stats = {"done": 0, "error": 0, "fallback": 0}

    error_breakdown: dict[str, int] = {}
    check_fault_breakdown: dict[str, int] = {}

    for i, row in enumerate(rows, 1):
        log.info("[%d/%d] Row id=%s ...", i, len(rows), row["id"])

        if not args.dry_run:
            conn = get_conn()
            mark_running(conn, row["id"])
            conn.close()

        result = process_row(row, excel_lookup, dry_run=args.dry_run)

        if args.dry_run:
            log.info("  -> [DRY-RUN] would write method=%s", result["method"])
            continue

        conn = get_conn()
        update_row(
            conn, row["id"],
            method          = result["method"],
            generated_patch = result["generated_patch"],
            elapsed         = result["elapsed"],
            usage           = result["usage"],
            api_cost        = result["api_cost"],
            status          = result["status"],
        )
        conn.close()

        if result["status"] == "done":
            stats["done"] += 1
            if result["method"] == "mystique-llm-fallback":
                stats["fallback"] += 1
            log.info(
                "  -> DONE  method=%-25s tokens=%d cost=$%.4f elapsed=%.1fs",
                result["method"], result["usage"].total_tokens,
                result["api_cost"], result["elapsed"],
            )
        # else:
        #     stats["error"] += 1
        #     log.warning("  -> ERROR %s", result.get("error_detail", ""))
        else:
            stats["error"] += 1
            log.warning("  -> ERROR %s", result.get("error_detail", ""))
            bp_error_code = (result.get("error_detail") or "").split(":", 1)[0]
            error_breakdown[bp_error_code] = error_breakdown.get(bp_error_code, 0) + 1
            check_reason = result.get("check_fail_reason")
            if check_reason:
                bucket = check_reason.split(":")[0].strip()
                check_fault_breakdown[bucket] = check_fault_breakdown.get(bucket, 0) + 1

    # log.info(
    #     "=== Phase 2 complete. done=%d (of which fallback=%d) error=%d ===",
    #     stats["done"], stats["fallback"], stats["error"],
    # )
    log.info(
        "=== Phase 2 complete. done=%d (of which fallback=%d) error=%d ===",
        stats["done"], stats["fallback"], stats["error"],
    )
    if error_breakdown:
        log.info("Error breakdown: %s", dict(
            sorted(error_breakdown.items(), key=lambda kv: -kv[1])))
    if check_fault_breakdown:
        log.info("CHECK_FAILED reason breakdown: %s", dict(
            sorted(check_fault_breakdown.items(), key=lambda kv: -kv[1])))


if __name__ == "__main__":
    main()
