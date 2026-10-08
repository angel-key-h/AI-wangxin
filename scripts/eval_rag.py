# -*- coding: utf-8 -*-
"""RAG 检索与问答评测（OHR-Bench 公开基准子集）。

对应赛题：
  · 功能指标 2「基础 RAG 单文档问答能力」10 分（≥10 个测试用例）
  · 性能指标 2「RAG 答案忠实度」10 分（≥70%→6 分，≥80%→8 分，≥90%→10 分）
  · 亮点预览页强制项「RAG 检索命中率」

三个指标：
  hit@1 / hit@4  —— gold 证据页是否被检索到（客观，不需要 LLM 判断）
  answer_acc     —— 生成答案与 gold 答案是否一致（LLM 裁判，短答案题也判得准）
  faithful       —— 答案是否**只依据检索到的上下文**（幻觉检测，对应「忠实度」）

用法：
    python scripts/eval_rag.py                 # 全量（含 LLM 裁判）
    python scripts/eval_rag.py --limit 20      # 先小样本试跑
    python scripts/eval_rag.py --no-judge      # 只跑确定性指标，快
    python scripts/eval_rag.py --save
退出码 0 = hit@4 ≥ 90% 且 answer_acc ≥ 90%；否则 1
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from tools.rag_tool import RAGTool  # noqa: E402

CASES_PATH = os.path.join(ROOT, "data", "eval", "rag_cases.json")
KB_DIR = os.path.join(ROOT, "data", "ohr_kb")
PERSIST_DIR = os.path.join(ROOT, "data", "vector_store_ohr")
REPORT_DIR = os.path.join(ROOT, "data", "eval", "reports")

JUDGE_PROMPT = """你是严格的评测裁判。请判断【模型答案】是否正确，以及是否忠实于【检索上下文】。

【问题】
{q}

【标准答案】
{gold}

【检索上下文】（模型能看到的全部资料）
{ctx}

【模型答案】
{ans}

请只输出一行 JSON：
{{"correct": "yes|partial|no", "faithful": "yes|no", "reason": "20 字以内"}}

判分口径：
- correct=yes：结论与标准答案一致（数值/是否/关键实体正确），措辞不同不影响
- correct=partial：部分正确或信息不完整
- correct=no：结论错误、答非所问，或声称"无法回答"而标准答案存在
- faithful=yes：答案中的事实性内容都能在检索上下文里找到；若答案编造了上下文没有的事实则为 no"""


def norm(s: str) -> str:
    s = str(s or "").lower().strip()
    s = re.sub(r"\s+", " ", s)
    return s.strip(" .,;:。；：'\"`*")


def numbers(s: str):
    """抽取所有数字（含千分位与百分号），用于数值题的宽松比对"""
    out = set()
    for m in re.finditer(r"-?\d[\d,]*\.?\d*", str(s or "")):
        t = m.group(0).replace(",", "").rstrip(".")
        try:
            out.add(round(float(t), 4))
        except ValueError:
            pass
    return out


def loose_match(gold: str, ans: str, form: str) -> bool:
    """确定性判分（不依赖 LLM）：宽松但不多数情况会误判的口径"""
    g, a = norm(gold), norm(ans)
    if not a:
        return False
    if g and g in a:
        return True
    f = (form or "").lower()
    if "yes/no" in f:
        head = a[:60]
        if g.startswith("yes"):
            return bool(re.search(r"\byes\b", head))
        if g.startswith("no"):
            return bool(re.search(r"\bno\b", head))
    if "numeric" in f:
        gn = numbers(gold)
        return bool(gn) and gn <= numbers(ans)
    # 短答案：gold 的词是否基本都出现
    toks = [t for t in re.split(r"[\s,]+", g) if len(t) > 2]
    return bool(toks) and sum(1 for t in toks if t in a) / len(toks) >= 0.8


def judge(llm, case, chunks, ans):
    ctx = "\n---\n".join(c["text"][:1200] for c in chunks)[:5000] or "（无检索结果）"
    prompt = JUDGE_PROMPT.format(q=case["question"], gold=case["answer"],
                                 ctx=ctx, ans=(ans or "")[:1500])
    try:
        raw = llm.invoke(prompt).content
        m = re.search(r"\{.*\}", raw, re.S)
        obj = json.loads(m.group(0)) if m else {}
        return (str(obj.get("correct", "")).lower(),
                str(obj.get("faithful", "")).lower(),
                str(obj.get("reason", ""))[:60])
    except Exception as e:
        return "", "", f"judge_error:{type(e).__name__}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    with open(CASES_PATH, encoding="utf-8") as fh:
        blob = json.load(fh)
    cases = blob["cases"]
    if args.limit:
        cases = cases[:args.limit]

    print("=" * 78)
    print(f"RAG 评测 · 知识库 {len([f for f in os.listdir(KB_DIR)])} 页 · "
          f"{len(cases)} 个问题 · LLM 裁判={'关' if args.no_judge else '开'}")
    print("=" * 78)

    t0 = time.time()
    rag = RAGTool(data_dir=KB_DIR, persist_dir=PERSIST_DIR)
    print(f"[准备] 知识库就绪，耗时 {time.time()-t0:.1f}s")

    def work(case):
        t = time.time()
        try:
            chunks = rag.search(case["question"])
            res = rag.query(case["question"])
            ans = res.get("answer") or ""
        except Exception as e:
            chunks, ans = [], f"<异常 {type(e).__name__}: {e}>"
        srcs = [c["source"] for c in chunks]
        return {
            "id": case["id"], "doc": case["doc"], "page": case["page"],
            "question": case["question"], "answer": case["answer"],
            "form": case.get("answer_form"), "ev": case.get("evidence_source"),
            "hit1": bool(srcs) and srcs[0] == case["kb_file"],
            "hitk": case["kb_file"] in srcs,
            "loose": loose_match(case["answer"], ans, case.get("answer_form")),
            "gen": ans, "srcs": srcs, "sec": round(time.time() - t, 2),
            "top_score": chunks[0]["score"] if chunks else None,
            "chunks": chunks,
        }

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(work, cases))

    if not args.no_judge:
        from langchain_openai import ChatOpenAI
        from config import Config
        llm = ChatOpenAI(model=Config.LLM_MODEL, api_key=Config.LLM_API_KEY,
                         base_url=Config.LLM_BASE_URL, temperature=0)

        def j(r):
            c, f, why = judge(llm, r, r["chunks"], r["gen"])
            r["correct"], r["faithful"], r["reason"] = c, f, why
            return r

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            results = list(ex.map(j, results))

    for r in results:
        mark = "✅" if r["hitk"] and (r.get("correct") == "yes" or r["loose"]) else "❌"
        print(f"{mark} [{r['ev'] or '?':8s}/{r['form'] or '?':13s}] {r['question'][:78]}")
        print(f"     期望: {str(r['answer'])[:70]!r}  |  hit@1={r['hit1']} hit@k={r['hitk']}"
              f" loose={r['loose']} judge={r.get('correct','-')} faith={r.get('faithful','-')}"
              f" ({r['sec']}s)")
        if not r["hitk"]:
            print(f"     ✗ 未命中 gold 页 | 实际来源: {[s[:46] for s in r['srcs'][:3]]}")
        elif r.get("correct") == "no":
            print(f"     ✗ 答案错误: {(r['gen'] or '')[:150]!r}  ({r.get('reason')})")

    n = len(results)
    hit1 = sum(1 for r in results if r["hit1"]) / n
    hitk = sum(1 for r in results if r["hitk"]) / n
    loose = sum(1 for r in results if r["loose"]) / n
    def pct(x): return f"{x*100:.1f}%"

    print()
    print("=" * 78)
    print("### 总指标")
    print(f"  检索命中率 hit@1 : {hit1:.3f}  ({pct(hit1)})")
    print(f"  检索命中率 hit@4 : {hitk:.3f}  ({pct(hitk)})")
    print(f"  答案宽松匹配率   : {loose:.3f}  ({pct(loose)})")
    if not args.no_judge:
        ok = sum(1 for r in results if r.get("correct") == "yes")
        part = sum(1 for r in results if r.get("correct") == "partial")
        fa = sum(1 for r in results if r.get("faithful") == "yes")
        print(f"  答案准确率(裁判) : {ok}/{n} = {pct(ok/n)}   （partial {part}）")
        print(f"  忠实度(裁判)     : {fa}/{n} = {pct(fa/n)}")

    print()
    print("### 分证据类型")
    for ev in ("text", "table", "equation"):
        sub = [r for r in results if r["ev"] == ev]
        if sub:
            print(f"  {ev:9s} n={len(sub):3d}  hit@4={pct(sum(1 for r in sub if r['hitk'])/len(sub))}"
                  f"  loose={pct(sum(1 for r in sub if r['loose'])/len(sub))}"
                  + (f"  judge={pct(sum(1 for r in sub if r.get('correct')=='yes')/len(sub))}"
                     if not args.no_judge else ""))
    print("### 分域")
    for d in sorted({r["doc"].split("/")[0] for r in results}):
        sub = [r for r in results if r["doc"].startswith(d + "/")]
        print(f"  {d:9s} n={len(sub):3d}  hit@4={pct(sum(1 for x in sub if x['hitk'])/len(sub))}")

    ok_exit = hitk >= 0.9 and (args.no_judge or
                               sum(1 for r in results if r.get("correct") == "yes") / n >= 0.9)

    if args.save:
        os.makedirs(REPORT_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        slim = [{k: v for k, v in r.items() if k != "chunks"} for r in results]
        with open(os.path.join(REPORT_DIR, f"rag-{stamp}.json"), "w", encoding="utf-8") as fh:
            json.dump({"hit@1": hit1, "hit@4": hitk, "loose": loose,
                       "results": slim}, fh, ensure_ascii=False, indent=2)
        md = [f"# RAG 评测报告（{stamp}）", "",
              f"- 知识库：{len(os.listdir(KB_DIR))} 页（OHR-Bench gt 子集，{blob['meta']['docs']} 份文档）",
              f"- 问题数：{n}", "",
              "| 指标 | 数值 |", "|---|---|",
              f"| 检索命中率 hit@1 | {pct(hit1)} |",
              f"| 检索命中率 hit@4 | {pct(hitk)} |",
              f"| 答案宽松匹配率 | {pct(loose)} |"]
        if not args.no_judge:
            md.append(f"| 答案准确率（LLM 裁判） | "
                      f"{pct(sum(1 for r in results if r.get('correct')=='yes')/n)} |")
            md.append(f"| 忠实度（LLM 裁判） | "
                      f"{pct(sum(1 for r in results if r.get('faithful')=='yes')/n)} |")
        md += ["", "| 证据类型 | 问题数 | hit@4 | 宽松匹配 | 裁判正确 |", "|---|---|---|---|---|"]
        for ev in ("text", "table", "equation"):
            sub = [r for r in results if r["ev"] == ev]
            if sub:
                md.append(f"| {ev} | {len(sub)} | "
                          f"{pct(sum(1 for r in sub if r['hitk'])/len(sub))} | "
                          f"{pct(sum(1 for r in sub if r['loose'])/len(sub))} | "
                          + (pct(sum(1 for r in sub if r.get('correct')=='yes')/len(sub))
                             if not args.no_judge else "-") + " |")
        with open(os.path.join(REPORT_DIR, f"rag-{stamp}.md"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(md))
        print(f"\n报告已落盘：{REPORT_DIR}")

    print()
    print("✅ 达标" if ok_exit else "❌ 未达标（目标 hit@4 ≥ 90% 且 答案准确率 ≥ 90%）")
    return 0 if ok_exit else 1


if __name__ == "__main__":
    sys.exit(main())
