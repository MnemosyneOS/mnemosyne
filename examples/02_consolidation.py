"""Example 2: Memory consolidation (summary compression).

Demonstrates merging similar memories and generating summaries.
"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mnemosyne import MemoryBrain

def _active_count(brain):
    return len([r for r in brain.store.all_records()
                if r.get("status", "active") in ("active", "working")])

tmp = tempfile.mkdtemp()
brain = MemoryBrain(tmp, enable_embeddings=False, enable_stats=False)
brain.ensure_init()

# Store similar memories
for i in range(10):
    brain.retain("苹果公司的信息: 苹果成立于1976年,总部位于美国加利福尼亚州", fast=True)

before = _active_count(brain)
print(f"Before consolidation: {before} active memories")

# Consolidate
report = brain.consolidate(min_similarity=0.6, generate_summary=True)
after = _active_count(brain)
print(f"After consolidation: {after} active memories")
print(f"  Groups: {len(report.all_groups)}")
print(f"  Total merges: {len(report.merge_plan)}")

brain.close()
import shutil
shutil.rmtree(tmp, ignore_errors=True)
