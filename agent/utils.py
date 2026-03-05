import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import requests


def utc_ts_ms() -> int:
    return int(time.time() * 1000)


def utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def shared_root() -> Path:
    root = os.getenv("SHARED_VOLUME_PATH", "").strip()
    if not root:
        # Use repository-relative default, not process cwd, to avoid
        # reading/writing different shared-volume paths when started
        # from another directory.
        root = str((Path(__file__).resolve().parents[1] / "shared-volume").resolve())
    p = Path(root)
    p.mkdir(parents=True, exist_ok=True)
    return p


def tasks_root() -> Path:
    p = shared_root() / "codex-tasks"
    (p / "pending").mkdir(parents=True, exist_ok=True)
    (p / "processing").mkdir(parents=True, exist_ok=True)
    (p / "results").mkdir(parents=True, exist_ok=True)
    (p / "logs").mkdir(parents=True, exist_ok=True)
    (p / "archive").mkdir(parents=True, exist_ok=True)
    (p / "state").mkdir(parents=True, exist_ok=True)
    return p


def task_file(stage: str, task_id: str) -> Path:
    return tasks_root() / stage / (task_id + ".json")


def new_task_id() -> str:
    return "task-" + str(utc_ts_ms()) + "-" + uuid.uuid4().hex[:6]


def save_json(path: Path, obj: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))


def load_json(path: Path) -> Dict:
    return json.loads(path.read_text(encoding="utf-8"))


def telegram_token() -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN_CODEX", "").strip()
    if not token:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("missing TELEGRAM_BOT_TOKEN_CODEX or TELEGRAM_BOT_TOKEN")
    return token


def tg_post(method: str, data: Dict, files: Optional[Dict] = None) -> Dict:
    payload: Dict[str, Any] = {}
    for k, v in (data or {}).items():
        if isinstance(v, (dict, list)):
            payload[k] = json.dumps(v, ensure_ascii=False)
        elif v is None:
            continue
        else:
            payload[k] = str(v)
    token = telegram_token()
    url = "https://api.telegram.org/bot{}/{}".format(token, method)
    resp = requests.post(url, data=payload, files=files, timeout=30)
    try:
        body = resp.json()
    except Exception:
        body = {"ok": False, "status_code": resp.status_code, "text": resp.text[:1000]}
    if resp.status_code >= 400 or not body.get("ok", False):
        raise RuntimeError("telegram {} failed: {}".format(method, body))
    return body


def send_text(
    chat_id: int,
    text: str,
    *,
    parse_mode: str = "",
    reply_markup: Optional[Dict[str, Any]] = None,
    disable_preview: bool = True,
) -> None:
    data: Dict[str, Any] = {"chat_id": str(chat_id), "text": text}
    if parse_mode:
        data["parse_mode"] = parse_mode
    if reply_markup:
        data["reply_markup"] = reply_markup
    if disable_preview:
        data["disable_web_page_preview"] = "true"
    tg_post("sendMessage", data)


def answer_callback_query(callback_query_id: str, text: str = "", show_alert: bool = False) -> None:
    tg_post(
        "answerCallbackQuery",
        {
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": "true" if show_alert else "false",
        },
    )


def download_telegram_file(file_id: str, dest_dir: Path) -> Path:
    """Download a Telegram file by file_id to dest_dir, return local Path."""
    if not file_id or not file_id.strip():
        raise ValueError("file_id must not be empty")
    token = telegram_token()
    # Step 1: getFile to obtain file_path
    url = "https://api.telegram.org/bot{}/getFile".format(token)
    resp = requests.get(url, params={"file_id": file_id}, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError("getFile failed: HTTP {}".format(resp.status_code))
    body = resp.json()
    if not body.get("ok"):
        raise RuntimeError("getFile failed: {}".format(body))
    file_path = body["result"]["file_path"]
    # Step 2: download binary
    dl_url = "https://api.telegram.org/file/bot{}/{}".format(token, file_path)
    dl_resp = requests.get(dl_url, timeout=30)
    if dl_resp.status_code != 200:
        raise RuntimeError("file download failed: HTTP {}".format(dl_resp.status_code))
    content_type = dl_resp.headers.get("Content-Type", "")
    if content_type and not content_type.startswith("image/"):
        raise RuntimeError("unexpected content type: {}".format(content_type))
    # Determine extension from file_path
    ext = Path(file_path).suffix or ".jpg"
    dest_dir.mkdir(parents=True, exist_ok=True)
    local_name = uuid.uuid4().hex[:12] + ext
    local_path = dest_dir / local_name
    local_path.write_bytes(dl_resp.content)
    return local_path


def extract_photos_from_message(msg: dict) -> list:
    """Extract photo info from a Telegram message. Returns list of dicts with file_id, width, height."""
    photos = msg.get("photo")
    if not photos or not isinstance(photos, list):
        return []
    # Take the largest resolution (last element)
    best = photos[-1]
    return [{
        "file_id": best.get("file_id", ""),
        "file_unique_id": best.get("file_unique_id", ""),
        "width": best.get("width", 0),
        "height": best.get("height", 0),
    }]


def send_document(chat_id: int, path: Path, caption: str = "") -> None:
    with path.open("rb") as f:
        tg_post(
            "sendDocument",
            {"chat_id": str(chat_id), "caption": caption},
            files={"document": f},
        )
