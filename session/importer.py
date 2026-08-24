"""Session conversation importer.

Imports a conversation (list of turns) from a JSON file and retains
each turn into the memory store.  User messages are retained directly;
assistant messages are summarized first.

Zero-dependency: uses only the Python standard library for the core
import logic.  Optional summarization uses a simple extractive approach.

Usage (standalone)::

    python -m session.importer <brain_dir> --file conversation.json

API::

    from session.importer import import_conversation
    result = import_conversation(brain, conversation, session_id="xyz")
"""
import json
import os
import sys
import hashlib

__all__ = ["import_conversation", "ConversationImporter"]


def _summarize(text, max_sentences=3):
    """Simple extractive summarizer: returns the first N sentences.

    Uses only the standard library — splits on sentence boundaries.
    """
    if not text or not text.strip():
        return ""
    # Split on common sentence endings
    import re
    sentences = re.split(r"[。.!?！？]+", text)
    sentences = [s.strip() for s in sentences if s.strip()]
    if not sentences:
        return text[:200]
    return "。".join(sentences[:max_sentences]) + ("。" if sentences else "")


def _extract_entities(text):
    """Extract potential entity names from text (simple keyword approach)."""
    entities = []
    # Extract quoted entities or capitalized proper nouns
    import re
    # Find words in quotes or brackets
    quoted = re.findall(r'[「「]([^」」]+)[」」]', text)
    entities.extend(quoted)
    # For English, find capitalized words
    cap_words = re.findall(r'[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*', text)
    entities.extend(cap_words)
    return entities


class ConversationImporter:
    """Imports conversation turns into a memory store.

    - User messages are retained directly as episodic memories.
    - Assistant messages are summarized and retained as semantic memories.
    - Entities are extracted and stored for graph relationships.
    """

    def __init__(self, brain):
        self.brain = brain

    def import_conversation(self, conversation, session_id=None):
        """Import a conversation (list of turn dicts).

        Parameters
        ----------
        conversation : list[dict]
            Each dict: {"role": "user"|"assistant", "content": str, ...}
        session_id : str or None
            Identifier for the session.  If None, a hash-based ID is generated.

        Returns
        -------
        dict : {imported, retained_ids, summary_ids, relationships}
        """
        if session_id is None:
            content_hash = hashlib.sha256(
                json.dumps(conversation, ensure_ascii=False).encode()
            ).hexdigest()[:12]
            session_id = f"conv-{content_hash}"

        retained_ids = []
        summary_ids = []
        relationships = []

        for i, turn in enumerate(conversation):
            role = turn.get("role", "user")
            content = turn.get("content", "")
            if not content.strip():
                continue

            metadata = {
                "session_id": session_id,
                "turn_index": i,
                "original_role": role,
            }
            # Copy extra fields
            for k, v in turn.items():
                if k not in ("role", "content"):
                    metadata[k] = v

            entities = _extract_entities(content)
            if entities:
                metadata["entities"] = entities

            if role == "user":
                # Retain user message directly as episodic memory
                mid = self.brain._retain_core(
                    content,
                    mtype="episodic",
                    source="conversation_import",
                    importance=turn.get("importance", 5),
                    confidence=turn.get("confidence", 0.9),
                    fact_type="observation",
                    source_type="conversation",
                    tags=turn.get("tags", []),
                    event_time=turn.get("event_time"),
                    session_id=session_id,
                    meta=metadata,
                )
                if mid:
                    retained_ids.append(mid)
                    # Create graph relationships between consecutive turns
                    if retained_ids and len(retained_ids) > 1:
                        relationships.append({
                            "from": retained_ids[-2],
                            "to": mid,
                            "relation": "next_turn",
                        })
            elif role == "assistant":
                # Summarize assistant message
                summary = _summarize(content)
                if not summary:
                    continue
                mid = self.brain._retain_core(
                    summary,
                    mtype="semantic",
                    source="conversation_import",
                    importance=turn.get("importance", 4),
                    confidence=turn.get("confidence", 0.8),
                    fact_type="inference",
                    source_type="conversation",
                    tags=turn.get("tags", []) + ["assistant_summary"],
                    event_time=turn.get("event_time"),
                    session_id=session_id,
                    meta=metadata,
                )
                if mid:
                    summary_ids.append(mid)
                    # Link summary to the preceding user message
                    if retained_ids:
                        relationships.append({
                            "from": retained_ids[-1],
                            "to": mid,
                            "relation": "response_to",
                        })

        return {
            "session_id": session_id,
            "imported": len(retained_ids) + len(summary_ids),
            "retained_ids": retained_ids,
            "summary_ids": summary_ids,
            "relationships": relationships,
        }


def import_conversation(brain, conversation, session_id=None):
    """Import a conversation (list of turn dicts) into the brain.

    Parameters
    ----------
    brain : MemoryBrain
        The brain instance.
    conversation : list[dict]
        Each dict: {"role", "content", ...}
    session_id : str or None
        Session identifier.

    Returns
    -------
    dict : {session_id, imported, retained_ids, summary_ids, relationships}
    """
    importer = ConversationImporter(brain)
    return importer.import_conversation(conversation, session_id)


def _cli():
    """CLI entry point: python -m session.importer <brain_dir> --file conversation.json"""
    if len(sys.argv) < 3:
        print("Usage: python -m session.importer <brain_dir> --file conversation.json [--session-id ID]")
        sys.exit(1)
    brain_dir = sys.argv[1]
    file_path = None
    session_id = None
    if "--file" in sys.argv:
        idx = sys.argv.index("--file")
        file_path = sys.argv[idx + 1]
    if "--session-id" in sys.argv:
        idx = sys.argv.index("--session-id")
        session_id = sys.argv[idx + 1]

    if not file_path:
        print("❌ --file is required")
        sys.exit(1)

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from mnemosyne import MemoryBrain
    brain = MemoryBrain(brain_dir, enable_embeddings=False, enable_stats=False)
    brain.ensure_init()

    with open(file_path, "r", encoding="utf-8") as f:
        conversation = json.load(f)

    result = import_conversation(brain, conversation, session_id=session_id)
    print(f"✅ Imported {result['imported']} memories from {len(conversation)} turns")
    print(f"   Session ID: {result['session_id']}")
    print(f"   Direct retains: {len(result['retained_ids'])}")
    print(f"   Summaries: {len(result['summary_ids'])}")
    print(f"   Relationships: {len(result['relationships'])}")
    brain.close()


if __name__ == "__main__":
    _cli()
