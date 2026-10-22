"""Provider adapters: the pinned, minimal-context invocation of each CLI.

Each adapter turns (model, effort, system prompt) into the container-side argv,
environment, and files that produced the minimal request in the provider
feasibility investigation (Feasibility §7.3, spec ISO-03, CFG-06, BUD-05,
PRO-11). Flag sets are version-specific: an adapter lists the CLI versions it
was verified against, and launching any other version requires an explicit
conformance run (CFG-03, PC-03).
"""

from __future__ import annotations

from dataclasses import dataclass

#: Container-side locations (fixed, neutral paths: ISO-02, PC-10).
CLI_MOUNT = "/opt/cli"
LAUNCHER_MOUNT = "/opt/synchri"
RELAY_SOCKET = "/run/synchri/relay.sock"
WORK_DIR = "/work"
HOME_DIR = "/home/slot"
BRIDGE_PORT = 18080


@dataclass(frozen=True)
class LaunchPlan:
    argv: list[str]
    env: dict[str, str]
    #: Files the launcher writes before starting the CLI: container path -> text.
    files: dict[str, str]
    #: Directories the launcher creates (mode 0700) before starting the CLI.
    dirs: tuple[str, ...]


class ProviderAdapter:
    runtime: str
    provider: str
    executable: str
    verified_versions: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    token_env: str
    #: Every environment variable the CLI may receive; anything else is refused.
    allowed_env: frozenset[str]

    def plan(self, *, model: str, token: str, system_prompt: str, effort: str | None,
             max_output_tokens: int | None) -> LaunchPlan:  # pragma: no cover - abstract
        raise NotImplementedError

    def relay_base_url(self) -> str:
        return f"http://127.0.0.1:{BRIDGE_PORT}/{self.provider}"


class ClaudeAdapter(ProviderAdapter):
    runtime = "claude_code"
    provider = "anthropic"
    executable = "claude"
    verified_versions = ("2.1.309",)
    allowed_tools = ()
    token_env = "ANTHROPIC_API_KEY"
    allowed_env = frozenset({
        "PATH", "HOME", "TMPDIR", "LANG", "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_MAX_RETRIES", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    })

    def plan(self, *, model, token, system_prompt, effort=None, max_output_tokens=None):
        system_file = f"{HOME_DIR}/system-prompt.txt"
        argv = [
            f"{CLI_MOUNT}/{self.executable}", "-p", "--bare", "--no-session-persistence",
            "--output-format", "stream-json", "--verbose",
            "--model", model,
            "--system-prompt-file", system_file,
            "--strict-mcp-config",
        ]
        if effort:
            argv += ["--effort", effort]
        # --tools is variadic and swallows a following positional prompt, so
        # it is followed by another option and the prompt goes on stdin.
        argv += ["--tools", "", "--disallowedTools", "*"]
        env = {
            "PATH": f"{CLI_MOUNT}:/usr/bin:/bin",
            "HOME": HOME_DIR,
            "TMPDIR": WORK_DIR,
            "LANG": "C.UTF-8",
            "ANTHROPIC_BASE_URL": self.relay_base_url(),
            "ANTHROPIC_API_KEY": token,
            "CLAUDE_CODE_MAX_RETRIES": "0",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
        if max_output_tokens is not None:
            env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(int(max_output_tokens))
        return LaunchPlan(argv=argv, env=env, files={system_file: system_prompt}, dirs=())


#: Codex 0.163.0's bundled system skills (Feasibility §4.4).
CODEX_BUNDLED_SKILLS = ("review-agent", "skill-creator", "imagegen", "skill-installer", "openai-docs")
CODEX_DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "multi_agent", "view_image", "goals", "apps", "plugins",
    "hooks", "image_generation", "browser_use", "computer_use", "tool_suggest",
    "skill_search", "sleep_tool",
)


class CodexAdapter(ProviderAdapter):
    runtime = "codex"
    provider = "openai"
    executable = "codex"
    verified_versions = ("0.163.0",)
    allowed_tools = ("request_user_input",)
    token_env = "RESEARCH_RELAY_TOKEN"
    allowed_env = frozenset({"PATH", "HOME", "TMPDIR", "LANG", "CODEX_HOME", "RESEARCH_RELAY_TOKEN"})

    def plan(self, *, model, token, system_prompt, effort=None, max_output_tokens=None):
        if max_output_tokens is not None:
            raise ValueError("Codex 0.163.0 exposes no output-token cap (Feasibility §4.3)")
        codex_home = f"{HOME_DIR}/codexhome"
        instructions = f"{HOME_DIR}/instructions.txt"
        skills = ",".join(
            f'{{path="{codex_home}/skills/.system/{name}/SKILL.md",enabled=false}}'
            for name in CODEX_BUNDLED_SKILLS
        )
        config = [
            'model_provider="research"',
            'model_providers.research.name="research"',
            f'model_providers.research.base_url="{self.relay_base_url()}/v1"',
            f'model_providers.research.env_key="{self.token_env}"',
            'model_providers.research.wire_api="responses"',
            "model_providers.research.request_max_retries=0",
            "model_providers.research.stream_max_retries=0",
            f'model_instructions_file="{instructions}"',
            "project_doc_max_bytes=0",
            "include_environment_context=false",
            "include_permissions_instructions=false",
            "include_apps_instructions=false",
            "include_collaboration_mode_instructions=false",
            f"skills.config=[{skills}]",
            'web_search="disabled"',
            'shell_environment_policy.inherit="none"',
            'model_reasoning_summary="none"',
        ]
        if effort:
            config.append(f'model_reasoning_effort="{effort}"')
        argv = [
            f"{CLI_MOUNT}/{self.executable}", "exec", "--json", "--ephemeral",
            "--skip-git-repo-check", "--strict-config", "--ignore-user-config", "--ignore-rules",
            "-s", "read-only", "-m", model,
        ]
        for entry in config:
            argv += ["-c", entry]
        for feature in CODEX_DISABLED_FEATURES:
            argv += ["--disable", feature]
        env = {
            "PATH": f"{CLI_MOUNT}:/usr/bin:/bin",
            "HOME": HOME_DIR,
            "CODEX_HOME": codex_home,
            "TMPDIR": WORK_DIR,
            "LANG": "C.UTF-8",
            "RESEARCH_RELAY_TOKEN": token,
        }
        return LaunchPlan(argv=argv, env=env, files={instructions: system_prompt},
                          dirs=(codex_home,))


ADAPTERS = {adapter.runtime: adapter for adapter in (ClaudeAdapter(), CodexAdapter())}


def adapter_for(runtime: str) -> ProviderAdapter:
    try:
        return ADAPTERS[runtime]
    except KeyError:
        raise ValueError(f"no research adapter for runtime {runtime!r}") from None
