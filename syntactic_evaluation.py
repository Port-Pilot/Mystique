"""
syntactic_evaluation.py

Automated syntactic-equivalence evaluation for patch-porting / backporting
results, stored in a
Postgres table `backport_benchmark_results`.

Compares each generated patch against its corresponding ground-truth
(developer-written) patch along two independent axes, following the same
distinction FixMorph draws between "correct content" and "correct location":

  1. Content match
     - Parses unified diffs into op-tagged ('+' / '-') token streams.
     - Normalizes each line by stripping comments and re-tokenizing, so
       whitespace/formatting-only differences don't cause false mismatches.
     - Tokens are flattened across lines (not compared line-by-line), so
       the same change reflowed across a different number of lines still
       registers as an exact match.
     - Produces both a binary `content_match` and a continuous
       `content_similarity` (difflib ratio, weighted by token volume).

  2. Location match
     - Extracts touched file path(s) from '---'/'+++' diff headers.
     - Extracts the enclosing-function context from the text following
       '@@ ... @@' hunk headers, when present.
     - Two patches are considered location-matched if their files agree
       and, when available, their hunk contexts agree.
     - When neither side has usable location information in the diff
       (e.g. patches stored as bare hunks without file headers), the
       result is left unresolved (`location_match = None`) rather than
       assumed correct, and the row is flagged `manual_review_needed`.

Final verdict:
  syntactic_match = content_match AND (location_match is not False)

This mirrors FixMorph's "Syntactic" column (patches identical to the
developer's backported patch) but computes it automatically instead of
via manual inspection. It does NOT evaluate semantic equivalence:
patches with `syntactic_match = False` but `compilation_success = True`
are exactly the population that still needs semantic review (manual,
test-based, or LLM-assisted), the way FixMorph's authors did by hand for
patches that were behaviorally correct but not textually identical.

Usage:
    python syntactic_evaluation.py
    python syntactic_evaluation.py --dataset FixMorph-Main --method mystique
    python syntactic_evaluation.py --dataset FixMorph-Main --method mystique --dry-run
    python syntactic_evaluation.py --recompute --limit 50

Environment:
    NEON_DATABASE_URL   Postgres connection string (or pass --database-url)
"""

import argparse
import difflib
import os
import re
import sys
from datetime import datetime, timezone
from dotenv import load_dotenv

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("Missing dependency. Install with: pip install psycopg2-binary", file=sys.stderr)
    sys.exit(1)


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------

C_LIKE_LANGS = {
    "c", "cpp", "c++", "java", "javascript", "js", "typescript", "ts",
    "go", "rust", "csharp", "c#", "kotlin", "swift", "scala",
}
HASH_COMMENT_LANGS = {"python", "py", "ruby", "rb", "shell", "bash", "perl"}

HUNK_HEADER_RE = re.compile(r"^@@\s*-\d+(?:,\d+)?\s*\+\d+(?:,\d+)?\s*@@(.*)$")
FILE_HEADER_OLD_RE = re.compile(r"^---\s+(\S+)")
FILE_HEADER_NEW_RE = re.compile(r"^\+\+\+\s+(\S+)")

# Strip common diff prefixes (a/, b/) and trailing timestamps so that
# "a/drivers/foo.c" and "drivers/foo.c" (or "/dev/null") compare sensibly.
_PATH_PREFIX_RE = re.compile(r"^[ab]/")


def strip_comments(text: str, language: str) -> str:
    lang = (language or "").strip().lower()
    if lang in C_LIKE_LANGS:
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        text = re.sub(r"//.*", "", text)
    elif lang in HASH_COMMENT_LANGS:
        text = re.sub(r"#.*", "", text)
    return text


_TOKEN_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*"          # identifiers/keywords
    r"|0[xX][0-9a-fA-F]+"              # hex literals
    r"|\d+\.\d+|\d+"                   # numeric literals
    r"|->|::|\+\+|--|&&|\|\||==|!=|<=|>=|<<|>>|[-+*/%&|^!=<>]="  # multi-char ops
    r"|\S"                             # any other single non-space char
)


def normalize_line(line: str, language: str) -> str:
    line = strip_comments(line, language)
    tokens = _TOKEN_RE.findall(line)
    return " ".join(tokens)


def normalize_path(path: str) -> str:
    if not path:
        return ""
    path = path.strip()
    path = _PATH_PREFIX_RE.sub("", path)
    # Strip trailing tab + timestamp some diff tools append
    path = path.split("\t")[0]
    return path


def normalize_context(context: str, language: str) -> str:
    """Normalize the function-context text trailing a '@@ ... @@' hunk header."""
    if not context:
        return ""
    return normalize_line(context, language)


# --------------------------------------------------------------------------
# Diff parsing: pulls out file paths, hunk contexts, and op-tagged content
# --------------------------------------------------------------------------

def parse_patch(patch_text: str, language: str):
    """
    Returns a dict:
      is_diff: bool
      files: sorted list of normalized file paths touched
      hunk_contexts: list of normalized non-empty function-context strings
      added_tokens: flat list of normalized tokens from '+' lines (diff mode)
                    or all lines (raw-code mode)
      removed_tokens: flat list of normalized tokens from '-' lines
                    (empty in raw-code mode)
    """
    lines = patch_text.splitlines() if patch_text else []
    is_unified_diff = any(HUNK_HEADER_RE.match(l) for l in lines)

    files = set()
    hunk_contexts = []
    added_tokens = []
    removed_tokens = []

    if not is_unified_diff:
        # Raw code snippet, not a diff: treat every non-empty line as content,
        # no location information available.
        for l in lines:
            nl = normalize_line(l, language)
            if nl:
                added_tokens.append(nl)
        return {
            "is_diff": False,
            "files": [],
            "hunk_contexts": [],
            "added_tokens": added_tokens,
            "removed_tokens": [],
        }

    for l in lines:
        m_old = FILE_HEADER_OLD_RE.match(l)
        m_new = FILE_HEADER_NEW_RE.match(l)
        m_hunk = HUNK_HEADER_RE.match(l)

        if m_old:
            p = normalize_path(m_old.group(1))
            if p and p != "dev/null":
                files.add(p)
            continue
        if m_new:
            p = normalize_path(m_new.group(1))
            if p and p != "dev/null":
                files.add(p)
            continue
        if m_hunk:
            ctx = normalize_context(m_hunk.group(1), language)
            if ctx:
                hunk_contexts.append(ctx)
            continue
        if l.startswith("diff ") or l.startswith("index ") or l.startswith("new file mode") \
                or l.startswith("deleted file mode") or l.startswith("similarity index") \
                or l.startswith("rename from") or l.startswith("rename to"):
            continue
        if l.startswith("+"):
            nl = normalize_line(l[1:], language)
            if nl:
                added_tokens.extend(nl.split(" "))
            continue
        if l.startswith("-"):
            nl = normalize_line(l[1:], language)
            if nl:
                removed_tokens.extend(nl.split(" "))
            continue
        # unchanged context lines are intentionally dropped

    return {
        "is_diff": True,
        "files": sorted(files),
        "hunk_contexts": hunk_contexts,
        "added_tokens": added_tokens,
        "removed_tokens": removed_tokens,
    }


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------

def compare_patches(generated_patch: str, ground_truth_patch: str, language: str):
    gen = parse_patch(generated_patch, language)
    gt = parse_patch(ground_truth_patch, language)

    if not gen["added_tokens"] and not gen["removed_tokens"] \
            and not gt["added_tokens"] and not gt["removed_tokens"]:
        return {
            "content_match": None,
            "content_similarity": None,
            "file_match": None,
            "location_match": None,
            "syntactic_match": None,
            "manual_review_needed": True,
            "syntactic_diff": None,
            "syntactic_eval_method": "normalized-diff-tokens (both empty)",
        }

    # --- content comparison (flat, op-tagged token streams) ---
    added_match = gen["added_tokens"] == gt["added_tokens"]
    removed_match = gen["removed_tokens"] == gt["removed_tokens"]
    content_match = added_match and removed_match

    added_sim = difflib.SequenceMatcher(None, gen["added_tokens"], gt["added_tokens"]).ratio()
    removed_sim = difflib.SequenceMatcher(None, gen["removed_tokens"], gt["removed_tokens"]).ratio()
    # Weight by token volume so an empty side doesn't distort the average
    total_gt_tokens = len(gt["added_tokens"]) + len(gt["removed_tokens"])
    if total_gt_tokens == 0:
        content_similarity = added_sim if gt["added_tokens"] or gen["added_tokens"] else removed_sim
    else:
        content_similarity = (
            added_sim * len(gt["added_tokens"]) + removed_sim * len(gt["removed_tokens"])
        ) / total_gt_tokens

    # --- location comparison ---
    file_match = None
    if gen["files"] and gt["files"]:
        file_match = gen["files"] == gt["files"]

    context_match = None
    if gen["hunk_contexts"] and gt["hunk_contexts"]:
        context_match = sorted(gen["hunk_contexts"]) == sorted(gt["hunk_contexts"])

    manual_review_needed = False
    if file_match is False or context_match is False:
        location_match = False
    elif file_match is True and (context_match is not False):
        location_match = True
    elif context_match is True:
        location_match = True
    else:
        # No usable location signal on one or both sides -- can't confirm
        # automatically. Don't silently assume it's correct.
        location_match = None
        manual_review_needed = True

    syntactic_match = bool(content_match) and (location_match is not False)
    if location_match is None:
        manual_review_needed = True

    diff_text = ""
    if not content_match:
        diff_text = "\n".join(
            difflib.unified_diff(
                gt["removed_tokens"] + ["---"] + gt["added_tokens"],
                gen["removed_tokens"] + ["---"] + gen["added_tokens"],
                fromfile="ground_truth (normalized)",
                tofile="generated (normalized)",
                lineterm="",
            )
        )

    return {
        "content_match": content_match,
        "content_similarity": round(content_similarity, 6),
        "file_match": file_match,
        "location_match": location_match,
        "syntactic_match": syntactic_match,
        "manual_review_needed": manual_review_needed,
        "syntactic_diff": diff_text,
        "syntactic_eval_method": "normalized-diff-tokens+location",
    }


# --------------------------------------------------------------------------
# DB I/O
# --------------------------------------------------------------------------

SELECT_SQL_TEMPLATE = """
    SELECT id, programming_language, generated_patch, old_version_patch
    FROM backport_benchmark_results
    WHERE generated_patch IS NOT NULL
      AND old_version_patch IS NOT NULL
      {compile_filter}
      {recompute_filter}
      {dataset_filter}
      {method_filter}
    ORDER BY id
    {limit_clause}
"""

# NOTE: run this once against your schema before using the script:
#
# ALTER TABLE backport_benchmark_results
#   ADD COLUMN IF NOT EXISTS content_match BOOLEAN,
#   ADD COLUMN IF NOT EXISTS content_similarity DOUBLE PRECISION,
#   ADD COLUMN IF NOT EXISTS file_match BOOLEAN,
#   ADD COLUMN IF NOT EXISTS location_match BOOLEAN,
#   ADD COLUMN IF NOT EXISTS syntactic_match BOOLEAN,
#   ADD COLUMN IF NOT EXISTS manual_review_needed BOOLEAN,
#   ADD COLUMN IF NOT EXISTS syntactic_diff TEXT,
#   ADD COLUMN IF NOT EXISTS syntactic_eval_method TEXT,
#   ADD COLUMN IF NOT EXISTS syntactic_evaluated_at TIMESTAMPTZ;

UPDATE_SQL = """
    UPDATE backport_benchmark_results
    SET content_match = %(content_match)s,
        content_similarity = %(content_similarity)s,
        file_match = %(file_match)s,
        location_match = %(location_match)s,
        syntactic_match = %(syntactic_match)s,
        manual_review_needed = %(manual_review_needed)s,
        syntactic_diff = %(syntactic_diff)s,
        syntactic_eval_method = %(syntactic_eval_method)s,
        syntactic_evaluated_at = %(syntactic_evaluated_at)s,
        updated_at = NOW()
    WHERE id = %(id)s
"""


def run(args):
    load_dotenv()

    dsn = args.database_url or os.getenv("NEON_DATABASE_URL")

    if not dsn:
        print(
            "ERROR: set --database-url or the NEON_DATABASE_URL environment variable.",
            file=sys.stderr,
        )
        sys.exit(1)

    compile_filter = "" if args.include_non_compiling else "AND compilation_success = TRUE"
    recompute_filter = "" if args.recompute else "AND syntactic_evaluated_at IS NULL"
    dataset_filter = "AND dataset = %(dataset)s" if args.dataset else ""
    method_filter = "AND method = %(method)s" if args.method else ""
    limit_clause = f"LIMIT {int(args.limit)}" if args.limit else ""

    select_sql = SELECT_SQL_TEMPLATE.format(
        compile_filter=compile_filter,
        recompute_filter=recompute_filter,
        dataset_filter=dataset_filter,
        method_filter=method_filter,
        limit_clause=limit_clause,
    )

    params = {}
    if args.dataset:
        params["dataset"] = args.dataset
    if args.method:
        params["method"] = args.method

    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(select_sql, params)
            rows = cur.fetchall()

        print(f"Fetched {len(rows)} row(s) to evaluate "
              f"(include_non_compiling={args.include_non_compiling}, recompute={args.recompute}).")

        if not rows:
            print("Nothing to do.")
            return

        n_match, n_mismatch, n_skipped, n_error, n_manual = 0, 0, 0, 0, 0

        with conn.cursor() as cur:
            for i, row in enumerate(rows, 1):
                rid = row["id"]
                try:
                    result = compare_patches(
                        row["generated_patch"],
                        row["old_version_patch"],
                        row["programming_language"],
                    )
                    result["id"] = rid
                    result["syntactic_evaluated_at"] = datetime.now(timezone.utc)

                    if result["syntactic_match"] is None:
                        n_skipped += 1
                    elif result["syntactic_match"]:
                        n_match += 1
                    else:
                        n_mismatch += 1
                    if result["manual_review_needed"]:
                        n_manual += 1

                    if not args.dry_run:
                        cur.execute(UPDATE_SQL, result)

                except Exception as e:  # noqa: BLE001 - keep going on per-row failures
                    n_error += 1
                    print(f"  [id={rid}] ERROR: {e}", file=sys.stderr)

                if i % 100 == 0:
                    print(f"  ...processed {i}/{len(rows)}")

        if args.dry_run:
            conn.rollback()
            print("\nDRY RUN -- no changes written to the database.")
        else:
            conn.commit()
            print("\nChanges committed.")

        print(f"\nSummary: {n_match} syntactic matches, {n_mismatch} mismatches, "
              f"{n_skipped} skipped (empty patches), {n_error} errors, "
              f"{len(rows)} total.")
        print(f"Flagged for manual review (location unconfirmed): {n_manual}")
        if n_match + n_mismatch:
            pct = 100.0 * n_match / (n_match + n_mismatch)
            print(f"Syntactic equivalence rate (of comparable rows): {pct:.1f}%")

    finally:
        conn.close()


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--database-url", help="Postgres connection string. Defaults to $NEON_DATABASE_URL.")
    p.add_argument("--dataset", help="Only evaluate rows where dataset = this value (e.g. 'fixmorph').")
    p.add_argument("--method", help="Only evaluate rows where method = this value (e.g. 'mystique').")
    p.add_argument("--limit", type=int, help="Max number of rows to process.")
    p.add_argument("--include-non-compiling", action="store_true",
                    help="By default only compilation_success=TRUE rows are evaluated "
                         "(plausible patches). Pass this to evaluate all rows regardless.")
    p.add_argument("--recompute", action="store_true",
                    help="Re-evaluate rows that already have a syntactic_evaluated_at timestamp.")
    p.add_argument("--dry-run", action="store_true", help="Compute results but do not write to the DB.")
    return p.parse_args()


if __name__ == "__main__":
    run(parse_args())