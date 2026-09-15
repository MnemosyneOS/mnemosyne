# Mnemosyne 7.0.2 部署principle：接入 DeepSeek Harness（通过 MCP）

> Deployment Guide: Integrate Mnemosyne 7.0.2 with DeepSeek Harness via MCP

> **让 AI 记住你 · 同时帮你省 Token**
> 从 pip 安装 → 接入 DeepSeek Harness → 验证 → 日常使用 → 升级，一站式全流程教程
>
> 面向完全零基础的新手，每个英文单词都附中文释义，每一条命令都讲清"是什么、为什么"。

---

## 0. 阅读principle（先花 1 分钟读这里）

本教程的目标非常明确：**把 Mnemosyne 7.0.2 装好、接上你的 AI 助手、让它"记得住"你说过的话，并且在每次对话里尽量少花钱（省 Token）**。

### 约定符号

| 符号 | 含义 |
| --- | --- |
| ```` `代码` ```` | 需要你在终端（终端 = 命令行窗口）里输入的命令 |
| **【这是什么】** | 解释这条命令/文件是干嘛的 |
| **【为什么】** | 解释为什么要做这一步，不做会怎样 |
| ⚠️ | 需要注意的坑 |
| 💡 | 小贴士 / 省 Token 技巧 |

### 术语速览（全文每个英文词都会注释，这里是高频的 6 个）

| 英文（中文释义） | 通俗解释 |
| --- | --- |
| **Agent**（智能体） | 你的 AI 助手程序，比如 DeepSeek Harness、Claude、豆包等 |
| **MCP**（模型上下文协议） | 一套"插座标准"，让 AI 助手能插上各种工具（记忆、搜索等） |
| **Memory**（记忆） | AI 记住的你的事，比如你的名字、偏好、项目进度 |
| **Token**（词元） | AI 计费/计算的基本单位，约等于半个到一个汉字，省 Token 就是省钱 |
| **MCP Server**（MCP 服务端） | 提供工具的一方；Mnemosyne 就是提供"记忆工具"的服务端 |
| **MCP Client**（MCP 客户端） | 使用工具的一方；DeepSeek Harness 就是客户端 |

### 设计总原则（全文的"宪法"）

> **能记得住 + 省 Token**
> 1. **记得住**：重要信息（偏好/身份/事实/决策）必须被自动写入并随时能召回
> 2. **省 Token**：规则文件保持精简、记忆按需召回（而不是一股脑全塞进对话）、定期压缩
> 3. **通用性**：所有配置尽量用默认路径/通用写法，换电脑、换目录也能用

---

## ★ 步骤总览（先看这张图，心里有数）

| 步骤 | 做什么 | 关键产出 |
| --- | --- | --- |
| 第 1 步 | 确认你的 AI 助手是否支持 MCP（不支持有替代方案） | 明确用什么接入 |
| 第 2 步 | 安装 Python（编程语言运行环境）和 Node.js（运行 DeepSeek Harness 需要） | 环境就绪 |
| 第 3 步 | 用 pip 安装 Mnemosyne 7.0.2（pip = Python 的安装工具） | 装好记忆核心 |
| 第 4 步 | 初始化记忆库 + 命令行测试写入/召回 | 记忆库可用 |
| 第 5 步 | 安装 DeepSeek Harness（你的 AI 助手主程序） | AI 助手可运行 |
| 第 6 步 | 把 Mnemosyne 注册成 DeepSeek Harness 的 MCP 工具 | 两者接通 |
| 第 7 步 | 重启 AI 助手，验证 13 个记忆工具全部出现 | 工具就绪 |
| 第 8 步 | 会话实测：写入一条记忆 + 换会话召回 | 记忆闭环 |
| 第 9 步 | 注入"记忆规则"（让 AI 自动记，不用每次喊"记住"） | 自动记忆 |
| 第 10 步 | 配置压缩引擎与省 Token 参数 | 长期不爆、省钱 |
| 第 11 步 | 批量预置你的画像（可选，加快 AI 认识你） | 冷启动加速 |
| 第 12 步 | 日常使用技巧 + 定时压缩建议 | 用好它 |
| 第 13 步 | 以后如何升级（含升级后要做的检查） | 长期可用 |
| 第 14 步 | 常见问题排查 | 遇到问题不慌 |

> 💡 **不想看细节？** 直接跟着每个步骤里的"要执行"黑框命令走就行，解释是辅助的。

---

## 1. 前置检查：你的 AI 助手支持 MCP 吗？

### 1.1 什么是 MCP？（小白版）

MCP 全称 **Model Context Protocol（模型上下文协议，可以理解成"AI 工具的通用插座"）**。

- 以前：每个 AI 助手只能用自己的专属工具，换个助手工具就废了
- 现在：只要 AI 助手支持 MCP，就能插上任何符合标准的工具，像 USB 一样即插即用
- Mnemosyne 就是这样一个"记忆工具"（MCP Server），任何支持 MCP 的助手都能接

### 1.2 国内外主流支持 MCP 的 AI 助手（截至 2026-08 快照）

> ⚠️ AI 行业发展极快，以下名单仅作参考，最终以各产品**官方文档**为准。判断方法见 1.4。

#### 国外（国际）主流

| AI 助手 | 开发方 | 支持 MCP？ | 备注（中文释义） |
| --- | --- | --- | --- |
| **Claude Desktop / Claude Code** | Anthropic | ✅ 原生支持 | MCP 协议发明者，支持最完整 |
| **ChatGPT 桌面版** | OpenAI | ✅ 支持 | 通过"连接器 Connectors"功能接入 |
| **Cursor** | Anysphere | ✅ 原生支持 | 流行的 AI 编程软件 |
| **Windsurf** | Codeium | ✅ 原生支持 | AI 编程软件 |
| **VS Code + GitHub Copilot** | Microsoft | ✅ 支持 | 通过 Copilot Chat 扩展 |
| **Zed** | Zed Industries | ✅ 支持 | 代码编辑器 |
| **Cline** | 开源 | ✅ 支持 | VS Code 里的免费 AI 插件 |
| **Roo Code** | 开源 | ✅ 支持 | Cline 的分支 |
| **Continue** | 开源 | ✅ 支持 | IDE 插件 |
| **OpenCode** | 开源 | ✅ 支持 | 命令行 AI |
| **Qoder** | Quora | ✅ 支持 | 编程助手 |
| **Gemini CLI** | Google | 🟡 部分支持 | 通过扩展方式 |
| **Cherry Studio** | 开源 | ✅ 支持 | 桌面聊天客户端 |
| **LibreChat** | 开源 | ✅ 支持 | 自托管聊天平台 |

#### 国内主流

| AI 助手 | 开发方 | 支持 MCP？ | 备注（中文释义） |
| --- | --- | --- | --- |
| **DeepSeek Harness（DSH）** | 深度求索 | ✅ 原生支持 | 本教程主角，免费开源 |
| **豆包 电脑端/客户端** | 字节跳动 | ✅ 支持 | 可在设置里添加 MCP 服务 |
| **扣子 Coze** | 字节跳动 | ✅ 支持 | 有 MCP 插件市场 |
| **Trae / Trae CN** | 字节跳动 | ✅ 支持 | AI 编程软件 |
| **Kimi / Kimi Code CLI** | 月之暗面 | ✅ 支持 | 可在对话里用 /mcp 命令 |
| **通义灵码** | 阿里巴巴 | ✅ 支持 | AI 编程助手 |
| **智谱清言 / GLM Coding** | 智谱 AI | ✅ 支持 | CodeGeeX 也支持 MCP |
| **CodeBuddy** | 腾讯 | ✅ 支持 | AI 编程助手 |
| **文心快码 Comate** | 百度 | ✅ 支持 | AI 编程助手 |
| **MarsCode** | 字节跳动 | ✅ 支持 | 云端 AI 编程 |
| **讯飞星火 / iFlyCode** | 科大讯飞 | 🟡 逐步支持 | 以官方最新为准 |

### 1.3 你的助手不支持 MCP，怎么办？

有三个替代方案，按推荐程度排序：

**方案 A：换用支持 MCP 的助手（最推荐）**
> 本文主角 DeepSeek Harness（DSH）完全免费、开源、官方原生支持 MCP，且安装步骤在下面。Claude Code、Cursor、豆包也都行。换一个最省心。

**方案 B：绕开 MCP，直接用 Mnemosyne 自带的"三张通行证"**
> Mnemosyne 不只提供 MCP 一种接入方式，还提供：
> - **CLI（命令行）**：`mnemosyne retain "内容"` 写入、`mnemosyne recall "关键词"` 召回（本教程第 4 步会教）
> - **Python API（Python 程序接口）**：写程序时直接调用
> - **Hermes 插件**：如果用的是 Hermes Agent，可装 Mnemosyne 官方插件
>
> 这样即使你的助手不支持 MCP，你也能用命令行手动喂记忆、导记忆。

**方案 C：用"中转网关"桥接（技术门槛较高，新手不推荐）**
> 通过第三方 MCP 代理网关把不支持 MCP 的助手桥接过去，配置复杂，容易踩坑，不展开。

### 1.4 怎么快速判断你的助手支持不支持 MCP？（3 个方法）

1. **搜官方文档**：在官网/文档站搜 "MCP" 三个字母
2. **看设置界面**：在助手的设置里找有没有 "MCP" / "外部工具 External Tools" / "连接器 Connectors" 字样
3. **直接问它**：在对话里问"你支持 MCP 协议吗？"，现在的 AI 基本都能答上来

---

## 2. 环境准备：安装 Python 和 Node.js

Mnemosyne 用 **Python** 写的，DeepSeek Harness 用 **Node.js** 跑。两个都要装。

### 2.1 安装 Python（建议 3.11 或 3.12）

1. 打开官网：<https://www.python.org/downloads/>，下载最新的 3.11 或 3.12 版本
2. 安装时**务必勾选**最下方的 `Add python.exe to PATH`（把 Python 加入系统路径，否则命令行找不到）
3. 安装完成后，**新开一个终端窗口**，验证：

```powershell
python --version
# 【这是什么】显示 Python 版本号，比如 Python 3.12.5
# 【为什么】确认 Python 装好了、命令行能找到它。找不到就是没勾 PATH 或没重开窗口
```

### 2.2 安装 Node.js（建议 22 LTS 或更高）

1. 打开官网：<https://nodejs.org/>，下载 **LTS（长期支持版）** 安装包
2. 一路下一步安装
3. 验证（同样**新开窗口**）：

```powershell
node --version
npm --version
# 【这是什么】分别显示 Node.js 和 npm（Node 的安装工具）的版本号
# 【为什么】DeepSeek Harness 需要 Node.js 环境运行；npm 用来安装它
```

> 💡 **省 Token 与版本的关系**：版本越新通常性能越好、同样任务耗 Token 越少，所以建议装最新稳定版。

---

## 3. 安装 Mnemosyne 7.0.2

### 3.1 用 pip 安装（pip 是 Python 的官方安装工具）

```powershell
pip install mnemosyne-os
# 【这是什么】从 Python 官方仓库下载并安装 mnemosyne-os（Mnemosyne 的正式包名）
# 【为什么】这就是 Mnemosyne 7.0.2 本体，装好后你的电脑就有了记忆引擎
```

**如果网速慢/装不动（国内常见）**，改用国内镜像源（镜像源 = 官方仓库的国内加速副本）：

```powershell
pip install mnemosyne-os -i https://pypi.tuna.tsinghua.edu.cn/simple
# 【这是什么】加 -i 参数指定清华镜像源下载
# 【为什么】直连国外仓库慢/失败，镜像源速度快得多
```

> ⚠️ **强烈建议在安装前先关闭代理/VPN**（如果开着）。代理经常导致 pip/npm 下载卡死或失败，这是新手最常见的坑之一。

### 3.2 验证安装成功

```powershell
pip show mnemosyne-os | Select-String "Version|Requires"
# 【这是什么】显示已安装的版本号和依赖列表
# 【为什么】确认装的是 7.0.2；Requires（依赖）为空说明它是零依赖设计，很干净
```

```powershell
mnemosyne --help
# 【这是什么】显示 Mnemosyne 的全部可用命令
# 【为什么】能呼出帮助 = 安装成功且命令行能找到它。看到 retain/recall/consolidate/doctor 等命令就对了
```

---

## 4. 初始化记忆库 + 命令行冒烟测试

### 4.1 初始化

```powershell
mnemosyne init
# 【这是什么】在默认目录 C:\Users\你的用户名\.mnemosyne 创建记忆库（数据库）
# 【为什么】第一次使用必须先建库，否则后续读写会报错
```

### 4.2 冒烟测试（冒烟测试 = 快速验证"能跑通"的最小测试）

```powershell
mnemosyne retain --content "测试记忆：我喜欢喝coffee"
# 【这是什么】把一句话写入记忆库
# 【为什么】验证"写入"通道正常
```

```powershell
mnemosyne recall "coffee"
# 【这是什么】按关键词从记忆库检索相关内容
# 【为什么】验证"召回"通道正常，应能搜回刚才那句
```

```powershell
mnemosyne stats
# 【这是什么】显示记忆库统计（多少条记忆等）
# 【为什么】确认记忆确实存进去了
```

> 💡 **省 Token 技巧①**：命令行测试确认 OK 后，第 8 步的会话测试就不用反复试错，少烧 Token。

---

## 5. 安装 DeepSeek Harness（DSH）

> DeepSeek Harness（简称 DSH，深度求索出的开源 AI 助手主程序）。本教程用它作为 MCP 客户端。

### 5.1 官方推荐安装（npm 方式，最简单）

```powershell
npm install -g @deepseek-ai/dsh
# 【这是什么】全局安装 DSH（-g = 全局，任何目录都能用）
# 【为什么】装完就能用 dsh 命令启动 AI 助手
```

**如果 npm 也慢/失败**，换国内镜像：

```powershell
npm config set registry https://registry.npmmirror.com
npm install -g @deepseek-ai/dsh
# 【这是什么】先把 npm 下载源换成国内镜像，再重新安装
# 【为什么】和 pip 同理，加速下载
```

**验证（新开窗口，很重要）：**

```powershell
dsh --version
# 【这是什么】显示 DSH 版本号
# 【为什么】确认安装成功且命令行能找到。提示"找不到 dsh"= 没重开窗口刷新路径，或安装被代理干扰失败
```

### 5.2 备选：不全局安装，直接用 npx 跑

```powershell
npx @deepseek-ai/dsh web
# 【这是什么】npx 是"临时下载并运行"，不装进系统也能用
# 【为什么】适合不想污染全局环境的人；缺点是每次可能重新下载较慢
```

### 5.3 备选：从源码仓库跑（高级，社区常用）

```powershell
git clone https://github.com/deepseek-ai/deepseek-harness.git
cd deepseek-harness
npm install   # 或 pnpm install（若你装了 pnpm）
npm run dsh web   # 或 pnpm dsh web
# 【这是什么】把 DSH 源码下载到本地再用包管理器启动
# 【为什么】能用上最新代码；适合想跟进新功能的人
```

> ⚠️ **DSH 配置档目录**：DSH 的所有配置都存在 `C:\Users\你的用户名\.dsh\` 这个文件夹里（我们后面改的就是它）。

---

## 6. 把 Mnemosyne 注册成 DSH 的 MCP 工具（核心步骤）

### 6.1 先拿到你电脑上 Python 的完整路径

```powershell
python -c "import sys; print(sys.executable)"
# 【这是什么】打印 Python 的可执行文件完整路径，例如 C:\Users\你的用户名\AppData\Local\Programs\Python\Python311\python.exe
# 【为什么】MCP 配置里要写死这个路径，DSH 才能用它启动 Mnemosyne 服务。先复制下来备用
```

### 6.2 编辑 DSH 的"补丁配置"文件

DSH 用一套**"补丁（patch）机制"**：你可以往它默认配置里"插一段自己的配置"，而不破坏默认配置。我们要插的，就是把 Mnemosyne 注册为 MCP 工具。

**第一步：找到配置文件**

```
C:\Users\你的用户名\.dsh\profiles\web\cordis.patch.yml
```

> - `.dsh` = DSH 的配置目录
> - `profiles\web` = "web 网页版"这套配置档（profile）
> - `cordis.patch.yml` = 这个配置档的"补丁文件"，不存在就新建

**第二步：用记事本（或其他文本编辑器）打开它，写入以下内容**

```yaml
# 文件：C:\Users\你的用户名\.dsh\profiles\web\cordis.patch.yml
# 作用：把 Mnemosyne 注册为 DSH 的一个 MCP 工具
- insert:
  - id: mcp-mnemosyne
    name: '@deepseek-ai/dsh-mcp-client'
    config:
      serverName: mnemosyne
      transport: stdio
      command: C:/Users/你的用户名/AppData/Local/Programs/Python/Python311/python.exe
      args:
        - '-m'
        - mnemosyne.webui.mcp_server
      failOnStartupError: true
```

**逐行解读（每个英文都解释）：**

| 配置项 | 中文释义 | 填什么 |
| --- | --- | --- |
| `- insert:` | 插入 | 告诉 DSH "我要往配置里插入一段" |
| `id: mcp-mnemosyne` | 标识 | 这条工具注册的唯一编号，随意但别和别的重名 |
| `name: '@deepseek-ai/dsh-mcp-client'` | 客户端插件名 | DSH 自带的 MCP 客户端插件，**不需要额外安装** |
| `serverName: mnemosyne` | 服务端名 | 工具名前缀的来源，最终工具长这样 `mcp__mnemosyne__retain` |
| `transport: stdio` | 传输方式 | 用"标准输入输出"通信（本地程序最常用的方式），不用开端口 |
| `command: ...` | 启动命令 | 换成 6.1 步拿到的 Python 路径 |
| `args:` | 参数列表 | 用 Python 运行 `mnemosyne.webui.mcp_server`（Mnemosyne 的 MCP 服务模块） |
| `failOnStartupError: true` | 启动失败就报错 | 如果 Mnemosyne 启动失败，DSH 直接提示你，方便排查（可选，不想被打断可删掉） |

> ⚠️ **三个易错点**：
> 1. `command` 里路径的斜杠用 `/`（正斜杠），Windows 默认的 `\`（反斜杠）在某些场景会出问题
> 2. 路径中的用户名（如 `<your-user>`）要替换成你自己的
> 3. 如果之前该文件已有内容（比如别的工具），**不要覆盖整份文件**，把上面这段**追加/合并**进已有的 `- insert:` 列表里

### 6.3 检查配置是否被 DSH 正确识别（不启动也能查）

```powershell
dsh --profile web --dump-config | Select-String -Pattern "mcp-mnemosyne" -Context 0,8
# 【这是什么】把 DSH 最终合并好的完整配置打印出来，筛出 mcp-mnemosyne 那一段
# 【为什么】确认配置"拼装"成功。看到 serverName: mnemosyne、command、args 都正确，就可以放心重启
```

> 💡 **省 Token 技巧②**：这一步在重启前做，能避免"重启后发现没生效"再折腾一轮，省时省钱。

---

## 7. 重启 DSH + 验证 13 个记忆工具

### 7.1 重启 DSH（让新配置生效）

MCP 工具是**启动时**加载的，所以必须重启 DSH 才能让 Mnemosyne 生效。

**第一步：找到并停掉正在运行的 DSH 进程**

```powershell
netstat -ano | findstr :3080
# 【这是什么】查 3080 端口（DSH 网页版默认端口）被哪个进程占用，记下最右边一列的 PID（进程编号）
# 【为什么】3080 是 DSH 网页版的端口；重启前要先停掉旧进程
```

```powershell
Stop-Process -Id <上一步查到的PID> -Force
# 【这是什么】强制结束该进程
# 【为什么】释放端口，才能启动一个加载了新配置的新 DSH
```

**第二步：确认端口已释放**

```powershell
netstat -ano | findstr :3080
# 【这是什么】再次查看 3080
# 【为什么】没有 LISTENING（监听中）那行 = 端口已释放，可以重启
```

**第三步：重新启动 DSH**

```powershell
dsh web
# 【这是什么】以"网页版"模式启动 DSH，默认监听 127.0.0.1:3080（127.0.0.1 = 本机地址）
# 【为什么】启动后就能在浏览器里用了
```

> 💡 如果想启动时不自动打开浏览器，加 `--no-open` 参数：`dsh web --no-open`

### 7.2 打开网页并验证 17 个工具

```powershell
Start-Process "http://127.0.0.1:3080"
# 【这是什么】在默认浏览器打开 DSH 网页版界面
# 【为什么】DSH 是网页版，通过浏览器操作
```

进入界面后，**先选择工作区（Workspace，工作目录）**——DSH 的安全设计要求必须先选定一个文件夹作为工作目录，否则无法输入对话。

然后在**工具列表**（一般在设置面板或新建会话的可用工具里）搜索 `mnemosyne`，应能看到这 17 个工具：

| 工具名 | 中文释义 / 作用 |
| --- | --- |
| `mcp__mnemosyne__retain` | 写入一条记忆 |
| `mcp__mnemosyne__recall` | 检索记忆 |
| `mcp__mnemosyne__retain_batch` | 批量写入多条记忆 |
| `mcp__mnemosyne__stats` | 查看记忆统计 |
| `mcp__mnemosyne__graph_query` | 知识图谱查询（看记忆之间的关系） |
| `mcp__mnemosyne__temporal_query` | 按时间维度查询 |
| `mcp__mnemosyne__list_projects` | 列出记忆项目 |
| `mcp__mnemosyne__doctor` | 记忆库健康检查 |
| `mcp__mnemosyne__audit` | 审计（查记忆的写入/修改记录） |
| `mcp__mnemosyne__confidence_history` | 置信度历史（记忆可信度的变化） |
| `mcp__mnemosyne__memory/export-v1` | 导出记忆 |
| `mcp__mnemosyne__memory/import-v1` | 导入记忆 |
| `mcp__mnemosyne__memory/claim` | 认领记忆 |

> ⚠️ 工具名里的斜杠（`memory/export-v1`）是 MCP 规范允许的，不用管它。
> 💡 工具名可能随版本微调，但 `retain`（写）和 `recall`（查）这两个核心工具一定在。

### 7.3 如果工具没出现，按顺序排查（见第 14 步详情）

1. 重启了吗？（新配置要重启才生效）
2. Python 路径对吗？（6.1 步）
3. `failOnStartupError` 有没有提示报错？
4. 用 6.3 步的 `--dump-config` 看配置有没有拼装进去

---

## 8. 会话实测：验证"记得住"闭环

这是最关键的一步：证明"写进去 → 换会话还能查回来"。

### 8.1 测试写入

在 DSH 的**会话 A** 里输入：

```
请记住：我的验证饮料是blue-739
```

观察 AI 是否调用了 `mcp__mnemosyne__retain` 并返回成功。

### 8.2 测试召回

**新建**一个会话 B（模拟"隔天再来"），输入：

```
我的验证饮料是什么？请查询记忆
```

观察 AI 是否调用 `mcp__mnemosyne__recall` 并正确回答"blue-739"。

### 8.3 通过命令行再交叉验证一次（可选，最严谨）

```powershell
mnemosyne recall "blue"
# 【这是什么】用命令行直接检索记忆库
# 【为什么】如果命令行也能搜到，说明数据真实存在，AI 没在"编"
```

> ✅ **闭环达成** = 写入成功 + 换会话能召回 + 命令行能查到。这说明 Mnemosyne 已经真正接入了。

---

## 9. 注入"记忆规则"：让 AI 自动记，不用每次喊"记住"

现在 AI 只有在你说"记住"时才会存。下一步是给它写一条**长期规则**，让它**主动判断**什么时候该记、什么时候该查——这才是"无感记忆"的关键，也是省 Token 的核心（按需召回，而不是每次全量塞进上下文）。

### 9.1 什么是 AGENTS.md？

DSH 支持一种叫 **AGENTS.md** 的"工作区the design requirement文件"：它会在**每个会话开始时自动注入**给 AI，相当于给 AI 的"长期员工手册"。我们把自己的记忆策略写进去，AI 每轮都会看到并遵守。

### 9.2 创建全局规则文件

在 PowerShell 里执行（一次性操作）：

```powershell
$content = @"
# 记忆策略（Mnemosyne）
- 当用户透露个人偏好、身份信息、重要事实、决策或进行中任务时，主动调用 mcp__mnemosyne__retain 写入记忆，不要等用户说"记住"。
- 当回答可能依赖历史背景时，先调用 mcp__mnemosyne__recall 检索相关记忆，并用 --budget-tokens 控制召回量以节省 Token。
- 每轮开始时，若话题可能涉及之前讨论过的内容，先检索记忆获取上下文。
- 当用户纠正或否定之前的说法时，先 recall 找到旧记忆，再 retain 写入更正后的内容。
- 只记有长期价值的信息（偏好/身份/事实/决策/项目状态），不要机械记录每一句话。
"@
Set-Content -Path "$env:USERPROFILE\.dsh\AGENTS.md" -Value $content -Encoding UTF8
Get-Content "$env:USERPROFILE\.dsh\AGENTS.md"
# 【这是什么】创建 C:\Users\你的用户名\.dsh\AGENTS.md 并写入记忆策略，最后打印确认
# 【为什么】这个文件里的一条条规则，AI 每个会话都会看到并遵守
```

**逐条解读这份规则（都是"省 Token + 记得住"的设计）：**

| 规则 | 中文释义 | 对"记得住"的作用 | 对"省 Token"的作用 |
| --- | --- | --- | --- |
| 主动 retain | 主动写入 | 不用你喊"记住"也能存 | 避免你反复强调 |
| 先 recall | 先查再答 | 不会忘事 | 避免 AI 瞎猜来回试 |
| 用 --budget-tokens 控制召回量 | 限制召回条数 | 取最相关的 | **只带最相关的记忆，不把整库都塞进来** |
| 只记长期价值信息 | 过滤噪音 | 记忆库干净 | 避免存一堆没用的、每轮都占 Token |
| 纠正时先查再改 | 更正记忆 | 记忆不撒谎 | 避免重复错误 |

### 9.3 验证规则生效

**新建一个会话**（旧会话不会重新加载规则），直接说：

```
我喜欢喝coffee，家在杭州，做 AI 开源项目
```

**不说"记住"**，看 AI 是否自动调用 `mcp__mnemosyne__retain`。然后新建会话问"我住在哪？"，看是否自动 `recall` 并答对。两条都过，你的"无感记忆"就完成了。

> 💡 **省 Token 技巧③**：规则文件务必精简（上面 5 条已够）。因为它是**每一轮对话都会注入**的，写多了每轮都浪费 Token。

---

## 10. 压缩引擎与省 Token 配置（重点）

> Mnemosyne 7.0.2 自带一套**压缩（Compression）能力**，用于：把旧记忆合并成更精炼的总结（省 Token）、去重（省空间）、提炼洞察（记得住）。**这些能力内置在 `mnemosyne` 命令里，不需要额外安装任何东西。**

### 10.1 三个压缩引擎命令

| 命令 | 中文释义 | 干什么用 |
| --- | --- | --- |
| `mnemosyne consolidate` | 记忆整合压缩 | 把零散/重复的旧记忆压缩成精炼总结，**最省 Token 的一招** |
| `mnemosyne reflect` | 反思提炼 | 从记忆里提炼长期洞察（比如"用户越来越偏好 X"） |
| `mnemosyne dedup` | 去重 | 删掉重复记忆，库更小更准 |

先看每个命令有哪些参数（不同版本参数可能微调，**以你本机 `--help` 输出为准**）：

```powershell
mnemosyne consolidate --help
mnemosyne reflect --help
mnemosyne dedup --help
# 【这是什么】分别打印三个压缩命令的可用参数说明
# 【为什么】确认你本机 7.0.2 的完整参数（下方给的是通用参数，帮你理解含义）
```

### 10.2 通用参数说明（结合 7.0.2 实际能力）

| 参数 | 中文释义 | 作用 / 建议 |
| --- | --- | --- |
| `--layer LAYER` | 记忆层级 | 指定只处理某层（如热层 L1 / 冷层 L2），通用做法是默认全层 |
| `--budget-tokens N` | Token 预算 | 限制一次召回最多带多少 Token，**省 Token 核心参数** |
| `--k N` | 返回条数 | 一次召回返回几条，越小越省 Token |
| `--tag TAG` | 标签 | 只处理带某标签的记忆，精确又省 |
| `--from / --to` | 时间范围 | 只处理某段时间的记忆 |
| `--multi-hop` | 多跳 | 跨多轮关联查询，用得少就关掉更省 |
| `--json` | JSON 输出 | 输出为程序可读格式（普通用户不用管） |

> 💡 **通用默认建议（配合"记得住 + 省 Token"）**：
> - 召回统一带 `--budget-tokens 400` 左右（够带回 1-2 条关键记忆，又不会爆上下文）
> - `recall` 的 `--k` 默认 5 够用，不用调大
> - 每周跑一次 `consolidate`，每月跑一次 `dedup`

### 10.3 让"压缩"自动发生：写进记忆规则（推荐）

在 `~/.dsh\AGENTS.md` 里再加一条，让 AI 平时就控制召回量：

```text
- 调用 mcp__mnemosyne__recall 时，优先用少量参数（如 top_k 默认值），只取最相关的记忆，避免一次带回过多内容。
```

> 这样"省 Token"就从"手动"变成"AI 自动遵守"了。

### 10.4 定期压缩：一键脚本（通用做法）

把下面的命令存成一个文件（比如 `压缩记忆.bat`），以后双击就能压缩，不用记命令：

```batch
@echo off
echo 开始压缩 Mnemosyne 记忆库...
mnemosyne consolidate
mnemosyne dedup
mnemosyne stats
pause
```

- `mnemosyne consolidate`（记忆整合压缩）：压缩旧记忆
- `mnemosyne dedup`（去重）：清理重复
- `mnemosyne stats`（统计）：看看压缩后剩多少条

### 10.5 省 Token 全景清单（所有配置的综合汇总）

| 手段 | 省在哪 | 配置位置 |
| --- | --- | --- |
| 规则文件保持精简（5 条） | 每轮注入的the design requirement少 | `~/.dsh\AGENTS.md` |
| 按需召回（不整库塞入） | 上下文只带相关记忆 | `AGENTS.md` 规则 + recall 参数 |
| recall 限量（budget/k） | 一次少带几条 | recall 参数 |
| 定期 consolidate/dedup | 库不膨胀、每轮检索快 | 压缩脚本 |
| 画像用 CLI 预置 | 不用在对话里反复喂 | 第 11 步 |
| 不把画像写进 AGENTS.md | 避免每轮都注入大段画像 | 遵循"规则进文件、数据进记忆库" |

---

## 11. 批量预置你的画像（可选，加快 AI 认识你）

如果你有一批"想让它一开始就知道"的信息（名字、职业、住址、偏好、项目背景），不用一条条在对话里说——用命令行一次性写进去，**省 Token 又省时间**：

```powershell
mnemosyne retain --content "用户画像：我叫XX，在杭州，独立开发AI开源项目Mnemosyne，主力显卡RTX 4070"
mnemosyne retain --content "用户偏好：喜欢喝茶（coffee/blue），偏好简洁直接的回复风格"
mnemosyne retain --content "项目背景：Mnemosyne定位为L1记忆缓存，纯Python零依赖无GPU"
# 【这是什么】把三条画像信息批量写入记忆库
# 【为什么】预置后 AI 需要时直接召回，不用你重新介绍
```

> ⚠️ **重要区分**：画像这类**会变的数据**放记忆库（Mnemosyne），**不变的规则**才放 `AGENTS.md`。数据放文件里会过期、还每轮占 Token，所以别混。

---

## 12. 日常使用技巧（怎么把它用顺）

### 12.1 一句话总结用法

- **想让它记住的事**：正常说就行，它按规则自动 `retain`
- **想确认它记得**：直接问，它自动 `recall`
- **个别重要信息**（临时密码、一次性约定）：主动说"记住 XXX"，提高它的重视程度
- **发现它记错了**：纠正它，它会按规则"先查旧记忆再写入新记忆"

### 12.2 想看记忆库现状

```powershell
mnemosyne stats        # 统计（memory 统计）
mnemosyne doctor       # 健康检查（doctor = 医生，查库有没有问题）
mnemosyne audit        # 审计（看最近写入了什么）
```

### 12.3 记忆分层使用（进阶）

Mnemosyne 支持**层级（layer）记忆**：热层（L1，常用、快）和冷层（更久远）。召回时指定层，能进一步省 Token。具体层名以 `mnemosyne recall --help` 输出为准。

---

## 13. 以后如何升级（升级流程 + 升级后必做检查）

> 软件会更新，Mnemosyne 和 DSH 都要会升级。**升级本身简单，关键是升级后要做几项检查，确保还能正常用。**

### 13.1 升级前的准备（重要）

```powershell
# 备份记忆库（复制一份，改个名字放旁边）
Copy-Item "$env:USERPROFILE\.mnemosyne" "$env:USERPROFILE\.mnemosyne_backup" -Recurse
# 【这是什么】把记忆库整个复制一份为备份
# 【为什么】升级万一出问题，记忆不会丢。备份是"记得住"的第一保险
```

> 💡 每次大版本升级前都建议备份，成本极低，收益是"记忆永不丢"。

### 13.2 升级 Mnemosyne

```powershell
pip install -U mnemosyne-os
# 【这是什么】-U = --upgrade，把 mnemosyne-os 升级到最新版
# 【为什么】获取新功能、性能优化和 Bug 修复
```

```powershell
mnemosyne --version   # 或 pip show mnemosyne-os | Select-String Version
# 【这是什么】确认新版本号
# 【为什么】确认真的升上去了
```

### 13.3 升级 DeepSeek Harness

```powershell
npm update -g @deepseek-ai/dsh
# 【这是什么】把全局的 DSH 更新到最新版
# 【为什么】获取 DSH 的新功能；如果当初是源码方式安装，去仓库目录 git pull 再重新 install
```

### 13.4 升级后必做的 7 项检查（确保还能正常用）

| 序号 | 检查项 | 命令/动作 | 为什么 |
| --- | --- | --- | --- |
| 1 | 记忆库健康检查 | `mnemosyne doctor` | 确认升级没损坏数据 |
| 2 | 命令行可用 | `mnemosyne retain --content "升级测试"` + `recall` | 确认核心功能没退化 |
| 3 | 看升级说明 | 打开 PyPI/GitHub 的 Release Notes（发布说明） | 确认有没有"破坏性变更" |
| 4 | 处理数据迁移 | 若说明里要求：`mnemosyne migrate` | 数据格式升级需要迁移 |
| 5 | 重启 DSH | 停旧进程 + `dsh web` | 让新的 MCP 服务生效 |
| 6 | 重新验证 17 个工具 | 网页工具列表搜 `mnemosyne` | 确认 MCP 接入没断 |
| 7 | 会话冒烟测试 | 写一条 + 新会话召回一条 | 确认"记得住"闭环仍成立 |

**其中第 3、4 项检查展开说明：**

```powershell
mnemosyne migrate
# 【这是什么】如果升级说明里提到"数据格式变了，需要迁移"，就运行它把旧数据转成新格式
# 【为什么】有些大版本会改存储格式，不迁移可能读不到旧记忆
```

**如果 7.0.2 → 未来版本后 MCP 配置里的模块名变了**（比如 `mnemosyne.webui.mcp_server` 改成别的），记得同步更新第 6.2 步的 `cordis.patch.yml` 里的 `args`，再重启 DSH。

> 💡 **省 Token 与升级**：升级通常意味着更快的检索和更省 Token 的实现，所以"勤升级"本身就是在省钱。但升级后一定要跑第 13.4 步的 7 项检查，别升完发现记忆工具丢了还不知道。

---

## 14. 常见问题排查（FAQ）

| 现象 | 原因 | 解决办法 |
| --- | --- | --- |
| pip 装 mnemosyne-os 很慢/失败 | 直连国外源慢，或开着代理 | 关代理 + 用清华镜像（第 3.1 步） |
| 提示 `mnemosyne` 不是命令 | Python 没加 PATH / 没重开窗口 | 重开终端；确认装了 Python 且勾了 PATH |
| `dsh --version` 找不到 dsh | 全局安装失败 / PATH 没刷新 | 关代理重装；**新开**终端窗口 |
| DSH 网页打不开 | 没启动 dsh web / 端口被占用 | 运行 `dsh web`；`netstat` 查 3080 |
| 工具列表里没有 mcp__mnemosyne__* | 没重启 DSH / 配置没拼上 / Python 路径错 | 按第 7.1 步重启；`--dump-config` 查配置；核对路径 |
| 有工具但调用报错 | Mnemosyne MCP 服务启动失败 | 看 `failOnStartupError` 报错；先跑 `mnemosyne doctor` |
| 命令行能查到，AI 却查不到 | 数据目录不一致（CLI 和 MCP 用的库不同） | 用环境变量 `MNEMOSYNE_DATA_DIR` 统一目录，两边都设同一个路径 |
| 换会话后 AI 忘了 | 没注入 AGENTS.md 规则 / 没新开会话 | 确认第 9 步文件存在；新开会话测试 |
| 召回结果太多太占 Token | 没限量 | 按第 10 步用 `--budget-tokens` / `--k` 限制 |

---

## 附录 A：术语表（中英对照）

| 英文 | 中文释义 | 一句话解释 |
| --- | --- | --- |
| Agent | 智能体 | 你的 AI 助手程序 |
| MCP | 模型上下文协议 | AI 工具的通用插座标准 |
| MCP Server | MCP 服务端 | 提供工具的一方（Mnemosyne） |
| MCP Client | MCP 客户端 | 使用工具的一方（DSH） |
| Memory | 记忆 | AI 记住的事 |
| Token | 词元 | AI 计费的基本单位，省 Token = 省钱 |
| pip | Python 安装工具 | 用 pip 装 Python 软件包 |
| npm / npx | Node 安装/运行工具 | 用 npm 装 Node 软件包 |
| CLI | 命令行界面 | 在终端里敲命令操作 |
| Profile | 配置档 | 一套配置方案，DSH 的 web 配置档 |
| Patch | 补丁 | 往默认配置里插入自己的片段 |
| PID | 进程编号 | 每个运行中程序的唯一编号 |
| Workspace | 工作区 | DSH 里选定的工作目录 |
| stdio | 标准输入输出 | 本地程序间最常用的通信方式 |
| consolidate | 记忆整合压缩 | 把旧记忆压缩成精炼总结 |
| dedup | 去重 | 删除重复记忆 |
| doctor | 健康检查 | 检查记忆库有没有问题 |
| migrate | 数据迁移 | 把旧数据转成新格式 |
| Release Notes | 发布说明 | 每次更新的改动说明 |
| Environment Variable | 环境变量 | 系统的全局配置项 |

## 附录 B：把 Mnemosyne 接到其它支持 MCP 的 AI 助手（通用示例）

> Mnemosyne 的 MCP 服务入口是 `python -m mnemosyne.webui.mcp_server`，任何支持 MCP 的助手都可以这样注册。下面是三个最常见的示例（把 `<你的用户名>` 和 Python 路径换成你自己的）。

### B.1 Claude Code

```bash
claude mcp add mnemosyne -- python -m mnemosyne.webui.mcp_server
# 【这是什么】把 Mnemosyne 注册为 Claude Code 的 MCP 工具
# 【为什么】Claude Code 原生支持 MCP，一条命令即插即用
```

### B.2 Cursor

在项目的 `.cursor/mcp.json` 里写入：

```json
{
  "mcpServers": {
    "mnemosyne": {
      "command": "python",
      "args": ["-m", "mnemosyne.webui.mcp_server"]
    }
  }
}
```

### B.3 OpenAI Codex CLI

在 `.codex/mcp.json` 里写入：

```json
{
  "mcpServers": {
    "mnemosyne": {
      "command": "python",
      "args": ["-m", "mnemosyne.webui.mcp_server"]
    }
  }
}
```

> ⚠️ 以上是通用写法，不同工具对 MCP 配置的字段名略有差异（比如有的用 `mcpServers`、DSH 用 `serverName`），以各工具官方文档为准。

---

## 结束语

到这一步，你已经完成了：

1. ✅ 确认 AI 助手支持 MCP（或不支持的替代方案）
2. ✅ 安装 Python + Node.js + Mnemosyne 7.0.2 + DeepSeek Harness
3. ✅ Mnemosyne 注册为 DSH 的 MCP 工具，13 个记忆工具上线
4. ✅ 会话实测"记得住"闭环通过
5. ✅ 注入记忆规则，AI 自动记忆、按需召回（省 Token）
6. ✅ 配置压缩引擎，定期 consolidate/dedup
7. ✅ 学会升级流程与升级后检查

**现在，你的 AI 助手终于"记得住你、还帮你省钱"了。** 放手正常聊天吧——重要的事它会自己记住，需要时它会自己想起来，还不会一股脑把旧账全翻出来烧你的 Token。

祝用得开心！
