# -*- coding: utf-8 -*-
"""多轮对话评测：量化「指代消解」到底有没有用。

对应赛题校内初赛评分标准「优化指标 · 多轮对话上下文保持
（具备多轮 3 分，连续 5 轮以上准确得 5 分）」。

做法：同一组对话跑两遍 ——
  baseline 不带 history（修改前的行为）
  fixed    带 history（修改后的行为）
期望全部落在 **SQL 语句** 上（确定性产物），答案只做关键数值校验，
因为答案里常会附带检索片段提到的无关学生名，用它判分会误伤。

用法：
    python scripts/eval_multiturn.py
退出码 0 = fixed 全通过；1 = 有项目没通过；2 = 缺少 API Key（无法评测）。

注意：本脚本**本质是模型行为评测**（多轮指代消解依赖 LLM 改写问题），
没有可离线降级的规则层。因此缺 Key 时给出可操作提示并以退出码 2 结束，
而不是抛 RuntimeError 崩栈 —— 便于 CI / 一键脚本区分「跑挂了」和「没配 Key」。
"""
import argparse
import json
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))   # 同目录的 _preflight
REPORT_DIR = os.path.join(ROOT, "data", "eval", "reports")

from _preflight import model_provenance  # noqa: E402
from config import Config  # noqa: E402

# 每轮：sql_has 必须全部出现在 SQL 里；sql_not 必须一个都不出现；ans_has 必须在答案里
DIALOGUES = [
    {
        "name": "A · 跨轮指代链（5 轮）",
        "turns": [
            {"q": "胡子扬的综合素质测评成绩和排名是多少？",
             "sql_has": ["胡子扬"], "ans_has": ["104.3"]},
            {"q": "那他的德育测评分呢？",
             "sql_has": ["胡子扬", "德育测评分"], "sql_not": ["杜佳玲"]},
            {"q": "排在第一名的是谁？",
             "ans_has": ["曾欣妍"]},
            {"q": "那第二名呢？",
             "ans_has": ["权晓蕊"]},
            {"q": "这两个人相差多少分？",
             "sql_has": ["曾欣妍", "权晓蕊"], "sql_not": ["胡子扬", "杜佳玲"]},
        ],
    },
    {
        "name": "B · 省略指代（3 轮）",
        "turns": [
            {"q": "每个行政班的平均综合素质测评成绩是多少？"},
            {"q": "最高的是哪个班？", "ans_has": ["2112506"]},
            {"q": "那个班有多少人？", "sql_has": ["2112506"]},
        ],
    },
]


def check_turn(result, spec):
    """返回 (是否通过, 失败原因列表)"""
    sql = (result.get("sql") or "")
    ans = (result.get("final_answer") or "")
    bad = []
    for s in spec.get("sql_has", []):
        if s not in sql:
            bad.append(f"SQL 缺「{s}」")
    for s in spec.get("sql_not", []):
        if s in sql:
            bad.append(f"SQL 误含「{s}」")
    for s in spec.get("ans_has", []):
        if s not in ans:
            bad.append(f"答案缺「{s}」")
    return (not bad), bad


def run_dialogue(agent, dlg, use_history):
    """按 app.py 的方式累历史：取本轮之前的所有消息"""
    thread = f"eval-{dlg['name'][0]}-{use_history}"
    messages, rows = [], []
    for i, spec in enumerate(dlg["turns"], 1):
        history = ([{"role": m["role"], "content": m["content"]} for m in messages]
                   if use_history else [])
        res = agent.run(spec["q"], thread, history=history)
        ok, bad = check_turn(res, spec)
        rows.append({
            "turn": i, "q": spec["q"], "ok": ok, "why": "；".join(bad),
            "resolved": res.get("resolved_question"),
            "rewritten": res.get("rewritten"),
            "sql": (res.get("sql") or "").replace("\n", " ")[:150],
            "ans": (res.get("final_answer") or "").replace("\n", " ")[:120],
        })
        messages.append({"role": "user", "content": spec["q"]})
        messages.append({"role": "assistant", "content": res.get("final_answer") or ""})
    return rows


def main():
    ap = argparse.ArgumentParser()
    # **默认落盘**：这个脚本此前不保存任何报告，于是 README 引用的
    # 「baseline 3/8 → 8/8」在 data/eval/reports/ 里找不到任何产物支撑，
    # 属于「有结论、无证据」。评测证据必须留成文件，需要时用 --no-save 关掉。
    ap.add_argument("--no-save", action="store_true", help="不落盘报告")
    args = ap.parse_args()

    missing = Config.missing_keys()
    if missing:
        print("=" * 78)
        print("⏭️  缺少必要的 API Key（" + "、".join(missing) + "），无法评测多轮指代消解。")
        print("    本评测依赖 LLM 改写问题，无离线降级路径。")
        print("    请在项目根目录 .env 中配置后重跑（模板见 .env.example）。")
        print("=" * 78)
        return 2

    from agent.graph import AgentGraph
    agent = AgentGraph()
    summary = []
    details = []

    for dlg in DIALOGUES:
        print("=" * 78)
        print(f"### {dlg['name']}")
        for mode, use_hist in (("baseline（无历史，修改前）", False), ("fixed（带历史，修改后）", True)):
            rows = run_dialogue(agent, dlg, use_hist)
            passed = sum(1 for r in rows if r["ok"])
            total = len(rows)
            print(f"\n  ── {mode}：{passed}/{total} 通过")
            for r in rows:
                mark = "✅" if r["ok"] else "❌"
                rw = f" ⇐「{r['resolved']}」" if r["rewritten"] else ""
                print(f"   {mark} T{r['turn']} {r['q']}{rw}")
                if r["sql"]:
                    print(f"        SQL: {r['sql']}")
                if not r["ok"]:
                    print(f"        ✗ {r['why']}")
                    print(f"        A: {r['ans']}")
            summary.append((dlg["name"], mode, passed, total))
            details.append({"dialogue": dlg["name"],
                            "mode": "fixed" if use_hist else "baseline",
                            "passed": passed, "total": total, "turns": rows})
            need_5 = dlg["name"].startswith("A")
            if use_hist and need_5 and passed < total:
                print(f"\n  ⚠️ 未达标：赛题要求「连续 5 轮以上准确」")

    print()
    print("=" * 78)
    print("### 汇总")
    print(f"  {'对话':<24}{'模式':<26}{'通过'}")
    for name, mode, p, t in summary:
        print(f"  {name:<24}{mode:<26}{p}/{t}")

    fixed_total = sum(p for n, m, p, t in summary if m.startswith("fixed"))
    fixed_all = sum(t for n, m, p, t in summary if m.startswith("fixed"))
    base_total = sum(p for n, m, p, t in summary if m.startswith("baseline"))
    print(f"\n  多轮准确率：baseline {base_total}/{fixed_all}"
          f" → fixed {fixed_total}/{fixed_all}"
          f"（{fixed_total / fixed_all * 100:.1f}%）")

    if not args.no_save:
        os.makedirs(REPORT_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(REPORT_DIR, f"multiturn-{stamp}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({
                "criterion": "赛题：具备多轮 3 分 / 连续 5 轮以上准确 5 分",
                "env": model_provenance(),
                "baseline_passed": base_total, "fixed_passed": fixed_total,
                "total": fixed_all,
                "fixed_rate": fixed_total / fixed_all if fixed_all else 0.0,
                "dialogues": details,
            }, fh, ensure_ascii=False, indent=2)
        print(f"\n报告已落盘：{path}")

    ok = fixed_total == fixed_all
    print("\n" + ("✅ 全部通过" if ok else "❌ 有未通过项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
