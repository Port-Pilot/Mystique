"""
syntactic_evaluation_core.py -- core comparison logic (no DB I/O), used both
by the real script and by the test suite below.

Two independent measures are computed per patch pair:

1. STRICT / FixMorph-comparable `syntactic_match`
   -----------------------------------------------
   Exact equality, after normalization (comments stripped, whitespace/
   spacing differences neutralized via tokenization), between the
   generated patch's changed content and the ground truth's changed
   content -- PLUS a confirmed location match (same file(s), and matching
   hunk context when available). No partial credit. This mirrors what
   FixMorph's two human reviewers were checking: "is this the same patch
   as the developer's, modulo formatting?" A patch that reproduces 95% of
   a hunk correctly but changes one token's meaning is NOT a match here,
   exactly as a human reviewer would not have called it one.

   This is the only column that should be compared against FixMorph's
   published "Syntactic" percentages.

2. DIAGNOSTIC fuzzy similarity (`content_similarity`, `high_similarity_match`)
   ----------------------------------------------------------------------
   The previous containment-ratio / weighted-token-overlap machinery,
   kept as auxiliary signal. This tolerates hunk-shape/reformatting
   differences via partial, threshold-based matching, which is exactly
   why it must NOT be reported as "syntactic equivalence" -- a patch that
   only 85%-overlaps the ground truth is, by construction, not identical
   to it. Its purpose is narrower: ranking/triaging the non-strict-match
   rows before manual or LLM-based semantic review (rows with high
   content_similarity are the ones most likely to be semantically
   equivalent-but-differently-written -- FixMorph's "Semantic but not
   Syntactic" bucket -- and worth reviewing first).

`manual_review_needed` is set whenever the strict check couldn't
confidently resolve (most commonly: content is an exact match but
location information was unparseable from the diff, e.g. no file
headers) -- these rows should go to human/semantic review rather than
being silently counted either way.
"""

import difflib
import re
import collections
import math


C_LIKE_LANGS = {
    "c", "cpp", "c++", "java", "javascript", "js", "typescript", "ts",
    "go", "rust", "csharp", "c#", "kotlin", "swift", "scala",
}
HASH_COMMENT_LANGS = {"python", "py", "ruby", "rb", "shell", "bash", "perl"}

HUNK_HEADER_RE = re.compile(r"^@@\s*-\d+(?:,\d+)?\s*\+\d+(?:,\d+)?\s*@@(.*)$")
FILE_HEADER_OLD_RE = re.compile(r"^---\s+(\S+)")
FILE_HEADER_NEW_RE = re.compile(r"^\+\+\+\s+(\S+)")

_PATH_PREFIX_RE = re.compile(r"^[ab]/")

DEFAULT_SIMILARITY_THRESHOLD = 0.85  # diagnostic only -- see module docstring


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------

def strip_comments(text, language):
    lang = (language or "").strip().lower()
    if lang in C_LIKE_LANGS:
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        text = re.sub(r"//.*", "", text)
    elif lang in HASH_COMMENT_LANGS:
        text = re.sub(r"#.*", "", text)
    return text


_TOKEN_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*"
    r"|0[xX][0-9a-fA-F]+"
    r"|\d+\.\d+|\d+"
    r"|->|::|\+\+|--|&&|\|\||==|!=|<=|>=|<<|>>|[-+*/%&|^!=<>]="
    r"|\S"
)


def normalize_line(line, language):
    line = strip_comments(line, language)
    tokens = _TOKEN_RE.findall(line)
    return " ".join(tokens)


def normalize_path(path):
    if not path:
        return ""
    path = path.strip()
    path = _PATH_PREFIX_RE.sub("", path)
    path = path.split("\t")[0]
    return path


def normalize_context(context, language):
    if not context:
        return ""
    return normalize_line(context, language)


# --------------------------------------------------------------------------
# Diff parsing
# --------------------------------------------------------------------------

def parse_patch(patch_text, language):
    """
    Returns:
      is_diff: bool
      files: sorted list of normalized file paths touched
      hunk_contexts: list of normalized non-empty function-context strings
      added_tokens: flat, ORDER-PRESERVING list of normalized tokens from
                    '+' lines (diff mode) or all lines (raw-code mode)
      removed_tokens: flat, order-preserving list of normalized tokens
                    from '-' lines (empty in raw-code mode)
      post_tokens: flat list of normalized tokens for the reconstructed
                    *resulting* code across all hunks (context + added,
                    removed lines dropped) -- used only for the
                    diagnostic fuzzy comparison, not the strict one.
    """
    lines = patch_text.splitlines() if patch_text else []
    is_unified_diff = any(HUNK_HEADER_RE.match(l) for l in lines)

    files = set()
    hunk_contexts = []
    added_tokens = []
    removed_tokens = []
    post_tokens = []

    if not is_unified_diff:
        for l in lines:
            nl = normalize_line(l, language)
            if nl:
                added_tokens.append(nl)
        post_tokens = list(added_tokens)
        return {
            "is_diff": False, "files": [], "hunk_contexts": [],
            "added_tokens": added_tokens, "removed_tokens": [], "post_tokens": post_tokens,
        }

    in_hunk = False
    for l in lines:
        if l.rstrip() == "--" and (in_hunk or files):
            break
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
            in_hunk = True
            ctx = normalize_context(m_hunk.group(1), language)
            if ctx:
                hunk_contexts.append(ctx)
            continue
        if l.startswith("diff ") or l.startswith("index ") or l.startswith("new file mode") \
                or l.startswith("deleted file mode") or l.startswith("similarity index") \
                or l.startswith("rename from") or l.startswith("rename to"):
            continue
        if not in_hunk:
            continue
        if l.startswith("+"):
            nl = normalize_line(l[1:], language)
            if nl:
                toks = nl.split(" ")
                added_tokens.extend(toks)
                post_tokens.extend(toks)
            continue
        if l.startswith("-"):
            nl = normalize_line(l[1:], language)
            if nl:
                removed_tokens.extend(nl.split(" "))
            continue
        nl = normalize_line(l[1:] if l.startswith(" ") else l, language)
        if nl:
            post_tokens.extend(nl.split(" "))

    return {
        "is_diff": True, "files": sorted(files), "hunk_contexts": hunk_contexts,
        "added_tokens": added_tokens, "removed_tokens": removed_tokens, "post_tokens": post_tokens,
    }


# --------------------------------------------------------------------------
# STRICT comparison (FixMorph-comparable)
# --------------------------------------------------------------------------

def _resolve_location(gen, gt):
    """
    Returns (location_match, location_resolved):
      location_match: True / False / None (None = couldn't be determined)
      location_resolved: whether we had enough info to make a confident call
    """
    file_match = None
    if gen["files"] and gt["files"]:
        file_match = gen["files"] == gt["files"]

    context_match = None
    if gen["hunk_contexts"] and gt["hunk_contexts"]:
        context_match = sorted(gen["hunk_contexts"]) == sorted(gt["hunk_contexts"])

    if file_match is False or context_match is False:
        return False, True
    if file_match is True:
        return True, True
    if context_match is True:
        return True, True
    # Neither side gave us anything to check against -- unresolved, not "pass".
    return None, False


def strict_compare(generated_patch, ground_truth_patch, language):
    """
    FixMorph-comparable syntactic equivalence: exact match after
    normalization, no partial credit, location must be confirmed (not
    merely "not disproven").
    """
    gen = parse_patch(generated_patch, language)
    gt = parse_patch(ground_truth_patch, language)

    if not gen["added_tokens"] and not gen["removed_tokens"] \
            and not gt["added_tokens"] and not gt["removed_tokens"]:
        return {
            "syntactic_match": None,
            "content_exact_match": None,
            "location_match": None,
            "location_resolved": False,
            "manual_review_needed": True,
        }

    content_exact_match = (gen["added_tokens"] == gt["added_tokens"]) and \
                           (gen["removed_tokens"] == gt["removed_tokens"])

    location_match, location_resolved = _resolve_location(gen, gt)

    if not content_exact_match:
        syntactic_match = False
        manual_review_needed = False
    elif location_match is True:
        syntactic_match = True
        manual_review_needed = False
    elif location_match is False:
        syntactic_match = False
        manual_review_needed = False
    else:
        # Content matches exactly, but we can't confirm location from the
        # diff text alone (e.g. no file headers, no hunk context). Don't
        # guess -- flag it.
        syntactic_match = None
        manual_review_needed = True

    return {
        "syntactic_match": syntactic_match,
        "content_exact_match": content_exact_match,
        "location_match": location_match,
        "location_resolved": location_resolved,
        "manual_review_needed": manual_review_needed,
    }


# --------------------------------------------------------------------------
# DIAGNOSTIC fuzzy comparison (kept from the containment-ratio version --
# NOT used for syntactic_match, see module docstring)
# --------------------------------------------------------------------------

def find_anchor_window(needle_tokens, haystack_tokens, margin=60, min_block_size=4):
    if not needle_tokens or not haystack_tokens:
        return None
    sm = difflib.SequenceMatcher(None, needle_tokens, haystack_tokens, autojunk=False)
    effective_min = min(min_block_size, len(needle_tokens))
    blocks = [b for b in sm.get_matching_blocks() if b.size >= effective_min]
    if not blocks:
        return None
    start = min(b.b for b in blocks)
    end = max(b.b + b.size for b in blocks)
    start = max(0, start - margin)
    end = min(len(haystack_tokens), end + margin)
    return start, end


def _token_weights(needle_tokens, haystack_tokens, floor=0.05):
    haystack_counts = collections.Counter(haystack_tokens)
    weights = {}
    for tok in set(needle_tokens):
        freq = haystack_counts.get(tok, 0)
        weights[tok] = max(1.0 / math.log2(freq + 2), floor)
    return weights


def containment_ratio(needle_tokens, haystack_tokens, min_block_size=4):
    if not needle_tokens:
        return 1.0
    if not haystack_tokens:
        return 0.0
    weights = _token_weights(needle_tokens, haystack_tokens)
    total_weight = sum(weights[t] for t in needle_tokens)
    if total_weight <= 0:
        return 0.0
    sm = difflib.SequenceMatcher(None, needle_tokens, haystack_tokens, autojunk=False)
    effective_min = min(min_block_size, len(needle_tokens))
    matched_weight = 0.0
    for block in sm.get_matching_blocks():
        if block.size >= effective_min:
            matched_weight += sum(weights[t] for t in needle_tokens[block.a: block.a + block.size])
    return matched_weight / total_weight


def diagnostic_similarity(generated_patch, ground_truth_patch, language,
                           threshold=DEFAULT_SIMILARITY_THRESHOLD, removal_window_margin=60):
    gen = parse_patch(generated_patch, language)
    gt = parse_patch(ground_truth_patch, language)

    if not gen["added_tokens"] and not gen["removed_tokens"] \
            and not gt["added_tokens"] and not gt["removed_tokens"]:
        return {"content_similarity": None, "added_coverage": None,
                "removal_completeness": None, "high_similarity_match": None}

    added_coverage = containment_ratio(gt["added_tokens"], gen["post_tokens"])
    if gt["removed_tokens"]:
        window = find_anchor_window(gt["added_tokens"], gen["post_tokens"], margin=removal_window_margin)
        removal_haystack = gen["post_tokens"][window[0]:window[1]] if window else gen["post_tokens"]
        removal_completeness = 1.0 - containment_ratio(gt["removed_tokens"], removal_haystack)
        content_similarity = (added_coverage + removal_completeness) / 2.0
    else:
        removal_completeness = 1.0
        content_similarity = added_coverage

    high_similarity_match = (added_coverage >= threshold) and (removal_completeness >= threshold)

    return {
        "content_similarity": round(content_similarity, 6),
        "added_coverage": round(added_coverage, 6),
        "removal_completeness": round(removal_completeness, 6),
        "high_similarity_match": high_similarity_match,
    }


# --------------------------------------------------------------------------
# Combined entry point used by the DB script
# --------------------------------------------------------------------------

def compare_patches(generated_patch, ground_truth_patch, language,
                     similarity_threshold=DEFAULT_SIMILARITY_THRESHOLD,
                     removal_window_margin=60):
    strict = strict_compare(generated_patch, ground_truth_patch, language)
    diag = diagnostic_similarity(generated_patch, ground_truth_patch, language,
                                  threshold=similarity_threshold,
                                  removal_window_margin=removal_window_margin)

    diff_text = ""
    if strict["syntactic_match"] is False:
        gen = parse_patch(generated_patch, language)
        gt = parse_patch(ground_truth_patch, language)
        diff_text = "\n".join(
            difflib.unified_diff(
                gt["added_tokens"] + ["--- removed ---"] + gt["removed_tokens"],
                gen["added_tokens"] + ["--- removed ---"] + gen["removed_tokens"],
                fromfile="ground_truth (normalized)",
                tofile="generated (normalized)",
                lineterm="",
            )
        )

    result = dict(strict)
    result.update(diag)
    result["syntactic_diff"] = diff_text
    result["syntactic_eval_method"] = "exact-normalized-content+confirmed-location (strict); " \
                                       "containment-ratio (diagnostic)"
    return result