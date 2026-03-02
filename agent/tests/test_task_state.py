"""Tests for task_state.py - task lifecycle state machine."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from task_state import (  # noqa: E402
    _build_archive_id,
    _new_task_code,
    _semantic_slug,
    _truncate_to_bytes,
    append_task_event,
    archive_task_result,
    clear_active_tasks,
    find_archive_entry,
    group_archive_entries,
    init_task_lifecycle,
    list_active_tasks,
    list_task_state_candidates,
    load_runtime_state,
    load_task_status,
    mark_task_completion_notified,
    mark_task_finished,
    mark_task_started,
    mark_task_timeout,
    read_task_events,
    register_task_created,
    resolve_task_ref,
    save_runtime_state,
    save_task_status,
    search_archive_entries,
    update_task_heartbeat,
    update_task_lifecycle,
    update_task_runtime,
)


class TaskStateTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def _make_task(self, task_id="task-test-001", **kwargs):
        base = {
            "task_id": task_id,
            "task_code": "",
            "chat_id": 123,
            "requested_by": 456,
            "action": "codex",
            "text": "测试任务",
            "status": "pending",
        }
        base.update(kwargs)
        return base


class TestRuntimeState(TaskStateTestBase):
    def test_default_state(self):
        state = load_runtime_state()
        self.assertEqual(state["next_code"], 1)
        self.assertEqual(state["aliases"], {})
        self.assertEqual(state["active"], {})

    def test_save_and_load(self):
        state = load_runtime_state()
        state["next_code"] = 5
        state["aliases"]["T0001"] = "task-1"
        save_runtime_state(state)

        loaded = load_runtime_state()
        self.assertEqual(loaded["next_code"], 5)
        self.assertEqual(loaded["aliases"]["T0001"], "task-1")


class TestNewTaskCode(TaskStateTestBase):
    def test_sequential_codes(self):
        state = {"next_code": 1, "aliases": {}}
        code1 = _new_task_code(state)
        self.assertEqual(code1, "T0001")
        self.assertEqual(state["next_code"], 2)

        code2 = _new_task_code(state)
        self.assertEqual(code2, "T0002")

    def test_skips_existing(self):
        state = {"next_code": 1, "aliases": {"T0001": "task-old"}}
        code = _new_task_code(state)
        self.assertEqual(code, "T0002")


class TestSemanticSlug(TaskStateTestBase):
    def test_basic_slug(self):
        slug = _semantic_slug("修复登录异常", "codex")
        self.assertTrue(len(slug) > 0)

    def test_empty_text(self):
        slug = _semantic_slug("", "codex")
        self.assertEqual(slug, "codex")

    def test_long_text_truncated(self):
        slug = _semantic_slug("a" * 200, "codex")
        self.assertLessEqual(len(slug.encode("utf-8")), 28)

    def test_chinese_slug_byte_limit(self):
        # Each Chinese char is 3 bytes; 10 chars = 30 bytes > 28
        slug = _semantic_slug("修复登录异常重启服务器测试", "codex")
        self.assertLessEqual(len(slug.encode("utf-8")), 28)

    def test_slug_no_trailing_dash(self):
        slug = _semantic_slug("ab cd ef gh ij kl mn", "codex")
        self.assertFalse(slug.endswith("-"))


class TestRegisterTask(TaskStateTestBase):
    def test_register_returns_code(self):
        task = self._make_task()
        code = register_task_created(task)
        self.assertTrue(code.startswith("T"))

    def test_register_creates_state(self):
        task = self._make_task()
        code = register_task_created(task)

        state = load_runtime_state()
        self.assertIn(code, state["aliases"])
        self.assertIn("task-test-001", state["active"])

    def test_register_missing_id_raises(self):
        with self.assertRaises(RuntimeError):
            register_task_created({"task_id": ""})

    def test_register_multiple_sequential(self):
        codes = []
        for i in range(5):
            task = self._make_task(task_id=f"task-{i}")
            code = register_task_created(task)
            codes.append(code)
        # All codes should be unique
        self.assertEqual(len(set(codes)), 5)


class TestResolveTaskRef(TaskStateTestBase):
    def test_resolve_by_code(self):
        task = self._make_task()
        code = register_task_created(task)
        resolved = resolve_task_ref(code)
        self.assertEqual(resolved, "task-test-001")

    def test_resolve_case_insensitive(self):
        task = self._make_task()
        code = register_task_created(task)
        resolved = resolve_task_ref(code.lower())
        self.assertEqual(resolved, "task-test-001")

    def test_resolve_by_task_id(self):
        resolved = resolve_task_ref("task-something")
        self.assertEqual(resolved, "task-something")

    def test_resolve_empty(self):
        self.assertIsNone(resolve_task_ref(""))
        self.assertIsNone(resolve_task_ref(None))


class TestTaskLifecycle(TaskStateTestBase):
    def test_init_creates_status(self):
        task = self._make_task()
        obj = init_task_lifecycle(task)
        self.assertEqual(obj["status"], "pending")
        self.assertEqual(obj["stage"], "pending")

    def test_mark_started(self):
        task = self._make_task()
        init_task_lifecycle(task)
        obj = mark_task_started(task)
        self.assertEqual(obj["status"], "processing")
        self.assertTrue(obj["started_at"])

    def test_mark_finished(self):
        task = self._make_task()
        init_task_lifecycle(task)
        obj = mark_task_finished(
            task, status="completed", summary="done", result_file="/tmp/result.json"
        )
        self.assertEqual(obj["status"], "completed")
        self.assertTrue(obj["has_end_marker"])
        self.assertTrue(obj["ended_at"])

    def test_update_lifecycle(self):
        task = self._make_task()
        init_task_lifecycle(task)
        obj = update_task_lifecycle(task, status="processing", stage="running")
        self.assertEqual(obj["status"], "processing")
        self.assertEqual(obj["stage"], "running")

    def test_mark_timeout(self):
        task = self._make_task()
        init_task_lifecycle(task)
        obj = mark_task_timeout("task-test-001", 123)
        self.assertEqual(obj["status"], "timeout")
        self.assertTrue(obj["has_end_marker"])

    def test_completion_notified(self):
        task = self._make_task()
        init_task_lifecycle(task)
        obj = mark_task_completion_notified("task-test-001")
        self.assertTrue(obj["completion_notified_at"])


class TestTaskEvents(TaskStateTestBase):
    def test_append_and_read(self):
        append_task_event("task-e1", "created", {"info": "test"})
        append_task_event("task-e1", "started", {"stage": "processing"})
        events = read_task_events("task-e1")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["event"], "created")
        self.assertEqual(events[1]["event"], "started")

    def test_read_empty(self):
        events = read_task_events("nonexistent")
        self.assertEqual(events, [])

    def test_limit(self):
        for i in range(30):
            append_task_event("task-e2", f"event_{i}")
        events = read_task_events("task-e2", limit=5)
        self.assertEqual(len(events), 5)
        # Should be the last 5
        self.assertEqual(events[0]["event"], "event_25")


class TestHeartbeat(TaskStateTestBase):
    def test_update_heartbeat(self):
        task = self._make_task()
        init_task_lifecycle(task)
        update_task_heartbeat("task-test-001", progress=50)
        status = load_task_status("task-test-001")
        self.assertTrue(status["heartbeat_at"])
        self.assertEqual(status["progress"], 50)

    def test_heartbeat_progress_only_increases(self):
        task = self._make_task()
        init_task_lifecycle(task)
        update_task_heartbeat("task-test-001", progress=80)
        update_task_heartbeat("task-test-001", progress=30)
        status = load_task_status("task-test-001")
        self.assertEqual(status["progress"], 80)  # should not decrease

    def test_heartbeat_nonexistent(self):
        # Should not raise
        update_task_heartbeat("nonexistent-task", progress=10)


class TestListActiveTasks(TaskStateTestBase):
    def test_list_active(self):
        register_task_created(self._make_task("task-1", chat_id=100))
        register_task_created(self._make_task("task-2", chat_id=100))
        register_task_created(self._make_task("task-3", chat_id=200))

        all_tasks = list_active_tasks()
        self.assertEqual(len(all_tasks), 3)

        chat_100 = list_active_tasks(chat_id=100)
        self.assertEqual(len(chat_100), 2)

    def test_list_empty(self):
        self.assertEqual(list_active_tasks(), [])


class TestClearActiveTasks(TaskStateTestBase):
    def test_clear_pending(self):
        register_task_created(self._make_task("task-1", chat_id=100))
        register_task_created(self._make_task("task-2", chat_id=100))
        count = clear_active_tasks(100)
        self.assertEqual(count, 2)
        self.assertEqual(list_active_tasks(chat_id=100), [])

    def test_clear_keeps_processing(self):
        register_task_created(self._make_task("task-1", chat_id=100))
        # Mark task as processing
        update_task_runtime(
            self._make_task("task-1", chat_id=100),
            status="processing",
            stage="processing",
        )
        count = clear_active_tasks(100)
        self.assertEqual(count, 0)  # should keep processing tasks


class TestUpdateTaskRuntime(TaskStateTestBase):
    def test_update_runtime(self):
        task = self._make_task()
        register_task_created(task)
        update_task_runtime(task, status="processing", stage="running")

        state = load_runtime_state()
        entry = state["active"].get("task-test-001")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["status"], "processing")


class TestArchive(TaskStateTestBase):
    def test_archive_task_result(self):
        task = self._make_task()
        register_task_created(task)
        task["status"] = "accepted"
        task["executor"] = {"last_message": "任务完成"}

        from utils import save_json, tasks_root
        result_path = tasks_root() / "results" / "task-test-001.json"
        save_json(result_path, task)

        entry = archive_task_result(task, result_path, None)
        self.assertIn("archive_id", entry)
        self.assertEqual(entry["task_id"], "task-test-001")

        # Task should be removed from active
        state = load_runtime_state()
        self.assertNotIn("task-test-001", state.get("active", {}))

    def test_find_archive_entry(self):
        task = self._make_task()
        task["task_code"] = "T0099"
        register_task_created(task)
        task["status"] = "accepted"
        task["executor"] = {"last_message": "done"}

        from utils import save_json, tasks_root
        result_path = tasks_root() / "results" / "task-test-001.json"
        save_json(result_path, task)
        archive_task_result(task, result_path, None)

        found = find_archive_entry("task-test-001")
        self.assertIsNotNone(found)
        self.assertEqual(found["task_id"], "task-test-001")

    def test_search_archive(self):
        for i in range(3):
            task = self._make_task(f"task-search-{i}", text=f"搜索任务{i}")
            task["task_code"] = f"T{i:04d}"
            task["status"] = "accepted"
            task["executor"] = {"last_message": f"完成任务{i}"}

            from utils import save_json, tasks_root
            result_path = tasks_root() / "results" / f"task-search-{i}.json"
            save_json(result_path, task)
            archive_task_result(task, result_path, None)

        results = search_archive_entries("搜索", limit=10)
        self.assertGreater(len(results), 0)


class TestGroupArchiveEntries(TaskStateTestBase):
    def test_grouping(self):
        items = [
            {"action": "codex", "task_id": "1"},
            {"action": "codex", "task_id": "2"},
            {"action": "claude", "task_id": "3"},
        ]
        grouped = group_archive_entries(items)
        self.assertIn("codex", grouped)
        self.assertIn("claude", grouped)
        self.assertEqual(grouped["codex"]["count"], 2)
        self.assertEqual(grouped["claude"]["count"], 1)


class TestListTaskStateCandidates(TaskStateTestBase):
    def test_list_candidates(self):
        for i in range(3):
            task = self._make_task(f"task-cand-{i}")
            init_task_lifecycle(task)

        candidates = list_task_state_candidates()
        self.assertEqual(len(candidates), 3)


class TestBuildArchiveIdLength(TaskStateTestBase):
    """Ensure _build_archive_id always produces IDs ≤ 48 bytes."""

    def test_short_ascii(self):
        aid = _build_archive_id("codex", "fix bug")
        self.assertLessEqual(len(aid.encode("utf-8")), 48)

    def test_long_ascii(self):
        aid = _build_archive_id("codex", "a very long task description that goes on and on")
        self.assertLessEqual(len(aid.encode("utf-8")), 48)

    def test_chinese_text(self):
        aid = _build_archive_id("codex", "修复点击归档任务报错按钮操作失败导致系统崩溃")
        self.assertLessEqual(len(aid.encode("utf-8")), 48)

    def test_mixed_text(self):
        aid = _build_archive_id("pipeline", "修复login接口返回500错误并添加retry逻辑")
        self.assertLessEqual(len(aid.encode("utf-8")), 48)

    def test_empty_text(self):
        aid = _build_archive_id("codex", "")
        self.assertLessEqual(len(aid.encode("utf-8")), 48)
        self.assertTrue(aid.startswith("arc-"))

    def test_special_chars(self):
        aid = _build_archive_id("codex", "!@#$%^&*()_+{}|:<>?")
        self.assertLessEqual(len(aid.encode("utf-8")), 48)


class TestTruncateToBytes(TaskStateTestBase):
    def test_ascii_no_truncation(self):
        self.assertEqual(_truncate_to_bytes("hello", 10), "hello")

    def test_ascii_truncation(self):
        self.assertEqual(_truncate_to_bytes("hello world", 5), "hello")

    def test_chinese_no_split(self):
        # "你好" = 6 bytes; truncating to 5 should give "你" (3 bytes)
        result = _truncate_to_bytes("你好", 5)
        self.assertEqual(result, "你")
        self.assertLessEqual(len(result.encode("utf-8")), 5)

    def test_chinese_exact(self):
        result = _truncate_to_bytes("你好", 6)
        self.assertEqual(result, "你好")


class TestFindArchiveByPrefix(TaskStateTestBase):
    """Test prefix matching in find_archive_entry."""

    def _create_archive(self, task_id="task-pfx-001", text="prefix test"):
        task = self._make_task(task_id, text=text)
        task["task_code"] = "T9999"
        register_task_created(task)
        task["status"] = "accepted"
        task["executor"] = {"last_message": "done"}
        from utils import save_json, tasks_root
        result_path = tasks_root() / "results" / (task_id + ".json")
        save_json(result_path, task)
        return archive_task_result(task, result_path, None)

    def test_exact_match(self):
        entry = self._create_archive()
        aid = entry["archive_id"]
        found = find_archive_entry(aid)
        self.assertIsNotNone(found)
        self.assertEqual(found["archive_id"], aid)

    def test_prefix_match(self):
        entry = self._create_archive()
        aid = entry["archive_id"]
        # Use first 20 chars as prefix
        prefix = aid[:20]
        found = find_archive_entry(prefix)
        self.assertIsNotNone(found)
        self.assertEqual(found["archive_id"], aid)

    def test_prefix_no_match(self):
        self._create_archive()
        found = find_archive_entry("arc-nonexistent")
        self.assertIsNone(found)

    def test_prefix_latest_wins(self):
        """When prefix matches multiple entries, the latest one wins."""
        e1 = self._create_archive("task-dup-1", text="same task")
        e2 = self._create_archive("task-dup-2", text="same task")
        # Both start with "arc-same"
        prefix = "arc-same"
        found = find_archive_entry(prefix)
        self.assertIsNotNone(found)
        # Should return the latest (e2)
        self.assertEqual(found["task_id"], "task-dup-2")


if __name__ == "__main__":
    unittest.main()
