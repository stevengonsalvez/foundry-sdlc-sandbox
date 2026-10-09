#!/usr/bin/env python3
"""
apply_codegen.py: Apply sdlc-codegen JSON output to the working tree.

Usage:
    PYTHONPATH=tools python scripts/apply_codegen.py <codegen_output.json> <manifest_out>

The input JSON must conform to:
    {"explanation": str, "files": [{"path": str, "content": str}, ...]}

Safety rules (validated before writing anything):
    - Path allow-list, file count and content limits come from sdlc_agents.codegen_policy
      (vendored under tools/, the same module the generate job validates with)
    - No duplicate normalised paths (case-insensitive)
    - No existing component may be a symlink; resolved path must stay inside the repo root
    - Each entry is exactly {path, content}
    - An existing file under tests/ may not lose any `def test_` name or shrink below 70%
      of its current line count
    - Every destination is pre-flighted (no parent component is a non-directory, the target
      is not a directory, no batch path is the parent of another) before the first write

On any violation the script exits non-zero and writes a clear message to stderr.
Nothing is written until all files pass validation.

After a successful write, <manifest_out> holds the written paths, NUL-separated, for
`git add --pathspec-from-file=<manifest_out> --pathspec-file-nul`. It is not created on failure.

stdlib plus tools/sdlc_agents (stdlib only); no external dependencies.
"""

import json
import os
import re
import sys

from sdlc_agents.codegen_policy import MAX_FILES, check_content, normalise_path

MIN_TEST_FILE_LINE_RATIO = 0.7  # a rewritten test file may not shrink below this share

_TEST_DEF_RE = re.compile(r"^[ \t]*(?:async[ \t]+)?def[ \t]+(test_\w*)", re.MULTILINE)


def _assert_no_symlinks(norm: str, repo_root: str) -> None:
    """Reject if any existing component of norm is a symlink or resolves outside the repo."""
    root = os.path.realpath(repo_root)
    cur = root
    for part in norm.split("/"):
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            raise ValueError(f"Path {norm!r} has a symlink component ({part!r}); refusing to write")
    dest = os.path.realpath(os.path.join(root, norm))
    if not dest.startswith(root + os.sep):
        raise ValueError(f"Path {norm!r} escapes the repository root after resolution")


def _validate_path(path: str, repo_root: str) -> str:
    """Lexical allow-list check plus symlink / containment check against repo_root."""
    norm = normalise_path(path)
    _assert_no_symlinks(norm, repo_root)
    return norm


def _assert_test_file_not_gutted(norm: str, content: str, repo_root: str) -> None:
    """Mechanical guard: the model may add tests to an existing test file, not drop them."""
    if not norm.startswith("tests/"):
        return
    existing_path = os.path.join(repo_root, norm)
    if not os.path.isfile(existing_path):
        return
    with open(existing_path, encoding="utf-8", errors="replace") as fh:
        existing = fh.read()
    dropped = set(_TEST_DEF_RE.findall(existing)) - set(_TEST_DEF_RE.findall(content))
    if dropped:
        raise ValueError(
            f"{norm!r} would drop existing test(s): {', '.join(sorted(dropped))}. "
            "Return the full file with all existing tests kept."
        )
    old_lines, new_lines = len(existing.splitlines()), len(content.splitlines())
    if new_lines < old_lines * MIN_TEST_FILE_LINE_RATIO:
        raise ValueError(
            f"{norm!r} would shrink from {old_lines} to {new_lines} lines "
            f"(below {MIN_TEST_FILE_LINE_RATIO:.0%} of the existing file)"
        )


def load_and_validate(json_path: str, repo_root: str = ".") -> list[dict]:
    """Load and fully validate a codegen JSON file. Returns the files list with
    each entry augmented by a 'norm_path' key (the normalised POSIX path)."""
    with open(json_path, encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("Top-level JSON must be an object")
    if "files" not in data:
        raise ValueError("JSON must have a 'files' key")
    files = data["files"]
    if not isinstance(files, list):
        raise ValueError("'files' must be an array")
    if not files:
        raise ValueError("'files' must not be empty")
    if len(files) > MAX_FILES:
        raise ValueError(f"Too many files: {len(files)} > {MAX_FILES}")

    seen: set[str] = set()
    validated: list[dict] = []
    for i, entry in enumerate(files):
        if not isinstance(entry, dict):
            raise ValueError(f"files[{i}] must be an object")
        if set(entry) != {"path", "content"}:
            raise ValueError(f"files[{i}] must have exactly the keys 'path' and 'content'")
        path = entry["path"]
        content = entry["content"]
        norm = _validate_path(path, repo_root)
        check_content(f"files[{i}] {norm!r}", content)
        _assert_test_file_not_gutted(norm, content, repo_root)
        if norm.lower() in seen:
            raise ValueError(f"Duplicate path {path!r} (normalised: {norm!r})")
        seen.add(norm.lower())
        validated.append({"path": path, "norm_path": norm, "content": content})

    return validated


def _assert_writable(norm: str, root: str) -> None:
    """Reject a destination that makedirs/open would fail on halfway through a batch."""
    cur = root
    for part in norm.split("/")[:-1]:
        cur = os.path.join(cur, part)
        if not os.path.lexists(cur):
            return  # nothing below a missing directory can conflict
        if not os.path.isdir(cur):
            raise ValueError(f"Path {norm!r}: parent {os.path.relpath(cur, root)!r} is not a directory")
    if os.path.isdir(os.path.join(root, norm)):
        raise ValueError(f"Path {norm!r} is an existing directory, cannot write a file there")


def _assert_no_file_dir_clash(norms: list[str]) -> None:
    """Reject a batch where one path is a file and another path needs it to be a directory."""
    lowered = {n.lower() for n in norms}
    for n in sorted(lowered):
        parts = n.split("/")
        for i in range(1, len(parts)):
            if "/".join(parts[:i]) in lowered:
                raise ValueError(f"Batch writes {'/'.join(parts[:i])!r} as a file and also under it: {n!r}")


def apply_files(files: list[dict], root: str = ".") -> list[str]:
    """Write files to disk. Called only after full validation passes.

    Re-checks symlinks for all paths first, then writes with
    O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW.
    Returns the list of normalised paths written.
    """
    # Pre-flight every path before the first write so a bad one cannot leave a partial tree.
    _assert_no_file_dir_clash([entry["norm_path"] for entry in files])
    for entry in files:
        _assert_no_symlinks(entry["norm_path"], root)
        _assert_writable(entry["norm_path"], root)
    written: list[str] = []
    for entry in files:
        norm = entry["norm_path"]
        content = entry["content"]
        dest = os.path.join(root, norm)
        dest_dir = os.path.dirname(dest)
        if dest_dir:
            os.makedirs(dest_dir, exist_ok=True)
        # O_NOFOLLOW refuses to open if dest is a symlink.
        fd = os.open(
            dest,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
            0o644,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        print(f"  wrote {norm}")
        written.append(norm)
    return written


def write_manifest(paths: list[str], manifest_path: str) -> None:
    """Write paths NUL-separated (validated paths never contain NUL)."""
    with open(manifest_path, "w", encoding="utf-8") as fh:
        fh.write("".join(f"{p}\0" for p in paths))


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 2:
        print("Usage: apply_codegen.py <codegen_output.json> <manifest_out>", file=sys.stderr)
        return 1
    json_path, manifest_path = args
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        files = load_and_validate(json_path, repo_root)
        write_manifest(apply_files(files, repo_root), manifest_path)
    except (ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Applied {len(files)} file(s) from {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
