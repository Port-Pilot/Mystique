#!/usr/bin/env python3
"""Check whether generated Linux backports compile and update Neon.

This implements FixMorph's *plausibility* criterion: apply the generated patch
to the target version before the developer backport (Pc), then compile the
single object recorded in FixMorph's ``Program CE`` column.  It intentionally
does not claim syntactic or semantic equivalence with the developer patch.

A result is written only when it is definitive:

* ``TRUE``  - the patched target object compiled;
* ``FALSE`` - the generated diff was invalid/inapplicable, or the same object
  failed after a successful unpatched baseline build;
* unchanged - checkout, configuration, toolchain, timeout, baseline-build, or
  database errors made the result inconclusive.

The script uses one reusable worktree and one disposable out-of-tree build
directory, so checking many kernel revisions has bounded checkout/build disk
usage.  The shared bare object store is retained between runs.  It is populated
one benchmark revision at a time instead of cloning the Linux repository's
entire history.

Typical use (``NEON_DATABASE_URL`` is loaded from ``.env``):

    python compile_check.py
    python compile_check.py --method mystique --limit 5 --dry-run

An ARM benchmark row needs a suitable cross compiler, for example:

    python compile_check.py --cross-compile arm=arm-linux-gnueabihf-
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from openpyxl import load_workbook


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DATASET = SCRIPT_DIR / "FixMorph-Dataset" / "Main-data-set.xlsx"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "mystique-compile-check"
DEFAULT_REPO_URL = "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git"
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
COMMIT_URL_RE = re.compile(r"/commit/([0-9a-fA-F]{7,40})(?:[/?#]|$)")


class InfrastructureError(RuntimeError):
    """The environment could not establish a meaningful compile result."""


class GeneratedPatchError(ValueError):
    """The generated patch itself is definitively invalid for this case."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return "\n".join(part for part in (self.stdout, self.stderr) if part)


@dataclass(frozen=True)
class BenchmarkTarget:
    pc_sha: str
    source_path: str
    object_path: str


def run(command: list[str], cwd: Path | None = None, timeout: int | None = 600) -> CommandResult:
    """Run a command without a shell and capture all diagnostic output."""
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            text=True,
            errors="replace",
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise InfrastructureError(
            f"command timed out after {timeout}s: {' '.join(command)}"
        ) from exc
    except OSError as exc:
        raise InfrastructureError(f"could not run {command[0]}: {exc}") from exc
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def output_tail(result: CommandResult, length: int = 3000) -> str:
    text = result.output.strip()
    return text[-length:] if text else "(command produced no output)"


def extract_sha(commit_url: str) -> str:
    match = COMMIT_URL_RE.search(commit_url or "")
    if not match:
        raise ValueError(f"could not extract a commit SHA from {commit_url!r}")
    return match.group(1).lower()


def object_to_source_path(object_path: str) -> str:
    if not object_path.endswith(".o"):
        raise ValueError(f"FixMorph Program CE target is not an .o file: {object_path}")
    return object_path[:-2] + ".c"


def validate_relative_repo_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError(f"unsafe repository path: {value!r}")
    return str(path)


def load_benchmark_targets(path: Path) -> dict[tuple[str, str], BenchmarkTarget]:
    """Map (Pb, Pe) to the exact Pc and Program CE target in the benchmark."""
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        raise InfrastructureError(f"could not read benchmark spreadsheet {path}: {exc}") from exc

    sheet = workbook.active
    rows = sheet.iter_rows(values_only=True)
    try:
        headings = [str(value).strip() if value is not None else "" for value in next(rows)]
    except StopIteration as exc:
        raise InfrastructureError(f"benchmark spreadsheet is empty: {path}") from exc

    required = {"Commit - Pb", "Commit - Pc", "Commit - Pe", "Program CE"}
    missing = required.difference(headings)
    if missing:
        raise InfrastructureError(
            f"benchmark spreadsheet is missing columns: {', '.join(sorted(missing))}"
        )
    column = {name: headings.index(name) for name in required}

    targets: dict[tuple[str, str], BenchmarkTarget] = {}
    for row_number, row in enumerate(rows, start=2):
        values = {
            name: str(row[index]).strip() if index < len(row) and row[index] is not None else ""
            for name, index in column.items()
        }
        if not any(values.values()):
            continue
        pb = values["Commit - Pb"].lower()
        pc = values["Commit - Pc"].lower()
        pe = values["Commit - Pe"].lower()
        if not all(SHA_RE.fullmatch(sha) for sha in (pb, pc, pe)):
            raise InfrastructureError(f"invalid commit SHA in spreadsheet row {row_number}")
        object_path = validate_relative_repo_path(values["Program CE"])
        target = BenchmarkTarget(
            pc_sha=pc,
            source_path=validate_relative_repo_path(object_to_source_path(object_path)),
            object_path=object_path,
        )
        key = (pb, pe)
        if key in targets and targets[key] != target:
            raise InfrastructureError(f"conflicting benchmark entries for Pb={pb}, Pe={pe}")
        targets[key] = target

    workbook.close()
    return targets


def patch_paths(patch_text: str) -> set[str]:
    """Return normalized paths from all file headers in a unified diff."""
    paths: set[str] = set()
    for line in patch_text.splitlines():
        if not (line.startswith("--- ") or line.startswith("+++ ")):
            continue
        value = line[4:].split("\t", 1)[0].strip()
        if value == "/dev/null":
            raise GeneratedPatchError(
                "generated patch creates or deletes the benchmark source file"
            )
        if value.startswith(("a/", "b/")):
            value = value[2:]
        try:
            paths.add(validate_relative_repo_path(value))
        except ValueError as exc:
            raise GeneratedPatchError(str(exc)) from exc
    if not paths:
        raise GeneratedPatchError("generated patch has no unified-diff file headers")
    return paths


def assert_patch_targets(patch_text: str, expected_source_path: str) -> None:
    paths = patch_paths(patch_text)
    if paths != {expected_source_path}:
        raise GeneratedPatchError(
            f"generated patch targets {sorted(paths)!r}; expected only {expected_source_path!r}"
        )


class KernelWorkspace:
    """A bounded, script-owned clone/worktree/build workspace."""

    MARKER = ".mystique-compile-check-owned"

    def __init__(self, cache_dir: Path, repo_url: str):
        self.cache_dir = cache_dir.resolve()
        self.repo_url = repo_url
        self.clone_dir = self.cache_dir / "linux-bare.git"
        self.worktree = self.cache_dir / "worktree"
        self.build_dir = self.cache_dir / "build"

    def initialize(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        marker = self.cache_dir / self.MARKER
        existing = [entry for entry in self.cache_dir.iterdir() if entry.name != self.MARKER]
        if not marker.exists() and existing:
            raise InfrastructureError(
                f"refusing to use non-empty unowned cache directory {self.cache_dir}"
            )
        marker.touch(exist_ok=True)

        if not self.clone_dir.exists():
            print(f"[setup] initialize bare object store -> {self.clone_dir}")
            result = run(["git", "init", "--bare", str(self.clone_dir)])
            if result.returncode != 0:
                raise InfrastructureError(f"Git repository initialization failed:\n{output_tail(result)}")
            result = run(
                ["git", "remote", "add", "origin", self.repo_url],
                cwd=self.clone_dir,
            )
            if result.returncode != 0:
                raise InfrastructureError(f"could not configure Linux remote:\n{output_tail(result)}")
        elif not (self.clone_dir / "HEAD").exists():
            raise InfrastructureError(f"clone path is not a bare Git repository: {self.clone_dir}")
        else:
            actual = run(["git", "remote", "get-url", "origin"], cwd=self.clone_dir)
            if actual.returncode != 0 or actual.stdout.strip() != self.repo_url:
                raise InfrastructureError(
                    f"cached clone origin is {actual.stdout.strip()!r}, not {self.repo_url!r}; "
                    "use the original --repo-url or a different --cache-dir"
                )

    def ensure_commit(self, sha: str) -> None:
        result = run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=self.clone_dir)
        if result.returncode == 0:
            return
        fetched = run(
            ["git", "fetch", "--depth=1", "--no-tags", "origin", sha],
            cwd=self.clone_dir,
            timeout=None,
        )
        if fetched.returncode != 0:
            raise InfrastructureError(f"could not fetch Pc {sha}:\n{output_tail(fetched)}")
        verified = run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=self.clone_dir)
        if verified.returncode != 0:
            raise InfrastructureError(f"fetched object is not a commit: {sha}")

    def checkout(self, sha: str) -> None:
        self.ensure_commit(sha)
        if not self.worktree.exists():
            run(["git", "worktree", "prune"], cwd=self.clone_dir)
            result = run(
                ["git", "worktree", "add", "--detach", str(self.worktree), sha],
                cwd=self.clone_dir,
                timeout=None,
            )
            if result.returncode != 0:
                raise InfrastructureError(f"worktree creation failed:\n{output_tail(result)}")
        else:
            inside = run(["git", "rev-parse", "--is-inside-work-tree"], cwd=self.worktree)
            if inside.returncode != 0 or inside.stdout.strip() != "true":
                raise InfrastructureError(f"cache worktree path is not a Git worktree: {self.worktree}")
            self.restore()
            result = run(["git", "checkout", "--detach", "--force", sha], cwd=self.worktree, timeout=None)
            if result.returncode != 0:
                raise InfrastructureError(f"checkout of Pc {sha} failed:\n{output_tail(result)}")
        self._clear_build_dir()

    def restore(self) -> None:
        reset = run(["git", "reset", "--hard", "HEAD"], cwd=self.worktree)
        clean = run(["git", "clean", "-fdx"], cwd=self.worktree)
        if reset.returncode != 0 or clean.returncode != 0:
            detail = output_tail(reset if reset.returncode else clean)
            raise InfrastructureError(f"could not clean the dedicated worktree:\n{detail}")

    def _clear_build_dir(self) -> None:
        if self.build_dir.exists():
            shutil.rmtree(self.build_dir)
        self.build_dir.mkdir(parents=True)

    def cleanup_row(self) -> None:
        try:
            if self.worktree.exists():
                self.restore()
        finally:
            self._clear_build_dir()


def parse_cross_compile(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"--cross-compile must be ARCH=PREFIX, got {value!r}")
        arch, prefix = value.split("=", 1)
        if not arch or not prefix:
            raise ValueError(f"--cross-compile must be ARCH=PREFIX, got {value!r}")
        result[arch] = prefix
    return result


def resolve_arch(object_path: str, requested_arch: str) -> str:
    if requested_arch != "auto":
        return requested_arch
    parts = PurePosixPath(object_path).parts
    return parts[1] if len(parts) > 2 and parts[0] == "arch" else "x86"


def make_args(workspace: KernelWorkspace, arch: str, cross_prefix: str | None) -> list[str]:
    # The benchmark includes kernels old enough to predate toolchains that
    # default to PIE and -fno-common.  Restore the compiler behavior those
    # kernels expect without changing their source or generated patches.
    hostcc = os.environ.get("HOSTCC", "gcc")
    args = [
        "make",
        f"O={workspace.build_dir}",
        f"ARCH={arch}",
        "KCFLAGS=-fno-pie -fcommon",
        f"HOSTCC={hostcc} -fcommon",
    ]
    if cross_prefix:
        args.append(f"CROSS_COMPILE={cross_prefix}")
    return args


def read_kernel_version(worktree: Path) -> tuple[int, int]:
    """Return (VERSION, PATCHLEVEL) from the checked-out kernel Makefile."""
    makefile = worktree / "Makefile"
    try:
        text = makefile.read_text(errors="replace")
    except OSError as exc:
        raise InfrastructureError(f"could not read {makefile}: {exc}") from exc

    version = re.search(r"^VERSION\s*=\s*(\d+)", text, re.MULTILINE)
    patchlevel = re.search(r"^PATCHLEVEL\s*=\s*(\d+)", text, re.MULTILINE)
    if not version or not patchlevel:
        raise InfrastructureError(
            f"could not read VERSION/PATCHLEVEL from {makefile}"
        )
    return int(version.group(1)), int(patchlevel.group(1))


def select_build_image(version: tuple[int, int]) -> str | None:
    """Return a validated Docker image tag for the given kernel version, or
    ``None`` if no image has been validated for that range yet.

    Only add entries here after a Docker image has been explicitly tested
    against representative commits from that version range.  Unvalidated
    ranges deliberately return ``None`` so the host compiler is used as a
    fallback and the row is flagged in the log for a future re-run.
    """
    # major, minor = version
    # if (3, 0) <= (major, minor) <= (4, 8):
    #     return "gcc:5.5"
    # return 
    major, minor = version
    if (3, 0) <= (major, minor) <= (4, 8):
        return "kbuild-gcc5.5"
    return None


def docker_wrap(
    command: list[str],
    workspace: KernelWorkspace,
    image: str,
) -> list[str]:
    """Wrap *command* in a ``docker run`` invocation that mounts the cache
    directory (which contains both the worktree and the build directory) and
    sets the working directory to the checked-out worktree.

    The container is run as the current user so that any files written by the
    build step are owned by the caller, not root.
    """
    uid = os.getuid()
    gid = os.getgid()
    return [
        "docker", "run", "--rm",
        "--user", f"{uid}:{gid}",
        "--volume", f"{workspace.cache_dir}:{workspace.cache_dir}",
        "--workdir", str(workspace.worktree),
        image,
    ] + command


def configure_kernel(
    workspace: KernelWorkspace,
    arch: str,
    cross_prefix: str | None,
    timeout: int,
    image: str | None = None,
) -> None:
    base = make_args(workspace, arch, cross_prefix)
    # for target in ("defconfig", "prepare"):
    for target in ("allmodconfig", "prepare"):
        cmd = base + [target]
        if image:
            cmd = docker_wrap(cmd, workspace, image)
        result = run(cmd, cwd=workspace.worktree, timeout=timeout)
        if result.returncode != 0:
            raise InfrastructureError(
                f"kernel {target} failed for ARCH={arch}:\n{output_tail(result)}"
            )


def remove_object_outputs(build_dir: Path, object_path: str) -> None:
    target = build_dir / object_path
    candidates = [
        target,
        target.parent / f".{target.name}.cmd",
        target.with_suffix(target.suffix + ".d"),
    ]
    for candidate in candidates:
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def compile_object(
    workspace: KernelWorkspace,
    object_path: str,
    arch: str,
    cross_prefix: str | None,
    timeout: int,
    image: str | None = None,
) -> tuple[bool, str]:
    remove_object_outputs(workspace.build_dir, object_path)
    cmd = make_args(workspace, arch, cross_prefix) + [object_path]
    if image:
        cmd = docker_wrap(cmd, workspace, image)
    result = run(cmd, cwd=workspace.worktree, timeout=timeout)
    object_file = workspace.build_dir / object_path
    success = result.returncode == 0 and object_file.is_file() and object_file.stat().st_size > 0
    if result.returncode == 0 and not success:
        detail = f"make exited successfully but did not produce {object_file}"
    else:
        detail = output_tail(result)
    return success, detail


def apply_patch(worktree: Path, patch_text: str, timeout: int) -> tuple[bool, str]:
    patch_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".patch", delete=False) as handle:
            handle.write(patch_text)
            patch_name = handle.name
        check = run(
            ["git", "apply", "--check", "--recount", patch_name],
            cwd=worktree,
            timeout=timeout,
        )
        if check.returncode != 0:
            return False, output_tail(check)
        applied = run(
            ["git", "apply", "--recount", "--whitespace=nowarn", patch_name],
            cwd=worktree,
            timeout=timeout,
        )
        return applied.returncode == 0, output_tail(applied)
    finally:
        if patch_name:
            try:
                os.unlink(patch_name)
            except FileNotFoundError:
                pass


def fetch_rows(dsn: str, method: str | None, limit: int | None) -> list[dict]:
    where = "generated_patch IS NOT NULL AND btrim(generated_patch) <> ''"
    params: list[object] = []
    if method:
        where += " AND method = %s"
        params.append(method)
    limit_clause = ""
    if limit is not None:
        limit_clause = " LIMIT %s"
        params.append(limit)
    query = (
        "SELECT id, new_version_patch_commit_url, old_version_patch_commit_url, "
        f"generated_patch FROM backport_benchmark_results WHERE {where} "
        f"ORDER BY id{limit_clause}"
    )
    with psycopg2.connect(dsn) as connection:
        with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute(query, params)
            return list(cursor.fetchall())


def update_compilation_result(dsn: str, row_id: int, success: bool) -> None:
    with psycopg2.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE backport_benchmark_results "
                "SET compilation_success = %s, updated_at = NOW() WHERE id = %s",
                (success, row_id),
            )
            if cursor.rowcount != 1:
                raise InfrastructureError(
                    f"expected to update one database row for id={row_id}, updated {cursor.rowcount}"
                )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compile-check generated FixMorph-benchmark patches and update Neon"
    )
    parser.add_argument(
        "--dsn",
        help="Postgres DSN (default: NEON_DATABASE_URL from the environment/.env)",
    )
    parser.add_argument(
        "--main",
        type=Path,
        default=DEFAULT_DATASET,
        help=f"FixMorph Main-data-set.xlsx (default: {DEFAULT_DATASET})",
    )
    parser.add_argument("--method", help="only process rows with this exact method")
    parser.add_argument("--limit", type=int, help="process at most N rows, ordered by id")
    parser.add_argument(
        "--arch",
        default="auto",
        help="kernel ARCH; 'auto' uses arch/<name>/... or x86 (default: auto)",
    )
    parser.add_argument(
        "--cross-compile",
        action="append",
        default=[],
        metavar="ARCH=PREFIX",
        help="CROSS_COMPILE prefix for an architecture; may be repeated",
    )
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--repo-url", default=DEFAULT_REPO_URL)
    parser.add_argument("--build-timeout", type=int, default=1800, metavar="SECONDS")
    parser.add_argument("--apply-timeout", type=int, default=60, metavar="SECONDS")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="perform checkouts/builds and report results without database updates",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv(SCRIPT_DIR / ".env")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be greater than zero")
    if args.build_timeout <= 0 or args.apply_timeout <= 0:
        parser.error("timeouts must be greater than zero")
    try:
        cross_compile = parse_cross_compile(args.cross_compile)
    except ValueError as exc:
        parser.error(str(exc))

    dsn = args.dsn or os.getenv("NEON_DATABASE_URL")
    if not dsn:
        parser.error("provide --dsn or set NEON_DATABASE_URL")

    try:
        targets = load_benchmark_targets(args.main.resolve())
        rows = fetch_rows(dsn, args.method, args.limit)
    except Exception as exc:
        print(f"[fatal] setup/database read failed: {exc}", file=sys.stderr)
        return 2

    print(f"[main] selected {len(rows)} generated patches; benchmark entries={len(targets)}")
    if not rows:
        return 0

    workspace = KernelWorkspace(args.cache_dir, args.repo_url)
    try:
        workspace.initialize()
    except InfrastructureError as exc:
        print(f"[fatal] {exc}", file=sys.stderr)
        return 2

    counts = {"passed": 0, "failed": 0, "inconclusive": 0, "db_errors": 0}
    for row in rows:
        row_id = row["id"]
        definitive: bool | None = None
        try:
            pb_sha = extract_sha(row["new_version_patch_commit_url"])
            pe_sha = extract_sha(row["old_version_patch_commit_url"])
            target = targets.get((pb_sha, pe_sha))
            if target is None:
                raise InfrastructureError(
                    f"no spreadsheet target for Pb={pb_sha[:12]}, Pe={pe_sha[:12]}"
                )
            assert_patch_targets(row["generated_patch"], target.source_path)

            arch = resolve_arch(target.object_path, args.arch)
            cross_prefix = cross_compile.get(arch)
            print(
                f"[row {row_id}] Pc={target.pc_sha[:12]} ARCH={arch} "
                f"target={target.object_path}"
            )
            workspace.checkout(target.pc_sha)

            kernel_version = read_kernel_version(workspace.worktree)
            image = select_build_image(kernel_version)
            version_str = ".".join(str(v) for v in kernel_version)
            if image:
                print(f"[row {row_id}] kernel={version_str} docker-image={image}")
            else:
                # No validated Docker image for this kernel version; falling
                # back to the host compiler.  Flag this row so it can be
                # re-run once a suitable image has been validated and added
                # to select_build_image().
                print(
                    f"[row {row_id}] kernel={version_str} "
                    "UNVALIDATED: no Docker image mapped for this version; "
                    "using host compiler as fallback"
                )

            configure_kernel(workspace, arch, cross_prefix, args.build_timeout, image)

            baseline_ok, baseline_detail = compile_object(
                workspace,
                target.object_path,
                arch,
                cross_prefix,
                args.build_timeout,
                image,
            )
            if not baseline_ok:
                raise InfrastructureError(
                    "unpatched baseline target did not compile; result is inconclusive:\n"
                    + baseline_detail
                )

            applied, apply_detail = apply_patch(
                workspace.worktree, row["generated_patch"], args.apply_timeout
            )
            if not applied:
                print(f"[row {row_id}] FAIL: generated patch does not apply\n{apply_detail}")
                definitive = False
            else:
                patched_ok, patched_detail = compile_object(
                    workspace,
                    target.object_path,
                    arch,
                    cross_prefix,
                    args.build_timeout,
                    image,
                )
                definitive = patched_ok
                if patched_ok:
                    print(f"[row {row_id}] PASS: patched object compiled")
                else:
                    print(f"[row {row_id}] FAIL: patched object did not compile\n{patched_detail}")
                if definitive is True and image is None:
                    # The patch compiled, but against an unvalidated host
                    # compiler.  A PASS here could be a false negative caused
                    # by toolchain incompatibility, so we withhold the result
                    # rather than recording it as a trusted outcome.  A FAIL
                    # on the host is still informative and is not affected.
                    print(
                        f"[row {row_id}] UNTRUSTED-PASS: compiled on unvalidated host "
                        "compiler; not recording as a result until a Docker image is "
                        "validated for this kernel version"
                    )
                    definitive = None

        except GeneratedPatchError as exc:
            # Malformed/misdirected generated output is a definitive non-plausible patch.
            print(f"[row {row_id}] FAIL: {exc}")
            definitive = False
        except ValueError as exc:
            counts["inconclusive"] += 1
            print(f"[row {row_id}] INCONCLUSIVE: invalid metadata: {exc}", file=sys.stderr)
        except InfrastructureError as exc:
            counts["inconclusive"] += 1
            print(f"[row {row_id}] INCONCLUSIVE: {exc}", file=sys.stderr)
        except Exception as exc:
            counts["inconclusive"] += 1
            print(f"[row {row_id}] INCONCLUSIVE: unexpected error: {exc}", file=sys.stderr)
        finally:
            try:
                workspace.cleanup_row()
            except Exception as exc:
                print(f"[row {row_id}] warning: cleanup failed: {exc}", file=sys.stderr)

        if definitive is None:
            continue
        counts["passed" if definitive else "failed"] += 1
        if args.dry_run:
            print(f"[row {row_id}] DRY RUN: database not updated")
            continue
        try:
            update_compilation_result(dsn, row_id, definitive)
            print(f"[row {row_id}] database compilation_success={definitive}")
        except Exception as exc:
            counts["db_errors"] += 1
            print(f"[row {row_id}] DATABASE ERROR: {exc}", file=sys.stderr)

    print(
        "[summary] "
        f"passed={counts['passed']} failed={counts['failed']} "
        f"inconclusive={counts['inconclusive']} db_errors={counts['db_errors']}"
    )
    return 1 if counts["inconclusive"] or counts["db_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
