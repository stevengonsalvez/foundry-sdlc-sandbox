# foundry-sdlc-sandbox

Synthetic PoC sandbox for the Azure AI Foundry SDLC agents proof of concept.

This repo hosts a tiny demo Python library (`parcelkit`) and GitHub Actions workflows
that drive Azure AI Foundry prompt agents through the software development lifecycle.

**Everything here is synthetic.** No real Royal Mail data or code.

---

## What this repo is

```
GitHub event (issue label / PR / CI failure / workflow_dispatch)
        |
        v
GitHub Actions job  (owner-only gate; fork PRs get no Azure access)
        |
        | OIDC login (no stored secrets)
        v
Azure Foundry prompt agent --> gpt-5-mini GlobalStandard
        |
        v
 GitHub write-back via GITHUB_TOKEN:
   comment / label / draft PR / release body update
```

The agents act on `parcelkit` (src layout under `src/parcelkit/`), a synthetic UK
postcode and tracking-number library with intentional small gaps for demo purposes.

Every agent workflow calls the agent through one composite action,
`.github/actions/call-agent` (inputs `agent`, `input-file`, `output-file`, `max-output-tokens`).
It sets up Python with a pip cache, installs the hash-pinned `requirements-agents.txt`, and
runs `python -m sdlc_agents.call` from `tools/`. Azure login stays in each workflow, because
OIDC needs `id-token: write` on the job.

`tools/sdlc_agents` is a committed copy of `agents/sdlc_agents` from the infrastructure repo.
Do not edit it here: change the infrastructure repo, run `scripts/sync-sandbox.sh` there (its
tests fail while the copy is stale), then publish `sandbox/` to this repo.

---

## One-time setup

### 1. Fork pull request approval setting (CRITICAL for public repos)

In **Settings > Actions > General > Fork pull request workflows from outside collaborators**:
- Select **Require approval for all external contributors**.

Without this, strangers can run arbitrary code on your runner via a fork PR. This is the
primary control; the job-level `if` guard in `ci.yml` is defence-in-depth.

### 2. Enable GitHub Actions write permissions

In **Settings > Actions > General > Workflow permissions**:
- Select **Read and write permissions**.
- Enable **Allow GitHub Actions to create and approve pull requests**.

Without this, `gh pr create` and `gh issue edit` will fail.

### 3. Set repository VARIABLES (not secrets)

Go to **Settings > Secrets and variables > Actions > Variables** and add:

| Variable | Example value |
|---|---|
| `AZURE_CLIENT_ID` | Client ID of the user-assigned managed identity from Terraform |
| `AZURE_TENANT_ID` | Your Azure tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Your Azure subscription ID |
| `FOUNDRY_PROJECT_ENDPOINT` | `your Foundry project endpoint (from the Terraform output foundry_project_endpoint)` |

These are **variables**, not secrets. They contain no credential material. OIDC login
exchanges the GitHub OIDC token for an Azure access token; no long-lived key is stored.

The Foundry endpoint above has local auth disabled (`local_auth_enabled = false` in
Terraform), so the URL alone cannot be used to call the API without a valid Entra token.

### 4. Fork PRs and Azure access

Fork PRs **never** get Azure access:
- On public repos, fork PRs run without `id-token: write` even when the job declares it.
- The PR-triggered agent workflows (`agent-review`, `agent-ci-triage`) also require
  `head.repo.full_name == github.repository`, blocking fork PRs at the job level.

### 5. Add branch protection ruleset

Apply a ruleset on the default branch so agents cannot push or merge:
- Require 1 approving review.
- `require_last_push_approval: true` (the bot that pushed cannot self-approve).
- `require_code_owner_review: true` (needs a CODEOWNERS file; add one pointing to the owner).
- Require the `tests` status check.
- Block deletion and non-fast-forward pushes.

### 6. Federated identity credential

The managed identity needs a federated credential for this repo. Terraform creates FICs for
the default branch, pull_request events, and optionally a named environment.

New repos (created after 2026-07-15) emit immutable OIDC subjects:
`repo:OWNER@OWNER_ID/REPO@REPO_ID:...`

Get the numeric IDs and pass them to Terraform:
```bash
gh api repos/OWNER/REPO --jq '.owner.id, .id'
```

### 7. Register Foundry prompt agents

Create the five agent versions in the Foundry project once, from a machine logged in with
`az login`: `FOUNDRY_PROJECT_ENDPOINT=<endpoint> python -m sdlc_agents.create_agents`
(run from `agents/` in the infrastructure repo). CI only calls the agents, it does not
create them.

### 8. Create the triage labels

`agent-triage` applies labels from the `sdlc-triage` allow-list in
`tools/sdlc_agents/definitions.py`. GitHub's default labels cover all but `needs-info`;
create it once: `gh label create needs-info --repo OWNER/REPO`.

---

## Demo walkthrough

### Stage 1: Issue triage

1. Open a new issue describing a bug or feature in `parcelkit`.
2. Apply the **`agent:triage`** label to the issue (owner only).
3. The `agent-triage.yml` workflow fires and calls `sdlc-triage`.
4. The agent labels the issue and posts a summary comment with acceptance criteria and
   task breakdown.

> **Note:** The workflow triggers on `labeled` only (not `opened`) to avoid double-spend
> when a label is pre-applied at issue creation.

### Stage 2: Code generation to draft PR

1. Apply the **`agent:implement`** label to a triaged issue (owner only).
2. The `agent-implement.yml` workflow fires:
   - **generate** job: calls `sdlc-codegen` with Azure OIDC; uploads the JSON artifact.
   - **publish** job: validates again with `scripts/apply_codegen.py` (allow-list enforced),
     stages exactly the paths in the manifest it writes (no `git add -A`), pushes branch
     `agent/issue-N` and opens a **draft** PR. If the generated files match `main`, it comments
     on the issue instead and pushes nothing.
3. The draft PR gets no CI or review until the owner closes and reopens it (or pushes a commit).

### Stage 3: PR review

1. When a PR is opened or pushed (same-repo, owner or `github-actions[bot]` on `agent/issue-*`
   branches), `agent-review.yml` fires.
2. `sdlc-review` receives the diff (capped at 12 KB) and returns a markdown review.
3. The review is posted as a PR comment.

### Stage 4: CI failure triage

1. If the `CI` workflow fails on a PR, `agent-ci-triage.yml` fires.
2. The PR number is looked up from the event payload **before** calling the agent. If no
   PR is found, the agent call is skipped.
3. `sdlc-ci-triage` receives the failed log (last 4 MiB fetched; the CLI keeps the first 2k
   and last 58k chars, because failure lines are at the end) and returns a root-cause analysis.
4. The analysis is posted as a PR comment.

> **Note:** CI runs on `push` to `main` only, and on `pull_request` for same-repo branches.
> This avoids double-triggering (and double triage calls) for same-repo PR branches.

### Stage 5: Release notes

1. Run the `agent-release-notes.yml` workflow via **workflow_dispatch** from the default
   branch (`--ref main`):
   ```bash
   gh workflow run agent-release-notes.yml --repo OWNER/REPO -f tag=v1.0.0
   ```
2. `sdlc-release-notes` receives merged PRs and commits since the previous tag.
3. The generated markdown changelog replaces the release body.

> **Note:** The `release:published` trigger was intentionally removed. It would produce
> an OIDC subject `ref:refs/tags/vX.Y.Z` which has no matching federated identity
> credential (the FIC is for `ref:refs/heads/main`). Use `workflow_dispatch` from main.

---

## Safety notes

- **Agents never merge.** All PRs are draft; a human must approve and merge.
- **Owner-only spend.** Every agent workflow (`agent-*.yml`) is gated on the repository owner
  (`github.actor`, the PR author, or the `workflow_run` actor; `agent-implement` also requires the
  issue author to be the owner) so strangers cannot trigger Azure spend on a public repo.
  `ci.yml` has no Azure access and is gated by the fork-approval setting instead.
- **Fork PRs excluded.** `agent-review` and `agent-ci-triage` check
  `head.repo.full_name == github.repository`. Fork PRs also get no `id-token` on public repos.
- **Untrusted text never interpolated inline.** Issue titles/bodies, PR titles, branch
  names, and log output go through `env:` into Python files, never with `${{ }}` inside
  `run:` blocks.
- **Selective git staging.** `agent-implement.yml` stages only the paths in the NUL-separated
  manifest from `apply_codegen.py`, as literal pathspecs, never `git add -A`. Git hooks and
  fsmonitor are disabled during commit.
- **Timeouts.** Every job has `timeout-minutes: 10`.
- **Output caps.** Every agent call uses `--max-output-tokens 8000` (16000 for codegen).
  Diff and log inputs are also capped before being sent.
- **Actions SHA-pinned, dependencies hash-pinned.** All third-party actions are pinned to full
  commit SHAs with the corresponding tag as a comment. The agent CLI's Python dependencies
  install from `requirements-agents.txt` with `--require-hashes`.
- **CI on agent PRs needs a human.** Events created with `GITHUB_TOKEN` do not start new workflow
  runs, so a PR opened by `agent-implement` gets no CI or agent review until the owner closes and
  reopens it (or pushes a commit). Verify once live.

---

## Running tests locally

```bash
pip install -e ".[dev]"
pytest tests/ -v
```
