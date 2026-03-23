"""Per-user model for tracking preferences, interests, and communication style.

User profiles are stored as markdown files at
``workspace/memory/users/{user_id}.md`` and loaded into the system prompt when
the user_id is known.  They evolve slowly — updated by end-of-conversation
reflection and by the agent's explicit memory tools.
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


def load_user_model(workspace: Path, user_id: str) -> str:
    """Load a user model file.  Returns empty string if none exists."""
    path = _user_model_path(workspace, user_id)
    if not path.exists():
        return ""
    try:
        return path.read_text().strip()
    except Exception as e:
        logger.warning("Failed to load user model for %s: %s", user_id, e)
        return ""


def ensure_user_model(workspace: Path, user_id: str) -> Path:
    """Create the user model file from the template if it doesn't exist.

    Returns the path to the file.
    """
    path = _user_model_path(workspace, user_id)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_TEMPLATE.format(user_id=user_id))
        logger.info("Created user model file: %s", path)
    return path


def append_to_user_model(
    workspace: Path, user_id: str, section: str, entries: list[str]
) -> None:
    """Append entries to a specific section of the user model.

    Parameters
    ----------
    section : one of "Communication Style", "Interests", "Preferences",
              "Working Patterns"
    entries : list of bullet-point strings to add (without leading "- ")
    """
    if not entries:
        return

    path = ensure_user_model(workspace, user_id)
    content = path.read_text()

    section_header = f"## {section}"
    if section_header not in content:
        # Append the section at the end
        content = content.rstrip() + f"\n\n{section_header}\n"
        for entry in entries:
            content += f"- {entry}\n"
    else:
        # Insert entries after the section header, before the next section
        lines = content.split("\n")
        insert_idx = None
        for i, line in enumerate(lines):
            if line.strip() == section_header:
                insert_idx = i + 1
                break

        if insert_idx is not None:
            new_lines = [f"- {entry}" for entry in entries]
            # Find the end of the section (next ## or end of file)
            end_idx = insert_idx
            for j in range(insert_idx, len(lines)):
                if lines[j].startswith("## ") and j != insert_idx - 1:
                    break
                end_idx = j + 1
            # Insert before the next section header
            lines = lines[:end_idx] + new_lines + lines[end_idx:]
            content = "\n".join(lines)

    path.write_text(content)
    logger.info("Updated user model for %s, section '%s': +%d entries", user_id, section, len(entries))
