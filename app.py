# -*- coding: utf-8 -*-
import hashlib
import importlib
import json
import os
import re
import sys
import uuid

import pandas as pd
import streamlit as st

from config import Config
from tools import schema_linker as SL
from tools import image_tool
from tools import doc_quality
from tools import explain
from tools import vlm_tool

import agent.graph  # noqa: F401  —— 走模块属性访问，方便热重载

# 会被热重载的模块。顺序很重要：被依赖的在前，agent.graph 在最后。
# config 必须排第一 —— 它的类属性被下游各模块以 `from config import Config`
# 绑定，只有下游模块也被重载，它们才会重新绑定到新的 Config 类。
# image_tool / rag_tool 都是懒加载（引擎在函数内建、向量库在实例内建），
# reload 不会顺带重建索引，故可安全纳入。
RELOADABLE = ("config", "tools.db_builder", "tools.file_utils", "tools.rag_tool",
              "tools.schema_linker", "tools.sql_tool", "tools.image_tool",
              "tools.vlm_tool", "tools.doc_quality", "tools.explain",
              "agent.state", "agent.clarify", "agent.graph")

st.set_page_config(
    page_title="精准问数/问答智能体",
    page_icon="🤖",
    layout="wide"
)

st.title("🤖 多模态数据驱动的可解释精准问数/问答智能体")
st.markdown("> 赛题八 · 中电云计算技术有限公司")
st.markdown("---")

def hot_reload() -> None:
    """就地重载项目模块，让改过的 .py 立刻生效。

    两个坑：
    1. @st.cache_resource 会一直复用旧的 AgentGraph 实例，函数体不变缓存就不失效；
    2. 即便清了缓存，`from agent.graph import AgentGraph` 也早把旧类绑死在 app.py 的
       命名空间里 —— 所以必须 importlib.reload，且 reload 后要用模块属性取类。
    """
    for name in RELOADABLE:
        mod = sys.modules.get(name)
        if mod is None:
            continue
        try:
            importlib.reload(mod)
        except Exception as e:               # 语法错误等：保留旧模块，别把页面打崩
            print(f"[hot_reload] {name} 重载失败：{type(e).__name__}: {e}")


@st.cache_resource
def get_agent():
    # 用模块属性而不是 import 进来的名字，否则热重载后还是旧类
    return sys.modules["agent.graph"].AgentGraph()

# 启动自检：缺 Key 时**不能**整页 traceback —— 页面必须自己说清缺了哪个 Key。
# 两层保护：
#   ① 先做不耗时的静态自检（Config.missing_keys），报出精确的变量名；
#   ② 再用 try 兜住其余初始化失败（依赖缺失、库损坏、网络不通）。
# 两种情况都渲染成可读提示，而不是把用户丢给一段堆栈。
agent = None
_missing_keys = Config.missing_keys()
if _missing_keys:
    st.error(
        "❌ 缺少必要的 API Key：" + "、".join(f"`{k}`" for k in _missing_keys)
        + "\n\n请在项目根目录创建 `.env` 并补齐上述变量（模板见 `.env.example`），"
          "然后重启应用。\n\n（页面其余部分可正常浏览，但提问功能不可用。）"
    )
else:
    try:
        with st.spinner("正在初始化智能体，加载文档索引..."):
            agent = get_agent()
        st.success("✅ 智能体就绪")
    except Exception as e:
        st.error(
            f"❌ 智能体初始化失败：`{type(e).__name__}: {e}`\n\n"
            "常见原因：API Key 无效或额度不足、网络不通、依赖未装全"
            "（可先执行 `pip install -r requirements.txt`）。"
        )

# ========== 图片导入（OCR） ==========
UPLOAD_DIR = os.path.join(Config.BASE_DIR, "data", "uploads")
# 文档处理策略清单（难题 8）。刻意放在 rag_data **之外**：
# 那个目录下所有文件都会进检索索引的「源文件指纹」，策略一变就全量重建。
PLAN_FILE = os.path.join(Config.BASE_DIR, "data", "doc_plans.json")


def save_doc_plan(fname: str, plan: dict) -> str:
    """把处理策略落盘，交给检索侧（RAGTool）消费。

    这是「复杂度评估**指导后续处理策略**」真正生效的地方 —— 写进去之后，
    这份文档会按自己的策略切块建索引、命中时按自己的策略放宽召回宽度。
    写失败只返回一句说明、不抛异常：策略是优化项，不该让入库失败。
    """
    try:
        os.makedirs(os.path.dirname(PLAN_FILE), exist_ok=True)
        data = {}
        if os.path.isfile(PLAN_FILE):
            with open(PLAN_FILE, encoding="utf-8") as f:
                data = json.load(f) or {}
        data[fname] = plan
        with open(PLAN_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return ""
    except Exception as e:
        return f"（策略落盘失败，将按默认参数处理：{type(e).__name__}: {e}）"

if flash := st.session_state.pop("ocr_flash", None):
    st.success(flash)

with st.expander("📷 从图片导入数据表（OCR 识别）", expanded=False):
    st.caption(
        "支持 Excel / 系统界面截图、纸质表格照片。识别后重建为**结构化表**并入库 —— "
        "之后就能像问数据库一样直接提问（排名、聚合、精确值），而不只是全文检索。"
    )
    # 引擎标签带真实模型名 —— 之前这里写死 "qwen-vl-max"，一旦 .env 换成别的
    # 多模态模型（如 deepseek-flash），界面就在自述一个并未使用的模型名。
    _vlm_name = getattr(Config, "VLM_MODEL", "") or "VLM"
    engine_label = st.radio(
        "识别引擎",
        [f"VLM（{_vlm_name}）", "本地 OCR（RapidOCR）"],
        horizontal=True,
        key="ocr_engine",
        help="VLM 在倾斜 / 模糊 / 公式 / 图表上明显更强（实测单元格召回 61.3%→81.1%、"
             "公式字符召回 28.2%→96.7%）；本地 OCR 离线可用、不消耗额度。",
    )
    use_vlm = engine_label.startswith("VLM")
    if use_vlm and not vlm_tool.available():
        st.info("VLM 未配置（缺 VLM_API_KEY / VLM_MODEL），本次自动改用本地 OCR。")
        use_vlm = False

    up = st.file_uploader("选择图片", type=["png", "jpg", "jpeg", "bmp", "webp"], key="ocr_upload")

    if up is not None:
        raw = up.getvalue()
        # 引擎也要进指纹：切换引擎必须重新识别，
        # 否则会直接命中上一次的缓存结果，看起来像「换了引擎没变化」。
        digest = hashlib.md5(raw + engine_label.encode("utf-8")).hexdigest()
        if st.session_state.get("ocr_digest") != digest:
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            safe = re.sub(r"[^\w\u4e00-\u9fff.\-]", "_", up.name)
            dest = os.path.join(UPLOAD_DIR, f"{digest[:8]}_{safe}")
            with open(dest, "wb") as fh:
                fh.write(raw)
            try:
                tip = (f"正在调用 {_vlm_name} 识别（约 5~15 秒）..." if use_vlm
                       else "正在本地识别并重建表格（首次运行需加载 OCR 模型，约 3~10 秒）...")
                with st.spinner(tip):
                    res = (vlm_tool.recognize_table(dest) if use_vlm
                           else image_tool.ocr_to_table(dest))
                    # 只有本地 OCR 分区框，VLM 是整图理解、不产出字符坐标；
                    # cells 为空时下游自动回落到展示原图，这里无需特判。
                    if res.get("cells"):
                        ann = os.path.join(UPLOAD_DIR, f"{digest[:8]}_annotated.png")
                        image_tool.render_annotated(dest, res["cells"]).save(ann)
                        res["_annotated"] = ann
                res["_path"] = dest
                res["_digest"] = digest
                res["_name"] = os.path.splitext(up.name)[0]
                # 难题 9：入库前先做质量综合评估。
                # 低质量扫描件（颠倒/倾斜/模糊）识别出来的错值一旦进索引，
                # 后面所有问答都会基于错数据 —— 报错比静默入库安全得多。
                try:
                    res["_quality"] = doc_quality.assess_document(dest)
                except Exception as qe:
                    res["_quality"] = {"quality_score": None,
                                       "issues": [f"质量检测失败：{type(qe).__name__}: {qe}"]}
                # 难题 8：对**已解析出的文本**评估复杂度并产出处理策略。
                # 与难题 9 分工明确 —— 质检管「解析前的预处理」（纠偏/放大/换件），
                # 复杂度管「解析后的切片与检索」。混在一起会得到一份既说不清依据、
                # 也无法复现的分数。
                try:
                    res["_plan"] = doc_quality.plan_for_text(res.get("text") or "")
                except Exception as pe:
                    res["_plan"] = {}
            except Exception as e:
                res = {"error": f"{type(e).__name__}: {e}"}
            st.session_state["ocr_digest"] = digest
            st.session_state["ocr_result"] = res

        res = st.session_state.get("ocr_result") or {}
        if res.get("error"):
            st.error(f"识别失败：{res['error']}")
        else:
            for w in res.get("warnings", []):
                st.warning(w)

            left, right = st.columns([3, 2])
            with left:
                if res.get("cells"):
                    st.markdown("**识别标注图**")
                    st.caption("绿框 = 置信度高；黄框 = 置信度偏低，建议人工核对")
                else:
                    st.markdown("**原图**")
                    st.caption("VLM 是整图理解，不产出字符级坐标框；"
                               "需要逐框核对置信度时请切到本地 OCR。")
                st.image(res.get("_annotated") or res["_path"])
            with right:
                ms = res.get("mean_score")
                if ms is not None:
                    st.metric("平均置信度", f"{ms:.3f}")
                else:
                    st.metric("识别引擎", "VLM" if res.get("engine") == "vlm" else "本地 OCR")
                gdf = res.get("dataframe")
                if gdf is not None:
                    st.metric("重建结果", f"{len(gdf)} 行 × {len(gdf.columns)} 列")
                # 难题 9 的可解释展示：不仅给分，还要说清「哪一条规则触发、建议怎么处置」
                q = res.get("_quality") or {}
                if q.get("quality_score") is not None:
                    st.metric("文档质量分", f"{q['quality_score']:.0f}",
                              q.get("quality_tier"))
                    for iss in q.get("issues", []):
                        st.warning(iss)
                    if q.get("suggested_actions"):
                        st.caption("处置建议：" + "；".join(q["suggested_actions"]))
                else:
                    for iss in q.get("issues", []):
                        st.info(iss)

                # 难题 8 的可解释展示：复杂度分 + **它导出的处理策略**。
                # 只报分数是「预估难度」，把策略摊开才是「指导后续处理策略」——
                # 而且这份策略会被落盘、被检索侧真正消费（见 save_doc_plan）。
                plan = res.get("_plan") or {}
                if plan:
                    st.metric("文档复杂度", f"{plan.get('complexity_score', 0):.0f}",
                              plan.get("complexity_tier"))
                    if plan.get("triggers"):
                        st.caption("复杂度触发项：" + "、".join(plan["triggers"]))
                    with st.expander("🧭 处理策略（将指导切片与检索）", expanded=False):
                        st.markdown(
                            f"- 解析通道：`{plan.get('parse_path')}`\n"
                            f"- 切块粒度：`{plan.get('chunk_size')} / 重叠 {plan.get('chunk_overlap')}`\n"
                            f"- 检索宽度：`top_k={plan.get('top_k')}`")
                        for a in plan.get("actions", []):
                            st.markdown(f"- {a}")
                        st.caption("入库后这份策略会被检索侧读取并生效：该文档按自己的"
                                   "切块参数建索引，命中时按自己的宽度放宽召回。")

            if gdf is not None:
                st.markdown("**重建出的表格**（核对无误后再入库）")
                st.dataframe(gdf, hide_index=True, width="stretch")

                name = st.text_input("入库名称（将作为表名）",
                                     value=res.get("_name") or "导入表",
                                     key=f"ocr_tname_{res.get('_digest', '')}")
                if st.button("💾 保存到知识库并重建索引", type="primary"):
                    out = os.path.join(Config.RAG_DATA_DIR, f"{name}.xlsx")
                    gdf.to_excel(out, index=False, sheet_name="Sheet0")
                    # 策略必须在清缓存**之前**落盘：RAGTool 重建索引时会读它，
                    # 顺序反了就会用默认参数建索引，策略看起来「写了但没生效」
                    plan_note = save_doc_plan(f"{name}.xlsx", res.get("_plan") or {})
                    get_agent.clear()
                    st.session_state.pop("ocr_digest", None)
                    st.session_state.pop("ocr_result", None)
                    tier = (res.get("_plan") or {}).get("complexity_tier")
                    st.session_state["ocr_flash"] = (
                        f"已写入 `rag_data/{name}.xlsx`，向量索引与问数库会自动重建，"
                        f"现在可以直接在下方提问了。"
                        + (f"（已按复杂度「{tier}」落盘处理策略，检索侧会据此切片与召回）"
                           if tier else "")
                        + plan_note
                    )
                    st.rerun()
            else:
                st.info("这张图片没有识别出表格结构，已按纯文本处理。")

            with st.expander("识别出的全部文字"):
                st.text(res.get("text", "")[:4000])

# ========== 侧边栏 ==========
with st.sidebar:
    st.header("⚙️ 设置")

    if "thread_id" not in st.session_state:
        st.session_state.thread_id = str(uuid.uuid4())

    st.info(f"会话: {st.session_state.thread_id[:8]}...")

    if st.button("🔄 新建会话"):
        st.session_state.thread_id = str(uuid.uuid4())
        st.session_state.messages = []
        st.rerun()

    # 改了 agent/ 或 tools/ 下的 .py 之后，必须走这里，否则页面跑的还是启动时那份代码
    if st.button("♻️ 重载代码", help="改完 .py 后点一下，无需重启服务"):
        get_agent.clear()
        hot_reload()
        st.session_state["reload_flash"] = "已重载 agent/ 与 tools/ 下的模块，缓存已清空。"
        st.rerun()

    if flash := st.session_state.pop("reload_flash", None):
        st.success(flash)

    st.divider()

    st.markdown("### 数据库信息")
    try:
        if agent is None:
            raise RuntimeError("智能体未就绪（缺少 API Key）")
        schema = agent.sql_tool.get_schema()
        with st.expander(f"📊 Schema（{len(schema)} 字符）", expanded=False):
            st.code(schema, language="sql")
        # Schema Linking 的状态：表一多就会走检索，这里说明「本次给了哪几张表」
        # 以及截断限额，避免出现「明明库里有表却答不了」时无从排查
        lk = agent.sql_tool.linker
        tables = lk.list_tables()
        with st.expander(f"🔎 Schema Linking（{len(tables)} 张表）", expanded=False):
            st.caption(
                "提问时按相关问题筛表，只把选中的表塞进提示词。"
                f"当前截断限额 {SL.FULL_SCHEMA_LIMIT} 字符，"
                f"全量小于该值则直接用全量。"
            )
            st.write("库中的表：")
            st.code("、".join(str(t) for t in tables), language=None)
    except Exception as e:
        st.warning(f"数据库未就绪: {e}")

    # RAG 索引状态：降级时必须显式告知，否则用户会把「检索不可用」误当成「资料里没有」，
    # 进而误信一个只看 SQL 的答案
    try:
        if agent is None:
            raise RuntimeError("智能体未就绪（缺少 API Key）")
        _rag = agent.rag
        if getattr(_rag, "degraded_reason", ""):
            st.warning("⚠️ 文档检索已降级：向量索引不可用\n\n"
                       f"`{_rag.degraded_reason[:160]}`\n\n"
                       "问数（SQL）功能不受影响。")
        else:
            st.caption(f"📚 文档索引：{_rag.vector_store._collection.count()} 个片段")
    except Exception as e:
        st.caption(f"📚 文档索引状态未知：{e}")

    st.divider()

    st.markdown("""
    ### 💡 示例问题

    **SQL 问数类（综测数据表 / chinook 库）：**
    - 智育测评分前十是谁？
    - 一共有多少人参评？
    - 综合素质测评成绩排名第一的是谁？
    - 每个行政班的平均综合素质测评成绩是多少？
    - 一共有多少位客户？（chinook 库）

    **RAG 问答类：**
    - 贾淑兰的德育测评分是多少？
    - 学号 2025900111 的学生叫什么？

    **Hybrid 融合类：**
    - 查询客户数最多的国家，并总结这组数据能说明什么
    """)

# ========== 来源展示（实时回答与历史消息共用） ==========
def html_block(markup: str) -> None:
    """渲染一段已转义的 HTML。

    为什么不用 st.markdown：片段里大量是 LaTeX（\\begin{tabular}、$...$）与
    表格竖线，走 Markdown 会被 KaTeX 与表格语法吃掉，用户看到的就不是原始证据了。
    st.html 不做 Markdown 解释，正是这里需要的保真语义；老版本没有它时退回
    markdown + unsafe_allow_html，最坏情况只是丢样式，不影响文字与高亮。
    """
    if hasattr(st, "html"):
        st.html(markup)
    else:
        st.markdown(markup, unsafe_allow_html=True)


def _highlight_vocab() -> list:
    """高亮用的实体/指标词表，取自真实 schema（表名 + 列名 + 样本值）。

    取不到就退回空表 —— 高亮会退化成「只用问题里的词」，功能不消失。
    """
    try:
        return agent.sql_tool.linker.vocabulary()
    except Exception:
        return []


def render_sources(result: dict) -> None:
    """把一次问答的「依据」全部摊开：SQL 语句与结果、引用文档片段、推理过程。

    抽成函数是为了让**历史消息**也带来源 —— 原来 messages 里只存纯文本，
    页面一 rerun 就只剩答案，引用片段全丢了，等于没有可解释性。
    """
    # ── 多轮：指代消解结果 ──
    # 「多轮对话上下文保持」最直接的可解释证据：让用户看到系统把
    # 「那他的德育测评分呢」理解成了什么。答偏时能一眼归因到消解这一步，
    # 而不是只看到一个莫名其妙的答案。
    if result.get("rewritten"):
        st.info(
            f"🔗 **多轮指代消解**：结合上文把 `{result.get('question')}` "
            f"补全为 `{result.get('resolved_question')}`"
        )

    # ── 澄清依据：把「缺了哪个维度」摊开 ──
    # 难题 3 要求的是「主动澄清补全信息」，而不是让用户猜系统在问什么。
    # 这里显式列出缺失维度 + 判定来源（模型判断 / 规则兜底），澄清才是可审计的。
    if result.get("clarification"):
        dims = result.get("missing_dimensions") or []
        if dims:
            src = "规则兜底" if result.get("clarify_by_rule") else "语义校验"
            st.caption(f"🧭 判定缺失维度：**{'、'.join(dims)}**（依据：{src}）")

    # ── SQL 语句与查询结果 ──
    if result.get("sql"):
        with st.expander("💻 生成的 SQL 与查询结果", expanded=bool(result.get("sql_result"))):
            st.code(result["sql"], language="sql")
            sql_res = result.get("sql_result")
            if sql_res is not None:
                df = None
                if isinstance(sql_res, pd.DataFrame):
                    df = sql_res
                elif isinstance(sql_res, str):
                    # 兜底：万一拿到的是 SQLDatabase.run() 的字符串结果
                    try:
                        import ast
                        parsed = ast.literal_eval(sql_res) if sql_res.strip() else []
                        if parsed and isinstance(parsed[0], dict):
                            df = pd.DataFrame(parsed)
                        elif parsed:
                            cols = result.get("sql_columns") or None
                            if cols and len(cols) != len(parsed[0]):
                                cols = None
                            df = pd.DataFrame(parsed, columns=cols)
                    except Exception:
                        df = None
                elif isinstance(sql_res, (list, tuple)) and len(sql_res) > 0:
                    if isinstance(sql_res[0], dict):
                        df = pd.DataFrame(sql_res)
                    else:
                        cols = result.get("sql_columns") or None
                        if cols and len(cols) != len(sql_res[0]):
                            cols = None
                        df = pd.DataFrame(sql_res, columns=cols)

                if df is not None:
                    if len(df) > 0:
                        st.markdown(f"**查询结果** ({len(df)} 行):")
                        st.dataframe(df, width="stretch", hide_index=True)
                    else:
                        st.info("查询执行成功，但没有匹配的数据（0 行）")
                elif isinstance(sql_res, list) and len(sql_res) == 0:
                    st.info("查询执行成功，但没有匹配的数据（0 行）")
                else:
                    st.markdown("**查询结果:**")
                    st.write(sql_res)
            if result.get("sql_error"):
                st.error(f"SQL 错误: {result['sql_error']}")

    # ── 引用文档片段：无论什么意图都渲染 ──
    # 检索现在对所有问题都会跑，所以这里一定拿得到内容；
    # 真的一无所获时也要明确说「没有」，而不是整块消失（消失等于来源缺失不可见）
    chunks = result.get("retrieved_chunks") or []
    with st.expander(f"📋 引用文档片段（{len(chunks)}）", expanded=False):
        if not chunks:
            st.info("本次未检索到相关文档片段。")
        else:
            intent = result.get("intent")
            if intent == "sql":
                st.caption("本次回答主要依据 SQL 查询结果，以下为同时检索到的相关文档，供核对参考。")
            # 高亮词表：本轮（消解后的）问题 + 真实 schema 里的实体/指标。
            # 用消解后的问题而不是原问题 —— 「那他的德育测评分呢」高亮「他」毫无意义，
            # 要核对的是消解出来的那个人名。
            terms = explain.query_terms(
                result.get("resolved_question") or result.get("question") or "",
                _highlight_vocab())
            st.caption("🖍 黄色为与提问直接对应的词（实体 / 指标 / 编号），"
                       "原文逐字保真，便于核对。")
            for i, chunk in enumerate(chunks):
                score = chunk.get("score")
                score_txt = f"{score:.3f}" if isinstance(score, (int, float)) else "—"
                match_txt = " · 精确匹配" if chunk.get("match") == "exact" else ""
                # 文档复杂度（难题 8）随片段回传：对复杂文档检索侧已按策略放大召回宽度
                cx = chunk.get("complexity")
                cx_txt = f" · 复杂度 `{cx}`" if cx else ""
                body, hits = explain.snippet_html(chunk.get("text") or "", terms)
                head = (f"**片段 {i+1}** | `{chunk.get('source', '未知来源')}` | "
                        f"相关度: `{score_txt}`{match_txt}{cx_txt}")
                if hits:
                    head += f"　命中：`{'、'.join(dict.fromkeys(hits))}`"
                st.markdown(head)
                html_block(body)
                st.divider()

    # ── 推理过程 ──
    if result.get("trace"):
        with st.expander("🧠 推理过程", expanded=False):
            for step in result.get("trace", []):
                st.markdown(step)


# ========== 主界面 ==========
if "messages" not in st.session_state:
    st.session_state.messages = []

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        # 历史回答同样回放来源，否则 rerun 一次证据就没了
        if msg.get("result"):
            render_sources(msg["result"])

def _pick_clarify_option(text: str) -> None:
    """把澄清选项变成「用户说的一句话」。

    刻意**不另开旁路**：点按钮与手打走的是同一条链路（同样写入 messages、
    同样带上历史、同样过指代消解）。如果按钮走旁路，多轮上下文就会出现
    「点选的和打字的待遇不一样」这种只在演示时才会暴露的怪问题。
    """
    st.session_state["pending_q"] = text


# 澄清选项点下的内容，与用户打字同样是「本轮提问」
pending_q = st.session_state.pop("pending_q", None)
typed = st.chat_input("请输入您的问题...")
question = typed or pending_q

if question:
    st.chat_message("user").markdown(question)

    # 智能体没起来（缺 Key / 初始化失败）时给出明确指引，而不是让 agent.run 抛异常
    if agent is None:
        st.error("智能体未就绪：请在项目根目录 `.env` 中配置所需的 API Key，"
                 "然后重启应用后再提问。")
        st.stop()

    # 对话历史必须在 append 当前提问**之前**取：
    # 若先 append，当前问题会同时出现在 history 与 question 里，
    # 指代消解那一步容易被诱导成「照着重复一遍」而不是补全指代。
    history = [{"role": m["role"], "content": m["content"],
                **({"clarification": True} if m.get("clarification") else {})}
               for m in st.session_state.messages]
    st.session_state.messages.append({"role": "user", "content": question})

    with st.chat_message("assistant"):
        with st.spinner("🤔 思考中..."):
            result = agent.run(question, st.session_state.thread_id, history=history)

        # 意图
        intent_emoji = {"sql": "💻 问数", "rag": "📚 问答", "hybrid": "🔗 融合查询"}
        st.caption(f"意图: {intent_emoji.get(result.get('intent', ''), '❓')}"
                   + ("　🔗 已消解指代" if result.get("rewritten") else "")
                   + ("　❓ 已发起澄清" if result.get("clarification") else ""))

        if result.get("clarification"):
            # 澄清是一种**交互**而不是答案：用醒目的提示块，并说明用户可以怎么回
            st.warning(result["final_answer"])
            opts = [str(o) for o in (result.get("clarify_options") or []) if str(o).strip()]
            if opts:
                # 选项必须**可点**：只给一行文字等于让用户自己重打一遍，
                # 那不是「引导」而是把问题踢回给用户（赛题决赛把「澄清精准且体验好」
                # 与「主动澄清」分成 10 分与 7 分两档，差的就是这一下点击）。
                st.markdown("**点一下即可直接回复：**")
                cols = st.columns(min(len(opts), 4))
                for i, opt in enumerate(opts):
                    with cols[i % len(cols)]:
                        st.button(opt, key=f"clar_{i}_{abs(hash(opt)) % 99999}",
                                  on_click=_pick_clarify_option, args=(opt,),
                                  use_container_width=True)
                st.caption("也可以直接输入自己的补充说明 —— 系统会接着上文继续。")
            else:
                st.caption("直接回复补充条件即可 —— 系统会接着上文继续。")
        else:
            st.markdown("### 📊 回答")
            st.markdown(result["final_answer"])

        render_sources(result)

    st.session_state.messages.append({
        "role": "assistant",
        "content": result["final_answer"],
        "result": result,          # 存整份结果，历史回放才有来源可展示
        # 标记澄清轮：下一轮消解时要靠它把用户的碎片式补充合并回原始问题
        "clarification": bool(result.get("clarification")),
    })