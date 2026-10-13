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
退出码 0 = hit@4 ≥ 90% 且 answer_acc ≥ 90%；否则 1；3 = 缺数据集；
      2 = LLM/Embedding 通道不可用（缺 Key **或裁判大面积失败**，如余额不足 402）。

裁判失败的处置（重要）：裁判调用失败时 answer_acc / faithful **不予采信** ——
这两个指标的分母必须只有「裁判真的判过」的题。若把失败也当成「答错」，
一次额度不足就会把 84.8% 写成 1.5%，读者会把它误读成**质量退化**。
（2026-10-13 实测踩到：DeepSeek 余额耗尽返回 402，65/66 条裁判失败，
旧代码照常打印「答案准确率 1.5% / 忠实度 1.5%」并落盘。）
确定性指标（hit@1 / hit@4 / 宽松匹配）不受影响，此时仍照常产出。
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# 裁判成功率低于这个比例，就认为裁判类指标整体不可采信（而不是"答得差"）。
# 取 0.9：留 10% 的偶发失败余量（限流/超时重试即可），超过就说明是系统性问题。
JUDGE_MIN_OK_RATE = 0.9

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
os.chdir(ROOT)

from _preflight import model_provenance, require_keys  # noqa: E402
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
        # 把**消息**也带上：只记类型（APIStatusError）无法区分「限流 429」
        # 「余额不足 402」「连接超时」—— 而这三者的处置完全不同。
        # 空字符串的 correct/faithful 在下游会被判为「失败」，不会混进"答错"。
        msg = re.sub(r"\s+", " ", str(e))[:140]
        return "", "", f"judge_error:{type(e).__name__}: {msg}"


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

    rc = require_keys("RAG 检索依赖向量模型（Embedding），问答与忠实度判定依赖 LLM。")
    if rc:
        return rc

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

    # ── 裁判成功率先行判定：它决定 answer_acc / faithful 能不能报 ──
    # 分母只算「裁判真的判过」的题。失败（余额不足 / 限流 / 超时）不进入分母，
    # 否则一次基础设施故障就会被写成一次质量退化。
    judge_errors = sum(1 for r in results
                       if str(r.get("reason", "")).startswith("judge_error:"))
    judge_ok = n - judge_errors
    judge_usable = (not args.no_judge) and (judge_ok / n >= JUDGE_MIN_OK_RATE)
    # 区分「用户主动关掉裁判」与「要了裁判但裁判跑不起来」：
    # 前者是正常模式（照旧按 hit@4 判达标），后者才是通道故障（退出码 2）。
    judge_failed_out = (not args.no_judge) and not judge_usable
    judge_den = judge_ok if judge_ok else 1     # 防 0 除
    judged = [r for r in results
              if not str(r.get("reason", "")).startswith("judge_error:")]

    print()
    print("=" * 78)
    print("### 总指标")
    print(f"  检索命中率 hit@1 : {hit1:.3f}  ({pct(hit1)})")
    print(f"  检索命中率 hit@4 : {hitk:.3f}  ({pct(hitk)})")
    print(f"  答案宽松匹配率   : {loose:.3f}  ({pct(loose)})")
    if not args.no_judge:
        if not judge_usable:
            print(f"  ⚠️ LLM 裁判失败 {judge_errors}/{n} 条"
                  f"（成功率 {pct(judge_ok / n)} < 阈值 {pct(JUDGE_MIN_OK_RATE)}）")
            print("     **答案准确率 / 忠实度不予采信** —— 裁判没跑起来时若按「答错」计，")
            print("     基础设施故障会被写成质量退化。失败原因（按出现次数）：")
            kinds = {}
            for r in results:
                why = str(r.get("reason", ""))
                if why.startswith("judge_error:"):
                    kinds[why] = kinds.get(why, 0) + 1
            for why, cnt in sorted(kinds.items(), key=lambda x: -x[1])[:3]:
                print(f"       ×{cnt:<3} {why[:110]}")
        else:
            ok = sum(1 for r in results if r.get("correct") == "yes")
            part = sum(1 for r in results if r.get("correct") == "partial")
            fa = sum(1 for r in results if r.get("faithful") == "yes")
            print(f"  答案准确率(裁判) : {ok}/{judge_ok} = {pct(ok / judge_den)}"
                  f"   （partial {part}）")
            print(f"  忠实度(裁判)     : {fa}/{judge_ok} = {pct(fa / judge_den)}")
            if judge_errors:
                print(f"  （另有 {judge_errors} 条裁判失败，已从上述分母中剔除）")

    print()
    print("### 分证据类型")
    for ev in ("text", "table", "equation"):
        sub = [r for r in results if r["ev"] == ev]
        if sub:
            print(f"  {ev:9s} n={len(sub):3d}  hit@4={pct(sum(1 for r in sub if r['hitk'])/len(sub))}"
                  f"  loose={pct(sum(1 for r in sub if r['loose'])/len(sub))}"
                  + (f"  judge={pct(sum(1 for r in sub if r.get('correct')=='yes')/len(sub))}"
                     if judge_usable else ("" if args.no_judge else "  judge=不采信")))
    print("### 分域")
    for d in sorted({r["doc"].split("/")[0] for r in results}):
        sub = [r for r in results if r["doc"].startswith(d + "/")]
        print(f"  {d:9s} n={len(sub):3d}  hit@4={pct(sum(1 for x in sub if x['hitk'])/len(sub))}")

    ok_exit = hitk >= 0.9 and (args.no_judge or
                               (judge_usable and
                                sum(1 for r in judged if r.get("correct") == "yes")
                                / judge_den >= 0.9))

    if args.save:
        os.makedirs(REPORT_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        slim = [{k: v for k, v in r.items() if k != "chunks"} for r in results]
        prov = model_provenance()

        def rate(pred, pool):
            return (sum(1 for r in pool if pred(r)) / len(pool)) if pool else None

        # answer_acc / faithful 此前只落在 .md 里，JSON 只有 hit@1/hit@4/loose ——
        # 「脚本化核对数字」这件事就做不成了（评委或后续脚本没法直接读）。
        # 既然赛题要求「实现效果关键数据」，就把聚合指标一并写进 JSON。
        by_ev, by_doc = {}, {}
        for ev in ("text", "table", "equation"):
            sub = [r for r in results if r["ev"] == ev]
            if sub:
                by_ev[ev] = {
                    "n": len(sub),
                    "hit@4": rate(lambda r: r["hitk"], sub),
                    "loose": rate(lambda r: r["loose"], sub),
                    "answer_acc": None if not judge_usable
                    else rate(lambda r: r.get("correct") == "yes",
                              [r for r in sub
                               if not str(r.get("reason", "")).startswith("judge_error:")]),
                }
        for dom in sorted({r["doc"].split("/")[0] for r in results}):
            sub = [r for r in results if r["doc"].startswith(dom + "/")]
            by_doc[dom] = {"n": len(sub), "hit@4": rate(lambda r: r["hitk"], sub)}

        with open(os.path.join(REPORT_DIR, f"rag-{stamp}.json"), "w", encoding="utf-8") as fh:
            json.dump({
                "n": n,
                "hit@1": hit1,
                "hit@4": hitk,
                "loose": loose,
                "judge_enabled": not args.no_judge,
                # 裁判失败率过高时**写 null，而不是写一个小数字**：
                # 一个「因为裁判没跑起来才算出来的 1.5%」比「没有这个数」更有害 ——
                # 它会被后续读者当成真实的质量指标引用。
                "judge_ok": None if args.no_judge else judge_ok,
                "judge_errors": None if args.no_judge else judge_errors,
                "judge_usable": judge_usable,
                "answer_acc": None if not judge_usable
                else rate(lambda r: r.get("correct") == "yes", judged),
                "answer_partial": None if not judge_usable
                else rate(lambda r: r.get("correct") == "partial", judged),
                "faithful": None if not judge_usable
                else rate(lambda r: r.get("faithful") == "yes", judged),
                # 实际使用的模型与端点：本报告最需要它 —— 裁判质量高度依赖模型，
                # 换模型会同时改变 answer_acc / faithful 的值（见 README §8）。
                "env": model_provenance(),
                "by_evidence": by_ev,
                "by_domain": by_doc,
                "results": slim,
            }, fh, ensure_ascii=False, indent=2)
        md = [f"# RAG 评测报告（{stamp}）", "",
              f"- 知识库：{len(os.listdir(KB_DIR))} 页（OHR-Bench gt 子集，{blob['meta']['docs']} 份文档）",
              f"- 问题数：{n}",
              (f"- 模型：`{prov.get('llm_model')}` @ {prov.get('llm_base_url')}（裁判）"
               f"；`{prov.get('embedding_model')}`（检索向量）") if prov else "- 模型：（未记录）",
              "",
              "| 指标 | 数值 |", "|---|---|",
              f"| 检索命中率 hit@1 | {pct(hit1)} |",
              f"| 检索命中率 hit@4 | {pct(hitk)} |",
              f"| 答案宽松匹配率 | {pct(loose)} |"]
        if not args.no_judge:
            if judge_usable:
                md.append(f"| 答案准确率（LLM 裁判） | "
                          f"{pct(sum(1 for r in judged if r.get('correct')=='yes')/judge_den)} |")
                md.append(f"| 忠实度（LLM 裁判） | "
                          f"{pct(sum(1 for r in judged if r.get('faithful')=='yes')/judge_den)} |")
                if judge_errors:
                    md.append(f"| ↳ 裁判失败（已从分母剔除） | {judge_errors}/{n} |")
            else:
                md.append(f"| 答案准确率（LLM 裁判） | **不可采信**：裁判失败 "
                          f"{judge_errors}/{n}，未达到 {pct(JUDGE_MIN_OK_RATE)} 成功率 |")
                md.append("| 忠实度（LLM 裁判） | **不可采信**（同上） |")
        md += ["", "| 证据类型 | 问题数 | hit@4 | 宽松匹配 | 裁判正确 |", "|---|---|---|---|---|"]
        for ev in ("text", "table", "equation"):
            sub = [r for r in results if r["ev"] == ev]
            if sub:
                md.append(f"| {ev} | {len(sub)} | "
                          f"{pct(sum(1 for r in sub if r['hitk'])/len(sub))} | "
                          f"{pct(sum(1 for r in sub if r['loose'])/len(sub))} | "
                          + (pct(sum(1 for r in sub if r.get('correct')=='yes')/len(sub))
                             if judge_usable
                             else ("-" if args.no_judge else "不采信")) + " |")
        with open(os.path.join(REPORT_DIR, f"rag-{stamp}.md"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(md))
        print(f"\n报告已落盘：{REPORT_DIR}")

    print()
    if judge_failed_out:
        # 不报 1（「跑完了但未达标」）—— 那会让人以为系统质量不达标。
        # 实际是**依赖的服务不可用**，确定性指标仍然有效，故与缺 Key 同为 2。
        print("⚠️ 本次只产出**确定性指标**（hit@1 / hit@4 / 宽松匹配），"
              "裁判类指标不予采信。")
        print("   退出码 2：依赖的 LLM 通道不可用（缺 Key / 额度不足 / 持续限流），"
              "恢复后重跑。")
        return 2
    print("✅ 达标" if ok_exit else "❌ 未达标（目标 hit@4 ≥ 90% 且 答案准确率 ≥ 90%）")
    return 0 if ok_exit else 1


if __name__ == "__main__":
    sys.exit(main())
