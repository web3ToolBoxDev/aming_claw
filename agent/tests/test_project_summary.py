"""Tests for project_summary.py - project information collection and formatting."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from project_summary import (  # noqa: E402
    collect_project_info,
    collect_recent_commits,
    format_summary_text,
    _detect_tech_stack,
    _collect_file_stats,
    _collect_top_dirs,
)


def _git(cwd, *args):
    """Helper to run git commands in test directories."""
    subprocess.run(
        ["git"] + list(args),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


class TestCollectProjectInfoGitRepo(unittest.TestCase):
    """Test collect_project_info against a real (temporary) git repository."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmpdir.name)
        _git(self.ws, "init")
        _git(self.ws, "config", "user.email", "test@test.com")
        _git(self.ws, "config", "user.name", "Test")
        # Create some files
        (self.ws / "main.py").write_text("print('hello')\n")
        (self.ws / "utils.py").write_text("def helper(): pass\n")
        (self.ws / "requirements.txt").write_text("requests\n")
        sub = self.ws / "src"
        sub.mkdir()
        (sub / "app.py").write_text("# app\n")
        _git(self.ws, "add", ".")
        _git(self.ws, "commit", "-m", "initial commit")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_returns_complete_fields(self):
        info = collect_project_info(self.ws)
        self.assertIn("name", info)
        self.assertIn("path", info)
        self.assertIn("git", info)
        self.assertIn("tech_stack", info)
        self.assertIn("file_stats", info)
        self.assertIn("top_dirs", info)

    def test_git_fields_populated(self):
        info = collect_project_info(self.ws)
        git = info["git"]
        self.assertTrue(git["is_repo"])
        self.assertIn(git["branch"], ("main", "master"))
        self.assertTrue(len(git["commit"]) > 0)
        self.assertFalse(git["has_uncommitted"])
        self.assertEqual(git["uncommitted_count"], 0)

    def test_git_uncommitted_detection(self):
        (self.ws / "new_file.txt").write_text("uncommitted\n")
        info = collect_project_info(self.ws)
        git = info["git"]
        self.assertTrue(git["has_uncommitted"])
        self.assertGreater(git["uncommitted_count"], 0)

    def test_file_stats(self):
        info = collect_project_info(self.ws)
        stats = info["file_stats"]
        self.assertGreater(stats["total_files"], 0)
        self.assertIn(".py", stats["by_extension"])

    def test_tech_stack_detected(self):
        info = collect_project_info(self.ws)
        self.assertIn("Python", info["tech_stack"])

    def test_top_dirs(self):
        info = collect_project_info(self.ws)
        dir_names = [d.strip().rstrip("/") for d in info["top_dirs"]]
        self.assertIn("src", dir_names)


class TestCollectProjectInfoNonGit(unittest.TestCase):
    """Test collect_project_info on a plain directory (non-git)."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmpdir.name)
        (self.ws / "hello.txt").write_text("hello\n")
        sub = self.ws / "docs"
        sub.mkdir()
        (sub / "readme.md").write_text("# doc\n")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_git_fields_empty(self):
        info = collect_project_info(self.ws)
        git = info["git"]
        self.assertFalse(git["is_repo"])
        self.assertEqual(git["branch"], "")
        self.assertEqual(git["commit"], "")
        self.assertFalse(git["has_uncommitted"])

    def test_file_stats_still_works(self):
        info = collect_project_info(self.ws)
        self.assertGreater(info["file_stats"]["total_files"], 0)

    def test_top_dirs(self):
        info = collect_project_info(self.ws)
        dir_names = [d.strip().rstrip("/") for d in info["top_dirs"]]
        self.assertIn("docs", dir_names)


class TestTechStackDetection(unittest.TestCase):
    """Test tech stack detection via feature files."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_nodejs_detected(self):
        (self.ws / "package.json").write_text("{}\n")
        result = _detect_tech_stack(self.ws)
        self.assertIn("Node.js", result)

    def test_python_requirements(self):
        (self.ws / "requirements.txt").write_text("flask\n")
        result = _detect_tech_stack(self.ws)
        self.assertIn("Python", result)

    def test_python_pyproject(self):
        (self.ws / "pyproject.toml").write_text("[tool.poetry]\n")
        result = _detect_tech_stack(self.ws)
        self.assertIn("Python", result)

    def test_rust_detected(self):
        (self.ws / "Cargo.toml").write_text("[package]\n")
        result = _detect_tech_stack(self.ws)
        self.assertIn("Rust", result)

    def test_go_detected(self):
        (self.ws / "go.mod").write_text("module example\n")
        result = _detect_tech_stack(self.ws)
        self.assertIn("Go", result)

    def test_docker_detected(self):
        (self.ws / "Dockerfile").write_text("FROM alpine\n")
        result = _detect_tech_stack(self.ws)
        self.assertIn("Docker", result)

    def test_docker_compose_detected(self):
        (self.ws / "docker-compose.yml").write_text("version: '3'\n")
        result = _detect_tech_stack(self.ws)
        self.assertIn("Docker Compose", result)

    def test_multiple_stacks(self):
        (self.ws / "package.json").write_text("{}\n")
        (self.ws / "Dockerfile").write_text("FROM node\n")
        (self.ws / "tsconfig.json").write_text("{}\n")
        result = _detect_tech_stack(self.ws)
        self.assertIn("Node.js", result)
        self.assertIn("Docker", result)
        self.assertIn("TypeScript", result)

    def test_empty_dir(self):
        result = _detect_tech_stack(self.ws)
        self.assertEqual(result, [])


class TestCollectRecentCommits(unittest.TestCase):
    """Test collect_recent_commits with real git repositories."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmpdir.name)
        _git(self.ws, "init")
        _git(self.ws, "config", "user.email", "test@test.com")
        _git(self.ws, "config", "user.name", "TestAuthor")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_commits_returned(self):
        (self.ws / "a.txt").write_text("a\n")
        _git(self.ws, "add", ".")
        _git(self.ws, "commit", "-m", "first commit")
        (self.ws / "b.txt").write_text("b\n")
        _git(self.ws, "add", ".")
        _git(self.ws, "commit", "-m", "second commit")

        commits = collect_recent_commits(self.ws)
        self.assertEqual(len(commits), 2)
        self.assertEqual(commits[0]["message"], "second commit")
        self.assertEqual(commits[1]["message"], "first commit")

    def test_commit_fields(self):
        (self.ws / "a.txt").write_text("a\n")
        _git(self.ws, "add", ".")
        _git(self.ws, "commit", "-m", "test message")

        commits = collect_recent_commits(self.ws)
        self.assertEqual(len(commits), 1)
        c = commits[0]
        self.assertIn("sha", c)
        self.assertIn("author", c)
        self.assertIn("date", c)
        self.assertIn("message", c)
        self.assertEqual(c["author"], "TestAuthor")
        self.assertEqual(c["message"], "test message")
        # Date should be YYYY-MM-DD format
        self.assertEqual(len(c["date"]), 10)

    def test_count_limit(self):
        for i in range(5):
            (self.ws / "f{}.txt".format(i)).write_text(str(i))
            _git(self.ws, "add", ".")
            _git(self.ws, "commit", "-m", "commit {}".format(i))

        commits = collect_recent_commits(self.ws, count=3)
        self.assertEqual(len(commits), 3)

    def test_empty_repo_no_commits(self):
        """Empty repo (no commits yet) returns empty list."""
        commits = collect_recent_commits(self.ws)
        self.assertEqual(commits, [])


class TestCollectRecentCommitsNonGit(unittest.TestCase):
    """Test collect_recent_commits on non-git directory."""

    def test_non_git_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            commits = collect_recent_commits(Path(tmpdir))
            self.assertEqual(commits, [])


class TestFormatSummaryText(unittest.TestCase):
    """Test format_summary_text with mock data."""

    def _make_info(self, **overrides):
        base = {
            "name": "test-project",
            "path": "/home/user/test-project",
            "git": {
                "is_repo": True,
                "branch": "main",
                "commit": "abc1234",
                "has_uncommitted": False,
                "uncommitted_count": 0,
            },
            "tech_stack": ["Python", "Docker"],
            "file_stats": {
                "total_files": 42,
                "by_extension": {".py": 20, ".json": 5, ".md": 3},
            },
            "top_dirs": ["agent/", "  tests/", "docs/"],
        }
        base.update(overrides)
        return base

    def _make_commits(self, count=3):
        return [
            {"sha": "abc{}".format(i), "author": "dev", "date": "2026-03-0{}".format(i + 1), "message": "commit {}".format(i)}
            for i in range(count)
        ]

    def test_contains_project_name(self):
        text = format_summary_text(self._make_info(), self._make_commits())
        self.assertIn("test-project", text)

    def test_contains_branch_info(self):
        text = format_summary_text(self._make_info(), self._make_commits())
        self.assertIn("main", text)
        self.assertIn("abc1234", text)

    def test_contains_tech_stack(self):
        text = format_summary_text(self._make_info(), self._make_commits())
        self.assertIn("Python", text)
        self.assertIn("Docker", text)

    def test_contains_file_stats(self):
        text = format_summary_text(self._make_info(), self._make_commits())
        self.assertIn("42", text)
        self.assertIn(".py", text)

    def test_contains_commits(self):
        commits = self._make_commits(3)
        text = format_summary_text(self._make_info(), commits)
        self.assertIn("commit 0", text)
        self.assertIn("commit 2", text)
        self.assertIn("3", text)  # commit count

    def test_contains_dir_structure(self):
        text = format_summary_text(self._make_info(), [])
        self.assertIn("agent/", text)
        self.assertIn("docs/", text)

    def test_non_git_repo(self):
        info = self._make_info()
        info["git"] = {
            "is_repo": False,
            "branch": "",
            "commit": "",
            "has_uncommitted": False,
            "uncommitted_count": 0,
        }
        text = format_summary_text(info, [])
        self.assertIn("\u975e Git", text)

    def test_uncommitted_changes(self):
        info = self._make_info()
        info["git"]["has_uncommitted"] = True
        info["git"]["uncommitted_count"] = 5
        text = format_summary_text(info, [])
        self.assertIn("5", text)
        self.assertIn("\u672a\u63d0\u4ea4", text)

    def test_empty_commits(self):
        text = format_summary_text(self._make_info(), [])
        # Should not contain commit section header when no commits
        self.assertNotIn("\u6700\u8fd1\u63d0\u4ea4", text)


class TestFormatSummaryLong(unittest.TestCase):
    """Test that long content is formatted correctly (not truncated)."""

    def test_long_file_stats_not_truncated(self):
        by_ext = {}
        for i in range(50):
            by_ext[".ext{}".format(i)] = 100 + i
        info = {
            "name": "big-project",
            "path": "/big",
            "git": {"is_repo": False, "branch": "", "commit": "", "has_uncommitted": False, "uncommitted_count": 0},
            "tech_stack": [],
            "file_stats": {"total_files": 5000, "by_extension": by_ext},
            "top_dirs": [],
        }
        text = format_summary_text(info, [])
        # Top 10 extensions shown + "其他" line
        self.assertIn("\u5176\u4ed6", text)
        self.assertIn("5000", text)

    def test_many_commits_all_shown(self):
        commits = [
            {"sha": "sha{}".format(i), "author": "dev", "date": "2026-01-01", "message": "msg {}".format(i)}
            for i in range(50)
        ]
        info = {
            "name": "proj",
            "path": "/proj",
            "git": {"is_repo": True, "branch": "main", "commit": "aaa", "has_uncommitted": False, "uncommitted_count": 0},
            "tech_stack": [],
            "file_stats": {"total_files": 0, "by_extension": {}},
            "top_dirs": [],
        }
        text = format_summary_text(info, commits)
        # All 50 commits should be present
        self.assertIn("msg 0", text)
        self.assertIn("msg 49", text)
        self.assertIn("50", text)  # count in header


if __name__ == "__main__":
    unittest.main()
