# Skills

Skills are reusable markdown knowledge fragments stored under
`ouro_agents/skills/` (built-ins) and `workspace/skills/` (overrides /
project-specific). Each file has optional YAML frontmatter and a markdown
body.

The skills system has two consumers:

1. **Main agent** — at startup, every skill with `load: always` in its
   frontmatter is inlined into the system prompt. Other skills are listed
   in a directory and can be pulled on demand by the `load_skill` tool.
2. **Subagents** — each `SubAgentProfile` has a `skills:` list of names.
   The runner resolves those into body text and appends it to the
   subagent's task context.

## Frontmatter

```markdown
---
description: Short, one-line description shown in the directory.
load: always
---

# Body content here
```

Recognized fields:

- `description` — used by the directory listing.
- `load` — `always` to inline into every system prompt; anything else
  (default `stub`) leaves it discoverable but not loaded.

When frontmatter is missing or malformed, the first markdown heading is
used as the description.

## Built-in skills

Shipping with the package today:

| Name | Purpose |
|------|---------|
| `ouro` | Core Ouro platform conventions (orgs, teams, asset types). |
| `ouro_markdown` | Extended markdown features Ouro renders (mentions, asset embeds, math). |
| `ouro_py` | Quickstart for the Ouro Python SDK. |
| `python` | Sandbox python-tool conventions. |
| `filesystem` | Workspace file conventions. |
| `web-search` | When and how to use web search. |
| `working-memory` | How to maintain `MEMORY.md` and daily logs. |
| `asset_output` | Output handoff conventions for asset-creating subagents. |
| `benchmarking` | Running benchmarks via Ouro routes. |
| `screening-campaigns` | Materials-science screening playbook. |

## Overriding from a workspace

Drop `<name>.md` into `workspace/skills/` and it replaces the built-in of
the same name for that workspace. This is how you tune a skill to a
specific agent without forking the package.

## Loading a skill at runtime

When the main agent needs a skill that wasn't preloaded, it calls:

```
load_skill(name="benchmarking")
```

The tool returns the skill body and the agent typically copies the parts
it needs into its working context. Subagents do not need this tool — their
skills are pre-resolved into the task message before the loop starts.

## Programmatic API

```python
from ouro_agents.skills import (
    load_startup_skills,    # returns inlined "always" skills as one string
    get_skill_directory,    # returns a `- name: description` listing
    list_skill_names,       # all skill names visible to a workspace
    resolve_skill,          # name -> body or None
    resolve_skills,         # list of names -> list of bodies
    list_builtin_skills,    # built-ins only
)
```

The internal index is cached per-workspace path, so repeated calls are
cheap.
