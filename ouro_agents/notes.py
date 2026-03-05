from pathlib import Path

def load_notes(path: Path) -> str:
    """Load NOTES.md if it exists, return empty string otherwise."""
    if path.exists():
        return path.read_text()
    return ""
