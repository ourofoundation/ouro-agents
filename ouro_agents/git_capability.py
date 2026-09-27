"""Detect what the agent can do with git from inside its sandbox.

Levels:

- ``none``: no shell in the sandbox, or the workspace is not a git repository.
- ``local``: commits work, but nothing can leave the machine (no usable
  remote or credentials).
- ``remote``: an HTTPS GitHub ``origin`` plus a non-empty ``GH_TOKEN``
  forwarded into the sandbox, so ``git push`` and ``gh pr create`` can work.

Detection runs on the host at startup and never touches the network, so
``remote`` means "configured", not "verified".
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Mapping, Optional

if TYPE_CHECKING:
    from .config import SandboxConfig

logger = logging.getLogger(__name__)

GitLevel = Literal["none", "local", "remote"]

_SSH_URL_RE = re.compile(r"^(?:ssh://|[\w.-]+@[\w.-]+:)")
_URL_USERINFO_RE = re.compile(r"^[a-z][a-z0-9+.-]*://[^/\s@]+@", re.IGNORECASE)


@dataclass(frozen=True)
class GitCapability:
    level: GitLevel
    reason: str = ""

    @property
    def skill_capabilities(self) -> frozenset[str]:
        """Capability tokens matched against skill ``requires:`` frontmatter."""
        if self.level == "remote":
            return frozenset({"git-remote"})
        if self.level == "local":
            return frozenset({"git-local"})
        return frozenset()


def _origin_url(repo: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def detect_git_capability(
    workspace: Path,
    sandbox: "SandboxConfig",
    environ: Optional[Mapping[str, str]] = None,
) -> GitCapability:
    from .memory.dream_git import find_git_root

    env = os.environ if environ is None else environ

    if sandbox.mode != "docker" or not sandbox.enable_shell:
        return GitCapability("none", "run_shell is not enabled in a Docker sandbox")

    repo = find_git_root(workspace)
    if repo is None:
        return GitCapability("none", "workspace is not inside a git repository")

    problems: list[str] = []
    url = _origin_url(repo)
    if not url:
        problems.append("no `origin` remote is configured")
    elif _SSH_URL_RE.match(url):
        problems.append(
            "`origin` uses SSH and the sandbox has no SSH client or keys; "
            "switch it to https://github.com/..."
        )
    elif _URL_USERINFO_RE.match(url):
        problems.append("`origin` URL embeds credentials; remove them from the URL")
    elif not url.lower().startswith("https://github.com/"):
        problems.append("`origin` is not an HTTPS GitHub remote")
    if "GH_TOKEN" not in sandbox.env_allowlist:
        problems.append("GH_TOKEN is not in sandbox.env_allowlist")
    elif not env.get("GH_TOKEN", "").strip():
        problems.append("GH_TOKEN is empty or unset")

    if problems:
        return GitCapability("local", "; ".join(problems))
    return GitCapability("remote")


_SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||[;|\n]")
_GIT_INVOCATION_RE = re.compile(
    r"\bgit(?:\s+(?:-[Cc]\s+\S+|--[\w-]+(?:=\S+)?))*\s+([\w-]+)(.*)", re.DOTALL
)
_CREDENTIAL_URL_RE = re.compile(r"\bhttps?://[^/\s@'\"]+@", re.IGNORECASE)
_REMOTE_EDIT_RE = re.compile(r"^\s*(?:add|set-url|rename|remove|rm)\b")
_CONFIG_EDIT_RE = re.compile(
    r"\b(?:credential\.|url\.[^\s]*\.(?:insteadof|pushinsteadof)|remote\.[^\s]*\.(?:url|pushurl))",
    re.IGNORECASE,
)
_FORCE_PUSH_RE = re.compile(r"(?:^|\s)(?:--force(?:-with-lease)?\b|-[a-zA-Z]*f[a-zA-Z]*\b|\+\S)")


def refused_git_command(command: str) -> Optional[str]:
    """Return a refusal reason for git commands that leak or bypass credentials."""
    for segment in _SEGMENT_SPLIT_RE.split(command):
        match = _GIT_INVOCATION_RE.search(segment)
        if match is None:
            continue
        if _CREDENTIAL_URL_RE.search(segment):
            return "git URLs must not embed credentials or tokens"
        subcommand, rest = match.group(1), match.group(2)
        if subcommand == "remote" and _REMOTE_EDIT_RE.match(rest):
            return "remotes are managed by the operator; do not add or rewrite them"
        if subcommand == "config" and _CONFIG_EDIT_RE.search(rest):
            return "git credential and remote URL config is managed by the operator"
        if subcommand == "push" and _FORCE_PUSH_RE.search(rest):
            return "force pushes are not allowed"
    return None


def log_git_capability(agent_name: str, capability: GitCapability) -> None:
    if capability.level == "remote":
        logger.info("[%s] git: push and pull requests enabled", agent_name)
    elif capability.level == "local":
        logger.warning(
            "[%s] git: local commits only, push disabled (%s)",
            agent_name,
            capability.reason,
        )
    else:
        logger.info("[%s] git: unavailable (%s)", agent_name, capability.reason)
