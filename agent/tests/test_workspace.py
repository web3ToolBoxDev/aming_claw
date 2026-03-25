"""Tests for workspace.py, workspace_registry.py - workspace management."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from workspace import (  # noqa: E402
    clear_thread_workspace,
    clear_workspace_override,
    get_thread_workspace,
    get_workspace_override,
    resolve_active_workspace,
    resolve_workspace_from_env,
    set_thread_workspace,
    set_workspace_override,
    thread_workspace_context,
)
from workspace_registry import (  # noqa: E402
    _paths_equal,
    add_workspace,
    ensure_current_workspace_registered,
    find_workspace_by_label,
    find_workspace_by_path,
    get_default_workspace,
    get_workspace,
    is_blocked_workspace,
    list_workspaces,
    remove_workspace,
    resolve_workspace_for_task,
    set_default_workspace,
    update_workspace,
)


class TestThreadWorkspace(unittest.TestCase):
    def setUp(self):
        clear_thread_workspace()

    def tearDown(self):
        clear_thread_workspace()

    def test_default_none(self):
        self.assertIsNone(get_thread_workspace())

    def test_set_and_get(self):
        ws = Path("/tmp/test_ws")
        set_thread_workspace(ws)
        self.assertEqual(get_thread_workspace(), ws)

    def test_clear(self):
        set_thread_workspace(Path("/tmp/ws"))
        clear_thread_workspace()
        self.assertIsNone(get_thread_workspace())

    def test_context_manager(self):
        ws = Path("/tmp/ctx_ws")
        self.assertIsNone(get_thread_workspace())
        with thread_workspace_context(ws):
            self.assertEqual(get_thread_workspace(), ws)
        self.assertIsNone(get_thread_workspace())

    def test_nested_context(self):
        ws1 = Path("/tmp/ws1")
        ws2 = Path("/tmp/ws2")
        with thread_workspace_context(ws1):
            self.assertEqual(get_thread_workspace(), ws1)
            with thread_workspace_context(ws2):
                self.assertEqual(get_thread_workspace(), ws2)
            self.assertEqual(get_thread_workspace(), ws1)


class TestWorkspaceOverride(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_default_none(self):
        self.assertIsNone(get_workspace_override())

    def test_set_and_get(self):
        ws = Path(self.tmp.name) / "myproject"
        ws.mkdir()
        set_workspace_override(ws, changed_by=123)
        loaded = get_workspace_override()
        self.assertEqual(str(loaded), str(ws))

    def test_clear(self):
        set_workspace_override(Path("/tmp/ws"), changed_by=123)
        clear_workspace_override(changed_by=123)
        self.assertIsNone(get_workspace_override())


class TestResolveActiveWorkspace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        os.environ.pop("CODEX_WORKSPACE", None)
        clear_thread_workspace()

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        os.environ.pop("CODEX_WORKSPACE", None)
        clear_thread_workspace()
        self.tmp.cleanup()

    def test_thread_local_priority(self):
        ws = Path(self.tmp.name) / "thread_ws"
        ws.mkdir()
        set_thread_workspace(ws)
        self.assertEqual(resolve_active_workspace(), ws)

    def test_env_fallback(self):
        ws = Path(self.tmp.name) / "env_ws"
        ws.mkdir()
        os.environ["CODEX_WORKSPACE"] = str(ws)
        self.assertEqual(resolve_active_workspace(), ws)

    def test_cwd_default(self):
        # Without any overrides, should return cwd
        result = resolve_active_workspace()
        self.assertIsInstance(result, Path)


class TestResolveWorkspaceFromEnv(unittest.TestCase):
    def test_env_set(self):
        os.environ["CODEX_WORKSPACE"] = "/custom/path"
        self.assertEqual(resolve_workspace_from_env(), Path("/custom/path"))
        os.environ.pop("CODEX_WORKSPACE")

    def test_env_empty(self):
        os.environ.pop("CODEX_WORKSPACE", None)
        self.assertEqual(resolve_workspace_from_env(), Path.cwd())


class TestIsBlockedWorkspace(unittest.TestCase):
    def test_ssh_blocked(self):
        self.assertTrue(is_blocked_workspace(Path("/home/user/.ssh")))

    def test_aws_blocked(self):
        self.assertTrue(is_blocked_workspace(Path("/home/user/.aws")))

    def test_normal_allowed(self):
        self.assertFalse(is_blocked_workspace(Path("/home/user/projects")))


class TestWorkspaceRegistry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        # Create workspace directories
        self.ws1_path = Path(self.tmp.name) / "project-a"
        self.ws1_path.mkdir()
        self.ws2_path = Path(self.tmp.name) / "project-b"
        self.ws2_path.mkdir()

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_list_empty(self):
        self.assertEqual(list_workspaces(), [])

    def test_add_workspace(self):
        ws = add_workspace(self.ws1_path, label="proj-a")
        self.assertEqual(ws["label"], "proj-a")
        self.assertTrue(ws["id"].startswith("ws-"))
        self.assertTrue(ws["active"])

    def test_add_duplicate_raises(self):
        add_workspace(self.ws1_path)
        with self.assertRaises(ValueError):
            add_workspace(self.ws1_path)

    def test_add_sensitive_raises(self):
        ssh_dir = Path(self.tmp.name) / ".ssh"
        ssh_dir.mkdir()
        with self.assertRaises(ValueError):
            add_workspace(ssh_dir)

    def test_add_nonexistent_raises(self):
        with self.assertRaises(ValueError):
            add_workspace(Path(self.tmp.name) / "nonexistent")

    def test_list_workspaces(self):
        add_workspace(self.ws1_path, label="a")
        add_workspace(self.ws2_path, label="b")
        ws_list = list_workspaces()
        self.assertEqual(len(ws_list), 2)

    def test_get_workspace(self):
        ws = add_workspace(self.ws1_path)
        found = get_workspace(ws["id"])
        self.assertEqual(found["id"], ws["id"])

    def test_find_by_label(self):
        add_workspace(self.ws1_path, label="my-label")
        found = find_workspace_by_label("my-label")
        self.assertIsNotNone(found)
        self.assertEqual(found["label"], "my-label")

    def test_find_by_path(self):
        add_workspace(self.ws1_path)
        found = find_workspace_by_path(self.ws1_path)
        self.assertIsNotNone(found)

    def test_remove_workspace(self):
        ws = add_workspace(self.ws1_path)
        self.assertTrue(remove_workspace(ws["id"]))
        self.assertEqual(list_workspaces(), [])
        # Remove nonexistent
        self.assertFalse(remove_workspace("ws-nonexistent"))

    def test_update_workspace(self):
        ws = add_workspace(self.ws1_path, label="old")
        updated = update_workspace(ws["id"], label="new")
        self.assertEqual(updated["label"], "new")

    def test_default_workspace(self):
        ws1 = add_workspace(self.ws1_path, is_default=True)
        add_workspace(self.ws2_path)
        default = get_default_workspace()
        self.assertEqual(default["id"], ws1["id"])

    def test_set_default(self):
        add_workspace(self.ws1_path, is_default=True)
        ws2 = add_workspace(self.ws2_path)
        set_default_workspace(ws2["id"])
        default = get_default_workspace()
        self.assertEqual(default["id"], ws2["id"])


class TestResolveWorkspaceForTask(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        self.ws_path = Path(self.tmp.name) / "project"
        self.ws_path.mkdir()

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_explicit_id(self):
        ws = add_workspace(self.ws_path, label="test")
        task = {"target_workspace_id": ws["id"], "text": "test"}
        resolved = resolve_workspace_for_task(task)
        self.assertEqual(resolved["id"], ws["id"])

    def test_explicit_label(self):
        add_workspace(self.ws_path, label="my-proj")
        task = {"target_workspace": "my-proj", "text": "test"}
        resolved = resolve_workspace_for_task(task)
        self.assertEqual(resolved["label"], "my-proj")

    def test_at_prefix(self):
        add_workspace(self.ws_path, label="demo")
        task = {"text": "@workspace:demo 修复bug"}
        resolved = resolve_workspace_for_task(task)
        self.assertEqual(resolved["label"], "demo")

    def test_default_fallback(self):
        ws = add_workspace(self.ws_path, is_default=True)
        task = {"text": "普通任务"}
        resolved = resolve_workspace_for_task(task)
        self.assertEqual(resolved["id"], ws["id"])


class TestEnsureCurrentWorkspaceRegistered(unittest.TestCase):
    """Tests for ensure_current_workspace_registered auto-registration logic."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        self.current_dir = Path(self.tmp.name) / "current-project"
        self.current_dir.mkdir()
        (self.current_dir / ".git").mkdir()  # Must have .git to be registered
        self.other_dir = Path(self.tmp.name) / "toolbox"
        self.other_dir.mkdir()
        (self.other_dir / ".git").mkdir()  # Must have .git to be registered
        clear_thread_workspace()

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        os.environ.pop("CODEX_WORKSPACE", None)
        clear_thread_workspace()
        self.tmp.cleanup()

    def test_empty_registry_registers_as_default(self):
        """AC2.1-1: 注册表为空 → 注册当前目录为默认"""
        set_thread_workspace(self.current_dir)
        result = ensure_current_workspace_registered()
        self.assertIsNotNone(result)
        self.assertTrue(result["is_default"])
        self.assertEqual(len(list_workspaces()), 1)

    def test_other_workspace_exists_appends_non_default(self):
        """AC2.1-2: 注册表有1个其他工作区 → 追加当前目录（非默认）"""
        add_workspace(self.other_dir, label="toolbox", is_default=True)
        set_thread_workspace(self.current_dir)
        result = ensure_current_workspace_registered()
        self.assertIsNotNone(result)
        self.assertFalse(result["is_default"])
        self.assertEqual(len(list_workspaces()), 2)

    def test_current_already_registered_skips(self):
        """AC2.1-3: 注册表已含当前目录 → 不重复注册"""
        add_workspace(self.current_dir, label="current")
        set_thread_workspace(self.current_dir)
        result = ensure_current_workspace_registered()
        self.assertIsNone(result)
        self.assertEqual(len(list_workspaces()), 1)

    def test_multiple_workspaces_appends(self):
        """AC2.4: 注册表有2+个工作区且不含当前目录 → 追加注册"""
        add_workspace(self.other_dir, label="toolbox", is_default=True)
        extra = Path(self.tmp.name) / "extra"
        extra.mkdir()
        (extra / ".git").mkdir()  # Must have .git
        add_workspace(extra, label="extra")
        set_thread_workspace(self.current_dir)
        result = ensure_current_workspace_registered()
        self.assertIsNotNone(result)
        self.assertFalse(result["is_default"])
        self.assertEqual(len(list_workspaces()), 3)

    def test_nonexistent_dir_skips(self):
        """AC2.1-4: 当前目录不存在 → 不注册"""
        nonexistent = Path(self.tmp.name) / "does-not-exist"
        set_thread_workspace(nonexistent)
        result = ensure_current_workspace_registered()
        self.assertIsNone(result)
        self.assertEqual(len(list_workspaces()), 0)

    def test_subdirectory_skips(self):
        """Subdirectory of registered workspace should NOT be registered."""
        add_workspace(self.current_dir, label="project")
        subdir = self.current_dir / "agent"
        subdir.mkdir()
        (subdir / ".git").mkdir()  # Even with .git, subdirectory should be blocked
        set_thread_workspace(subdir)
        result = ensure_current_workspace_registered()
        self.assertIsNone(result)
        self.assertEqual(len(list_workspaces()), 1)

    def test_no_git_skips(self):
        """Directory without .git should NOT be registered."""
        no_git_dir = Path(self.tmp.name) / "no-git-project"
        no_git_dir.mkdir()
        set_thread_workspace(no_git_dir)
        result = ensure_current_workspace_registered()
        self.assertIsNone(result)

    def test_integration_extra_workspace_then_ensure(self):
        """集成测试: 有额外工作区 + 当前目录未注册 → list_workspaces 返回2个"""
        add_workspace(self.other_dir, label="toolbox", is_default=True)
        self.assertEqual(len(list_workspaces()), 1)
        set_thread_workspace(self.current_dir)
        ensure_current_workspace_registered()
        workspaces = list_workspaces()
        self.assertEqual(len(workspaces), 2)
        labels = {ws["label"] for ws in workspaces}
        self.assertIn("toolbox", labels)
        self.assertIn(self.current_dir.name, labels)


class TestPathsEqual(unittest.TestCase):
    """Tests for _paths_equal cross-platform path comparison."""

    def test_identical_paths(self):
        self.assertTrue(_paths_equal(Path("/tmp/a"), Path("/tmp/a")))

    def test_different_paths(self):
        self.assertFalse(_paths_equal(Path("/tmp/a"), Path("/tmp/b")))


class TestLooksLikePath(unittest.TestCase):
    """Tests for bot_commands._looks_like_path helper."""

    def setUp(self):
        from bot_commands import _looks_like_path
        self._looks_like_path = _looks_like_path

    def test_windows_absolute(self):
        self.assertTrue(self._looks_like_path("C:\\Users\\me\\project"))
        self.assertTrue(self._looks_like_path("D:/repos/foo"))

    def test_unix_absolute(self):
        self.assertTrue(self._looks_like_path("/home/user/project"))
        self.assertTrue(self._looks_like_path("/tmp/test"))

    def test_relative_with_separators(self):
        self.assertTrue(self._looks_like_path("./myproject"))
        self.assertTrue(self._looks_like_path("dir/subdir"))
        self.assertTrue(self._looks_like_path("dir\\subdir"))

    def test_tilde_path(self):
        self.assertTrue(self._looks_like_path("~/projects/foo"))

    def test_keyword_not_path(self):
        self.assertFalse(self._looks_like_path("toolbox"))
        self.assertFalse(self._looks_like_path("my-project"))
        self.assertFalse(self._looks_like_path("frontend"))
        self.assertFalse(self._looks_like_path("aming_claw"))

    def test_empty(self):
        self.assertFalse(self._looks_like_path(""))
        self.assertFalse(self._looks_like_path("   "))


class TestFuzzyWorkspaceAddFlow(unittest.TestCase):
    """Tests for fuzzy workspace search (find_git_workspace_candidates)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        # Create mock git repos
        for name in ["my-toolbox", "toolbox-utils", "frontend-app", "backend-api"]:
            d = self.root / name
            d.mkdir()
            (d / ".git").mkdir()
        # Non-git dir (should not match)
        nogit = self.root / "toolbox-docs"
        nogit.mkdir()

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        os.environ.pop("WORKSPACE_SEARCH_ROOTS", None)
        self.tmp.cleanup()

    def test_fuzzy_match_keyword(self):
        from bot_commands import find_git_workspace_candidates
        os.environ["WORKSPACE_SEARCH_ROOTS"] = str(self.root)
        results = find_git_workspace_candidates("toolbox")
        names = [p.name for p in results]
        self.assertIn("my-toolbox", names)
        self.assertIn("toolbox-utils", names)
        # toolbox-docs has no .git, should not appear
        self.assertNotIn("toolbox-docs", names)

    def test_fuzzy_no_match(self):
        from bot_commands import find_git_workspace_candidates
        os.environ["WORKSPACE_SEARCH_ROOTS"] = str(self.root)
        results = find_git_workspace_candidates("nonexistent-xyz")
        self.assertEqual(results, [])

    def test_fuzzy_single_match(self):
        from bot_commands import find_git_workspace_candidates
        os.environ["WORKSPACE_SEARCH_ROOTS"] = str(self.root)
        results = find_git_workspace_candidates("frontend")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].name, "frontend-app")


from utils import normalize_project_id  # noqa: E402
from workspace_registry import find_workspace_by_project_id, migrate_project_ids  # noqa: E402


class TestNormalizeProjectId(unittest.TestCase):
    """Tests for utils.normalize_project_id."""

    def test_camel_case(self):
        self.assertEqual(normalize_project_id("amingClaw"), "aming-claw")
        self.assertEqual(normalize_project_id("toolBoxClient"), "tool-box-client")

    def test_underscore(self):
        self.assertEqual(normalize_project_id("aming_claw"), "aming-claw")

    def test_already_kebab(self):
        self.assertEqual(normalize_project_id("aming-claw"), "aming-claw")

    def test_spaces(self):
        self.assertEqual(normalize_project_id("My App"), "my-app")

    def test_empty(self):
        self.assertEqual(normalize_project_id(""), "")
        self.assertEqual(normalize_project_id("  "), "")

    def test_all_variants_match(self):
        """amingClaw, aming_claw, aming-claw all normalize to same value."""
        expected = "aming-claw"
        self.assertEqual(normalize_project_id("amingClaw"), expected)
        self.assertEqual(normalize_project_id("aming_claw"), expected)
        self.assertEqual(normalize_project_id("aming-claw"), expected)


class TestFindWorkspaceByProjectId(unittest.TestCase):
    """Tests for workspace_registry.find_workspace_by_project_id."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        self.ws_path = Path(self.tmp.name) / "project"
        self.ws_path.mkdir()

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_exact_match(self):
        add_workspace(self.ws_path, label="aming", project_id="aming-claw")
        ws = find_workspace_by_project_id("aming-claw")
        self.assertIsNotNone(ws)
        self.assertEqual(ws["project_id"], "aming-claw")

    def test_normalized_match(self):
        """amingClaw should find workspace with project_id aming-claw."""
        add_workspace(self.ws_path, label="aming", project_id="aming-claw")
        ws = find_workspace_by_project_id("amingClaw")
        self.assertIsNotNone(ws)

    def test_no_match(self):
        add_workspace(self.ws_path, label="aming", project_id="aming-claw")
        ws = find_workspace_by_project_id("unknown-project")
        self.assertIsNone(ws)


class TestProjectIdInResolveWorkspaceForTask(unittest.TestCase):
    """Tests for project_id priority in resolve_workspace_for_task."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        self.ws_a = Path(self.tmp.name) / "project-a"
        self.ws_a.mkdir()
        self.ws_b = Path(self.tmp.name) / "project-b"
        self.ws_b.mkdir()

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_project_id_routes_correctly(self):
        """Task with project_id should resolve to matching workspace."""
        add_workspace(self.ws_a, label="toolbox", project_id="toolbox-client")
        add_workspace(self.ws_b, label="aming", project_id="aming-claw")
        task = {"project_id": "amingClaw", "text": "fix bug"}
        resolved = resolve_workspace_for_task(task)
        self.assertEqual(resolved["project_id"], "aming-claw")

    def test_label_beats_project_id(self):
        """Explicit target_workspace label should take priority over project_id."""
        add_workspace(self.ws_a, label="toolbox", project_id="toolbox-client")
        add_workspace(self.ws_b, label="aming", project_id="aming-claw")
        task = {"target_workspace": "toolbox", "project_id": "amingClaw", "text": "fix"}
        resolved = resolve_workspace_for_task(task)
        self.assertEqual(resolved["label"], "toolbox")

    def test_project_id_fallback_to_default(self):
        """Unknown project_id falls back to default workspace."""
        ws = add_workspace(self.ws_a, label="default", is_default=True)
        task = {"project_id": "unknown", "text": "test"}
        resolved = resolve_workspace_for_task(task)
        self.assertEqual(resolved["id"], ws["id"])

    def test_case_variants_all_resolve(self):
        """aming_claw, aming-claw, amingClaw all resolve to same workspace."""
        add_workspace(self.ws_a, label="aming", project_id="aming-claw")
        for variant in ["aming_claw", "aming-claw", "amingClaw"]:
            task = {"project_id": variant, "text": "test"}
            resolved = resolve_workspace_for_task(task)
            self.assertIsNotNone(resolved, f"Failed for variant: {variant}")
            self.assertEqual(resolved["project_id"], "aming-claw")


class TestMigrateProjectIds(unittest.TestCase):
    """Tests for workspace_registry.migrate_project_ids."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        self.ws_path = Path(self.tmp.name) / "toolBoxClient"
        self.ws_path.mkdir()

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_populates_missing_project_id(self):
        add_workspace(self.ws_path, label="toolBoxClient")
        ws_before = find_workspace_by_label("toolBoxClient")
        self.assertEqual(ws_before.get("project_id", ""), "")
        updated = migrate_project_ids()
        self.assertEqual(updated, 1)
        ws_after = find_workspace_by_label("toolBoxClient")
        self.assertEqual(ws_after["project_id"], "tool-box-client")

    def test_idempotent(self):
        add_workspace(self.ws_path, label="toolBoxClient")
        migrate_project_ids()
        updated = migrate_project_ids()
        self.assertEqual(updated, 0)

    def test_preserves_existing_project_id(self):
        add_workspace(self.ws_path, label="toolbox", project_id="my-custom-id")
        updated = migrate_project_ids()
        self.assertEqual(updated, 0)
        ws = find_workspace_by_label("toolbox")
        self.assertEqual(ws["project_id"], "my-custom-id")


class TestAddWorkspaceWithProjectId(unittest.TestCase):
    """Tests for add_workspace with project_id parameter."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        self.ws_path = Path(self.tmp.name) / "project"
        self.ws_path.mkdir()

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_stores_normalized_project_id(self):
        ws = add_workspace(self.ws_path, label="test", project_id="amingClaw")
        self.assertEqual(ws["project_id"], "aming-claw")

    def test_empty_project_id(self):
        ws = add_workspace(self.ws_path, label="test")
        self.assertEqual(ws["project_id"], "")


if __name__ == "__main__":
    unittest.main()
