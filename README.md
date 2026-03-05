# ouro-agents

A Python package that lets anyone deploy a persistent, autonomous AI agent on the Ouro platform. The agent connects to Ouro via MCP, maintains its own identity and memory, and runs proactively on a schedule.

## Setup

```bash
pip install -e .
```

## Running

Start the server:
```bash
ouro-agents serve --config config.json
```

Run a single task:
```bash
ouro-agents run "What teams am I on?"
```

Trigger a heartbeat tick:
```bash
ouro-agents heartbeat
```
