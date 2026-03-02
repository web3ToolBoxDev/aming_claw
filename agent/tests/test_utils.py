"""Tests for utils.py - core utility functions."""
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

from utils import (  # noqa: E402
    load_json,
    new_task_id,
    save_json,
    shared_root,
    task_file,
    tasks_root,
    utc_iso,
    utc_ts_ms,
)


class TestUtcIso(unittest.TestCase):
    def test_format_is_iso8601(self):
        ts = utc_iso()
        self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_returns_string(self):
        self.assertIsInstance(utc_iso(), str)


class TestUtcTsMs(unittest.TestCase):
    def test_returns_integer(self):
        self.assertIsInstance(utc_ts_ms(), int)

    def test_reasonable_range(self):
        ts = utc_ts_ms()
        self.assertGreater(ts, 1_700_000_000_000)  # after ~2023


class TestNewTaskId(unittest.TestCase):
    def test_prefix(self):
        tid = new_task_id()
        self.assertTrue(tid.startswith("task-"))

    def test_unique(self):
        ids = {new_task_id() for _ in range(100)}
        self.assertEqual(len(ids), 100)

    def test_contains_hex_suffix(self):
        tid = new_task_id()
        parts = tid.split("-")
        self.assertTrue(len(parts) >= 3)
        # last part is a 6-char hex
        self.assertEqual(len(parts[-1]), 6)


class TestSaveLoadJson(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip(self):
        data = {"key": "value", "num": 42, "nested": {"a": [1, 2, 3]}}
        path = Path(self.tmp.name) / "test.json"
        save_json(path, data)
        loaded = load_json(path)
        self.assertEqual(loaded, data)

    def test_creates_parent_dirs(self):
        path = Path(self.tmp.name) / "deep" / "nested" / "dir" / "test.json"
        save_json(path, {"hello": "world"})
        self.assertTrue(path.exists())
        self.assertEqual(load_json(path), {"hello": "world"})

    def test_unicode_support(self):
        data = {"msg": "中文测试", "emoji": "🎉"}
        path = Path(self.tmp.name) / "unicode.json"
        save_json(path, data)
        loaded = load_json(path)
        self.assertEqual(loaded["msg"], "中文测试")
        self.assertEqual(loaded["emoji"], "🎉")

    def test_atomic_write(self):
        """save_json should not leave .tmp files on success."""
        path = Path(self.tmp.name) / "atomic.json"
        save_json(path, {"ok": True})
        tmp_file = path.with_suffix(".json.tmp")
        self.assertFalse(tmp_file.exists())

    def test_overwrite_existing(self):
        path = Path(self.tmp.name) / "overwrite.json"
        save_json(path, {"v": 1})
        save_json(path, {"v": 2})
        self.assertEqual(load_json(path)["v"], 2)


class TestSharedRoot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = os.environ.get("SHARED_VOLUME_PATH")
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        if self._orig is not None:
            os.environ["SHARED_VOLUME_PATH"] = self._orig
        else:
            os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_returns_path(self):
        root = shared_root()
        self.assertIsInstance(root, Path)
        self.assertTrue(root.exists())

    def test_uses_env(self):
        root = shared_root()
        self.assertEqual(str(root), self.tmp.name)


class TestTasksRoot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_creates_subdirs(self):
        root = tasks_root()
        self.assertTrue((root / "pending").is_dir())
        self.assertTrue((root / "processing").is_dir())
        self.assertTrue((root / "results").is_dir())
        self.assertTrue((root / "logs").is_dir())
        self.assertTrue((root / "archive").is_dir())
        self.assertTrue((root / "state").is_dir())


class TestTaskFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_pending_path(self):
        p = task_file("pending", "task-123")
        # Windows uses backslash, normalize for comparison
        normalized = str(p).replace("\\", "/")
        self.assertTrue(normalized.endswith("pending/task-123.json"))

    def test_results_path(self):
        p = task_file("results", "task-456")
        normalized = str(p).replace("\\", "/")
        self.assertTrue(normalized.endswith("results/task-456.json"))


if __name__ == "__main__":
    unittest.main()
