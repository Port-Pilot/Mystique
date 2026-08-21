#!/usr/bin/env python3
"""
syntactic_evaluation.py

Automated syntactic-equivalence evaluation for patch-porting / backporting
results, stored in `backport_benchmark_results_mystique_new`.

Two independent measures are written per row:

  syntactic_match (BOOLEAN, nullable)
      STRICT, FixMorph-comparable. True only if the generated patch's
      changed content is EXACTLY equal to the ground truth's after
      normalization (comments stripped, whitespace/spacing differences
      neutralized), AND the location (file, and hunk context when
      available) is confirmed to match. No partial credit -- this is
      the metric to compare against FixMorph's published "Syntactic"
      percentages. NULL means it couldn't be confidently resolved
      (see manual_review_needed) rather than being silently counted
      as a pass or fail.

  content_similarity / high_similarity_match / added_coverage /
  removal_completeness (DIAGNOSTIC ONLY)
      A fuzzy, threshold-based containment score tolerant of hunk-shape
      and reformatting differences. NOT comparable to FixMorph's
      syntactic numbers -- a patch can score 0.97 here and still be a
      real, meaningful behavioral difference (see the script's test
      suite for a concrete example: a security-relevant bounds check
      flipped from '>' to '>=' inside an otherwise-identical 20-line
      block scores 0.977 similarity but is NOT a syntactic match).
      Its purpose is to triage the non-matching rows: high-similarity
      mismatches are the best candidates to check first for FixMorph's
      "Semantic but not Syntactic" bucket (same behavior, different
      code) during your semantic review pass.

manual_review_needed (BOOLEAN)
    True whenever syntactic_match could not be confidently resolved
    (content matched exactly but location was unparseable from the
    diff -- e.g. no file headers present). These rows need a human (or
    LLM-assisted) look, same as the harder cases FixMorph's own authors
    resolved by hand.

Usage:
    python syntactic_evaluation.py
    python syntactic_evaluation.py --dataset FixMorph-Main --method mystique
    python syntactic_evaluation.py --dataset FixMorph-Main --method mystique --dry-run
    python syntactic_evaluation.py --recompute --limit 50
    python syntactic_evaluation.py --similarity-threshold 0.8

Environment:
    NEON_DATABASE_URL   Postgres connection string (or pass --database-url)
"""

import argparse
import os
import sys
from datetime import datetime, timezone
import collections

from dotenv import load_dotenv

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("Missing dependency. Install with: pip install psycopg2-binary", file=sys.stderr)
    sys.exit(1)

from syntactic_evaluation_core import compare_patches, DEFAULT_SIMILARITY_THRESHOLD


# --------------------------------------------------------------------------
# DB I/O
# --------------------------------------------------------------------------

SELECT_SQL_TEMPLATE = """
    SELECT id, programming_language, generated_patch, old_version_patch
    FROM backport_benchmark_results_mystique_new
    WHERE generated_patch IS NOT NULL
      AND old_version_patch IS NOT NULL
      {compile_filter}
      {recompute_filter}
      {dataset_filter}
      {method_filter}
    ORDER BY id
    {limit_clause}
"""

UPDATE_SQL = """
    UPDATE backport_benchmark_results_mystique_new
    SET syntactic_match = %(syntactic_match)s,
        content_exact_match = %(content_exact_match)s,
        location_match = %(location_match)s,
        location_resolved = %(location_resolved)s,
        manual_review_needed = %(manual_review_needed)s,
        content_similarity = %(content_similarity)s,
        added_coverage = %(added_coverage)s,
        removal_completeness = %(removal_completeness)s,
        high_similarity_match = %(high_similarity_match)s,
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
        print("ERROR: set --database-url or the NEON_DATABASE_URL environment variable.", file=sys.stderr)
        sys.exit(1)

    compile_filter = "" if args.include_non_compiling else "AND compilation_success = TRUE"
    recompute_filter = "" if args.recompute else "AND syntactic_evaluated_at IS NULL"
    dataset_filter = "AND dataset = %(dataset)s" if args.dataset else ""
    method_filter = "AND method = %(method)s" if args.method else ""
    limit_clause = f"LIMIT {int(args.limit)}" if args.limit else ""

    select_sql = SELECT_SQL_TEMPLATE.format(
        compile_filter=compile_filter, recompute_filter=recompute_filter,
        dataset_filter=dataset_filter, method_filter=method_filter, limit_clause=limit_clause,
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
              f"(include_non_compiling={args.include_non_compiling}, recompute={args.recompute}, "
              f"similarity_threshold={args.similarity_threshold}).")

        if not rows:
            print("Nothing to do.")
            return

        n_match, n_mismatch, n_unresolved, n_error = 0, 0, 0, 0
        n_manual = 0
        high_sim_among_mismatches = collections.Counter()

        with conn.cursor() as cur:
            for i, row in enumerate(rows, 1):
                rid = row["id"]
                try:
                    result = compare_patches(
                        row["generated_patch"], row["old_version_patch"], row["programming_language"],
                        similarity_threshold=args.similarity_threshold,
                    )
                    result["id"] = rid
                    result["syntactic_evaluated_at"] = datetime.now(timezone.utc)

                    if result["syntactic_match"] is True:
                        n_match += 1
                    elif result["syntactic_match"] is False:
                        n_mismatch += 1
                        if result["content_similarity"] is not None:
                            bucket = f"{int(result['content_similarity']*10)*10}-{int(result['content_similarity']*10)*10+10}%"
                            high_sim_among_mismatches[bucket] += 1
                    else:
                        n_unresolved += 1

                    if result["manual_review_needed"]:
                        n_manual += 1

                    if not args.dry_run:
                        cur.execute(UPDATE_SQL, result)

                except Exception as e:  # noqa: BLE001
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

        total_resolved = n_match + n_mismatch
        print(f"\n=== STRICT syntactic_match (FixMorph-comparable) ===")
        print(f"  Match:      {n_match}")
        print(f"  Mismatch:   {n_mismatch}")
        print(f"  Unresolved: {n_unresolved}  (location couldn't be confirmed -- see manual_review_needed)")
        print(f"  Errors:     {n_error}")
        print(f"  Total rows: {len(rows)}")
        if total_resolved:
            pct = 100.0 * n_match / total_resolved
            print(f"  Syntactic equivalence rate (of resolved rows): {pct:.1f}%")
        print(f"\nFlagged manual_review_needed: {n_manual}")

        if high_sim_among_mismatches:
            print(f"\ncontent_similarity distribution among strict MISMATCHES "
                  f"(diagnostic -- high scores here are your best semantic-review candidates):")
            for k in sorted(high_sim_among_mismatches, key=lambda b: b):
                print(f"  {k}: {high_sim_among_mismatches[k]}")

    finally:
        conn.close()


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--database-url", help="Postgres connection string. Defaults to $NEON_DATABASE_URL.")
    p.add_argument("--dataset", help="Only evaluate rows where dataset = this value.")
    p.add_argument("--method", help="Only evaluate rows where method = this value.")
    p.add_argument("--limit", type=int, help="Max number of rows to process.")
    p.add_argument("--include-non-compiling", action="store_true",
                    help="By default only compilation_success=TRUE rows are evaluated. "
                         "Pass this to evaluate all rows regardless.")
    p.add_argument("--recompute", action="store_true",
                    help="Re-evaluate rows that already have a syntactic_evaluated_at timestamp.")
    p.add_argument("--dry-run", action="store_true", help="Compute results but do not write to the DB.")
    p.add_argument("--similarity-threshold", type=float, default=DEFAULT_SIMILARITY_THRESHOLD,
                    help=f"Threshold for the DIAGNOSTIC high_similarity_match column only -- "
                         f"has no effect on syntactic_match. Default {DEFAULT_SIMILARITY_THRESHOLD}.")
    return p.parse_args()


if __name__ == "__main__":
    run(parse_args())
    

# """
# syntactic_evaluation.py

# Automated syntactic-equivalence evaluation for patch-porting / backporting
# results, stored in a Postgres table `backport_benchmark_results_mystique`.

# Compares each generated patch against its corresponding ground-truth
# (developer-written) patch along two independent axes, following the same
# distinction FixMorph draws between "correct content" and "correct location":

#   1. Content match
#      - Parses unified diffs into (a) op-tagged ('+' / '-') token streams,
#        and (b) a reconstructed "post-image" token stream -- the resulting
#        code across all hunks (context + added lines, removed lines
#        dropped), in original order.
#      - Normalizes each line by stripping comments and re-tokenizing, so
#        whitespace/formatting-only differences don't cause false mismatches.
#      - Rather than comparing the two patches' raw +/- token streams
#        directly (which breaks whenever one patch reformats or rewrites a
#        larger surrounding scope than the other -- e.g. an LLM re-emitting
#        a whole function with different brace style vs. a developer's
#        minimal 2-line insertion expressing the identical fix), content
#        match is computed via CONTAINMENT: what fraction of the ground
#        truth's added tokens are recoverable (as an ordered, but not
#        necessarily contiguous, subsequence -- tolerating interspersed
#        reformatting noise) inside the generated patch's reconstructed
#        resulting code, and what fraction of the ground truth's removed
#        tokens are confirmed absent from it.
#      - Produces both a binary `content_match` and a continuous
#        `content_similarity`.

#   2. Location match
#      - Extracts touched file path(s) from '---'/'+++' diff headers.
#      - Extracts the enclosing-function context from the text following
#        '@@ ... @@' hunk headers, when present.
#      - Two patches are considered location-matched if their files agree
#        and, when available, their hunk contexts agree.
#      - When neither side has usable location information in the diff
#        (e.g. patches stored as bare hunks without file headers), the
#        result is left unresolved (`location_match = None`) rather than
#        assumed correct, and the row is flagged `manual_review_needed`.

# Final verdict:
#   syntactic_match = content_match AND (location_match is not False)

# This mirrors FixMorph's "Syntactic" column (patches identical to the
# developer's backported patch) but computes it automatically instead of
# via manual inspection. It does NOT evaluate semantic equivalence:
# patches with `syntactic_match = False` but `compilation_success = True`
# are exactly the population that still needs semantic review (manual,
# test-based, or LLM-assisted), the way FixMorph's authors did by hand for
# patches that were behaviorally correct but not textually identical.

# KNOWN LIMITATION: post-image reconstruction is built purely from each
# patch's own hunk context lines, not by applying the patch to the shared
# original source file. If the ground-truth diff itself uses very little
# context (e.g. --unified=0) while the generated diff rewrites a much
# larger region, coverage can still be conservative in the opposite
# direction. If you have access to the actual pre-patch target file
# content (most FixMorph-style pipelines fetch this already, e.g. via
# insert_to_neon.py's cached blobs), a more robust version of this script
# would apply both patches to that shared base and compare the two full
# resulting files/functions directly instead of reconstructing post-image
# from hunk context alone. Ask before implementing this if you have that
# column/field available -- it removes this limitation entirely.

# Usage:
#     python syntactic_evaluation.py
#     python syntactic_evaluation.py --dataset FixMorph-Main --method mystique
#     python syntactic_evaluation.py --dataset FixMorph-Main --method mystique --dry-run
#     python syntactic_evaluation.py --recompute --limit 50
#     python syntactic_evaluation.py --threshold 0.8

# Environment:
#     NEON_DATABASE_URL   Postgres connection string (or pass --database-url)
# """

# import argparse
# import difflib
# import os
# import re
# import sys
# from datetime import datetime, timezone
# from dotenv import load_dotenv
# import collections
# import math

# try:
#     import psycopg2
#     import psycopg2.extras
# except ImportError:
#     print("Missing dependency. Install with: pip install psycopg2-binary", file=sys.stderr)
#     sys.exit(1)


# # --------------------------------------------------------------------------
# # Normalization
# # --------------------------------------------------------------------------

# C_LIKE_LANGS = {
#     "c", "cpp", "c++", "java", "javascript", "js", "typescript", "ts",
#     "go", "rust", "csharp", "c#", "kotlin", "swift", "scala",
# }
# HASH_COMMENT_LANGS = {"python", "py", "ruby", "rb", "shell", "bash", "perl"}

# HUNK_HEADER_RE = re.compile(r"^@@\s*-\d+(?:,\d+)?\s*\+\d+(?:,\d+)?\s*@@(.*)$")
# FILE_HEADER_OLD_RE = re.compile(r"^---\s+(\S+)")
# FILE_HEADER_NEW_RE = re.compile(r"^\+\+\+\s+(\S+)")

# # Strip common diff prefixes (a/, b/) and trailing timestamps so that
# # "a/drivers/foo.c" and "drivers/foo.c" (or "/dev/null") compare sensibly.
# _PATH_PREFIX_RE = re.compile(r"^[ab]/")

# DEFAULT_CONTENT_MATCH_THRESHOLD = 0.85


# def strip_comments(text: str, language: str) -> str:
#     lang = (language or "").strip().lower()
#     if lang in C_LIKE_LANGS:
#         text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
#         text = re.sub(r"//.*", "", text)
#     elif lang in HASH_COMMENT_LANGS:
#         text = re.sub(r"#.*", "", text)
#     return text


# _TOKEN_RE = re.compile(
#     r"[A-Za-z_][A-Za-z0-9_]*"          # identifiers/keywords
#     r"|0[xX][0-9a-fA-F]+"              # hex literals
#     r"|\d+\.\d+|\d+"                   # numeric literals
#     r"|->|::|\+\+|--|&&|\|\||==|!=|<=|>=|<<|>>|[-+*/%&|^!=<>]="  # multi-char ops
#     r"|\S"                             # any other single non-space char
# )


# def normalize_line(line: str, language: str) -> str:
#     line = strip_comments(line, language)
#     tokens = _TOKEN_RE.findall(line)
#     return " ".join(tokens)


# def normalize_path(path: str) -> str:
#     if not path:
#         return ""
#     path = path.strip()
#     path = _PATH_PREFIX_RE.sub("", path)
#     # Strip trailing tab + timestamp some diff tools append
#     path = path.split("\t")[0]
#     return path


# def normalize_context(context: str, language: str) -> str:
#     """Normalize the function-context text trailing a '@@ ... @@' hunk header."""
#     if not context:
#         return ""
#     return normalize_line(context, language)


# # --------------------------------------------------------------------------
# # Diff parsing: pulls out file paths, hunk contexts, op-tagged content, and
# # a reconstructed post-image (the resulting code) per patch.
# # --------------------------------------------------------------------------

# def parse_patch(patch_text: str, language: str):
#     """
#     Returns a dict:
#       is_diff: bool
#       files: sorted list of normalized file paths touched
#       hunk_contexts: list of normalized non-empty function-context strings
#       added_tokens: flat list of normalized tokens from '+' lines (diff mode)
#                     or all lines (raw-code mode)
#       removed_tokens: flat list of normalized tokens from '-' lines
#                     (empty in raw-code mode)
#       post_tokens: flat list of normalized tokens representing the
#                     reconstructed *resulting* code across all hunks
#                     (context + added lines, in original order, removed
#                     lines dropped). In raw-code mode this equals
#                     added_tokens (the whole snippet IS the resulting code).
#     """
#     lines = patch_text.splitlines() if patch_text else []
#     is_unified_diff = any(HUNK_HEADER_RE.match(l) for l in lines)

#     files = set()
#     hunk_contexts = []
#     added_tokens = []
#     removed_tokens = []
#     post_tokens = []

#     if not is_unified_diff:
#         # Raw code snippet, not a diff: treat every non-empty line as
#         # content; no location information available. The whole snippet
#         # is both "what was added" and "the resulting code".
#         for l in lines:
#             nl = normalize_line(l, language)
#             if nl:
#                 added_tokens.append(nl)
#         post_tokens = list(added_tokens)
#         return {
#             "is_diff": False,
#             "files": [],
#             "hunk_contexts": [],
#             "added_tokens": added_tokens,
#             "removed_tokens": [],
#             "post_tokens": post_tokens,
#         }

#     in_hunk = False
#     for l in lines:
#         # git-format-patch appends a "-- \n<git version>" trailer after
#         # all real diff content. Without this guard, the "--" line (which
#         # starts with '-') gets misparsed as a removed-diff-line, and any
#         # "-"-prefixed bullet list in the commit message above it could
#         # be misparsed too. Once we've seen any real diff structure,
#         # a bare "--" line reliably marks end-of-patch -- stop there.
#         if l.rstrip() == "--" and (in_hunk or files):
#             break

#         m_old = FILE_HEADER_OLD_RE.match(l)
#         m_new = FILE_HEADER_NEW_RE.match(l)
#         m_hunk = HUNK_HEADER_RE.match(l)

#         if m_old:
#             p = normalize_path(m_old.group(1))
#             if p and p != "dev/null":
#                 files.add(p)
#             continue
#         if m_new:
#             p = normalize_path(m_new.group(1))
#             if p and p != "dev/null":
#                 files.add(p)
#             continue
#         if m_hunk:
#             in_hunk = True
#             ctx = normalize_context(m_hunk.group(1), language)
#             if ctx:
#                 hunk_contexts.append(ctx)
#             continue
#         if l.startswith("diff ") or l.startswith("index ") or l.startswith("new file mode") \
#                 or l.startswith("deleted file mode") or l.startswith("similarity index") \
#                 or l.startswith("rename from") or l.startswith("rename to"):
#             continue
#         if not in_hunk:
#             # Still inside the commit-message / diffstat preamble (e.g. a
#             # git-format-patch email's Subject/body, or a diffstat summary
#             # line like " fs/crypto/policy.c | 4 ++++"). Never treat this
#             # as diff content, even if it happens to start with '+' or '-'.
#             continue
#         if l.startswith("+"):
#             nl = normalize_line(l[1:], language)
#             if nl:
#                 toks = nl.split(" ")
#                 added_tokens.extend(toks)
#                 post_tokens.extend(toks)
#             continue
#         if l.startswith("-"):
#             nl = normalize_line(l[1:], language)
#             if nl:
#                 removed_tokens.extend(nl.split(" "))
#             continue
#         # Unchanged context line inside a hunk: contributes to the
#         # reconstructed post-image but is not itself a "change". Standard
#         # unified-diff context lines are prefixed with a single space.
#         nl = normalize_line(l[1:] if l.startswith(" ") else l, language)
#         if nl:
#             post_tokens.extend(nl.split(" "))

#     return {
#         "is_diff": True,
#         "files": sorted(files),
#         "hunk_contexts": hunk_contexts,
#         "added_tokens": added_tokens,
#         "removed_tokens": removed_tokens,
#         "post_tokens": post_tokens,
#     }


# def find_anchor_window(needle_tokens, haystack_tokens, margin=60, min_block_size=4):
#     """Locate the region of `haystack_tokens` where `needle_tokens`' content was
#     actually found, expanded by `margin` tokens on each side.

#     This exists to fix a specific false-positive: when checking whether a
#     removed line is genuinely gone, comparing against the *entire*
#     reconstructed file lets an unrelated, coincidentally similar call
#     elsewhere in the same file (e.g. a different function that happens to
#     share the same argument pattern) count as a match. Anchoring the search
#     to the neighborhood of where the real edit landed -- found via the
#     already-reliable added-content match -- rules that out.

#     Returns (start, end) token-index bounds into haystack_tokens, or None if
#     no anchor could be found (e.g. a pure-deletion patch with no added
#     tokens to anchor on).
#     """
#     if not needle_tokens or not haystack_tokens:
#         return None
#     sm = difflib.SequenceMatcher(None, needle_tokens, haystack_tokens, autojunk=False)
#     effective_min = min(min_block_size, len(needle_tokens))
#     blocks = [b for b in sm.get_matching_blocks() if b.size >= effective_min]
#     if not blocks:
#         return None
#     start = min(b.b for b in blocks)
#     end = max(b.b + b.size for b in blocks)
#     start = max(0, start - margin)
#     end = min(len(haystack_tokens), end + margin)
#     return start, end


# def _token_weights(needle_tokens, haystack_tokens, floor=0.05):
#     """Weight each distinct needle token by how rare it is in the haystack.

#     Punctuation and short keywords ('(', ';', 'if', 'true') recur constantly
#     in real code and carry almost no evidence value when "found" -- an
#     identifier or literal that appears once or twice is what actually tells
#     you a specific line is present. Frequency-based weighting lets a matched
#     run of common tokens contribute little, while a matched distinctive
#     identifier contributes a lot, without needing any language-specific
#     keyword list.

#     `floor` keeps ubiquitous tokens from hitting exactly zero weight (which
#     would let arbitrarily long runs of pure punctuation "match for free").
#     """
#     haystack_counts = collections.Counter(haystack_tokens)
#     weights = {}
#     for tok in set(needle_tokens):
#         freq = haystack_counts.get(tok, 0)
#         # freq=0 (token doesn't even appear) -> weight 1.0 (maximally distinctive
#         # in this haystack, though it can't itself be "matched"). Weight decays
#         # as the token recurs more often nearby.
#         weights[tok] = max(1.0 / math.log2(freq + 2), floor)
#     return weights


# # --------------------------------------------------------------------------
# # Comparison
# # --------------------------------------------------------------------------

# # def containment_ratio(needle_tokens, haystack_tokens):
# #     """What fraction of `needle_tokens` (as an ordered token sequence) is
# #     recoverable within `haystack_tokens`, tolerating unrelated tokens
# #     interspersed on the haystack side (e.g. reformatting noise like
# #     inserted braces). Uses difflib's matching-block coverage rather than
# #     exact sequence equality, which is what makes this robust to two
# #     patches expressing the identical change at different hunk granularity.

# #     Returns 1.0 for an empty needle (nothing needs to be found) and 0.0
# #     for a non-empty needle against an empty haystack.
# #     """
# #     if not needle_tokens:
# #         return 1.0
# #     if not haystack_tokens:
# #         return 0.0
# #     sm = difflib.SequenceMatcher(None, needle_tokens, haystack_tokens, autojunk=False)
# #     matched = sum(block.size for block in sm.get_matching_blocks())
# #     return matched / len(needle_tokens)
# def containment_ratio(needle_tokens, haystack_tokens, min_block_size=4):
#     """Weighted fraction of `needle_tokens` recoverable within
#     `haystack_tokens`, via contiguous runs of at least `min_block_size`
#     tokens, where each needle token's contribution is weighted by how rare
#     it is in the haystack (see `_token_weights`). This makes a matched run
#     of generic punctuation/keywords count for little, while a matched
#     distinctive identifier counts for a lot -- without this, two unrelated
#     calls that happen to share a common argument shape (e.g. two different
#     functions both called as `f(dev, true)`) can register as a near-total
#     match purely on structural coincidence.
#     """
#     if not needle_tokens:
#         return 1.0
#     if not haystack_tokens:
#         return 0.0

#     weights = _token_weights(needle_tokens, haystack_tokens)
#     total_weight = sum(weights[t] for t in needle_tokens)
#     if total_weight <= 0:
#         return 0.0

#     sm = difflib.SequenceMatcher(None, needle_tokens, haystack_tokens, autojunk=False)
#     effective_min = min(min_block_size, len(needle_tokens))
#     matched_weight = 0.0
#     for block in sm.get_matching_blocks():
#         if block.size >= effective_min:
#             matched_weight += sum(
#                 weights[t] for t in needle_tokens[block.a: block.a + block.size]
#             )
#     return matched_weight / total_weight


# def bucket(x):
#     if x is None:
#         return "n/a"
#     return f"{int(x*10)*10}-{int(x*10)*10+10}%"


# def compare_patches(generated_patch: str, ground_truth_patch: str, language: str,
#                      threshold: float = DEFAULT_CONTENT_MATCH_THRESHOLD,
#                      include_debug_tokens: bool = False,
#                      removal_window_margin: int = 60):
#     gen = parse_patch(generated_patch, language)
#     gt = parse_patch(ground_truth_patch, language)

#     if not gen["added_tokens"] and not gen["removed_tokens"] \
#             and not gt["added_tokens"] and not gt["removed_tokens"]:
#         return {
#             "content_match": None,
#             "content_similarity": None,
#             "added_coverage": None,
#             "removal_completeness": None, 
#             "file_match": None,
#             "location_match": None,
#             "syntactic_match": None,
#             "manual_review_needed": True,
#             "syntactic_diff": None,
#             "syntactic_eval_method": "containment-based-normalized-tokens (both empty)",
#         }

#     # --- content comparison (containment-based, robust to hunk-shape
#     #     differences between two patches expressing the same net change) ---
#     # added_coverage = containment_ratio(gt["added_tokens"], gen["post_tokens"])
#     # if gt["removed_tokens"]:
#     #     removal_completeness = 1.0 - containment_ratio(gt["removed_tokens"], gen["post_tokens"])
#     #     content_similarity = (added_coverage + removal_completeness) / 2.0
#     # else:
#     #     removal_completeness = 1.0
#     #     content_similarity = added_coverage
#     added_coverage = containment_ratio(gt["added_tokens"], gen["post_tokens"])
#     removal_window = None
#     if gt["removed_tokens"]:
#         removal_window = find_anchor_window(
#             gt["added_tokens"], gen["post_tokens"], margin=removal_window_margin
#         )
#         if removal_window:
#             w_start, w_end = removal_window
#             removal_haystack = gen["post_tokens"][w_start:w_end]
#         else:
#             # No added-content anchor available (e.g. a pure-deletion patch) --
#             # fall back to the whole post-image rather than silently skipping
#             # the check.
#             removal_haystack = gen["post_tokens"]
#         removal_completeness = 1.0 - containment_ratio(gt["removed_tokens"], removal_haystack)
#         content_similarity = (added_coverage + removal_completeness) / 2.0
#     else:
#         removal_completeness = 1.0
#         content_similarity = added_coverage

#     content_match = (added_coverage >= threshold) and (removal_completeness >= threshold)

#     # --- location comparison (unchanged from prior version) ---
#     file_match = None
#     if gen["files"] and gt["files"]:
#         file_match = gen["files"] == gt["files"]

#     context_match = None
#     if gen["hunk_contexts"] and gt["hunk_contexts"]:
#         context_match = sorted(gen["hunk_contexts"]) == sorted(gt["hunk_contexts"])

#     manual_review_needed = False
#     if file_match is False or context_match is False:
#         location_match = False
#     elif file_match is True and (context_match is not False):
#         location_match = True
#     elif context_match is True:
#         location_match = True
#     else:
#         # No usable location signal on one or both sides -- can't confirm
#         # automatically. Don't silently assume it's correct.
#         location_match = None
#         manual_review_needed = True

#     syntactic_match = bool(content_match) and (location_match is not False)
#     if location_match is None:
#         manual_review_needed = True

#     diff_text = ""
#     if not content_match:
#         diff_text = "\n".join(
#             difflib.unified_diff(
#                 gt["post_tokens"],
#                 gen["post_tokens"],
#                 fromfile="ground_truth (reconstructed post-image, normalized)",
#                 tofile="generated (reconstructed post-image, normalized)",
#                 lineterm="",
#             )
#         )

#     result = {
#         "content_match": content_match,
#         "content_similarity": round(content_similarity, 6),
#         "added_coverage": round(added_coverage, 6),
#         "removal_completeness": round(removal_completeness, 6),
#         "file_match": file_match,
#         "location_match": location_match,
#         "syntactic_match": syntactic_match,
#         "manual_review_needed": manual_review_needed,
#         "syntactic_diff": diff_text,
#         "syntactic_eval_method": "containment-based-normalized-tokens+location",
#     }
#     if include_debug_tokens:
#         result["debug_gt_removed_tokens"] = gt["removed_tokens"]
#         result["debug_gt_added_tokens"] = gt["added_tokens"]
#         result["debug_gen_post_tokens"] = gen["post_tokens"]
#         result["debug_removal_window"] = removal_window
#     return result

#     # return {
#     #     "content_match": content_match,
#     #     "content_similarity": round(content_similarity, 6),
#     #     "added_coverage": round(added_coverage, 6),
#     #     "removal_completeness": round(removal_completeness, 6),
#     #     "file_match": file_match,
#     #     "location_match": location_match,
#     #     "syntactic_match": syntactic_match,
#     #     "manual_review_needed": manual_review_needed,
#     #     "syntactic_diff": diff_text,
#     #     "syntactic_eval_method": "containment-based-normalized-tokens+location",
#     # }


# # --------------------------------------------------------------------------
# # DB I/O
# # --------------------------------------------------------------------------

# SELECT_SQL_TEMPLATE = """
#     SELECT id, programming_language, generated_patch, old_version_patch
#     FROM backport_benchmark_results_mystique
#     WHERE generated_patch IS NOT NULL
#       AND old_version_patch IS NOT NULL
#       {compile_filter}
#       {recompute_filter}
#       {dataset_filter}
#       {method_filter}
#     ORDER BY id
#     {limit_clause}
# """

# # NOTE: run this once against your schema before using the script:
# #
# # ALTER TABLE backport_benchmark_results_mystique
# #   ADD COLUMN IF NOT EXISTS content_match BOOLEAN,
# #   ADD COLUMN IF NOT EXISTS content_similarity DOUBLE PRECISION,
# #   ADD COLUMN IF NOT EXISTS file_match BOOLEAN,
# #   ADD COLUMN IF NOT EXISTS location_match BOOLEAN,
# #   ADD COLUMN IF NOT EXISTS syntactic_match BOOLEAN,
# #   ADD COLUMN IF NOT EXISTS manual_review_needed BOOLEAN,
# #   ADD COLUMN IF NOT EXISTS syntactic_diff TEXT,
# #   ADD COLUMN IF NOT EXISTS syntactic_eval_method TEXT,
# #   ADD COLUMN IF NOT EXISTS syntactic_evaluated_at TIMESTAMPTZ;
# #
# # (Schema is unchanged from the previous version of this script -- this
# #  fix is a drop-in replacement, no migration needed.)

# UPDATE_SQL = """
#     UPDATE backport_benchmark_results_mystique
#     SET content_match = %(content_match)s,
#         content_similarity = %(content_similarity)s,
#         file_match = %(file_match)s,
#         location_match = %(location_match)s,
#         syntactic_match = %(syntactic_match)s,
#         manual_review_needed = %(manual_review_needed)s,
#         syntactic_diff = %(syntactic_diff)s,
#         syntactic_eval_method = %(syntactic_eval_method)s,
#         syntactic_evaluated_at = %(syntactic_evaluated_at)s,
#         updated_at = NOW()
#     WHERE id = %(id)s
# """


# def run(args):
#     load_dotenv()

#     dsn = args.database_url or os.getenv("NEON_DATABASE_URL")

#     if not dsn:
#         print(
#             "ERROR: set --database-url or the NEON_DATABASE_URL environment variable.",
#             file=sys.stderr,
#         )
#         sys.exit(1)

#     compile_filter = "" if args.include_non_compiling else "AND compilation_success = TRUE"
#     recompute_filter = "" if args.recompute else "AND syntactic_evaluated_at IS NULL"
#     dataset_filter = "AND dataset = %(dataset)s" if args.dataset else ""
#     method_filter = "AND method = %(method)s" if args.method else ""
#     limit_clause = f"LIMIT {int(args.limit)}" if args.limit else ""

#     select_sql = SELECT_SQL_TEMPLATE.format(
#         compile_filter=compile_filter,
#         recompute_filter=recompute_filter,
#         dataset_filter=dataset_filter,
#         method_filter=method_filter,
#         limit_clause=limit_clause,
#     )

#     params = {}
#     if args.dataset:
#         params["dataset"] = args.dataset
#     if args.method:
#         params["method"] = args.method

#     conn = psycopg2.connect(dsn)
#     conn.autocommit = False
#     try:
#         with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
#             cur.execute(select_sql, params)
#             rows = cur.fetchall()

#         print(f"Fetched {len(rows)} row(s) to evaluate "
#               f"(include_non_compiling={args.include_non_compiling}, recompute={args.recompute}, "
#               f"threshold={args.threshold}).")

#         if not rows:
#             print("Nothing to do.")
#             return

#         n_match, n_mismatch, n_skipped, n_error, n_manual = 0, 0, 0, 0, 0
#         n_content_fail_only, n_location_fail_only, n_both_fail = 0, 0, 0
#         added_buckets = collections.Counter()
#         removal_buckets = collections.Counter()
#         n_dumped = 0 

#         with conn.cursor() as cur:
#             for i, row in enumerate(rows, 1):
#                 rid = row["id"]
#                 try:
#                     result = compare_patches(
#                         row["generated_patch"],
#                         row["old_version_patch"],
#                         row["programming_language"],
#                         threshold=args.threshold,
#                         include_debug_tokens=bool(args.dump_mismatches),
#                         removal_window_margin=args.removal_window_margin,
#                     )
#                     result["id"] = rid
#                     result["syntactic_evaluated_at"] = datetime.now(timezone.utc)

#                     if result["syntactic_match"] is None:
#                         n_skipped += 1
#                     elif result["syntactic_match"]:
#                         n_match += 1
#                     else:
#                         n_mismatch += 1
#                         if not result["content_match"] and result["location_match"] is False:
#                             n_both_fail += 1
#                         elif not result["content_match"]:
#                             n_content_fail_only += 1
#                         elif result["location_match"] is False:
#                             n_location_fail_only += 1
#                         added_buckets[bucket(result["added_coverage"])] += 1       
#                         removal_buckets[bucket(result["removal_completeness"])] += 1  

#                         # if args.dump_mismatches and n_dumped < args.dump_mismatches:
#                         #     n_dumped += 1
#                         #     print(f"\n--- mismatch dump [{n_dumped}/{args.dump_mismatches}] id={rid} ---")
#                         #     print(f"  added_coverage={result['added_coverage']}  "
#                         #         f"removal_completeness={result['removal_completeness']}")
#                         #     print(f"  gt removed_tokens ({len(result['debug_gt_removed_tokens'])}):")
#                         #     print(f"    {' '.join(result['debug_gt_removed_tokens'])}")
#                         #     print(f"  gen post_tokens ({len(result['debug_gen_post_tokens'])}):")
#                         #     print(f"    {' '.join(result['debug_gen_post_tokens'])}")
#                         if args.dump_mismatches and n_dumped < args.dump_mismatches:
#                             n_dumped += 1
#                             print(f"\n--- mismatch dump [{n_dumped}/{args.dump_mismatches}] id={rid} ---")
#                             print(f"  added_coverage={result['added_coverage']}  "
#                                 f"removal_completeness={result['removal_completeness']}")
#                             print(f"  gt removed_tokens ({len(result['debug_gt_removed_tokens'])}):")
#                             print(f"    {' '.join(result['debug_gt_removed_tokens'])}")

#                             window = result.get('debug_removal_window')
#                             gen_toks = result['debug_gen_post_tokens']
#                             if window:
#                                 w_start, w_end = window
#                                 windowed = gen_toks[w_start:w_end]
#                                 print(f"  gen post_tokens, windowed [{w_start}:{w_end}] "
#                                     f"({len(windowed)} of {len(gen_toks)} total tokens):")
#                                 print(f"    {' '.join(windowed)}")
#                             else:
#                                 preview = ' '.join(gen_toks[:300]) + (' ...[truncated]' if len(gen_toks) > 300 else '')
#                                 print(f"  gen post_tokens (no anchor window -- showing preview, "
#                                     f"{len(gen_toks)} tokens total):")
#                                 print(f"    {preview}")

#                     if result["manual_review_needed"]:
#                         n_manual += 1

#                     if not args.dry_run:
#                         cur.execute(UPDATE_SQL, result)

#                 except Exception as e:  # noqa: BLE001 - keep going on per-row failures
#                     n_error += 1
#                     print(f"  [id={rid}] ERROR: {e}", file=sys.stderr)

#                 if i % 100 == 0:
#                     print(f"  ...processed {i}/{len(rows)}")

#         if args.dry_run:
#             conn.rollback()
#             print("\nDRY RUN -- no changes written to the database.")
#         else:
#             conn.commit()
#             print("\nChanges committed.")

#         print(f"\nSummary: {n_match} syntactic matches, {n_mismatch} mismatches, "
#               f"{n_skipped} skipped (empty patches), {n_error} errors, "
#               f"{len(rows)} total.")
#         print(f"Flagged for manual review (location unconfirmed): {n_manual}")
#         if n_match + n_mismatch:
#             pct = 100.0 * n_match / (n_match + n_mismatch)
#             print(f"Syntactic equivalence rate (of comparable rows): {pct:.1f}%")
#         if n_mismatch:
#             print(f"\nMismatch breakdown:")
#             print(f"  content differs only (location matched or unconfirmed): {n_content_fail_only}")
#             print(f"  location differs only (content matched or unconfirmed): {n_location_fail_only}")
#             print(f"  both content and location differ: {n_both_fail}")

#             print(f"\nadded_coverage distribution (mismatches only):")
#             for k in sorted(added_buckets, key=lambda b: (b != "n/a", b)):
#                 print(f"  {k}: {added_buckets[k]}")
#             print(f"\nremoval_completeness distribution (mismatches only):")
#             for k in sorted(removal_buckets, key=lambda b: (b != "n/a", b)):
#                 print(f"  {k}: {removal_buckets[k]}")

#     finally:
#         conn.close()


# def parse_args():
#     p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
#     p.add_argument("--database-url", help="Postgres connection string. Defaults to $NEON_DATABASE_URL.")
#     p.add_argument("--dataset", help="Only evaluate rows where dataset = this value (e.g. 'fixmorph').")
#     p.add_argument("--method", help="Only evaluate rows where method = this value (e.g. 'mystique').")
#     p.add_argument("--limit", type=int, help="Max number of rows to process.")
#     p.add_argument("--include-non-compiling", action="store_true",
#                     help="By default only compilation_success=TRUE rows are evaluated "
#                          "(plausible patches). Pass this to evaluate all rows regardless.")
#     p.add_argument("--recompute", action="store_true",
#                     help="Re-evaluate rows that already have a syntactic_evaluated_at timestamp.")
#     p.add_argument("--dry-run", action="store_true", help="Compute results but do not write to the DB.")
#     p.add_argument("--threshold", type=float, default=DEFAULT_CONTENT_MATCH_THRESHOLD,
#                     help=f"Containment-ratio threshold (0-1) required on both the added-content "
#                          f"coverage and removed-content-absence checks for content_match to be "
#                          f"True. Default {DEFAULT_CONTENT_MATCH_THRESHOLD}.")
#     p.add_argument("--dump-mismatches", type=int, default=0, metavar="N",
#                 help="print id, added_coverage, removal_completeness, and the raw "
#                      "removed/post token lists for the first N mismatches encountered, "
#                      "for manual inspection of the containment-ratio matcher.")
#     p.add_argument("--removal-window-margin", type=int, default=60, metavar="N",
#                 help="token margin on each side of the located added-content "
#                      "anchor, used when checking whether ground-truth-removed "
#                      "code is still present in the generated patch. Smaller "
#                      "values are stricter about locality; too small risks "
#                      "missing genuinely nearby context. Default 60.")
#     return p.parse_args()


# if __name__ == "__main__":
#     run(parse_args())