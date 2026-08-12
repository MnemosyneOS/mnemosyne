#!/usr/bin/env python3
"""Mnemosyne Memory v4.0.0 Stable — zero-download installer."""
import os, sys, shutil
HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(HERE, "mnemosyne.py")
if not os.path.exists(ENGINE):
    print("⚠ Engine file not found. Please re-download from SkillHub.")
    sys.exit(1)
target = os.path.join(os.getcwd(), "mnemosyne.py")
if os.path.abspath(target) == os.path.abspath(ENGINE):
    print("✓ Mnemosyne v4.0.0 Stable already in current directory. Ready to use.")
else:
    shutil.copy(ENGINE, target)
    print(f"✓ Mnemosyne v4.0.0 Stable copied to {target}")
