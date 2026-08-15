# ============================================================================
# Mnemosyne OS — Docker 镜像（The Memory Operating System for AI）
# 纯标准库零依赖记忆引擎 + MCP Server（stdio 模式）
# 基础镜像 python:3.11-slim（~120MB），引擎无需任何 pip 依赖
# ============================================================================
FROM python:3.11-slim

# 构建参数：源码目录（版本升级时只改这里，或用 --build-arg 覆盖）
ARG SRC_DIR="Mnemosyne Memory v5.1.4 20260814"

LABEL org.opencontainers.image.title="Mnemosyne OS" \
      org.opencontainers.image.description="Zero-dependency AI Agent Memory Engine - L1 Lexical Cache / MCP Server" \
      org.opencontainers.image.source="https://github.com/FrankHu-HK/mnemosyne" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="5.1.4"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 非 root 运行（安全基线）
RUN useradd --create-home --uid 1000 mnemosyne \
    && mkdir -p /data/mnemosyne \
    && chown -R mnemosyne:mnemosyne /data

WORKDIR /app

# 只拷贝引擎 + MCP Server（零依赖，无需 pip install，保持镜像最小）
COPY "$SRC_DIR/mnemosyne.py" "$SRC_DIR/mcp_server.py" ./

USER mnemosyne

# 记忆库数据卷（named volume 首次挂载自动继承 /data 所有权）
VOLUME ["/data/mnemosyne"]

# MCP Server 为 stdio 模式：客户端通过 docker run -i / docker exec -i 交互
# 用法示例：
#   docker run -i --rm -v mnemosyne-data:/data/mnemosyne mnemosynoos/mnemosyne
#   echo '{"jsonrpc":"2.0","method":"initialize","id":1}' | docker run -i --rm mnemosynoos/mnemosyne
ENTRYPOINT ["python", "mcp_server.py"]
CMD ["--brain-dir", "/data/mnemosyne"]
