#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Memory Notary - pre-write trust pipeline for Mnemosyne OS v7.0.2.

Implements the four pre-write checks:
1. Source fingerprint / duplicate detection
2. Cross-validation / corroboration (Bayesian confidence update)
3. Injection detection (suspicious flag)
4. Temporal consistency
"""
import re
import hashlib
from datetime import datetime as _dt
from datetime import timezone as _tz

# === Injection detection patterns ===
_INJECTION_PATTERNS = [
    # English instruction keywords
    (r"ignore\s+(all\s+)?previous\s+instructions", "instruction_keywords:ignore previous"),
    (r"ignore\s+all\s+instructions", "instruction_keywords:ignore all"),
    (r"disregard\s+(all\s+)?(previous|prior)", "instruction_keywords:disregard"),
    (r"you\s+are\s+now", "instruction_keywords:you are now"),
    (r"new\s+instructions", "instruction_keywords:new instructions"),
    (r"system\s+prompt", "instruction_keywords:system prompt"),
    (r"reveal\s+(your|the)\s+(system|instructions)", "instruction_keywords:reveal system"),
    (r"say\s+(i\s+am|you\s+are)\s+admin", "instruction_keywords:admin"),
    (r"bypass\s+(all\s+)?(safety|filters|restrictions)", "instruction_keywords:bypass safety"),
    (r"jailbreak", "instruction_keywords:jailbreak"),
    # Chinese instruction keywords
    (r"忽略(之前|所有|以上)(的)?the design requirement", "instruction_keywords:忽略the design requirement"),
    (r"忽略(之前|所有)(的)?(所有)?(the design requirement|提示)", "instruction_keywords:忽略提示"),
    (r"现在说你是(管理员|admin)", "instruction_keywords:管理员"),
    (r"你现在是", "instruction_keywords:你现在是"),
    (r"忽略(之前|所有|以上)", "instruction_keywords:忽略"),
    (r"系统提示(词|语)?", "instruction_keywords:系统提示"),
    (r"绕过(所有|安全)", "instruction_keywords:绕过"),
]

# Imperative start patterns (content starting with imperative)
_IMPERATIVE_START = [
    r"^(please\s+)?(ignore|disregard|forget)\b",
    r"^请(忽略|忘记)\b",
    r"^(reveal|show|display)\s+(your|the)\s+system",
    r"^请(显示|展示|透露)\b",
]


class MemoryNotary:
    """Pre-write trust pipeline for memory records.
    
    Provides four checks that produce a dynamic confidence trajectory:
    1. Source fingerprint / duplicate detection
    2. Cross-validation / corroboration (Bayesian update)
    3. Injection detection (suspicious flag)
    4. Temporal consistency
    """
    
    def __init__(self, base_confidence=0.7):
        self.base_confidence = base_confidence
    
    def check_injection(self, content):
        """Check 3: Injection detection.
        
        Returns (is_suspicious: bool, score: float, flags: list[str]).
        """
        if not content:
            return False, 0.0, []
        
        flags = []
        score = 0.0
        lower = content.lower()
        
        # Check instruction keywords
        for pattern, flag in _INJECTION_PATTERNS:
            if re.search(pattern, lower, re.IGNORECASE):
                flags.append(flag)
                score += 0.3
        
        # Check imperative start
        for pattern in _IMPERATIVE_START:
            if re.search(pattern, lower, re.IGNORECASE):
                flags.append("imperative_start")
                score += 0.3
                break
        
        # Check URL density
        urls = re.findall(r'https?://\S+', content)
        if urls:
            url_ratio = len(urls) / max(len(content.split()), 1)
            if url_ratio > 0.3:
                flags.append("url_density")
                score += 0.4
            elif url_ratio > 0.1:
                flags.append("url_density_low")
                score += 0.2
        
        # Check for suspicious character patterns
        suspicious_chars = sum(1 for c in content if ord(c) > 0xFFFF)
        if suspicious_chars > len(content) * 0.1:
            flags.append("unusual_characters")
            score += 0.2

        # 凭据/隐写检测（复用 utils._injection_score，覆盖密码/API key/不可见字符）
        try:
            from .utils import _injection_score
            inj_score, inj_reasons = _injection_score(content)
            if inj_score >= 0.25:
                score += inj_score
                for r in inj_reasons:
                    if r not in flags:
                        flags.append(r)
        except Exception:
            pass

        is_suspicious = score >= 0.3
        if is_suspicious:
            flags.append("suspicious")
        
        return is_suspicious, min(score, 1.0), flags
    
    def check_duplicate(self, content, existing_records):
        """Check 1: Source fingerprint / duplicate detection.
        
        Returns (is_duplicate: bool, duplicate_ids: list[str], fingerprint: str).
        """
        if not content:
            return False, [], ""
        
        # Normalize content for comparison
        normalized = self._normalize_for_hash(content)
        fingerprint = hashlib.sha256(normalized.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]
        
        dup_ids = []
        for rec in existing_records:
            if not isinstance(rec, dict):
                continue
            rec_content = rec.get("content", "")
            if not rec_content:
                continue
            rec_normalized = self._normalize_for_hash(rec_content)
            if rec_normalized == normalized:
                dup_ids.append(rec.get("id", ""))
        
        return len(dup_ids) > 0, dup_ids, fingerprint
    
    def check_temporal(self, content, record):
        """Check 4: Temporal consistency.
        
        Returns (flags: list[str], penalty: float).
        """
        flags = []
        penalty = 0.0
        now = _dt.now(_tz.utc).replace(tzinfo=None)  # 等价 utcnow()，兼容 3.12 弃用警告
        
        # Extract dates from content (YYYY-MM-DD format)
        dates = self._extract_dates(content)
        for date_str in dates:
            try:
                parsed = _dt.strptime(date_str, "%Y-%m-%d")
                days_ahead = (parsed - now).days
                if days_ahead > 365:  # More than 1 year in the future
                    flags.append("future_date")
                    penalty += 0.1
            except ValueError:
                continue
        
        # Check event_time field
        event_time = record.get("event_time") if isinstance(record, dict) else None
        if event_time and "future_date" not in flags:
            try:
                for fmt in ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"]:
                    try:
                        # Try to parse the event_time string
                        et_str = str(event_time)
                        parsed = _dt.strptime(et_str[:10], "%Y-%m-%d")
                        days_ahead = (parsed - now).days
                        if days_ahead > 365:
                            flags.append("future_date")
                            penalty += 0.1
                        break
                    except ValueError:
                        continue
            except Exception:
                pass
        
        return flags, penalty
    
    def assess(self, record, content, existing_records, max_scan=1000):
        """Run all four checks and produce a confidence assessment.

        性能优化（v7.0.0 局部扫描，替代原 O(n) 全量扫描）：
        - 交叉印证/矛盾检测改为基于实体倒排索引的局部扫描，仅比对与当前
          记录共享实体的已有记忆，而非遍历全部记忆；
        - 重复检测复用记录预计算的 ``template_hash``/``content_hash`` 字段
          （O(1) 比对），避免逐条对 content 做正则归一化；
        - ``existing_records`` 超过 ``max_scan`` 时截断最近条目并标记告警，
          兜底防止极端规模下的 O(n^2) 雪崩。

        Returns dict with:
        - confidence: float (adjusted confidence)
        - flags: list[str]
        - duplicate_ids: list[str]
        - fingerprint: str
        """
        flags = []
        confidence = self.base_confidence

        current_id = record.get("id") if isinstance(record, dict) else None
        entities = record.get("entities", []) if isinstance(record, dict) else []
        entity_set = set(entities) if entities else set()

        normalized = self._normalize_for_hash(content) if content else ""
        fingerprint = (hashlib.sha256(normalized.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]
                       if normalized else "")

        # 截断兜底：超过 max_scan 时仅扫描最近 max_scan 条，并记录告警
        if len(existing_records) > max_scan:
            existing_records = existing_records[:max_scan]
            flags.append("scan_truncated")

        # 一次遍历：构建实体倒排索引 + 收集重复候选（用预计算哈希字段，O(1) 比对）
        entity_index = {}
        dup_ids = []
        for rec in existing_records:
            if not isinstance(rec, dict):
                continue
            rid = rec.get("id")
            if rid == current_id:
                continue
            # duplicate 检测：优先用预计算哈希字段，避免逐条归一化
            th = (rec.get("meta") or {}).get("template_hash") or rec.get("content_hash")
            if th:
                if th == fingerprint:
                    dup_ids.append(rid)
            else:
                rc = rec.get("content", "")
                if rc and self._normalize_for_hash(rc) == normalized:
                    dup_ids.append(rid)
            # 实体倒排
            for e in (rec.get("entities") or []):
                entity_index.setdefault(e, []).append(rec)

        # Check 1: Duplicate detection
        if dup_ids:
            flags.append("duplicate")
            confidence -= 0.1  # Slight penalty for duplicates

        # Check 2: Corroboration —— 实体倒排局部扫描（仅共享实体的记录）
        if entity_set:
            seen = set()
            content_bigrams = MemoryNotary._bigrams(content) if content else set()
            for e in entity_set:
                for rec in entity_index.get(e, []):
                    rid = rec.get("id")
                    if rid in seen:
                        continue
                    seen.add(rid)
                    common = entity_set & set(rec.get("entities", []))
                    if common and rec.get("fact_type") == "fact" and record.get("fact_type") == "fact":
                        rec_content = rec.get("content", "")
                        if self._is_similar(rec_content, content, b2=content_bigrams):
                            if "corroborated_by" not in flags:
                                flags.append("corroborated_by")
                                confidence = min(confidence + 0.1, 1.0)
                        else:
                            if "contradiction" not in flags:
                                flags.append("contradiction")
                                confidence -= 0.15

        # Check 3: Injection detection
        is_susp, inj_score, inj_flags = self.check_injection(content)
        flags.extend(inj_flags)
        if is_susp:
            confidence -= 0.2  # Penalty for suspicious content

        # Check 4: Temporal consistency
        temporal_flags, temporal_penalty = self.check_temporal(content, record)
        flags.extend(temporal_flags)
        confidence -= temporal_penalty

        # Clamp confidence
        confidence = max(0.1, min(confidence, 1.0))

        return {
            "confidence": round(confidence, 3),
            "flags": flags,
            "duplicate_ids": dup_ids,
            "fingerprint": fingerprint,
        }
    
    @staticmethod
    def _normalize_for_hash(text):
        """Normalize text for hashing - remove whitespace/punctuation/case."""
        return re.sub(r"[\s，。！？、；：\"\"''（）《》\[\]{}]", '', text).lower()[:200]
    
    @staticmethod
    def _bigrams(text):
        """字符 bigram 集合（供相似度判定复用，避免重复计算）。"""
        text = text.lower()
        return set(text[i:i + 2] for i in range(len(text) - 1))

    @staticmethod
    def _is_similar(text1, text2, b1=None, b2=None):
        """Check if two texts are nearly identical (for corroboration).
        
        Uses a strict threshold: >0.8 bigram overlap means "same fact stated
        slightly differently" (corroboration). Below that, with shared entities,
        it's a contradiction.

        ``b1``/``b2`` 可传入预计算的 bigram 集合，避免热点重复计算。
        """
        if not text1 or not text2:
            return False
        if b1 is None:
            b1 = MemoryNotary._bigrams(text1)
        if b2 is None:
            b2 = MemoryNotary._bigrams(text2)
        if not b1 or not b2:
            return False
        overlap = len(b1 & b2) / min(len(b1), len(b2))
        return overlap > 0.8
    
    @staticmethod
    def _extract_dates(text):
        """Extract YYYY-MM-DD dates from text."""
        return re.findall(r'(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)', text)


def register(brain):
    """Plugin entry point."""
    return MemoryNotary()
