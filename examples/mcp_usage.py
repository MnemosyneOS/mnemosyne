#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 使用示例：以编程方式调用 Mnemosyne 的 MCP 服务器。

本示例直接调用 `mcp_server.handle_request`，演示 MCP 协议的三个核心步骤：
  1. initialize —— 初始化（含可选 tenant 命名空间）
  2. tools/list —— 列出可用工具
  3. tools/call —— 调用 retain / recall 工具

说明：
  - 核心零依赖，MCP 服务器为纯标准库 JSON-RPC over stdio。
  - 配置 MNEMOSYNE_MCP_TOKEN 后，请求需携带 _meta.authToken，否则返回 -32001。

运行：python examples/mcp_usage.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mcp_server


def call(req):
    """把 JSON-RPC 请求发给 mcp_server 并打印响应。"""
    resp = mcp_server.handle_request(req)
    print(json.dumps(resp, ensure_ascii=False, indent=2))
    return resp


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="mnemosyne_mcp_")
    # 把 MCP 脑目录指向临时目录，避免污染用户主目录；重置脑缓存
    mcp_server._brain_dir = tmp
    if hasattr(mcp_server._ensure_brain, "_brains"):
        mcp_server._ensure_brain._brains = {}

    print("=== 1) initialize ===")
    call({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"clientInfo": {"name": "demo-client"}}})

    print("\n=== 2) tools/list ===")
    resp = call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tool_names = [t["name"] for t in resp.get("result", {}).get("tools", [])]
    print("可用工具：", ", ".join(tool_names))

    print("\n=== 3) tools/call retain ===")
    call({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
          "params": {"name": "retain",
                     "arguments": {"content": "苹果公司成立于1976年"}}})

    print("\n=== 4) tools/call recall ===")
    call({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
          "params": {"name": "recall",
                     "arguments": {"query": "苹果", "k": 3}}})

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
