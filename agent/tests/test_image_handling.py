"""Tests for image handling in task and chat modes."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from utils import extract_photos_from_message  # noqa: E402


class TestExtractPhotosFromMessage(unittest.TestCase):
    """Test extract_photos_from_message utility."""

    def test_no_photo_returns_empty(self):
        msg = {"text": "hello"}
        self.assertEqual(extract_photos_from_message(msg), [])

    def test_empty_photo_list_returns_empty(self):
        msg = {"photo": []}
        self.assertEqual(extract_photos_from_message(msg), [])

    def test_photo_none_returns_empty(self):
        msg = {"photo": None}
        self.assertEqual(extract_photos_from_message(msg), [])

    def test_single_photo_extracted(self):
        msg = {
            "photo": [
                {"file_id": "small_id", "file_unique_id": "su", "width": 90, "height": 90},
            ]
        }
        result = extract_photos_from_message(msg)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["file_id"], "small_id")
        self.assertEqual(result[0]["width"], 90)

    def test_multiple_sizes_picks_largest(self):
        msg = {
            "photo": [
                {"file_id": "small", "file_unique_id": "s", "width": 90, "height": 90},
                {"file_id": "medium", "file_unique_id": "m", "width": 320, "height": 320},
                {"file_id": "large", "file_unique_id": "l", "width": 1280, "height": 720},
            ]
        }
        result = extract_photos_from_message(msg)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["file_id"], "large")
        self.assertEqual(result[0]["width"], 1280)
        self.assertEqual(result[0]["height"], 720)

    def test_missing_fields_default_zero(self):
        msg = {"photo": [{"file_id": "abc"}]}
        result = extract_photos_from_message(msg)
        self.assertEqual(result[0]["width"], 0)
        self.assertEqual(result[0]["height"], 0)
        self.assertEqual(result[0]["file_unique_id"], "")


class TestDownloadTelegramFile(unittest.TestCase):
    """Test download_telegram_file utility (mocked HTTP)."""

    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN_CODEX": "test-token-123"})
    @patch("utils.requests.get")
    def test_successful_download(self, mock_get):
        # Mock getFile response
        get_file_resp = MagicMock()
        get_file_resp.status_code = 200
        get_file_resp.json.return_value = {
            "ok": True,
            "result": {"file_path": "photos/file_0.jpg"},
        }
        # Mock file download response
        dl_resp = MagicMock()
        dl_resp.status_code = 200
        dl_resp.headers = {"Content-Type": "image/jpeg"}
        dl_resp.content = b"\xff\xd8\xff\xe0fake-jpeg-data"
        mock_get.side_effect = [get_file_resp, dl_resp]

        with tempfile.TemporaryDirectory() as tmpdir:
            from utils import download_telegram_file
            result = download_telegram_file("test_file_id", Path(tmpdir))
            self.assertTrue(result.exists())
            self.assertTrue(result.suffix == ".jpg")
            self.assertEqual(result.read_bytes(), b"\xff\xd8\xff\xe0fake-jpeg-data")

    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN_CODEX": "test-token-123"})
    @patch("utils.requests.get")
    def test_getfile_http_error(self, mock_get):
        resp = MagicMock()
        resp.status_code = 400
        mock_get.return_value = resp

        with tempfile.TemporaryDirectory() as tmpdir:
            from utils import download_telegram_file
            with self.assertRaises(RuntimeError) as ctx:
                download_telegram_file("bad_id", Path(tmpdir))
            self.assertIn("HTTP 400", str(ctx.exception))

    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN_CODEX": "test-token-123"})
    @patch("utils.requests.get")
    def test_download_http_error(self, mock_get):
        get_file_resp = MagicMock()
        get_file_resp.status_code = 200
        get_file_resp.json.return_value = {
            "ok": True,
            "result": {"file_path": "photos/file_0.jpg"},
        }
        dl_resp = MagicMock()
        dl_resp.status_code = 404
        mock_get.side_effect = [get_file_resp, dl_resp]

        with tempfile.TemporaryDirectory() as tmpdir:
            from utils import download_telegram_file
            with self.assertRaises(RuntimeError) as ctx:
                download_telegram_file("test_id", Path(tmpdir))
            self.assertIn("HTTP 404", str(ctx.exception))

    def test_empty_file_id_raises(self):
        from utils import download_telegram_file
        with self.assertRaises(ValueError):
            download_telegram_file("", Path("/tmp"))
        with self.assertRaises(ValueError):
            download_telegram_file("  ", Path("/tmp"))


class TestCreateTaskWithImages(unittest.TestCase):
    """Test create_task includes images field."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmpdir.name
        os.environ["TELEGRAM_BOT_TOKEN_CODEX"] = "test-token"

    def tearDown(self):
        self.tmpdir.cleanup()
        os.environ.pop("SHARED_VOLUME_PATH", None)
        os.environ.pop("TELEGRAM_BOT_TOKEN_CODEX", None)

    @patch("bot_commands.download_telegram_file")
    @patch("bot_commands.register_task_created", return_value="T0001")
    def test_task_with_no_photos_has_empty_images(self, mock_reg, mock_dl):
        from bot_commands import create_task
        task_id = create_task(123, 456, "/task fix bug")
        from utils import load_json, task_file
        task = load_json(task_file("pending", task_id))
        self.assertEqual(task.get("images"), [])
        mock_dl.assert_not_called()

    @patch("bot_commands.download_telegram_file")
    @patch("bot_commands.register_task_created", return_value="T0002")
    def test_task_with_photos_downloads_and_stores(self, mock_reg, mock_dl):
        # Setup mock download
        fake_path = Path(self.tmpdir.name) / "fake_img.jpg"
        fake_path.write_bytes(b"fake")
        mock_dl.return_value = fake_path

        photos = [{"file_id": "abc123", "file_unique_id": "u1", "width": 800, "height": 600}]
        from bot_commands import create_task
        task_id = create_task(123, 456, "/task fix layout", photos=photos)
        from utils import load_json, task_file
        task = load_json(task_file("pending", task_id))
        self.assertEqual(len(task["images"]), 1)
        self.assertEqual(task["images"][0]["file_id"], "abc123")
        self.assertEqual(task["images"][0]["width"], 800)

    @patch("bot_commands.download_telegram_file")
    @patch("bot_commands.register_task_created", return_value="T0003")
    def test_task_photo_no_caption_uses_default_text(self, mock_reg, mock_dl):
        fake_path = Path(self.tmpdir.name) / "img.jpg"
        fake_path.write_bytes(b"fake")
        mock_dl.return_value = fake_path

        photos = [{"file_id": "x", "file_unique_id": "u", "width": 100, "height": 100}]
        from bot_commands import create_task
        # Empty text (no caption, no /task prefix)
        task_id = create_task(123, 456, "", photos=photos)
        from utils import load_json, task_file
        task = load_json(task_file("pending", task_id))
        self.assertIn("请根据图片内容执行任务", task["text"])


class TestBuildPromptWithImages(unittest.TestCase):
    """Test prompt builders include image hints."""

    def test_codex_prompt_no_images(self):
        from backends import build_codex_prompt
        task = {"task_id": "test-001", "text": "hello", "images": []}
        prompt = build_codex_prompt(task)
        self.assertNotIn("附件", prompt)

    def test_codex_prompt_with_images(self):
        from backends import build_codex_prompt
        task = {
            "task_id": "test-002",
            "text": "fix bug",
            "images": [{"file_id": "a", "local_path": "/tmp/a.jpg", "width": 100, "height": 100}],
        }
        prompt = build_codex_prompt(task)
        self.assertIn("附件", prompt)
        self.assertIn("图片 1 张", prompt)

    def test_claude_prompt_with_images(self):
        from backends import build_claude_prompt
        task = {
            "task_id": "test-003",
            "text": "review",
            "images": [
                {"file_id": "a", "local_path": "/tmp/a.jpg", "width": 100, "height": 100},
                {"file_id": "b", "local_path": "/tmp/b.jpg", "width": 200, "height": 200},
            ],
        }
        prompt = build_claude_prompt(task)
        self.assertIn("图片 2 张", prompt)

    def test_claude_prompt_no_images_key(self):
        """Task without images key (backward compat) should not crash."""
        from backends import build_claude_prompt
        task = {"task_id": "test-004", "text": "old task"}
        prompt = build_claude_prompt(task)
        self.assertNotIn("附件", prompt)


class TestEncodeImageBase64(unittest.TestCase):
    def test_encode_image(self):
        import base64
        from backends import encode_image_base64
        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = Path(tmpdir) / "test.jpg"
            fpath.write_bytes(b"test-image-data")
            result = encode_image_base64(str(fpath))
            self.assertEqual(base64.b64decode(result), b"test-image-data")


class TestHandlePendingActionWithPhotos(unittest.TestCase):
    """Test handle_pending_action handles photo input."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmpdir.name
        os.environ["TELEGRAM_BOT_TOKEN_CODEX"] = "test-token"

    def tearDown(self):
        self.tmpdir.cleanup()
        os.environ.pop("SHARED_VOLUME_PATH", None)
        os.environ.pop("TELEGRAM_BOT_TOKEN_CODEX", None)

    @patch("bot_commands.send_text")
    def test_text_only_action_rejects_photo(self, mock_send):
        from bot_commands import set_pending_action, handle_pending_action
        set_pending_action(100, 200, "screenshot")
        photos = [{"file_id": "x", "file_unique_id": "u", "width": 100, "height": 100}]
        result = handle_pending_action(100, 200, "", photos=photos)
        self.assertTrue(result)
        mock_send.assert_called()
        call_text = mock_send.call_args[0][1]
        self.assertIn("仅支持文本", call_text)

    @patch("bot_commands.download_telegram_file")
    @patch("bot_commands.register_task_created", return_value="T0010")
    @patch("bot_commands.send_text")
    def test_new_task_with_photo(self, mock_send, mock_reg, mock_dl):
        fake_path = Path(self.tmpdir.name) / "img.jpg"
        fake_path.write_bytes(b"fake")
        mock_dl.return_value = fake_path

        from bot_commands import set_pending_action, handle_pending_action
        set_pending_action(100, 200, "new_task")
        photos = [{"file_id": "abc", "file_unique_id": "u", "width": 640, "height": 480}]
        result = handle_pending_action(100, 200, "fix this", photos=photos)
        self.assertTrue(result)
        # Verify task was created with images
        from utils import tasks_root
        import glob
        pending_files = list((tasks_root() / "pending").glob("*.json"))
        self.assertTrue(len(pending_files) >= 1)


class TestRunClaudeChatWithImage(unittest.TestCase):
    """Test run_claude_chat_with_image function."""

    @patch("bot_commands.subprocess.run")
    @patch("bot_commands.get_claude_model", return_value="claude-sonnet-4-20250514")
    def test_calls_claude_with_image_flags(self, mock_model, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "This image shows a login page."
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        from bot_commands import run_claude_chat_with_image
        result = run_claude_chat_with_image("describe this", ["/tmp/img1.jpg", "/tmp/img2.jpg"])
        self.assertIn("login page", result)
        # Verify --image flags were passed
        cmd = mock_run.call_args[0][0]
        image_indices = [i for i, arg in enumerate(cmd) if arg == "--image"]
        self.assertEqual(len(image_indices), 2)

    @patch("bot_commands.subprocess.run")
    @patch("bot_commands.get_claude_model", return_value="")
    def test_timeout_raises(self, mock_model, mock_run):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=300)
        from bot_commands import run_claude_chat_with_image
        with self.assertRaises(RuntimeError) as ctx:
            run_claude_chat_with_image("test", ["/tmp/img.jpg"])
        self.assertIn("timeout", str(ctx.exception))


class TestImageAttachmentHint(unittest.TestCase):
    """Test _image_attachment_hint helper."""

    def test_no_images(self):
        from backends import _image_attachment_hint
        self.assertEqual(_image_attachment_hint({}), "")
        self.assertEqual(_image_attachment_hint({"images": []}), "")

    def test_with_images(self):
        from backends import _image_attachment_hint
        task = {"images": [{"file_id": "a"}, {"file_id": "b"}]}
        hint = _image_attachment_hint(task)
        self.assertIn("图片 2 张", hint)
        self.assertIn("attachments/", hint)


if __name__ == "__main__":
    unittest.main()
