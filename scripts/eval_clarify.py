# -*- coding: utf-8 -*-
"""语义校验与澄清评测（中级难题 3）。

对应赛题：
  · 中级难题 3「智能语义校验与澄清」：维度缺失时应主动澄清，而不是直接生成错误查询
  · 决赛评分「智能意图理解与澄清」10 分（意图识别 4 / 主动澄清 7 / 精准且体验好 10）
  · **核心亮点预览页强制项「澄清交互成功率」**

必须双向测，否则会奖励「什么都反问」这种最差行为：
  澄清召回率 = 该问的问了 / 该问的（漏问 = 基于错误假设硬答）
  澄清精确率 = 该问的问了 / 实际问了的（误问 = 打断用户）
  F1         = 对外报的「澄清交互成功率」
另外测**闭环**：澄清之后用户补一句，系统要真的把答案补齐（不能问完就完）。

还测**维度层**（赛题原文的落点就是「维度缺失」）：
  维度命中率  = 澄清时报告出的缺失维度命中了 expect_dims 的正例占比
  维度误报数  = 负例里被报出维度（或正例报了 expect 之外的维度）的条数
  待补维度分布 = 所有报告维度的计数，用来回答「用户最常缺哪一维」
并额外跑一遍**纯规则层**（agent/clarify，不调任何模型）作为对照基线 ——
这是「评分规则设计与实现路径」的可复现证据：规则自己就能判，且不受接口可用性影响。

用法：
    python scripts/eval_clarify.py [--limit N] [--save] [--rule-only]
退出码 0 = F1 ≥ 0.8 且 误澄清率 ≤ 0.2

离线可用性：
    没有 API Key 时**不再直接崩**（此前 `AgentGraph()` 会抛 RuntimeError，
    连规则层对照都跑不出来，而规则层恰恰是「接口不可用时的兜底能力证据」）。
    现在两种情况都走规则层：
      · 显式传 `--rule-only`  → 只跑规则层对照，跳过模型层
      · 缺 Key 时自动降级     → 同上，并在报告里标 `mode: "rule-only"`
    模型层指标在离线模式下为 None，不参与达标判定（只判规则层维度命中）。
"""
import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from config import Config  # noqa: E402
from agent import clarify  # noqa: E402

CASES_PATH = os.path.join(ROOT, "data", "eval", "clarify_cases.json")
REPORT_DIR = os.path.join(ROOT, "data", "eval", "reports")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--rule-only", action="store_true",
                    help="只跑规则层对照（不构造 AgentGraph、不调模型）")
    args = ap.parse_args()

    blob = json.load(open(CASES_PATH, encoding="utf-8"))
    cases = blob["cases"][:args.limit] if args.limit else blob["cases"]

    print("=" * 78)
    print(f"语义校验与澄清评测 · {len(cases)} 个用例")
    print("=" * 78)

    # ── 决定跑不跑模型层 ──
    # 缺 Key 时**降级而不崩**：模型层指标置 None，只报规则层对照。
    rule_only = args.rule_only
    if not rule_only:
        missing = Config.missing_keys()
        if missing:
            rule_only = True
            print(f"⚠️  缺少 API Key（{'、'.join(missing)}），自动降级为 --rule-only。")
            print("    模型层（澄清召回/精确/F1、闭环）本次不测；规则层照常。")
            print()
    if rule_only:
        print("### 模式：rule-only（只跑规则层对照，不调任何模型）")
        print()

    agent = None
    if not rule_only:
        from agent.graph import AgentGraph  # 仅在有 Key 时导入并构造
        agent = AgentGraph()

    if rule_only:
        # 规则层也要词表/实体表（R5 实体确认靠它做近邻匹配）。
        # 这两项由 SchemaLinker 提供，而 SQLTool 本身不依赖 API Key
        # （LLM/embedding 不可用时只是降级为无画像的词法检索），
        # 所以离线也能拿到与线上一致的真实 schema 词表。
        try:
            from tools.sql_tool import SQLTool
            _linker = SQLTool().linker
            vocab = _linker.vocabulary()
            ents = _linker.sample_values()
        except Exception as e:
            print(f"⚠️  词表加载失败（{type(e).__name__}: {e}），规则层将在无词表下运行。")
            vocab, ents = [], []
    else:
        vocab = agent._vocab() if hasattr(agent, "_vocab") else []
        ents = agent._entities() if hasattr(agent, "_entities") else []
    asked = tp = fp = fn = tn = 0
    rows = []
    dim_hit = dim_n = dim_false = 0
    dim_counter = Counter()
    # 纯规则层的独立对照：不用模型，只用 agent/clarify 的规则判「缺哪一维」
    rule_hit = rule_n = 0
    rule_counter = Counter()

    for c in cases:
        if rule_only:
            did = False
            got_dims = []
            r = {}
        else:
            r = agent.run(c["q"], f"eval-clarify-{c['id']}")
            did = bool(r.get("clarification"))
            got_dims = list(r.get("missing_dimensions") or [])
        want = c["expect"] == "clarify"
        if want and did:
            tp += 1
        elif want and not did:
            fn += 1
        elif not want and did:
            fp += 1
        else:
            tn += 1
        if did:
            asked += 1

        # ── 维度层 ──（规则层模式下模型层维度为空，不参与维度层统计）
        exp_dims = list(c.get("expect_dims") or [])
        if not rule_only:
            for d in got_dims:
                dim_counter[d] += 1
            if exp_dims:
                dim_n += 1
                if set(got_dims) & set(exp_dims):
                    dim_hit += 1
                # 多报维度 = 误导用户补不需要的信息，同样计入误报
                dim_false += len(set(got_dims) - set(exp_dims))
            else:
                # 负例不该报出任何维度
                dim_false += len(got_dims)

        # ── 规则层独立对照（不调模型）──
        # 必须把**实体表**一起传进去：R5「实体确认」靠它做近邻匹配，
        # 漏传会让规则层少报一维、兜底能力被低估（这条对照数字是要写进文档的）
        rd = clarify.infer_missing_dimensions(c["q"], vocab=vocab,
                                             has_history=False, entities=ents)
        for d in rd:
            rule_counter[d] += 1
        if exp_dims:
            rule_n += 1
            if set(rd) & set(exp_dims):
                rule_hit += 1

        rows.append({"id": c["id"], "q": c["q"], "expect": c["expect"], "did": did,
                     "missing_dimensions": got_dims,
                     "by_rule": bool(r.get("clarify_by_rule")),
                     "reason": r.get("clarify_reason") or "",
                     "clarification": r.get("clarification") or "",
                     "rule_dims": rd,
                     "answer": (r.get("final_answer") or "")[:160],
                     "note": c.get("note", "")})
        if rule_only:
            # 离线：只展示规则层判定，模型层无数据
            hit = "✅" if (exp_dims and set(rd) & set(exp_dims)) else "—"
            print(f"{hit} {c['id']} [{c['expect']:7s}] {c['q']}")
            print(f"      规则层缺失维度: {rd or '（无）'}")
        else:
            mark = "✅" if (want == did) else "❌"
            print(f"{mark} {c['id']} [{c['expect']:7s}] {c['q']}")
            print(f"      实际: {'澄清' if did else '作答'}"
                  + (f" → 缺失维度 {got_dims}"
                     f"{'（规则兜底）' if r.get('clarify_by_rule') else ''}" if did else "")
                  + (f"  ✗ {c.get('note')}" if want != did else ""))

    # ── 闭环：澄清后补一句，答案要真的补齐 ──
    print()
    print("=" * 78)
    print("### 澄清闭环（用户补充后必须真的给出答案）")
    closure = []
    for c in ([] if rule_only else blob.get("closure", [])):
        tid = f"eval-closure-{c['id']}"
        r1 = agent.run(c["first"], tid)
        # 必须与 app.py 一样带上 clarification 标记，否则消解节点识别不出
        # 「上一轮在澄清」，这条闭环用例就测不到真正的路径
        msgs = [{"role": "user", "content": c["first"]},
                {"role": "assistant", "content": r1.get("final_answer") or "",
                 "clarification": bool(r1.get("clarification"))}]
        r2 = agent.run(c["reply"], tid, history=msgs)
        ans = r2.get("final_answer") or ""
        ok = (not r2.get("clarification")) and any(
            k.lower() in ans.lower() for k in c["expect_any"])
        closure.append({"id": c["id"], "first": c["first"], "reply": c["reply"],
                        "asked_first": bool(r1.get("clarification")),
                        "dims": list(r1.get("missing_dimensions") or []),
                        "ok": ok, "answer": ans[:200]})
        print(f"{'✅' if ok else '❌'} {c['id']} 「{c['first']}」→澄清={bool(r1.get('clarification'))}"
              f"（缺失维度 {r1.get('missing_dimensions') or []}）"
              f" → 回复「{c['reply']}」→ 答案含期望值={ok}")
        if not ok:
            print(f"      A: {ans[:160]}")

    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    fpr = fp / (fp + tn) if (fp + tn) else 0.0

    print()
    print("=" * 78)
    print("### 指标")
    if rule_only:
        print("  （rule-only 模式：模型层指标不适用，下面只看规则层对照）")
    else:
        print(f"  真阳 {tp}  假阴(该问没问) {fn}  假阳(不该问却问) {fp}  真阴 {tn}")
        print(f"  澄清召回率 : {rec*100:.1f}%   （漏问 = 基于错误假设硬答）")
        print(f"  澄清精确率 : {prec*100:.1f}%   （误问 = 打断用户）")
        print(f"  **澄清交互成功率 F1 : {f1*100:.1f}%**   ← 亮点预览页要报这个")
        print(f"  误澄清率   : {fpr*100:.1f}%")

    dim_rate = (dim_hit / dim_n) if dim_n else 0.0
    rule_rate = (rule_hit / rule_n) if rule_n else 0.0
    print()
    print("### 维度层（赛题原文的落点是『维度缺失』，这里单独量化）")
    print(f"  受控维度词表 : {'、'.join(clarify.DIMENSIONS)}")
    if not rule_only:
        print(f"  维度命中率   : {dim_hit}/{dim_n} = {dim_rate*100:.1f}%"
              f"   （正例澄清时报出的维度命中 expect_dims）")
        print(f"  维度误报数   : {dim_false}   （负例报出维度 + 正例多报的维度）")
        print(f"  待补维度分布 : "
              + ("、".join(f"{k}×{v}" for k, v in dim_counter.most_common()) or "（无）"))
    print(f"  规则层独立对照: {rule_hit}/{rule_n} = {rule_rate*100:.1f}%"
          f"   （纯规则、不调任何模型；接口不可用时的兜底能力）")
    print(f"  规则层维度分布: "
          + ("、".join(f"{k}×{v}" for k, v in rule_counter.most_common()) or "（无）"))

    cl_ok = sum(1 for c in closure if c["ok"])
    if closure:
        print(f"  澄清闭环成功率 : {cl_ok}/{len(closure)}"
              f" = {cl_ok/len(closure)*100:.1f}%")

    if args.save:
        os.makedirs(REPORT_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        mode = "rule-only" if rule_only else "full"
        with open(os.path.join(REPORT_DIR, f"clarify-{stamp}.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"mode": mode,
                       "tp": tp, "fp": fp, "fn": fn, "tn": tn,
                       "precision": prec, "recall": rec, "f1": f1, "fpr": fpr,
                       "dimensions": {"controlled": list(clarify.DIMENSIONS),
                                      "hit": dim_hit, "n": dim_n, "rate": dim_rate,
                                      "false": dim_false,
                                      "distribution": dict(dim_counter)},
                       "rule_only": {"hit": rule_hit, "n": rule_n, "rate": rule_rate,
                                     "distribution": dict(rule_counter)},
                       "cases": rows, "closure": closure}, fh,
                      ensure_ascii=False, indent=2)
        print(f"\n报告已落盘：{REPORT_DIR}（mode={mode}）")

    # 达标判定：有模型层就按原口径（F1 + 误澄清率）；
    # 纯离线只判规则层维度命中（≥80%），否则离线永远不可能「达标」。
    # 退出码：0 = 完整评测达标；1 = 完整评测未达标；
    #         10 = 降级完成（缺 Key，只跑了规则层）且规则层达标；
    #         11 = 降级完成但规则层未达标。
    # 10/11 与 0/1 区分开，是为了让 CI 一眼看出「这次不是完整评测」，
    # 避免把降级结果当成完整结果记账。
    if rule_only:
        ok = rule_rate >= 0.8
        print()
        print("✅ 规则层达标（rule-only：维度命中 ≥ 80%）" if ok
              else "❌ 规则层未达标（rule-only：维度命中 < 80%）")
        print("   注意：这是**降级**结果（缺 API Key），模型层指标未测。")
        return 10 if ok else 11
    ok = f1 >= 0.8 and fpr <= 0.2
    print()
    print("✅ 达标" if ok else "❌ 未达标（目标 F1 ≥ 80% 且 误澄清率 ≤ 20%）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
