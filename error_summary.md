# Mystique → FixMorph Reproduction: Error Summary

Scope: debugging session covering the `TSBPORT-Joern` branch of `Port-Pilot/Mystique`, evaluated against `FixMorph-Dataset/Main-data-set.xlsx`.

This discusses the bug fixes for the joern errors and the other errors. The patches discussed here are the ones that could not generate a patch due to those errors.

---

## Fixed

### 1. Kernel list-iteration macros flagged as syntax errors
**Symptom:** `CHECK_FAILED <method> [There is a syntax error in line N: list_for_each_entry_safe(...)]` (and `list_for_each_entry`, `list_for_each_entry_rcu`, `for_each_child_of_node`).

**Cause:** `checking_ast_error()` in `check.py` runs tree-sitter's plain-C grammar over LLM-generated code. Tree-sitter has no macro expansion, so it can't resolve that `list_for_each_entry_safe(args) { ... }` is a for-loop header, and reports the macro call as an unexpected/error AST node.

**Fix:** Added a `kernel_iter_macro_prefixes` tuple and a `error_code.startswith(kernel_iter_macro_prefixes)` exclusion, mirroring the existing (exact-match) `syntax_code_exclude` allowlist the original authors already used for a different macro. Matched by prefix instead of exact string, since call-site arguments differ every time.

**Confirmed fixed:** `core_tmr_drain_state_list`, `clocksource_watchdog`, `tango_nand_probe`, `dev_pm_opp_get_opp_count` — all 4 rows moved from `CHECK_FAILED` to `DONE` on rerun, no side effects on unrelated rows.

### 2. `__acquires`/lock-annotation macros flagged as syntax errors
**Symptom:** `CHECK_FAILED lock_timer_base [There is a syntax error in line 2: >]` — a bare `>` token, no useful macro name in the error text.

**Cause:** Same root cause as #1, but tree-sitter's error recovery took a different path (`node.is_missing` instead of `node.is_error`), reporting the parent node's isolated `>` character rather than the full macro call. The line itself was `__acquires(timer->base->lock) {` — a valid kernel lock-context annotation macro, same category of problem, different recovery shape. The fix in #1 didn't cover this because it matched on macro-name text, and here the reported text has no macro name in it at all.

**Fix:** Added a full-source-line check (`__acquires(`, `__releases(`, `__must_hold(`, etc.) evaluated before both the `is_error` and `is_missing` branches, since the error text alone wasn't a reliable anchor.

**Confirmed fixed:** `lock_timer_base` — `DONE` on rerun, first attempt (no retries needed, confirming the earlier duplicate-declaration artifact in retry output was a side effect of the false positive, not an independent bug).

### 3. Doubled relative path in per-file `joern-parse` invocation
**Symptom:** `Joern parse failed on <file>: java.lang.AssertionError: Input path does not exist at 'cache_bug/.../pre/_joern_work/0/src'` — every file, every row, every fresh (non-cached) run.

**Cause:** In `export()`'s per-file isolation loop, both the `-o <cpg_bin>` and source-path arguments passed to `joern-parse` were relative paths that already included `work_dir` as a prefix, while the subprocess's `cwd` was *also* `work_dir` — so the paths resolved to a doubled, nonexistent location (`work_dir/work_dir/src`). The `joern-export` calls in the same function were unaffected since they already wrapped their `--out` argument in `os.path.abspath()`; only `joern-parse` was missed.

**Fix:** Wrapped both the `-o` output path and the source directory argument in `os.path.abspath()`, matching the pattern already used for `joern-export`.

**Impact — this was the most consequential fix of the session.** Because `export_with_preprocess_and_merge()` only rebuilds Joern output when `cpg_dir/export.dot` is missing, any row with a pre-existing cache directory (from before the per-file refactor, or from any run predating this fix) silently kept reusing old, valid, real-Joern-built output and never hit this bug — which is why it went undetected through several rounds of testing. Once caches were wiped, it turned out **real Joern had never successfully run** under the per-file isolation code in any fresh invocation; every file was silently falling back to the synthetic tree-sitter PDG instead. **Caveat for interpreting earlier results in this session:** any row tested before the cache wipe may have been validated against stale pre-refactor caches rather than the actual code being evaluated — the syntax-checker fixes (#1, #2) are unaffected by this (they operate on LLM output text, not PDGs), but PDG-dependent outcomes from before the wipe should be treated with caution.

---

## Not fixed — accepted as inherent to Mystique's method

These aren't bugs; fixing them would mean changing what Mystique fundamentally does, not fixing a defect in this reproduction.

| Error | Occurrences | Reason |
|---|---|---|
| `CHANGE_OUTSIDE_METHOD` | 26 | Mystique patches at method granularity by design. A diff touching a macro, global variable, struct, or `#ifdef` block has no function body to attach to. |
| `CHECK_FAILED` — `SIM_DIFF` | 21 | `checking_similarity()` is a Levenshtein-distance heuristic comparing the LLM's output to ground truth. A functionally correct but textually different patch can still fail this check — an accepted characteristic of using text similarity as a correctness proxy, present in the original method's design. |

---

## Not fixed — investigated, judged genuine (not a reproduction bug)

| Error | Occurrences | Finding |
|---|---|---|
| `TARGET_METHOD_NOT_FOUND` | 20 | Spot-checked `bsg_prepare_job`: dumped the actual target file and grepped for the function — zero matches. Genuinely absent from that target codebase snapshot (cross-version rename/removal), not a lookup bug. Only one of the four instances in this batch was individually verified; the rest were not checked one-by-one, but the verified case supports treating this as a dataset characteristic rather than a bug. |
| `PDG_NOT_FOUND` — `avx2_usable` | 1 (confirmed) | Traced end-to-end: real Joern (post path-fix) ran successfully on the file overall, but emitted a `PARSER_TYPE_NAME="CASTProblemDeclaration"` node for this function instead of a proper `METHOD` node. Root cause: `avx2_usable` is defined inside nested `#ifdef CONFIG_AS_AVX` → `#ifdef CONFIG_AS_AVX2` blocks — Kconfig-controlled macros with no value when Joern parses an isolated file outside a real kernel build. This is a genuine limitation of Joern's C2CPG frontend interacting with unresolvable nested preprocessor conditionals around a function definition, not a bug in the Python pipeline. Fixing it would require patching Joern's own Scala frontend — out of scope. |

---

## Not fixed — identified as a real, narrow bug, but deliberately deprioritized

| Error | Occurrences | Finding | Why not fixed |
|---|---|---|---|
| `GROUNDTRUTH_FAILED` | 10 | Traced `__dm_destroy`: the hunk text `map = dm_get_live_table(md, &srcu_idx);` is both (a) a line the upstream patch adds, and (b) one of the lines an *earlier* substitution pass in the same function replaces with an opaque `/*<<<GT-HUNK>>>*/` placeholder before the later exact-substring search runs — so the text is gone from `gt_code` by the time it's searched for. A genuine ordering bug between two passes in the ground-truth-annotation logic, not a whitespace/tab issue as initially hypothesized. | Fixing it correctly requires a clear read on what this ground-truth-annotation step feeds downstream, since a naive fix (e.g. treating a placeholder-covered region as an automatic match) risks silently corrupting the similarity/verification baseline used elsewhere. At ~8% of errors, judged lower priority than the risk of a wrong fix. |

---

## Not fixed — not yet triaged (surfaced late in session, investigation not started)

These appeared in the larger later-session runs and weren't individually debugged:

- `PDG_NOT_FOUND` — `stm32_clockevent_init` (all three of pre/post/target return no PDG; investigation pivoted to `avx2_usable` instead and didn't return to this one — cause not confirmed, may or may not share the same nested-`#ifdef` root cause).
- `CHECK_FAILED` — `check_hw_exists`, `(KERN_ERR FW_BUG` at line 50: looks like a truncated multi-line macro call. Unlike the list-iteration/lock-annotation cases, no clear tree-sitter-limitation explanation — reads more like a genuine LLM generation or `recover_placeholder` reconstruction artifact.
- `CHECK_FAILED` — single-occurrence syntax errors seen only in the larger batch: `use of undeclared identifier 'flags'` (`target_release_cmd_kref`), `incompatible integer to pointer conversion ... 'int'` (`__skb_flow_dissect`), plus two more `syntax error in line 29` / `line 18` cases with reasons not yet inspected. Each occurred once; not established whether these are further tree-sitter false positives (same family as #1/#2) or genuine LLM output problems.

---

## Overall picture

Across the 146-row session total: **27 done, 119 errors**, breaking down roughly as ~40% structurally inherent to the method (`CHANGE_OUTSIDE_METHOD` + `SIM_DIFF`), with the remainder split between confirmed-genuine dataset/Joern limitations, one identified-but-deprioritized bug, and a handful of not-yet-triaged single occurrences. The three fixes made (macro-prefix exclusion, lock-annotation exclusion, absolute-path Joern invocation) were all small, isolated, and confirmed via direct before/after reruns — the path-prefix fix in particular corrected a bug that had been silently masked by stale caching throughout most of the session, and is worth flagging as the reason to treat any pre-cache-wipe PDG-related conclusions with caution.