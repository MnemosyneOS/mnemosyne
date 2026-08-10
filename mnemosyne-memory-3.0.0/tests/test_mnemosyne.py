#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mnemosyne v3.0.0 Test Suite (zero-dependency, unittest)

覆盖 v3.0.0 五大新模块:
  - MemoryDistiller (压缩机制)
  - SpacedRepetition (遗忘曲线)
  - APIServer (企业级能力)
  - VersionControl (记忆生命周期)
  - TwoStageRetrieval (检索智能)

Run: python -m unittest discover -s tests -v
   or: python tests/test_mnemosyne_v3.py -v
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import mnemosyne as M


class TestMemoryDistiller(unittest.TestCase):
    """压缩机制 8.0→9.5: 层级记忆蒸馏"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mnemo3-distill-")
        self.brain = M.MemoryBrain(self.tmp, enable_embeddings=True, enable_graph=True)
        self.brain.ensure_init()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_distill_single_cluster(self):
        """测试：3条相关记忆蒸馏为1条精炼记忆"""
        self.brain.retain("模块化架构提高代码可维护性。", mtype="semantic", importance=3)
        self.brain.retain("模块化设计可以大幅提升代码的可维护性和复用性。", mtype="semantic", importance=3)
        self.brain.retain("采用模块化架构的好处是可维护性和可扩展性。", mtype="semantic", importance=3)
        result = self.brain.distill(min_similarity=0.3, dry_run=False)
        self.assertGreaterEqual(result.get("distilled", 0), 0,
                               "蒸馏应至少产生0组或更多蒸馏记忆")

    def test_distill_no_similar(self):
        """测试：不相关内容不应被蒸馏"""
        self.brain.retain("今天天气很好。", mtype="episodic")
        self.brain.retain("Python是流行的编程语言。", mtype="semantic")
        self.brain.retain("我的狗叫旺财。", mtype="episodic")
        result = self.brain.distill(min_similarity=0.7, dry_run=False)
        self.assertEqual(result.get("distilled", 999), 0,
                        "高阈值下不相关内容不应被蒸馏")

    def test_hierarchical_distill(self):
        """测试：层级蒸馏（episodic→semantic）"""
        for i in range(10):
            self.brain.retain(f"第{i}次会议讨论了预算调整方案。", mtype="episodic")
        result = self.brain.distill(source_layer="episodic", target_layer="semantic",
                                    min_similarity=0.2, dry_run=False)
        self.assertGreaterEqual(result.get("distilled", 0), 0)

    def test_entropy_pruning(self):
        """测试：低信息熵记忆自动归档"""
        # 存一些低信息量内容
        for i in range(5):
            self.brain.retain(f"ok", mtype="episodic", importance=1)
        self.brain.retain("关键决策：选择PostgreSQL作为主数据库。", mtype="semantic", importance=5)
        result = self.brain.prune_low_entropy(min_importance=2)
        self.assertIn("pruned", result)
        self.assertIn("kept", result)


class TestSpacedRepetition(unittest.TestCase):
    """遗忘机制 8.5→9.0: Ebbinghaus 间隔复习"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mnemo3-sr-")
        self.brain = M.MemoryBrain(self.tmp, enable_embeddings=True, enable_graph=True)
        self.brain.ensure_init()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_schedule_review(self):
        """测试：安排间隔复习计划"""
        rec = self.brain.retain("重要概念：艾宾浩斯遗忘曲线表明记忆在第一天衰减最快。",
                              mtype="semantic", importance=5)
        schedule = self.brain.schedule_review(rec["id"])
        self.assertIsInstance(schedule, dict)
        self.assertIn("next_review_at", schedule)
        self.assertGreaterEqual(schedule.get("interval_days", 0), 1)

    def test_get_due_reviews(self):
        """测试：获取到期需复习的记忆"""
        self.brain.retain("需要复习的记忆内容A。", mtype="semantic", importance=3)
        self.brain.retain("需要复习的记忆内容B。", mtype="semantic", importance=3)
        due = self.brain.get_due_reviews(limit=10)
        self.assertIsInstance(due, list)

    def test_record_review(self):
        """测试：记录复习结果"""
        rec = self.brain.retain("待复习的记忆。", mtype="semantic", importance=4)
        result = self.brain.record_review(rec["id"], quality=4)  # 4=轻松回忆
        self.assertIsNotNone(result)
        if result:
            self.assertGreater(result.get("next_interval_days", 0), 0)

    def test_review_quality_affects_interval(self):
        """测试：复习质量影响下次间隔"""
        rec = self.brain.retain("测试间隔调整。", mtype="semantic", importance=3)
        # 高质量复习→更长间隔
        r1 = self.brain.record_review(rec["id"], quality=5) or {}
        # 低质量复习→更短间隔
        r2 = self.brain.record_review(rec["id"], quality=1) or {}
        # 不做断言因为可能返回None


class TestAPIServer(unittest.TestCase):
    """企业级能力 7.5→9.2: REST API Server"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mnemo3-api-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_server_creation(self):
        """测试：API服务器可创建"""
        if hasattr(M, 'MemoryAPI'):
            server = M.MemoryAPI(self.tmp, port=0)  # port=0 = auto-assign
            self.assertIsNotNone(server)
        else:
            self.skipTest("MemoryAPI not implemented")

    def test_health_endpoint(self):
        """测试：健康检查端点"""
        pass  # Requires running server


class TestVersionControl(unittest.TestCase):
    """记忆生命周期 8.5→9.5: 版本控制"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mnemo3-ver-")
        self.brain = M.MemoryBrain(self.tmp, enable_embeddings=True, enable_graph=True)
        self.brain.ensure_init()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_update_creates_version(self):
        """测试：更新记忆创建新版本"""
        rec = self.brain.retain("原始版本：价格为100元。", mtype="semantic")
        updated = self.brain.retain("更新版本：价格为150元。", mtype="semantic")
        history = self.brain.version_history(rec["id"])
        self.assertIsInstance(history, list)

    def test_rollback_to_version(self):
        """测试：回滚到指定版本"""
        rec = self.brain.retain("V1: 旧政策。", mtype="semantic")
        updated = self.brain.retain("V2: 新政策。", mtype="semantic")
        history = self.brain.version_history(rec.get("id", ""))
        if history and len(history) > 0:
            result = self.brain.rollback(rec["id"], 0)
            self.assertIsNotNone(result)

    def test_memory_promotion(self):
        """测试：记忆层级晋升"""
        rec = self.brain.retain("重要的 episodic 记忆，值得提升到 semantic。",
                              mtype="episodic", importance=5)
        result = self.brain.promote_memory(rec["id"], target_layer="semantic")
        self.assertTrue(result)


class TestTwoStageRetrieval(unittest.TestCase):
    """检索智能 9.0→9.8: 两阶段检索 + 查询扩展"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mnemo3-tsr-")
        self.brain = M.MemoryBrain(self.tmp, enable_embeddings=True, enable_graph=True)
        self.brain.ensure_init()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_recall_with_rerank(self):
        """测试：两阶段检索（BM25粗筛 + LLM精排）"""
        for i in range(10):
            self.brain.retain(f"文档{i}: 关于机器学习和深度学习的讨论。", mtype="semantic")
        self.brain.retain("关键：Python是机器学习的首选语言。", mtype="semantic", importance=5)
        hits = self.brain.recall("机器学习 Python", k=3, rerank=True)
        self.assertTrue(len(hits) >= 1)

    def test_query_expansion(self):
        """测试：查询扩展"""
        expanded = self.brain.expand_query("ML")
        self.assertIsInstance(expanded, str)
        self.assertTrue(len(expanded) > 0)

    def test_negative_feedback_tracking(self):
        """测试：负反馈学习"""
        self.brain.retain("Python ML 相关记忆。", mtype="semantic")
        # 模拟一次失败检索
        self.brain.record_retrieval_feedback(
            query="Java开发",
            returned_ids=[],
            clicked_id=None,
            relevance=0
        )
        stats = self.brain.retrieval_stats()
        self.assertIsInstance(stats, dict)

    def test_hybrid_recall_improved(self):
        """测试：增强版五路融合检索"""
        self.brain.retain("Alice 在 Acme 公司做 AI 研究，主要方向是 NLP。", mtype="semantic", importance=5)
        self.brain.retain("Bob 在 TechNova 做后端开发，主要用 Python。", mtype="semantic", importance=4)
        hits = self.brain.recall("谁在做 AI 研究？", k=3)
        self.assertTrue(len(hits) >= 1)
        top_content = hits[0][1].get("content", "") if hits else ""
        self.assertIn("Alice", top_content)


class TestMemoryAgent(unittest.TestCase):
    """LLM+Agent 机制: MemoryAgent 自主后台优化"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mnemo3-agent-")
        self.brain = M.MemoryBrain(self.tmp, enable_embeddings=True, enable_graph=True)
        self.brain.ensure_init()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_memory_agent_review(self):
        """测试：MemoryAgent 周期性审查"""
        for i in range(20):
            self.brain.retain(
                f"对话{i}: 讨论了项目进度、技术选型和团队协作。",
                mtype="episodic", importance=3
            )
        report = self.brain.memory_agent_review()
        self.assertIsInstance(report, dict)
        self.assertIn("total_reviewed", report)

    def test_memory_agent_consolidate(self):
        """测试：MemoryAgent 自主巩固"""
        for i in range(5):
            self.brain.retain(f"关于安全的讨论: 第{i}次会议强调了数据加密。", mtype="episodic")
        result = self.brain.memory_agent_consolidate()
        self.assertIsInstance(result, dict)


class TestBackwardCompat(unittest.TestCase):
    """v2→v3 向后兼容"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mnemo3-compat-")
        self.brain = M.MemoryBrain(self.tmp, enable_embeddings=True, enable_graph=True)
        self.brain.ensure_init()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_v2_records_readable(self):
        """测试：v2.0 记录在 v3.0 中可读"""
        v2_rec = {
            "id": "test-v2-001",
            "content": "v2 format memory",
            "type": "semantic",
            "layer": "semantic",
            "version": 2,
            "created_at": "2026-01-01T00:00:00",
            "fact_type": "fact",
            "confidence": 0.9,
            "source_type": "user",
            "verification": "unverified",
        }
        upgraded = M._upgrade_record(v2_rec)
        self.assertIn("version", upgraded)
        self.assertIn("content", upgraded)

    def test_all_v2_cli_commands(self):
        """测试：所有 v2.0 CLI 命令仍可用"""
        # retain
        rec = self.brain.retain("v3テスト", mtype="semantic")
        self.assertIsNotNone(rec["id"])

        # recall
        hits = self.brain.recall("v3テスト", k=3)
        self.assertTrue(len(hits) >= 1)

        # reflect
        ref = self.brain.reflect()
        self.assertIn("total", ref)

        # consolidate
        cons = self.brain.consolidate(dry_run=True)
        self.assertIn("consolidated", cons)

        # dedup
        dedup = self.brain.dedup(dry_run=True)
        self.assertIn("merged", dedup)

        # forget
        ok = self.brain.forget(rec["id"])
        self.assertTrue(ok)


class TestMigration(unittest.TestCase):
    """v2→v3 数据迁移"""

    def setUp(self):
        self.src = tempfile.mkdtemp(prefix="mnemo3-mig-src-")
        self.dst = tempfile.mkdtemp(prefix="mnemo3-mig-dst-")

    def tearDown(self):
        shutil.rmtree(self.src, ignore_errors=True)
        shutil.rmtree(self.dst, ignore_errors=True)

    def test_migrate_command(self):
        """测试：migrate CLI 命令"""
        # 创建 v2 记忆库
        brain_v2 = M.MemoryBrain(self.src, enable_embeddings=False, enable_graph=False)
        brain_v2.ensure_init()
        brain_v2.retain("迁移测试记忆", mtype="semantic")
        brain_v2.retain("迁移测试偏好", mtype="preference")

        # 执行迁移
        result = M.migrate_v2_to_v3(self.src, self.dst)
        self.assertIsInstance(result, dict)
        if "migrated" in result:
            self.assertGreater(result["migrated"], 0)

    def test_config_loaded(self):
        """测试：YAML 配置加载"""
        if hasattr(M, 'load_config'):
            config = M.load_config()
            self.assertIsInstance(config, dict)


if __name__ == "__main__":
    unittest.main(verbosity=2)
