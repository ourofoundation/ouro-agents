from pathlib import Path

def load_soul(path: Path) -> str:
    """Load SOUL.md if it exists, return empty string otherwise."""
    if path.exists():
        return path.read_text()
    return ""

def build_prompt(soul: str, notes: str, skills: str, memory_context: str) -> str:
    """Build the full system prompt from its components."""
    parts = []
    
    if soul:
        parts.append("## IDENTITY AND RULES (SOUL)\n" + soul)
        
    if notes:
        parts.append("## DEPLOYMENT CONTEXT (NOTES)\n" + notes)
        
    if skills:
        parts.append("## SKILLS AND KNOWLEDGE\n" + skills)
        
    if memory_context:
        parts.append("## RELEVANT MEMORIES\n" + memory_context)
        
    return "\n\n---\n\n".join(parts)
