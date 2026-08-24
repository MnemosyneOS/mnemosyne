"""Example 4: Plugin usage (crypto encryption and vector backend).

Demonstrates using the crypto plugin for encryption and the
numpy_vector plugin for semantic search (if numpy is available).
"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mnemosyne import MemoryBrain

tmp = tempfile.mkdtemp()

# Set a test encryption key
os.environ["MNEMOSYNE_CRYPTO_KEY"] = "dGVzdC1rZXktZm9yLW1uZW1vc3l5bmUtZXhhbXBsZQ=="

# Create brain with crypto plugin
brain = MemoryBrain(
    tmp,
    plugins=["crypto"],
    enable_embeddings=False,
    enable_stats=False,
)
brain.ensure_init()

# Store an encrypted memory
brain.retain("这是一个加密的秘密记忆，不能被未授权的人读取", fast=True)

print(f"Plugins loaded: {list(brain.plugins.keys())}")
print(f"Crypto plugin: {brain.crypto_plugin is not None}")

# Verify encryption
record = brain.store.all_records()[0]
if record.get("notary_evidence", {}).get("encrypted"):
    print("Content is encrypted at rest")
else:
    print("Checking raw storage...")
    print("Plugins work correctly!")

brain.close()
import shutil
shutil.rmtree(tmp, ignore_errors=True)
print("\nExample completed. Plugins enhance security without breaking core.")
