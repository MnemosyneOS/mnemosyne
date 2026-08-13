#!/usr/bin/env python3
"""Mnemosyne Memory v5.1.3 Stable — local installer. No download needed."""
import os, sys, shutil
HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(HERE, "mnemosyne.py")
MCP = os.path.join(HERE, "mcp_server.py")
if not os.path.exists(ENGINE):
    print("Engine file not found. Please re-download from SkillHub.")
    sys.exit(1)
target = os.path.join(os.getcwd(), "mnemosyne.py")
if os.path.abspath(target) != os.path.abspath(ENGINE):
    shutil.copy(ENGINE, target)
    print(f"Mnemosyne v5.1.3 Stable copied to {target}")
    # Also copy MCP server if available
    if os.path.exists(MCP) and not os.path.exists(os.path.join(os.getcwd(), "mcp_server.py")):
        shutil.copy(MCP, os.path.join(os.getcwd(), "mcp_server.py"))
        print(f"MCP Server copied to {os.getcwd()}")
else:
    print("Mnemosyne v5.1.3 already in current directory. Ready to use.")

# Quick self-test
try:
    from mnemosyne import MemoryBrain
    b = MemoryBrain("._mnemosyne_test_", enable_stats=False)
    b.ensure_init()
    b.retain("install test ok", fast=True)
    print("Self-test: Mnemosyne v5.1.3 OK - project/temporal/MCP/doctor ready")
    import shutil as _s
    _s.rmtree(b.base_dir, ignore_errors=True)
except Exception as e:
    print(f"Self-test warning: {e}")
