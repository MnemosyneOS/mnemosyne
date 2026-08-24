#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne OS Engine v7.0.0 — Zero-Dependency AI Memory System
===============================================================
"""
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_ROOT)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

__version__ = "7.0.0"
VERSION = "7.0.0"

# === Core classes ===
from .brain import MemoryBrain
from .memory_adapter import MnemosyneMemory

# === Constants ===
MEMORY_TYPES = {
    "semantic", "episodic", "procedural", "preference", "lesson",
    "identity", "reflection", "strategy", "todo", "note",
    "conversation", "fact", "event",
}
MEMORY_LAYERS = {"working", "episodic", "semantic", "procedural", "reflective"}
FACT_TYPES = {"fact", "opinion", "belief", "observation", "inference", "hypothesis"}
SOURCE_TYPES = {"user", "system", "inference", "web_search", "file", "agent_generated", "external"}
VERIFY_STATUS = {"unverified", "verified", "contradicted", "outdated", "superseded"}
DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".mnemosyne")

# === Utilities ===
from .utils import (
    _tokenize, _tf_vector,
    _now_iso, _today_str, _utcnow_ts, _stable_id,
    _fail, _ok, _softmax,
    compress_text,
    _tokenize_preprocess, inject_time_expressions,
    _extract_entities, _extract_entity_names,
    _extract_relationships,
    _injection_score,
    _redact_sensitive_fields,
    _normalize_template_hash, _content_signature, _compute_pair_similarity,
    _memory_value,
    StatsTracker,
)

# === Models ===
from .models import (
    _build_record, _default_layer, _infer_fact_type, _infer_source_type,
    _default_confidence, _auto_importance, _extract_event_time,
    ConsolidationReport, DemoteReport,
)

# === Storage ===
from .storage import MemoryStore

# === Graph ===
from .graph import MemoryGraphStore, _upgrade_record

# === Retrieval ===
from .retrieval import RetrievalEngine

# === Cognitive ===
from .cognitive import CognitiveResolver

# === Notary (v7.0.0) ===
from .notary import MemoryNotary

# === CLI ===
from .cli import main

# === Public API ===
__all__ = [
    "MemoryBrain", "MnemosyneMemory",
    "__version__", "VERSION",
    "MEMORY_TYPES", "MEMORY_LAYERS", "FACT_TYPES",
    "SOURCE_TYPES", "VERIFY_STATUS", "DEFAULT_DIR",
    "_tokenize", "_tf_vector", "_build_record",
    "_now_iso", "_today_str", "_stable_id",
    "compress_text",
    "_extract_entities", "_extract_entity_names", "_extract_relationships",
    "_injection_score",
    "_redact_sensitive_fields",
    "_normalize_template_hash", "_content_signature", "_compute_pair_similarity",
    "_memory_value",
    "ConsolidationReport", "DemoteReport",
    "MemoryStore", "MemoryGraphStore",
    "RetrievalEngine", "CognitiveResolver",
    "MemoryNotary",
    "StatsTracker",
    "main",
]

# === CLI entry point ===
def main_cli(argv=None):
    """CLI entry point for python -m mnemosyne."""
    from .cli import main as _main
    return _main(argv)

if __name__ == "__main__":
    main_cli()
