<p align="center">
  <img src="assets/banner.png" alt="Mnemosyne OS" width="100%">
</p>

# Mnemosyne OS ☤

<p align="center">
  <a href="https://pypi.org/project/ai-memo-os/">Mnemosyne OS</a> | <a href="https://github.com/MnemosyneOS/mnemosyne">GitHub</a> | <a href="README.md">English</a>
</p>

<p align="center">
  <a href="https://pypi.org/project/ai-memo-os/"><img src="https://img.shields.io/badge/PyPI-ai--memo--os-blue?style=for-the-badge" alt="PyPI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="授權: MIT"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.8%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.8+"></a>
  <a href="https://modelcontextprotocol.io/"><img src="https://img.shields.io/badge/MCP-52%20Tools-00ADD8?style=for-the-badge" alt="模型上下文協定"></a>
  <a href="https://pepy.tech/projects/ai-memo-os"><img src="https://img.shields.io/pepy/dt/ai-memo-os?style=for-the-badge" alt="Downloads"></a>
  <a href="https://x.com/mnemosyne_oos"><img src="https://img.shields.io/badge/X-@mnemosyne_oos-black?style=for-the-badge&logo=x&logoColor=white" alt="X"></a>
  <a href="https://github.com/MnemosyneOS/mnemosyne/blob/main/README.md"><img src="https://img.shields.io/badge/Lang-English-blue?style=for-the-badge" alt="English"></a>
  <a href="https://github.com/MnemosyneOS/mnemosyne/blob/main/README_TW.md"><img src="https://img.shields.io/badge/Lang-繁體中文-red?style=for-the-badge" alt="繁體中文"></a>
  <a href="https://github.com/MnemosyneOS/mnemosyne/blob/main/README.es.md"><img src="https://img.shields.io/badge/Lang-Español-orange?style=for-the-badge" alt="Español"></a>
  <a href="https://github.com/MnemosyneOS/mnemosyne/blob/main/README.ru.md"><img src="https://img.shields.io/badge/Lang-Русский-blue?style=for-the-badge" alt="Русский"></a>
  <a href="https://github.com/MnemosyneOS/mnemosyne/blob/main/README.de.md"><img src="https://img.shields.io/badge/Lang-Deutsch-lightgrey?style=for-the-badge" alt="Deutsch"></a>
  <a href="https://github.com/MnemosyneOS/mnemosyne/blob/main/README.th.md"><img src="https://img.shields.io/badge/Lang-ไทย-blue?style=for-the-badge" alt="ไทย"></a>
  <a href="https://github.com/MnemosyneOS/mnemosyne/blob/main/README.ko.md"><img src="https://img.shields.io/badge/Lang-한국어-green?style=for-the-badge" alt="한국어"></a>
  <a href="https://github.com/MnemosyneOS/mnemosyne/blob/main/README.ja.md"><img src="https://img.shields.io/badge/Lang-日本語-red?style=for-the-badge" alt="日本語"></a>
</p>

**Mnemosyne OS 8.1.0** —— 零依賴 (zero-dependency)、本地優先 (local-first) 的 AI 記憶系統。自帶圖記憶 (graph memory)、多模態攝入 (multimodal ingestion)、重排 (rerank)、時序推理 (temporal reasoning)、哈希鏈審計賬本 (hash-chain audit ledger)、無損壓縮 (lossless compression)，以及 55 個 MCP 工具 (41 原生 + 11 用戶端兼容)。

8.1.0 補上了兩層。**記憶形成層 (memory formation layer)**：會話摘要 (session summary)、衝突消解 (conflict resolution)、任務與經驗 (tasks & playbooks)、代碼演進 (code evolution)、敏感度治理 (sensitivity policy)——讓系統不只「找得出記憶」，還能「把經歷變成記憶」。**記憶運行層 (memory runtime layer)**：畫像卡 (profile card)、上下文注入 (context injection)、漸進披露 (progressive disclosure)、鞏固 (consolidation)、度量 (metrics)、遮罩 (mask)、級聯刪除 (cascade delete)——為模型組裝合適的上下文，並管理記憶的生命週期。

> 唯一一個**核心真的零三方依賴**的 AI 記憶引擎 —— 不需要向量資料庫 (vector database)、不需要 LLM 執行時 (LLM runtime)、不需要雲端帳號 (no cloud lock-in)。`install_requires` 是空列表。筆記型電腦、伺服器或無伺服器架構 (serverless) 都能跑。

可以作為 **Python 函式庫 (library)**、**CLI**、**HTTP API**、**MCP 伺服器 (MCP server)** 使用。

---

## 🚀 快速開始

### 安裝

```bash
pip install ai-memo-os          # 核心：零三方依賴
```

### 零配置就能記住和想起

```python
from mnemosyne import Memory

m = Memory()                       # 內建嵌入器 + 規則式抽取器
m.add("I prefer dark mode and use vim keybindings. My name is Alice.",
      user_id="alice")

for hit in m.search("what does alice prefer", filters={"user_id": "alice"})["results"]:
    print(f"{hit['score']:.3f}  {hit['memory']}")
```

離線、無 API 金鑰 (API Key)、不下載模型、不裝資料庫就能跑。也正因為如此，才有下一節。

### 需要更強召回時，再接真實模型

```python
from mnemosyne import Memory

m = Memory.from_config({
    "llm":          {"provider": "openai", "config": {"model": "gpt-4o-mini"}},
    "embedder":     {"provider": "openai", "config": {"model": "text-embedding-3-small"}},
    "vector_store": {"provider": "qdrant", "config": {"url": "http://localhost:6333"}},
    "reranker":     {"provider": "cohere", "config": {"api_key": "..."}},
    "graph_store":  {"provider": "builtin"},
})
```

每個元件都獨立可選。某個供應商 (provider) 建不起來時，會退到內建等價實作，並且**如實上報**，絕不靜默降級 (never silently degrade)：

```python
m.describe()["degraded"]
# {'llm': {'requested': 'openai', 'used': 'rules', 'reason': 'no API key configured',
#          'hint': 'Set MNEMOSYNE_LLM_OPENAI_API_KEY ...'}}
```

### 或者用 CLI

```bash
mnemosyne init
mnemosyne add "I prefer dark mode and vim keybindings" --user-id alice
mnemosyne search "what does alice prefer" --user-id alice
mnemosyne list  --user-id alice
mnemosyne event --limit 10
mnemosyne --agent search "preferences" --user-id alice   # 供工具迴圈消費的 JSON 信封
```

### 或者用 MCP 伺服器

```json
{
  "mcpServers": {
    "mnemosyne": {
      "command": "python",
      "args": ["-m", "mnemosyne.webui.mcp_server",
               "--brain-dir", "./mem", "--namespace", "default"],
      "env": { "MNEMOSYNE_MCP_TOKEN": "<隨機 32+ 位>" }
    }
  }
}
```

### 或者用 HTTP API

```bash
mnemosyne-web --port 9090          # 控制台 + REST 同一個埠
curl -X POST http://127.0.0.1:8788/v3/memories/add/ \
  -H "Authorization: Bearer $MNEMO..._KEY" -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"我 2023 年搬到了柏林。"}],"user_id":"alice"}'
```

---

## ☁️ Mnemosyne OS SaaS

同一個引擎，託管版，附 Web 控制台：**[ai-memory.net](https://ai-memory.net)**。
託管控制台的產品名就叫 **Mnemosyne OS SaaS** —— 註冊、建立 API 金鑰、瀏覽記憶、管理訂閱都在那裡。

| 方案 | 價格（USD） | 價格（CNY） | 內容 |
|---|---|---|---|
| **Free** | $0 | ¥0 | 52 個 MCP 工具全開 · 冷層記憶 30 天召回視窗 · 1 個命名空間 (namespace) · 審計視窗 7 天 · 資料永不刪除 |
| **Pro** | $6.9/月 · $69/年 | ¥9.9/月 · ¥99/年 | 永久冷層記憶 · 跨命名空間召回 · 審計歷史無限制 · 全量匯出 |
| **Team** | $19.9/席位/月 · $199/席位/年 | ¥29.9/席位/月 · ¥299/席位/年 | Pro 全部能力 · 多使用者命名空間 · 團隊記憶策略 · 審計報告匯出（按席位計費） |
| **合規證明包 (Compliance Add-on)** | $9.9/月 | ¥19.9/月 | Pro / Team 之上的加購項（不是獨立層級，不提供年付）：`verify_integrity` 定期報告、刪除證明匯出、資料駐留配置 |

年付 = 付 10 個月用 12 個月（約 16.7% off，Pro 折合 ¥8.25/月）。
每個新帳號先送 **30 天 Team 權益，無需綁卡** —— 到期後可以永久留在 Free。
美元走 PayPal 訂閱自動續費（隨時可在 PayPal 內取消）；人民幣走微信收款碼 + 管理員人工確認。

自部署永遠是 MIT、不設限 —— SaaS 線只是給「不想自己跑伺服器」的人準備的。引擎完全一致，**開源版沒有任何功能被鎖**。

---

## 📊 基準測試

用倉庫內隨套件交付的複現腳本實測得出。複現腳本：`scripts/verify_recall_quality.py`、`scripts/verify_precision_recall.py`。

| 基準 | 分數 | 衡量什麼 |
| --- | --- | --- |
| LongMemEval | **96.2** | 長跨度對話召回 |
| LoCoMo | **94.8** | 多會話對話記憶 |
| BEAM (1M) | **68.5** | 1M token 上下文預算下的召回 |
| BEAM (10M) | **53.9** | 10M token 上下文預算下的召回 |

滿分 100。

---

## 🧩 功能一覽

<table>
<tr><td><b>記憶 API</b></td><td><code>Memory</code> / <code>AsyncMemory</code> / <code>MemoryClient</code>，方法面齊全：<code>add</code> <code>get</code> <code>get_all</code> <code>search</code> <code>update</code> <code>delete</code> <code>delete_all</code> <code>history</code> <code>reset</code> <code>close</code> <code>from_config</code>。</td></tr>
<tr><td><b>四維作用域</b></td><td><code>user_id</code> / <code>agent_id</code> / <code>run_id</code> / <code>app_id</code> —— 做的是<b>實體隔離</b>：每個作用域一個 SQLite 檔案，而不是共享列上加過濾條件。</td></tr>
<tr><td><b>過濾語言</b></td><td><code>eq</code> <code>ne</code> <code>gt</code> <code>gte</code> <code>lt</code> <code>lte</code> <code>in</code> <code>nin</code> <code>contains</code> <code>icontains</code> <code>wildcard</code>，支援 <code>AND</code>/<code>OR</code>/<code>NOT</code> 任意巢狀。</td></tr>
<tr><td><b>單遍 ADD-only 抽取</b></td><td>每次寫入一次模型呼叫，記憶只累加、不覆蓋。因為「不改寫」，壞抽取只會引入雜訊，永遠不會毀掉真事實。</td></tr>
<tr><td><b>圖記憶常開</b></td><td>實體連結與多跳遍歷由同一個 SQLite 檔案承載，不需要任何外部圖資料庫。</td></tr>
<tr><td><b>多模態攝入</b></td><td>支援 OpenAI / Anthropic / Gemini 三種圖片內容格式（含音訊）。配了視覺模型就生成描述，沒配就存引用——不丟東西。</td></tr>
<tr><td><b>多信號檢索</b></td><td>語意 + BM25 關鍵字 + 實體圖 + 時序 + 標籤五路融合，帶標定過的相關性下限與詞面兜底。</td></tr>
<tr><td><b>時序推理</b></td><td>觀測日期、相對時間解析、過期語意，以及按實體的版本鏈。</td></tr>
<tr><td><b>分層記憶</b></td><td>熱 / 溫 / 冷三層 + 遺忘經濟學 (economic forgetting)：低價值記憶降級壓縮，絕不靜默刪除。</td></tr>
<tr><td><b>無損壓縮（AIC）</b></td><td>把一條記憶壓成「指標 + 結構化事實 + 內容原子」。數字、日期、金額、型號在任何層級都完整保留；<code>expand()</code> 逐字取回原文並校驗哈希。</td></tr>
<tr><td><b>哈希鏈審計賬本</b></td><td>SHA-256 鏈式賬本；<code>verify_integrity()</code> 能發現篡改並指出具體是哪一條被改。</td></tr>
<tr><td><b>非同步 API 與事件</b></td><td><code>AsyncMemory</code> 支撐高吞吐寫入，加上持久化操作日誌，讓「已受理的寫入」跨程序可查。</td></tr>
<tr><td><b>中文優化</b></td><td>雙字分詞 (bigram) + FTS5 + 內建同義詞辭典，同時完整支援拉丁文字。</td></tr>
<tr><td><b>安全公證器</b></td><td>寫入前偵測憑據、不可見 Unicode 與 HTML 注入，並做欄位級脫敏 (field-level redaction)。</td></tr>
</table>

---

## 🔌 支援的整合

所有適配器都是可選的。標 **stdlib** 的不需要任何三方套件——直接用 `urllib` 走 HTTP；標 **sdk** 的惰性載入 SDK，缺套件時會明確告訴你裝哪個。

### LLM 供應商（20）

| 傳輸方式 | 供應商 |
| --- | --- |
| **stdlib HTTP** | `openai` `openai_structured` `azure_openai` `azure_openai_structured` `ollama` `anthropic` `gemini` `groq` `together` `deepseek` `minimax` `xai` `sarvam` `openrouter` `litellm` `lmstudio` `vllm` |
| **sdk** | `langchain` `aws_bedrock` |
| **內建** | `rules` —— 確定性離線抽取器，所以完全不配模型也能 `add()` |

### 嵌入模型（13）

| 傳輸方式 | 供應商 |
| --- | --- |
| **stdlib HTTP** | `openai` `azure_openai` `ollama` `gemini` `vertexai` `together` `lmstudio` `huggingface` |
| **sdk** | `fastembed` `langchain` `aws_bedrock` |
| **內建** | `builtin`（128 維、零依賴、確定性）· `hashing`（任意維度、離線） |

### 向量庫（28）

| 傳輸方式 | 向量庫 |
| --- | --- |
| **內嵌** | `builtin`（一個 SQLite 檔案同時存記憶和向量）· `memory` · `generic`（宣告式 REST） |
| **stdlib HTTP** | `qdrant` `pinecone` `elasticsearch` `opensearch` `weaviate` `upstash_vector` `turbopuffer` |
| **sdk** | `chroma` `pgvector` `milvus` `mongodb` `redis` `valkey` `azure_ai_search` `azure_mysql` `baidu` `cassandra` `databricks` `faiss` `langchain` `neptune` `oracledb` `s3_vectors` `supabase` `vertex_ai_vector_search` |

### 圖資料庫（6）

`builtin`（原生 SQLite 三元組）· `neo4j` · `memgraph` · `neptune` · `kuzu` · `sparql`（任意 SPARQL 1.1 端點）

### 重排器（5）

`llm` · `cohere` · `zero_entropy` · `huggingface` · `sentence_transformer`

### 框架適配

LangChain · LlamaIndex · CrewAI · Dify · n8n · Vercel AI SDK · Ollama · MCP（stdio + Streamable HTTP）

---

## 🛠 MCP 伺服器

以 stdio JSON-RPC 執行：

```bash
export MNEMOSYNE_MCP_TOKEN="your-secret-token"   # 可選，但建議設定
python -m mnemosyne.webui.mcp_server --brain-dir ./mem --namespace default
```

**55 個工具** —— 41 個原生工具（其中 21 個是 8.1.0 新增：9 個記憶形成層 + 12 個記憶執行層），外加 11 個沿用通行 Agent 記憶工具命名的工具，已有的 MCP 宿主（Claude Desktop、Hermes Agent 等）不必改寫工具定義就能直接指向 Mnemosyne。

**原生 · 檢索與治理（23）：**

| 工具 | 說明 |
| --- | --- |
| `retain` | 寫入一條記憶 |
| `recall` | 檢索記憶 |
| `retain_batch` | 批次寫入，約快 15× |
| `forget` | 遺忘一條記憶 —— 可用 id，也可用自然語言查詢定位 |
| `capsule` | 把一條記憶壓成 指標 + 事實 + 原子 |
| `expand` | 逐字取回膠囊原文 |
| `recall_health` | 唯讀的召回品質指標 |
| `consolidate` | 把近似重複的記憶合併成一條代表性記憶 |
| `reflect` | 統計、高頻實體、衝突偵測、認知模式 |
| `dedup` | 偵測重複與近似重複 |
| `graph_query` | 知識圖譜遍歷 |
| `temporal_query` | 版本鏈查詢 |
| `list_projects` | 列出隔離的專案 |
| `doctor` | 健康檢查 —— 完整性、筆數、磁碟、後端狀態 |
| `stats` | 執行統計 |
| `audit` | 審計鏈查詢 |
| `confidence_history` | 可信度軌跡 |
| `memory/export-v1` | 依 Memory Exchange Protocol 匯出 |
| `memory/import-v1` | 依 Memory Exchange Protocol 匯入 |
| `memory/claim` | 認領外部匯出的記憶 |
| `memory_export-v1` | `memory/export-v1` 的別名（供不接受 `/` 的用戶端使用） |
| `memory_import-v1` | `memory/import-v1` 的別名（供不接受 `/` 的用戶端使用） |
| `memory_claim` | `memory/claim` 的別名（供不接受 `/` 的用戶端使用） |

**原生 · 記憶形成層（9，v8.1.0 新增）：**

| 工具 | 說明 |
| --- | --- |
| `session_summarize` | 會話摘要：七要素 + 錨點原文保留，未決事項獨立成條 |
| `session_recall` | 召回摘要與未決事項，供下次對話主動追問 |
| `conflict_resolve` | 六類衝突判定 + 四操作；`scope=audit` 做全庫審計（只報真衝突） |
| `task_record` | 記錄任務事實（只追加），依 `goal_type` 支撐同類任務檢索 |
| `playbook_save` | 沉澱經驗，**強制** `preconditions` 三件套 |
| `playbook_recall` | 檢索候選經驗並做前置條件校驗，回傳 `applicable` / `blocked_by` |
| `code_change_record` | 記錄變更事件：意圖、約束、影響面、放棄的方案 |
| `code_archaeology` | 代碼考古 / 坑位預警（傳 `intent` 即轉為坑位檢查） |
| `memory_policy` | 敏感度判定、寫入准入、存量高危記憶審計 |

**原生 · 記憶執行層（12，v8.1.0 新增）：**

| 工具 | 說明 |
| --- | --- |
| `profile_card` | 使用者畫像卡：三段分級（穩定身份 / 偏好與約束 / 當前情境）+ 未決事項，可直接拼進 system prompt。`max_tokens` 是硬上限——超預算按「情境→未決→偏好」逐段裁，絕不截斷單條。 |
| `memory_inject` | 上下文注入流水線：查詢構造 → 四路召回 → RRF 融合 → 重排 → 預算內注入，回傳可直接注入的 text 加四段配額帳目。給模型準備上下文時比 `recall` 更合適。 |
| `recall_catalog` | 漸進披露·目錄層：只回傳標題 + 一句話 + 展開成本，不回傳正文。先看目錄判斷哪幾條相關，再對 1~2 條調 `expand_memory`——注入成本從 O(全部) 降到 O(目錄 + 少量)。 |
| `expand_memory` | 漸進披露·展開：取一條記憶的正文 + 統一信封 + 圖鄰居 + 證據鏈。與 `recall_catalog` 配對；鄰居來自知識圖譜，所以「當時這個決定影響了哪些模組」是可回答的。 |
| `memory_envelope` | 統一記憶信封：八類分類 + 四維 scope + 證據鏈 + 雙時間軸 + TTL + SPO 結構。省略 `memory_id` 時回傳全庫八類分佈統計。 |
| `memory_consolidate` | 鞏固三路徑：情境→語意、情境→程序性、失敗→負記憶。冪等——同一批來源不會重複產生。 |
| `memory_metrics` | 度量指標：Chat 5 項 + Working 4 項 + Coding 4 項。誠實原則——資料不足回傳 `{value:null, status:"insufficient_data"}` 而不是填 0，0 和「沒資料」是兩件事。 |
| `memory_cascade_delete` | 級聯刪除：軟刪原始 → 失效派生結論 → 清檢索快取 → 依賴摘要標 stale。`hard=true` 才物理刪除；先跑 `dry_run=true` 看 dependents。 |
| `memory_mask` | 遮罩：把正文替換為佔位符但保留記錄骨架，保持上下文長度與前綴不變（不破壞 KV-Cache 複用）。使用者說「別記這個」時優先用遮罩。`action=unmask` 可撤銷。 |
| `topic_summarize` | L3 主題摘要：把同一 project 下的多條 L2 會話摘要合併成「這個專案現在處於什麼狀態」。合併規則確定性（goal 取最新、decisions 累積、pending 只留最新）。`persist=true` 落庫並記錄 `pointer.children`。 |
| `memory_trace` | 指標鏈回溯：沿 L3→L2→L1 回溯，回傳每層內容、覆蓋的輪次區間與 `message_ids`。回答「這條結論是從哪句話來的」。 |
| `memory_hot_path` | 熱路徑緩衝：`action=enable` 開啟非同步寫入（熱路徑只入隊）；`action=status` 檢視佇列與統計；`action=flush` 等佇列落碟——寫完立刻讀之前必須 flush 的讀一致性屏障。 |

**客戶端相容（11）：**

| 工具 | 說明 |
| --- | --- |
| `add_memory` | 為使用者／代理儲存文字或對話歷史 |
| `search_memories` | 帶過濾條件的語意檢索 |
| `get_memories` | 結構化過濾 + 分頁列表 |
| `get_memory` | 依 id 取一條 |
| `update_memory` | 覆蓋文字和／或中繼資料 |
| `delete_memory` | 刪除一條 |
| `delete_all_memories` | 清空某個作用域 |
| `delete_entities` | 刪除實體並級聯 |
| `list_entities` | 列出 users/agents/apps/runs |
| `list_events` | 列出記憶操作 |
| `get_event_status` | 輪詢非同步操作 |

---

## 🌐 自託管 REST API

一個程序、一個埠，支援 `X-API-Key` / `Bearer` / `Token` 三種鑑權寫法，控制台與 API 共用同一個監聽。

| 方法 | 路徑 | 用途 |
| --- | --- | --- |
| `GET` | `/v1/status/` | 存活探針 + 即時設定報告 |
| `GET` | `/v1/providers/` | 所有供應商及其目前可用性 |
| `POST` | `/v3/memories/add/` | 抽取並寫入（非同步，回傳事件 id） |
| `POST` | `/v3/memories/search/` | 語意檢索 |
| `POST` | `/v3/memories/get-all/` | 帶過濾的列表 |
| `GET` / `PUT` / `DELETE` | `/v3/memories/{id}/` | 取 / 改 / 刪一條 |
| `DELETE` | `/v3/memories/` | 清空作用域 |
| `GET` | `/v3/memories/{id}/history/` | 變更歷史 |
| `GET` | `/v1/event/{id}/` · `/v1/events/` | 輪詢 / 列出操作 |
| `GET` / `DELETE` | `/v2/entities/` | 列出 / 刪除作用域 |
| `POST` | `/v3/graph/{add,search,get-all,delete-all}/` | 圖記憶 |
| `POST` | `/v1/capsule/` · `/v1/expand/` | 無損壓縮 |
| `GET` | `/v1/integrity/` | 賬本校驗 |
| `GET` / `POST` / `DELETE` | `/v1/keys/` | API 金鑰管理 |

---

## 🧠 Python API

```python
from mnemosyne import Memory, AsyncMemory, MemoryClient

# --- 抽取 / 作用域 / 過濾 -----------------------------------------------------
m = Memory()
m.add([{"role": "user", "content": "我 2023 年搬到了柏林。"}],
      user_id="alice", metadata={"source": "onboarding"},
      observation_date="2023-06-01")
m.add("發票號是 INV-2024-001。", user_id="alice", immutable=True)

hits = m.search("使用者住在哪",
                filters={"user_id": "alice",
                         "AND": [{"source": {"eq": "onboarding"}}]},
                top_k=5, threshold=0.1, rerank=False, explain=True)

# --- 多模態 -------------------------------------------------------------------
m.add([{"role": "user", "content": [
    {"type": "text", "text": "我的新桌面。"},
    {"type": "image_url", "image_url": {"url": "https://example.com/desk.jpg"}},
]}], user_id="alice")

# --- 圖記憶 -------------------------------------------------------------------
m.graph_add("賈伯斯在庫比蒂諾創立了蘋果。", user_id="alice")
m.graph_search("蘋果", filters={"user_id": "alice"})

# --- 非同步與事件 -------------------------------------------------------------
async def ingest():
    am = AsyncMemory()
    await am.add_many([{"messages": t, "options": {"user_id": "alice"}}
                       for t in transcripts])
```

`Memory` / `AsyncMemory` / `MemoryClient` 同時接受其他記憶庫通行的呼叫形態，所以已有程式碼只需改一行 import 即可切換。見 [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)。

引擎自身的能力掛在同一個物件上：

```python
from mnemosyne import MemoryBrain

brain = MemoryBrain("./memories", enable_embeddings=True)
brain.ensure_init()
brain.retain("他那台筆記型電腦是 ASUS VivoBook Pro 14", fast=True)

results = brain.recall("那台機器的設定是什麼", k=5)
results, cost = brain.recall("那台機器的設定", k=5, budget_tokens=100)

cap = brain.capsule("<memory_id>", budget_tokens=60)   # 指標 + 事實 + 原子
brain.expand(cap["ref"])                                # 逐字取回
brain.verify_integrity()                                # SHA-256 賬本校驗
```

---

## 📂 目錄結構

```
mnemosyne/
├── api/                 # 記憶 API：Memory / AsyncMemory / MemoryClient
│   ├── memory.py        #   引擎支撐的客戶端
│   ├── config.py        #   MemoryConfig + 維度一致性校驗
│   ├── filters.py       #   過濾語言 → 述語
│   ├── extract.py       #   單遍 ADD-only 抽取
│   ├── multimodal.py    #   圖片 / 音訊附件解析
│   ├── events.py        #   持久化操作日誌
│   └── client.py        #   內嵌 + HTTP 雙傳輸
├── providers/           # 可選元件適配器（共 72 個）
│   ├── llms.py          #   20 個 LLM 供應商
│   ├── embedders.py     #   13 個嵌入供應商
│   ├── vector_stores.py #   28 個向量庫
│   ├── graph_stores.py  #   6 個圖資料庫
│   ├── rerankers.py     #   5 個重排器
│   ├── vision.py        #   三種圖片 wire format
│   └── transport.py     #   標準庫 HTTP + 重試 + 憑據脫敏
├── brain.py             # MemoryBrain —— 引擎門面
├── capsule.py           # AIC 無損壓縮
├── retrieval.py         # 多信號融合與相關性標定
├── graph.py             # 時序三元組儲存
├── notary.py            # 寫入前信任流水線
├── cli.py               # 原生 CLI
├── api_cli.py           # 客戶端 API CLI
└── webui/
    ├── web_server.py    # 控制台 + REST 宿主
    ├── api_routes.py    # /v1 /v2 /v3 路由
    ├── mcp_server.py    # 41 個原生 MCP 工具（含 21 個新增於 8.1.0）
    └── mcp_api.py       # 11 個客戶端相容 MCP 工具

storage/                 # SQLite 後端、哈希鏈賬本、外掛 SDK
security/                # 矛盾偵測、安全報告
scripts/                 # 驗收腳本
docs/                    # 相容說明
```

---

## ✅ 測試

```bash
python verify.py                              # 自檢
python scripts/verify_api.py                  # 客戶端 API 驗收，全離線
python scripts/verify_memory_lifecycle.py --brain-dir ./mem --src-root .
python scripts/verify_precision_recall.py     # 離線精準迴歸
python scripts/verify_recall_quality.py       # 端到端召回品質
```

---

## 📚 文件

- [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) —— 通行的 Agent 記憶呼叫形態
- [docs/DEPLOY_DEEPSEEK_HARNESS.md](docs/DEPLOY_DEEPSEEK_HARNESS.md) —— MCP 部署實操
- [CHANGELOG.md](CHANGELOG.md) —— 版本歷史

---

## 📄 授權

MIT 授權 —— 見 [LICENSE](LICENSE)。

由 Mnemosyne OS 貢獻者共同構建。
