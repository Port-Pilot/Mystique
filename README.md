### Overview of Mystique (Original Method)

[Mystique](https://github.com/Mystique-OpenSource/mystique-opensource.github.io) is an automated patch-porting framework designed to adapt security fixes across different branches or repositories. The original method operates as follows:

1. **Function-Level Scope**: Mystique works primarily at the individual C/Java function level rather than full files or repositories.
2. **Static Analysis & Slicing**: It uses [Joern](mystique-opensource.github.io/src/joern.py) to build Abstract Syntax Trees (AST), Control Flow Graphs (CFG), and Program Dependency Graphs (PDG). It extracts both semantic and syntactic signatures to isolate modified statements and context lines.
3. **LLM Generation**: Extracted signatures are formatted into structured prompts and passed to a fine-tuned open-source model (e.g., CodeLlama-13b fine-tuned via LoRA) or GPT models to generate the fixed function code.
4. **AST Cleaning & Validation**: Tree-sitter AST parsers ([ast_parser.py](mystique-opensource.github.io/src/ast_parser.py)) parse and clean the LLM's output to extract exact function definitions and check correctness.

---

### How the Original Method Was Converted for the FixMorph Dataset

The [FixMorph](https://github.com/rshariffdeen/FixMorph) dataset evaluates patch backporting on real-world C projects (specifically the Linux kernel). It provides 4 commit SHAs per case:
* $P_a \rightarrow P_b$: Mainline patch before and after the fix (`new_version_patch`).
* $P_c \rightarrow P_e$: Target branch code before and after the fix (`old_version_patch`, ground truth).

To evaluate Mystique on FixMorph, several adaptations were implemented:

```
                                 FixMorph Main Data Set (Excel)
                                               │
                                               ▼
                                    insert_to_neon.py
                         (Fetch Pa, Pb, Pc, Pe & Cache in SQLite)
                                               │
                                               ▼
                                     phase2_generate.py
                                               │
                       ┌───────────────────────┴───────────────────────┐
                       ▼                                               ▼
              patch_bp_warper.py                               Direct Fallback
       (Mystique Joern + Slicing Pipeline)                   (llm_fix_diff with GPT-5.5)
                       │                                               │
                       └───────────────────────┬───────────────────────┘
                                               │
                                               ▼
                                    syntactic_evaluation.py
                                               │
                                               ▼
                                       compile_check.py
```

#### 1. Bridging Granularity (Function-Level $\rightarrow$ Full-File Diffs)
* **Problem**: Mystique expects isolated function snippets (`origin_before_func_code`, etc.), while FixMorph benchmark cases consist of multi-line diffs across entire source files.
* **Solution**: Implemented [patch_bp_warper.py](file:///home/ubuntu/Mystique/patch_bp_warper.py):
  1. Uses Tree-sitter AST parsing ([project.py](file:///home/ubuntu/Mystique/mystique-opensource.github.io/src/project.py)) to map diff lines between $P_a$ and $P_b$ to specific C function names.
  2. Runs Mystique’s core function-slicing pipeline ([patchbp.py](file:///home/ubuntu/Mystique/mystique-opensource.github.io/src/patchbp.py)) per modified function.
  3. Replaces target function nodes in $P_c$ using byte-offset substitution and generates a unified file diff matching FixMorph's expected output format using `difftools.git_diff_code`.

#### 2. Upgrading the LLM Engine (Local CodeLlama $\rightarrow$ OpenAI GPT-5.5)
* Modified [llm.py](file:///home/ubuntu/Mystique/mystique-opensource.github.io/src/llm.py) to replace the local `oobabooga` server calls with the standard `OpenAI` client pointing to `gpt-5.5` (or `gpt-4o`).
* Added detailed token and cost tracking via the `LLMUsage` data structure (`input_tokens`, `output_tokens`, `reasoning_tokens`, `api_cost`).

#### 3. Fault-Tolerant Direct LLM Fallback Pipeline
* **Problem**: Linux kernel source files often fail Joern static analysis or PDG slicing due to complex C macros, header dependencies, or changes outside traditional function bodies.
* **Solution**: In [phase2_generate.py](file:///home/ubuntu/Mystique/phase2_generate.py), a list of pre-LLM static analysis errors was defined (`_PRE_LLM_ERRORS`, e.g., `JOERN_ERROR`, `METHOD_NOT_FOUND`, `PDG_NOT_FOUND`, `SLICE_FAILED`). If `bp_warper` fails before reaching the LLM, the system seamlessly falls back to `llm_fix_diff()` in [llm.py](file:///home/ubuntu/Mystique/mystique-opensource.github.io/src/llm.py#L136-L186), which directly feeds the raw diff and full target source code to GPT-5.5 to output a unified diff.

#### 4. Data Ingestion & Evaluation Pipeline
* **Data Ingestion**: [insert_to_neon.py](file:///home/ubuntu/Mystique/insert_to_neon.py) reads `Main-data-set.xlsx`, fetches file blobs from GitHub, caches them in `github_fetch_cache.sqlite`, and stores records in a Neon Postgres database.
* **Evaluation**: [syntactic_evaluation.py](file:///home/ubuntu/Mystique/syntactic_evaluation.py) normalizes tokens (stripping comments/whitespace) to evaluate Content Match and Location Match against ground-truth patches, while [compile_check.py](file:///home/ubuntu/Mystique/compile_check.py) evaluates compilation success using GCC/Docker setups.

---

### Was It Easy to Convert?

#### **What Was Easy**
1. **Core Prompting Concept**: The underlying prompting logic (*Mainline Patch + Target Vulnerable Code $\rightarrow$ Ported Code*) transferred smoothly from local models (CodeLlama) to OpenAI API models (GPT-5.5).
2. **AST Parsing Infrastructure**: Tree-sitter capabilities already present in Mystique made it easy to locate function boundaries in full C files.

#### **What Was Challenging (Nontrivial Engineering Effort)**
1. **Granularity Adapter**: Converting file-level kernel patches into method-level inputs, running Mystique per method, and then correctly splicing fixed method byte ranges back into full source files required writing custom wrapper logic ([patch_bp_warper.py](file:///home/ubuntu/Mystique/patch_bp_warper.py)).
2. **Handling Static Analysis Failures (Joern Fragility)**: Joern frequently fails on un-preprocessed C kernel code with macro magic or missing headers. Designing a fallback mechanism to route failed static-analysis cases directly to the LLM was necessary to achieve high benchmark coverage.
3. **Diff Validation & Normalization**: LLMs often output raw code blocks or slightly malformed unified diff headers. Implementing automatic unified diff validation and normalization ([difftools.py](file:///home/ubuntu/Mystique/mystique-opensource.github.io/src/difftools.py)) was required to generate valid patches.

#### **Summary Verdict**
The overall conversion is **conceptually straightforward** but **required substantial wrapper engineering** (~1,500+ lines of Python glue across [phase2_generate.py](file:///home/ubuntu/Mystique/phase2_generate.py), [patch_bp_warper.py](file:///home/ubuntu/Mystique/patch_bp_warper.py), [syntactic_evaluation.py](file:///home/ubuntu/Mystique/syntactic_evaluation.py), and [insert_to_neon.py](file:///home/ubuntu/Mystique/insert_to_neon.py)) to handle database ingestion, AST method splicing, static analysis fallbacks, and patch verification.