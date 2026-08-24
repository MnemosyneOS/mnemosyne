"""Example 3: Memory export and import (exchange protocol).

Demonstrates exporting memories to JSONL + manifest, then importing.
"""
import os, sys, tempfile, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mnemosyne import MemoryBrain

tmp = tempfile.mkdtemp()
brain = MemoryBrain(tmp, enable_embeddings=False, enable_stats=False)
brain.ensure_init()

# Store some memories
brain.retain("导出测试记忆 1", fast=True)
brain.retain("导出测试记忆 2", fast=True)
brain.retain("导出测试记忆 3", fast=True)

# Export
export_path = os.path.join(tmp, "export")
os.makedirs(export_path, exist_ok=True)
brain.export_memories(os.path.join(export_path, "memories.jsonl"))

print(f"Exported to: {export_path}")
print(f"Files: {os.listdir(export_path)}")

# Import into a new namespace
brain2 = MemoryBrain(tmp, namespace="imported", enable_embeddings=False, enable_stats=False)
brain2.ensure_init()
brain2.import_memories(os.path.join(export_path, "memories.jsonl"), namespace="imported")

print(f"\nOriginal: {brain._active_count()} active memories")
print(f"Imported: {brain2._active_count()} active memories")

brain.close()
brain2.close()
import shutil
shutil.rmtree(tmp, ignore_errors=True)
