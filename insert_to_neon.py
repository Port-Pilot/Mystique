"""
insert_to_neon.py

Ingests FixMorph's Main-data-set.xlsx (350 records) into the Neon
`backport_benchmark_results` table. CVE-data-set.xlsx is intentionally NOT
used -- this dataset is Main-data-set.xlsx only.

Confirmed direction (per FixMorph README / ISSTA'21 paper):
    pa -> pb   mainline diff (the original fix)         -> new_version_patch
    pc -> pe   target-branch diff (the real backport,
               i.e. ground truth)                       -> old_version_patch
    pc alone (no pe) is just the starting state FixMorph works from -- NOT
    a scorable case, since there's no ground truth to compare against.

pe is documented as optional. Since old_version_patch_commit_url is NOT NULL
on this table, any case missing pe is skipped (logged, not silently dropped).

Main-data-set.xlsx columns used:
    ID              -> case reference (for logging only, not stored)
    Patch Type      -> patch_type
    Commit - Pa     -> pa
    Commit - Pb     -> pb
    Commit - Pc     -> pc
    Commit - Pe     -> pe
    Program AB      -> source file path for pa/pb (mainline side)
    Program CE      -> source file path for pc/pe (target side)

`method` is NOT NULL on this table but nothing has run yet at ingest time, so
rows are inserted with method='pending' and the default status='ready'. Your
phase-2 generation script should:
    SELECT * FROM backport_benchmark_results WHERE status = 'ready'
run the LLM, then UPDATE method / generated_patch / status accordingly.

No local Linux kernel clone is required -- file content is fetched from
raw.githubusercontent.com and cached locally in sqlite so re-runs don't
re-fetch anything already retrieved.

Requirements:
    pip install pandas psycopg2-binary requests openpyxl python-dotenv --break-system-packages

Environment variables (loaded from a .env file if present):
    NEON_DATABASE_URL   Neon Postgres connection string (pooled -pooler host)
    GITHUB_TOKEN        GitHub PAT, public_repo read scope (not strictly required
                         for raw.githubusercontent.com fetches, but harmless to set)

Usage:
    python insert_to_neon.py --main FixMorph-Dataset/Main-data-set.xlsx
"""

import argparse
import difflib
import logging
import os
import sqlite3
import time
from dotenv import load_dotenv

import pandas as pd
import psycopg2
import requests
from psycopg2.extras import execute_values

load_dotenv()

# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------

PROGRAMMING_LANGUAGE = "C"
GITHUB_OWNER = "torvalds"
GITHUB_REPO = "linux"
PROJECT_NAME = "linux"
DATASET_NAME = "FixMorph-Main"

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
NEON_DATABASE_URL = os.getenv("NEON_DATABASE_URL")

CACHE_DB_PATH = "github_fetch_cache.sqlite"
BATCH_SIZE = 50
REQUEST_TIMEOUT = 15

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("insert_to_neon")


# --------------------------------------------------------------------------------------
# Local cache (sha, path) -> file content, so re-runs don't re-hit GitHub
# --------------------------------------------------------------------------------------

_cache_conn = sqlite3.connect(CACHE_DB_PATH)
_cache_conn.execute(
    "CREATE TABLE IF NOT EXISTS files "
    "(sha TEXT, path TEXT, content TEXT, missing INTEGER DEFAULT 0, PRIMARY KEY (sha, path))"
)
_cache_conn.commit()


def _cache_get_file(sha: str, path: str):
    row = _cache_conn.execute(
        "SELECT content, missing FROM files WHERE sha=? AND path=?", (sha, path)
    ).fetchone()
    if row is None:
        return None  # never attempted
    content, missing = row
    return "" if missing else content


def _cache_set_file(sha: str, path: str, content: str | None) -> None:
    _cache_conn.execute(
        "INSERT OR REPLACE INTO files VALUES (?,?,?,?)",
        (sha, path, content or "", 0 if content is not None else 1),
    )
    _cache_conn.commit()


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------

def clean_val(val):
    if pd.isna(val):
        return None
    v = str(val).strip()
    return v if v else None


def object_to_source_path(o_path: str) -> str:
    """FixMorph stores compiled .o paths; source is almost always the same path
    with a .c extension. A small number of files may be .S or built from multiple
    sources -- those will fail to fetch (logged, diff left NULL) rather than crash."""
    if o_path.endswith(".o"):
        return o_path[:-2] + ".c"
    return o_path


def get_file_at_commit(sha: str, path: str) -> str | None:
    if not sha or not path:
        return None
    cached = _cache_get_file(sha, path)
    if cached is not None:
        return cached or None
    url = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/{sha}/{path}"
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        except Exception as e:
            log.warning(f"Error fetching {path} @ {sha}: {e}; retrying...")
            time.sleep(2)
            continue
        if resp.status_code == 200:
            _cache_set_file(sha, path, resp.text)
            return resp.text
        if resp.status_code == 404:
            log.warning(f"Not found: {path} @ {sha}")
            _cache_set_file(sha, path, None)
            return None
        if resp.status_code == 429:
            time.sleep(5)
            continue
        log.warning(f"HTTP {resp.status_code} fetching {path} @ {sha}")
        _cache_set_file(sha, path, None)
        return None
    return None


def unified_diff_text(before: str | None, after: str | None, path: str) -> str | None:
    if before is None or after is None:
        return None
    diff_lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(diff_lines)


# --------------------------------------------------------------------------------------
# DB layer
# --------------------------------------------------------------------------------------

def get_conn():
    return psycopg2.connect(NEON_DATABASE_URL)


def fetch_existing_pairs(conn) -> set[tuple[str, str]]:
    """Dedup key: (new_version_patch_commit_url, old_version_patch_commit_url),
    i.e. (pb, pe) -- unique per case, so re-running this script is safe."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT new_version_patch_commit_url, old_version_patch_commit_url "
            "FROM backport_benchmark_results"
        )
        return {(r[0], r[1]) for r in cur.fetchall()}


INSERT_SQL = """
    INSERT INTO backport_benchmark_results (
        dataset, project, programming_language,
        new_version_patch_commit_url, new_version_patch,
        old_version_patch_commit_url, old_version_patch,
        patch_type, method
    ) VALUES %s
"""


def insert_rows(conn, rows: list[tuple]) -> None:
    if not rows:
        log.info("No new rows to insert.")
        return
    with conn.cursor() as cur:
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i : i + BATCH_SIZE]
            execute_values(cur, INSERT_SQL, batch)
            conn.commit()
            log.info(f"Inserted batch {i // BATCH_SIZE + 1} ({len(batch)} rows)")


# --------------------------------------------------------------------------------------
# Main-data-set.xlsx processing
# --------------------------------------------------------------------------------------

def process_row(case_ref: str, pa, pb, pc, pe, path_ab, path_ce, patch_type,
                 existing_pairs: set, skip_counts: dict) -> tuple | None:
    if not pe:
        skip_counts["missing_pe"] += 1
        log.info(f"{case_ref}: no ground-truth commit (pe) -- skipping (not scorable).")
        return None

    if not all([pa, pb, pc, path_ab, path_ce]):
        skip_counts["missing_fields"] += 1
        log.warning(f"{case_ref}: missing required field(s), skipping.")
        return None

    new_url = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/commit/{pb}"
    old_url = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/commit/{pe}"

    if (new_url, old_url) in existing_pairs:
        skip_counts["already_present"] += 1
        return None

    ref_path = object_to_source_path(path_ab)
    target_path = object_to_source_path(path_ce)

    content_pa = get_file_at_commit(pa, ref_path)
    content_pb = get_file_at_commit(pb, ref_path)
    content_pc = get_file_at_commit(pc, target_path)
    content_pe = get_file_at_commit(pe, target_path)

    new_version_patch = unified_diff_text(content_pa, content_pb, ref_path)
    old_version_patch = unified_diff_text(content_pc, content_pe, target_path)

    if new_version_patch is None:
        log.warning(f"{case_ref}: could not build mainline diff ({ref_path}).")
    if old_version_patch is None:
        log.warning(f"{case_ref}: could not build target-branch diff ({target_path}).")

    return (
        DATASET_NAME,
        PROJECT_NAME,
        PROGRAMMING_LANGUAGE,
        new_url,
        new_version_patch,
        old_url,
        old_version_patch,
        clean_val(patch_type),
        "pending",  # method: filled in by phase-2 generation script
    )


def process_main_file(path: str, existing_pairs: set, skip_counts: dict) -> list[tuple]:
    df = pd.read_excel(path)
    log.info(f"Loaded {len(df)} rows from {path}")
    rows = []
    for _, r in df.iterrows():
        case_ref = f"main#{clean_val(r.get('ID'))}"
        row = process_row(
            case_ref=case_ref,
            pa=clean_val(r.get("Commit - Pa")),
            pb=clean_val(r.get("Commit - Pb")),
            pc=clean_val(r.get("Commit - Pc")),
            pe=clean_val(r.get("Commit - Pe")),
            path_ab=clean_val(r.get("Program AB")),
            path_ce=clean_val(r.get("Program CE")),
            patch_type=r.get("Patch Type"),
            existing_pairs=existing_pairs,
            skip_counts=skip_counts,
        )
        if row is not None:
            rows.append(row)
            existing_pairs.add((row[3], row[5]))  # new_url, old_url
        time.sleep(0.05)
    return rows


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--main", required=True, help="Path to Main-data-set.xlsx")
    args = parser.parse_args()

    conn = get_conn()
    existing_pairs = fetch_existing_pairs(conn)
    log.info(f"{len(existing_pairs)} rows already present; will skip those.")
    conn.close()

    skip_counts = {"missing_pe": 0, "missing_fields": 0, "already_present": 0}

    log.info(f"Processing {args.main} ...")
    main_rows = process_main_file(args.main, existing_pairs, skip_counts)
    log.info(f"Built {len(main_rows)} rows from Main-data-set.xlsx")

    conn = get_conn()
    insert_rows(conn, main_rows)
    conn.close()

    log.info(
        f"Done. Inserted {len(main_rows)} rows. "
        f"Skipped -- no ground truth (pe): {skip_counts['missing_pe']}, "
        f"missing fields: {skip_counts['missing_fields']}, "
        f"already present: {skip_counts['already_present']}."
    )


if __name__ == "__main__":
    main()