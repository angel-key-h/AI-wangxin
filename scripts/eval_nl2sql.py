# -*- coding: utf-8 -*-
"""NL2SQL 执行准确率评测。

对应赛题校内初赛：
  · 功能指标 1「基础 NL2SQL 单表查询能力」10 分（≥10 个测试用例，每个正确得 1 分）
  · 性能指标 1「NL2SQL 执行准确率」10 分（≥70%→6 分，≥80%→8 分，≥90%→10 分）
  · 设计报告「项目核心亮点预览页」**强制**要这个数字

判分口径：**执行准确率** —— 智能体生成的 SQL 与 gold SQL 的查询结果一致才算对，
不是比对 SQL 文本（同义写法应当都算对）。比较规则见 data/eval/nl2sql_cases.json 的 meta。

用法：
    python scripts/eval_nl2sql.py               # 全量
    python scripts/eval_nl2sql.py --tier single # 只跑某一档
    python scripts/eval_nl2sql.py --save        # 额外落盘 Markdown/JSON 报告
退出码 0 = 准确率 ≥ 90%（评分表最高档）；1 = 未达标；2 = 缺少 API Key（无法评测）
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))   # 同目录的 _preflight
os.chdir(ROOT)

from _preflight import model_provenance  # noqa: E402
from config import Config  # noqa: E402

CASES_PATH = os.path.join(ROOT, "data", "eval", "nl2sql_cases.json")
REPORT_DIR = os.path.join(ROOT, "data", "eval", "reports")

TIER_LABEL = {"single": "单表简单查询", "ranking": "排序与 Top-N",
              "aggregate": "聚合与分组", "multitable": "多表关联"}


def load_cases():
    with open(CASES_PATH, encoding="utf-8") as fh:
        blob = json.load(fh)
    return blob["meta"], blob["cases"]


def norm(v):
    """归一化单个值：数字统一成 float，字符串去空白；数字型字符串也转数字"""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if s == "":
        return ""
    try:
        return float(s)
    except ValueError:
        return s


def eq(a, b, tol):
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= tol
    return a == b


def row_covered(gold_vals, agent_vals, tol):
    """gold 行的每个值是否都能在 agent 行里找到对应（agent 多返回列不算错）"""
    pool = list(agent_vals)
    for gv in gold_vals:
        for i, av in enumerate(pool):
            if eq(gv, av, tol):
                pool.pop(i)
                break
        else:
            return False
    return True


def compare(gold_rows, agent_rows, mode, tol):
    """返回 (是否通过, 失败原因)"""
    if len(gold_rows) != len(agent_rows):
        return False, f"行数不同：期望 {len(gold_rows)}，实际 {len(agent_rows)}"
    if not gold_rows:
        return False, "两边都是空结果，无法判定（gold 不应为空）"
    if mode == "scalar":
        g, a = gold_rows[0], agent_rows[0]
        for gv in g:
            if not any(eq(gv, av, tol) for av in a):
                return False, f"值不符：期望 {g}，实际 {a}"
        return True, ""
    if mode == "ordered":
        for gr, ar in zip(gold_rows, agent_rows):
            if not row_covered(gr, ar, tol):
                return False, f"行不匹配：期望 {gr}，实际 {ar}"
        return True, ""
    pool = list(agent_rows)                      # rows：无序，双射匹配
    for gr in gold_rows:
        for i, ar in enumerate(pool):
            if row_covered(gr, ar, tol):
                pool.pop(i)
                break
        else:
            return False, f"有期望行未被覆盖：{gr}"
    return True, ""


def run_gold(con, sql):
    cur = con.execute(sql)
    return [[norm(v) for v in row] for row in cur.fetchall()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default=None, help="只跑某一档：single/ranking/aggregate/multitable")
    ap.add_argument("--save", action="store_true", help="落盘报告")
    args = ap.parse_args()

    meta, cases = load_cases()
    tol = float(meta.get("float_tolerance", 0.02))
    if args.tier:
        cases = [c for c in cases if c["tier"] == args.tier]

    # gold 自检：gold 本身跑不出结果说明评测集写错了，必须立刻暴露
    db = Config.SQL_DB_PATH
    con = sqlite3.connect("file:" + db.replace("\\", "/") + "?mode=ro&immutable=1", uri=True)
    gold_cache, gold_bad = {}, []
    for c in cases:
        try:
            rows = run_gold(con, c["gold"])
            if not rows:
                gold_bad.append((c["id"], "gold 返回空结果"))
            gold_cache[c["id"]] = rows
        except Exception as e:
            gold_bad.append((c["id"], f"gold 执行失败: {e}"))
    if gold_bad:
        print("❌ 评测集 gold SQL 自身有问题，先修评测集：")
        for i, m in gold_bad:
            print(f"   {i}: {m}")
        return 1

    print("=" * 78)
    print(f"NL2SQL 执行准确率评测 · {len(cases)} 个用例 · 浮点容差 {tol}")
    print(f"数据库: {db}")
    print("=" * 78)

    missing = Config.missing_keys()
    if missing:
        print("=" * 78)
        print("⏭️  缺少必要的 API Key（" + "、".join(missing) + "），无法评测 NL2SQL。")
        print("    本评测依赖 LLM 生成 SQL，无离线降级路径。")
        print("    请在项目根目录 .env 中配置后重跑（模板见 .env.example）。")
        print("=" * 78)
        return 2

    from agent.graph import AgentGraph
    agent = AgentGraph()
    results, t_start = [], time.time()
    for c in cases:
        t0 = time.time()
        try:
            res = agent.run(c["q"], f"eval-nl2sql-{c['id']}")
        except Exception as e:
            res = {"sql": None, "sql_result": None,
                   "final_answer": f"<异常 {type(e).__name__}: {e}>"}
        dt = time.time() - t0

        sql = res.get("sql") or ""
        raw = res.get("sql_result")
        agent_rows = []
        if isinstance(raw, list) and raw and isinstance(raw[0], dict):
            agent_rows = [[norm(v) for v in row.values()] for row in raw]
        elif isinstance(raw, list):
            agent_rows = [[norm(v) for v in (row if isinstance(row, (list, tuple)) else [row])]
                          for row in raw]

        if not agent_rows:
            ok, why = False, ("未生成 SQL" if not sql or sql.strip() == "NOSQL"
                              else f"查询无结果（error={res.get('sql_error')}）")
        else:
            ok, why = compare(gold_cache[c["id"]], agent_rows, c["mode"], tol)

        results.append({"id": c["id"], "tier": c["tier"], "mode": c["mode"], "q": c["q"],
                        "ok": ok, "why": why, "sec": round(dt, 2),
                        "intent": res.get("intent"), "sql": sql.replace("\n", " ").strip(),
                        "agent_rows": agent_rows[:4], "gold_rows": gold_cache[c["id"]][:4],
                        "note": c.get("note", "")})
        print(f"{'✅' if ok else '❌'} {c['id']} [{TIER_LABEL[c['tier']]}] {c['q']}"
              f"  ({dt:.1f}s)")
        if not ok:
            print(f"      ✗ {why}")
            print(f"      SQL: {sql[:200] or '(空)'}")

    total = len(results)
    passed = sum(1 for r in results if r["ok"])
    acc = passed / total if total else 0.0

    print()
    print("=" * 78)
    print("### 分档统计")
    for tier in ("single", "ranking", "aggregate", "multitable"):
        sub = [r for r in results if r["tier"] == tier]
        if sub:
            p = sum(1 for r in sub if r["ok"])
            print(f"  {TIER_LABEL[tier]:16s} {p}/{len(sub)}  {p/len(sub)*100:5.1f}%")

    print()
    print("=" * 78)
    print(f"### 总执行准确率：{passed}/{total} = {acc*100:.1f}%"
          f"   总耗时 {time.time()-t_start:.0f}s")
    if acc >= 0.9:
        print("   评分表对应：≥90% → 10 分（满分档）")
    elif acc >= 0.8:
        print("   评分表对应：≥80% → 8 分")
    elif acc >= 0.7:
        print("   评分表对应：≥70% → 6 分")
    else:
        print("   评分表对应：<70% → 0 分")

    if args.save:
        os.makedirs(REPORT_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        with open(os.path.join(REPORT_DIR, f"nl2sql-{stamp}.json"), "w", encoding="utf-8") as fh:
            json.dump({"accuracy": acc, "passed": passed, "total": total,
                       "env": model_provenance(),
                       "results": results}, fh, ensure_ascii=False, indent=2)
        md = [f"# NL2SQL 执行准确率评测报告（{stamp}）", "",
              f"- 准确率：**{passed}/{total} = {acc*100:.1f}%**",
              # 写相对路径：绝对路径会把作者本机目录结构带进公开报告，且对读者无意义
              f"- 数据库：`{os.path.relpath(db, ROOT) if os.path.isabs(db) else db}`",
              f"- 浮点容差：{tol}",
              f"- 模型：`{Config.LLM_MODEL}` @ {Config.LLM_BASE_URL}", "",
              "| 档位 | 通过 | 占比 |", "|---|---|---|"]
        for tier in ("single", "ranking", "aggregate", "multitable"):
            sub = [r for r in results if r["tier"] == tier]
            if sub:
                p = sum(1 for r in sub if r["ok"])
                md.append(f"| {TIER_LABEL[tier]} | {p}/{len(sub)} | {p/len(sub)*100:.1f}% |")
        md += ["", "| ID | 档位 | 问题 | 结果 | 耗时 | 生成的 SQL |", "|---|---|---|---|---|---|"]
        for r in results:
            md.append(f"| {r['id']} | {TIER_LABEL[r['tier']]} | {r['q']} | "
                      f"{'✅' if r['ok'] else '❌ ' + r['why']} | {r['sec']}s | "
                      f"`{r['sql'][:120]}` |")
        with open(os.path.join(REPORT_DIR, f"nl2sql-{stamp}.md"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(md))
        print(f"\n报告已落盘：{REPORT_DIR}")

    con.close()
    return 0 if acc >= 0.9 else 1


if __name__ == "__main__":
    sys.exit(main())
