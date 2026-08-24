"""Example 1: Basic memory retain and recall.

Demonstrates the core API for storing and retrieving memories.
"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mnemosyne import MemoryBrain

tmp = tempfile.mkdtemp()
brain = MemoryBrain(tmp, enable_embeddings=False, enable_stats=False)
brain.ensure_init()

# Retain some memories
brain.retain("苹果公司成立于1976年,由乔布斯创立", fast=True)
brain.retain("谷歌公司成立于1998年", fast=True)
brain.retain("微软公司成立于1975年,由盖茨创立", fast=True)

# Recall
print("=== Recall: 苹果 ===")
results = brain.recall("苹果", k=3)
for score, record, reasons in results:
    print(f"  Score: {score:.4f} | Content: {record['content'][:40]}")

print("\n=== Recall: 1998年 ===")
results = brain.recall("1998年", k=3)
for score, record, reasons in results:
    print(f"  Score: {score:.4f} | Content: {record['content'][:40]}")

print("\n=== Stats ===")
stats = brain.stats()
print(f"  Total memories: {stats.get('total', 0)}")

brain.close()
import shutil
shutil.rmtree(tmp, ignore_errors=True)
