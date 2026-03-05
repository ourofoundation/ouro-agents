from pathlib import Path
from .config import OuroAgentsConfig

def load_all_skills(config: OuroAgentsConfig) -> str:
    """Load all skill files from built-in + workspace directories."""
    skills = []

    # Built-in skills (shipped with the package)
    builtin_dir = Path(__file__).parent / "skills"
    for f in sorted(builtin_dir.glob("*.md")):
        skills.append(f.read_text())

    # Workspace skills (user-added)
    workspace_skills = config.agent.workspace / "skills"
    if workspace_skills.exists():
        for f in sorted(workspace_skills.glob("*.md")):
            skills.append(f.read_text())

    return "\n\n---\n\n".join(skills)
