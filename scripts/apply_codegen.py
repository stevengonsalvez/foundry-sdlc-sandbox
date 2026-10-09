#!/usr/bin/env python3
"""
apply_codegen.py: Apply sdlc-codegen JSON output to the working tree.

Usage:
    python scripts/apply_codegen.py <codegen_output.json>

The input JSON must conform to:
    {"explanation": str, "files": [{"path": str, "content": str}, ...]}

Safety rules (validated before writing anything):
    - Paths must be under an allow-listed root: src/, tests/, docs/
    - No absolute paths, backslashes, colons, control chars, non-ASCII, surrounding whitespace
    - No path traversal (..) and no dot-prefixed segments (.git, .github, .env, ...)
    - First component is exactly src, tests or docs (case-sensitive); "./" and "//" collapse
    - No NUL bytes, no trailing slash
    - No duplicate normalised paths (case-insensitive)
    - No existing component may be a symlink; resolved path must stay inside the repo root
    - 1 to 20 files, each entry exactly {path, content}
    - Maximum 200 KB per file content (as UTF-8 bytes)

On any violation the script exits non-zero and writes a clear message to stderr.
Nothing is written until all files pass validation.

stdlib only; no external dependencies.
"""

import json
import os
import re
import sys
from pathlib import PurePosixPath

MAX_FILES = 20
MAX_FILE_BYTES = 200 * 1024  # 200 KB

# Only these roots may be written.
ALLOWED_ROOTS = ("src/", "tests/", "docs/")

# Control chars, backslash and colon (Windows separators / drive letters / NTFS streams).
_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f\\:]")


def normalise_path(path: str) -> str:
    """Return the normalised POSIX path if it is allowed; raise ValueError otherwise.

    Lexical only. Keep behaviourally identical to sdlc_agents.call.normalise_path
    (the generate job rejects with the same rules before this script re-checks).
    """
    if not isinstance(path, str) or not path:
        raise ValueError(f"path must be a non-empty string, got {path!r}")
    if "\x00" in path:
        raise ValueError(f"NUL byte in path: {path!r}")
    if len(path) > 255:
        raise ValueError(f"path too long: {path!r}")
    if path != path.strip():
        raise ValueError(f"forbidden whitespace around path: {path!r}")
    if not path.isascii():
        raise ValueError(f"forbidden non-ASCII path: {path!r}")
    if _BAD_CHARS.search(path):
        raise ValueError(f"forbidden character (control, backslash or colon) in path: {path!r}")
    if path.endswith("/"):
        raise ValueError(f"Path must not be a directory (trailing slash): {path!r}")
    p = PurePosixPath(path)
    if p.is_absolute():
        raise ValueError(f"Absolute path not allowed: {path!r}")
    if not p.parts:
        raise ValueError(f"path has no components: {path!r}")
    if ".." in p.parts:
        raise ValueError(f"Path contains a forbidden segment '..': {path!r}")
    for seg in p.parts:
        # casefold + strip trailing dots/spaces defeats ".GITHUB", ".git." and friends.
        folded = seg.casefold().rstrip(". ")
        if not folded or seg.startswith("."):
            raise ValueError(f"Path contains a forbidden segment {seg!r}: {path!r}")
    norm = "/".join(p.parts)
    if not norm.startswith(ALLOWED_ROOTS):
        raise ValueError(
            f"Path {path!r} is not under an allowed root {ALLOWED_ROOTS}. "
            "Only src/, tests/, docs/ may be written."
        )
    return norm


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


def _validate_content(path: str, content: str) -> None:
    """Raise ValueError if content exceeds the size limit."""
    if not isinstance(content, str):
        raise ValueError(f"content for {path!r} must be a string")
    if "\x00" in content:
        raise ValueError(f"content for {path!r} contains a NUL byte")
    size = len(content.encode("utf-8"))
    if size > MAX_FILE_BYTES:
        raise ValueError(
            f"File {path!r} is {size} bytes, exceeds limit of {MAX_FILE_BYTES} bytes"
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
        _validate_content(path, content)
        if norm.lower() in seen:
            raise ValueError(f"Duplicate path {path!r} (normalised: {norm!r})")
        seen.add(norm.lower())
        validated.append({"path": path, "norm_path": norm, "content": content})

    return validated


def apply_files(files: list[dict], root: str = ".") -> list[str]:
    """Write files to disk. Called only after full validation passes.

    Re-checks symlinks for all paths first, then writes with
    O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW.
    Returns the list of normalised paths written.
    """
    # Pre-flight every path before the first write so a bad one cannot leave a partial tree.
    for entry in files:
        _assert_no_symlinks(entry["norm_path"], root)
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


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print("Usage: apply_codegen.py <codegen_output.json>", file=sys.stderr)
        return 1
    json_path = args[0]
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        files = load_and_validate(json_path, repo_root)
        apply_files(files, repo_root)
    except (ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Applied {len(files)} file(s) from {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
