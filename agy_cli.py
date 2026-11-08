"""Stateless Antigravity CLI execution for the API backend."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable


MODEL_FAMILIES = {"flash-lite", "flash", "pro", "argon"}
THINKING_LEVELS = {"low", "medium", "high"}
MODEL_SLUG_RE = re.compile(
    r"^gemini-(?P<version>\d+(?:\.\d+)*)-(?P<family>flash-lite|flash|pro|argon)-(?P<thinking>low|medium|high)$",
    re.IGNORECASE,
)
ANY_MODEL_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$", re.IGNORECASE)
API_AGENT_NAME = "api-model"
API_SEARCH_AGENT_NAME = "api-search"
REQUIRED_CLI_FLAGS = (
    "--settings",
    "--agent",
    "--input-format",
    "--output-format",
    "--sandbox",
    "--disable-slash-commands",
    "--log-file",
    "--print-timeout",
)
HOOK_EVENT_KEYS = (
    "SessionStart",
    "PreToolUse",
    "PostToolUse",
    "PreInvocation",
    "PostInvocation",
    "Stop",
)


class AgyCliError(RuntimeError):
    """Antigravity CLI could not produce a successful model response."""


class AgyModelSelectionError(AgyCliError):
    """The pinned CLI model was rejected before any model tokens were spent."""


class AgyCliTimeoutError(AgyCliError):
    """Antigravity did not finish the requested turn within its deadline."""


@dataclass(frozen=True)
class AgyResult:
    response: str
    conversation_id: str
    usage: Dict[str, Any]
    duration_seconds: float | None
    model_slug: str
    prompt_transport: str
    prompt_chars: int
    effective_thinking_level: str | None
    tools_used: tuple[str, ...]


def _version_key(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def parse_model_slugs(lines: Iterable[str]) -> Dict[tuple[str, str], str]:
    """Return the newest CLI model slug for each stable family/thinking pair."""

    selected: Dict[tuple[str, str], tuple[tuple[int, ...], str]] = {}
    for raw_line in lines:
        slug = raw_line.strip().split(maxsplit=1)[0] if raw_line.strip() else ""
        match = MODEL_SLUG_RE.match(slug)
        if not match:
            continue

        family = match.group("family").lower()
        thinking = match.group("thinking").lower()
        version = _version_key(match.group("version"))
        key = (family, thinking)
        current = selected.get(key)
        if current is None or version > current[0]:
            selected[key] = (version, slug)

    return {key: value[1] for key, value in selected.items()}


def parse_available_model_slugs(lines: Iterable[str]) -> list[str]:
    """Return every concrete model ID advertised by ``agy models``."""

    slugs: list[str] = []
    seen = set()
    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            continue
        slug = stripped.split(maxsplit=1)[0]
        if not ANY_MODEL_SLUG_RE.match(slug) or slug.lower() == "fetching":
            continue
        if slug not in seen:
            seen.add(slug)
            slugs.append(slug)
    return slugs


def default_runtime_dir() -> Path:
    """Use a sterile directory so the CLI does not index this service repo."""

    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path(tempfile.gettempdir())
    return base / "gemini-studio-api" / "agy-runtime"


class AgyCliRunner:
    def __init__(
        self,
        agy_path: str | None = None,
        runtime_dir: str | Path | None = None,
    ):
        resolved = agy_path or shutil.which("agy")
        if not resolved:
            raise AgyCliError("Antigravity CLI executable 'agy' was not found on PATH")

        self.agy_path = str(resolved)
        self.runtime_dir = Path(runtime_dir) if runtime_dir else default_runtime_dir()
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._models: Dict[tuple[str, str], str] | None = None
        self._available_model_slugs: list[str] = []
        self._prepare_runtime_workspace()

    def _prepare_runtime_workspace(self) -> None:
        """Create one sterile workspace shared by stateless CLI processes."""

        # Remove artifacts from the earlier per-request-directory design and
        # any log left behind by an interrupted process.
        for child in self.runtime_dir.glob("agy-request-*"):
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
        for log_path in self.runtime_dir.glob("agy-run-*.log"):
            try:
                log_path.unlink(missing_ok=True)
            except OSError:
                pass

        agent_dir = self.runtime_dir / ".agents" / "agents"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / f"{API_AGENT_NAME}.md").write_text(
            self._agent_markdown(API_AGENT_NAME, use_search=False),
            encoding="utf-8",
        )
        (agent_dir / f"{API_SEARCH_AGENT_NAME}.md").write_text(
            self._agent_markdown(API_SEARCH_AGENT_NAME, use_search=True),
            encoding="utf-8",
        )

    def check_capabilities(self) -> None:
        """Fail early when the installed CLI is too old for the API contract."""

        try:
            completed = subprocess.run(
                [self.agy_path, "--help"],
                cwd=self.runtime_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=15,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgyCliError("agy --help timed out while checking CLI capabilities") from exc
        except OSError as exc:
            raise AgyCliError(f"Could not start Antigravity CLI: {exc}") from exc
        help_text = f"{completed.stdout}\n{completed.stderr}"
        if completed.returncode != 0:
            raise AgyCliError("agy --help failed; cannot verify CLI capabilities")

        missing = [flag for flag in REQUIRED_CLI_FLAGS if flag not in help_text]
        if missing:
            raise AgyCliError(
                "Installed Antigravity CLI is missing required flags: " + ", ".join(missing)
            )

    def check_environment_isolation(self) -> None:
        """Reject global customizations that can mutate headless API sessions."""

        hooks_path = Path.home() / ".gemini" / "config" / "hooks.json"
        if hooks_path.exists():
            try:
                hooks_payload = json.loads(hooks_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise AgyCliError(f"Cannot verify Antigravity global hooks: {exc}") from exc
            if not isinstance(hooks_payload, dict):
                raise AgyCliError("Antigravity global hooks file must contain a JSON object")

            active_hooks = []
            for name, config in hooks_payload.items():
                if not isinstance(config, dict):
                    raise AgyCliError(f"Antigravity global hook {name!r} has invalid configuration")
                if config.get("enabled") is False:
                    continue
                if any(config.get(event) for event in HOOK_EVENT_KEYS):
                    active_hooks.append(str(name))
            if active_hooks:
                raise AgyCliError(
                    "Active global Antigravity hooks are not allowed for the API backend: "
                    + ", ".join(sorted(active_hooks))
                )

        try:
            completed = subprocess.run(
                [self.agy_path, "plugin", "list", "--json"],
                cwd=self.runtime_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=15,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgyCliError("agy plugin list timed out while verifying API isolation") from exc
        except OSError as exc:
            raise AgyCliError(f"Could not inspect Antigravity plugins: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
            raise AgyCliError(f"Could not verify Antigravity plugins: {detail}")
        try:
            plugins = json.loads(completed.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise AgyCliError("agy plugin list --json returned invalid JSON") from exc
        if not isinstance(plugins, list):
            raise AgyCliError("agy plugin list --json did not return a JSON array")

        enabled_plugins = [
            str(plugin.get("name") or "<unnamed>")
            for plugin in plugins
            if isinstance(plugin, dict) and plugin.get("enabled") is True
        ]
        malformed_plugins = [plugin for plugin in plugins if not isinstance(plugin, dict)]
        if malformed_plugins:
            raise AgyCliError("agy plugin list --json returned malformed plugin entries")
        if enabled_plugins:
            raise AgyCliError(
                "Enabled Antigravity plugins are not allowed for the API backend: "
                + ", ".join(sorted(enabled_plugins))
            )

    def refresh_models(self) -> Dict[tuple[str, str], str]:
        try:
            completed = subprocess.run(
                [self.agy_path, "models"],
                cwd=self.runtime_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgyCliError("agy models timed out") from exc
        except OSError as exc:
            raise AgyCliError(f"Could not run agy models: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
            raise AgyCliError(f"agy models failed: {detail}")

        lines = completed.stdout.splitlines()
        models = parse_model_slugs(lines)
        all_models = parse_available_model_slugs(lines)
        if not all_models:
            raise AgyCliError("agy models returned no usable model IDs")
        self._models = models
        self._available_model_slugs = all_models
        return dict(models)

    def available_model_slugs(self) -> list[str]:
        if self._models is None:
            self.refresh_models()
        return list(self._available_model_slugs)

    def resolve_model(self, family: str, thinking_level: str) -> str:
        normalized_family = family.strip().lower()
        normalized_thinking = thinking_level.strip().lower()
        if normalized_thinking not in THINKING_LEVELS:
            raise AgyCliError(f"Unsupported thinking level: {thinking_level}")

        models = self._models or self.refresh_models()
        exact = next(
            (slug for slug in self._available_model_slugs if slug.lower() == normalized_family),
            None,
        )
        if exact:
            return exact
        if normalized_family not in MODEL_FAMILIES:
            raise AgyCliError(f"Antigravity CLI does not currently expose model {family!r}")
        slug = models.get((normalized_family, normalized_thinking))
        if not slug:
            raise AgyCliError(
                f"Antigravity CLI does not currently expose {normalized_family}/{normalized_thinking}"
            )
        return slug

    @staticmethod
    def _session_settings(use_search: bool) -> Dict[str, Any]:
        """Override personal CLI settings with an API-safe policy for one run."""

        deny = [
            "write_file(*)",
            "command(*)",
            "unsandboxed(*)",
            "mcp(*)",
            "execute_url(*)",
        ]
        permissions: Dict[str, list[str]] = {"deny": deny}
        if use_search:
            permissions["allow"] = ["read_url(*)"]
        else:
            deny.append("read_url(*)")

        return {
            "toolPermission": "request-review",
            "allowNonWorkspaceAccess": False,
            "enableTerminalSandbox": True,
            "historySize": 0,
            "modelProvider": "",
            "useG1Credits": False,
            "notifications": False,
            "permissions": permissions,
        }

    @staticmethod
    def _agent_markdown(agent_name: str, use_search: bool) -> str:
        if use_search:
            tools_block = "tools:\n  - search_web\n  - read_url_content\n"
            instructions = (
                "Web research is required for this request. Use search_web at least once before "
                "answering, and use read_url_content when source detail is useful. Do not use browser interaction, local files, "
                "commands, plugins, scheduling, account tools, or subagents."
            )
        else:
            tools_block = "tools: []\n"
            instructions = (
                "Answer from the supplied prompt without tools. Do not access local files or "
                "the web, run commands, use plugins, schedule tasks, manage accounts, or delegate work."
            )

        return (
            "---\n"
            f"name: {agent_name}\n"
            "description: Stateless responder for model API requests.\n"
            f"{tools_block}"
            "mainAgent: true\n"
            "subagent: false\n"
            "model: inherit\n"
            "commandExecutionPolicy: off\n"
            "---\n"
            "# System Prompt\n"
            "Answer the user request directly and completely. "
            f"{instructions}\n"
        )

    def run_prompt(
        self,
        prompt: str,
        *,
        family: str = "flash",
        thinking_level: str = "medium",
        use_search: bool = False,
        timeout_seconds: int = 480,
    ) -> AgyResult:
        """Run one isolated prompt through Antigravity stream-json print mode."""

        if not isinstance(prompt, str) or not prompt.strip():
            raise AgyCliError("Prompt must be a non-empty string")
        if timeout_seconds <= 0:
            raise AgyCliError("timeout_seconds must be positive")

        normalized_thinking = thinking_level.strip().lower()
        model_slug = self.resolve_model(family, normalized_thinking)
        prompt_chars = len(prompt)
        agent_name = API_SEARCH_AGENT_NAME if use_search else API_AGENT_NAME
        settings = self._session_settings(use_search)
        input_message = json.dumps(
            {"event": "user", "message": {"content": prompt}},
            ensure_ascii=False,
        ) + "\n"

        args = [
            self.agy_path,
            "--settings",
            json.dumps(settings, separators=(",", ":")),
            "--agent",
            agent_name,
            "--model",
            model_slug,
            "--sandbox",
            "--disable-slash-commands",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--log-file",
            os.devnull,
            "--print-timeout",
            f"{timeout_seconds}s",
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                args,
                cwd=self.runtime_dir,
                input=input_message,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout_seconds + 15,
                check=False,
                creationflags=creationflags,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgyCliTimeoutError(f"Antigravity CLI exceeded {timeout_seconds}s") from exc
        except OSError as exc:
            raise AgyCliError(f"Could not start Antigravity CLI request: {exc}") from exc

        result_payload: Dict[str, Any] | None = None
        tool_attempts: list[str] = []
        tools_used: list[str] = []
        for raw_line in completed.stdout.splitlines():
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if event.get("event") == "step_update" and isinstance(event.get("step_update"), dict):
                step = event["step_update"]
                tool_name = step.get("tool_name")
                if step.get("step_type") == "tool" and isinstance(tool_name, str):
                    if tool_name not in tool_attempts:
                        tool_attempts.append(tool_name)
                    if step.get("state") == "DONE" and tool_name not in tools_used:
                        tools_used.append(tool_name)
            if event.get("event") == "result" and isinstance(event.get("result"), dict):
                result_payload = event["result"]

        if result_payload is None:
            detail = completed.stderr.strip() or completed.stdout.strip() or "no result event"
            raise AgyCliError(f"Antigravity CLI returned no result: {detail}")

        status = str(result_payload.get("status") or "").upper()
        finished_reason = str(result_payload.get("finished_reason") or "").lower()
        if finished_reason == "print_timeout":
            raise AgyCliTimeoutError(
                f"Antigravity CLI print timeout after {timeout_seconds}s; partial output was discarded"
            )
        if completed.returncode != 0 or status != "SUCCESS":
            detail = str(result_payload.get("error") or completed.stderr.strip() or status or "unknown error")
            usage = result_payload.get("usage")
            total_tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
            if "invalid model selection" in detail.lower() and total_tokens == 0:
                raise AgyModelSelectionError(detail)
            raise AgyCliError(f"Antigravity CLI request failed: {detail}")

        response = str(result_payload.get("response") or "")
        if not response.strip():
            raise AgyCliError("Antigravity CLI returned an empty response")

        allowed_tools = {"search_web", "read_url_content"} if use_search else set()
        unexpected_tools = [tool for tool in tool_attempts if tool not in allowed_tools]
        if unexpected_tools:
            raise AgyCliError(
                "Antigravity CLI used tools outside the API policy: " + ", ".join(unexpected_tools)
            )
        if use_search and "search_web" not in tools_used:
            raise AgyCliError("Web search was requested but Antigravity did not use search_web")

        usage = result_payload.get("usage")
        thinking_match = re.search(r"-(low|medium|high)$", model_slug, re.IGNORECASE)
        return AgyResult(
            response=response,
            conversation_id=str(result_payload.get("conversation_id") or ""),
            usage=usage if isinstance(usage, dict) else {},
            duration_seconds=(
                float(result_payload["duration_seconds"])
                if isinstance(result_payload.get("duration_seconds"), (int, float))
                else None
            ),
            model_slug=model_slug,
            prompt_transport="stdin",
            prompt_chars=prompt_chars,
            effective_thinking_level=(thinking_match.group(1).capitalize() if thinking_match else None),
            tools_used=tuple(tools_used),
        )
