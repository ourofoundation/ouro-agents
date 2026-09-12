"""Create a standalone agent project from the installed package."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from importlib import resources
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,62}$")


def _runtime_version() -> str:
    try:
        return version("ouro-agents")
    except PackageNotFoundError:
        return "0.1.5"


def sandbox_image_name() -> str:
    return f"ouro-agents-sandbox:{_runtime_version()}"


def build_sandbox_image() -> str:
    """Build the packaged sandbox image and return its local tag."""
    dockerfile = (
        resources.files("ouro_agents.resources")
        .joinpath("Dockerfile.sandbox")
        .read_text()
    )
    image = sandbox_image_name()
    with tempfile.TemporaryDirectory() as context:
        subprocess.run(
            ["docker", "build", "--file", "-", "--tag", image, context],
            input=dockerfile,
            text=True,
            check=True,
        )
    return image


def _agent_config(name: str) -> str:
    runtime_version = _runtime_version()
    config = {
        "agent": {
            "name": name,
            "workspace": ".",
            "data_dir": f"~/ouro-data/{name}",
            "org_id": "00000000-0000-0000-0000-000000000000",
            "sandbox": {
                "mode": "docker",
                "image": f"ouro-agents-sandbox:{runtime_version}",
                "workspace_mount": "/workspace",
                "enable_shell": True,
                "env_allowlist": [
                    "OURO_API_KEY",
                    "OURO_BASE_URL",
                    "GH_TOKEN",
                    "GIT_AUTHOR_NAME",
                    "GIT_AUTHOR_EMAIL",
                    "GIT_COMMITTER_NAME",
                    "GIT_COMMITTER_EMAIL",
                ],
            },
        },
        "models": {
            "strong": {
                "id": "anthropic/claude-4.6-sonnet",
                "reasoning": {"effort": "medium"},
            },
            "light": {
                "id": "openai/gpt-4.1-mini",
                "reasoning": {"effort": "none"},
            },
        },
        "security": {"controllers": ["your-handle"], "trusted": []},
        "modes": {
            "run": {"max_steps": 60},
            "chat": {"max_steps": 40},
            "heartbeat": {
                "enabled": True,
                "every": "1h",
                "servers": ["ouro"],
                "max_steps": 40,
            },
        },
        "memory": {
            "provider": "mem0",
            "path": f"~/ouro-data/{name}/memory",
            "embedder": "openai/text-embedding-3-small",
        },
        "mcp_servers": [
            {
                "name": "ouro",
                "transport": "stdio",
                "command": "python",
                "args": ["-m", "ouro_mcp.server"],
                "env": {
                    "OURO_API_KEY": "${OURO_API_KEY}",
                    "OURO_BASE_URL": "${OURO_BASE_URL}",
                },
            }
        ],
        "server": {
            "host": "0.0.0.0",
            "port": 8000,
            "webhook_path": f"/{name}/events",
        },
        "env_file": ".env",
    }
    return json.dumps(config, indent=2) + "\n"


def _files(name: str) -> dict[str, str]:
    runtime_version = _runtime_version()
    return {
        "agent.json": _agent_config(name),
        "SOUL.md": (
            f"# {name}\n\n"
            "Describe this agent's identity, responsibilities, values, and boundaries.\n\n"
            "## Code Ownership\n\n"
            "This repository is your working home. Follow the always-loaded `git` "
            "skill for every code, skill, identity, or curated-memory change: "
            "branch, test, commit, push, and open a pull request. Use the "
            "`self_improvement` skill when real Ouro route evidence motivates a "
            "service or coil change.\n"
        ),
        "HEARTBEAT.md": (
            "# Heartbeat\n\n"
            "- Review notifications and active work.\n"
            "- Continue the highest-priority safe action.\n"
            "- Record durable learnings in MEMORY.md or skills/.\n"
        ),
        "MEMORY.md": "# Memory\n\nDurable, curated context for this agent.\n",
        "skills/.gitkeep": "",
        "coils/.gitkeep": "",
        ".env.example": (
            "OPENROUTER_API_KEY=\n"
            "OURO_API_KEY=\n"
            "OURO_BASE_URL=https://api.ouro.foundation\n"
            "GH_TOKEN=\n"
            f"GIT_AUTHOR_NAME={name}\n"
            f"GIT_AUTHOR_EMAIL={name}@ouro.foundation\n"
            f"GIT_COMMITTER_NAME={name}\n"
            f"GIT_COMMITTER_EMAIL={name}@ouro.foundation\n"
        ),
        ".gitignore": (
            ".env\n"
            ".env.*\n"
            "!.env.example\n"
            ".venv/\n"
            "__pycache__/\n"
            "*.py[cod]\n"
            ".pytest_cache/\n"
            ".ruff_cache/\n"
            ".DS_Store\n"
            "conversations/\n"
            "protected/\n"
            "scratch/\n"
            "runs.db*\n"
            "*.sqlite3\n"
            "*.jsonl\n"
        ),
        "pyproject.toml": (
            "[project]\n"
            f'name = "{name}"\n'
            'version = "0.1.0"\n'
            "requires-python = \">=3.10\"\n"
            f'dependencies = ["ouro-agents=={runtime_version}"]\n'
        ),
        "ecosystem.config.cjs": (
            "module.exports = {\n"
            "  apps: [{\n"
            f'    name: "{name}-agent",\n'
            '    script: ".venv/bin/ouro-agents",\n'
            '    args: ["--config", "agent.json", "--env-file", ".env", "serve"],\n'
            '    interpreter: "none",\n'
            "    autorestart: true,\n"
            "    max_memory_restart: \"2G\",\n"
            "  }],\n"
            "};\n"
        ),
        "README.md": (
            f"# {name}\n\n"
            "An autonomous agent powered by [ouro-agents]"
            "(https://pypi.org/project/ouro-agents/).\n\n"
            "```bash\n"
            "python -m venv .venv\n"
            "source .venv/bin/activate\n"
            "pip install -e .\n"
            "cp .env.example .env\n"
            "ouro-agents build-sandbox\n"
            "ouro-agents --config agent.json serve\n"
            "```\n"
        ),
    }


def init_agent_project(name: str, directory: Path | None = None) -> Path:
    """Create a new agent project and return its absolute path."""
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            "name must start with a letter and contain only lowercase letters, "
            "numbers, and dashes"
        )

    target = (directory or Path(name)).expanduser().resolve()
    files = _files(name)
    conflicts = [relative for relative in files if (target / relative).exists()]
    if conflicts:
        joined = ", ".join(conflicts)
        raise FileExistsError(f"refusing to overwrite existing files: {joined}")

    for relative, content in files.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return target
