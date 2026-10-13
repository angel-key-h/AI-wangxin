# 多模态数据驱动的可解释精准问数/问答智能体

> 「中国电子杯」第三届「四邮四电」高校 ICT 产教融合创新大赛
> 赛题八 · 中国电子云计算技术有限公司命题

一个**单一入口的多模态 Agent**：用户用自然语言提问（可附图片），系统自动判定该去
**结构化数据库问数**还是**非结构化文档问答**，执行后给出答案，并**始终附带引用的
原文片段**——包括走 SQL 路径的问答，来源链路不断档。

```text
用户提问 ─┬─ 指代消解（多轮） → 意图路由 ─┬─ SQL 路径：Schema Linking → NL2SQL → 执行 → 结果解读
          │                              └─ RAG 路径：混合检索 → 重排 → 生成
          └─ 附件图片 → VLM 识别（表格 / 公式 / 图表 / 质量诊断）
                                  ↓
                    统一作答 + 引用文档片段高亮 + 处理链路 trace
```

---

## 1. 项目亮点速览

| 能力 | 实现要点 | 可复现的量化结果 |
|---|---|---|
| **问数准确率** | 表名/别名画像 + Schema Linking 裁剪 + SQL 自修复重试 | `eval_nl2sql.py`：**34/34 = 100%** |
| **答案可解释性** | 检索是**所有**问题的必经节点，SQL 路径也回传引用｜来源片段**逐字高亮**命中词 | `verify_live.py` 断言通过；`tools/explain.py` 纯函数测试 |
| **多轮上下文保持** | LangGraph 首节点显式指代消解，只对真含指代的问题触发 | `eval_multiturn.py`：**baseline 3/8 → 8/8** |
| **智能语义校验与澄清** | 6 维受控词表 + 规则兜底 + 规则层独立对照基线 + 选项可点即回 | 召回 **100%** / 精确 **100%** / F1 **100%**（误澄清 **0%**，见 `data/eval/reports/clarify-*.json`）；规则层独立对照（不调 LLM）**9/9**、误报 **0** |
| **文档复杂度评估** | 10 特征打分 → 5 档 → 输出切块/解析策略并经索引链路真实消费 | 单调性 **97.5%**；**29/40 = 72.5%** 的样本拿到非默认策略 |
| **扭曲表格识别** | 本地 RapidOCR 与 qwen-vl-max 双通道，同一份比对代码 | 单元格 F1 **58.9% → 80.4%**（平均 **+23.1pt**） |
| **公式 / 图表理解** | VLM 还原二维公式结构与图表数据序列 | 公式字符召回 **28.2% → 96.7%**；图表类别→数值 **5/5** |
| **鲁棒性** | 单文档解析失败不拖垮全局；OCR/VLM 不可用时静默降级 | `verify_live.py` 全链路断言；退化检出率 **100%**、干净假阳性 **0/3** |

---

## 2. 与赛题评分表的对应关系

### 2.1 基础任务（40 分）

| 评分项 | 分值 | 本项目的实现位置 |
|---|---|---|
| 单表问数 NL2SQL | 10 | `tools/sql_tool.py`、`tools/schema_linker.py` |
| 单文档 RAG 问答 | 10 | `tools/rag_tool.py`、`agent/graph.py::execute_rag` |
| 答案可解释性（引用片段高亮） | 5 | `tools/explain.py`、`app.py` 来源区渲染 |
| 可运行的 Demo | 5 | `app.py`（Streamlit 单页应用） |
| 覆盖 ≥1 个中级难题 | — | 已覆盖 **9 个**（见下） |

### 2.2 已实现的中级难题

| 编号 | 难题 | 状态 | 代码位置 |
|---|---|---|---|
| 1 | 要素精准识别与问题改写 | ✅ | `agent/graph.py::resolve_question`、`tools/schema_linker.py` |
| 2 | 可扩展的要素提取（不依赖 Few-Shot） | ✅ | `data/schema_profile.json` + 指纹失效重建 |
| **3** | **智能语义校验与澄清** | ✅ | `agent/clarify.py`（6 维受控词表 + 规则兜底） |
| 4 | 精准表定位 Schema Linking | ✅ | `tools/schema_linker.py` |
| 5 | 高质量多表关联 NL2SQL | ✅ | `tools/sql_tool.py`（JOIN 路径 / 聚合 / 嵌套） |
| **6** | **文档公式识别与计算** | ✅ | `tools/vlm_tool.py`、`tools/image_tool.py` |
| 7 | 不标准目录结构的鲁棒识别 | ✅ | `tools/rag_tool.py::split_by_heading / split_document_aware`（标题层级切块 + 章节路径注入） |
| **8** | **文档复杂度评估（指导处理策略）** | ✅ | `tools/doc_quality.py::complexity_plan` |
| 9 | 文档质量综合评估（倾斜/清晰度/错字） | ✅ | `tools/doc_quality.py::quality_report` |

> 难题 6 / 8 / 9 同时覆盖了「文档质量评估」与「图像理解」两条线，也是赛题少数
> 区分度高的方向。

---

## 3. 运行环境说明

**实测通过的运行环境**（本机 `Windows 11`）：

| 项 | 版本 |
|---|---|
| 操作系统 | Windows 11 (x64) |
| Python | **3.13+**（本仓库实测 3.13.12；版本一律由 `requirements.txt` 钉死，仓库无 pyproject） |
| Streamlit | 1.65.0 |
| pandas / openpyxl | 3.0.6 / 3.1.5 |
| langgraph / langchain-core | 1.2.14 / 1.6.9 |
| chromadb | 1.5.9 |
| onnxruntime（本地 OCR） | 1.31.0 |
| SQLite | Python 内置 `sqlite3`（无需额外安装） |

**外部服务**：阿里云百炼（DashScope）OpenAI 兼容端点，需要三个模型：

| 用途 | 模型 | 说明 |
|---|---|---|
| 对话 / SQL 生成 / 改写 | `qwen3-max` | 主 LLM |
| 向量化 | `text-embedding-v4` | 默认 1024 维，单批上限 10 条 |
| 图像识别 | `qwen-vl-max` | 表格 / 公式 / 图表；未配置时自动退回本地 OCR |

> `qwen-vl-max` 未配置时系统**不会报错**，会静默降级到 RapidOCR（见
> `Config.vlm_ready()`）。这保证了离线环境也能跑通基础任务。

**无需 GPU**：本地 OCR 走 `rapidocr-onnxruntime`（CPU 推理）；重模型全部在云端 API。

---

## 4. 快速开始

### 4.1 一键启动（Windows）

```bat
start.bat
```

脚本会自动完成：检查 Python → 检查/创建虚拟环境 → 安装依赖 → 校验 `.env` →
首次运行时构建问数库与向量索引 → 启动 Streamlit。

### 4.2 手动启动

```bash
# 1) 安装依赖
python -m pip install -r requirements.txt

# 2) 配置密钥（见第 5 节）
cp .env.example .env   # 若无 .env.example，直接新建 .env

# 3) 首次初始化（建问数库 + 导出 schema，全程离线、不需要 API Key）
python scripts/init_runtime.py --force

# 4) 启动（向量索引在首次打开页面时自动构建）
streamlit run app.py
```

> `scripts/build_demo_kb.py` 用于**重新生成**多格式演示知识库，它依赖 OHR-Bench
> 数据集（`data/ohr/`，由 `.workbuddy/download_ohr.py` 下载，未随仓库分发）；
> 只想把项目跑起来不需要它 —— 仓库里已带 13 份成品文档。

浏览器打开 <http://localhost:8501> 即可。

---

## 5. 模型配置（`.env`）

`.env` 与项目根目录同级，**优先级高于代码里的默认值**：

```ini
# ── 主 LLM ──
LLM_API_KEY=sk-xxxxxxxxxxxxxxxx
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen3-max

# ── Embedding ──
EMBEDDING_API_KEY=sk-xxxxxxxxxxxxxxxx
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_MODEL=text-embedding-v4

# ── VLM（可选，缺省时自动退回本地 OCR）──
VLM_MODEL=qwen-vl-max
VLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

> ⚠️ **端点地域必须与 Key 的地域一致**：北京用
> `dashscope.aliyuncs.com`，国际用 `dashscope-intl.aliyuncs.com`，混用会 401。
> 忘记配置时页面会明确提示缺失的 Key 名（`Config.missing_keys()`），
> 而不是抛一个看起来像「Key 过期」的 401。

---

## 6. 目录结构

```text
intelligent-qa-agent/
├── app.py                     # Streamlit 单页前端（唯一入口）
├── config.py                  # 全局配置（模型 / 路径 / 开关）
├── requirements.txt
├── start.bat                  # 一键启动脚本
├── agent/                     # LangGraph 主流程
│   ├── graph.py               #   resolve → classify → retrieve → (sql) → generate
│   ├── state.py               #   会话状态定义
│   └── clarify.py             #   6 维受控词表 + 规则兜底的澄清引擎
├── tools/                     # 能力层
│   ├── rag_tool.py            #   向量索引构建 / 混合检索 / 重排 / 策略消费
│   ├── sql_tool.py            #   NL2SQL 生成、只读执行、自修复
│   ├── schema_linker.py       #   表/字段定位与裁剪
│   ├── db_builder.py          #   从 rag_data 表格文件自动建表
│   ├── doc_quality.py         #   复杂度评估 + 质量诊断 + 处理策略
│   ├── vlm_tool.py            #   qwen-vl-max 通道（表格/公式/图表）
│   ├── image_tool.py          #   本地 OCR 通道 + 图像预处理
│   ├── explain.py             #   来源片段查询词高亮
│   └── file_utils.py          #   真实类型嗅探 / 多编码读取
├── rag_data/                  # 知识库源文档（13 份，7 种格式）
├── data/                      # 运行时数据（见第 7 节）
│   ├── demo_table.png         #   ★ 多模态输入演示素材（见 7.3）
│   └── schema.sql             #   ★ 数据库 schema 定义文件（自动导出）
├── scripts/                   # 初始化 + 评测 + 运维脚本
└── docs/                      # 补充文档
```

---

## 7. 数据集与知识库

### 7.1 结构化问数库（`data/agent.sqlite`，16 张表 / 16241 行）

库由两条来源合成，`data/schema.sql` 是它的**完整 schema 定义文件**：

1. **Chinook 示例库**（11 张表）—— 官方 `lerocha/chinook-database` 完整版，
   用于演示多表 JOIN、聚合、嵌套查询。
2. **`rag_data/` 下结构化文件自动建表**（5 张）：

   | 表 | 源文件 | 规模 |
   |---|---|---|
   | `finance_JPMORGAN_2022_10K_page0` | `finance_JPMORGAN_2022_10K.xlsx` | 9 行 × 3 列 |
   | `finance_JPMORGAN_2022_10K_page3` | `finance_JPMORGAN_2022_10K.xlsx` | 6 行 × 2 列 |
   | `finance_JPMORGAN_2023Q2_10Q` | `finance_JPMORGAN_2023Q2_10Q.csv` | 49 行 × 16 列 |
   | `law_ArmstrongFlooringInc_20190107_8K_EX1` | `law_...Intellectual_Prop.xlsx` | 1 行 × 2 列 |
   | `综合素质测评情况` | `【电子学院…】综合素质测评情况.xlsx` | 569 行 × 23 列 |

   > 这条链路本身就是「**非结构化文件 → 结构化问数**」能力的演示：
   > 「智育前十是谁」在只接 Chinook 时无表可用，`db_builder` 把 xlsx 建成表后
   > 就退化成一条普通的 `ORDER BY ... LIMIT 10`。

> `data/schema.sql` 由 `scripts/export_schema.py` **从真实库导出**，不是手写整理；
> 库结构一变重跑脚本即可，不存在「文档写的是旧字段」的失分点。

### 7.2 知识库文档（`rag_data/`，13 份 / 7 种格式）

| 格式 | 份数 | 示例 |
|---|---|---|
| `.pdf` | 4 | `finance_3M_2023Q2_10Q.pdf`、综测结果 PDF |
| `.xlsx` | 3 | `finance_JPMORGAN_2022_10K.xlsx`、综测结果 XLSX |
| `.docx` | 2 | 法律合同、数学教材章节 |
| `.pptx` | 1 | `manual_2021-Apple-Catalog.pptx` |
| `.csv` | 1 | `finance_JPMORGAN_2023Q2_10Q.csv` |
| `.md` | 1 | 概率论教材节选 |
| `.txt` | 1 | 新闻稿 |

内容横跨**财报 / 法律文书 / 产品手册 / 教材 / 新闻 / 中文业务表格**六类领域，
天然覆盖赛题「涵盖不同类型格式」的要求，同时也构成复杂度评估的分档样本集。

### 7.3 多模态输入演示素材（`data/demo_table.png`）

`data/demo_table.png`（1220 × 948）是「电子学院2025级电子工程类综测素质测评结果」
的表格**截图**（含学号/姓名/行政班/综合素质测评成绩/智育测评分/智育测评分排名 6 列，
共 15 行）。它与 `rag_data/` 里的同名 PDF、XLSX 是**同一份数据的三种形态**，
分别对应三条不同的入库路径：

| 形态 | 文件 | 进入系统的路径 |
|---|---|---|
| 截图 | `data/demo_table.png` | 页面「选择图片」上传 → `tools/image_tool.ocr_to_table` |
| PDF | `rag_data/…综合素质测评情况.pdf` | RAG 知识库（文本检索） |
| XLSX | `rag_data/…综合素质测评情况.xlsx` | `tools/db_builder.py` 建成 SQL 表 `综合素质测评情况`（569 行） |

**用途：演示「图片 → 表格结构还原 → 问数」这条链路**（对应赛题的多模态输入要求）。
在页面上传该图，即可看到 OCR 还原出的行列结构并直接提问。

实测（**本地 OCR 通道，不需要 API Key、不依赖外部服务**）：

```text
识别到的列名: ['学号', '姓名', '行政班', '综合素质测评成绩', '智育测评分', '智育测评分排名']
行数: 15
首行: 2025210751 / 胡子扬 / 2112503 / 104.30 / 90.70 / 1
```

即截图被**逐列、逐值正确还原**，且与库表数据一致 —— 胡子扬一行（综合素质 104.3、
智育 90.7、排名 1）与 `data/agent.sqlite` 中的记录**逐字段吻合**。这也是澄清演示
用例（`scripts/verify_clarify_ui.py`，问「胡子杨」→ 澄清为「胡子扬」→ 答出 90.7）
里那两个数字的**肉眼可核对依据**。

> 该图自首次提交起即在库中；本节即为其用途说明，避免被误当成无关文件。

---

## 8. 评测与验证

所有指标均可用脚本独立复现，**不需要人工判分**：

```bash
# ── 结构化问数 ──
python scripts/eval_nl2sql.py            # 端到端执行准确率        → 34/34 = 100%

# ── 知识问答 ──
python scripts/eval_rag.py --save        # 检索命中率 / 答案准确率 / 忠实度
python scripts/eval_multiturn.py         # 多轮指代与省略（默认落盘报告）

# ── 语义澄清（难题 3）──
python scripts/eval_clarify.py --rule-only --save  # 规则层对照（离线，不调模型）
python scripts/eval_clarify.py           # 模型层：维度命中 / 误报 / F1（需 Key）

# ── 文档复杂度与质量（难题 8 / 9）──
python scripts/eval_doc_quality.py --save   # 外部效度：真实文档分档 + 策略分化
python scripts/eval_complexity_plan.py      # 内部效度：不变量 + 策略一致性 + 端到端

# ── 图像 / 表格 / 公式识别（难题 6，含赛题点名的 F1）──
python scripts/eval_doc_recog.py --engine ocr --save   # 本地 RapidOCR 基线
python scripts/eval_doc_recog.py --engine vlm --save   # qwen-vl-max 通道
python scripts/eval_doc_recog.py --compare             # 两通道 F1 前后对比（不重跑）

# ── 其它 ──
python scripts/eval_schema_link.py --no-vector --save   # Schema Linking（纯词法，离线可跑）
python scripts/eval_correction.py        # 拼写纠错
python scripts/eval_extensible_schema.py # 可扩展要素提取

# ── 全链路断言（不需要真实 API Key；缺 Key 时跳过依赖模型的断言）──
python scripts/verify_live.py            # 路由 / 澄清 / 纯函数不变量
python scripts/verify_app.py             # 页面元素存在性

# ── 需 API Key：澄清按钮「可点且能接着答」的交互闭环（缺 Key 以退出码 2 退出）──
python scripts/verify_clarify_ui.py

# ── 需 OHR 素材：物化知识库（缺数据以退出码 3 退出）──
python scripts/build_demo_kb.py          # 演示知识库（需 retrieval.zip）
python scripts/prepare_ohr_kb.py         # 评测知识库（需 retrieval.zip + parquet）

# ── 检索优化等价性（需 EMBEDDING_API_KEY：要统计真实 embedding 调用次数）──
python scripts/verify_search_equiv.py    # 重排复用 query 向量 → 结果一致 + 调用降半
```

**评测脚本的退出码约定**（缺环境时不再崩栈，而是给可操作提示后退出）：
`0` = 完成（达标与否见脚本结论）｜`1` = 完成但未达标｜
`2` = 缺少 API Key｜`3` = 缺少外部数据集（OHR-Bench / `retrieval.zip`）｜
`10`/`11` = 降级完成（澄清专用：缺 Key 时只跑规则层，达标/未达标）。
公共逻辑见 `scripts/_preflight.py`。其中 `--rule-only` 让澄清评测在**无 Key**
时也能产出规则层证据（这是赛题「评分规则设计与实现路径」要求的可复现基线）。

**退出码不会替你猜**：请求了需要 Key/数据的模式却缺环境时，脚本**在开跑前**就
以 2/3 退出，不会「跑完再给个数」。典型例子是 `eval_schema_link.py` 的向量模式：
embedding 调用失败会被静默吞掉、结果与纯词法逐项相同，若照常落盘就会把纯词法
标成 `lexical+vector`。现在它缺 Key 直接退出 2，且报告里的 `mode` 按
`vector_engaged_queries`（真正算出向量分的查询次数）**实测**填写。

**注意 OHR-Bench 不入库**：`eval_doc_recog` / `eval_doc_quality` / `eval_correction`
依赖公开数据集 `data/ohr/OHR-Bench.parquet`（体积大，故意不纳入版本库），
新克隆仓库需自行下载，或用 `OHR_DIR` 指向别处。`data/ohr_kb/`（195 页物化知识库）
在库内，故 RAG 评测不缺数据、只缺 Key。

### 参考结果（本机实测）

| 脚本 | 指标 | 结果 |
|---|---|---|
| `eval_nl2sql.py` | 端到端执行准确率 | **34/34 = 100%** |
| `eval_rag.py` | 检索命中率 hit@1 / hit@4 | **84.8% / 95.5%** |
| `eval_rag.py` | 答案准确率（模型裁判） | **56/66 = 84.8%** |
| `eval_rag.py` | 忠实度（模型裁判） | **65/66 = 98.5%** |
| `eval_multiturn.py` | 多轮指代消解 | baseline **3/8** → **8/8** |
| `eval_clarify.py` | 澄清召回率 / 精确率 / F1 | **100% / 100% / 100%** |
| `eval_clarify.py` | 维度误报数 | **0** |
| `eval_clarify.py` | 规则层独立对照（不调 LLM，可离线复现） | **9/9**（维度命中，6 维全覆盖）/ 误报 **0** |
| `eval_schema_link.py` | Schema Linking top1 / recall@6（纯词法） | **19/20 = 95.0%** / **20/20 = 100%** |
| `eval_schema_link.py` | 大小写一致性（同句三种写法同 top1） | **5/5 = 100%** |
| `eval_extensible_schema.py` | 可扩展要素提取（难题 2，确定性检查） | **9/9 = 100%**（不消耗 LLM） |
| `eval_doc_quality.py` | 复杂度单调性 | **97.5%** |
| `eval_doc_quality.py` | 退化检出率 / 干净假阳性 | **100%** / **0/3** |
| `eval_doc_quality.py` | 样本获得非默认策略的比例 | **29/40 = 72.5%** |
| `eval_complexity_plan.py` | 分档准确率（内部效度） | **6/6 = 100%** |
| `eval_doc_recog.py` | **表格单元格 F1**：clean / skew / blur / lowres | **58.9 → 80.4 / 56.5 → 81.3 / 54.9 → 81.4 / 57.0 → 76.8**（%） |
| `eval_doc_recog.py` | 平均单元格 F1 提升 | **+23.1 个百分点** |
| `eval_doc_recog.py` | 公式字符召回 / 完全正确 | **28.2% → 96.7%** / **0/10 → 2/10** |
| `eval_doc_recog.py` | 图表类别 → 数值映射 | **0/5（本地 OCR 读不出）→ 5/5** |
| `verify_search_equiv.py` | 检索结果等价性 / query 编码次数 | **8/8 完全一致** / **16 → 8 次** |

> 结果 JSON 落在 `data/eval/reports/`，每次运行带时间戳，可回看历史。
> **全部报告的分工与权威产物对照见 [`data/eval/reports/INDEX.md`](data/eval/reports/INDEX.md)**
> —— 同族会有多份历史报告，核对数字时请以该索引为准。

### 测量口径的几点说明（避免误读）

1. **本地 OCR 通道可逐位复现，VLM 通道有轻微波动。** 同一份 OCR 报告重跑，
   `header_acc` / `cell_recall` 与历史报告**逐位相同**；VLM 侧即使
   `temperature=0`，同一样本的单元格召回也会有 ±1 个百分点级波动（源于服务端）。
   因此跨通道对比取同一批次两次运行的报告，不要拿不同日期的 VLM 数字互相比较。
2. **表格 F1 是「单元格级集合口径」**：真值与识别结果各自去重成字符串集合，
   `TP = |真值 ∩ 识别|`，P/R/F1 由此计算。单元格用集合口径（无重复计数歧义），
   表头用**一对一贪心匹配**（宽松子串规则下 TP 会超过预测数，会算出大于 1 的
   precision）。旧有的 `header_acc` / `cell_recall` 保持原口径不变，与历史可比。
3. **部分指标的报告早于最近的代码改动，需重跑确认。** 逐项说明：
   - `eval_clarify` 的报告生成于 **4 维 / 17 条用例**时期，而代码已扩到
     **6 维 / 19 条用例**（2026-10-11），因此上表中的澄清指标应视为**下界**；
     规则层口径已于本轮离线补跑（`clarify-20261012-235033.json`，9/9），
     模型层（F1 / 闭环）仍需配 Key 重跑；
   - `eval_multiturn` 此前**不落盘** —— README 引用的 8/8 在 `data/eval/reports/`
     里没有对应文件。现已在脚本内补上落盘（默认开启，`--no-save` 可关闭），
     配好 Key 重跑一次即可生成报告；
   - 2026-10-11 的修复把 RAG **索引版本升到 v4**（新增标题层级切块，见难题 7），
     `eval_rag` 的 hit@1 / hit@4 需在**新索引**上重跑 —— 旧数字对应旧的切块逻辑。
   - `eval_schema_link` 的评测集于 2026-10-12 由 15 条扩到 **20 条**（新增 5 条
     英文直问，用于覆盖英文词形归一），上表数字是**新评测集**上的实测值；
     扩集前的 15 条口径结果是 14/15 = 93.3%。
   - 2026-10-13 修掉一处**「靠漏判侥幸正确」**：样本值匹配原为大小写敏感，
     导致同一句英文大小写不同选表不同（基线一致性仅 2/5）。修法见
     `data/eval/reports/INDEX.md` 二·8。**准确率 95.0% 不变**，但结论从此
     不再依赖 bug；上表新增的「大小写一致性」一栏即为该修复的回归断言。
   - 2026-10-13 另修三处「退出码 / 自述与事实不符」（见 `INDEX.md` 二·9）：
     `eval_schema_link` 向量模式缺 Key 时**静默降级却把报告标成
     `lexical+vector`**（等于交一份自述错误的证据）→ 现在缺 Key 开跑前退出 2，
     报告 `mode` 按 `vector_engaged_queries` 实测填写；`verify_clarify_ui`
     缺 Key 返回 1（会被误读成「界面坏了」）→ 改为 2；`build_demo_kb` /
     `prepare_ohr_kb` 缺数据返回 1 → 改为 3，并各自报出缺的是哪个素材文件。
   - `eval_extensible_schema`（难题 2）此前只往终端打印、不落盘，现已默认落盘。
   - `eval_doc_recog` / `eval_doc_quality` / `eval_correction` 依赖 **OHR-Bench
     外部数据集**（不入库），新克隆仓库需先准备好 `data/ohr/` 才能重跑；
     未就绪时脚本会以退出码 3 给出提示，不再抛 `FileNotFoundError` 崩栈。
     `build_demo_kb` / `prepare_ohr_kb` 同理（还需 `retrieval.zip`）。

---

## 9. 关键设计说明

### 9.1 为什么检索是所有问题的必经节点

赛题基础任务要求「**所有问答结果**都要能给出引用的文档片段」。如果把检索挂在
`if intent == "rag"` 分支下，走 SQL 的问题就没有来源可展示，来源展示会断档。
因此 `agent/graph.py` 把 `retrieve` 提为**必经节点**，意图只决定要不要**再**查库。

### 9.2 来源高亮为什么不能直接 Markdown 渲染

`tools/explain.py` 在插入 `<mark>` **之前先做 `html.escape`**，避免原文里的
`<`、`&` 被当成标签。同时用**两趟扫描**（强词锚定 → 弱词填缝）解决抢位问题：
若只用「长词优先贪婪扫描」，问题的 4-gram 如「的智育测」起点更靠前会先占位，
真正的关键词「智育测评分」反而永远匹配不上。

### 9.3 澄清为什么要有「规则兜底」

`agent/clarify.py` 分两层：LLM 负责判断缺失维度（理解力强），但**维度取值必须落在
6 维受控词表内**；LLM 调用失败或返回越界值时，由规则层（R1 时间 / R2 指标 /
R3 指代 / R4 对比 / R5 实体确认 / R6 查询维度）接管。这样做的收益是可报告、可审计：
`eval_clarify.py` 能单独跑规则层，得到一个**不依赖 LLM 的对照基线**。

### 9.4 复杂度评估必须被真实消费

只输出一个分数是「评分规则」，无法证明「指导处理策略」。因此链路是闭环的：

```text
complexity_plan() → 页面展示 → 落盘 data/doc_plans.json
                              → RAGTool 按策略逐文档选 splitter（简单 1000/160，密集 600/240）
                              → 按策略放宽 top_k（上限 20）
                              → 检索结果回传 complexity 档位
```

实测同一批文档：无策略约 **5 块** → 有策略 **16 块**，端到端可验证
（`eval_complexity_plan.py` part D）。

### 9.5 标题层级切块为什么必须带「分布保护」

`tools/rag_tool.py` 把 Markdown `#`、中文「第三章 / 一、/（二）」与 Word 段落样式
三类信号**归一成同一套 `(层级, 标题)`**，再按章节边界切块，并把章节路径
前置到正文（让 embedding 感知上下文）+ 写进 `metadata["heading_path"]`
（让引用展示能说出出自哪一节）。

难点不在识别标题，而在**别把列表和表格数据行当成标题** —— 它们逐行长得很像：

```text
1. Southern Tier West        ← 编号列表
74.03 208 90.70 1 59.0       ← 表格数据行
```

所以判据只能落在**分布**上：章节标题之间必然隔着正文，列表 / 表格行则成串紧挨着。
实测不设这道保护，一份 12 页 PDF 会被切出 **179 块**（应约 19 块）、一份 30 页
成绩单 PDF 切出 **629 块**（应约 89 块）。加上保护后，13 份知识库文档的块数只从
**839** 变到 **855**（+1.9%），其中 8 份无结构文档**一块不差**（逐字节等价于改造前）。

---

## 10. 交付物自查

| 交付物 | 状态 | 位置 |
|---|---|---|
| 源码 | ✅ | 项目根目录 |
| README | ✅ | `README.md` |
| **数据库 schema 定义文件** | ✅ | `data/schema.sql`（`scripts/export_schema.py` 导出，含空库自检） |
| **知识库文档 ≥10 份、涵盖不同类型格式** | ✅ | `rag_data/`：13 份 / 7 种格式 |
| **运行环境说明** | ✅ | 本文第 3 节 |
| **一键启动脚本** | ✅ | `start.bat` |
| **依赖包列表及版本号** | ✅ | `requirements.txt`（全部钉死版本） |
| 评测脚本（指标可复现） | ✅ | `scripts/eval_*.py`、`scripts/verify_*.py` |
| **设计报告（≤20 页，含核心亮点预览页）** | 🚧 待补 | — |
| **技术实现说明书（≤30 页，7 个模块章节）** | 🚧 待补 | — |

---

## 11. 常见问题

**Q：启动后报 401 / 认证失败？**
A：先确认 `.env` 里的 `LLM_API_KEY` / `EMBEDDING_API_KEY`，再确认 `*_BASE_URL`
的地域与 Key 地域一致（北京 `dashscope.aliyuncs.com` / 国际 `dashscope-intl.aliyuncs.com`）。

**Q：改了知识库文档但不生效？**
A：索引指纹包含文档内容哈希与 embedding 模型名，改动后**会自动重建**索引。
若想强制重建，删除 `data/vector_store/` 后重启。

**Q：没有 VLM Key 能跑吗？**
A：能。图像识别会静默降级到本地 RapidOCR（CPU 推理），基础功能不受影响，
只是公式与图表类识别精度下降。

**Q：换数据库规模（如 AdventureWorks）要改代码吗？**
A：不需要。把库文件放入 `data/` 并指向 `Config.SQL_DB_PATH` 即可，
Schema Linking 会按配置的限额自动裁剪 —— 表越多，该能力的收益越明显。
