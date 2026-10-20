# -*- coding: utf-8 -*-
"""用户删除级联与掩码。


## 为什么「删主表」不够

a common anti-pattern：**删除只删主表，派生结论、摘要、缓存里仍有残留。**

用户说「忘掉我的公司」，如果只把那条记录标成 deleted：

- 由它巩固出来的「用户在甲公司」还活着，下次照样被召回；
- 包含它的会话摘要还在，模型仍能读到；
- 检索缓存里还有它，要等缓存失效才消失。

用户视角就是「我明明让它忘了，它还记得」。这是 GDPR 被遗忘权的技术含义，
也是信任的底线。

## 级联的四个动作

1. **删原始条目** —— 软删（``status=deleted``），不物理删除，保留审计链；
2. **失效派生结论** —— ``derived_from`` 含目标的记录，降权或失效；
3. **清缓存** —— 让下一次检索重新读盘；
4. **标记依赖摘要重算** —— 摘要不自动改写（改写会引入新的漂移），
   只打 ``stale=True`` 让上层决定何时重建。

## 掩码：遮蔽而非删除

Manus 的做法：需要「忘掉」但必须保持上下文连续的部分，**遮蔽而非删除**，
以保护 KV-Cache 前缀稳定性。工程上的意义是：删掉一条消息会让前缀失效、
整段重算；替换成等长占位符则前缀稳定、只损失那一条的信息。

本模块的 ``mask()`` 保留 id、时间戳、标签、关系，只替换正文，并在
``meta.masked`` 里留痕（原内容是否保留由 ``keep_original`` 决定，默认不留）。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence

__all__ = [
    "CascadeManager",
    "DEFAULT_MASK",
]

#: 默认掩码占位符。长度刻意接近常见句子，减少上下文长度突变。
DEFAULT_MASK = "[此条记忆已按用户要求掩码]"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class CascadeManager:
    """级联删除与掩码。

    所有方法都通过 ``brain.store`` 的公开方法操作，不直接碰文件。
    """

    def __init__(self, brain: Any):
        self.brain = brain

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _all(self) -> List[Dict[str, Any]]:
        try:
            return [r for r in self.brain.store.all_records() if isinstance(r, dict)]
        except Exception:
            return []

    @staticmethod
    def _derived_ids(record: Dict[str, Any]) -> List[str]:
        """一条记录声明了「我由哪些记忆推导而来」。三种历史写法都要认。"""
        out: List[str] = []
        meta = record.get("meta") or {}
        if isinstance(meta, dict):
            for key in ("derived_from", "consolidated_from"):
                v = meta.get(key)
                if isinstance(v, (list, tuple)):
                    out.extend(str(x) for x in v if str(x).strip())
        for key in ("derived_from", "consolidated_from"):
            v = record.get(key)
            if isinstance(v, (list, tuple)):
                out.extend(str(x) for x in v if str(x).strip())
        return out

    def _invalidate_cache(self) -> None:
        """清检索缓存与记录缓存，保证下一次读取看到最新状态。"""
        for obj in (getattr(self.brain, "retrieval", None),
                    getattr(self.brain, "store", None)):
            if obj is None:
                continue
            for meth in ("_invalidate_cache", "invalidate_cache", "clear_cache"):
                fn = getattr(obj, meth, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:
                        pass

    # ------------------------------------------------------------------
    # 级联删除
    # ------------------------------------------------------------------

    def delete(self, memory_id: str, hard: bool = False,
               cascade: bool = True,
               reason: str = "user_request") -> Dict[str, Any]:
        """删除一条记忆并级联处理依赖它的东西。

        参数
        ----
        hard:
            ``True`` 时把正文清空（真正不可恢复）；默认 ``False`` 只软删。
            即使 hard=True 也保留 id 与审计记录 —— 「删数据」和「毁审计」是两件事。
        cascade:
            是否失效派生结论。默认 True；关掉只删这一条（用于精确修订场景）。
        """
        target = self.brain.store.find_by_id(memory_id)
        if not target:
            return {"ok": False, "error": "not_found", "id": memory_id}

        report: Dict[str, Any] = {
            "ok": True, "id": memory_id, "reason": reason,
            "deleted": [], "invalidated": [], "stale_summaries": [],
            "cache_cleared": False, "at": _now_iso(),
        }

        updates: Dict[str, Any] = {
            "status": "deleted",
            "deleted_at": _now_iso(),
            "deleted_reason": reason,
        }
        if hard:
            updates["content"] = ""
            updates["hard_deleted"] = True
        try:
            self.brain.store.update_by_id(memory_id, updates)
            report["deleted"].append(memory_id)
        except Exception as exc:
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                    "id": memory_id}

        if cascade:
            records = self._all()
            for r in records:
                rid = str(r.get("id") or "")
                if not rid or rid == memory_id:
                    continue
                if memory_id not in self._derived_ids(r):
                    continue
                meta = r.get("meta") or {}
                kind = str(meta.get("kind") or "") if isinstance(meta, dict) else ""
                if kind in ("session_summary", "topic_summary"):
                    # 摘要不自动改写（改写会引入新漂移），只标 stale
                    try:
                        self.brain.store.update_by_id(rid, {
                            "meta": dict(meta, stale=True,
                                         stale_reason="source_deleted",
                                         stale_at=_now_iso()),
                        })
                        report["stale_summaries"].append(rid)
                    except Exception:
                        pass
                    continue
                # 派生结论：失效而非删除（可能还有其他源支撑）
                remaining = [x for x in self._derived_ids(r) if x != memory_id]
                try:
                    self.brain.store.update_by_id(rid, {
                        "status": "superseded",
                        "superseded_at": _now_iso(),
                        "superseded_reason": "derived_from_deleted",
                        "confidence": max(0.1, float(r.get("confidence") or 0.7) * 0.5),
                        "meta": dict(meta, derived_from=remaining) if isinstance(meta, dict) else meta,
                    })
                    report["invalidated"].append(rid)
                except Exception:
                    pass

        self._invalidate_cache()
        report["cache_cleared"] = True
        report["counts"] = {
            "deleted": len(report["deleted"]),
            "invalidated": len(report["invalidated"]),
            "stale_summaries": len(report["stale_summaries"]),
        }
        return report

    def delete_many(self, memory_ids: Sequence[str], **kwargs: Any) -> Dict[str, Any]:
        """批量删除。逐条走 ``delete()``，保证级联一致。"""
        out = [self.delete(mid, **kwargs) for mid in memory_ids]
        return {
            "ok": all(x.get("ok") for x in out),
            "results": out,
            "total_deleted": sum(len(x.get("deleted") or []) for x in out),
            "total_invalidated": sum(len(x.get("invalidated") or []) for x in out),
        }

    # ------------------------------------------------------------------
    # 掩码
    # ------------------------------------------------------------------

    def mask(self, memory_id: str, replacement: str = DEFAULT_MASK,
             keep_original: bool = False,
             reason: str = "user_request") -> Dict[str, Any]:
        """遮蔽一条记忆的正文，保留结构。

        与 ``delete()`` 的区别：掩码后记录仍然 active、仍参与时间线与关系图，
        只是正文变成占位符。适用于「这段内容不该被读，但它的存在是上下文的一部分」
        —— 例如一次涉及隐私的排障记录，过程可以保留，细节要抹掉。
        """
        target = self.brain.store.find_by_id(memory_id)
        if not target:
            return {"ok": False, "error": "not_found", "id": memory_id}

        old = str(target.get("content") or "")
        _tmeta = target.get("meta") if isinstance(target.get("meta"), dict) else {}
        if target.get("masked") is True or _tmeta.get("masked") is True or old == replacement:
            return {"ok": True, "id": memory_id, "already_masked": True}

        meta = target.get("meta") or {}
        if not isinstance(meta, dict):
            meta = {}
        new_meta = dict(meta)
        new_meta.update({
            "masked": True,
            "masked_at": _now_iso(),
            "masked_reason": reason,
            "masked_length": len(old),
        })
        if keep_original:
            # 显式要求保留时存原文（默认不留 —— 掩码的目的就是不让人读到）
            new_meta["masked_original"] = old

        try:
            self.brain.store.update_by_id(memory_id, {
                "content": replacement,
                "masked": True,
                "meta": new_meta,
            })
        except Exception as exc:
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                    "id": memory_id}

        self._invalidate_cache()
        return {"ok": True, "id": memory_id, "masked": True,
                "original_length": len(old), "kept_original": bool(keep_original)}

    def unmask(self, memory_id: str) -> Dict[str, Any]:
        """撤销掩码（仅当当初 ``keep_original=True``）。"""
        target = self.brain.store.find_by_id(memory_id)
        if not target:
            return {"ok": False, "error": "not_found", "id": memory_id}
        meta = target.get("meta") or {}
        if not isinstance(meta, dict) or not meta.get("masked"):
            return {"ok": False, "error": "not_masked", "id": memory_id}
        original = meta.get("masked_original")
        if not original:
            return {"ok": False,
                    "error": "original_not_kept",
                    "note": "掩码时未保留原文，无法恢复"}
        new_meta = {k: v for k, v in meta.items()
                    if k not in ("masked", "masked_at", "masked_reason",
                                 "masked_length", "masked_original")}
        try:
            self.brain.store.update_by_id(memory_id, {
                "content": original, "masked": False, "meta": new_meta,
            })
        except Exception as exc:
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                    "id": memory_id}
        self._invalidate_cache()
        return {"ok": True, "id": memory_id, "unmasked": True}

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------

    def dependents(self, memory_id: str) -> Dict[str, Any]:
        """查一条记忆被谁依赖（删除前的预检）。

        给「删除确认」对话框用：让用户看到「删了这条会影响这 3 条派生结论」，
        而不是删完才发现画像里少了一块。
        """
        records = self._all()
        direct: List[Dict[str, Any]] = []
        summaries: List[Dict[str, Any]] = []
        for r in records:
            if memory_id not in self._derived_ids(r):
                continue
            meta = r.get("meta") or {}
            kind = str(meta.get("kind") or "") if isinstance(meta, dict) else ""
            item = {"id": r.get("id"),
                    "content": str(r.get("content") or "")[:100],
                    "kind": kind or str(r.get("type") or "")}
            (summaries if kind in ("session_summary", "topic_summary") else direct).append(item)
        return {"id": memory_id, "derived": direct, "summaries": summaries,
                "total": len(direct) + len(summaries)}
