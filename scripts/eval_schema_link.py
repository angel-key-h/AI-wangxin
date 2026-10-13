# -*- coding: utf-8 -*-
"""Schema Linking 评测：检索出的表是否覆盖回答该问题所需的表。

用途：
  1. 调参时看 top1 / recall@k 的变化，避免靠感觉改权重
  2. 赛题要求提交「实现效果关键数据」，这份评测就是中级难题 4 的量化证据

用法：
    python scripts/eval_schema_link.py            # 用内置评测集
    python scripts/eval_schema_link.py --top-k 6
    python scripts/eval_schema_link.py --no-vector   # 关掉向量，只看词法能力

评测集的口径：**每道题的期望表是「回答该问题必须用到的表」的集合**。
只要 top1 落在期望集合里就算 top1 命中；期望集合与召回结果有交集就算 recall 命中。
多表 JOIN 的题目期望集合会有多张表（如「销售额最高的客户」需要 Invoice + Customer）。
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

from _preflight import model_provenance, require_keys    # noqa: E402
from tools.schema_linker import SchemaLinker              # noqa: E402
from tools.sql_tool import SQLTool                        # noqa: E402

# (问题, 期望表集合)
CASES = [
    # ── 中文数据表（综测），考察中文列名匹配 ──
    ("智育前十是谁", {"综合素质测评情况"}),
    ("各班平均分是多少", {"综合素质测评情况"}),
    ("有多少人参评", {"综合素质测评情况"}),
    ("综测排名第一的是谁", {"综合素质测评情况"}),
    ("胡子扬的智育测评分是多少", {"综合素质测评情况"}),
    ("行政班 2112506 有多少人", {"综合素质测评情况"}),
    ("综合素质测评成绩最高的人", {"综合素质测评情况"}),
    ("德育测评分排名第 3 的是谁", {"综合素质测评情况"}),
    # ── Chinook（英文库），考察跨语言与多表关联 ──
    ("哪个歌手拥有的专辑最多", {"Artist", "Album"}),
    ("销售额最高的客户是谁", {"Customer", "Invoice", "InvoiceLine"}),
    ("公司有多少员工", {"Employee"}),
    ("播放列表里一共有多少首歌", {"Playlist", "PlaylistTrack"}),
    ("每张专辑有多少首曲目", {"Album", "Track"}),
    ("哪个曲风最流行", {"Genre", "Track", "InvoiceLine"}),
    ("客户主要来自哪些国家", {"Customer"}),
    # ── 英文直问（跨语言：提问语言直接换成英文库的语言）──
    # 专门盯英文词形归一。此前 _grams() 只做整词小写，"customers" 与表名
    # "Customer" 因单复数差异零命中 → 全表同分 → 退化成表名字母序，
    # top1 变成 Album。这几条是那次修复的回归用例。
    ("How many customers are there", {"Customer"}),
    ("How many employees do we have", {"Employee"}),
    ("How many tracks are there", {"Track"}),
    ("List all albums", {"Album"}),
    ("Which genres are the most popular", {"Genre", "Track", "InvoiceLine"}),
]

# 大小写一致性断言（与上面的准确率**分开报**，不混进 top1 命中率的口径）。
#
# 为什么需要单独一节：样本值匹配曾用 `s in question` 做**大小写敏感**的子串判断，
# 于是 "How many employees do we have"（小写）与 "How many Employees do we have"
# （首字母大写）会选出不同的表 —— 同义问题不同答案。而且小写那次之所以"对"，
# 只是因为漏判了 finance 表的样本值 "Employees"（它是列名的复述，不是实体），
# 并非推理正确。这类"靠漏判侥幸答对"必须能被断言挡住，否则下次静默复发。
#
# 断言内容：把每条英文用例变换大小写后重跑，top1 必须与原始写法**完全一致**。
CONSISTENCY_CASES = [q for q, _ in CASES if q.isascii()]


def make_linker(use_vector: bool) -> SchemaLinker:
    """构建 SchemaLinker。

    两条外部依赖都必须能**独立降级**：
      · LLM 只用于一次性生成中文语义画像（结果缓存到 data/schema_profile.json），
        缺 key 或调用失败时退回缓存 / 纯词法，不该让整个评测崩掉；
      · 向量仅在 `--no-vector` 之外构建。

    此前这里无条件 new 一个 ChatOpenAI，于是标称「只看词法」的 `--no-vector`
    在没有 API Key 的机器上依然直接抛异常 —— 与它的说明完全不符。
    """
    from config import Config
    tool = SQLTool()
    llm = None
    if Config.LLM_API_KEY:
        try:
            from langchain_openai import ChatOpenAI
            llm = ChatOpenAI(model=Config.LLM_MODEL, api_key=Config.LLM_API_KEY,
                             base_url=Config.LLM_BASE_URL, temperature=0)
        except Exception as e:
            print(f"[警告] LLM 不可用（{type(e).__name__}: {e}），"
                  "跳过语义画像生成、改用已有缓存\n")
    else:
        print("[提示] 未配置 LLM_API_KEY：跳过语义画像生成，"
              "改用 data/schema_profile.json 的已有缓存\n")

    emb = None
    if use_vector:
        try:
            from langchain_openai import OpenAIEmbeddings
            emb = OpenAIEmbeddings(
                model=Config.EMBEDDING_MODEL,
                api_key=Config.EMBEDDING_API_KEY,
                base_url=Config.EMBEDDING_BASE_URL,
                # 非 OpenAI 官方端点必须关掉 token 预编码（与 rag_tool / sql_tool 口径一致）
                check_embedding_ctx_length=False,
            )
        except Exception as e:
            print(f"[警告] 向量不可用（{type(e).__name__}），退化为纯词法\n")
    return SchemaLinker(tool.db, emb, llm)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=6)
    ap.add_argument("--no-vector", action="store_true", help="只测词法")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--save", action="store_true", help="把评测结果落盘为 JSON 报告")
    args = ap.parse_args()

    # 向量模式需要 Embedding Key。缺 Key 时必须**在开跑前**退出（退出码 2），
    # 因为 embedding 调用失败会被 SchemaLinker 静默吞掉，结果与纯词法完全相同，
    # 但报告会被标成 "lexical+vector" —— 等于交出一份自述错误的证据。
    # 只想看可离线复现的纯词法口径，显式加 --no-vector 即可。
    if not args.no_vector:
        rc = require_keys("Schema Linking 的向量通道需要 Embedding API Key；"
                          "若只想跑可离线复现的纯词法口径，请加 --no-vector")
        if rc:
            return rc

    lk = make_linker(not args.no_vector)
    tables = lk.list_tables()
    print(f"库中共 {len(tables)} 张表：{tables}")
    print(f"模式：{'词法 + 向量' if not args.no_vector else '仅词法'}   top_k={args.top_k}")
    print("=" * 74)

    top1_ok = recall_ok = 0
    results = []
    for q, exp in CASES:
        rank = lk.select(q, top_k=args.top_k)
        got = [r[0] for r in rank]
        got_set = set(got)
        hit1 = bool(got) and got[0] in exp
        hitr = bool(exp & got_set)
        top1_ok += hit1
        recall_ok += hitr
        results.append({
            "q": q, "expect": sorted(exp), "got": got,
            "top1": bool(hit1), "recall": bool(hitr),
            "top_rank": [{"table": t, "score": round(s, 3), "why": w}
                         for t, s, w in rank[:4]],
        })

        flag = "OK  " if hit1 else ("MISS" if not hitr else "半中")
        print(f"{flag} {q}")
        if args.verbose or not hit1:
            print(f"      期望 {sorted(exp)}")
            print(f"      实得 " + " | ".join(f"{n}({s:.2f})" for n, s, _ in rank[:4]))
            top = rank[0] if rank else ("-", 0, "")
            print(f"      理由 {top[2]}")
    n = len(CASES)
    print("=" * 74)
    print(f"top1 命中率  {top1_ok}/{n} = {top1_ok / n * 100:.1f}%")
    print(f"recall@{args.top_k}  {recall_ok}/{n} = {recall_ok / n * 100:.1f}%")

    # ── 大小写一致性断言（独立一节，不并入上面的命中率）──
    print()
    print("=" * 74)
    print("大小写一致性断言（同一句的三种写法必须给出同一个 top1）")
    print("=" * 74)
    cons_rows, cons_ok = [], 0
    for q in CONSISTENCY_CASES:
        variants = {"原句": q, "首字母大写": q.title(), "全大写": q.upper()}
        tops = {k: ([r[0] for r in lk.select(v, top_k=args.top_k)][:1] or [None])[0]
                for k, v in variants.items()}
        same = len(set(tops.values())) == 1
        cons_ok += same
        cons_rows.append({"q": q, "tops": tops, "consistent": same})
        if args.verbose or not same:
            mark = "✅" if same else "❌"
            print(f"{mark} {q}")
            if not same:
                for k, v in tops.items():
                    print(f"      {k:6s} → {v}")
    print(f"一致率  {cons_ok}/{len(cons_rows)}"
          f" = {cons_ok / len(cons_rows) * 100:.1f}%"
          if cons_rows else "（无英文用例，跳过）")

    # ── 模式按**实测**写进报告，而不是按命令行传了什么 ──
    # 向量通道可能因为接口报错 / 额度不足而静默退化成纯词法（此时分数与纯词法
    # 逐项相同）。若照抄 flag 标成 lexical+vector，等于交一份自述错误的证据。
    # 判据是 SchemaLinker.vec_ok_count：真正算出向量分的查询次数。
    vec_engaged = (not args.no_vector) and lk.vec_ok_count > 0
    if not args.no_vector and not vec_engaged:
        print()
        print("⏭️  向量通道未生效（0 次成功的 embedding 调用）——")
        print("    本次结果实为**纯词法**，报告将如实标为 lexical，不会标成 lexical+vector。")
        print("    请检查 EMBEDDING_API_KEY / EMBEDDING_BASE_URL 后重跑。")
    mode = "lexical+vector" if vec_engaged else "lexical"

    # 落盘：中级难题 4 要求提交「实现效果关键数据」，没有报告就等于没有证据。
    # 文件名带模式前缀，方便把「仅词法」与「词法+向量」两份摆在一起对比。
    if args.save:
        os.makedirs(REPORT_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(REPORT_DIR, f"schemalink-{mode}-{stamp}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({
                "mode": mode, "top_k": args.top_k,
                # 向量实际生效的查询次数：0 表示 mode 是退化的 lexical
                "vector_engaged_queries": lk.vec_ok_count,
                # 本次实际使用的模型与端点。这一份尤其需要它：`mode` 是否为
                # lexical+vector 完全取决于**用了哪个 embedding 模型/提供方**，
                # 而不同提供方的向量维度与语义都不通用 —— 不记下来，
                # 这份 20/20 就说不清是在什么条件下取得的。
                "env": model_provenance(),
                "n_tables": len(tables), "n": n,
                "top1_hit": top1_ok, "top1_rate": top1_ok / n,
                "recall_hit": recall_ok, "recall_rate": recall_ok / n,
                "consistency": {
                    "n": len(cons_rows), "ok": cons_ok,
                    "rate": (cons_ok / len(cons_rows)) if cons_rows else None,
                    "cases": cons_rows,
                },
                "cases": results,
            }, fh, ensure_ascii=False, indent=2)
        print(f"\n报告已落盘：{path}")

    # 退出码：准确率与一致性**都**要过关，否则任一项退化都算失败。
    # 这里用 0/1 表达，方便 CI 判断；一致性用 len(cons_rows) 为 0 时跳过。
    acc_ok = top1_ok / n >= 0.9
    cons_all_ok = (not cons_rows) or cons_ok == len(cons_rows)
    return 0 if (acc_ok and cons_all_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
