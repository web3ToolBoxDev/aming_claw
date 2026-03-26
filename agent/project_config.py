"""
project_config.py — Multi-project configuration loader.

Any project can register with the auto-chain workflow by providing a
.aming-claw.yaml (or .aming-claw.json) file at its workspace root.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# sys.path bootstrap (mirrors other agent modules)
# ---------------------------------------------------------------------------
_agent_dir = os.path.dirname(os.path.abspath(__file__))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TestingConfig:
    unit_command: str = "python -m pytest"
    e2e_command: str = ""
    allowed_commands: List[Dict[str, Any]] = field(default_factory=list)
    """Each entry: {"executable": str, "args_prefixes": list[str]}"""


@dataclass
class BuildConfig:
    command: str = ""
    release_checks: List[str] = field(default_factory=list)


@dataclass
class ServiceRule:
    patterns: List[str] = field(default_factory=list)
    """Glob patterns (forward-slash normalised) that trigger this service."""
    services: List[str] = field(default_factory=list)
    """Service names that should be restarted / reloaded."""


@dataclass
class SmokeCheck:
    name: str = ""
    url: str = ""
    expected_status: int = 200
    timeout: int = 10


@dataclass
class DeployConfig:
    strategy: str = "none"
    """One of: docker | electron | systemd | process | none"""
    service_rules: List[ServiceRule] = field(default_factory=list)
    commands: Dict[str, str] = field(default_factory=dict)
    smoke_checks: List[SmokeCheck] = field(default_factory=list)


@dataclass
class GovernanceConfig:
    enabled: bool = False
    test_tool_label: str = "pytest"


@dataclass
class ProjectConfig:
    project_id: str = ""
    language: str = "python"
    testing: TestingConfig = field(default_factory=TestingConfig)
    build: BuildConfig = field(default_factory=BuildConfig)
    deploy: DeployConfig = field(default_factory=DeployConfig)
    governance: GovernanceConfig = field(default_factory=GovernanceConfig)


# ---------------------------------------------------------------------------
# DEFAULT_CONFIG — aming-claw hardcoded fallback
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = ProjectConfig(
    project_id="aming-claw",
    language="python",
    testing=TestingConfig(
        unit_command="python -m unittest discover -s agent/tests -p 'test_*.py' -v",
        e2e_command="",
        allowed_commands=[
            {"executable": "python", "args_prefixes": ["-m unittest", "-m pytest"]},
            {"executable": "pytest", "args_prefixes": []},
        ],
    ),
    build=BuildConfig(
        command="",
        release_checks=[],
    ),
    deploy=DeployConfig(
        strategy="docker",
        service_rules=[
            ServiceRule(
                patterns=["agent/telegram_gateway/**"],
                services=["gateway"],
            ),
        ],
        commands={
            "restart_gateway": "docker compose restart telegram-gateway",
            "logs_gateway": "docker compose logs --tail 30 telegram-gateway",
        },
        smoke_checks=[
            SmokeCheck(
                name="executor-api",
                url="http://localhost:40100/status",
                expected_status=200,
                timeout=5,
            ),
            SmokeCheck(
                name="governance",
                url="http://localhost:40000/api/health",
                expected_status=200,
                timeout=5,
            ),
            SmokeCheck(
                name="container-running",
                url="",
                expected_status=0,
                timeout=5,
            ),
        ],
    ),
    governance=GovernanceConfig(
        enabled=True,
        test_tool_label="pytest",
    ),
)
"""Hardcoded fallback for the aming-claw project itself.

Used ONLY when no .aming-claw.yaml / .aming-claw.json is found at the
workspace root.  A deprecation warning is emitted whenever this fallback
is active so projects are encouraged to migrate to an explicit config file.
"""

# ---------------------------------------------------------------------------
# Command-safety helpers
# ---------------------------------------------------------------------------

_SHELL_METACHARACTERS = (";", "&&", "|", "`")


def validate_commands(config: ProjectConfig) -> List[str]:
    """Return a list of violation messages for unsafe shell metacharacters.

    Checks all command strings in testing, build, and deploy sections.
    """
    violations: List[str] = []

    def _check(label: str, value: str) -> None:
        for meta in _SHELL_METACHARACTERS:
            if meta in value:
                violations.append(
                    f"Command '{label}' contains unsafe metacharacter '{meta}': {value!r}"
                )

    _check("testing.unit_command", config.testing.unit_command)
    _check("testing.e2e_command", config.testing.e2e_command)
    _check("build.command", config.build.command)
    for key, cmd in config.deploy.commands.items():
        _check(f"deploy.commands.{key}", cmd)

    return violations


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = ("project_id", "language")
_KNOWN_TOP_LEVEL = {
    "project_id",
    "language",
    "testing",
    "build",
    "deploy",
    "governance",
}
_VALID_STRATEGIES = {"docker", "electron", "systemd", "process", "none"}
_KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def validate_project_config(raw: dict) -> Tuple[bool, List[str]]:
    """Validate raw config dict.

    Returns (is_valid, messages) where messages may include errors and
    warnings.  is_valid is False if any error is present.
    """
    errors: List[str] = []
    warnings: List[str] = []

    # Required fields
    for f in _REQUIRED_FIELDS:
        if f not in raw:
            errors.append(f"Missing required field: '{f}'")

    # kebab-case project_id
    pid = raw.get("project_id", "")
    if pid and not _KEBAB_RE.match(pid):
        errors.append(
            f"project_id must be kebab-case (lowercase letters, digits, hyphens): got {pid!r}"
        )

    # Deploy strategy
    deploy = raw.get("deploy", {})
    if isinstance(deploy, dict):
        strategy = deploy.get("strategy", "none")
        if strategy not in _VALID_STRATEGIES:
            errors.append(
                f"deploy.strategy must be one of {sorted(_VALID_STRATEGIES)}: got {strategy!r}"
            )

    # Unknown top-level fields → warnings
    for key in raw:
        if key not in _KNOWN_TOP_LEVEL:
            warnings.append(f"Unknown top-level field (ignored): '{key}'")

    # Shell metacharacter safety — build a temporary config for checking
    if not errors:
        try:
            tmp = _parse_raw(raw)
            cmd_violations = validate_commands(tmp)
            errors.extend(cmd_violations)
        except Exception as exc:
            warnings.append(f"Could not run command-safety check: {exc}")

    messages = errors + warnings
    return (len(errors) == 0, messages)


# ---------------------------------------------------------------------------
# YAML / JSON parsing
# ---------------------------------------------------------------------------


def _try_load_yaml(path: Path) -> dict:
    """Load YAML file; fall back to stdlib json if pyyaml is unavailable."""
    try:
        import yaml  # type: ignore[import]

        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except ImportError:
        logger.debug("pyyaml not available; falling back to json for %s", path)
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)


def _try_load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Raw dict → dataclass conversion
# ---------------------------------------------------------------------------


def _parse_raw(raw: dict) -> ProjectConfig:
    """Convert a validated raw dict to a ProjectConfig, merging with defaults."""

    # ---- testing ----
    t_raw = raw.get("testing", {})
    testing = TestingConfig(
        unit_command=t_raw.get("unit_command", DEFAULT_CONFIG.testing.unit_command),
        e2e_command=t_raw.get("e2e_command", DEFAULT_CONFIG.testing.e2e_command),
        allowed_commands=t_raw.get("allowed_commands", []),
    )

    # ---- build ----
    b_raw = raw.get("build", {})
    build = BuildConfig(
        command=b_raw.get("command", ""),
        release_checks=b_raw.get("release_checks", []),
    )

    # ---- deploy ----
    d_raw = raw.get("deploy", {})
    service_rules: List[ServiceRule] = []
    for sr in d_raw.get("service_rules", []):
        patterns = [p.replace("\\", "/") for p in sr.get("patterns", [])]
        service_rules.append(
            ServiceRule(patterns=patterns, services=sr.get("services", []))
        )

    smoke_checks: List[SmokeCheck] = []
    for sc in d_raw.get("smoke_checks", []):
        smoke_checks.append(
            SmokeCheck(
                name=sc.get("name", ""),
                url=sc.get("url", ""),
                expected_status=sc.get("expected_status", 200),
                timeout=sc.get("timeout", 10),
            )
        )

    deploy = DeployConfig(
        strategy=d_raw.get("strategy", "none"),
        service_rules=service_rules,
        commands=d_raw.get("commands", {}),
        smoke_checks=smoke_checks,
    )

    # ---- governance ----
    g_raw = raw.get("governance", {})
    governance = GovernanceConfig(
        enabled=g_raw.get("enabled", False),
        test_tool_label=g_raw.get("test_tool_label", "pytest"),
    )

    return ProjectConfig(
        project_id=raw.get("project_id", ""),
        language=raw.get("language", "python"),
        testing=testing,
        build=build,
        deploy=deploy,
        governance=governance,
    )


# ---------------------------------------------------------------------------
# Public loaders
# ---------------------------------------------------------------------------


def load_project_config(workspace_path: Path) -> ProjectConfig:
    """Discover and load the project config from *workspace_path*.

    Search order:
      1. .aming-claw.yaml
      2. .aming-claw.json

    Falls back to DEFAULT_CONFIG only when the project_id would be
    'aming-claw' and no file is found (with a deprecation warning).

    Raises FileNotFoundError for non-aming-claw projects with no config.
    """
    workspace_path = Path(workspace_path)

    config_file: Optional[Path] = None
    raw: Optional[dict] = None

    for candidate_name, loader in [
        (".aming-claw.yaml", _try_load_yaml),
        (".aming-claw.json", _try_load_json),
    ]:
        candidate = workspace_path / candidate_name
        if candidate.is_file():
            config_file = candidate
            raw = loader(candidate)
            break

    if raw is None:
        # Attempt to derive project_id from workspace path basename
        basename = workspace_path.name.lower().replace("_", "-")
        if basename == "aming-claw" or "aming-claw" in str(workspace_path).replace(
            "\\", "/"
        ):
            logger.warning(
                "DEPRECATION: No .aming-claw.yaml found at %s; using hardcoded "
                "DEFAULT_CONFIG. Please create a .aming-claw.yaml config file.",
                workspace_path,
            )
            return DEFAULT_CONFIG
        raise FileNotFoundError(
            f"No .aming-claw.yaml or .aming-claw.json found at {workspace_path}"
        )

    is_valid, messages = validate_project_config(raw)
    for msg in messages:
        if msg.startswith("Unknown") or msg.startswith("Could not"):
            logger.warning("Config warning (%s): %s", config_file, msg)
        else:
            logger.error("Config error (%s): %s", config_file, msg)

    if not is_valid:
        raise ValueError(
            f"Invalid project config at {config_file}: "
            + "; ".join(m for m in messages if not m.startswith("Unknown"))
        )

    return _parse_raw(raw)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_CONFIG_CACHE: Dict[Tuple[str, str, str], ProjectConfig] = {}


def _config_cache_key(
    workspace_path: Path, config_file: Optional[Path]
) -> Tuple[str, str, str]:
    ws_str = str(workspace_path)
    cf_str = str(config_file) if config_file else ""
    if config_file and config_file.is_file():
        content = config_file.read_bytes()
        content_hash = hashlib.md5(content).hexdigest()
    else:
        content_hash = ""
    return (ws_str, cf_str, content_hash)


def _find_config_file(workspace_path: Path) -> Optional[Path]:
    for name in (".aming-claw.yaml", ".aming-claw.json"):
        p = workspace_path / name
        if p.is_file():
            return p
    return None


# ---------------------------------------------------------------------------
# Registry-backed resolution
# ---------------------------------------------------------------------------


def resolve_project_config(project_id: str) -> ProjectConfig:
    """Look up *project_id* in the workspace registry, then load its config.

    Falls back to DEFAULT_CONFIG when the project is 'aming-claw' and no
    config file exists.

    Raises LookupError when the workspace cannot be found.
    """
    try:
        from utils import normalize_project_id  # noqa: PLC0415
        from workspace_registry import find_workspace_by_project_id  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(
            f"Cannot import registry utilities; ensure agent/ is on sys.path: {exc}"
        ) from exc

    normalized = normalize_project_id(project_id)
    ws = find_workspace_by_project_id(normalized)
    if ws is None:
        raise LookupError(
            f"No workspace registered for project_id={normalized!r}"
        )

    workspace_path = Path(ws.get("path", ws.get("workspace_path", "")))
    return load_project_config(workspace_path)


def get_project_config(project_id: str) -> ProjectConfig:
    """Cached version of :func:`resolve_project_config`.

    Cache key = (workspace_path, config_file_path, md5_of_config_content).
    """
    try:
        from utils import normalize_project_id  # noqa: PLC0415
        from workspace_registry import find_workspace_by_project_id  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(
            f"Cannot import registry utilities; ensure agent/ is on sys.path: {exc}"
        ) from exc

    normalized = normalize_project_id(project_id)
    ws = find_workspace_by_project_id(normalized)
    if ws is None:
        raise LookupError(
            f"No workspace registered for project_id={normalized!r}"
        )

    workspace_path = Path(ws.get("path", ws.get("workspace_path", "")))
    config_file = _find_config_file(workspace_path)
    key = _config_cache_key(workspace_path, config_file)

    if key not in _CONFIG_CACHE:
        _CONFIG_CACHE[key] = load_project_config(workspace_path)

    return _CONFIG_CACHE[key]


# ---------------------------------------------------------------------------
# Convenience accessors
# ---------------------------------------------------------------------------


def get_test_command(project_id: str) -> str:
    """Return the unit test command for *project_id*."""
    return get_project_config(project_id).testing.unit_command


def get_service_rules(project_id: str) -> List[ServiceRule]:
    """Return the list of ServiceRule objects for *project_id*."""
    return get_project_config(project_id).deploy.service_rules


def get_smoke_checks(project_id: str) -> List[SmokeCheck]:
    """Return the list of SmokeCheck objects for *project_id*."""
    return get_project_config(project_id).deploy.smoke_checks


# ---------------------------------------------------------------------------
# explain_config
# ---------------------------------------------------------------------------


def explain_config(
    project_id: str,
    changed_files: Optional[List[str]] = None,
) -> dict:
    """Return a human-readable summary of the resolved config.

    If *changed_files* is provided, also reports which services are
    affected according to the deploy service_rules.
    """
    config = get_project_config(project_id)

    affected_services: List[str] = []
    if changed_files:
        # Normalise file paths to forward slashes for fnmatch
        normalised_files = [f.replace("\\", "/") for f in changed_files]
        seen: set = set()
        for rule in config.deploy.service_rules:
            for pattern in rule.patterns:
                norm_pattern = pattern.replace("\\", "/")
                for f in normalised_files:
                    if fnmatch.fnmatch(f, norm_pattern):
                        for svc in rule.services:
                            if svc not in seen:
                                seen.add(svc)
                                affected_services.append(svc)
                        break

    return {
        "project_id": config.project_id,
        "language": config.language,
        "testing": {
            "unit_command": config.testing.unit_command,
            "e2e_command": config.testing.e2e_command,
            "allowed_commands": config.testing.allowed_commands,
        },
        "build": {
            "command": config.build.command,
            "release_checks": config.build.release_checks,
        },
        "deploy": {
            "strategy": config.deploy.strategy,
            "service_rules": [
                {"patterns": r.patterns, "services": r.services}
                for r in config.deploy.service_rules
            ],
            "commands": config.deploy.commands,
            "smoke_checks": [
                {
                    "name": s.name,
                    "url": s.url,
                    "expected_status": s.expected_status,
                    "timeout": s.timeout,
                }
                for s in config.deploy.smoke_checks
            ],
        },
        "governance": {
            "enabled": config.governance.enabled,
            "test_tool_label": config.governance.test_tool_label,
        },
        "affected_services": affected_services,
    }
