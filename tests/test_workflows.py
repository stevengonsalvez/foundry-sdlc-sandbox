"""Behavioural tests for the shell/python embedded in .github/workflows/*.yml.

Each test extracts a step's `run:` script from the YAML and executes it under the same
`bash -euo pipefail` the workflows use, against throw-away git repos and stub CLIs.
"""

import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest
import yaml

WORKFLOWS = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows"
REAL_GIT = shutil.which("git")

needs_tools = pytest.mark.skipif(
    not (REAL_GIT and shutil.which("jq") and shutil.which("bash")), reason="needs git, jq, bash"
)


def _load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _run_scripts(name):
    wf = _load(name)
    return [
        (step.get("name", ""), step["run"])
        for job in wf["jobs"].values()
        for step in job["steps"]
        if "run" in step
    ]


def _step_script(workflow, step_name):
    matches = [s for n, s in _run_scripts(workflow) if n == step_name]
    assert len(matches) == 1, f"{step_name!r} in {workflow}: {len(matches)} matches"
    return matches[0]


@pytest.fixture
def bin_dir(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    (d / "python").symlink_to(sys.executable)
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


def _git(cwd, *args):
    out = subprocess.run(
        [REAL_GIT, "-c", "commit.gpgsign=false", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
    )
    return out.stdout.strip()


@pytest.mark.parametrize("name", sorted(p.name for p in WORKFLOWS.glob("*.yml")))
def test_every_workflow_is_valid_yaml_with_jobs(name):
    assert _load(name)["jobs"]


@pytest.mark.parametrize("name", sorted(p.name for p in WORKFLOWS.glob("*.yml")))
def test_no_run_step_pipes_a_producer_into_head(name):
    # `producer | head -c N` exits 141 (SIGPIPE) under pipefail once output exceeds N.
    for step_name, script in _run_scripts(name):
        assert not re.search(r"\|\s*(?:\\\n\s*)?head\b", script), f"{name}: {step_name}"


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
        {"ISSUE_TITLE": "t", "ISSUE_BODY": "b", "ISSUE_NUMBER": "7", "RUNNER_TEMP": str(temp)},
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


@pytest.fixture
def push_env(tmp_path, bin_dir):
    """origin (bare) + a work clone with one staged-and-listed file, ready for the push step."""
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
    (work / "written_paths.txt").write_text("src/a.py\n")
    _git(work, "config", "commit.gpgsign", "false")
    script = _step_script("agent-implement.yml", "Commit and push branch")
    return origin, work, script, bin_dir, tmp_path


@needs_tools
def test_push_creates_a_new_branch_with_a_plain_push(push_env):
    origin, work, script, bin_dir, _ = push_env

    r = _run_step(script, work, {"ISSUE_NUMBER": "5"}, bin_dir)

    assert r.returncode == 0, r.stderr
    assert _git(origin, "rev-parse", "agent/issue-5") == _git(work, "rev-parse", "HEAD")


@needs_tools
def test_push_overwrites_an_existing_agent_branch_when_the_lease_holds(push_env):
    origin, work, script, bin_dir, tmp_path = push_env
    stale = tmp_path / "stale"
    _git(tmp_path, "clone", "-q", str(origin), str(stale))
    _git(stale, "checkout", "-q", "-b", "agent/issue-5")
    (stale / "old.txt").write_text("old\n")
    _git(stale, "add", "old.txt")
    _git(stale, "commit", "-q", "-m", "old run")
    _git(stale, "push", "-q", "origin", "agent/issue-5")

    r = _run_step(script, work, {"ISSUE_NUMBER": "5"}, bin_dir)

    assert r.returncode == 0, r.stderr
    assert _git(origin, "rev-parse", "agent/issue-5") == _git(work, "rev-parse", "HEAD")


@needs_tools
def test_push_fails_and_keeps_the_remote_when_the_branch_moves_after_the_lease_is_read(push_env):
    origin, work, script, bin_dir, tmp_path = push_env
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

    r = _run_step(
        script, work,
        {"ISSUE_NUMBER": "5", "RACER": str(racer), "RACE_DONE": str(tmp_path / "race.done")},
        bin_dir,
    )

    assert r.returncode != 0
    assert "refusing to overwrite" in r.stderr
    assert _git(origin, "rev-parse", "agent/issue-5") == racer_tip


def test_push_step_never_uses_a_bare_force():
    script = _step_script("agent-implement.yml", "Commit and push branch")
    code = "\n".join(l for l in script.splitlines() if not l.lstrip().startswith("#"))
    assert not re.search(r"--force(?!-with-lease)", code)
    assert "--force-with-lease=" in code
