"""
CLI: invoke a Foundry SDLC prompt agent and print its text answer to stdout.

Usage:
    python -m sdlc_agents.call --agent <name> --input-file <path> [--max-output-tokens N]

Env:
    FOUNDRY_PROJECT_ENDPOINT  required  e.g. https://<resource>.services.ai.azure.com/api/projects/<project>

Stdout: agent's text answer (markdown or JSON per contract).
Stderr: "usage: in=N out=M reasoning=R" and any error messages.
Exit code: 0 on success, non-zero on any failure.

Invoke path: agent_reference on the project /openai/v1 client (S1 from review-agents.md).
Retries: SDK max_retries=1, timeout=180s (2 HTTP attempts on 429/5xx).
Guards:
- AZURE_AI_PROJECTS_CONSOLE_LOGGING removed from env before client creation (stdout safety).
- Input nonce-delimited; ci-triage keeps head 2k + tail up to limit.
- Input truncated at HEAD_LIMIT + TAIL_LIMIT chars total.
- max_output_tokens defaults: 8000 general, 16000 codegen; hard ceiling 32000.
- response.status must be 'completed' and output_text non-empty or exit non-zero.
- JSON-output agents: schema validated, extra keys rejected, duplicate JSON keys rejected.
- codegen: allowlist path normalisation, 20-file cap, 200k content cap, unique paths.
- Markdown agents: images, raw HTML stripped; @mentions and 'Fixes #' defanged.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
from pathlib import PurePosixPath
from typing import Any

import openai
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

from sdlc_agents.definitions import AGENT_NAMES, ALLOWED_LABELS

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HEAD_LIMIT = 2_000        # chars kept from log head for ci-triage
TAIL_LIMIT = 58_000       # chars kept from log tail; general head limit
INPUT_CHAR_LIMIT = HEAD_LIMIT + TAIL_LIMIT  # 60k total, matches original contract

# Per-agent max_output_tokens defaults.
DEFAULT_MAX_OUTPUT_TOKENS = 8_000
CODEGEN_MAX_OUTPUT_TOKENS = 16_000
HARD_MAX_OUTPUT_TOKENS = 32_000

# Codegen path guard constants (S3 from review-agents.md).
ALLOWED_ROOTS = ("src/", "tests/", "docs/")
_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f\\:]")

MAX_CODEGEN_FILES = 20
MAX_CONTENT_BYTES = 200_000

# Triage label constraints.
TRIAGE_REQUIRED_KEYS = frozenset({"labels", "summary", "acceptance_criteria", "tasks"})

# Markdown agents whose output is sanitised before printing.
MARKDOWN_AGENTS = frozenset({"sdlc-review", "sdlc-ci-triage", "sdlc-release-notes"})

# Validators dispatch table.
VALIDATORS: dict[str, Any] = {}  # populated after function definitions below


# ---------------------------------------------------------------------------
# Path normalisation (allowlist, S3)
# ---------------------------------------------------------------------------

def normalise_path(path: str) -> str:
    """
    Normalise and validate a codegen file path.
    Returns the normalised POSIX path string.
    Raises ValueError for any rejected path.
    Lexical only; symlink checks happen in the sandbox's scripts/apply_codegen.py, whose
    normalise_path is behaviourally identical (same corpus is tested in both repos).
    """
    if not path or len(path) > 255:
        raise ValueError(f"path rejected (empty or too long): {path!r}")
    if path != path.strip():
        raise ValueError(f"path rejected (leading/trailing whitespace): {path!r}")
    if not path.isascii():
        raise ValueError(f"path rejected (non-ASCII): {path!r}")
    if _BAD_CHARS.search(path):
        raise ValueError(f"path rejected (control char, backslash, or colon): {path!r}")
    if path.endswith("/"):
        raise ValueError(f"path rejected (trailing slash): {path!r}")
    p = PurePosixPath(path)
    if p.is_absolute():
        raise ValueError(f"path rejected (absolute): {path!r}")
    if not p.parts:
        raise ValueError(f"path rejected (no components): {path!r}")
    if ".." in p.parts:
        raise ValueError(f"path rejected ('..') : {path!r}")
    for seg in p.parts:
        s = seg.casefold().rstrip(". ")
        if not s:
            raise ValueError(f"path rejected (empty or dot-only component): {path!r}")
        # Any dot-prefixed component (.git, .github, .claude, .env, ...) is refused.
        if seg.startswith("."):
            raise ValueError(f"path rejected (dot-prefixed component): {path!r}")
    norm = "/".join(p.parts)
    if not any(norm.startswith(root) for root in ALLOWED_ROOTS):
        raise ValueError(f"path outside allowed roots {ALLOWED_ROOTS}: {path!r}")
    return norm


# ---------------------------------------------------------------------------
# JSON parsing helpers (duplicate-key rejection)
# ---------------------------------------------------------------------------

def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    seen: dict[str, Any] = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError(f"duplicate JSON key: {k!r}")
        seen[k] = v
    return seen


def _parse_json_strict(text: str) -> Any:
    """Parse JSON rejecting duplicate keys and JSON NaN/Infinity constants."""
    # parse_constant is called for NaN, Infinity, -Infinity
    def _bad_constant(c: str) -> None:
        raise ValueError(f"JSON constant not allowed: {c}")

    return json.loads(
        text,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_bad_constant,
    )


# ---------------------------------------------------------------------------
# Schema validators
# ---------------------------------------------------------------------------

def _validate_triage(data: Any) -> None:
    """Validate sdlc-triage JSON output."""
    if not isinstance(data, dict):
        raise ValueError("sdlc-triage: response is not a JSON object")
    extra = set(data) - TRIAGE_REQUIRED_KEYS
    if extra:
        raise ValueError(f"sdlc-triage: unexpected keys: {extra}")
    missing = TRIAGE_REQUIRED_KEYS - set(data)
    if missing:
        raise ValueError(f"sdlc-triage: missing keys: {missing}")

    labels = data["labels"]
    if not isinstance(labels, list) or not (1 <= len(labels) <= 3):
        raise ValueError("sdlc-triage: 'labels' must be a list of 1-3 items")
    for lbl in labels:
        if not isinstance(lbl, str):
            raise ValueError("sdlc-triage: each label must be a string")
        if lbl not in ALLOWED_LABELS:
            raise ValueError(f"sdlc-triage: label {lbl!r} not in allowed set")

    summary = data["summary"]
    if not isinstance(summary, str) or not summary:
        raise ValueError("sdlc-triage: 'summary' must be a non-empty string")
    if len(summary) > 120:
        raise ValueError("sdlc-triage: 'summary' exceeds 120 chars")

    ac = data["acceptance_criteria"]
    if not isinstance(ac, list) or not (1 <= len(ac) <= 5):
        raise ValueError("sdlc-triage: 'acceptance_criteria' must be a list of 1-5 items")
    for item in ac:
        if not isinstance(item, str) or len(item) > 300:
            raise ValueError("sdlc-triage: each acceptance criterion must be a string <=300 chars")

    tasks = data["tasks"]
    if not isinstance(tasks, list) or not (1 <= len(tasks) <= 8):
        raise ValueError("sdlc-triage: 'tasks' must be a list of 1-8 items")
    for item in tasks:
        if not isinstance(item, str) or len(item) > 300:
            raise ValueError("sdlc-triage: each task must be a string <=300 chars")


def _validate_codegen(data: Any) -> None:
    """Validate sdlc-codegen JSON output with allowlist path normalisation."""
    if not isinstance(data, dict):
        raise ValueError("sdlc-codegen: response is not a JSON object")
    extra = set(data) - {"explanation", "files"}
    if extra:
        raise ValueError(f"sdlc-codegen: unexpected keys: {extra}")
    for key in ("explanation", "files"):
        if key not in data:
            raise ValueError(f"sdlc-codegen: missing key '{key}'")

    explanation = data["explanation"]
    if not isinstance(explanation, str) or not explanation.strip():
        raise ValueError("sdlc-codegen: 'explanation' must be a non-empty string")
    if len(explanation) > 500:
        raise ValueError("sdlc-codegen: 'explanation' exceeds 500 chars")

    files = data["files"]
    if not isinstance(files, list):
        raise ValueError("sdlc-codegen: 'files' must be a list")
    if not (1 <= len(files) <= MAX_CODEGEN_FILES):
        raise ValueError(
            f"sdlc-codegen: 'files' must have 1-{MAX_CODEGEN_FILES} entries, got {len(files)}"
        )

    seen_paths: set[str] = set()
    for i, f in enumerate(files):
        if not isinstance(f, dict):
            raise ValueError(f"sdlc-codegen: files[{i}] must be an object")
        extra_f = set(f) - {"path", "content"}
        if extra_f:
            raise ValueError(f"sdlc-codegen: files[{i}] unexpected keys: {extra_f}")
        if "path" not in f or "content" not in f:
            raise ValueError(f"sdlc-codegen: files[{i}] missing 'path' or 'content'")
        if not isinstance(f["path"], str) or not isinstance(f["content"], str):
            raise ValueError(f"sdlc-codegen: files[{i}] 'path' and 'content' must be strings")
        # Allowlist normalisation (raises ValueError on any rejected path).
        norm = normalise_path(f["path"])
        norm_lower = norm.lower()
        if norm_lower in seen_paths:
            raise ValueError(f"sdlc-codegen: duplicate path '{norm}'")
        seen_paths.add(norm_lower)
        content = f["content"]
        if "\x00" in content:
            raise ValueError(f"sdlc-codegen: files[{i}] content contains NUL byte")
        if len(content.encode("utf-8", errors="replace")) > MAX_CONTENT_BYTES:
            raise ValueError(
                f"sdlc-codegen: files[{i}] content exceeds {MAX_CONTENT_BYTES} bytes"
            )
        # Store normalised path back so callers see the canonical form.
        f["path"] = norm


def _validate_json_output(agent_name: str, text: str) -> Any:
    """Parse (with duplicate-key rejection) and validate JSON output for JSON agents."""
    try:
        data = _parse_json_strict(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{agent_name}: response is not valid JSON: {exc}") from exc
    VALIDATORS[agent_name](data)
    return data


VALIDATORS["sdlc-triage"] = _validate_triage
VALIDATORS["sdlc-codegen"] = _validate_codegen


# ---------------------------------------------------------------------------
# Markdown output sanitisation
# ---------------------------------------------------------------------------

# Inline images and reference-style images (![alt][ref] / ![alt]): both can beacon to an
# attacker host when rendered.
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\](?:\([^)]*\)|\[[^\]]*\])?")
_HTML_TAG_RE = re.compile(r"<[^>]{0,200}>", re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_MENTION_RE = re.compile(r"(?<!\w)@([\w/-]+)")
# GitHub closing keywords (close/fix/resolve + s/d forms) followed by #N, owner/repo#N or a URL.
_FIXES_RE = re.compile(
    r"\b((?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+(?:https?://\S+|[\w./-]*#\d+))",
    re.IGNORECASE,
)


def _sanitise_markdown(text: str) -> str:
    """
    Strip or neutralise markdown images, raw HTML, @mentions,
    and 'Fixes #NN' keywords from agent output before posting to GitHub.
    """
    # Strip to a fixed point: removing one construct can splice the pieces of another
    # together (e.g. "!<b>[a](http://x)" becomes an image once the tag is gone).
    for _ in range(8):
        stripped = _HTML_COMMENT_RE.sub("", text)
        stripped = _HTML_TAG_RE.sub("", stripped)
        stripped = _MD_IMAGE_RE.sub("[image removed]", stripped)
        if stripped == text:
            break
        text = stripped
    text = _MENTION_RE.sub(r"`@\1`", text)
    # Backticks put the keyword in a code span, where GitHub does not parse closing keywords.
    text = _FIXES_RE.sub(lambda m: f"`{m.group(1)}` [keyword defanged]", text)
    return text


# ---------------------------------------------------------------------------
# Input preparation
# ---------------------------------------------------------------------------

def _delimit(body: str, nonce: str) -> str:
    return (
        f"<untrusted_input nonce={nonce}>\n"
        f"{body}\n"
        f"</untrusted_input nonce={nonce}>"
    )


def _log_body(head: str, tail: str, nonce: str) -> str:
    # The marker carries the per-call nonce so input text cannot forge it.
    return (
        head
        + f"\n\n[LOG TRUNCATED nonce={nonce}: showing first {HEAD_LIMIT} and last {TAIL_LIMIT} chars]\n\n"
        + tail
    )


def _wrap_untrusted(text: str, agent_name: str) -> str:
    """
    Wrap untrusted input in a per-call random nonce delimiter so the model
    can clearly distinguish data from instructions.
    For ci-triage: keep head 2k + tail 58k chars (failure lines are at the tail).
    For other agents: keep head 60k chars.
    The nonce is generated fresh per call so it cannot be forged by the input.
    """
    nonce = secrets.token_hex(8)
    if len(text) <= INPUT_CHAR_LIMIT:
        return _delimit(text, nonce)
    if agent_name == "sdlc-ci-triage":
        return _delimit(_log_body(text[:HEAD_LIMIT], text[-TAIL_LIMIT:], nonce), nonce)
    body = text[:INPUT_CHAR_LIMIT] + f"\n\n[INPUT TRUNCATED AT {INPUT_CHAR_LIMIT} CHARS nonce={nonce}]\n"
    return _delimit(body, nonce)


def _read_log_head_tail(fh: Any) -> str:
    """
    Stream a (possibly huge) log keeping only the first HEAD_LIMIT chars and the last
    TAIL_LIMIT chars, so the real tail of the file survives. Memory is O(HEAD+TAIL).
    """
    head = fh.read(HEAD_LIMIT)
    tail = ""
    skipped = False
    while chunk := fh.read(64 * 1024):
        tail += chunk
        if len(tail) > TAIL_LIMIT:
            tail = tail[-TAIL_LIMIT:]
            skipped = True
    nonce = secrets.token_hex(8)
    return _delimit(_log_body(head, tail, nonce) if skipped else head + tail, nonce)


# ---------------------------------------------------------------------------
# Client helpers
# ---------------------------------------------------------------------------

def _build_client(endpoint: str) -> AIProjectClient:
    return AIProjectClient(
        endpoint=endpoint,
        credential=DefaultAzureCredential(),
    )


def _get_latest_version(project: AIProjectClient, agent_name: str) -> str | None:
    """Return the latest version string for agent_name, or None if agent not found."""
    try:
        details = project.agents.get(agent_name)
        latest = getattr(getattr(details, "versions", None), "latest", None)
        if latest is not None:
            v = getattr(latest, "version", None)
            if v:
                return str(v)
        return None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Invocation (S1: agent_reference on /openai/v1 client)
# ---------------------------------------------------------------------------

class AgentRunError(RuntimeError):
    pass


def invoke_agent(
    project: AIProjectClient,
    agent_name: str,
    version: str | None,
    prompt: str,
    max_output_tokens: int,
) -> tuple[str, object]:
    """
    Invoke a prompt agent via agent_reference on the project /openai/v1 client.
    SDK retries handled internally (max_retries=1 -> 2 HTTP attempts on 429/5xx).
    Returns (output_text, usage).
    Raises AgentRunError on HTTP errors, incomplete responses, or empty output.
    """
    agent_ref: dict[str, Any] = {"type": "agent_reference", "name": agent_name}
    if version:
        agent_ref["version"] = version

    with project.get_openai_client(max_retries=1, timeout=180.0) as oc:
        try:
            resp = oc.responses.create(
                input=prompt,
                max_output_tokens=max_output_tokens,
                extra_body={"agent_reference": agent_ref},
            )
        except openai.APIStatusError as exc:
            raise AgentRunError(f"HTTP {exc.status_code}: {exc.message}") from exc
        except openai.APIError as exc:  # connection errors and timeouts (no status code)
            raise AgentRunError(f"request failed: {type(exc).__name__}: {exc.message}") from exc

    if resp.status != "completed":
        reason = (
            resp.incomplete_details.reason
            if getattr(resp, "incomplete_details", None)
            else resp.status
        )
        raise AgentRunError(f"response {resp.id} not completed: {reason}")

    text = resp.output_text
    if not text.strip():
        raise AgentRunError(f"response {resp.id} returned empty text")

    return text, resp.usage


# ---------------------------------------------------------------------------
# CLI arg parsing
# ---------------------------------------------------------------------------

def _positive_int(val: str) -> int:
    n = int(val)
    if n < 16:
        raise argparse.ArgumentTypeError("must be at least 16")
    return n


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m sdlc_agents.call",
        description="Invoke a Foundry SDLC prompt agent.",
    )
    parser.add_argument(
        "--agent",
        required=True,
        choices=AGENT_NAMES,
        help="Agent name",
    )
    parser.add_argument(
        "--input-file",
        required=True,
        help="Path to input text file ('-' for stdin)",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=_positive_int,
        default=None,
        help=(
            f"Max output tokens (defaults: {DEFAULT_MAX_OUTPUT_TOKENS} general, "
            f"{CODEGEN_MAX_OUTPUT_TOKENS} codegen; hard ceiling {HARD_MAX_OUTPUT_TOKENS})"
        ),
    )
    return parser.parse_args(argv)


def _read_input(path: str, agent_name: str) -> str:
    def read(fh: Any) -> str:
        if agent_name == "sdlc-ci-triage":
            return _read_log_head_tail(fh)
        return _wrap_untrusted(fh.read(INPUT_CHAR_LIMIT + 1), agent_name)

    if path == "-":
        return read(sys.stdin)
    with open(path, encoding="utf-8", errors="replace") as fh:
        return read(fh)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    # Must happen before any SDK client exists: when set, the SDK attaches a DEBUG
    # StreamHandler on stdout and logs request/response bodies (stdout must be the answer only).
    os.environ.pop("AZURE_AI_PROJECTS_CONSOLE_LOGGING", None)
    args = _parse_args(argv)

    # Per-agent default tokens.
    if args.max_output_tokens is not None:
        requested = args.max_output_tokens
    elif args.agent == "sdlc-codegen":
        requested = CODEGEN_MAX_OUTPUT_TOKENS
    else:
        requested = DEFAULT_MAX_OUTPUT_TOKENS

    max_tokens = min(requested, HARD_MAX_OUTPUT_TOKENS)
    if max_tokens != requested:
        print(
            f"WARNING: --max-output-tokens clamped to {HARD_MAX_OUTPUT_TOKENS}.",
            file=sys.stderr,
        )

    # Read and wrap input.
    try:
        prompt = _read_input(args.input_file, args.agent)
    except OSError as exc:
        print(f"ERROR reading input file: {exc}", file=sys.stderr)
        sys.exit(1)

    # Build project client.
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").strip()
    if not endpoint:
        print("ERROR: FOUNDRY_PROJECT_ENDPOINT is not set.", file=sys.stderr)
        sys.exit(1)

    with _build_client(endpoint) as project:
        # Look up latest pinned version (best-effort).
        version = _get_latest_version(project, args.agent)

        # Invoke.
        try:
            text, usage = invoke_agent(project, args.agent, version, prompt, max_tokens)
        except AgentRunError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)

    # Log usage.
    in_tok = getattr(usage, "input_tokens", 0) if usage else 0
    out_tok = getattr(usage, "output_tokens", 0) if usage else 0
    details = getattr(usage, "output_tokens_details", None) if usage else None
    reasoning_tok = getattr(details, "reasoning_tokens", 0) if details else 0
    print(f"usage: in={in_tok} out={out_tok} reasoning={reasoning_tok}", file=sys.stderr)

    # Validate JSON output for JSON agents.
    if args.agent in VALIDATORS:
        try:
            data = _validate_json_output(args.agent, text)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        if args.agent == "sdlc-codegen":
            # The explanation lands in a PR body, so it gets the markdown treatment too.
            data["explanation"] = _sanitise_markdown(data["explanation"])
        # Emit the validated, normalised object (codegen paths are canonical), not raw model text.
        text = json.dumps(data, ensure_ascii=False)

    # Sanitise markdown output for markdown agents.
    if args.agent in MARKDOWN_AGENTS:
        text = _sanitise_markdown(text)

    print(text)


if __name__ == "__main__":
    main()
