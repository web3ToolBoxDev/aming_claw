"""Tests for git_rollback.py - needs_service_restart and summarize_changes_english."""
import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from git_rollback import needs_service_restart, summarize_changes_english  # noqa: E402


class NeedsServiceRestartTests(unittest.TestCase):
    """Tests for needs_service_restart()."""

    def test_agent_py_returns_true(self):
        self.assertTrue(needs_service_restart(["agent/executor.py"]))

    def test_multiple_agent_py_returns_true(self):
        self.assertTrue(needs_service_restart(["agent/executor.py", "agent/backends.py"]))

    def test_agent_tests_only_returns_false(self):
        self.assertFalse(needs_service_restart(["agent/tests/test_foo.py"]))

    def test_agent_tests_subdir_returns_false(self):
        self.assertFalse(needs_service_restart(["agent/tests/sub/test_bar.py"]))

    def test_readme_only_returns_false(self):
        self.assertFalse(needs_service_restart(["README.md"]))

    def test_docs_only_returns_false(self):
        self.assertFalse(needs_service_restart(["docs/guide.md", "CHANGELOG.md"]))

    def test_empty_list_returns_false(self):
        self.assertFalse(needs_service_restart([]))

    def test_config_file_returns_true(self):
        self.assertTrue(needs_service_restart(["config.yaml"]))

    def test_config_subpath_returns_true(self):
        self.assertTrue(needs_service_restart(["some/config.json"]))

    def test_scripts_dir_returns_true(self):
        self.assertTrue(needs_service_restart(["scripts/deploy.sh"]))

    def test_docker_compose_returns_true(self):
        self.assertTrue(needs_service_restart(["docker-compose.yml"]))

    def test_dockerfile_returns_true(self):
        self.assertTrue(needs_service_restart(["Dockerfile"]))

    def test_requirements_txt_returns_true(self):
        self.assertTrue(needs_service_restart(["requirements.txt"]))

    def test_requirements_dev_txt_returns_true(self):
        self.assertTrue(needs_service_restart(["requirements-dev.txt"]))

    def test_pyproject_toml_returns_true(self):
        self.assertTrue(needs_service_restart(["pyproject.toml"]))

    def test_mixed_test_and_agent_returns_true(self):
        """If any file triggers restart, the result is True."""
        self.assertTrue(needs_service_restart([
            "agent/tests/test_foo.py",
            "README.md",
            "agent/config.py",
        ]))

    def test_mixed_safe_files_returns_false(self):
        self.assertFalse(needs_service_restart([
            "agent/tests/test_a.py",
            "README.md",
            "docs/notes.md",
        ]))

    def test_windows_backslash_paths(self):
        """Backslash paths should be normalized correctly."""
        self.assertTrue(needs_service_restart(["agent\\executor.py"]))
        self.assertFalse(needs_service_restart(["agent\\tests\\test_foo.py"]))


class SummarizeChangesEnglishTests(unittest.TestCase):
    """Tests for summarize_changes_english()."""

    def test_empty_list_returns_no_file_changes(self):
        self.assertEqual(summarize_changes_english([]), "No file changes")

    def test_agent_core_files(self):
        result = summarize_changes_english(["agent/executor.py", "agent/backends.py"])
        self.assertIn("executor", result)
        self.assertIn("backends", result)

    def test_test_files(self):
        result = summarize_changes_english(["agent/tests/test_foo.py"])
        self.assertIn("tests", result.lower())

    def test_readme_docs(self):
        result = summarize_changes_english(["README.md"])
        self.assertIn("docs", result.lower())

    def test_scripts_category(self):
        result = summarize_changes_english(["scripts/deploy.sh"])
        self.assertIn("scripts", result.lower())

    def test_config_category(self):
        result = summarize_changes_english(["config.yaml"])
        self.assertIn("configuration", result.lower())

    def test_summary_max_72_chars(self):
        # Many files to force a long summary
        files = ["agent/{}.py".format(name) for name in
                 ["a_very_long_module_name", "another_long_name", "yet_another",
                  "more_stuff", "extra_module"]]
        result = summarize_changes_english(files)
        self.assertLessEqual(len(result), 72)

    def test_starts_with_update(self):
        result = summarize_changes_english(["agent/executor.py"])
        self.assertTrue(result.startswith("Update"))

    def test_mixed_categories(self):
        result = summarize_changes_english([
            "agent/executor.py",
            "agent/tests/test_exec.py",
            "README.md",
        ])
        self.assertIn("executor", result)
        self.assertIn("tests", result.lower())
        self.assertIn("docs", result.lower())


if __name__ == "__main__":
    unittest.main()
