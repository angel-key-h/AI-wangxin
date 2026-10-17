# -*- coding: utf-8 -*-
"""中级难题 3：语义校验与澄清 —— 把「维度缺失」做成**结构化、可审计**的对象。

赛题原文：当用户问题存在**维度缺失**（如问"增长率"却未指定**时间范围**）时，
系统需主动发起澄清，引导用户补全信息，而非直接生成错误查询。

改造前的状态：能力已经能跑（该澄清的澄清、F1 100%），但「缺了什么」只是 LLM
自己写的 20 字理由，说不清缺的是哪一维 —— 于是三件事都做不到：
  · 报告里没法给出「待补维度分布」这类量化结论
  · 用户被打断时不知道系统到底想要什么信息
  · LLM 调用失败时整条澄清能力直接消失（失败分支写死了 need_clarification=False）

这里补的是**规则层**：一份受控维度词表 + 一套可审计的判定规则。
它与 LLM 判断的关系是「兜底 + 对照」，不是取代：
  · LLM 正常给出结构化维度  → 用 LLM 的（它能理解更细的说法）
  · LLM 说要澄清但没给维度  → 用规则补齐，澄清消息里就有具体维度可报
  · LLM 调用失败            → 用规则决定要不要澄清，澄清能力不再随接口一起挂掉

为什么用规则而不是再调一次模型：赛题要的是「评分规则设计与实现路径」——
规则可解释、可审计、零额外延迟，且不受接口可用性影响。
规则的两个关键输入都来自**真实 schema**（列名/表名/样本值）而不是硬编码词表，
换一套库判定口径自动跟着变。
"""
import re

# ── 受控维度词表 ──
# 只用这几个词，不允许自由发挥：报告要能按维度聚合，自由文本没法聚合。
DIM_TIME = "时间范围"
DIM_METRIC = "指标"
DIM_REF = "指代对象"
DIM_TARGET = "对比对象"
# 第 5 维：实体确认。来自一个真实事故 —— 用户问「贾素兰」（库里是「贾淑兰」），
# 系统既不纠错也不澄清，直接跑 `WHERE 姓名='贾素兰'` 得 0 行，然后回一句
# "资料中未找到" —— 用户根本不知道自己名字打错了。这正是赛题说的
# 「直接生成错误查询」，属于必须澄清的场景。
DIM_ENTITY = "实体确认"
# 第 6 维：查询维度。来自另一类真实输入 —— 用户只打了「贾淑兰」（一个名字）。
# 名字不是问题：没说想了解他的哪一方面。系统把整条记录倒出来看着"贴心"，
# 实际是把"你想问什么"这个决策又丢回给用户；赛题要求维度缺失时主动澄清。
DIM_ASPECT = "查询维度"
DIMENSIONS = (DIM_TIME, DIM_METRIC, DIM_REF, DIM_TARGET, DIM_ENTITY, DIM_ASPECT)

# 每个维度给一句「为什么缺了它会答错」，澄清消息与报告都用它
DIM_HINT = {
    DIM_TIME: "增长/同比类结论按时间窗口统计，窗口不同答案完全不同",
    DIM_METRIC: "排名要有排序依据，指标不同名次完全不同",
    DIM_REF: "指代对象在本轮对话里没有出现过，无从确定指谁",
    DIM_TARGET: "没有给出参与对比的对象，无法构成对比",
    DIM_ENTITY: "问题里的实体在本库精确匹配不到，但存在仅一字之差的实体 —— "
                "选错会查到另一个人，直接查则必然是 0 行",
    DIM_ASPECT: "只给了查询对象、没说要查它的哪个方面 —— 系统只能把整条记录倒出来，"
                "而用户往往只关心其中一两项",
}

# 可选的补全选项（只给真的能枚举出来的维度，凑不出来就不给）
DIM_OPTIONS = {
    DIM_TIME: ["本学年", "本学期", "近一年"],
    DIM_ASPECT: ["成绩和排名", "各分项测评分", "全部信息"],
}

# ── 判定规则 ──
# 时间词：出现即认为「时间范围」这一维已给出。
# 注意**不把「同比/环比」算作时间词** —— 它们恰恰是「要求按时间对比、但没说哪个
# 窗口」的触发词；把触发词同时当满足条件，规则就永远不会触发（本模块最易写错处）。
_TIME_WORD = re.compile(
    r"(\d{4}\s*年|\d{1,2}\s*月|\d{1,2}\s*日|第?[一二三四1-4]\s*季度|季度|学期|学年|"
    r"年度|去年|今年|明年|上个月|下个月|本月|最近|近期|过去|上半年|下半年|全年|"
    r"\d{4}|上周|本周)")

# 需要时间窗口才能成立的问法
_TIME_TRIGGER = re.compile(
    r"(增长率|增幅|增速|涨幅|跌幅|同比|环比|趋势|走势|变化情况|增长|下降|提升|"
    r"上升|回落|涨了|跌了|波动)")

# 排名类问法：排序依据必须另行给出，否则「第几名」无从计算
_RANK_TRIGGER = re.compile(
    r"(前十|前[一二三四五六七八九十0-9]+名?|排名|排行榜|排行|"
    r"第[一二三四五六七八九十0-9]+名|倒数第?[一二三四五六七八九十0-9]*)")

# 极值类问法：这类词**常自带**依据（「客户最多」「扣分最多」），
# 只有它前面没接名词时才真的缺依据。
_EXTREME_TRIGGER = re.compile(r"(最高|最低|最大|最小|最多|最少|最好|最差|最强|最弱)")

# 指代词：单看无法确定指谁，必须靠上文。
# 「他/她/它」加负向环视把「其他」排除掉；「该」必须后接名词，否则「应该」会误命中。
_PRONOUN = re.compile(
    r"(他们|她们|它们|那个|这个|这些|那些|上述|前者|后者|"
    r"该[班级生员表项条个人部司]|(?<!其)他|(?<!其)她|(?<!其)它)")

_CONTRAST = re.compile(r"(对比|比较|相比|差异|区别|哪个更)")
_CONNECTOR = re.compile(r"(和|与|跟|同|比)")
# 连续中文块：用于实体近邻匹配（见 suggest_entities）
CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")

# 「问句信号」：出现即说明用户表达了想问什么，不属于「只点了名」
_QUESTION_WORD = re.compile(
    r"(多少|几个|几名|几人|谁|哪[个些里]|什么|怎么|如何|为什么|"
    r"总数|合计|总计|平均|汇总|比例|占比|分布|"
    r"最高|最低|最大|最小|最多|最少|最好|最差|"
    r"排名|前十|前[一二三四五六七八九十0-9]+名?|第[一二三四五六七八九十0-9]+名|"
    r"有没有|是否|列出|列举|对比|比较)")
# 填充词：去掉它们之后若没剩什么内容，说明这句实质上只有实体
_FILLER = re.compile(
    r"(的|了|是|请问|帮我|帮忙|给|查一下|查查|看看|看一下|一下|一下儿|"
    r"信息|资料|情况|数据|相关|关于|想知道|了解|说一下|介绍)")

# 功能字：极值词前面若紧邻这些字（或处于句首），说明没有名词给它当依据
_FUNCTION = set(
    "的了是在有和与及或吗呢吧啊谁是什怎么如何哪几个多少不没我你他她它们这那该其"
    "请帮给出一下列举告诉查询看找能可要会就去来说对把被让并且但因为所以如果那么"
)


def _mentioned(question: str, vocab) -> list:
    """问题里出现了哪些指标/实体名。

    两档匹配，第二档是为了**错别字**：
      · 精确包含 —— 「智育测评分前十是谁」直接命中
      · 放松一位 —— 「只育测评分前十是谁」写错了一个字，严格包含匹配不到，
        规则会误报「缺指标」。做法是看词表里 >= 3 字的词，其「去掉首字」或
        「去掉尾字」的子串是否出现在问题里（「育测评分」在 → 算命中）。
    只对 >= 3 字的词放宽：2 字词再放宽一位等于没有约束。
    """
    q = question or ""
    hits = []
    for v in vocab or ():
        s = str(v).strip()
        if len(s) < 2:
            continue
        if s in q:
            hits.append(s)
        elif len(s) >= 3 and (s[1:] in q or s[:-1] in q):
            hits.append(s)
    return hits


def suggest_entities(question: str, entities=()) -> list:
    """找出「问题里的实体在库中匹配不到，但存在等长且仅差一字的近邻」。

    返回 [{"asked": 问题里的写法, "candidate": 库中的真实实体}]，可能为空。

    **必须只拿「样本值」当匹配目标，不能拿整个 vocabulary（含列名）** ——
    第一版拿全表匹配，立刻出现两个致命假阳性：
      「智育测评**分**」里的子串「评分」↔列名「扣分」、
      「**排名**」↔列名「姓名」，
    于是每一个正常问句都会被判成「实体写错了」而发起澄清。
    用真实数据里的**值**（姓名这类可指称实体）当目标，这类噪声自动消失：
    「扣分/姓名」是列名，不是谁的名字。

    另外候选只取每个中文块的**前缀**（2~4 字）与整块，不取任意内部子串 ——
    姓名几乎总在块首（「贾素兰的智育测评分」），而内部子串正是上面噪声的来源。
    """
    q = question or ""
    targets = {str(v).strip() for v in (entities or ()) if len(str(v).strip()) >= 2}
    if not q or not targets:
        return []
    out = []
    for run in CJK_RUN.findall(q):
        cands = [run[:k] for k in (4, 3, 2) if k < len(run)] + [run]
        for cand in cands:
            if len(cand) < 2 or cand in targets:
                continue
            near = next((v for v in targets
                         if len(v) == len(cand)
                         and sum(1 for a, b in zip(cand, v) if a != b) == 1), None)
            if near:
                item = {"asked": cand, "candidate": near}
                if item not in out:
                    out.append(item)
    return out


def infer_missing_dimensions(question: str, vocab=(), context: str = "",
                             has_history: bool = False, entities=()) -> list:
    """用可审计的规则推断「缺了哪些维度」，返回受控词表里的维度名列表。

    参数：
      question  本轮问题（应传**消解后**的问题；否则「那他的德育测评分呢」里的
                指代会被误判成「缺指代对象」，而这其实是多轮里已经解决的事）
      vocab     库中的实体/指标词表（表名+列名+样本值），来自 SchemaLinker.vocabulary()
      context   可用的上文文本；判「指标是否已给出」时与问题合并看 ——
                多轮里指标常常只在上一轮说过（「排在第一名的是谁？」
                单看缺指标，结合上文就不缺）
      has_history  是否存在上文。为 False 时，指代词才算真的无从确定。
    """
    q = (question or "").strip()
    if not q:
        return []
    joined = f"{context}\n{q}"
    dims = []

    # R1 时间范围：要求按时间对比，却没有任何时间窗口
    if _TIME_TRIGGER.search(q) and not _TIME_WORD.search(joined):
        dims.append(DIM_TIME)

    # R2 指标：排名问法，或「裸极值」（极值词前面没接名词），
    #          且问题与上文里都找不到任何指标名
    need_metric = bool(_RANK_TRIGGER.search(q))
    if not need_metric:
        for m in _EXTREME_TRIGGER.finditer(q):
            i = m.start()
            if i == 0 or q[i - 1] in _FUNCTION:
                need_metric = True
                break
    if need_metric and not _mentioned(joined, vocab):
        dims.append(DIM_METRIC)

    # R3 指代对象：有指代词，但整轮对话没有上文可供消解
    if not has_history and _PRONOUN.search(q):
        dims.append(DIM_REF)

    # R4 对比对象：要对比，却没说和谁比。
    # 先把「对比/比较」去掉再找连接词 —— 否则「对比」自带的那个「比」会被
    # 当成「已经给了对比对象」，规则永远不触发。
    if _CONTRAST.search(q) and not _CONNECTOR.search(_CONTRAST.sub(" ", q)):
        dims.append(DIM_TARGET)

    # R5 实体确认：问题里的实体在库中精确匹配不到，却有等长仅差一字的近邻。
    # 这是**确定性信号**，不该交给模型判断：直接查必然 0 行，然后系统回一句
    # "没找到" —— 用户无从知道自己打错了字（真实事故见 DIM_ENTITY 注释）。
    if suggest_entities(q, entities):
        dims.append(DIM_ENTITY)

    # R6 查询维度：只点了名，没说想查哪一方面。
    # 三个必要条件缺一不可（否则会误伤正常问句）：
    #   ① 句子里**真的出现了库中的实体值**（「北京的首都」没有实体 → 不触发）
    #   ② 没有问句信号词（「贾淑兰的智育测评分」有指标 → 不触发）
    #   ③ 上下文里也没有指标（多轮里指标常只在上一轮说过）
    ent_set = {str(e).strip() for e in (entities or ()) if str(e).strip()}
    ent_hits = [e for e in ent_set if e in q]
    metric_hits = [h for h in _mentioned(joined, vocab) if h not in ent_set]
    if (ent_hits and not _QUESTION_WORD.search(q) and not metric_hits):
        residue = q
        for h in _mentioned(q, vocab):
            residue = residue.replace(h, "")
        residue = _FILLER.sub("", residue)
        residue = re.sub(r"[\s，。？！,.?!、；;：:]+", "", residue).strip()
        # 必须是**整句都由实体 + 填充词构成**，不能只是"剩得不多"。
        # 早先用 len(residue) <= 6，结果「北京的首都」去掉实体与『的』后还剩『首都』
        # 也被判成「只点了名」→ 误澄清（负例精确率掉到 90%）。
        if residue == "":
            dims.append(DIM_ASPECT)

    return dims


def clarify_text(dims: list, entities: list = None) -> str:
    """规则兜底时的澄清问句：把缺失维度 + 该维为何重要摊开给用户。

    刻意不编造「具体选项」以外的东西 —— 系统并不知道用户想要哪个时间窗口，
    假装知道反而是误导。只把「我需要什么、为什么需要」讲清楚。

    唯一的例外是实体近邻：这不是「我不知道你要什么」，而是「你写的名字库里没有，
    但有一个只差一字的」—— 这种情况必须**把近邻直接说出来**，
    否则用户仍然不知道该改哪个字。
    """
    dims = [d for d in dims if d]
    if not dims and not entities:
        return ""
    ent = entities or []
    lines = []
    for e in ent:
        lines.append(f"库里没有「{e['asked']}」，但存在**「{e['candidate']}」**"
                     f"（仅一字之差）—— 你是想问这个吗？")
    if dims:
        if lines:
            lines.append("")
            lines.append(f"另外，为了给出准确答案，请补充以下信息：**{'、'.join(dims)}**")
        else:
            lines.append(f"为了给出准确答案，请补充以下信息：**{'、'.join(dims)}**")
        lines.append("")
        for d in dims:
            lines.append(f"- **{d}**：{DIM_HINT.get(d, '缺少该项会导致答案不唯一')}")
    return "\n".join(lines)


def rule_options(dims: list, entities: list = None) -> list:
    """规则路径的可选答案：只给真的能枚举出来的维度"""
    out = []
    for e in (entities or []):
        if e.get("candidate") and e["candidate"] not in out:
            out.append(e["candidate"])
    for d in dims:
        for o in DIM_OPTIONS.get(d, []):
            if o not in out:
                out.append(o)
    return out[:4]


def sanitize(dims) -> list:
    """把 LLM 给出的维度名对齐到受控词表：能对上就用标准名，对不上就丢掉。

    丢弃而不是保留自由文本 —— 报告要按维度聚合，任何非受控项都会让聚合失效；
    而丢弃是安全的：LLM 给的维度只是「补充说明」，主判定仍是 need_clarification。
    顺带做一次同义归一，「时间」「时间段」「统计时间」都收敛到「时间范围」。
    """
    alias = {
        "时间": DIM_TIME, "时间区间": DIM_TIME, "时间段": DIM_TIME,
        "统计时间": DIM_TIME, "年份": DIM_TIME,
        "指标": DIM_METRIC, "排序依据": DIM_METRIC, "字段": DIM_METRIC,
        "维度": DIM_METRIC, "排序指标": DIM_METRIC, "排序字段": DIM_METRIC,
        "指代对象": DIM_REF, "指代": DIM_REF, "对象": DIM_REF, "主体": DIM_REF,
        "对比对象": DIM_TARGET, "对比": DIM_TARGET, "比较对象": DIM_TARGET,
    }
    out = []
    for d in dims or ():
        s = str(d).strip()
        if not s:
            continue
        key = alias.get(s, s if s in DIMENSIONS else None)
        if key and key not in out:
            out.append(key)
    return out
