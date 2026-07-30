"""
phase2_generate.py
==================
Phase 2 of the FixMorph/Mystique evaluation.

For every row in `backport_benchmark_results` with status='ready' this script:

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

Run from d:\\FYP\\Mystique\\ :
    python phase2_generate.py --main FixMorph-Dataset/Main-data-set.xlsx

Optional flags:
    --limit N      Process at most N rows (smoke-testing)
    --dry-run      Reconstruct patch dict and print, skip LLM and DB writes
    --overwrite    Re-process rows that already have status='done'

Environment variables (loaded from .env):
    NEON_DATABASE_URL   Postgres connection string
    GPT_MODEL           Model name (default: gpt-5.5)
    GPT_API_KEY         Base64-encoded OpenAI API key
    COST_INPUT_PER_1M   Cost per 1M input tokens  (default: 2.50)
    COST_OUTPUT_PER_1M  Cost per 1M output tokens (default: 10.00)
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

load_dotenv()

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
import llm      # noqa: E402
import patchbp  # noqa: E402
from common import ErrorCode, Language  # noqa: E402

# ---------------------------------------------------------------------------
# Config / constants
# ---------------------------------------------------------------------------

NEON_DATABASE_URL  = os.getenv("NEON_DATABASE_URL")
CACHE_DB_PATH      = os.path.join(_SCRIPT_DIR, "github_fetch_cache.sqlite")
COST_INPUT_PER_1M  = float(os.getenv("COST_INPUT_PER_1M",  "2.50"))
COST_OUTPUT_PER_1M = float(os.getenv("COST_OUTPUT_PER_1M", "10.00"))

# Error codes that mean bp() exited BEFORE calling the LLM.
# In these cases we run a direct LLM fallback.
_PRE_LLM_ERRORS = {
    ErrorCode.JOERN_ERROR.value,
    ErrorCode.METHOD_NOT_FOUND.value,
    ErrorCode.PDG_NOT_FOUND.value,
    ErrorCode.SLICE_FAILED.value,
    ErrorCode.GROUNDTRUTH_SLICE_FAILED.value,
    ErrorCode.AST_ERROR.value,
}

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
    statuses = ("'ready'", "'done'") if overwrite else ("'ready'",)
    sql = (
        "SELECT id, new_version_patch_commit_url, old_version_patch_commit_url, "
        "       new_version_patch, old_version_patch "
        "FROM backport_benchmark_results "
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
            "UPDATE backport_benchmark_results "
            "SET status='running', updated_at=NOW() WHERE id=%s",
            (row_id,),
        )
    conn.commit()


def update_row(conn, row_id: int, *, method: str, generated_patch: str | None,
               elapsed: float, usage, api_cost: float, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE backport_benchmark_results SET
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
             if kpb.startswith(pb_sha) or pb_sha.startswith(kpb)),
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
            Language.C, overwrite=False,
        )
    except Exception:
        log.error("Row %s: bp_warper raised:\n%s", row["id"], traceback.format_exc())
        bp_result = {"error": ErrorCode.EXCEPTION.value}

    bp_error    = bp_result.get("error", "")
    fixed_code  = bp_result.get("fixed_code")
    bp_usage    = bp_result.get("usage", llm.LLMUsage())
    method_used = "mystique"

    # ---- LLM fallback ----
    # If Mystique's pipeline stopped BEFORE reaching its own LLM call
    # (e.g. no Joern, method not found, etc.) we call the LLM directly
    # with the stored raw diff + target file content.
    if fixed_code is None and bp_error in _PRE_LLM_ERRORS:
        log.info("Row %s: bp() stopped at '%s' — running direct LLM fallback",
                 row["id"], bp_error)
        stored_patch = row.get("new_version_patch") or ""
        target_code  = bp_result.get("target") or c_pc
        fb_usage = llm.LLMUsage()
        try:
            fixed_code = llm.llm_fix(stored_patch, target_code, Language.C, fb_usage)
        except Exception:
            log.error("Row %s: direct LLM call raised:\n%s",
                      row["id"], traceback.format_exc())
            fixed_code = None

        # Merge token counts (bp may have spent 0 tokens; fallback adds on top)
        bp_usage = llm.LLMUsage(
            calls            = bp_usage.calls + fb_usage.calls,
            input_tokens     = bp_usage.input_tokens + fb_usage.input_tokens,
            output_tokens    = bp_usage.output_tokens + fb_usage.output_tokens,
            reasoning_tokens = bp_usage.reasoning_tokens + fb_usage.reasoning_tokens,
        )
        method_used = "mystique-llm-fallback"

    elapsed = time.time() - t0
    cost    = estimate_cost(bp_usage)
    status  = "done" if fixed_code is not None else "error"

    return dict(
        method          = method_used,
        generated_patch = fixed_code,
        elapsed         = elapsed,
        usage           = bp_usage,
        api_cost        = cost,
        status          = status,
        error_detail    = bp_error if fixed_code is None else "",
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
    args = parser.parse_args()

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
        else:
            stats["error"] += 1
            log.warning("  -> ERROR %s", result.get("error_detail", ""))

    log.info(
        "=== Phase 2 complete. done=%d (of which fallback=%d) error=%d ===",
        stats["done"], stats["fallback"], stats["error"],
    )


if __name__ == "__main__":
    main()
