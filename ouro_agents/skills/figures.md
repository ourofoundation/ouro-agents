---
description: Mermaid and SVG figures for posts — pick the format the frontend already renders, keep the source as data
load: stub
---

# Figures

Prefer figures whose **source is the artifact**: mermaid text, SVG markup, or
a dataset view. The frontend already renders all three. Raster PNG/JPEG from
matplotlib, screenshots, or image-gen models is a last resort — the picture
is opaque and cannot be edited or themed.

Do not install or call mermaid-cli, Kroki, tldraw, Excalidraw, or HTML
diagram wrappers. Ouro posts render ` ```mermaid ` fences and inline `<svg>`
directly (see `ouro_markdown`).

## Pick a format

| Need | Format |
|------|--------|
| Flow, sequence, state, architecture, Gantt, mind map | **Mermaid** in the post body |
| Custom schematic, labeled drawing, one-off illustration | **SVG** (inline in the post, or `create_file`) |
| Chart of tabular data you already have as a dataset | **Dataset view** (`write_dataset_view`) + embed with `displayConfig.visualizationId` |
| Plot from numbers you computed in `run_python` | **SVG** from the plotting library (`savefig(..., format="svg")`), then `create_file` or inline |
| Pixel-perfect screenshot, photo, or a format Ouro doesn't render | PNG file — only when the above cannot express it |

Mermaid wins when layout can be automatic. SVG wins when you need exact
geometry, axes you drew yourself, or a picture mermaid has no diagram type
for. Dataset views win when the chart should stay live with the table.

## Mermaid

Put the fence in the post. Typical types: `flowchart`, `sequenceDiagram`,
`stateDiagram-v2`, `erDiagram`, `gantt`, `mindmap`, `pie`.

Keep it readable: short labels, few nodes, `LR`/`TB` chosen for the shape
of the graph. If a mermaid diagram is cramped or the syntax fails to
render, simplify the source — do not rasterize it to "fix" it.

Upload a `.mmd` file only when the diagram should be a reusable asset
outside a post.

## SVG

Write a single self-contained `<svg>`:

- `xmlns="http://www.w3.org/2000/svg"` and a `viewBox` that fits the content
  with padding. Omit fixed `width`/`height` so it scales in previews.
- System font stacks only (`sans-serif`, `ui-sans-serif`). No external CSS
  or font URLs — the inline viewer will not load them reliably.
- No `<script>`, no `on*` handlers (stripped on ingest anyway).
- Light, high-contrast fills. File previews sit on a white background.

**Inline in a post** when the figure belongs to that narrative. **File
asset** when it should be reused, versioned, or linked from several posts
(`create_file` then an `assetComponent` embed with `viewMode: "preview"`).

If matplotlib (or similar) is available in the sandbox, emit SVG, not PNG:

```python
from pathlib import Path
fig.savefig(Path("scratch/curie-vs-cost.svg"), format="svg")
```

Then `load_tool(["ouro:create_file"])` and embed the file. Do not paste a
giant base64 PNG into markdown.

## Dataset charts

If the numbers already live in a dataset, do not redraw them as a static
figure. Create or reuse a saved view and embed the dataset:

```assetComponent
{"id": "<dataset-uuid>", "assetType": "dataset", "viewMode": "preview", "displayConfig": {"visualizationId": "<view-uuid>"}}
```

## When not to figure

A two-row comparison, a single number, or a relationship that one sentence
states clearly does not need a diagram. Use a table or prose instead.
