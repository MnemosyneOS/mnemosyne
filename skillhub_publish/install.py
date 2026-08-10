#!/usr/bin/env python3
"""Mnemosyne Memory v4.0.0 — One-line installer."""
import urllib.request, os, sys
GITHUB_URL = "https://raw.githubusercontent.com/FrankHu-HK/mnemosyne/main/mnemosyne-memory-4.0.0/scripts/mnemosyne.py"
# 如果本地已有机率文件，直接用；否则从 GitHub 下载
if os.path.exists("mnemosyne.py"):
    print("✓ mnemosyne.py 已在当前目录")
else:
    print(f"下载 mnemosyne.py ...")
    urllib.request.urlretrieve(GITHUB_URL, "mnemosyne.py")
    print("✓ 下载完成")
print("\n安装完成！使用方法：")
print("  from mnemosyne import MemoryBrain")
print("  brain = MemoryBrain('我的记忆库')")
print("  brain.ensure_init()")
print("  brain.retain('内容...')")
print("  results = brain.recall('查询...')")
