# 评测报告索引

本目录保存全部评测的**原始产物**，文件名格式为 `<族>-<YYYYMMDD-HHMMSS>.{json,md}`。

同一族出现多份是正常的：开发期反复重跑会各自留档。**这些历史文件不删除**——
它们构成可追溯的证据链（能看出某个指标是怎么一步步调上来的）。但核对数字时，
请以本索引指定的**权威产物**为准，不要随手取目录里最后修改的那份。

---

## 一、权威产物对照表

| 指标 | 生成脚本 | 权威产物 | 实测值 |
|---|---|---|---|
| **NL2SQL 准确率** | `scripts/eval_nl2sql.py` | `nl2sql-20261013-152707.json` | **34/34 = 100%**（四档全满；`deepseek-flash`，见二·10） |
| **RAG 检索命中率** | `scripts/eval_rag.py` | `rag-20260925-111245.json` | hit@1 **84.8%** / hit@4 **95.5%** / 宽松匹配 **71.2%**（n=66）⚠️ 见二·3、二·10 |
| RAG 答案准确率 / 忠实度 | `scripts/eval_rag.py` | `rag-20260925-111245.md` | 答案准确率 **84.8%** / 忠实度 **98.5%**（LLM 裁判，n=66） |
| **澄清 精确/召回/F1（模型层）** | `scripts/eval_clarify.py --save` | `clarify-20261013-152833.json` | **100% / 100% / 100%**，误澄清率 **0%**（tp9/fp0/fn0/tn10）；维度命中 **9/9**、维度误报 **2**（见二·10） |
| **多轮指代消解** | `scripts/eval_multiturn.py` | `multiturn-20261013-152930.json` | baseline **3/8** → fixed **8/8 = 100%** |
| **澄清 · 规则层（离线可复现）** | `scripts/eval_clarify.py --rule-only --save` | `clarify-20261012-235033.json`（mode=rule-only） | 规则层维度命中 **9/9 = 100%**（6 维全覆盖，n=9 正例）、误报 **0** |
| **Schema Linking（纯词法）** | `scripts/eval_schema_link.py --no-vector --save` | `schemalink-lexical-20261013-135500.json` | top1 **19/20 = 95.0%** / recall@6 **20/20 = 100%**（16 表）/ 大小写一致性 **5/5 = 100%** / `vector_engaged_queries` **0**（如实标注未跑向量） |
| **中级难题 2 · 可扩展要素提取** | `scripts/eval_extensible_schema.py` | `extensible-20261012-232537.json` | **9/9 = 100%**（确定性检查，不消耗 LLM） |
| **文档质量分档单调性** | `scripts/eval_doc_quality.py --save` | `docquality-20260925-111801.json` | 单调率 **0.975**；干净文档误报 **0/3** |
| **表格识别（VLM 路径）** | `scripts/eval_doc_recog.py` | `recog-vlm-20260925-110736.json` | clean：cell_f1 **0.804** / 结构率 **0.917** |
| **表格识别（本地 OCR 路径）** | `scripts/eval_doc_recog.py` | `recog-ocr-20260925-110255.json` | clean：cell_f1 **0.589** / 结构率 **0.750** |
| **公式识别** | `scripts/eval_doc_recog.py` | `recog-vlm-20260925-110736.json` → `formula` | 字符召回 **96.7%**，完全正确 **2/10** |
| **图表数值抽取** | `scripts/eval_doc_recog.py` | `recog-vlm-20260925-110736.json` → `chart` | 数值召回 **100%**，映射校验 **5/5** |
| **错别字校正** | `scripts/eval_correction.py` | `correction-20260924-123541.json` | 漏检率 **6.14%**，共改动 228 处 |
| **效率基准（3 种规模）** | `scripts/bench_scale.py` | `bench-20260924-102241.json` | 见下表 |

### 效率基准明细（`bench-20260924-102241.json`）

| 规模 | 页数 | 块数 | 建索引 | 每块耗时 | 检索 |
|---|---|---|---|---|---|
| S | 200 | 1 090 | 78.4 s | 71.9 ms | 1670 ms |
| M | 1 000 | 4 749 | 282.7 s | 59.5 ms | 1464 ms |
| L | 4 000 | 40 892 | 2468.9 s | 60.4 ms | 4524 ms |

---

## 二、口径说明与待重跑项

以下几处的报告与**当前代码/用例**不一致，引用数字时务必注意。
（README §8 也同步标注了这些项。）

1. **澄清报告已分两代口径，比对时务必看清** ——
   `clarify-20260924-120924.json` 是**旧口径：17 条用例 / 4 个维度 / n=7**
   （tp7/fp0/fn0/tn10，维度误报 0）；
   `clarify-20261013-152833.json` 是**现行口径：19 条（P1–P9）/ 6 个维度**
   （tp9/fp0/fn0/tn10，维度命中 9/9、**维度误报 2**）。
   后加的 P8 实体确认、P9 查询维度已于 2026-10-13 **在模型层重跑**，
   不再是"从未测量"的状态。两份**口径不同、不可直接比较**：
   用例由 17 扩到 19、维度由 4 扩到 6，多报维度的机会自然变多 ——
   旧报告的「误报 0」不构成新报告的回归。
   → 规则层对照：`clarify-20261012-235033.json`（`--rule-only`，6 维命中 9/9、误报 0）。

2. **多轮指代消解已落盘** —— `scripts/eval_multiturn.py` 原先不落盘，
   导致 README 引用的 8/8 没有产物支撑。2026-10-13 已补跑：
   `multiturn-20261013-152930.json`，baseline 3/8 → fixed 8/8。
   （脚本默认落盘，`--no-save` 可关闭。）

3. **NL2SQL 已在现行库上重跑；RAG 仍缺 Embedding 额度** ——
   `tools/rag_tool.py` 的 `INDEX_VERSION` 已由 3 升到 4（新增标题层级切块），
   旧报告的 hit@1/hit@4 是在旧切块上测的。→ 2026-10-13 实测本机
   **Embedding 账户余额不足**（HTTP 429 / code `1113 余额不足或无可用资源包`），
   向量索引建不起来，因此 **RAG 检索与 Schema Linking 的向量通道仍跑不了**；
   相关脚本会退化为「跳过检索」/「仅词法」并在日志里写明原因，属设计内降级。
   NL2SQL 不依赖 embedding，已于同日重跑：`nl2sql-20261013-152707.json`（34/34）。

4. **Schema Linking 有三份报告，评测集/口径不同** ——
   `schemalink-lexical-20261011-102209.json` 是 **15 条用例**的旧口径
   （14/15 = 93.3%）；`schemalink-lexical-20261012-232503.json` 是扩到
   **20 条**后的口径（19/20 = 95.0%）；
   `schemalink-lexical-20261013-135500.json` 是同 20 条，但**首次附带
   大小写一致性断言**（5/5），并修掉了样本值误命中（见下方二·8）；
   还带 `vector_engaged_queries` 字段，如实记录向量通道是否真的跑了
   （本次为 `0`，故 mode 标 `lexical` 而非 `lexical+vector`，见二·9）。
   三份**不可直接比较**，引用时请连同 n 一起说。
   另有一份同日过渡产物 `…133455.json` 已由本文件取代并移出目录
   —— 两者只有「多一个字段」之差，属同一状态的等价重测，无信息损失；
   其原始内容可在 commit `69d7c56` 中回溯。
   另外，词法 + 向量模式需 Embedding API Key，本轮无法执行 ——
   纯词法 95.0% 是可离线复现的下限值。

5. **RAG 的 JSON 曾缺少聚合指标** —— 早先 `answer_acc` / `faithful` 只写在 .md
   里，JSON 只有 `hit@1/hit@4/loose`，导致数字无法被脚本复读。本轮已补齐
   （含分证据类型、分域明细），但**需下次重跑后才会出现在新报告里**。

6. **`recog-20260924-*.json`（4 份）是拆分前的老格式** —— 那时表格/公式/图表
   的识别还没分成 OCR 与 VLM 两条路径，指标结构与现在不同，不要与
   `recog-ocr-*` / `recog-vlm-*` 混比。

7. **OHR-Bench 数据集不入库** —— `eval_doc_recog.py` / `eval_doc_quality.py` /
   `eval_correction.py` 依赖 `data/ohr/OHR-Bench.parquet`（公开数据集，
   7 领域 1261 份文档，体积大，**故意不纳入版本库**）。新克隆的仓库必然没有，
   需自行下载放入 `data/ohr/`，或用环境变量 `OHR_DIR` 指向别处。
   注意 `data/ohr_kb/`（195 页物化知识库）**在库内**，RAG 评测不缺数据、只缺 Key。

8. **Schema Linking 曾有一条「靠漏判侥幸正确」的用例（2026-10-13 已修）** ——
   样本值匹配原先用 `s in question` 做**大小写敏感**的子串判断，导致：
   - 同一句话 `How many employees ...` 写成小写 vs 大写，会选出**不同的表**；
   - 小写那次之所以 top1 正确，只是因为漏判了 finance 表的样本值 `"Employees"`
     —— 那是该表列名 `Employee Breakdown by Region` 的**复述（列头式标签）**，
     不是实体。换句话说，这一条是"靠 bug 侥幸答对"。
   修法（`tools/schema_linker.py`）三条，逐条实测：
   a) 样本值匹配改 `casefold`，与英文词形归一口径对齐；
   b) 新增 `_sample_is_entity()`：含中文 ≥2 字、纯拉丁 ≥4 字、纯数字不算实体
      （挡 `Invoice` 表样本值 `"MA"` 撞上英文 `how many` 的误命中）；
   c) 样本值若落在**本表自己的 schema 词**里则不计命中（修 finance 表那条）。
   另加一个**刻意取小值**的表名加成 `TABLE_NAME_GAIN=0.5`，只用于打断
   Employee 与 finance 的平局（避免靠表名字母序分先后），给足会弄坏
   「Artist 3 的专辑」这类"表名只是筛选条件"的题。
   **验证**：中文 15 条排名 0 变化、Q 类（筛选句式）0 变化、T 类 0 变化、
   留出集 14/15 → 15/15、大小写一致性 2/5 → **5/5**，准确率 19/20 不变。
   评测脚本自此**内置大小写一致性断言**（对每条英文用例跑 原句/首字母大写/
   全大写 三种写法，要求 top1 完全一致），退出码把一致性一并计入。

9. **三处「退出码 / 自述」不一致（2026-10-13 已修）** ——
   都属于「报告或退出码说了与事实不符的话」，会让核对的人得出错误结论：
   a) **`eval_schema_link.py` 向量模式静默降级还标错 mode**：
      缺 Embedding Key 时 embedding 调用失败被静默吞掉，分数与纯词法**逐项相同**，
      但报告被标成 `lexical+vector`（实测确实写出过
      `schemalink-lexical+vector-20261013-135218.json`，已删除）——
      等于交一份自述错误的证据。修法两层：①请求向量模式却缺 Key 时**开跑前**
      以退出码 2 退出，并提示可改用 `--no-vector`；②`SchemaLinker` 新增
      `vec_ok_count`（真正算出向量分的查询次数），报告的 `mode` 按**实测**写，
      并带上 `vector_engaged_queries` 字段。接口欠费/报错时也会如实标 `lexical`。
   b) **`verify_clarify_ui.py` 缺 Key 返回 1**：退出码 1 的语义是「跑完了但未达标」，
      读日志的人会以为**界面链路坏了**，实际只是没配环境。改为走 `_preflight`，
      以 2 退出（与 `verify_app` / `verify_live` 的降级口径一致）。
   c) **`build_demo_kb.py` / `prepare_ohr_kb.py` 缺数据返回 1**：
      语义应为 3（缺外部数据集）。两者还各需不同的素材文件，故
      `_preflight.require_ohr()` 扩展出 `files` 参数，**缺哪个报哪个**
      （`build_demo_kb` 要 `retrieval.zip`；`prepare_ohr_kb` 要
      `retrieval.zip` + `OHR-Bench.parquet`）。默认参数不变，
      既有 `eval_doc_recog` / `eval_doc_quality` / `eval_correction` 行为不受影响。
   **验证**：A) `eval_schema_link.py --save`（向量，无 Key）→ 退出码 **2** 且
   **不落盘**；B) `verify_clarify_ui.py` → **2**；C) `build_demo_kb.py` → **3**
   （只列 `retrieval.zip`）；D) `prepare_ohr_kb.py` → **3**（列出两个文件）；
   E) `eval_doc_recog.py --save` → **3**（默认签名未被破坏）；
   F) `eval_schema_link.py --no-vector --save` → **0**，mode 标 `lexical`、
   `vector_engaged_queries: 0`，top1 19/20、一致性 5/5 均不变。

10. **报告不记录所用模型 + 一处被它掩盖的数字口径问题（2026-10-13）** ——
   评测报告此前**不写自己是用哪个模型跑的**，于是同一指标在不同模型下的值混在
   一起、无法区分来源（README 表里既有 qwen3-max 时期的数字，也有新跑的数字）。
   这与二·9 的「报告 mode 标错」是同一类缺陷：**产物应当自述运行条件**。
   → 修法：`scripts/_preflight.py` 新增 `model_provenance()`，
   `eval_nl2sql` / `eval_clarify` / `eval_multiturn` 的报告 JSON 增加 `env`
   字段（`llm_model` / `llm_base_url` / `embedding_model`）；
   NL2SQL 的 .md 报告也加一行「模型」。
   **本批报告生成于该字段加入之前，故以 README「参考结果」上方的溯源段为准。**
   ⚠️ **该字段记的是"请求的模型名"，不是"服务的模型名"**：DeepSeek 端点会把
   别名 `deepseek-chat` 静默映射成 `deepseek-flash`（实测响应里的 `model`
   字段即为后者的名字）。因此 `.env` 一律写**显式模型 id**。
   本批三项评测实际由 **`deepseek-flash`（DeepSeek-V4.1-Flash）** 服务。
   该账号可用模型仅两个：`deepseek-flash`（输入模态含 image）、`deepseek-v4-pro`。
   另注：**DeepSeek 无 embedding 服务**（`/v1/embeddings` 实测 **404**），
   向量化必须另找提供方 —— 这也解释了为何 embedding 走的是智谱端点。
   → **附带解锁**：`deepseek-flash` 的输入模态含 **image**，实测
   `vlm_tool.recognize_table('data/demo_table.png')` 能正确读出 6 个中文列名
   与全部数值（与本地 OCR 通道结果一致）。即**多模态识别无需百炼 key**。
   注意 `eval_doc_recog --engine vlm` 的 F1 仍缺 OHR-Bench 数据，无法重跑。
   → 顺带纠正一处**因口径变更而失真的数字**：README 原写「澄清 维度误报数 **0**」，
   该值出自 **17 条 / 4 维**的旧报告；在 **19 条 / 6 维**的现行口径下实测为 **2**
   （`clarify-20261013-152833.json` 的 `dimensions.false`）。
   已把 README 该行改为 **2** 并注明两个口径，避免读者把「口径扩张」误读成「质量回退」。
   F1 / 误澄清率不受影响（仍为 100% / 0%）。
   → 附带清理：`eval_nl2sql.py` 有一处重复的 `from config import Config`。

---

## 二·补：评测脚本的退出码约定

上一轮起，所有 `eval_*.py` 在「没配齐环境」时**不再崩栈**，而是打印可操作提示后
按约定退出（公共逻辑见 `scripts/_preflight.py`）：

| 退出码 | 含义 | 触发条件 |
|---|---|---|
| 0 | 正常完成（达标与否见各脚本结论） | — |
| 1 | 正常完成但**未达标** | 跑完了，指标没到阈值 |
| 2 | **缺少 API Key** | 需 LLM/Embedding，配 `.env` 后重跑 |
| 3 | **缺少外部数据集** | 缺 OHR-Bench（`OHR-Bench.parquet` / `retrieval.zip`），配 `OHR_DIR` 后重跑 |
| 10 / 11 | **降级完成**（澄清专用）：只跑了规则层，达标 / 未达标 | 缺 Key 时 `eval_clarify` 自动降级 |

这样一键脚本 / CI 能区分「代码坏了（其它非零）」与「环境没配（2/3）」，
以及「跑的是降级版（10/11）」。
其中 `eval_clarify.py` 支持 `--rule-only`：完全不调模型，只跑规则层对照，
是**无 Key 环境下唯一能产出的澄清证据**（已被本索引纳入权威表）。

---

## 三、如何复现

```bash
# 离线可跑（无需 API Key）
python scripts/init_runtime.py --force
python scripts/eval_schema_link.py --no-vector --save
python scripts/eval_extensible_schema.py
python scripts/eval_complexity_plan.py --no-net
python scripts/eval_clarify.py --rule-only --save      # 规则层对照（离线）
python scripts/verify_live.py
python scripts/verify_app.py

# 需要 API Key（写入 .env 后执行；缺 Key 会以退出码 2 优雅退出）
python scripts/eval_nl2sql.py --save
python scripts/eval_rag.py --save
python scripts/eval_clarify.py --save                  # 模型层 F1/闭环
python scripts/eval_multiturn.py
python scripts/eval_schema_link.py --save              # 词法 + 向量（缺 Key 需改用 --no-vector）
python scripts/verify_search_equiv.py                  # 检索等价性（需真实调 embedding）
python scripts/verify_clarify_ui.py                    # 澄清按钮「可点且能接着答」（需真实作答）

# 需要 OHR 素材（缺数据会以退出码 3 优雅退出）
python scripts/build_demo_kb.py                        # 物化演示知识库（需 retrieval.zip）
python scripts/prepare_ohr_kb.py                       # 物化评测知识库（需 retrieval.zip + parquet）
python scripts/eval_doc_recog.py --save
python scripts/eval_correction.py --save
python scripts/eval_doc_quality.py --save
```
