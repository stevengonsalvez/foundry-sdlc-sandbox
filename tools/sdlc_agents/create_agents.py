"""
Idempotent script: registers or updates all SDLC prompt agents against the
Foundry project identified by FOUNDRY_PROJECT_ENDPOINT.

Idempotency: a SHA-256 of the serialised definition is stored in agent
metadata. If the hash matches the currently deployed version, the version
is reused and printed as 'unchanged'. Otherwise a new version is created.

Requires Foundry User role on the project.
Run from a workflow_dispatch job; CI invoke-only jobs do not need this.

Usage:
    python -m sdlc_agents.create_agents

Env:
    FOUNDRY_PROJECT_ENDPOINT  required

Prints one line per agent: "<name>  version=<version>  [unchanged|created]"
Exits non-zero on any error.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    PromptAgentDefinition,
    PromptAgentDefinitionTextOptions,
    Reasoning,
    TextResponseFormatJsonSchema,
)
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential

from sdlc_agents.definitions import (
    AGENTS,
    CODEGEN_SCHEMA,
    MODEL_DEPLOYMENT,
    TRIAGE_SCHEMA,
)

# Per-agent JSON schemas; None means text output (markdown agents).
_SCHEMAS: dict[str, dict | None] = {
    "sdlc-triage": TRIAGE_SCHEMA,
    "sdlc-codegen": CODEGEN_SCHEMA,
    "sdlc-review": None,
    "sdlc-ci-triage": None,
    "sdlc-release-notes": None,
}


def _build_definition(name: str, instructions: str) -> PromptAgentDefinition:
    """Build a PromptAgentDefinition with reasoning=low and optional JSON schema."""
    schema = _SCHEMAS.get(name)
    text = None
    if schema is not None:
        text = PromptAgentDefinitionTextOptions(
            format=TextResponseFormatJsonSchema(
                name="output",
                schema=schema,
                strict=True,
            )
        )
    return PromptAgentDefinition(
        model=MODEL_DEPLOYMENT,
        instructions=instructions,
        reasoning=Reasoning(effort="low"),
        text=text,
    )


def _definition_hash(definition: PromptAgentDefinition) -> str:
    """Return a SHA-256 hex digest of the canonical serialised definition."""
    canonical = json.dumps(definition.as_dict(), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _ensure_version(
    client: AIProjectClient,
    name: str,
    definition: PromptAgentDefinition,
) -> tuple[str, str]:
    """
    Register or reuse a version for the agent.
    Returns (version_string, action) where action is 'unchanged' or 'created'.
    """
    digest = _definition_hash(definition)
    try:
        details = client.agents.get(name)
        latest = getattr(getattr(details, "versions", None), "latest", None)
        if latest is not None:
            existing_hash = (getattr(latest, "metadata", None) or {}).get(
                "definition_sha256"
            )
            if existing_hash == digest:
                return str(latest.version), "unchanged"
    except ResourceNotFoundError:
        pass  # agent does not exist yet; create it
    # Any other error propagates: minting a version on an unreadable state defeats idempotency.

    result = client.agents.create_version(
        agent_name=name,
        definition=definition,
        metadata={"definition_sha256": digest},
    )
    return str(result.version), "created"


def _get_client() -> AIProjectClient:
    os.environ.pop("AZURE_AI_PROJECTS_CONSOLE_LOGGING", None)
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").strip()
    if not endpoint:
        print("ERROR: FOUNDRY_PROJECT_ENDPOINT is not set.", file=sys.stderr)
        sys.exit(1)
    return AIProjectClient(
        endpoint=endpoint,
        credential=DefaultAzureCredential(),
    )


def main() -> None:
    with _get_client() as client:
        errors: list[str] = []
        for agent in AGENTS:
            try:
                definition = _build_definition(agent.name, agent.instructions)
                version, action = _ensure_version(client, agent.name, definition)
                print(f"{agent.name}  version={version}  {action}")
            except Exception as exc:  # noqa: BLE001
                msg = f"ERROR registering {agent.name}: {exc}"
                print(msg, file=sys.stderr)
                errors.append(msg)
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
