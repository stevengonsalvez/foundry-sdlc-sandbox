"""
Write policy for sdlc-codegen output: the single source for the path allow-list and limits.

Used by sdlc_agents.call (generate job) and by the sandbox's scripts/apply_codegen.py
(publish job), which imports this module from the vendored tools/ copy.
Stdlib only, so the publish job can import it without the Azure SDK installed.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

ALLOWED_ROOTS: tuple[str, ...] = ("src/", "tests/", "docs/")
MAX_FILES = 20
MAX_CONTENT_BYTES = 200_000
MAX_PATH_LENGTH = 255

# Control chars, backslash and colon (Windows separators, drive letters, NTFS streams).
_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f\\:]")


def allowed_roots_text() -> str:
    """Human-readable list of the allowed roots, e.g. 'src/, tests/, or docs/'."""
    return ", ".join(ALLOWED_ROOTS[:-1]) + ", or " + ALLOWED_ROOTS[-1]


def normalise_path(path: object) -> str:
    """
    Return the normalised POSIX path if it may be written; raise ValueError otherwise.

    Lexical only: the publish job adds symlink and containment checks against the checkout.
    """
    if not isinstance(path, str) or not path:
        raise ValueError(f"path rejected (empty or not a string): {path!r}")
    if "\x00" in path:
        raise ValueError(f"path rejected (NUL byte): {path!r}")
    if len(path) > MAX_PATH_LENGTH:
        raise ValueError(f"path rejected (too long, max {MAX_PATH_LENGTH}): {path[:80]!r}...")
    if path != path.strip():
        raise ValueError(f"path rejected (leading/trailing whitespace): {path!r}")
    if not path.isascii():
        raise ValueError(f"path rejected (non-ASCII): {path!r}")
    if _BAD_CHARS.search(path):
        raise ValueError(f"path rejected (control char, backslash, or colon): {path!r}")
    if path.endswith("/"):
        raise ValueError(f"path rejected (trailing slash, directory): {path!r}")
    p = PurePosixPath(path)
    if p.is_absolute():
        raise ValueError(f"path rejected (absolute path): {path!r}")
    if ".." in p.parts:
        raise ValueError(f"path rejected (forbidden segment '..'): {path!r}")
    for seg in p.parts:
        # casefold + strip trailing dots/spaces defeats ".GITHUB", ".git." and friends;
        # any dot-prefixed component (.git, .github, .claude, .env, ...) is refused.
        if not seg.casefold().rstrip(". ") or seg.startswith("."):
            raise ValueError(f"path rejected (forbidden segment {seg!r}): {path!r}")
    norm = "/".join(p.parts)
    if not norm.startswith(ALLOWED_ROOTS):
        raise ValueError(f"path rejected (outside allowed roots {allowed_roots_text()}): {path!r}")
    return norm


def check_content(label: str, content: object) -> None:
    """Raise ValueError unless content is NUL-free UTF-8 text within MAX_CONTENT_BYTES."""
    if not isinstance(content, str):
        raise ValueError(f"{label}: content must be a string")
    if "\x00" in content:
        raise ValueError(f"{label}: content contains a NUL byte")
    try:
        size = len(content.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label}: content is not valid UTF-8 text ({exc.reason})") from exc
    if size > MAX_CONTENT_BYTES:
        raise ValueError(f"{label}: content is {size} bytes, exceeds limit of {MAX_CONTENT_BYTES} bytes")
