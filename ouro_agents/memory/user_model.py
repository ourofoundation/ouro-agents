"""Per-user model for tracking preferences, interests, and communication style.

User profiles are stored as Ouro posts (``USER:{user_id}``) in the shared
agent team when configured, falling back to local markdown files at
``workspace/memory/users/{user_id}.md``.

The creating agent owns the post and updates it directly.  Other agents
discover it via search, read it, and contribute via comments.  The owner
consolidates comments during heartbeat.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_TEMPLATE = """\
# User: {user_id}

## Communication Style


## Interests


## Preferences


## Working Patterns

"""


def _user_model_path(workspace: Path, user_id: str) -> Path:
    return workspace / "memory" / "users" / f"{user_id}.md"


def load_user_model(workspace: Path, user_id: str, doc_store=None) -> str:
    """Load a user model. Tries Ouro first, falls back to local file."""
    if doc_store:
        content = doc_store.read(f"USER:{user_id}")
        if content:
            return content

    path = _user_model_path(workspace, user_id)
    if not path.exists():
        return ""
    try:
        return path.read_text().strip()
    except Exception as e:
        logger.warning("Failed to load user model for %s: %s", user_id, e)
        return ""


def ensure_user_model(workspace: Path, user_id: str, doc_store=None) -> Path | None:
    """Ensure the user model exists. Creates from template if needed.

    Returns the local file path (or None when using doc_store).
    """
    if doc_store:
        name = f"USER:{user_id}"
        if not doc_store.exists(name):
            if not doc_store.write(name, _TEMPLATE.format(user_id=user_id)):
                logger.warning("Failed to create user model post: %s", name)
                return _user_model_path(workspace, user_id)
            logger.info("Created user model post: %s", name)
        return None

    path = _user_model_path(workspace, user_id)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_TEMPLATE.format(user_id=user_id))
        logger.info("Created user model file: %s", path)
    return path


def _insert_entries_into_content(content: str, section: str, entries: list[str]) -> str:
    """Insert entries into a section of a markdown document."""
    section_header = f"## {section}"
    if section_header not in content:
        content = content.rstrip() + f"\n\n{section_header}\n"
        for entry in entries:
            content += f"- {entry}\n"
    else:
        lines = content.split("\n")
        insert_idx = None
        for i, line in enumerate(lines):
            if line.strip() == section_header:
                insert_idx = i + 1
                break

        if insert_idx is not None:
            new_lines = [f"- {entry}" for entry in entries]
            end_idx = insert_idx
            for j in range(insert_idx, len(lines)):
                if lines[j].startswith("## ") and j != insert_idx - 1:
                    break
                end_idx = j + 1
            lines = lines[:end_idx] + new_lines + lines[end_idx:]
            content = "\n".join(lines)
    return content


def append_to_user_model(
    workspace: Path,
    user_id: str,
    section: str,
    entries: list[str],
    doc_store=None,
) -> None:
    """Append entries to a specific section of the user model.

    When doc_store is available: if this agent owns the post, update directly;
    otherwise contribute via comment so the owner can consolidate.
    """
    if not entries:
        return

    name = f"USER:{user_id}"

    if doc_store:
        formatted = "\n".join(f"- [{section}] {e}" for e in entries)
        if doc_store.is_owner(name):
            content = doc_store.read(name)
            if not content:
                content = _TEMPLATE.format(user_id=user_id)
            content = _insert_entries_into_content(content, section, entries)
            if not doc_store.write(name, content):
                logger.warning(
                    "Failed to update user model post %s; falling back to local file",
                    name,
                )
                path = ensure_user_model(workspace, user_id)
                if path is None:
                    path = _user_model_path(workspace, user_id)
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    path.write_text(_TEMPLATE.format(user_id=user_id))
                local_content = path.read_text()
                local_content = _insert_entries_into_content(
                    local_content, section, entries
                )
                path.write_text(local_content)
                logger.info(
                    "Updated user model for %s locally after Ouro write failure",
                    user_id,
                )
                return
        else:
            if not doc_store.comment(name, formatted):
                logger.warning("Failed to comment on user model post %s", name)
                return
        logger.info(
            "Updated user model for %s via Ouro, section '%s': +%d entries",
            user_id,
            section,
            len(entries),
        )
        return

    path = ensure_user_model(workspace, user_id)
    if path is None:
        return
    content = path.read_text()
    content = _insert_entries_into_content(content, section, entries)
    path.write_text(content)
    logger.info(
        "Updated user model for %s, section '%s': +%d entries",
        user_id,
        section,
        len(entries),
    )
