"""AI Output Parser — Extract structured JSON from Claude CLI stdout.

Claude may output mixed text + JSON. This parser extracts the JSON decision block.
Supports schema_version validation.
"""

import json
import logging
import re
from typing import Optional

log = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = "v1"


def parse_ai_output(stdout: str, role: str = "coordinator") -> dict:
    """Extract structured decision JSON from AI stdout.

    Claude output may contain:
    - Pure JSON
    - Markdown code block with JSON
    - Text before/after JSON
    - Multiple JSON blocks (take the last valid one)

    Returns:
        Parsed decision dict, or {"parse_error": "reason"} on failure.
    """
    if not stdout or not stdout.strip():
        return {"parse_error": "empty_output"}

    # Strategy 1: Try parsing entire output as JSON
    try:
        result = json.loads(stdout.strip())
        if isinstance(result, dict):
            return _validate_schema(result, role)
    except (json.JSONDecodeError, ValueError):
        pass

    # Strategy 2: Extract from markdown code block ```json ... ```
    json_blocks = re.findall(r'```(?:json)?\s*\n(.*?)\n```', stdout, re.DOTALL)
    for block in reversed(json_blocks):  # Last block is most likely the decision
        try:
            result = json.loads(block.strip())
            if isinstance(result, dict):
                return _validate_schema(result, role)
        except (json.JSONDecodeError, ValueError):
            continue

    # Strategy 3: Find JSON object by brace matching
    json_str = _extract_json_object(stdout)
    if json_str:
        try:
            result = json.loads(json_str)
            if isinstance(result, dict):
                return _validate_schema(result, role)
        except (json.JSONDecodeError, ValueError):
            pass

    # Strategy 4: AI output is plain text (no JSON) — wrap as reply_only
    return {
        "reply": stdout.strip()[:4000],
        "actions": [],
        "context_update": {},
        "schema_version": CURRENT_SCHEMA_VERSION,
        "_parsed_as": "freeform_text",
    }


def _extract_json_object(text: str) -> Optional[str]:
    """Extract the last complete JSON object from text using brace matching."""
    # Find all { positions
    last_valid = None
    for i in range(len(text) - 1, -1, -1):
        if text[i] == '}':
            # Try to find matching {
            depth = 0
            for j in range(i, -1, -1):
                if text[j] == '}':
                    depth += 1
                elif text[j] == '{':
                    depth -= 1
                    if depth == 0:
                        candidate = text[j:i+1]
                        try:
                            json.loads(candidate)
                            return candidate
                        except (json.JSONDecodeError, ValueError):
                            break
            break
    return None


def _validate_schema(data: dict, role: str) -> dict:
    """Validate and normalize the parsed output."""
    # Ensure required fields
    data.setdefault("reply", "")
    data.setdefault("actions", [])
    data.setdefault("context_update", {})
    data.setdefault("schema_version", CURRENT_SCHEMA_VERSION)

    # Validate schema version
    sv = data.get("schema_version", "")
    if sv and sv != CURRENT_SCHEMA_VERSION:
        log.warning("Schema version mismatch: got %s, expected %s", sv, CURRENT_SCHEMA_VERSION)

    # Normalize actions
    normalized_actions = []
    for action in data.get("actions", []):
        if isinstance(action, dict) and "type" in action:
            action.setdefault("prompt", "")
            action.setdefault("target_files", [])
            action.setdefault("related_nodes", [])
            normalized_actions.append(action)

    data["actions"] = normalized_actions
    data["_parsed_as"] = "structured_json"

    return data
