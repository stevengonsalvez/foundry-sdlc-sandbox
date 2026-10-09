"""Behavioural tests for the shell/python embedded in .github/workflows/*.yml.

Each test extracts a step's `run:` script from the YAML and executes it under the same
`bash -euo pipefail` the workflows use, against throw-away git repos and stub CLIs.
Structural tests check that every agent workflow goes through the call-agent action.
"""

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest
import yaml

from sdlc_agents.definitions import AGENT_BY_NAME

SANDBOX = pathlib.Path(__file__).resolve().parent.parent
WORKFLOWS = SANDBOX / ".github" / "workflows"
CALL_AGENT = SANDBOX / ".github" / "actions" / "call-agent" / "action.yml"
TOOLS = SANDBOX / "tools"
AGENT_WORKFLOWS = sorted(p.name for p in WORKFLOWS.glob("agent-*.yml"))
REAL_GIT = shutil.which("git")

needs_tools = pytest.mark.skipif(
    not (REAL_GIT and shutil.which("jq") and shutil.which("bash")), reason="needs git, jq, bash"
)


def _load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _steps(name):
    return [step for job in _load(name)["jobs"].values() for step in job["steps"]]


def _run_scripts(name):
    return [(step.get("name", ""), step["run"]) for step in _steps(name) if "run" in step]


def _step(workflow, step_name):
    matches = [s for s in _steps(workflow) if s.get("name") == step_name]
    assert len(matches) == 1, f"{step_name!r} in {workflow}: {len(matches)} matches"
    return matches[0]


def _step_script(workflow, step_name):
    matches = [s for n, s in _run_scripts(workflow) if n == step_name]
    assert len(matches) == 1, f"{step_name!r} in {workflow}: {len(matches)} matches"
    return matches[0]


@pytest.fixture
def bin_dir(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    (d / "python").symlink_to(sys.executable)
    (d / "python3").symlink_to(sys.executable)
    return d


def _run_step(script, cwd, env, bin_dir):
    full_env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(cwd),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        **env,
    }
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=cwd, env=full_env, capture_output=True, text=True, timeout=60,
    )


def _git(cwd, *args, env=None):
    out = subprocess.run(
        [REAL_GIT, "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
         "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
             **(env or {})},
    )
    return out.stdout.strip()


def _stub(bin_dir, name, body):
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


@pytest.mark.parametrize("name", sorted(p.name for p in WORKFLOWS.glob("*.yml")))
def test_every_workflow_is_valid_yaml_with_jobs(name):
    assert _load(name)["jobs"]


@pytest.mark.parametrize("name", sorted(p.name for p in WORKFLOWS.glob("*.yml")))
def test_no_run_step_pipes_a_producer_into_head(name):
    # `producer | head -c N` exits 141 (SIGPIPE) under pipefail once output exceeds N.
    for step_name, script in _run_scripts(name):
        assert not re.search(r"\|\s*(?:\\\n\s*)?head\b", script), f"{name}: {step_name}"


# ---------------------------------------------------------------------------
# call-agent composite action
# ---------------------------------------------------------------------------

def _action():
    return yaml.safe_load(CALL_AGENT.read_text(encoding="utf-8"))


def test_there_are_five_agent_workflows():
    assert len(AGENT_WORKFLOWS) == 5


@pytest.mark.parametrize("name", AGENT_WORKFLOWS)
def test_agent_workflow_calls_the_agent_only_through_the_composite_action(name):
    calls = [s for s in _steps(name) if s.get("uses") == "./.github/actions/call-agent"]
    assert len(calls) == 1, name
    call = calls[0]
    assert call["with"]["agent"] in AGENT_BY_NAME
    assert call["env"] == {"FOUNDRY_PROJECT_ENDPOINT": "${{ vars.FOUNDRY_PROJECT_ENDPOINT }}"}
    assert set(call["with"]) <= set(_action()["inputs"])
    for step_name, script in _run_scripts(name):
        assert "-m sdlc_agents.call" not in script, f"{name}: {step_name}"


@pytest.mark.parametrize("name", sorted(p.name for p in WORKFLOWS.glob("*.yml")))
def test_no_workflow_pip_installs_the_sdk(name):
    for step_name, script in _run_scripts(name):
        assert not re.search(r"pip\b.*\binstall\b.*azure", script), f"{name}: {step_name}"


@pytest.mark.parametrize("name", AGENT_WORKFLOWS)
def test_azure_login_comes_before_the_agent_call_with_one_pinned_sha(name):
    uses = [s.get("uses", "") for s in _steps(name)]
    logins = [u for u in uses if u.startswith("azure/login@")]
    assert logins == ["azure/login@a641126d1b8aa4d1fa005f4f92df94a3a4c4c906"], name
    assert uses.index(logins[0]) < uses.index("./.github/actions/call-agent")


def test_action_installs_only_the_hash_pinned_requirements_then_calls_the_cli():
    steps = _action()["runs"]["steps"]
    setup = steps[0]
    assert setup["uses"].startswith("actions/setup-python@")
    assert re.fullmatch(r"actions/setup-python@[0-9a-f]{40}", setup["uses"])
    assert setup["with"]["cache"] == "pip"
    runs = [s["run"] for s in steps if "run" in s]
    install = [r for r in runs if "pip" in r]
    assert install == ["python -m pip install --require-hashes --no-deps -r requirements-agents.txt"]
    call = steps[-1]
    assert "python -m sdlc_agents.call" in call["run"]
    assert call["env"]["PYTHONPATH"] == "${{ github.workspace }}/tools"


def test_requirements_are_all_pinned_with_hashes():
    text = (SANDBOX / "requirements-agents.txt").read_text(encoding="utf-8")
    entries = re.split(r"\n(?=[a-z0-9])", text.split("\n", 2)[2].strip())
    names = set()
    for entry in entries:
        name, _, rest = entry.partition("==")
        assert rest, f"not pinned: {entry.splitlines()[0]}"
        assert "--hash=sha256:" in rest, f"no hash: {name}"
        names.add(name)
    assert {"azure-ai-projects", "azure-identity", "openai"} <= names


def test_composite_action_steps_run_under_strict_bash():
    for step in _action()["runs"]["steps"]:
        if "run" in step:
            assert step["shell"] == "bash -euo pipefail {0}"


@needs_tools
def test_review_diff_step_survives_a_diff_larger_than_the_cap(tmp_path, bin_dir):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "f.txt").write_text("base\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "f.txt").write_text("".join(f"line {i} " + "x" * 80 + "\n" for i in range(2000)))
    _git(repo, "commit", "-q", "-am", "big")
    head = _git(repo, "rev-parse", "HEAD")
    temp = tmp_path / "tmp"
    temp.mkdir()

    r = _run_step(
        _step_script("agent-review.yml", "Collect diff (capped at 12 KB)"),
        repo, {"BASE_SHA": base, "HEAD_SHA": head, "RUNNER_TEMP": str(temp)}, bin_dir,
    )

    assert r.returncode == 0, r.stderr
    assert (temp / "pr_diff.txt").stat().st_size == 12288
    assert (temp / "pr_diff_full.txt").stat().st_size > 12288


@needs_tools
def test_release_notes_step_survives_commit_log_larger_than_the_cap(tmp_path, bin_dir):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "first")
    _git(repo, "tag", "v1.0")
    for i in range(120):
        _git(repo, "commit", "-q", "--allow-empty", "-m", f"commit {i} " + "y" * 60)
    _git(repo, "tag", "v1.1")
    gh = bin_dir / "gh"
    gh.write_text("#!/bin/sh\necho '[]'\n")
    gh.chmod(0o755)
    temp = tmp_path / "tmp"
    temp.mkdir()

    r = _run_step(
        _step_script("agent-release-notes.yml", "Collect merged PRs since previous tag"),
        repo,
        {"CURRENT_TAG": "v1.1", "RUNNER_TEMP": str(temp), "GITHUB_REPOSITORY": "o/r", "GH_TOKEN": "x"},
        bin_dir,
    )

    assert r.returncode == 0, r.stderr
    context = (temp / "release_context.txt").read_text()
    assert "Range: v1.0..v1.1" in r.stdout
    assert "commit 119" in context  # newest commit is first in the log, so it survives the cap
    assert (temp / "range_commits_full.txt").stat().st_size > 4096


def _build_prompt(tmp_path, bin_dir, big_files):
    """Run the implement workflow's 'Build prompt file' step over a synthetic repo."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    for rel, size in big_files.items():
        (repo / rel).write_text("# " + "z" * size + "\n")
    temp = tmp_path / "tmp"
    temp.mkdir()
    r = _run_step(
        _step_script("agent-implement.yml", "Build prompt file"),
        repo,
        {"ISSUE_TITLE": "t", "ISSUE_BODY": "b", "ISSUE_NUMBER": "7", "RUNNER_TEMP": str(temp),
         "PYTHONPATH": str(TOOLS)},
        bin_dir,
    )
    assert r.returncode == 0, r.stderr
    return (temp / "prompt.txt").read_text()


def test_implement_prompt_lists_files_skipped_for_the_size_cap(tmp_path, bin_dir):
    prompt = _build_prompt(tmp_path, bin_dir, {"src/a.py": 30_000, "src/b.py": 30_000, "tests/test_c.py": 100})

    assert "--- FILE src/a.py ---" in prompt
    assert "--- FILE src/b.py ---" not in prompt
    assert "Files NOT shown (do not modify them): src/b.py" in prompt


def test_implement_prompt_has_no_omitted_line_when_everything_fits(tmp_path, bin_dir):
    prompt = _build_prompt(tmp_path, bin_dir, {"src/a.py": 100})

    assert "Files NOT shown" not in prompt


def test_implement_prompt_takes_allowed_roots_from_the_policy_module(tmp_path, bin_dir):
    from sdlc_agents.codegen_policy import allowed_roots_text

    prompt = _build_prompt(tmp_path, bin_dir, {"src/a.py": 100})

    assert f"Only paths under {allowed_roots_text()} are allowed." in prompt
    assert "src/, tests/" not in _step_script("agent-implement.yml", "Build prompt file")


@pytest.fixture
def push_env(tmp_path, bin_dir):
    """origin (bare) + a work clone with one applied file listed in the manifest."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "checkout", "-q", "-b", "main")
    (work / "README").write_text("r\n")
    _git(work, "add", "README")
    _git(work, "commit", "-q", "-m", "init")
    _git(work, "push", "-q", "origin", "main")
    (work / "src").mkdir()
    (work / "src" / "a.py").write_text("x = 1\n")
    temp = tmp_path / "runner_temp"
    temp.mkdir()
    (temp / "written_paths.nul").write_bytes(b"src/a.py\0")
    _git(work, "config", "commit.gpgsign", "false")
    env = {"ISSUE_NUMBER": "5", "RUNNER_TEMP": str(temp), "GITHUB_OUTPUT": str(temp / "out")}
    return origin, work, env, bin_dir, tmp_path


def _stage_and_push(work, env, bin_dir):
    staged = _run_step(_step_script("agent-implement.yml", "Stage and commit"), work, env, bin_dir)
    assert staged.returncode == 0, staged.stderr
    return _run_step(_step_script("agent-implement.yml", "Push branch"), work, env, bin_dir)


def _outputs(env):
    return dict(line.split("=", 1) for line in pathlib.Path(env["GITHUB_OUTPUT"]).read_text().split())


@needs_tools
def test_stage_commits_exactly_the_manifest_paths_as_literal_pathspecs(push_env):
    _, work, env, bin_dir, _ = push_env
    (work / "src" / "a*.py").write_text("literal\n")
    (work / "src" / "ab.py").write_text("must not be staged by a glob\n")
    (work / "stray.txt").write_text("not in the manifest\n")
    pathlib.Path(env["RUNNER_TEMP"], "written_paths.nul").write_bytes(b"src/a.py\0src/a*.py\0")

    r = _run_step(_step_script("agent-implement.yml", "Stage and commit"), work, env, bin_dir)

    assert r.returncode == 0, r.stderr
    assert _outputs(env) == {"changed": "true"}
    committed = _git(work, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert sorted(committed) == ["src/a*.py", "src/a.py"]
    assert _git(work, "rev-parse", "--abbrev-ref", "HEAD") == "agent/issue-5"


@needs_tools
def test_stage_reports_no_change_when_content_is_identical_to_main(push_env):
    _, work, env, bin_dir, _ = push_env
    pathlib.Path(env["RUNNER_TEMP"], "written_paths.nul").write_bytes(b"README\0")
    main_tip = _git(work, "rev-parse", "HEAD")

    r = _run_step(_step_script("agent-implement.yml", "Stage and commit"), work, env, bin_dir)

    assert r.returncode == 0, r.stderr
    assert _outputs(env) == {"changed": "false"}
    assert _git(work, "rev-parse", "HEAD") == main_tip


def test_publish_pushes_and_opens_a_pr_only_when_something_changed():
    for step_name in ("Push branch", "Open or update draft PR"):
        assert _step("agent-implement.yml", step_name)["if"] == "steps.commit.outputs.changed == 'true'"
    jobs = _load("agent-implement.yml")["jobs"]
    assert jobs["publish"]["outputs"] == {"changed": "${{ steps.commit.outputs.changed }}"}
    assert jobs["publish"]["permissions"]["issues"] == "read"


def test_noop_is_reported_by_a_job_that_can_only_write_issues():
    job = _load("agent-implement.yml")["jobs"]["report-noop"]
    assert job["needs"] == "publish"
    assert job["if"] == "needs.publish.outputs.changed == 'false'"
    assert job["permissions"] == {"issues": "write"}
    assert [s for s in job["steps"] if "uses" in s] == []  # no checkout, no code
    script = job["steps"][0]["run"]
    assert "gh issue comment" in script
    assert "git " not in script and "gh pr" not in script


def test_publish_stages_from_the_manifest_not_from_log_lines():
    apply = _step_script("agent-implement.yml", "Validate and apply files (re-validates in publish job)")
    stage = _step_script("agent-implement.yml", "Stage and commit")
    assert '"$RUNNER_TEMP/written_paths.nul"' in apply
    assert "--pathspec-from-file=\"$RUNNER_TEMP/written_paths.nul\" --pathspec-file-nul" in stage
    assert "grep" not in apply + stage


@needs_tools
def test_push_creates_a_new_branch_with_a_plain_push(push_env):
    origin, work, env, bin_dir, _ = push_env

    r = _stage_and_push(work, env, bin_dir)

    assert r.returncode == 0, r.stderr
    assert _git(origin, "rev-parse", "agent/issue-5") == _git(work, "rev-parse", "HEAD")


@needs_tools
def test_push_overwrites_an_existing_agent_branch_when_the_lease_holds(push_env):
    origin, work, env, bin_dir, tmp_path = push_env
    stale = tmp_path / "stale"
    _git(tmp_path, "clone", "-q", str(origin), str(stale))
    _git(stale, "checkout", "-q", "-b", "agent/issue-5")
    (stale / "old.txt").write_text("old\n")
    _git(stale, "add", "old.txt")
    _git(stale, "commit", "-q", "-m", "old run")
    _git(stale, "push", "-q", "origin", "agent/issue-5")

    r = _stage_and_push(work, env, bin_dir)

    assert r.returncode == 0, r.stderr
    assert _git(origin, "rev-parse", "agent/issue-5") == _git(work, "rev-parse", "HEAD")


@needs_tools
def test_push_fails_and_keeps_the_remote_when_the_branch_moves_after_the_lease_is_read(push_env):
    origin, work, env, bin_dir, tmp_path = push_env
    racer = tmp_path / "racer"
    _git(tmp_path, "clone", "-q", str(origin), str(racer))
    _git(racer, "checkout", "-q", "-b", "agent/issue-5")
    (racer / "old.txt").write_text("old\n")
    _git(racer, "add", "old.txt")
    _git(racer, "commit", "-q", "-m", "old run")
    _git(racer, "push", "-q", "origin", "agent/issue-5")
    # A `git` shim that lets a competing push land between the lease read and our push.
    (racer / "new.txt").write_text("someone else\n")
    _git(racer, "add", "new.txt")
    _git(racer, "commit", "-q", "-m", "concurrent human work")
    shim = bin_dir / "git"
    shim.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = push ] && [ ! -e "$RACE_DONE" ]; then\n'
        '  touch "$RACE_DONE"\n'
        f'  {REAL_GIT} -C "$RACER" push -q origin HEAD:refs/heads/agent/issue-5 --force\n'
        "fi\n"
        f'exec {REAL_GIT} "$@"\n'
    )
    shim.chmod(0o755)
    # Point origin back at the earlier tip so the step reads the pre-race value.
    pre_race = _git(racer, "rev-parse", "HEAD~1")
    _git(origin, "update-ref", "refs/heads/agent/issue-5", pre_race)
    racer_tip = _git(racer, "rev-parse", "HEAD")

    r = _stage_and_push(
        work, {**env, "RACER": str(racer), "RACE_DONE": str(tmp_path / "race.done")}, bin_dir
    )

    assert r.returncode != 0
    assert "refusing to overwrite" in r.stderr
    assert _git(origin, "rev-parse", "agent/issue-5") == racer_tip


def test_push_step_never_uses_a_bare_force():
    script = _step_script("agent-implement.yml", "Push branch")
    code = "\n".join(l for l in script.splitlines() if not l.lstrip().startswith("#"))
    assert not re.search(r"--force(?!-with-lease)", code)
    assert "--force-with-lease=" in code


# ---------------------------------------------------------------------------
# Release notes: UTC date window, failures are not hidden
# ---------------------------------------------------------------------------

@pytest.fixture
def release_repo(tmp_path, bin_dir):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    at = lambda when: {"GIT_COMMITTER_DATE": when, "GIT_AUTHOR_DATE": "2025-06-01T00:00:00Z"}
    _git(repo, "commit", "-q", "--allow-empty", "-m", "first", env=at("2026-01-01T09:00:00+01:00"))
    _git(repo, "tag", "v1.0")
    # 12:00 at +01:00 is 11:00Z. The author date is far older: the window uses committer dates.
    _git(repo, "commit", "-q", "--allow-empty", "-m", "second", env=at("2026-01-10T12:00:00+01:00"))
    _git(repo, "tag", "-a", "v1.1", "-m", "v1.1", env=at("2026-01-10T12:00:00+01:00"))
    temp = tmp_path / "tmp"
    temp.mkdir()
    env = {"CURRENT_TAG": "v1.1", "RUNNER_TEMP": str(temp), "GITHUB_REPOSITORY": "o/r", "GH_TOKEN": "x"}
    return repo, temp, env


def _collect(repo, env, bin_dir):
    script = _step_script("agent-release-notes.yml", "Collect merged PRs since previous tag")
    return _run_step(script, repo, env, bin_dir)


@needs_tools
def test_release_window_compares_offset_tag_dates_with_utc_merged_at(release_repo, bin_dir, tmp_path):
    repo, temp, env = release_repo
    prs = tmp_path / "prs.json"
    prs.write_text(json.dumps([
        # Lexically "2026-01-10T11:30:00Z" < "2026-01-10T12:00:00+01:00", but it is 30 min AFTER the tag.
        {"number": 1, "title": "after the tag", "mergedAt": "2026-01-10T11:30:00Z"},
        {"number": 2, "title": "inside the window", "mergedAt": "2026-01-10T10:30:00Z"},
        {"number": 3, "title": "before the previous tag", "mergedAt": "2026-01-01T07:59:59Z"},
        {"number": 4, "title": "exactly at the tag", "mergedAt": "2026-01-10T11:00:00Z"},
        # Exactly at the previous tag (09:00 at +01:00 is 08:00Z): that release already listed it.
        {"number": 5, "title": "at the previous tag", "mergedAt": "2026-01-01T08:00:00Z"},
    ]))
    _stub(bin_dir, "gh", 'cat "$GH_PRS"\n')

    r = _collect(repo, {**env, "GH_PRS": str(prs)}, bin_dir)

    assert r.returncode == 0, r.stderr
    assert "Range: v1.0..v1.1" in r.stdout
    assert (temp / "pr_list.txt").read_text().splitlines() == [
        "- PR #2: inside the window",
        "- PR #4: exactly at the tag",
    ]


@needs_tools
def test_release_step_fails_when_gh_fails(release_repo, bin_dir):
    repo, temp, env = release_repo
    _stub(bin_dir, "gh", "echo 'HTTP 502' >&2\nexit 1\n")

    r = _collect(repo, env, bin_dir)

    assert r.returncode != 0
    assert "HTTP 502" in r.stderr
    assert not (temp / "release_context.txt").exists()


@needs_tools
def test_release_step_fails_for_an_unknown_tag(release_repo, bin_dir):
    repo, _, env = release_repo
    _stub(bin_dir, "gh", "echo '[]'\n")

    r = _collect(repo, {**env, "CURRENT_TAG": "v9.9"}, bin_dir)

    assert r.returncode != 0
    assert "tag v9.9 does not exist" in r.stderr


@needs_tools
def test_first_release_window_starts_at_the_root_commit(tmp_path, bin_dir):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "root")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "feature")
    _git(repo, "tag", "v0.1")
    temp = tmp_path / "tmp"
    temp.mkdir()
    _stub(bin_dir, "gh", "echo '[]'\n")

    r = _collect(repo, {"CURRENT_TAG": "v0.1", "RUNNER_TEMP": str(temp),
                        "GITHUB_REPOSITORY": "o/r", "GH_TOKEN": "x"}, bin_dir)

    assert r.returncode == 0, r.stderr
    assert f"Range: {_git(repo, 'rev-list', '--max-parents=0', 'HEAD')}..v0.1" in r.stdout
    assert "feature" in (temp / "release_context.txt").read_text()


def test_release_step_does_not_swallow_errors():
    script = _step_script("agent-release-notes.yml", "Collect merged PRs since previous tag")
    assert "2>/dev/null" not in script
    assert "|| echo" not in script
    assert "%aI" not in script


# ---------------------------------------------------------------------------
# Triage: labels come from the agent definition
# ---------------------------------------------------------------------------

@needs_tools
def test_triage_applies_only_labels_on_the_agent_definition(tmp_path, bin_dir):
    temp = tmp_path / "tmp"
    temp.mkdir()
    allowed = AGENT_BY_NAME["sdlc-triage"].allowed_labels
    (temp / "triage_output.json").write_text(json.dumps({
        "labels": ["good first issue", "agent:implement", "needs-info"],
        "summary": "s", "acceptance_criteria": ["a"], "tasks": ["t"],
    }))
    calls = tmp_path / "gh_calls.txt"
    _stub(bin_dir, "gh", f'printf "%s|" "$@" >> "{calls}"; echo >> "{calls}"; cat > /dev/null\n')

    r = _run_step(
        _step_script("agent-triage.yml", "Validate and apply triage output"), tmp_path,
        {"RUNNER_TEMP": str(temp), "ISSUE_NUMBER": "3", "GITHUB_REPOSITORY": "o/r",
         "GH_TOKEN": "x", "PYTHONPATH": str(TOOLS)},
        bin_dir,
    )

    assert r.returncode == 0, r.stderr
    added = [line.split("|")[4] for line in calls.read_text().splitlines() if "--add-label" in line]
    assert added == ["good first issue", "needs-info"]
    assert set(added) <= set(allowed)
    assert "bug" not in _step_script("agent-triage.yml", "Validate and apply triage output")


@needs_tools
def test_first_release_with_several_root_commits_still_gets_a_window(tmp_path, bin_dir):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "root a")
    _git(repo, "checkout", "-q", "--orphan", "other")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "root b")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--allow-unrelated-histories", "-m", "join", "other")
    _git(repo, "tag", "v0.1")
    temp = tmp_path / "tmp"
    temp.mkdir()
    _stub(bin_dir, "gh", "echo '[]'\n")

    r = _collect(repo, {"CURRENT_TAG": "v0.1", "RUNNER_TEMP": str(temp),
                        "GITHUB_REPOSITORY": "o/r", "GH_TOKEN": "x"}, bin_dir)

    assert r.returncode == 0, r.stderr
    assert "join" in (temp / "release_context.txt").read_text()


@needs_tools
def test_triage_posts_the_comment_and_fails_the_job_when_a_label_cannot_be_applied(tmp_path, bin_dir):
    temp = tmp_path / "tmp"
    temp.mkdir()
    (temp / "triage_output.json").write_text(json.dumps({
        "labels": ["needs-info", "bug"], "summary": "s", "acceptance_criteria": ["a"], "tasks": ["t"],
    }))
    calls = tmp_path / "gh_calls.txt"
    _stub(bin_dir, "gh", (
        f'echo "$*" >> "{calls}"\n'
        'case "$*" in *"--add-label needs-info"*) echo "label not found" >&2; exit 1;; esac\n'
        'cat > /dev/null 2>&1 || true\n'
    ))

    r = _run_step(
        _step_script("agent-triage.yml", "Validate and apply triage output"), tmp_path,
        {"RUNNER_TEMP": str(temp), "ISSUE_NUMBER": "3", "GITHUB_REPOSITORY": "o/r",
         "GH_TOKEN": "x", "PYTHONPATH": str(TOOLS)},
        bin_dir,
    )

    lines = calls.read_text().splitlines()
    assert r.returncode != 0
    assert "could not apply label: needs-info" in r.stdout
    assert lines[0].startswith("issue comment")  # comment is posted before any label
    assert any("--add-label bug" in line for line in lines)  # later labels still tried
