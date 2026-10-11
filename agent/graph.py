# -*- coding: utf-8 -*-
"""
Agent 主图

流程：resolve → classify → retrieve → (sql →) generate

三个设计要点：

1. **文档检索是必经节点**，不再只服务 rag 意图 —— 只有检索对所有问题都跑，
   「所有问答结果」才都拿得出引用片段、来源展示才不断档（赛题基础任务要求）。
   意图只决定要不要**再**查一次库。

2. **首节点做指代消解（resolve）**。LangGraph 的 checkpointer 只保存 state，
   并不会把历轮提问喂给 LLM；「那他的德育测评分呢」里的「他」如果没人翻译，
   模型只能从 schema 的样例行里挑个名字填坑 —— 实测会稳定地张冠李戴。
   所以这里显式把历史对话带进来做改写，下游全部改用改写后的自足问题。

3. **改写只在真的有指代/省略时才做**（见 _needs_context）：大多数提问本身自足，
   走快路径不额外花 LLM 调用，端到端延迟不受影响。
"""
import json
import re

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langchain_openai import ChatOpenAI
from agent.state import AgentState
from agent import clarify
from tools.rag_tool import RAGTool, build_context
from tools.sql_tool import SQLTool
from config import Config

# ── 指代消解的门控与上下文预算 ──
# 代词与「那/这」类指示词：出现即说明可能有指代
_PRONOUN = re.compile(
    r"(他们|她们|它们|他|她|它|其|该|此|这些|那些|这|那|上述|前者|后者|本人|该生|此人)")
# 句首的承接词：省略主语或宾语的典型信号
_LEADING = re.compile(r"^(那|那么|还有|另外|然后|再|同样|继续|所以|因此)")
# 「呢」是中文里最强的省略标记（"那第二名呢？" / "平均分呢？"）
_ELLIPSIS = "呢"
# 比较 / 排名类提问在**没带指标名**时同样依赖上文：
# 「排在第一名的是谁？」「最高的是哪个班？」单看都缺「按哪个指标」，
# 而上一轮通常刚说过 —— 用户认为这是显然的，系统不该反过来问。
# 这类问题一个代词都没有（没有「他/那/呢」），代词门控永远命中不了。
# 实测后果是一条链式破坏：resolve 不改写 → 语义校验判「未指明指标」→ 发起澄清
# → 该轮助手消息里没有正确实体 → 下一轮「这两个人相差多少分」只能从更早的轮次
# 取实体，于是张冠李戴（本该是刚回答的第一/第二名）。**一处漏判毁掉两轮。**
_COMPARATIVE = re.compile(
    r"(第[一二三四五六七八九十0-9]+|前[一二三四五六七八九十0-9]+|"
    r"最高|最低|最大|最小|最多|最少|最好|最差|排名|排第|倒数)")

MAX_HISTORY_MSGS = 8         # 最多回看 4 轮（轮 = 用户+助手各一条）
HISTORY_ANSWER_CHARS = 300   # 助手答案的截断长度：提示词已要求「先给结论」，实体都在开头
_RESOLVE_MAX_CHARS = 200     # 改写结果长度上限，超过视为模型跑偏，退回原问题


def _needs_context(question: str, history: list) -> bool:
    """判断这一问题是否依赖上文。

    宁缺毋滥的反面是「漏判」——漏判会退回原来的瞎猜行为，代价是答错；
    误判只是多一次 LLM 调用，代价是 ~0.5s。所以判据故意放宽一点，
    但**不放宽到「短问题就改写」**：`一共有多少人参评？` 这类自足短问
    会被误伤，白白增加延迟。
    """
    if not history:
        return False
    q = (question or "").strip()
    if not q:
        return False
    # 上一轮助手是在向用户澄清 → 用户这一轮是在补信息，**必须**与澄清前的原始问题合并。
    # 这类补充往往是「按智育测评分」「2112503 班」这种不含任何代词或承接词的碎片，
    # 代词门控永远命中不了 —— 实测后果是反复澄清、永远进不了作答（死循环）。
    if history[-1].get("clarification"):
        return True
    if (_ELLIPSIS in q or _PRONOUN.search(q) or _LEADING.match(q)
            or _COMPARATIVE.search(q)):
        return True
    return False


def _format_history(history: list, max_msgs: int = MAX_HISTORY_MSGS) -> str:
    """把历史对话压成一段纯文本。助手答案只保留开头 —— 按提示词约定，
    结论与涉及的实体（人名/数值）都在最前面。"""
    if not history:
        return ""
    lines = []
    for m in history[-max_msgs:]:
        role = "用户" if m.get("role") == "user" else "助手"
        if role == "助手" and m.get("clarification"):
            # 显式标注，消解节点才知道「上一轮问过什么」，才能把用户的补充合并回去
            role = "助手（澄清提问）"
        content = (m.get("content") or "").strip().replace("\n", " ")
        if role.startswith("助手"):
            content = content[:HISTORY_ANSWER_CHARS]
        if content:
            lines.append(f"{role}：{content}")
    return "\n".join(lines)


def _parse_json(raw: str) -> dict:
    """从 LLM 输出里抠出 JSON 对象；抠不出来返回空 dict（调用方按「不澄清」降级）"""
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


class AgentGraph:
    def __init__(self):
        # 先给出「缺哪一个环境变量」的明确报错。直接构造 ChatOpenAI 时，
        # api_key 为空只会抛一句带 "api_key" 字样的校验异常，看不出该去配哪一项；
        # 调用方（页面 / 脚本）捕获后可以原样展示这条可操作的提示。
        missing = Config.missing_keys()
        if missing:
            raise RuntimeError(
                "缺少必要的 API Key：" + "、".join(missing)
                + "。请在项目根目录 .env 中配置（模板见 .env.example）")
        self.llm = ChatOpenAI(
            model=Config.LLM_MODEL,
            api_key=Config.LLM_API_KEY,
            base_url=Config.LLM_BASE_URL,
            temperature=0
        )
        self.rag = RAGTool()
        self.sql_tool = SQLTool()
        self._vocab_cache = None
        self._ent_cache = None
        self.graph = self._build_graph()

    def _vocab(self) -> list:
        """库中的实体/指标词表（表名+列名+样本值），缓存一次。

        两处用它，都是为了让判断依据来自真实 schema 而非硬编码词表：
        澄清时的「指标缺失」判定、以及答案片段的高亮词。
        必须缓存：样本值可能上千条，每轮重算会把提问路径拖慢。
        """
        if self._vocab_cache is None:
            try:
                self._vocab_cache = self.sql_tool.linker.vocabulary()
            except Exception:
                self._vocab_cache = []
        return self._vocab_cache

    def _entities(self) -> list:
        """库中真实出现的**值**（姓名这类可指称实体），不含表名/列名，缓存一次。

        专供「实体近邻」判断：拿列名一起匹配会误报（「评分」↔列名「扣分」、
        「排名」↔列名「姓名」都只差一字），详见 SchemaLinker.sample_values()。
        """
        if self._ent_cache is None:
            try:
                self._ent_cache = self.sql_tool.linker.sample_values()
            except Exception:
                self._ent_cache = []
        return self._ent_cache

    def _build_graph(self):
        builder = StateGraph(AgentState)

        # ========== 节点 0：指代消解 ==========
        def resolve_question(state: AgentState) -> AgentState:
            """把「那他的德育测评分呢」这类依赖上文的追问改写成自足问题。

            为什么非做不可：checkpointer 只持久化 state，不构成对话记忆。
            实测「这两个人相差多少分？」在没有历史时会解析成
            schema 样例行里的前两个人（胡子扬/杜佳玲），而用户指的是
            上一轮刚回答的第一名与第二名 —— 答案看起来合理，其实完全错位。
            """
            q = (state.get("question") or "").strip()
            history = state.get("history") or []
            state["resolved_question"] = q
            state["rewritten"] = False

            if not _needs_context(q, history):
                return state

            hist_text = _format_history(history)
            if not hist_text:
                return state

            prompt = f"""你是对话理解模块。请根据【历史对话】把【当前问题】改写成一个**自足、不含任何代词或省略**的完整问题，供下游的数据库查询与文档检索直接使用。

【改写规则】
1. 只做指代消解与省略补全：把「他/她/它/那/这/该」等替换成历史对话里明确出现的具体实体
2. **若上一条助手消息标注为「澄清提问」，而当前问题只是在补充缺失信息，
   必须把补充内容与「澄清提问之前的那个原始问题」合并成一个完整问题。**
   例：原始问题「前十是谁？」→ 助手澄清「您想按哪个指标查看前十名？」
       → 当前问题「按智育测评分」⇒ 输出「智育测评分前十是谁？」
   例：原始问题「那个班有多少人？」→ 助手澄清「您指的是哪个行政班？」
       → 当前问题「2112503 班」⇒ 输出「2112503 班有多少人？」
3. 不要回答问题、不要改变原意、不要新增历史中没有的条件
4. 若当前问题本身已经自足，**原样输出**，不要任何解释
5. 只输出改写后的问题本身，不要引号、不要前后缀、不要换行

【历史对话】
{hist_text}

【当前问题】
{q}

改写后的问题："""

            try:
                out = self.llm.invoke(prompt).content.strip()
            except Exception as e:
                state["trace"] = [f"🔗 [指代消解] → 调用失败（{type(e).__name__}），沿用原问题"]
                return state

            out = out.strip().strip('"\'“”‘’').strip()
            # 合理性护栏：空 / 过长 / 多行 / 换行都说明模型没守格式，宁可退回原问题，
            # 也不能把一个跑偏的改写喂给 SQL 生成
            if not out or len(out) > _RESOLVE_MAX_CHARS or "\n" in out:
                state["trace"] = ["🔗 [指代消解] → 输出不合规，沿用原问题"]
                return state

            if out != q:
                state["resolved_question"] = out
                state["rewritten"] = True
                state["trace"] = [f"🔗 [指代消解] → 「{q}」⇒「{out}」"]
            return state

        # ========== 节点 1：意图分类 + 语义校验（合并成一次调用） ==========
        # 为什么合并：澄清校验本身也要看 schema 与问题，跟意图分类的输入几乎一样。
        # 分成两次调用等于每题多花一次 LLM 延迟（实测约 +1s），而「系统效率」是评分项，
        # 所以这里用一个 JSON 同时要两个结论。
        def classify_intent(state: AgentState) -> AgentState:
            question = state.get("resolved_question") or state["question"]
            # schema 走 Schema Linking 裁剪，不再「整库 dump + 硬截断」：
            # 截断会把排在后面的表整张切掉，意图识别直接看不到可用表（NOSQL 死局根源）
            picked = self.sql_tool.get_schema_for(question)
            schema_preview = picked["text"]

            # 维度规则的三个输入。用**消解后**的问题：多轮里「那他的德育测评分呢」
            # 若不先消解，规则会把「他」判成「缺指代对象」—— 而这恰恰是上文已解决的。
            history = state.get("history") or []
            hist_text = _format_history(history)
            vocab = self._vocab()
            ents = self._entities()
            # 实体近邻是**确定性信号**（等长且仅差一字，比对的是库中真实样本值），
            # 所以不交给模型判断：库里没有「胡子杨」却有「胡子扬」时，
            # 直接查必然 0 行、系统回一句"没找到"—— 用户根本不知道自己打错了字。
            ent = clarify.suggest_entities(question, ents)

            def rule_dims(q: str) -> list:
                return clarify.infer_missing_dimensions(
                    q, vocab=vocab, context=hist_text,
                    has_history=bool(history), entities=ents)

            # 规则层的**确定性结构信号**必须能覆盖模型判断。
            # 起因：模型对「只点了名」（如「胡子扬」）常判 need_clarification=false，
            # 而规则层只在「模型说要澄清但没给维度」时才被调用 —— 于是 R6 白写，
            # 实测召回率从 100% 掉到 88.9%。这两维是纯结构判定（实体在场 + 无问句词 +
            # 无指标），不依赖语义理解，不该被模型的漏判挡掉。
            rule_pre = rule_dims(question)
            force_clarify = bool(ent) or clarify.DIM_ASPECT in rule_pre

            # 必须把历史**真的塞进提示词**：任务二/任务三的规则里写着
            # 「上文从未出现」「历史对话里也没有」，但原先只写了这句话、
            # 没有把 hist_text 插值进来 —— 等于要求模型参考一份它看不见的材料，
            # 结果就是多轮追问被误判成「缺指标」而反复澄清。
            hist_block = (
                f"\n【历史对话】（判断「上文是否已给出该维度」时必须参考这里）\n{hist_text}\n"
                if hist_text else
                "\n【历史对话】（本轮无历史对话，是首轮提问）\n")

            prompt = f"""分析用户问题，完成两件事并以 JSON 输出。

【数据库 Schema 预览】
{schema_preview}

【用户问题】
{question}
{hist_block}
【任务一：判断意图 intent】
- "sql"：查询数据、统计数字、排名、金额/数量、对比数据
- "rag"：查找知识、规章制度、方法论、经验、定义、流程说明
- "hybrid"：既要查库又要结合文档解释
只要 schema 里有能回答该问题的表就判 "sql"；不要因为表名是中文就回避。

【任务二：语义校验 need_clarification（中级难题 3，须从严）】
判 true 仅限「缺了关键信息就必然答错」的三种情形：
  a. 关键维度缺失且会改变答案：问"增长率/同比/环比"却没说时间范围；问"前十/排名"却没说按哪个指标
  b. 指代对象缺失且上文从未出现（如"那个班""这个人的成绩"，历史对话里也没有）
  c. 实体有多个同样合理的候选，选错会给出完全不同的答案
必须判 false 的情形：
  - 只是口语化 / 含别名 / 有错别字，但意图明确可推断（如"只育测评分前十""北京有多少客户"）
  - 能由 schema 或常识合理默认（如"多少人参评"就是 count）
  - 只是问法简短，并不缺信息
  - **该维度在上面的【历史对话】里已经给出** —— 多轮追问常常省略主语或指标，
    上文说过就不再算缺失（如上一轮问过"综合素质测评成绩排名"，本轮"排在第一名的是谁？"
    就不缺"指标"）
  - **拿不准时判 false**：宁可先按最合理解释作答，也不要为求稳而反问

【任务三：missing_dimensions —— 只从下面四个词里选，不要自创】
- "时间范围"：要求按时间统计/对比（增长率、同比、环比、趋势）却没说时间窗口
- "指标"：要求排名却没给排序依据（"前十是谁"缺"按什么排"）
- "指代对象"：出现"他/她/那个/这个/该X"，但历史对话里从未出现对应实体
- "对比对象"：要求对比却没给出参与对比的两方
need_clarification 为 false 时，这一项必须是空数组 []。

仅输出 JSON，不要其它文字：
{{"intent": "sql|rag|hybrid", "need_clarification": true|false,
  "missing_dimensions": ["时间范围"], "reason": "20字以内理由",
  "question": "要反问用户的一句话（不澄清则空）",
  "options": ["可选答案1", "可选答案2"]}}"""

            raw = ""
            state["missing_dimensions"] = []
            state["clarify_by_rule"] = False
            try:
                raw = self.llm.invoke(prompt).content
            except Exception as e:
                # 接口异常不能让整页崩。原来的降级是「不澄清，按 rag 继续」——
                # 那等于接口一挂，澄清能力就整体消失（该问的也不问了）。
                # 现在改用规则层兜底：规则不依赖任何外部接口，判定代价是 0。
                rd = rule_dims(question)
                state["intent"] = "sql" if rd else "rag"
                state["needs_clarification"] = bool(rd)
                state["missing_dimensions"] = rd
                state["clarify_by_rule"] = True
                if rd:
                    state["clarification"] = clarify.clarify_text(rd, ent)
                    state["clarify_options"] = clarify.rule_options(rd, ent)
                    state["clarify_reason"] = f"接口异常（{type(e).__name__}），改用规则校验"
                    state["final_answer"] = (
                        f"❓ 为了让答案准确，我需要先确认一点：\n\n"
                        f"{clarify.clarify_text(rd, ent)}\n\n"
                        + ("\n".join(f"- {o}" for o in state["clarify_options"])
                           if state["clarify_options"] else "")
                        + f"\n\n> 判断依据：{state['clarify_reason']}")
                state["trace"] = state.get("trace", []) + [
                    f"🎯 [意图识别] → 调用失败（{type(e).__name__}），"
                    f"改用规则校验维度"
                    + (f"并发起澄清：{'、'.join(rd)}" if rd else "：未发现缺失")
                ]
                return state

            obj = _parse_json(raw)
            intent = str(obj.get("intent", "")).strip().lower()
            if intent not in ("sql", "rag", "hybrid"):
                # 模型没守 JSON 格式时退回关键词判断，至少不把意图判死
                low = raw.lower()
                intent = "sql" if "sql" in low else ("hybrid" if "hybrid" in low else "rag")

            state["intent"] = intent
            state["trace"] = state.get("trace", []) + [
                f"🎯 [意图识别] → {intent}",
                f"🔎 [Schema Linking] → {picked['note']}"
            ] + ([f"　选中表：{'、'.join(picked['chosen'])}"] if picked.get("chosen") else [])

            # ── 语义校验结果 ──
            # 实体近邻/只点名这类确定性信号命中时**强制澄清**，模型漏判不构成漏澄清的理由
            need = bool(obj.get("need_clarification")) or force_clarify
            state["needs_clarification"] = need
            if not need:
                return state

            # 维度：模型给了就用（sanitize 对齐受控词表，对不上的丢掉）；
            # 模型说要澄清却说不出缺哪一维时，用规则补齐 —— 澄清消息必须能告诉
            # 用户「缺什么」，只报「信息不足」等于把问题踢回给用户。
            dims = clarify.sanitize(obj.get("missing_dimensions"))
            by_rule = False
            if not dims:
                dims = list(rule_pre)
                by_rule = bool(dims)
            elif force_clarify:
                # 模型给了维度，但确定性信号又发现它漏掉的维度 → 补进去
                dims = list(dims) + [d for d in rule_pre if d not in dims]
            if ent and clarify.DIM_ENTITY not in dims:
                dims = list(dims) + [clarify.DIM_ENTITY]
            state["missing_dimensions"] = dims
            state["clarify_by_rule"] = by_rule

            cq = str(obj.get("question") or "").strip()
            if not cq and dims:
                # 说了要澄清却没给问句 —— 用维度规则生成的问句顶上，
                # 比放弃澄清、直接基于错误假设硬答要安全
                cq = clarify.clarify_text(dims, ent)
            opts = [str(x) for x in (obj.get("options") or []) if str(x).strip()][:4]
            if not opts and (dims or ent):
                opts = clarify.rule_options(dims, ent)
            # 实体近邻必须把候选直接说出来，否则用户仍不知道该改哪个字
            if ent:
                cq = clarify.clarify_text([d for d in dims if d != clarify.DIM_ENTITY], ent)
            reason = str(obj.get("reason") or "").strip()[:60]
            if ent:
                # 实体近邻是我们**强制**澄清的，此时模型的 reason 往往是
                # 「历史对话已明确查询对象」这类与「正在反问」自相矛盾的话
                # （实测截图里就是这样）。强制路径必须自己给依据，不能沿用模型的。
                reason = (f"「{ent[0]['asked']}」在库中无精确匹配，"
                          f"但存在仅一字之差的「{ent[0]['candidate']}」")

            if not cq:
                # 既没有问句、也推不出维度 —— 无法向用户提问，只能放弃澄清照常作答
                state["needs_clarification"] = False
                state["missing_dimensions"] = []
                state["clarify_by_rule"] = False
            else:
                state["clarification"] = cq
                state["clarify_options"] = opts
                state["clarify_reason"] = reason
                state["final_answer"] = (
                    f"❓ 为了让答案准确，我需要先确认一点：\n\n**{cq}**\n\n"
                    + ("\n".join(f"- {o}" for o in opts) if opts else "")
                    + (f"\n\n> 判断依据：{reason}" if reason else "")
                )
                state["trace"] = state.get("trace", []) + [
                    f"❓ [语义校验] → 缺失维度：{'、'.join(dims) if dims else '未细分'}，发起澄清"
                    f"{'（规则兜底）' if by_rule else ''}"
                    + (f"｜{reason}" if reason else "")]

            return state

        def route_after_classify(state: AgentState) -> str:
            """信息不足就先反问，不要硬答；否则照常检索/查库"""
            return "clarify" if state.get("needs_clarification") else "retrieve"

        # ========== 节点 2：文档检索（必经） ==========
        # 这一步对所有问题都跑，不再只服务 rag 意图：
        # 只有跑了检索，「所有问答结果」才都拿得出引用片段；
        # 否则纯问数的回答下面空无一物，来源展示是断的。
        def execute_rag(state: AgentState) -> AgentState:
            question = state.get("resolved_question") or state["question"]
            chunks = self.rag.search(question)
            state["retrieved_chunks"] = chunks
            sources = set(c["source"] for c in chunks)
            state["trace"] = state.get("trace", []) + [
                f"📚 [文档检索] → 命中 {len(chunks)} 个片段，来自 {len(sources)} 个文档"
            ]
            # 降级要显式说出来：「没检索到」和「检索功能不可用」是两回事，
            # 不区分会让用户以为是资料里没有，从而误信答案
            if getattr(self.rag, "degraded_reason", ""):
                state["trace"].append(
                    f"⚠️ [文档检索] → 向量索引不可用，已降级为仅问数"
                    f"（{self.rag.degraded_reason[:80]}）")
            return state

        # ========== 节点 3a：SQL 查询 ==========
        def execute_sql(state: AgentState) -> AgentState:
            # 用消解后的自足问题，而不是带「他/那」的原问题
            question = state.get("resolved_question") or state["question"]
            # 只给相关表的 schema：表一多整库塞进 prompt 必然超限，
            # 超限后被切掉的表 LLM 就当它不存在，只能胡编或答 NOSQL
            picked = self.sql_tool.get_schema_for(question)
            state["schema_picked"] = picked

            sql_prompt = f"""根据数据库 schema 和用户问题，生成一条 SQLite 查询语句。

【数据库 Schema】
{picked['text']}

【用户问题】
{question}

【要求】
- 只输出 SQL 语句，不要多余内容
- 如果是 MySQL 特有语法，请转为 SQLite 兼容写法
- 表和字段名可能是中文（如 综合素质测评情况.智育测评分），直接使用即可，
  必要时用双引号包裹，不要在 SQL 里写中文注释
- 「前十 / 前N名」用 ORDER BY 对应分值 DESC LIMIT N，要带上姓名等可读字段
- 只能使用上面列出的表；若上方有「其他未展开的表」，说明它未被选中，
  不要臆造它的字段，也不要凭表名猜测列名
- 确实无法生成时输出：NOSQL"""

            sql_query = self.llm.invoke(sql_prompt).content.strip()
            sql_query = sql_query.replace("```sql", "").replace("```", "").strip()
            state["sql"] = sql_query
            state["sql_reasoning"] = "LLM 根据 schema 生成"

            if sql_query == "NOSQL" or not sql_query.lower().startswith(("select", "with", "explain")):
                state["sql_error"] = f"无法生成有效 SQL: {sql_query[:200]}"
                state["sql_result"] = []
                state["sql_columns"] = []
            else:
                result = self.sql_tool.run_query(sql_query)
                rows = result.get("result")
                state["sql_error"] = result.get("error")
                # 用 is not None 判断，避免 [] / 空 DataFrame 等真值歧义
                state["sql_result"] = rows if rows is not None else []
                # 列名直接取自结构化数据 list[dict] 的键，不再对字符串按逗号切分
                if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                    state["sql_columns"] = [str(c) for c in rows[0].keys()]
                else:
                    state["sql_columns"] = []

            trace_msg = f"💻 [SQL 生成] → {sql_query[:200] if sql_query else '无法生成'}"
            if state["sql_error"]:
                trace_msg += f"\n⚠️ [SQL 错误] → {state['sql_error']}"
            else:
                rows = state.get("sql_result")
                row_count = len(rows) if isinstance(rows, (list, tuple)) else 0
                trace_msg += f"\n📊 [SQL 结果] → 返回 {row_count} 行"
            state["trace"] = state.get("trace", []) + [trace_msg]
            return state

        # ========== 节点 3：生成答案 ==========
        def generate_answer(state: AgentState) -> AgentState:
            intent = state["intent"]
            context_parts = []

            if intent in ("sql", "hybrid") and state.get("sql"):
                context_parts.append(f"""
【SQL 查询】
{state['sql']}

【查询结果】
列: {state.get('sql_columns', [])}
数据(前50行): {state.get('sql_result', [])[:50]}
错误: {state.get('sql_error', '无')}
""")

            if state.get("retrieved_chunks"):
                # 与 RAGTool.query() 共用同一个上下文构造器：同源块合并后再注入。
                # 两边各写一份的话，检索参数一改就会口径分叉（本项目踩过）。
                doc_text = build_context(state["retrieved_chunks"])
                role = ("本次作答的主要依据" if intent == "rag"
                        else "与问题相关的补充资料")
                context_parts.append(
                    f"【文档检索结果】（{role}；检索对所有问题都会执行。"
                    f"若某段与问题无关，直接忽略，不要牵强引用）\n{doc_text}")

            context = "\n\n".join(context_parts) if context_parts else "（无相关数据）"

            # 多轮：把历史对话一并给生成端，答案才能承接上文而不是从零开始；
            # 同时明确「只回答当前问题」，避免模型把历史里的旧结论再复述一遍
            history_text = _format_history(state.get("history") or [])
            history_block = (
                f"\n【历史对话】（仅用于理解当前问题的指代，不要重复回答历史问题）\n"
                f"{history_text}\n" if history_text else "")

            question = state.get("resolved_question") or state["question"]

            prompt = f"""你是企业数据分析助手。请基于以下信息回答用户问题。
{history_block}
【要求】
1. 先给结论，再展示依据
2. 如果涉及数据，用表格或列表清晰展示
3. **每个结论都要标注来源**：出自 SQL 查询结果就写明"来源：SQL 查询结果"，
   来自文档就写明文档名。没有用到的资料不要列出来充当引用
4. 如果信息不足，诚实说明
5. 只回答【当前问题】；历史对话只用来判断"他/那第二个/这两个人"指的是谁，
   不要把历史里已经答过的内容再答一遍
6. 涉及文档检索时：**回答语言与提问语言保持一致**（英文提问就用英文答）；
   只要资料里有支撑结论的内容就据实作答，不要因为「找不到与问题逐字相同的句子」
   而回答"无法回答"——表格要按行列对齐读、公式要按符号读；
   只有确实找不到任何相关信息时才说明信息不足
7. 数值/日期/名称必须与资料**逐字一致**，不得近似、换算或推算；
   表格资料先锁定行与列的交点再读数，不要跨行跨列串读；
   只使用资料中出现过的事实，不要引入资料之外的知识——
   资料里没有的数值就明说「资料中未给出」，不要用相近的数字顶上

【可用信息】
{context}

【当前问题】
{question}

请回答："""

            answer = self.llm.invoke(prompt).content
            state["final_answer"] = answer
            state["trace"] = state.get("trace", []) + ["✅ [生成答案] → 完成"]
            return state

        # ========== 路由逻辑（串行，不并行） ==========
        def route_after_retrieve(state: AgentState) -> str:
            """检索已经跑完，这里只决定要不要再查库"""
            return "sql" if state["intent"] in ("sql", "hybrid") else "generate"

        def route_after_sql(state: AgentState) -> str:
            # 检索已是必经节点，SQL 这条路没产出时不必再补检索，
            # 直接进生成：文档片段已经在 state 里，生成端会用它兜底作答
            rows = state.get("sql_result")
            if state.get("sql_error") or not rows:
                state["trace"] = state.get("trace", []) + [
                    "↩️ [SQL 无产出] → 改用文档片段作答"
                ]
            return "generate"

        # ========== 注册节点 ==========
        builder.add_node("resolve", resolve_question)
        builder.add_node("classify", classify_intent)
        builder.add_node("retrieve", execute_rag)
        builder.add_node("sql", execute_sql)
        builder.add_node("generate", generate_answer)

        # 指代消解是入口：先把问题补全，再判意图、再检索/查库
        builder.set_entry_point("resolve")
        builder.add_edge("resolve", "classify")
        # 语义校验不通过 → 直接结束并发起澄清，不进入检索/查库（避免基于错误假设硬答）
        builder.add_conditional_edges(
            "classify",
            route_after_classify,
            {"clarify": END, "retrieve": "retrieve"}
        )
        # 检索是必经的：retrieve → (sql | generate)
        builder.add_conditional_edges(
            "retrieve",
            route_after_retrieve,
            {"sql": "sql", "generate": "generate"}
        )
        builder.add_conditional_edges(
            "sql",
            route_after_sql,
            {"generate": "generate"}
        )

        builder.add_edge("generate", END)

        memory = MemorySaver()
        return builder.compile(checkpointer=memory)

    def run(self, question: str, thread_id: str = "default", history: list = None) -> dict:
        """执行一轮问答。

        history：本轮的对话历史（不含当前问题），由调用方提供 ——
                 进程内 state 只在一处权威，就是界面上的消息列表。
        """
        config = {"configurable": {"thread_id": thread_id}}
        initial_state: AgentState = {
            "question": question,
            "resolved_question": question,
            "rewritten": False,
            "history": list(history or []),
            "needs_clarification": False,
            "clarification": "",
            "clarify_options": [],
            "clarify_reason": "",
            "missing_dimensions": [],
            "clarify_by_rule": False,
            "intent": "",
            "sql": None,
            "sql_result": None,
            "sql_error": None,
            "sql_reasoning": "",
            "sql_columns": [],
            "schema_picked": {},
            "retrieved_chunks": [],
            "final_answer": "",
            "trace": []
        }
        try:
            return self.graph.invoke(initial_state, config)
        except Exception as e:
            # 兜底：任何节点异常都不要让整个页面崩掉，转为可读的失败结果
            initial_state["final_answer"] = (
                f"⚠️ 本次处理失败：`{type(e).__name__}`\n\n"
                f"```\n{e}\n```\n\n"
                f"可展开「🧠 推理过程」查看已完成步骤；若为接口超时或限流，稍后重试即可。"
            )
            initial_state["trace"] = [f"❌ [异常中止] → {type(e).__name__}: {e}"]
            return initial_state
