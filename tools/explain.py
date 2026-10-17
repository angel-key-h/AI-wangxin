# -*- coding: utf-8 -*-
"""答案可解释性：引用片段的**查询词高亮**。

赛题基础任务原文是「系统能够对答案进行基本的来源展示，例如**高亮显示**引用的
文档片段，或展示生成的 SQL 语句」。展示 SQL 这条早就有了（见 app.render_sources），
而引用片段此前只是把原文贴出来 —— 那是「展示」不是「高亮」。

难点不在「找到一个词就染色」，而在两件事：

1. **不能把片段当 Markdown 渲染**。检索回来的片段大量含 LaTeX
   （``\\begin{tabular}``、``$...$``、``&`` 分列）与 Markdown 表格分隔符 ``|``。
   走 st.markdown 的话 ``$公式$`` 会被 KaTeX 吃掉、``|`` 会被当表格分隔符，
   用户看到的和原始证据就不是同一件事了 —— 可解释性反而被破坏。
   所以这里先做 **HTML 转义**再插入 ``<mark>``，最后交给 st.html() 渲染，
   原文逐字保真。

2. **高亮要克制**。候选词是 2~4 元组，全染色会让半屏都是荧光色，反而看不出
   重点。这里按「长词优先、互不重叠、限量、剔除纯功能字」四条规则切分区间，
   并把真正命中的词回传给调用方列在片段头部 —— 高亮本身也要是可解释的。

对外三个函数：
  query_terms()   从问题 + schema 词表抽出值得高亮的词
  highlight()     在纯文本上切不重叠区间，返回 (HTML, 命中词)
  snippet_html()  一步到位产出可直接喂给 st.html() 的片段块
"""
import html
import re
from collections import namedtuple

# 单次最多渲染的高亮片段数。超过这个数量，视觉上等于没高亮。
MAX_MARKS = 14
# 高亮词的最短长度（中文 2 字起，避免「的」「是」这类单字噪声）
MIN_TERM_LEN = 2

# 高亮词。strong=True 表示它来自真实 schema（表名/列名/样本值）——
# 这类词是「实体与指标」，必须优先占位，理由见 _spans() 的注释。
Term = namedtuple("Term", "text strong")

# 纯功能字：若一个候选词**完全**由这些字组成，则它不承载任何区分信息。
# 注意判据是「完全由」，不是「包含」—— 「前十」含「前」，但它是有意义的，
# 不该被误杀；而「是谁」「多少」「哪些」整词都是功能字，必须杀掉。
_FUNCTION_CHARS = set(
    "的了是在有和与及或吗呢吧啊谁是什怎么如何哪几个多少不没我你他她它们这那该其"
    "请帮给出一下列举告诉查询看找能可要会就去来说对把被让并且但因为所以如果那么"
)

_ALNUM = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-/]*")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")


def _is_noise(term: str) -> bool:
    """整词皆功能字 → 噪声（「是谁」「多少」），否则保留（「前十」「德育测评分」）"""
    return all(ch in _FUNCTION_CHARS for ch in term)


def query_terms(question: str, vocab=(), limit: int = 60) -> list:
    """抽出「值得高亮」的词，返回 Term 列表（长词优先）。

    三个来源，精度依次递减、覆盖依次递增：
      1. **schema 词表**（表名 / 列名 / 样本值）—— 真正的实体与指标，标记为 strong。
         用户核对片段时最想看的就是它们。用真实 schema 而不是硬编码词表，
         库一换这层就跟着变。
      2. 问题里的**字母数字串** —— 学号、班级号、英文术语。
      3. 问题里 CJK 连串的 2~4 元组 —— 兜底覆盖上面两层没收录的说法，
         用「整词皆功能字」过滤掉噪声。

    strong 这个标记不是装饰，它是高亮正确性的前提，见 _spans()。
    """
    q = (question or "").strip()
    if not q:
        return []
    out, seen = [], set()

    def add(term: str, strong: bool):
        if term not in seen:
            seen.add(term)
            out.append(Term(term, strong))

    # 1. schema 词表：只保留真的出现在问题里的，否则等于给每个片段都染色
    for v in vocab or ():
        s = str(v).strip()
        if len(s) >= MIN_TERM_LEN and s in q:
            add(s, True)

    # 2. 字母数字串
    for tok in _ALNUM.findall(q):
        if len(tok) >= MIN_TERM_LEN:
            add(tok, False)

    # 3. CJK n-gram（长在前）
    for run in _CJK_RUN.findall(q):
        n = len(run)
        for size in (4, 3, 2):
            if size > n:
                continue
            for i in range(n - size + 1):
                gram = run[i:i + size]
                if not _is_noise(gram):
                    add(gram, False)

    return sorted(out, key=lambda t: len(t.text), reverse=True)[:limit]


def _greedy(segment: str, words: list, offset: int = 0) -> list:
    """在 segment 上从左到右扫描，每一步取**最长匹配**，返回全文坐标区间。"""
    spans, i, n = [], 0, len(segment)
    while i < n:
        best = 0
        for w in words:
            lw = len(w)
            if lw > best and segment.startswith(w, i):
                best = lw
        if best:
            spans.append((offset + i, offset + i + best))
            i += best
        else:
            i += 1
    return spans


def _spans(text: str, terms: list) -> list:
    """在 text 上切出**互不重叠**的高亮区间。

    分两趟，这一层是必需的而不是优化：

    第 1 趟只用 **strong 词**（schema 里的实体/指标）锚定。因为它们是最该被
    看到的，而且一旦被短词抢先占位就会整体错位 —— 实测「贾淑兰的智育测评分」
    这类问题会生成 4-gram「的智育测」「评分是」，它们**起点更靠前**，
    贪心扫描会先吃掉这 4 个字，导致真正的「智育测评分」永远匹配不上，
    高亮出来的是 `的智育测` + `评分是` 两个毫无意义的片段。

    第 2 趟才用**全部词**（含 n-gram）填第一趟留下的空隙，保证覆盖率。

    另一层好处是可读性：强词先占位后，「智育测评分」整体染色，
    而不是「智育」+「测评」+「评分」三段相邻荧光块（覆盖字符一样，
    读起来却像三个不同的命中词）。
    """
    strong = [t.text for t in terms if t.strong]
    weak = [t.text for t in terms]
    if not strong:
        return _greedy(text, weak) if weak else []

    spans, prev = [], 0
    for a, b in _greedy(text, strong):
        spans.extend(_greedy(text[prev:a], weak, offset=prev))
        spans.append((a, b))
        prev = b
    spans.extend(_greedy(text[prev:], weak, offset=prev))
    return spans


def highlight(text: str, terms: list, max_marks: int = MAX_MARKS) -> tuple:
    """把 text 里的 terms 包成 <mark>，返回 (已转义的 HTML, 命中的词列表)。

    转义先行、插标签在后：片段里的 ``<`` ``>`` ``&`` 必须先变成实体，
    否则既会破坏排版，也是一个注入面。
    """
    text = text or ""
    spans = _spans(text, terms)
    if not spans:
        return html.escape(text, quote=False), []

    # 高亮太多等于没高亮：超限时按「长度优先」保留最有信息量的那些，
    # 再按位置还原顺序（否则渲染出来会跳来跳去）
    if len(spans) > max_marks:
        keep = sorted(spans, key=lambda s: s[1] - s[0], reverse=True)[:max_marks]
        spans = sorted(keep)

    out, last, hits = [], 0, []
    for a, b in spans:
        out.append(html.escape(text[last:a], quote=False))
        piece = text[a:b]
        hits.append(piece)
        # 显式给 mark 配色：浏览器默认的 <mark> 是黄底黑字，但在深色主题下
        # 不同内核的默认值不一致，写死一份保证两种主题都可读
        out.append(f'<mark style="background:#ffe58f;color:#1a1a1a;'
                   f'border-radius:2px;padding:0 1px;">'
                   f'{html.escape(piece, quote=False)}</mark>')
        last = b
    out.append(html.escape(text[last:], quote=False))
    return "".join(out), hits


def snippet_html(text: str, terms: list, limit: int = 400,
                 max_marks: int = MAX_MARKS) -> tuple:
    """产出可直接交给 st.html() 的片段块，返回 (html, 命中词)。

    用 ``white-space:pre-wrap`` 保住原文换行；LaTeX 与表格的竖线原样呈现，
    不做任何 Markdown 解释 —— 这是「逐字保真」的关键。
    """
    body = (text or "")[:limit]
    if not body:
        return "", []
    inner, hits = highlight(body, terms, max_marks)
    if len(text or "") > limit:
        inner += '<span style="opacity:.55;">…（已截断）</span>'
    style = ("background:rgba(127,127,127,0.10);"
             "border-left:3px solid rgba(127,127,127,0.45);"
             "padding:8px 12px;border-radius:4px;white-space:pre-wrap;"
             "word-break:break-word;font-size:0.88rem;line-height:1.6;")
    return f'<div style="{style}">{inner}</div>', hits