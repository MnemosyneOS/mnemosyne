#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne OS — Single-pass ADD-only fact extraction
===================================================

The reference algorithm, reproduced faithfully and then extended:

**Single pass.** One model call per ``add()``. There is no second call to decide
whether to UPDATE or DELETE — memories accumulate. That is a real design
commitment, not a shortcut: an update pass requires the model to see the existing
memories, which triples the prompt, doubles the latency and introduces a failure
mode where a bad update silently erases a true fact. Appending is monotone, so a
bad extraction can only add noise, never destroy signal — and Mnemosyne's
confidence/verification machinery already handles the "two statements disagree"
case that the update pass used to.

**ADD-only events.** Every returned event has ``event="ADD"``, including events
for MCP/agent-generate facts, matching the reference contract.

**Agent-generated facts are first-class.** A statement the *assistant* confirms
performing (``"I've saved your file to /tmp/x"``) is stored with the same weight
as a user statement, because an agent's own actions are exactly the kind of thing
a later turn needs to know.  This is a reference behaviour that naive extractors
miss, since they only read user turns.

**Entity linking.** Entities are extracted alongside the facts and written to the
graph on the same code path, so hybrid retrieval has an entity signal available
immediately rather than after a backfill.

Unlike the reference, extraction never *fails closed*: if the model is
unavailable or returns unusable output, the handler falls back to
:class:`~mnemosyne.providers.llms.RuleBasedLLM`, which is deterministic and needs
nothing. A memory system that refuses to remember because an API key lapsed is
not a memory system.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from ..providers.json_utils import coerce_str_list, ensure_list, loads_lenient
from .errors import ApiValidationError

__all__ = ["ExtractionResult", "FactExtractor", "DEFAULT_CATEGORIES",
           "extract_facts", "extract_entities", "classify_category"]

#: The default category catalog.  A caller may replace it wholesale through
#: ``custom_categories``; the shapes below cover the reference catalog's intent
#: while remaining usable by the rule-based fallback, which needs regexes rather
#: than free-text descriptions.
DEFAULT_CATEGORIES: List[Dict[str, Any]] = [
    {"name": "personal_details",
     "description": "Name, age, location, language, contact details",
     "patterns": [r"\bmy name is\b", r"\bi(?:'m| am) \d{1,3}\b", r"\bi live in\b",
                  r"\bi(?:'m| am) from\b", r"\bi speak\b"]},
    {"name": "preferences",
     "description": "Likes, dislikes, favourites, style choices, tool choices",
     "patterns": [r"\bi (?:like|love|prefer|enjoy|hate|dislike|avoid)\b",
                  r"\bmy favou?rite\b", r"\bi(?:'m| am) (?:using|not using)\b",
                  r"\bi always\b", r"\bi never\b"]},
    {"name": "professional_details",
     "description": "Job, employer, title, team, clients, industry",
     "patterns": [r"\bi work\b", r"\bi(?:'m| am) a\b", r"\bmy (?:job|role|title|"
                  r"company|team|employer|manager)\b", r"\bi(?:'ve| have) been working\b"]},
    {"name": "health",
     "description": "Allergies, medication, conditions, diet, fitness",
     "patterns": [r"\ballerg", r"\bmedicat", r"\bdiagnos", r"\bi(?:'m| am) on a "
                  r"(?:diet|medication)\b", r"\bmy (?:blood|heart|knee|back)\b"]},
    {"name": "relationships",
     "description": "Family, partner, friends, colleagues by name",
     "patterns": [r"\bmy (?:wife|husband|partner|son|daughter|mother|father|"
                  r"sister|brother|friend|colleague|boss)\b"]},
    {"name": "goals",
     "description": "Objectives, plans, intentions, deadlines",
     "patterns": [r"\bi (?:want|plan|intend|need|hope) to\b", r"\bi(?:'m| am) "
                  r"(?:planning|working) (?:to|on)\b", r"\bby the end of\b",
                  r"\bmy goal\b"]},
    {"name": "technical_interests",
     "description": "Languages, frameworks, tools, hardware, infra",
     "patterns": [r"\bi use\b", r"\bi(?:'m| am) (?:building|writing|developing|"
                  r"learning)\b", r"\bmy (?:stack|setup|server|machine|laptop|gpu)\b"]},
    {"name": "locations",
     "description": "Places visited, moved to, or planned",
     "patterns": [r"\bi(?:'ve| have) (?:been to|moved to|visited)\b",
                  r"\bi(?:'m| am) (?:in|going to|travelling to)\b"]},
    {"name": "dates_and_events",
     "description": "Dated events, appointments, anniversaries, trips",
     "patterns": [r"\bon \d{4}[-/年]\d{1,2}", r"\bnext (?:week|month|year|monday|"
                  r"tuesday|wednesday|thursday|friday|saturday|sunday)\b",
                  r"\blast (?:week|month|year)\b", r"\b\d{1,2}:\d{2}\b"]},
    {"name": "miscellaneous",
     "description": "Durable facts that fit no other category",
     "patterns": []},
]

#: Categories the assistant-voice branch is allowed to produce.
_AGENT_CONFIRMATION = re.compile(
    r"\b(i(?:'ve| have) (?:saved|created|updated|deleted|added|set|booked|"
    r"scheduled|sent|moved|copied|installed|deployed|run|executed))\b", re.I)

_EXTRACTION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fact": {"type": "string"},
                    "category": {"type": "string"},
                    "entities": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number"},
                    "speaker": {"type": "string", "enum": ["user", "agent"]},
                },
                "required": ["fact", "category"],
            },
        }
    },
    "required": ["facts"],
}


class ExtractionResult(dict):
    """One extracted fact plus the metadata the write path needs.

    A ``dict`` subclass so it serialises directly into a record without a
    conversion step, while still documenting its keys as attributes.
    """

    @property
    def fact(self) -> str:
        return self.get("fact", "")

    @property
    def category(self) -> str:
        return self.get("category", "miscellaneous")

    @property
    def entities(self) -> List[str]:
        return list(self.get("entities") or [])

    @property
    def confidence(self) -> float:
        try:
            return float(self.get("confidence", 0.7))
        except (TypeError, ValueError):
            return 0.7

    @property
    def speaker(self) -> str:
        return self.get("speaker", "user")


class FactExtractor:
    """Turns a conversation into a list of :class:`ExtractionResult`.

    Parameters
    ----------
    llm:
        Any :class:`~mnemosyne.providers.base.LLMProvider`.  When ``None`` or
        rule-based, extraction is deterministic and offline.
    instructions:
        The extraction instruction text.
    categories:
        Category catalog to expose to the model; must include ``name`` keys.
    infer:
        When false the extractor is bypassed entirely and each turn is stored
        verbatim — the documented behaviour of ``add(..., infer=False)``.
    """

    def __init__(self, llm: Any = None, instructions: str = "",
                 categories: Optional[List[Any]] = None,
                 max_facts: int = 40):
        self.llm = llm
        self.instructions = instructions
        self.categories = list(categories or DEFAULT_CATEGORIES)
        self.max_facts = int(max_facts)

    # -- public API ---------------------------------------------------------

    def extract(self, messages: List[Dict[str, str]], *,
                memory_type: Optional[str] = None,
                prompt: Optional[str] = None,
                observation_date: Optional[str] = None) -> List[ExtractionResult]:
        """Extract facts from *messages*.

        Never raises for model problems: an unusable response falls through to
        the deterministic path.  The only exception raised is
        ``VALIDATION_003``-style input rejection, which the caller has already
        performed by this point.
        """
        if not messages:
            return []

        if self._is_rule_based():
            return self._extract_rules(messages, memory_type=memory_type)

        try:
            results = self._extract_llm(messages, memory_type=memory_type,
                                        prompt=prompt,
                                        observation_date=observation_date)
        except Exception:
            results = []

        if not results:
            results = self._extract_rules(messages, memory_type=memory_type)
        return results[: self.max_facts]

    def extract_verbatim(self, messages: List[Dict[str, str]]) -> List[ExtractionResult]:
        """Store each turn as-is — the ``infer=False`` path."""
        out: List[ExtractionResult] = []
        for m in messages:
            text = (m.get("content") or "").strip()
            if not text:
                continue
            speaker = "agent" if m.get("role") == "assistant" else "user"
            out.append(ExtractionResult(
                fact=text,
                category="miscellaneous",
                entities=extract_entities(text),
                confidence=0.9 if speaker == "user" else 0.6,
                speaker=speaker,
                source="verbatim",
            ))
        return out

    # -- model path ---------------------------------------------------------

    def _is_rule_based(self) -> bool:
        if self.llm is None:
            return True
        return bool(getattr(self.llm, "is_rule_based", False))

    def _extract_llm(self, messages: List[Dict[str, str]], *,
                     memory_type: Optional[str],
                     prompt: Optional[str],
                     observation_date: Optional[str]) -> List[ExtractionResult]:
        catalog = ", ".join(
            c.get("name", "") if isinstance(c, dict) else str(c)
            for c in self.categories if _cat_name(c)
        )
        system = self.instructions
        if catalog:
            system += f"\n\nAllowed categories: {catalog}."
        if memory_type:
            system += (f"\n\nThis batch is of type '{memory_type}'; prefer that "
                       f"reading when it is ambiguous.")
        if observation_date:
            system += (f"\n\nThe conversation was observed on {observation_date}. "
                       f"Resolve relative dates against it.")
        if prompt:
            system += f"\n\n{prompt}"

        transcript = "\n".join(
            f"{m.get('role', 'user')}: {m.get('content', '')}" for m in messages
        )
        raw = self.llm.complete(
            [{"role": "system", "content": system},
             {"role": "user", "content": transcript}],
            schema=_EXTRACTION_SCHEMA,
            temperature=0.0,
        )
        return self._parse(raw)

    def _parse(self, raw: Any) -> List[ExtractionResult]:
        try:
            parsed = loads_lenient(raw)
        except ValueError:
            return []
        if isinstance(parsed, dict):
            rows = parsed.get("facts")
            if rows is None:
                rows = parsed.get("memories")
            if rows is None:
                # A single fact returned without the envelope.
                rows = [parsed] if parsed.get("fact") else []
        else:
            rows = parsed
        out: List[ExtractionResult] = []
        for row in ensure_list(rows):
            if isinstance(row, str):
                text = row.strip()
                if text:
                    out.append(ExtractionResult(fact=text,
                                                category=classify_category(text),
                                                entities=extract_entities(text),
                                                confidence=0.7, speaker="user",
                                                source="llm"))
                continue
            if not isinstance(row, dict):
                continue
            text = str(row.get("fact") or row.get("memory") or row.get("text") or "").strip()
            if not text:
                continue
            out.append(ExtractionResult(
                fact=text,
                category=str(row.get("category")
                             or classify_category(text)).strip() or "miscellaneous",
                entities=coerce_str_list(row.get("entities")) or extract_entities(text),
                confidence=_clamp(row.get("confidence"), 0.75),
                speaker=str(row.get("speaker") or "user").lower(),
                source="llm",
            ))
        return out

    # -- deterministic path -------------------------------------------------

    def _extract_rules(self, messages: List[Dict[str, str]], *,
                       memory_type: Optional[str]) -> List[ExtractionResult]:
        """Sentence-level extraction with no model and no network.

        Two signals decide what survives: a category pattern (what kind of fact
        this looks like) and an agent-action pattern (whether the assistant
        confirmed doing something).  Both are cheap, and together they give a
        usable precision floor without a model.  The point is not to match an
        LLM's quality — it is that ``add()`` has a correct, offline behaviour
        instead of an error.
        """
        out: List[ExtractionResult] = []
        seen: set = set()

        for m in messages:
            role = str(m.get("role") or "user").lower()
            text = (m.get("content") or "").strip()
            if not text:
                continue
            for sentence in _sentences(text):
                cleaned = _clean(sentence)
                if not _worth_keeping(cleaned):
                    continue
                key = cleaned.lower()
                if key in seen:
                    continue

                category = classify_category(cleaned)
                speaker = "agent" if (role == "assistant" and
                                      _AGENT_CONFIRMATION.search(cleaned)) else \
                          ("agent" if role == "assistant" else "user")
                # An assistant turn with no user-derived content and no confirmed
                # action is chatter; storing it is how a memory fills with noise.
                if speaker == "agent" and not _AGENT_CONFIRMATION.search(cleaned):
                    continue
                if category == "miscellaneous" and speaker == "user" and \
                        not _is_rememberable(cleaned):
                    continue

                seen.add(key)
                out.append(ExtractionResult(
                    fact=cleaned,
                    category=category,
                    entities=extract_entities(cleaned),
                    confidence=0.85 if speaker == "user" else 0.7,
                    speaker=speaker,
                    source="rules",
                ))
                if len(out) >= self.max_facts:
                    return out
        return out


# ---------------------------------------------------------------------------
# Free functions
# ---------------------------------------------------------------------------

def extract_facts(messages: List[Dict[str, str]],
                  llm: Any = None,
                  instructions: str = "",
                  categories: Optional[List[Any]] = None) -> List[ExtractionResult]:
    """Convenience wrapper around :class:`FactExtractor`."""
    return FactExtractor(llm=llm, instructions=instructions,
                         categories=categories).extract(messages)


def extract_entities(text: str) -> List[str]:
    """Pull candidate entities out of *text*.

    Prefers the engine's own extractor (which understands the CJK bigram path
    as well as Latin capitalisation) and degrades to a conservative capitalised
    -token scan if that is unavailable.  Never returns the empty string or
    single characters, both of which pollute the graph with useless nodes.
    """
    if not text:
        return []
    names: List[str] = []
    try:
        from ..utils import _extract_entity_names

        names = list(_extract_entity_names(text) or [])
    except Exception:  # pragma: no cover - utils always present in-tree
        names = []
    if not names:
        names = re.findall(r"\b[A-Z][A-Za-z0-9+#.\-]{1,30}\b", text)
        blocked = {"I", "The", "A", "An", "And", "But", "If", "It", "We", "You"}
        names = [n for n in names if n not in blocked]
    seen, out = set(), []
    for n in names:
        s = str(n).strip()
        if len(s) < 2 or s.lower() in seen:
            continue
        seen.add(s.lower())
        out.append(s)
    return out[:24]


def classify_category(text: str) -> str:
    """Assign a default category by pattern match.

    Used by the deterministic path and as the fallback when a model omits the
    field.  Returns ``"miscellaneous"`` when nothing matches — an honest
    answer, and one that keeps the category distribution meaningful instead of
    inflating whichever category happens to be checked first.
    """
    lowered = (text or "").lower()
    if not lowered:
        return "miscellaneous"
    best = ("miscellaneous", 0)
    for cat in DEFAULT_CATEGORIES:
        score = 0
        for pat in cat.get("patterns") or []:
            try:
                if re.search(pat, lowered):
                    score += 1
            except re.error:  # pragma: no cover - catalog is authored in-tree
                continue
        if score > best[1]:
            best = (cat["name"], score)
    return best[0]


def category_names(categories: Optional[List[Any]] = None) -> List[str]:
    return [n for n in (_cat_name(c) for c in (categories or DEFAULT_CATEGORIES)) if n]


def _cat_name(c: Any) -> str:
    if isinstance(c, dict):
        return str(c.get("name") or "")
    return str(c or "")


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?])|(?<=\.)\s+|(?<=\n)")
_DURABLE_SIGNALS = (
    r"\bi (?:like|love|prefer|use|work|live|am|have|need|want|plan|own)\b",
    r"\bmy \w+", r"\bi(?:'ve| have)\b", r"\bwe (?:use|need|work|plan)\b",
    r"\bremember\b", r"\bimportant\b", r"\bmust\b", r"\balways\b",
    r"\bnever\b", r"\bprefer\b",
    r"我(?:喜欢|用|住在|需要|想要|计划|的)", r"记住", r"重要",
)
_DURABLE_RE = re.compile("|".join(_DURABLE_SIGNALS), re.I)

#: Patterns that mark a sentence as carrying an objective, quotable fact.  Kept
#: separate from the durable-signal set because the two catch different things: a
#: durable signal is about the *speaker* ("I prefer…"), a factual signal is about
#: the *world* ("the invoice number is INV-2024-001"). Requiring only the former
#: silently discarded every third-person declarative, which is the shape most
#: answers actually take.
_FACTUAL_SIGNALS = (
    r"\d",                              # any digit: amounts, counts, years, versions
    r"\b[A-Z]{2,}\b",                   # acronyms and identifiers: INV, SKU, GPU
    r"https?://\S+",                    # URLs
    r"[\w.+-]+@[\w-]+\.[\w.]{2,}",      # email addresses
    r"\b[A-Z][a-z]{2,}\s+[A-Z][a-z]{2,}\b",  # two capitalised words (proper nouns)
    r"[¥$€£]\s?\d",                     # currency amounts
    r"\d+\s*(?:GB|MB|TB|KB|PB|GHz|MHz|kg|g|km|cm|mm|ms|ns|W|kW|V|A|mAh)\b",
    r"\d{4}[-/年]\d{1,2}",              # dated values
    r"\bv?\d+(?:\.\d+){1,3}\b",         # version / dotted numbers
)
_FACTUAL_RE = re.compile("|".join(_FACTUAL_SIGNALS))
_NOISE_RE = re.compile(
    r"^(?:hi|hello|hey|thanks|thank you|ok|okay|sure|yes|no|yeah|nope|"
    r"got it|sounds good|cool|nice|great|please|sorry|goodbye|bye|"
    r"你好|谢谢|好的|嗯|是的|不客气|再见)[.!。！？\s]*$", re.I)


def _sentences(text: str) -> List[str]:
    return [s for s in _SENTENCE_SPLIT.split(text) if s and s.strip()]


def _clean(sentence: str) -> str:
    s = re.sub(r"\s+", " ", sentence).strip()
    s = s.strip("\"'“”‘’ ")
    return s


def _worth_keeping(text: str) -> bool:
    if len(text) < 6 or len(text) > 600:
        return False
    if _NOISE_RE.match(text):
        return False
    if not re.search(r"[A-Za-z\u4e00-\u9fff]", text):
        return False
    # A bare question carries no fact about the asker.
    if text.rstrip().endswith(("?", "？")) and not _DURABLE_RE.search(text):
        return False
    return True


def _has_durable_signal(text: str) -> bool:
    return bool(_DURABLE_RE.search(text))


def _is_rememberable(text: str) -> bool:
    """Whether a plain declarative sentence is worth storing.

    Three independent qualifiers, any of which suffices:

    * a **durable** signal — a first-person preference, habit, possession or plan,
      the classic memory shape;
    * a **factual** signal — a number, identifier, amount, unit, URL, date or a
      pair of proper nouns, i.e. something that can be quoted back verbatim;
    * plain **substance** — at least three content words after stopword removal.

    The third qualifier exists because the first two are marker patterns, and
    marker patterns miss ordinary statements: "This offer expires soon" carries a
    real fact and matched neither. The failure is asymmetric — a dropped fact can
    never be recovered, whereas an extra low-value memory is filtered later by
    deduplication, confidence and importance — so the tie is broken toward
    keeping. Greetings, questions and fragments are already rejected upstream.
    """
    if _DURABLE_RE.search(text) or _FACTUAL_RE.search(text):
        return True
    return _signal_token_count(text) >= 3


#: Words that carry no topic signal in English. Deliberately compact: this runs
#: on the zero-dependency path, where shipping a full stopword table for dozens of
#: languages would cost more than it buys.
_EN_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "am", "do", "does", "did", "done", "has", "have", "had", "having",
    "of", "in", "on", "at", "to", "for", "with", "and", "or", "but", "if",
    "then", "than", "so", "as", "that", "this", "these", "those", "there",
    "here", "it", "its", "he", "she", "they", "them", "his", "her", "their",
    "i", "me", "my", "mine", "we", "us", "our", "you", "your", "yours",
    "what", "which", "who", "whom", "whose", "when", "where", "why", "how",
    "will", "would", "shall", "should", "can", "could", "may", "might",
    "must", "not", "no", "nor", "too", "very", "just", "also", "about",
    "from", "by", "into", "over", "under", "again", "more", "most", "some",
    "any", "all", "each", "other", "such", "only", "own", "same", "now",
})

#: Latin words and CJK runs long enough to be meaningful on their own.
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’\-]*|[\u4e00-\u9fff]{2,}")


def _signal_token_count(text: str) -> int:
    """Count speech-bearing tokens after stopword removal."""
    tokens = [t.lower() for t in _WORD_RE.findall(text or "")]
    return sum(1 for t in tokens
               if len(t) > 1 and t not in _EN_STOPWORDS)


def _clamp(value: Any, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default
