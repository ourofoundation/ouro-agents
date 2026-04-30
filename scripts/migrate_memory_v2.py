#!/usr/bin/env python
"""Best-effort Chroma metadata migration for memory schema v2.

This script is intentionally conservative: it normalizes metadata through the
same helpers used at runtime and writes only metadata updates. Run it from the
ouro-agents package root after backing up the configured memory directory.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from chromadb import PersistentClient

from ouro_agents.memory.model import memory_item_from_raw, to_metadata


def migrate(chroma_path: Path, collection_name: str = "ouro_agent_memory") -> int:
    client = PersistentClient(path=str(chroma_path))
    collection = client.get_or_create_collection(collection_name)
    batch = collection.get(include=["metadatas", "documents"])
    ids = batch.get("ids", [])
    metadatas = batch.get("metadatas", [])
    documents = batch.get("documents", [])

    migrated = 0
    for memory_id, metadata, document in zip(ids, metadatas, documents):
        metadata = metadata or {}
        text = document or metadata.get("data") or metadata.get("memory") or ""
        item = memory_item_from_raw(text, metadata or {})
        new_metadata = dict(metadata)
        new_metadata.update(to_metadata(item))
        collection.update(ids=[memory_id], metadatas=[new_metadata])
        migrated += 1
    return migrated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--chroma-path",
        type=Path,
        required=True,
        help="Path to the Chroma directory, e.g. workspace/memory/chroma",
    )
    parser.add_argument("--collection", default="ouro_agent_memory")
    args = parser.parse_args()
    count = migrate(args.chroma_path, args.collection)
    print(f"Migrated {count} memories to schema_version=2 metadata.")


if __name__ == "__main__":
    main()
