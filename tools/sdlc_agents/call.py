"""
CLI: invoke a Foundry SDLC prompt agent and print its text answer to stdout.

Usage:
    python -m sdlc_agents.call --agent <name> --input-file <path> [--max-output-tokens N]

Env:
    FOUNDRY_PROJECT_ENDPOINT  required  e.g. https://<resource>.services.ai.azure.com/api/projects/<project>

Stdout: agent's text answer (markdown or JSON per contract).
Stderr: "usage: in=N out=M reasoning=R" and any error messages.
Exit code: 0 on success, non-zero on any failure.

Invoke path: agent_reference on the project /openai/v1 client.
Retries: SDK max_retries=1, timeout=180s (2 HTTP attempts on 429/5xx).
Guards:
- AZURE_AI_PROJECTS_CONSOLE_LOGGING removed from env before client creation (stdout safety).
- Input nonce-delimited; ci-triage keeps head 2k + tail up to limit.
- Input truncated at HEAD_LIMIT + TAIL_LIMIT chars total.
- max_output_tokens defaults: 8000 general, 16000 codegen; hard ceiling 32000.
- response.status must be 'completed' and output_text non-empty or exit non-zero.
- JSON-output agents: validated against the schema registered with the agent (AgentDef),
  duplicate JSON keys rejected.
- codegen: allow-list path normalisation, file and content caps (sdlc_agents.codegen_policy).
- The agent is invoked by name only, which resolves to its latest version.
- Markdown agents, triage text and codegen explanation: images, tag-shaped HTML stripped
  (code spans/fences kept); @mentions and 'Fixes #' / 'GH-N' defanged.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
from typing import Any

import openai
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

from sdlc_agents.definitions import AGENT_BY_NAME, AGENT_NAMES

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


def _validate_json_output(agent_name: str, text: str) -> Any:
    """Parse (with duplicate-key rejection) and validate JSON output against the agent's schema."""
    try:
        data = _parse_json_strict(text)
    except ValueError as exc:  # json.JSONDecodeError is a ValueError
        raise ValueError(f"{agent_name}: response is not valid JSON: {exc}") from exc
    AGENT_BY_NAME[agent_name].validate_output(data)
    return data


# ---------------------------------------------------------------------------
# Markdown output sanitisation
# ---------------------------------------------------------------------------

# Regex cannot reliably tell where a renderer sees code, so the sanitiser never trusts code
# spans or fences: HTML-ish markup is stripped everywhere, and any '<' left over is escaped.
# Comments and CDATA that never close run to end of text, the way a renderer swallows the rest.
# Images cover inline and reference style (![alt][ref] / ![alt]): both can beacon when rendered.
_MARKUP_RE = re.compile(
    r"(?P<comment><!--.*?(?:-->|\Z))"
    r"|(?P<cdata><!\[CDATA\[.*?(?:\]\]>|\Z))"
    r"|(?P<tag><[A-Za-z/!?][^>]{0,200}>)"
    r"|(?P<image>!\[[^\]]*\](?:\([^)]*\)|\[[^\]]*\])?)",
    re.DOTALL,
)
_MENTION_RE = re.compile(r"(?<!\w)@([\w/-]+)")
# GitHub closing keywords (close/fix/resolve + s/d forms) followed by #N, owner/repo#N, GH-N or a URL.
_FIXES_RE = re.compile(
    r"\b((?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+(?:https?://\S+|[\w./-]*#\d+|GH-\d+))",
    re.IGNORECASE,
)


def _strip_markup(match: re.Match[str]) -> str:
    return "[image removed]" if match.group("image") else ""


def _sanitise_markdown(text: str) -> str:
    """
    Strip or neutralise markdown images, raw HTML, @mentions,
    and 'Fixes #NN' keywords from agent output before posting to GitHub.
    Code spans and fences get no special treatment (a regex can disagree with the renderer
    about where code starts), so every remaining '<' is escaped: generics in code show as '&lt;'.
    """
    # Strip to a fixed point: removing one construct can splice the pieces of another
    # together (e.g. "!<b>[a](http://x)" becomes an image once the tag is gone).
    for _ in range(8):
        stripped = _MARKUP_RE.sub(_strip_markup, text)
        if stripped == text:
            break
        text = stripped
    text = text.replace("<", "&lt;")
    text = _MENTION_RE.sub(r"`@\1`", text)
    # Backticks put the keyword in a code span, where GitHub does not parse closing keywords.
    text = _FIXES_RE.sub(lambda m: f"`{m.group(1)}` [keyword defanged]", text)
    return text


def _sanitise_triage(data: dict) -> None:
    """Sanitise every free-text field of a validated triage result (labels are allow-listed)."""
    data["summary"] = _sanitise_markdown(data["summary"])
    data["acceptance_criteria"] = [_sanitise_markdown(s) for s in data["acceptance_criteria"]]
    data["tasks"] = [_sanitise_markdown(s) for s in data["tasks"]]


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


# ---------------------------------------------------------------------------
# Invocation (agent_reference on the /openai/v1 client)
# ---------------------------------------------------------------------------

class AgentRunError(RuntimeError):
    pass


def invoke_agent(
    project: AIProjectClient,
    agent_name: str,
    prompt: str,
    max_output_tokens: int,
) -> tuple[str, object]:
    """
    Invoke a prompt agent via agent_reference on the project /openai/v1 client.
    No version is sent, so the service resolves the agent's latest version.
    SDK retries handled internally (max_retries=1 -> 2 HTTP attempts on 429/5xx).
    Returns (output_text, usage).
    Raises AgentRunError on HTTP errors, incomplete responses, or empty output.
    """
    agent_ref = {"type": "agent_reference", "name": agent_name}
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
        try:
            text, usage = invoke_agent(project, args.agent, prompt, max_tokens)
        except AgentRunError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)

    # Log usage.
    in_tok = getattr(usage, "input_tokens", 0) if usage else 0
    out_tok = getattr(usage, "output_tokens", 0) if usage else 0
    details = getattr(usage, "output_tokens_details", None) if usage else None
    reasoning_tok = getattr(details, "reasoning_tokens", 0) if details else 0
    print(f"usage: in={in_tok} out={out_tok} reasoning={reasoning_tok}", file=sys.stderr)

    agent = AGENT_BY_NAME[args.agent]
    if agent.output_kind == "json":
        try:
            data = _validate_json_output(args.agent, text)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        if args.agent == "sdlc-codegen":
            # The explanation lands in a PR body, so it gets the markdown treatment too.
            data["explanation"] = _sanitise_markdown(data["explanation"])
        elif args.agent == "sdlc-triage":
            # Triage text lands in an issue comment.
            _sanitise_triage(data)
        # Emit the validated, normalised object (codegen paths are canonical), not raw model text.
        text = json.dumps(data, ensure_ascii=False)
    else:
        text = _sanitise_markdown(text)

    print(text)


if __name__ == "__main__":
    main()
