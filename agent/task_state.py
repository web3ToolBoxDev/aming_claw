import json
import re
import time
import uuid
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils import load_json, save_json, tasks_root, utc_iso


def _runtime_state_file() -> Path:
    return tasks_root() / "state" / "task_runtime_state.json"


def _task_state_root() -> Path:
    p = tasks_root() / "state" / "task_state"
    p.mkdir(parents=True, exist_ok=True)
    return p


def task_state_dir(task_id: str) -> Path:
    p = _task_state_root() / str(task_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def task_status_file(task_id: str) -> Path:
    return task_state_dir(task_id) / "status.json"


def task_events_file(task_id: str) -> Path:
    return task_state_dir(task_id) / "events.jsonl"


def read_task_events(task_id: str, limit: int = 20) -> List[Dict]:
    p = task_events_file(task_id)
    if not p.exists():
        return []
    rows = p.read_text(encoding="utf-8").splitlines()
    out: List[Dict] = []
    for line in rows:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    if limit > 0:
        return out[-limit:]
    return out


def _archive_root() -> Path:
    p = tasks_root() / "archive"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _archive_index_file() -> Path:
    return _archive_root() / "archive_index.jsonl"


def _default_runtime_state() -> Dict:
    return {
        "next_code": 1,
        "aliases": {},
        "active": {},
        "updated_at": utc_iso(),
    }


def _default_task_status(task: Optional[Dict] = None) -> Dict:
    task = task or {}
    return {
        "task_id": str(task.get("task_id") or ""),
        "task_code": str(task.get("task_code") or ""),
        "chat_id": int(task.get("chat_id") or 0),
        "requested_by": int(task.get("requested_by") or 0),
        "action": str(task.get("action") or "codex"),
        "text": str(task.get("text") or ""),
        "status": str(task.get("status") or "pending"),
        "stage": str(task.get("stage") or "pending"),
        "created_at": str(task.get("created_at") or utc_iso()),
        "updated_at": utc_iso(),
        "started_at": str(task.get("started_at") or ""),
        "ended_at": str(task.get("ended_at") or ""),
        "has_end_marker": False,
        "result_file": "",
        "runlog_file": "",
        "summary": "",
        "error": "",
        "completion_notified_at": "",
        # State machine required fields
        "progress": 0,
        "worker_id": str(task.get("worker_id") or ""),
        "attempt": int(task.get("attempt") or 0),
        "heartbeat_at": "",
    }


def load_runtime_state() -> Dict:
    path = _runtime_state_file()
    if not path.exists():
        return _default_runtime_state()
    try:
        data = load_json(path)
    except Exception:
        return _default_runtime_state()
    if not isinstance(data, dict):
        return _default_runtime_state()
    out = _default_runtime_state()
    out["next_code"] = int(data.get("next_code", 1) or 1)
    out["aliases"] = data.get("aliases") if isinstance(data.get("aliases"), dict) else {}
    out["active"] = data.get("active") if isinstance(data.get("active"), dict) else {}
    out["updated_at"] = str(data.get("updated_at") or utc_iso())
    return out


def save_runtime_state(state: Dict) -> None:
    state["updated_at"] = utc_iso()
    save_json(_runtime_state_file(), state)


def load_task_status(task_id: str) -> Optional[Dict]:
    p = task_status_file(task_id)
    if not p.exists():
        return None
    try:
        obj = load_json(p)
    except Exception:
        return None
    if isinstance(obj, dict):
        return obj
    return None


def save_task_status(task_id: str, status_obj: Dict) -> Dict:
    # Build defaults once, then overlay with provided values
    obj = _default_task_status()
    if status_obj:
        obj.update(status_obj)
    obj["task_id"] = str(task_id)
    obj["updated_at"] = utc_iso()
    save_json(task_status_file(task_id), obj)
    return obj


def append_task_event(task_id: str, event: str, data: Optional[Dict[str, Any]] = None) -> None:
    row = {
        "task_id": str(task_id),
        "event": str(event),
        "ts": utc_iso(),
        "data": data or {},
    }
    p = task_events_file(task_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def init_task_lifecycle(task: Dict) -> Dict:
    task_id = str(task.get("task_id") or "").strip()
    if not task_id:
        raise RuntimeError("task_id is required")
    base = _default_task_status(task)
    base["status"] = str(task.get("status") or "pending")
    base["stage"] = "pending"
    obj = save_task_status(task_id, base)
    append_task_event(task_id, "task_created", {
        "status": obj.get("status"),
        "stage": obj.get("stage"),
        "task_code": obj.get("task_code", ""),
    })
    return obj


def update_task_lifecycle(task: Dict, *, status: Optional[str] = None, stage: Optional[str] = None, extra: Optional[Dict] = None) -> Dict:
    task_id = str(task.get("task_id") or "").strip()
    if not task_id:
        raise RuntimeError("task_id is required")
    current = load_task_status(task_id) or _default_task_status(task)
    current.update(
        {
            "task_code": str(task.get("task_code") or current.get("task_code") or ""),
            "chat_id": int(task.get("chat_id") or current.get("chat_id") or 0),
            "requested_by": int(task.get("requested_by") or current.get("requested_by") or 0),
            "action": str(task.get("action") or current.get("action") or "codex"),
            "text": str(task.get("text") or current.get("text") or ""),
            "created_at": str(task.get("created_at") or current.get("created_at") or utc_iso()),
        }
    )
    if status is not None:
        current["status"] = str(status)
    if stage is not None:
        current["stage"] = str(stage)
    if extra:
        current.update(extra)
    return save_task_status(task_id, current)


def mark_task_started(task: Dict, stage: str = "processing") -> Dict:
    now = utc_iso()
    obj = update_task_lifecycle(
        task,
        status="processing",
        stage=stage,
        extra={"started_at": str(task.get("started_at") or now) or now},
    )
    append_task_event(
        str(task.get("task_id") or ""),
        "task_started",
        {
            "status": obj.get("status"),
            "stage": obj.get("stage"),
            "started_at": obj.get("started_at"),
        },
    )
    return obj


def mark_task_finished(
    task: Dict,
    *,
    status: str,
    stage: str = "results",
    result_file: str = "",
    runlog_file: str = "",
    summary: str = "",
    error: str = "",
) -> Dict:
    now = utc_iso()
    obj = update_task_lifecycle(
        task,
        status=status,
        stage=stage,
        extra={
            "ended_at": now,
            "has_end_marker": True,
            "result_file": str(result_file or ""),
            "runlog_file": str(runlog_file or ""),
            "summary": str(summary or ""),
            "error": str(error or ""),
        },
    )
    append_task_event(
        str(task.get("task_id") or ""),
        "task_finished",
        {
            "status": obj.get("status"),
            "stage": obj.get("stage"),
            "ended_at": obj.get("ended_at"),
            "has_end_marker": obj.get("has_end_marker"),
            "result_file": obj.get("result_file"),
            "runlog_file": obj.get("runlog_file"),
        },
    )
    return obj


def mark_task_completion_notified(task_id: str) -> Dict:
    existing = load_task_status(task_id) or _default_task_status({"task_id": task_id})
    existing["completion_notified_at"] = utc_iso()
    obj = save_task_status(task_id, existing)
    append_task_event(task_id, "completion_notified", {"completion_notified_at": obj.get("completion_notified_at")})
    return obj


def update_task_heartbeat(task_id: str, progress: int = 0) -> None:
    """Executor calls this periodically to prove liveness. Coordinator uses
    heartbeat_at to detect stuck tasks and mark them as timeout."""
    existing = load_task_status(task_id)
    if existing is None:
        return
    existing["heartbeat_at"] = utc_iso()
    if progress:
        existing["progress"] = max(int(existing.get("progress") or 0), progress)
    save_json(task_status_file(task_id), existing)


def mark_task_timeout(task_id: str, chat_id: int) -> Dict:
    existing = load_task_status(task_id) or _default_task_status({"task_id": task_id, "chat_id": chat_id})
    existing["status"] = "timeout"
    existing["ended_at"] = utc_iso()
    existing["has_end_marker"] = True
    existing["error"] = "task timed out (no heartbeat)"
    obj = save_task_status(task_id, existing)
    append_task_event(task_id, "task_timeout", {"heartbeat_at": existing.get("heartbeat_at", "")})
    return obj


def list_task_state_candidates() -> List[Dict]:
    root = _task_state_root()
    out: List[Dict] = []
    for p in root.glob("*/status.json"):
        try:
            obj = load_json(p)
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    out.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
    return out


def _new_task_code(state: Dict) -> str:
    next_code = int(state.get("next_code", 1) or 1)
    while True:
        code = "T{:04d}".format(next_code)
        if code not in (state.get("aliases") or {}):
            state["next_code"] = next_code + 1
            return code
        next_code += 1


def register_task_created(task: Dict) -> str:
    state = load_runtime_state()
    task_id = str(task.get("task_id") or "").strip()
    if not task_id:
        raise RuntimeError("task_id is required")
    task_code = str(task.get("task_code") or "").strip()
    if not task_code:
        task_code = _new_task_code(state)
    aliases = state.get("aliases") or {}
    aliases[task_code] = task_id
    aliases[task_code.upper()] = task_id
    state["aliases"] = aliases
    state["active"][task_id] = {
        "task_id": task_id,
        "task_code": task_code,
        "chat_id": int(task.get("chat_id") or 0),
        "action": task.get("action", "codex"),
        "text": task.get("text", ""),
        "status": task.get("status", "pending"),
        "stage": "pending",
        "created_at": task.get("created_at") or utc_iso(),
        "updated_at": utc_iso(),
    }
    save_runtime_state(state)
    task_local = dict(task)
    task_local["task_code"] = task_code
    init_task_lifecycle(task_local)
    return task_code


def resolve_task_ref(task_ref: str) -> Optional[str]:
    ref = str(task_ref or "").strip()
    if not ref:
        return None
    state = load_runtime_state()
    aliases = state.get("aliases") or {}
    normalized = ref.upper()
    if ref in aliases:
        return str(aliases.get(ref) or "")
    if normalized in aliases:
        return str(aliases.get(normalized) or "")
    if ref.startswith("task-"):
        return ref
    return None


def list_active_tasks(chat_id: Optional[int] = None) -> List[Dict]:
    state = load_runtime_state()
    items: List[Dict] = []
    for entry in (state.get("active") or {}).values():
        if not isinstance(entry, dict):
            continue
        if chat_id is not None and int(entry.get("chat_id") or 0) != int(chat_id):
            continue
        items.append(entry)
    items.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
    return items


def clear_active_tasks(chat_id: int) -> int:
    """Remove non-running active tasks for a given chat_id.

    Keeps tasks whose status is 'processing' (运行中).
    Removes tasks with status 'pending_acceptance' (待验收),
    'accepted'/'rejected'/'completed'/'failed' (已归档/已完成), etc.
    Returns count of removed tasks.
    """
    state = load_runtime_state()
    active = state.get("active") or {}
    keep_statuses = {"processing"}
    to_remove: List[str] = []
    for tid, entry in active.items():
        if not isinstance(entry, dict):
            continue
        if int(entry.get("chat_id") or 0) != int(chat_id):
            continue
        # Check latest status from task_state snapshot first, fall back to active entry
        latest = load_task_status(tid)
        status = str((latest or entry).get("status") or entry.get("status") or "").strip().lower()
        if status in keep_statuses:
            continue
        to_remove.append(tid)
    for tid in to_remove:
        del active[tid]
    state["active"] = active
    save_runtime_state(state)
    return len(to_remove)


def update_task_runtime(task: Dict, status: str, stage: str) -> None:
    task_id = str(task.get("task_id") or "").strip()
    if not task_id:
        return
    state = load_runtime_state()
    active = state.get("active") or {}
    entry = active.get(task_id) if isinstance(active.get(task_id), dict) else {}
    if not entry:
        entry = {
            "task_id": task_id,
            "task_code": task.get("task_code", ""),
            "chat_id": int(task.get("chat_id") or 0),
            "action": task.get("action", "codex"),
            "text": task.get("text", ""),
            "created_at": task.get("created_at") or utc_iso(),
        }
    entry["status"] = status
    entry["stage"] = stage
    entry["updated_at"] = utc_iso()
    if task.get("task_code"):
        entry["task_code"] = str(task.get("task_code"))
        aliases = state.get("aliases") or {}
        code = str(task.get("task_code"))
        aliases[code] = task_id
        aliases[code.upper()] = task_id
        state["aliases"] = aliases
    active[task_id] = entry
    state["active"] = active
    save_runtime_state(state)
    update_task_lifecycle(task, status=status, stage=stage)


def _semantic_slug(text: str, action: str) -> str:
    raw = (text or "").strip().lower()
    cleaned = re.sub(r"[^\w]+", "-", raw, flags=re.UNICODE).strip("-_")
    if not cleaned:
        cleaned = action or "task"
    parts = [p for p in cleaned.split("-") if p]
    stem = "-".join(parts[:3])[:48]
    return stem or (action or "task")


def _build_archive_id(action: str, text: str) -> str:
    date_part = time.strftime("%Y%m%d", time.gmtime())
    stem = _semantic_slug(text, action)
    return "arc-{}-{}-{}".format(stem, date_part, uuid.uuid4().hex[:6])


def _append_archive_index(entry: Dict) -> None:
    path = _archive_index_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _task_summary(task: Dict) -> str:
    executor = task.get("executor") or {}
    msg = str(executor.get("last_message") or "").strip()
    if msg:
        return msg[:240]
    noop_reason = str(executor.get("noop_reason") or "").strip()
    if noop_reason:
        return ("失败原因: " + noop_reason)[:240]
    err = str(task.get("error") or "").strip()
    if err:
        return ("错误: " + err)[:240]
    return "(无概要)"


def archive_task_result(task: Dict, result_path: Path, run_log_path: Optional[Path]) -> Dict:
    archive_id = _build_archive_id(str(task.get("action") or "task"), str(task.get("text") or ""))
    archive_file = _archive_root() / (archive_id + ".json")
    task_id = str(task.get("task_id") or "")
    task_code = str(task.get("task_code") or "")
    summary = _task_summary(task)
    entry = {
        "archive_id": archive_id,
        "task_id": task_id,
        "task_code": task_code,
        "chat_id": int(task.get("chat_id") or 0),
        "action": task.get("action", "codex"),
        "status": task.get("status", "unknown"),
        "text": task.get("text", ""),
        "summary": summary,
        "created_at": task.get("created_at", ""),
        "completed_at": task.get("completed_at", ""),
        "updated_at": utc_iso(),
        "result_file": str(result_path),
        "run_log_file": str(run_log_path) if run_log_path else "",
        "archive_file": str(archive_file),
    }
    save_json(archive_file, entry)
    _append_archive_index(entry)

    state = load_runtime_state()
    active = state.get("active") or {}
    if task_id in active:
        del active[task_id]
        state["active"] = active
    save_runtime_state(state)
    mark_task_finished(
        task,
        status=str(task.get("status") or "accepted"),
        stage="archive",
        result_file=str(result_path),
        runlog_file=str(run_log_path) if run_log_path else "",
        summary=summary,
        error=str(task.get("error") or ""),
    )
    return entry


def _read_archive_index() -> List[Dict]:
    path = _archive_index_file()
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    out: List[Dict] = []
    for line in lines:
        row = line.strip()
        if not row:
            continue
        try:
            obj = json.loads(row)
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def find_archive_entry(task_ref: str) -> Optional[Dict]:
    ref = str(task_ref or "").strip()
    if not ref:
        return None
    entries = _read_archive_index()
    for item in reversed(entries):
        if (
            ref == str(item.get("archive_id") or "")
            or ref == str(item.get("task_id") or "")
            or ref == str(item.get("task_code") or "")
        ):
            return item
    return None


def search_archive_entries(query: str, limit: int = 20) -> List[Dict]:
    q = str(query or "").strip().lower()
    entries = _read_archive_index()
    if not q:
        return list(reversed(entries))[:limit]
    tokens = [x for x in re.split(r"\s+", q) if x]
    scored: List[Dict] = []
    for item in reversed(entries):
        searchable = " ".join(
            [
                str(item.get("archive_id") or ""),
                str(item.get("task_id") or ""),
                str(item.get("task_code") or ""),
                str(item.get("action") or ""),
                str(item.get("text") or ""),
                str(item.get("summary") or ""),
            ]
        ).lower()
        token_hits = sum(1 for tok in tokens if tok in searchable) if tokens else 0
        if tokens and token_hits == 0:
            continue
        ratio = SequenceMatcher(None, q, searchable[:500]).ratio()
        score = token_hits * 2.0 + ratio
        if str(item.get("archive_id") or "").lower() == q:
            score += 10
        elif str(item.get("task_id") or "").lower() == q or str(item.get("task_code") or "").lower() == q:
            score += 8
        scored.append({"score": score, "item": item})
    scored.sort(key=lambda x: float(x.get("score") or 0), reverse=True)
    out: List[Dict] = []
    for row in scored[:limit]:
        obj = row.get("item")
        if isinstance(obj, dict):
            out.append(obj)
    return out


def group_archive_entries(items: List[Dict], limit_per_group: int = 5) -> Dict[str, Dict]:
    grouped: Dict[str, Dict] = {}
    for item in items:
        action = str(item.get("action") or "unknown")
        if action not in grouped:
            grouped[action] = {"count": 0, "items": []}
        grouped[action]["count"] += 1
        if len(grouped[action]["items"]) < limit_per_group:
            grouped[action]["items"].append(item)
    return grouped


def grouped_archive_overview(limit_per_group: int = 5) -> Dict[str, Dict]:
    entries = list(reversed(_read_archive_index()))
    grouped: Dict[str, Dict] = {}
    for item in entries:
        action = str(item.get("action") or "unknown")
        if action not in grouped:
            grouped[action] = {"count": 0, "items": []}
        grouped[action]["count"] += 1
        if len(grouped[action]["items"]) < limit_per_group:
            grouped[action]["items"].append(item)
    return grouped
