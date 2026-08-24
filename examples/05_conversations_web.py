"""Example 5: Conversation history and web management interface.

Demonstrates storing conversation turns and starting the web UI.
"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mnemosyne import MemoryBrain

tmp = tempfile.mkdtemp()
brain = MemoryBrain(tmp, enable_embeddings=False, enable_stats=False)
brain.ensure_init()

# Store conversation turns
session_id = "demo-session-001"
brain.add_conversation_turn(session_id, "user", "你好,请介绍一下苹果公司")
brain.add_conversation_turn(session_id, "assistant", "苹果公司成立于1976年,由史蒂夫·乔布斯创立")
brain.add_conversation_turn(session_id, "user", "谷歌呢?")
brain.add_conversation_turn(session_id, "assistant", "谷歌成立于1998年")

# Search conversations
results = brain.search_conversations("苹果", session_id=session_id)
print(f"Conversation search results: {len(results)}")
for r in results:
    print(f"  [{r['role']}] {r['content'][:40]}")

# Build a context snapshot
snapshot = brain.build_context_prompt(query="苹果公司", max_chars=500)
print(f"\nSnapshot ({len(snapshot)} chars):")
print(snapshot[:200])

print(f"\nTo start the web interface:")
print(f"  python -c \"from web_server import run_server; run_server(port=9090, base_dir='{tmp}')\"")

brain.close()
import shutil
shutil.rmtree(tmp, ignore_errors=True)
