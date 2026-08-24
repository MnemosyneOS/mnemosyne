#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne OS Engine v7.0.0 — AI Memory Operating System
============================================================
Zero-dependency, cross-platform, multi-language AI Agent memory engine.

This is a thin facade that re-exports the public API from the
mnemosyne package for backward compatibility.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

__version__ = "7.0.0"
VERSION = __version__

from mnemosyne import (
    MemoryBrain,
    MnemosyneMemory,
    __version__, VERSION,
    MEMORY_TYPES, MEMORY_LAYERS, FACT_TYPES,
    SOURCE_TYPES, VERIFY_STATUS, DEFAULT_DIR,
)
from mnemosyne.cli import main as _cli_main

def main(argv=None):
    """CLI entry point."""
    return _cli_main(argv)

if __name__ == "__main__":
    sys.exit(main() or 0)
