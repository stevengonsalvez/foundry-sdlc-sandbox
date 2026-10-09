"""Tests for scripts/apply_codegen.py."""

import json
import os
import sys
import tempfile

import pytest

# Allow importing from scripts/ without a package install
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from apply_codegen import load_and_validate, apply_files, main, ALLOWED_ROOTS  # noqa: E402


def _write_json(tmp_path, data):
    p = tmp_path / "codegen.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


class TestLoadAndValidate:
    """Security-focused path validation tests."""

    # --- happy paths ---

    def test_valid_src_path(self, tmp_path):
        data = {"explanation": "ok", "files": [{"path": "src/foo.py", "content": "x=1\n"}]}
        files = load_and_validate(_write_json(tmp_path, data), str(tmp_path))
        assert len(files) == 1
        assert files[0]["norm_path"] == "src/foo.py"

    def test_valid_tests_path(self, tmp_path):
        data = {"files": [{"path": "tests/test_bar.py", "content": ""}]}
        files = load_and_validate(_write_json(tmp_path, data), str(tmp_path))
        assert files[0]["norm_path"] == "tests/test_bar.py"

    def test_valid_docs_path(self, tmp_path):
        data = {"files": [{"path": "docs/guide.md", "content": "# Guide"}]}
        files = load_and_validate(_write_json(tmp_path, data), str(tmp_path))
        assert files[0]["norm_path"] == "docs/guide.md"

    def test_exactly_20_files_allowed(self, tmp_path):
        data = {"files": [{"path": f"src/f{i}.py", "content": ""} for i in range(20)]}
        files = load_and_validate(_write_json(tmp_path, data), str(tmp_path))
        assert len(files) == 20

    def test_exact_200kb_allowed(self, tmp_path):
        exact = "x" * (200 * 1024)
        data = {"files": [{"path": "src/big.txt", "content": exact}]}
        files = load_and_validate(_write_json(tmp_path, data), str(tmp_path))
        assert len(files) == 1

    # --- file limit ---

    def test_too_many_files(self, tmp_path):
        data = {"files": [{"path": f"src/f{i}.py", "content": ""} for i in range(21)]}
        with pytest.raises(ValueError, match="Too many files"):
            load_and_validate(_write_json(tmp_path, data), str(tmp_path))

    def test_file_too_large(self, tmp_path):
        big = "x" * (200 * 1024 + 1)
        data = {"files": [{"path": "src/big.txt", "content": big}]}
        with pytest.raises(ValueError, match="exceeds limit"):
            load_and_validate(_write_json(tmp_path, data), str(tmp_path))

    # --- allow-list rejections ---

    def test_top_level_readme_rejected(self, tmp_path):
        """README.md is not under src/tests/docs/ so it must be rejected."""
        data = {"files": [{"path": "README.md", "content": "bad"}]}
        with pytest.raises(ValueError, match="allowed root"):
            load_and_validate(_write_json(tmp_path, data), str(tmp_path))

    def test_scripts_dir_rejected(self, tmp_path):
        data = {"files": [{"path": "scripts/evil.py", "content": ""}]}
        with pytest.raises(ValueError, match="allowed root"):
            load_and_validate(_write_json(tmp_path, data), str(tmp_path))

    def test_dotgithub_rejected(self, tmp_path):
        data = {"files": [{"path": ".github/workflows/evil.yml", "content": ""}]}
        with pytest.raises(ValueError, match="forbidden segment"):
            load_and_validate(_write_json(tmp_path, data), str(tmp_path))

    # --- .git/ blocking ---

    def test_git_config_rejected(self, tmp_path):
        data = {"files": [{"path": ".git/config", "content": "[core]\nfsmonitor=evil"}]}
        with pytest.raises(ValueError, match="forbidden segment"):
            load_and_validate(_write_json(tmp_path, data), str(tmp_path))

    def test_git_hooks_rejected(self, tmp_path):
        data = {"files": [{"path": ".git/hooks/pre-commit", "content": "#!/bin/sh\nevil"}]}
        with pytest.raises(ValueError, match="forbidden segment"):
            load_and_validate(_write_json(tmp_path, data), str(tmp_path))

    def test_src_then_git_config_traversal(self, tmp_path):
        """src/../.git/config must be rejected."""
        data = {"files": [{"path": "src/../.git/config", "content": "bad"}]}
        with pytest.raises(ValueError, match="forbidden segment"):
            load_and_validate(_write_json(tmp_path, data), str(tmp_path))

    # --- path traversal ---

    def test_absolute_path_rejected(self, tmp_path):
        data = {"files": [{"path": "/etc/passwd", "content": ""}]}
        with pytest.raises(ValueError, match="Absolute path"):
            load_and_validate(_write_json(tmp_path, data), str(tmp_path))

    def test_traversal_rejected(self, tmp_path):
        data = {"files": [{"path": "../outside.py", "content": ""}]}
        with pytest.raises(ValueError, match="forbidden segment"):
            load_and_validate(_write_json(tmp_path, data), str(tmp_path))

    def test_nested_traversal_detected(self, tmp_path):
        data = {"files": [{"path": "src/a/../../etc/passwd", "content": ""}]}
        with pytest.raises(ValueError, match="forbidden segment|allowed root"):
            load_and_validate(_write_json(tmp_path, data), str(tmp_path))

    # --- symlink escape ---

    def test_symlink_escape_rejected(self, tmp_path):
        """A symlink inside src/ pointing outside the root must be rejected."""
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        outside = tmp_path.parent / "outside_file.txt"
        outside.write_text("secret")
        link = src_dir / "link.txt"
        link.symlink_to(outside)
        # Writing to src/link.txt would follow the symlink to outside.
        # load_and_validate uses realpath containment check.
        data = {"files": [{"path": "src/link.txt", "content": "overwrite"}]}
        with pytest.raises(ValueError, match="escapes the repository root|symlink"):
            load_and_validate(_write_json(tmp_path, data), str(tmp_path))

    # --- misc rejections ---

    def test_nul_byte_rejected(self, tmp_path):
        data = {"files": [{"path": "src/foo\x00bar.py", "content": ""}]}
        with pytest.raises(ValueError, match="NUL byte"):
            load_and_validate(_write_json(tmp_path, data), str(tmp_path))

    def test_trailing_slash_rejected(self, tmp_path):
        data = {"files": [{"path": "src/", "content": ""}]}
        with pytest.raises(ValueError, match="trailing slash|directory"):
            load_and_validate(_write_json(tmp_path, data), str(tmp_path))

    def test_duplicate_paths_rejected(self, tmp_path):
        data = {"files": [
            {"path": "src/foo.py", "content": "a"},
            {"path": "src/foo.py", "content": "b"},
        ]}
        with pytest.raises(ValueError, match="Duplicate path"):
            load_and_validate(_write_json(tmp_path, data), str(tmp_path))

    def test_no_files_key(self, tmp_path):
        with pytest.raises(ValueError, match="'files'"):
            load_and_validate(_write_json(tmp_path, {"explanation": "ok"}), str(tmp_path))

    def test_invalid_json(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("not json", encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid JSON"):
            load_and_validate(str(p), str(tmp_path))


class TestApplyFiles:
    def test_writes_files(self, tmp_path):
        (tmp_path / "src").mkdir()
        files = [{"path": "src/hello.py", "norm_path": "src/hello.py", "content": "print('hi')\n"}]
        apply_files(files, root=str(tmp_path))
        assert (tmp_path / "src" / "hello.py").read_text() == "print('hi')\n"

    def test_overwrites_existing(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "existing.py").write_text("old")
        files = [{"path": "src/existing.py", "norm_path": "src/existing.py", "content": "new"}]
        apply_files(files, root=str(tmp_path))
        assert (tmp_path / "src" / "existing.py").read_text() == "new"

    def test_returns_written_paths(self, tmp_path):
        (tmp_path / "tests").mkdir()
        files = [{"path": "tests/t.py", "norm_path": "tests/t.py", "content": ""}]
        written = apply_files(files, root=str(tmp_path))
        assert written == ["tests/t.py"]

    def test_refuses_symlink_target(self, tmp_path):
        """apply_files must not follow a symlink (O_NOFOLLOW)."""
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        outside = tmp_path.parent / "secret.txt"
        outside.write_text("secret")
        link = src_dir / "link.txt"
        link.symlink_to(outside)
        files = [{"path": "src/link.txt", "norm_path": "src/link.txt", "content": "overwrite"}]
        # Pre-flight symlink check (ValueError) fires before O_NOFOLLOW (OSError) is reached.
        with pytest.raises((ValueError, OSError)):
            apply_files(files, root=str(tmp_path))
        assert (tmp_path.parent / "secret.txt").read_text() == "secret"

    def test_no_partial_write_when_a_later_path_is_a_symlink(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "link.txt").symlink_to(tmp_path / "elsewhere")
        files = [
            {"path": "src/first.py", "norm_path": "src/first.py", "content": "x"},
            {"path": "src/link.txt", "norm_path": "src/link.txt", "content": "y"},
        ]
        with pytest.raises(ValueError):
            apply_files(files, root=str(tmp_path))
        assert not (tmp_path / "src" / "first.py").exists()


class TestMain:
    def test_no_args_returns_1(self, capsys):
        assert main([]) == 1
        assert "Usage" in capsys.readouterr().err

    def test_missing_file_returns_1(self, capsys):
        assert main(["/nonexistent/codegen.json"]) == 1

    def test_invalid_json_returns_1(self, tmp_path, capsys):
        p = tmp_path / "bad.json"
        p.write_text("not json")
        assert main([str(p)]) == 1
        assert "ERROR" in capsys.readouterr().err

    def test_rejected_path_returns_1(self, tmp_path, capsys):
        data = {"files": [{"path": ".git/config", "content": "bad"}]}
        p = tmp_path / "codegen.json"
        p.write_text(json.dumps(data))
        assert main([str(p)]) == 1

    def test_valid_src_input_applies_and_returns_0(self, tmp_path, capsys):
        data = {"explanation": "x", "files": [{"path": "src/out.py", "content": "# ok"}]}
        json_path = _write_json(tmp_path, data)
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        # main() uses scripts/../ as repo_root (the directory containing scripts/).
        # We directly call load_and_validate + apply_files to test the round-trip.
        files = load_and_validate(json_path, str(tmp_path))
        written = apply_files(files, str(tmp_path))
        assert (tmp_path / "src" / "out.py").read_text() == "# ok"
        assert written == ["src/out.py"]


# Same corpus lives in agents/tests/test_call.py (sdlc_agents.call.normalise_path).
# Both guards must give the same verdict and the same normalised form.
PATH_CORPUS = [
    ("src/foo.py", "src/foo.py"),
    ("./src/foo.py", "src/foo.py"),
    ("src//a/./b.py", "src/a/b.py"),
    ("tests/unit/test_x.py", "tests/unit/test_x.py"),
    ("docs/api.md", "docs/api.md"),
    ("SRC/foo.py", None),
    ("Tests/x.py", None),
    ("src", None),
    ("src/", None),
    ("src/a/", None),
    ("./", None),
    ("", None),
    (".", None),
    ("src/..", None),
    ("src/../.git/config", None),
    ("src/a/../../etc/passwd", None),
    ("../outside.py", None),
    ("/etc/passwd", None),
    ("//src/foo.py", None),
    (".git/config", None),
    (".git/hooks/pre-commit", None),
    ("./.git/hooks/pre-commit", None),
    ("src/.git/config", None),
    ("src/.GIT/config", None),
    ("src/.git./config", None),
    ("src/a/.gitignore", None),
    ("src/.env", None),
    (".github/workflows/x.yml", None),
    ("./.github/actions/setup-atlas/action.yml", None),
    (".GITHUB/w", None),
    (".github./w", None),
    (".github\u200b/w", None),
    ("src/.github/x", None),
    (".claude/settings.json", None),
    (".devcontainer/devcontainer.json", None),
    ("C:/x.dll", None),
    ("C:\\x.dll", None),
    ("src\\foo.py", None),
    ("src/c:d.py", None),
    ("src/a\x00b.py", None),
    ("src/a\nb.py", None),
    ("src/a\tb.py", None),
    (" src/foo.py", None),
    ("src/foo.py ", None),
    ("src/caf\u00e9.py", None),
    ("README.md", None),
    ("pyproject.toml", None),
    ("action.yml", None),
    ("CLAUDE.md", None),
    ("scripts/apply_codegen.py", None),
    ("scripts/refresh_knowledge.py", None),
    ("src/" + "a" * 300, None),
]


@pytest.mark.parametrize("raw,expected", PATH_CORPUS)
def test_path_policy_corpus(raw, expected):
    from apply_codegen import normalise_path
    if expected is None:
        with pytest.raises(ValueError):
            normalise_path(raw)
    else:
        assert normalise_path(raw) == expected


class TestSymlinksAndShape:
    def test_symlinked_directory_into_dotgit_inside_repo_rejected(self, tmp_path):
        """Symlink that stays INSIDE the repo (src/x -> .git) is not an 'escape' but must be refused."""
        (tmp_path / ".git").mkdir()
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "x").symlink_to(tmp_path / ".git", target_is_directory=True)
        data = {"files": [{"path": "src/x/config", "content": "[core]\nfsmonitor=evil"}]}
        with pytest.raises(ValueError, match="symlink"):
            load_and_validate(_write_json(tmp_path, data), str(tmp_path))
        assert not (tmp_path / ".git" / "config").exists()

    def test_symlinked_root_dir_rejected(self, tmp_path):
        outside = tmp_path.parent / "elsewhere_dir"
        outside.mkdir(exist_ok=True)
        (tmp_path / "docs").symlink_to(outside, target_is_directory=True)
        data = {"files": [{"path": "docs/a.md", "content": "x"}]}
        with pytest.raises(ValueError, match="symlink|escapes"):
            load_and_validate(_write_json(tmp_path, data), str(tmp_path))
        assert not (outside / "a.md").exists()

    def test_case_insensitive_duplicate_rejected(self, tmp_path):
        data = {"files": [{"path": "src/Foo.py", "content": "a"}, {"path": "src/foo.py", "content": "b"}]}
        with pytest.raises(ValueError, match="Duplicate"):
            load_and_validate(_write_json(tmp_path, data), str(tmp_path))

    def test_empty_files_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="empty"):
            load_and_validate(_write_json(tmp_path, {"explanation": "x", "files": []}), str(tmp_path))

    def test_extra_entry_key_rejected(self, tmp_path):
        data = {"files": [{"path": "src/a.py", "content": "x", "mode": "0777"}]}
        with pytest.raises(ValueError, match="exactly the keys"):
            load_and_validate(_write_json(tmp_path, data), str(tmp_path))

    def test_nul_in_content_rejected(self, tmp_path):
        data = {"files": [{"path": "src/a.py", "content": "a\x00b"}]}
        with pytest.raises(ValueError, match="NUL"):
            load_and_validate(_write_json(tmp_path, data), str(tmp_path))

    def test_end_to_end_rejects_git_config_and_writes_nothing(self, tmp_path, capsys):
        (tmp_path / ".git").mkdir()
        data = {"files": [{"path": "src/ok.py", "content": "x"}, {"path": ".git/config", "content": "evil"}]}
        with pytest.raises(ValueError):
            load_and_validate(_write_json(tmp_path, data), str(tmp_path))
        assert not (tmp_path / "src" / "ok.py").exists()
        assert not (tmp_path / ".git" / "config").exists()
