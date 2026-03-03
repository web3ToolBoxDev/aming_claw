"""Tests for project_summary.py - project information collection and formatting."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from project_summary import (  # noqa: E402
    collect_project_info,
    collect_recent_commits,
    format_summary_text,
    generate_ai_summary,
    _collect_commit_diffs,
    _build_summary_prompt,
    _build_fallback_summary,
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


class TestCollectCommitDiffs(unittest.TestCase):
    """Test _collect_commit_diffs with real git repositories."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmpdir.name)
        _git(self.ws, "init")
        _git(self.ws, "config", "user.email", "test@test.com")
        _git(self.ws, "config", "user.name", "TestDev")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_returns_diffs_for_commits(self):
        (self.ws / "a.py").write_text("def a(): pass\n")
        _git(self.ws, "add", ".")
        _git(self.ws, "commit", "-m", "add function a")
        (self.ws / "b.py").write_text("def b(): pass\n")
        _git(self.ws, "add", ".")
        _git(self.ws, "commit", "-m", "add function b")

        diffs = _collect_commit_diffs(self.ws, 2)
        self.assertEqual(len(diffs), 2)
        # Most recent first
        self.assertEqual(diffs[0]["message"], "add function b")
        self.assertEqual(diffs[1]["message"], "add function a")
        # Each should have diff content
        for d in diffs:
            self.assertIn("hash", d)
            self.assertIn("diff_stat", d)
            self.assertIn("diff_content", d)
            self.assertIn("author", d)
            self.assertEqual(d["author"], "TestDev")

    def test_respects_count_limit(self):
        for i in range(5):
            (self.ws / "f{}.txt".format(i)).write_text(str(i))
            _git(self.ws, "add", ".")
            _git(self.ws, "commit", "-m", "commit {}".format(i))
        diffs = _collect_commit_diffs(self.ws, 2)
        self.assertEqual(len(diffs), 2)

    def test_non_git_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            diffs = _collect_commit_diffs(Path(d))
            self.assertEqual(diffs, [])

    def test_no_commits_returns_empty(self):
        diffs = _collect_commit_diffs(self.ws)
        self.assertEqual(diffs, [])

    def test_count_clamped(self):
        """commit_count is clamped to 1-10."""
        (self.ws / "a.txt").write_text("a")
        _git(self.ws, "add", ".")
        _git(self.ws, "commit", "-m", "one")
        diffs = _collect_commit_diffs(self.ws, 0)  # clamped to 1
        self.assertEqual(len(diffs), 1)


class TestBuildSummaryPrompt(unittest.TestCase):
    """Test prompt construction."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmpdir.name)
        (self.ws / "requirements.txt").write_text("flask\n")
        sub = self.ws / "src"
        sub.mkdir()
        (sub / "app.py").write_text("# app\n")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_prompt_contains_commit_info(self):
        commit_diffs = [
            {"hash": "abc1234", "full_hash": "abc1234" * 5, "message": "add feature",
             "author": "dev", "date": "2026-03-01",
             "diff_stat": "1 file changed", "diff_content": "+def feature(): pass"},
        ]
        prompt = _build_summary_prompt(self.ws, commit_diffs)
        self.assertIn("abc1234", prompt)
        self.assertIn("add feature", prompt)
        self.assertIn("Python", prompt)  # tech stack detected
        self.assertIn("中文", prompt)

    def test_prompt_multiple_commits(self):
        commit_diffs = [
            {"hash": "aaa", "full_hash": "aaa" * 10, "message": "first",
             "author": "a", "date": "2026-03-01",
             "diff_stat": "", "diff_content": ""},
            {"hash": "bbb", "full_hash": "bbb" * 10, "message": "second",
             "author": "b", "date": "2026-03-02",
             "diff_stat": "", "diff_content": ""},
        ]
        prompt = _build_summary_prompt(self.ws, commit_diffs)
        self.assertIn("提交 1", prompt)
        self.assertIn("提交 2", prompt)


class TestBuildFallbackSummary(unittest.TestCase):
    """Test fallback summary generation."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_contains_commit_info(self):
        diffs = [
            {"hash": "abc1234", "message": "add feature", "author": "dev",
             "date": "2026-03-01", "diff_stat": "1 file changed, 10 insertions",
             "diff_content": ""},
        ]
        text = _build_fallback_summary(self.ws, diffs)
        self.assertIn("abc1234", text)
        self.assertIn("add feature", text)
        self.assertIn("AI 分析不可用", text)


class TestGenerateAiSummary(unittest.TestCase):
    """Test generate_ai_summary with mocked AI calls."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmpdir.name)
        _git(self.ws, "init")
        _git(self.ws, "config", "user.email", "test@test.com")
        _git(self.ws, "config", "user.name", "TestDev")
        (self.ws / "main.py").write_text("print('hello')\n")
        _git(self.ws, "add", ".")
        _git(self.ws, "commit", "-m", "initial commit")

    def tearDown(self):
        self.tmpdir.cleanup()

    @patch("project_summary._call_ai_api")
    def test_ai_success(self, mock_ai):
        mock_ai.return_value = "这是一个测试项目，实现了基本功能。"
        result = generate_ai_summary(self.ws)
        self.assertIn("项目总结", result)
        self.assertIn("测试项目", result)
        mock_ai.assert_called_once()

    @patch("project_summary._call_ai_api")
    def test_ai_failure_fallback(self, mock_ai):
        mock_ai.return_value = None
        result = generate_ai_summary(self.ws)
        self.assertIn("AI 分析不可用", result)
        self.assertIn("initial commit", result)

    def test_non_git_repo(self):
        with tempfile.TemporaryDirectory() as d:
            result = generate_ai_summary(d)
            self.assertIn("非 Git 仓库", result)

    def test_no_commits(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            _git(ws, "init")
            result = generate_ai_summary(ws)
            self.assertIn("无提交记录", result)

    @patch("project_summary._call_ai_api")
    def test_multiple_commits(self, mock_ai):
        (self.ws / "extra.py").write_text("# extra\n")
        _git(self.ws, "add", ".")
        _git(self.ws, "commit", "-m", "add extra module")
        mock_ai.return_value = "项目新增了额外模块。"
        result = generate_ai_summary(self.ws, commit_count=2)
        self.assertIn("项目总结", result)
        mock_ai.assert_called_once()
        # Check prompt was built with 2 commits
        prompt_arg = mock_ai.call_args[0][0]
        self.assertIn("initial commit", prompt_arg)
        self.assertIn("add extra module", prompt_arg)

    @patch("project_summary._call_ai_api")
    def test_commit_count_clamped(self, mock_ai):
        mock_ai.return_value = "OK"
        generate_ai_summary(self.ws, commit_count=100)
        # Should have been clamped to 10
        mock_ai.assert_called_once()


if __name__ == "__main__":
    unittest.main()
