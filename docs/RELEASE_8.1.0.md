# Mnemosyne OS 8.1.0 — the memory formation layer and the memory runtime layer

**8.0.0 could *find* memories. 8.1.0 can *form* them — and *feed* them back.**

8.0.0 brought retrieval to a world-class level: five-signal fusion, budgeted recall,
a calibrated relevance floor, capsule compression, and the memory exchange protocol.
Auditing that build against common practice exposed two
structural skews:

> **8.0.0 is good at "bringing memories back". It is not good at "turning experience into
> memory", nor at "assembling the right context for the model right now".**

Seven gaps, each measured, each closed in 8.1.0:

| Gap in 8.0.0 | Closed by |
| --- | --- |
| No session summaries — only atomic facts, so "where did we leave off?" had no answer | `session_summarize` / `session_recall` |
| Conflicts detected but never resolved, and the detector false-positived badly | `conflict_resolve` — six classes, four actions, `scope=audit` |
| No task or experience structure — one-off effort could not compound | `task_record` / `playbook_save` / `playbook_recall` |
| No code-evolution memory — a coding agent saw only the current tree | `code_change_record` / `code_archaeology` |
| No sensitivity grading — secrets stored in plaintext and recalled normally | `memory_policy`, plus a policy gate on both recall exits |
| No way to assemble context for the model — `recall` returns facts, not a prompt | `profile_card` / `memory_inject` / `recall_catalog` / `expand_memory` |
| No lifecycle operations — memories were written once and never consolidated, measured, masked or rolled back | `memory_envelope` / `memory_consolidate` / `memory_metrics` / `memory_cascade_delete` / `memory_mask` |

**Tools 31 → 52** — **41 native + 11 client-compatible**, i.e. **21 new native tools**
(9 formation-layer + 12 runtime-layer), five new modules, two design documents.
**No old tool signature changed. No data format changed. No existing record rewritten.**

---

## 1. The memory formation layer (9 tools)

```
session ──► session_summarize ──► seven elements + verbatim anchors, open items split out
conflict ─► conflict_resolve  ──► six classes / four actions; scope=audit reports real conflicts only
task ─────► task_record       ──► append-only facts, retrievable by goal_type
playbook ─► playbook_save     ──► experience, preconditions (must / must_not / typical_env) mandatory
code ─────► code_change_record / code_archaeology
policy ───► memory_policy     ──► sensitivity grading, write admission, legacy-record audit
```

`session_summarize` keeps the seven elements (goal / constraints / decisions / pending /
artifacts / failures / preferences) with verbatim anchors, and splits open items into
stand-alone records so the next session can ask about them.

`playbook_recall` checks preconditions before returning a candidate, so a past solution
is never offered for a context where its assumptions do not hold — it answers
`applicable` / `blocked_by` instead of a bare similarity score.

`code_archaeology` returns the change chain for an anchor, the last three intents, whether
it was ever reverted, and the constraints that were in force — about 500 tokens per call.

---

## 2. The memory runtime layer (12 tools)

These are the read-side and lifecycle operations. They are the difference between a memory
*store* and a memory *system* — and in 8.0.0 they simply did not exist.

**Assembling context for the model**

| Tool | What it does |
| --- | --- |
| `profile_card` | Three-tier user profile (stable identity / preferences & constraints / current situation) + open items, ready to splice into a system prompt. `max_tokens` is a hard ceiling that trims whole tiers in order (situation → pending → preferences) and **never truncates a single fact** — a half-sentence is worse than none. |
| `memory_inject` | The context-injection pipeline: query construction → four-path recall → RRF fusion → rerank → budgeted injection. Returns ready-to-inject text plus a four-bucket quota ledger (profile 30% / task-related 35% / playbook 20% / pending 15%). Over-budget items are dropped whole, not truncated. |
| `recall_catalog` | Progressive disclosure, catalog layer: titles + one-liners + expansion cost only. Judge which few matter, then `expand_memory` those. |
| `expand_memory` | Progressive disclosure, expansion: full text + unified envelope + **graph neighbours** + evidence chain. The neighbours are where the answer to "which modules did that decision affect" actually lives. |

**The memory lifecycle**

| Tool | What it does |
| --- | --- |
| `memory_envelope` | Unified envelope for a record: eight-way classification (semantic / episodic / procedural / working / resource / anti_pattern / meta / relational), four-dimension scope (user / project / agent / session), evidence chain, dual timeline (`t_valid` / `t_invalid`), TTL and SPO structure. Without `memory_id` it returns the whole-library eight-way distribution. |
| `memory_consolidate` | Three consolidation paths — episodic→semantic (same fact in ≥3 sessions), episodic→procedural (same task succeeded ≥2 times → a playbook with preconditions), failure→anti-pattern (`failed` / `rolled_back` / `reverted` → a prohibition entry). Idempotent: the same source batch never generates twice. |
| `memory_metrics` | Chat 5 + Working 4 + Coding 4 indicators. **Honesty rule**: an indicator without enough data returns `{value: null, status: "insufficient_data", need: "..."}` rather than `0` — because `0` and "no data" are different things. |
| `memory_cascade_delete` | Four actions: soft-delete the original → invalidate derived conclusions (confidence halved + `superseded`) → clear the retrieval cache → mark dependent summaries `stale`. Run `dry_run=true` first to see the dependents. `reason` is written to the audit trail. |
| `memory_mask` | Replaces the body with a placeholder but keeps the record skeleton — gentler than deletion, and it preserves context length and prefix, so KV-Cache reuse is not broken. Prefer this when the user says "don't remember that". `action=unmask` reverts it. |
| `topic_summarize` | L3 topic summary: merges the L2 session summaries under one project into "what state is this project in". Merge rules are deterministic — goal takes the newest, decisions accumulate, pending keeps only the newest. |
| `memory_trace` | Walks the pointer chain L3→L2→L1 and returns each level's content, the turn ranges it covers and the `message_ids`. Answers "which sentence did this conclusion come from". The memory store keeps no L0 text, so the caller resolves `message_ids` against its own conversation history. |
| `memory_hot_path` | Hot-path buffer: `enable` switches to async writes (the hot path only enqueues; extraction / conflict / consolidation run in the background), `status` shows queue and stats, `flush` drains the queue — a read-consistency barrier you must call before reading back something you just wrote. |

---

## Defects fixed

1. **`reflect` conflict false positives** — entity co-occurrence is no longer counted as a
   conflict. Detection now delegates to `ConflictResolver.audit()` with assertion-aligned
   criteria. Measured: 8.0.0's 14 false positives → **0**; 7/7 positive/negative controls pass.
2. **`_detect_conflicts_at_write` recorded but never resolved** — results went into
   `meta.write_conflicts` and were never consumed. Now returns `kind` / `action` / `reason`
   and callers act on `action`.
3. **Sensitivity gate was not on the default recall path** — the first 8.1.0 build shipped
   only the `memory_policy` tool, so users who never called it were entirely unprotected.
   `Brain.recall()` and `_budget_recall()` both pass through `_apply_policy_filter()` now:
   `secret` never enters context, `private` passes in internal context, `normal` always passes.
4. **Chinese entity extraction split words, hiding conflicts** — "该方案是安全的" and
   "该方案是不安全的" were sliced into different fragments, so the two memories shared no
   entity and never paired. Added `_ent_key()` (predicate-based entity head) and
   `_ent_bigram()` bucketing.
5. **`recall_health` instrumentation was incomplete** — under `namespace=default` the metrics
   stayed at 0. Marked as a known issue in the 8.1.0 verification script, scheduled for 8.2.0.

---

## Compatibility

- The old **20 native tools** keep their parameters, return shapes and error codes. The
  11 client-compatible tools are unchanged too.
- New fields (`sensitivity`, `task_id`, `goal_type`, …) are all optional and default when absent.
- Existing data is not rewritten: sensitivity is recomputed at read time, the stored record
  is left untouched.
- Policy defaults are permissive: `sensitivity` defaults to `normal`, so existing writes behave as before.

---

## Verification

Both suites run against the 8.1.0 source and pass with zero failures.

| Check | Result |
| --- | --- |
| `python scripts/verify_v810.py` | **40 / 40 passing** |
| `python scripts/verify_api.py` | **29 / 29 passing** |

`verify_api.py` also exercises the live REST surface (`18 REST surface` checks) and the CLI
envelope; it spins up an ephemeral local listener on `127.0.0.1` and tears it down.

---

## Install

```
pip install mnemosyne-os        # zero third-party dependencies
```

```python
from mnemosyne import Memory

m = Memory()
m.add("I prefer dark mode and use vim keybindings. My name is Alice.", user_id="alice")
m.search("what does alice prefer", user_id="alice")
```

**MCP — 52 tools** (41 native + 11 client-compatible) · **REST** — `/v1`, `/v2`, `/v3` on the console's listener ·
**CLI** — compatible command set with `--agent` JSON envelopes ·
**Python** — `Memory` / `AsyncMemory` / `MemoryClient`

Full changelog: [`CHANGELOG.md`](https://github.com/MnemosyneOS/mnemosyne/blob/main/CHANGELOG.md)
