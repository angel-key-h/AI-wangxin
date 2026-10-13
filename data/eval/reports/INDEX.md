# 评测报告索引

本目录保存全部评测的**原始产物**，文件名格式为 `<族>-<YYYYMMDD-HHMMSS>.{json,md}`。

同一族出现多份是正常的：开发期反复重跑会各自留档。**这些历史文件不删除**——
它们构成可追溯的证据链（能看出某个指标是怎么一步步调上来的）。但核对数字时，
请以本索引指定的**权威产物**为准，不要随手取目录里最后修改的那份。

---

## 一、权威产物对照表

| 指标 | 生成脚本 | 权威产物 | 实测值 |
|---|---|---|---|
| **NL2SQL 准确率** | `scripts/eval_nl2sql.py` | `nl2sql-20260925-105615.json` | **34/34 = 100%** |
| **RAG 检索命中率** | `scripts/eval_rag.py` | `rag-20260925-111245.json` | hit@1 **84.8%** / hit@4 **95.5%** / 宽松匹配 **71.2%**（n=66） |
| RAG 答案准确率 / 忠实度 | `scripts/eval_rag.py` | `rag-20260925-111245.md` | 答案准确率 **84.8%** / 忠实度 **98.5%**（LLM 裁判，n=66） |
| **澄清 精确/召回/F1** | `scripts/eval_clarify.py` | `clarify-20260924-120924.json` | **100% / 100% / 100%**，误澄清率 **0%**（tp7/fp0/fn0/tn10）⚠️ 见二·1 |
| **澄清 · 规则层（离线可复现）** | `scripts/eval_clarify.py --rule-only --save` | `clarify-20261012-235033.json`（mode=rule-only） | 规则层维度命中 **9/9 = 100%**（6 维全覆盖，n=9 正例） |
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

1. **澄清报告是旧口径** —— `clarify-20260924-120924.json` 记录的是
   **17 条用例 / 4 个维度 / n=7**，而用例文件 `data/eval/clarify_cases.json`
   现已扩到 **19 条（P1–P9）/ 6 个维度**。后加的 P8 实体确认、P9 查询维度
   **从未重跑**（模型层口径）。
   → **规则层已离线补齐**：`clarify-20261012-235033.json`（`--rule-only`）
   覆盖全部 19 条用例的规则层判定，6 维命中 9/9。模型层（F1/闭环）仍需 Key。
   → 模型层 **待重跑（需 API Key）**。

2. **多轮指代消解无报告** —— `scripts/eval_multiturn.py` 原先不落盘。
   本轮已补上默认落盘，但尚未重跑。→ **待重跑（需 API Key）**

3. **RAG / NL2SQL 的检索指标对应旧切块逻辑** —— `tools/rag_tool.py` 的
   `INDEX_VERSION` 已由 3 升到 4（新增标题层级切块），旧报告的 hit@1/hit@4
   是在旧切块上测的。→ **待重跑（需 API Key）**

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
