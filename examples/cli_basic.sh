#!/usr/bin/env bash
# Mnemosyne CLI 基本用法示例（bash）
# 说明：核心零依赖，仅需 Python 3.8+。入口为仓库根目录的 mnemosyne.py。

set -euo pipefail

# 进入仓库根目录（脚本位于 examples/ 下）
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# 使用临时目录作为记忆库，避免污染用户主目录
MEM_DIR="$(mktemp -d)"

echo "== 1) 初始化记忆库 =="
python mnemosyne.py --dir "$MEM_DIR" init

echo "== 2) 写入几条记忆 =="
python mnemosyne.py --dir "$MEM_DIR" retain --content "苹果公司成立于1976年"
python mnemosyne.py --dir "$MEM_DIR" retain --content "谷歌公司成立于1998年"

echo "== 3) 检索记忆 =="
python mnemosyne.py --dir "$MEM_DIR" recall "苹果" --k 3

echo "== 4) 查看状态（JSON）=="
python mnemosyne.py --dir "$MEM_DIR" status --json

echo "== 5) 健康检查 =="
python mnemosyne.py --dir "$MEM_DIR" doctor --json

echo "== 6) 账本完整性校验 =="
python mnemosyne.py --dir "$MEM_DIR" verify-integrity --json

echo "== 7) 导出记忆 =="
python mnemosyne.py --dir "$MEM_DIR" export --format json --out "$MEM_DIR/memories.json"

echo "== 清理临时目录 =="
rm -rf "$MEM_DIR"
echo "完成。"
