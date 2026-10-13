# -*- coding: utf-8 -*-
"""上线自检：确认「有没有跑最新代码」「SQL 类问题还带不带引用片段」。

用法：
    python scripts/verify_live.py

退出码 0 = 全部通过（可直接对用户展示的当前状态）；1 = 有项目没通过。
"""
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent.graph as graph_mod  # noqa: E402

FAIL = []


def check(name, ok, detail=""):
    print(f"{'✅' if ok else '❌'} {name}" + (f" —— {detail}" if detail else ""))
    if not ok:
        FAIL.append(name)


print("=" * 62)
print("1) 图结构：检索必须是所有问题都经过的节点")
print("=" * 62)
with open(os.path.join(os.path.dirname(graph_mod.__file__), "graph.py"), encoding="utf-8") as fh:
    src = fh.read()
check("存在 retrieve 节点", 'add_node("retrieve"' in src)
check("resolve 是指代消解入口", 'set_entry_point("resolve")' in src)
check("classify → retrieve/clarify 条件边", "route_after_classify" in src)
check("retrieve → sql/generate 条件边", "route_after_retrieve" in src)
check("graph.py 里不再有 classify → sql 直连",
      'add_edge("classify", "sql")' not in src)

print()
print("=" * 62)
print("2) 热重载：reload 之后能拿到新的类对象")
print("=" * 62)
old_cls = graph_mod.AgentGraph
importlib.reload(graph_mod)
new_cls = sys.modules["agent.graph"].AgentGraph
check("importlib.reload 产生新类（缓存清了就能生效）", old_cls is not new_cls,
      f"old={old_cls} new={new_cls}")

print()
print("=" * 62)
print("3) 端到端：SQL 类问题必须同时返回引用片段")
print("=" * 62)
from config import Config  # noqa: E402

# 3) 4) 两节要真实调用云端模型，缺 Key 时物理上跑不了。**明确跳过并说明缺哪一项**，
# 而不是抛异常终止整个脚本 —— 本脚本的价值恰恰在于「不花接口也能自查
# 图结构 / 热重载 / 纯函数」，不该被一次缺 Key 全部废掉。
_missing_keys = Config.missing_keys()
# 「Key 存在但通道不可用」也要单独判：欠费（402）/ 持续限流时，意图识别必然失败，
# 第 3 节的「片段 > 0」断言同样物理上不可能成立。若不区分，一次外部抖动就会
# 被显示成「代码坏了」，真正的回归反而被淹没 —— 正是本节开头要避免的情况。
# 置为 True 时脚本以退出码 2（通道不可用）结束，而不是 1（断言失败）。
_api_down = False
if _missing_keys:
    print(f"⏭️  缺少 API Key（{'、'.join(_missing_keys)}），跳过第 3、4 节端到端断言。")
    print("    在项目根目录配好 .env 后重跑，即可覆盖这两节。")
else:
    from agent.graph import AgentGraph  # noqa: E402

    agent = AgentGraph()
    # 环境降级 vs 代码回归必须分开判：向量索引不可用（embedding 欠费/断网）时
    # 「引用片段数 > 0」这条断言在物理上就不可能成立，此时应当跳过而不是报红 ——
    # 否则每次外部接口抖动都会显示成「代码坏了」，真正的回归反而被淹没。
    rag_ok = getattr(agent.rag, "available", True)
    if not rag_ok:
        print("⏭️  文档检索处于降级状态，本节仅校验意图识别；引用片段断言已跳过。")
        print(f"    降级原因：{(getattr(agent.rag, 'degraded_reason', '') or '')[:130]}")

    # 问题必须指向语料/库里确实存在的内容，否则检出 0 片段反映的是「语料没有」，
    # 而不是「检索坏了」—— 拿它当回归用例只会误导排查。
    # 另外，指代不明的问题会被语义校验拦进 clarify 分支，连检索都不跑。
    CASES = [
        ("智育前十是谁", "sql"),
        ("一共有多少位客户？", "sql"),
        ("公司有多少员工", "sql"),
        ("Differential Geometry 讲了什么", "rag"),
    ]
    _api_skipped = []
    for q, want_intent in CASES:
        state = agent.run(q, "verify-live")
        chunks = state.get("retrieved_chunks") or []
        got = state.get("intent")
        # 意图识别调用了真实模型。trace 里出现失败标记 = 通道不可用，
        # 这条既不该报红、也不该当成「意图识别错了」。
        _trace_i = " ".join(state.get("trace") or [])
        if "APIStatusError" in _trace_i or "调用失败" in _trace_i:
            _api_down = True
            _api_skipped.append(q)
            continue
        if rag_ok:
            check(f"{q} → intent={got} 片段={len(chunks)}", len(chunks) > 0)
        else:
            ok = (want_intent is None) or (got == want_intent)
            check(f"{q} → intent={got}（片段断言已跳过）", ok,
                  "" if ok else f"期望 intent={want_intent}")
    if _api_skipped:
        print(f"⏭️  LLM 通道调用失败，第 3 节端到端断言跳过 {len(_api_skipped)}/{len(CASES)} 条："
              f"{_api_skipped}")
        print("    这是**环境状态**（Key 存在但欠费 402 / 持续限流），不是代码回归；")
        print("    本脚本将以退出码 2（通道不可用）结束，而不是 1（断言失败）。")

    print()
    print("=" * 62)
    print("4) 澄清分支：信息不足时应当反问，而不是硬答")
    print("=" * 62)
    # 故意用一个指代不明的提问：应当触发澄清并直接结束，不进入检索。
    # 对照组是上一节的明确问题 —— 那些都会走 retrieve 并拿到片段。
    # （「介绍一下这个比赛」曾被我当检索回归用例，换语料后它稳定检出 0 片段，
    #   实际是走了澄清分支，白白误导过一轮排查。）
    _state = agent.run("介绍一下这个", "verify-live-clarify")
    _trace = " ".join(_state.get("trace") or [])
    _chunks = _state.get("retrieved_chunks") or []
    check("触发澄清（trace 含「澄清」）", "澄清" in _trace, f"trace={_trace[:110]}")
    check("澄清时不进入检索，故无片段", len(_chunks) == 0, f"片段={len(_chunks)}")
    # 难题 3 的落点是「维度缺失」：澄清必须说得出缺**哪一维**，
    # 只报「信息不足」等于把问题原封不动踢回给用户
    check("澄清时给出结构化缺失维度", bool(_state.get("missing_dimensions")),
          f"dims={_state.get('missing_dimensions')}")

print()
print("=" * 62)
print("5) 纯函数层：高亮 / 复杂度策略（不花接口，纯本地断言）")
print("=" * 62)
from tools.explain import highlight, query_terms, snippet_html  # noqa: E402
from tools.doc_quality import plan_for_text  # noqa: E402

# 高亮必须**逐字保真**：片段里的 LaTeX 与竖线不能被 Markdown 吃掉
_snip = "胡子扬 的智育测评分是 92.5，见 \\begin{tabular}{|c|c|} 与 $E=mc^2$"
_terms = query_terms("胡子扬的智育测评分是多少", vocab=["智育测评分", "胡子扬"])
_html, _hits = snippet_html(_snip, _terms)
# 命中必须是**完整词**：「的智育测」「评分是」这种跨词边界的 4-gram 抢位
# 会让高亮看起来像是系统认错了词（强词锚定就是为这件事加的）
check("高亮命中完整实体与指标", set(_hits) >= {"胡子扬", "智育测评分"}, f"hits={_hits}")
check("高亮保留 LaTeX 字面（不被 Markdown 解析）",
      "\\begin{tabular}" in _html and "$E=mc^2$" in _html, f"html={_html[:120]}")
check("高亮不残留未闭合标签", _html.count("<mark") == _html.count("</mark>") == len(_hits))

# 转义必须真的发生：片段里的尖括号与 & 要先变实体，否则既破坏排版也是注入面
_esc, _ = snippet_html("a < b & c > d", query_terms("a"))
check("高亮对特殊字符做 HTML 转义",
      "&lt;" in _esc and "&amp;" in _esc and "&gt;" in _esc and "<mark" not in _esc,
      f"html={_esc[-80:]}")

_plan = plan_for_text("普通文本内容" * 20)
check("复杂度策略：简单档不改默认参数",
      (_plan["chunk_size"], _plan["chunk_overlap"], _plan["top_k"]) == (1000, 160, 6),
      f"{_plan['chunk_size']}/{_plan['chunk_overlap']} top_k={_plan['top_k']}")
_plan_c = plan_for_text(r"\begin{tabular}{|c|c|}" + r"$x=\frac{1}{2}$" * 8)
check("复杂度策略：密集档收紧切块并走 VLM",
      _plan_c["chunk_size"] < 1000 and _plan_c["parse_path"] == "vlm",
      f"chunk={_plan_c['chunk_size']} path={_plan_c['parse_path']}")

print()
print("=" * 62)
if FAIL:
    print(f"❌ 未通过 {len(FAIL)} 项：" + "、".join(FAIL))
    sys.exit(1)
if _api_down:
    # 本地断言全过，只是外部通道不可用，有断言没跑成 —— 不能报 0（"全部通过"
    # 会让人以为端到端也验过了），也不该报 1（那是"代码坏了"的语义）。
    print("⚠️  本地断言全部通过；但 LLM 通道不可用，第 3 节部分端到端断言**未执行**。")
    print("    退出码 2：恢复额度/凭据后重跑可覆盖这些断言。")
    sys.exit(2)
print("✅ 全部通过")
sys.exit(0)
