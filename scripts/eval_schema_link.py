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
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.schema_linker import SchemaLinker          # noqa: E402
from tools.sql_tool import SQLTool                    # noqa: E402

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
]


def make_linker(use_vector: bool) -> SchemaLinker:
    from config import Config
    from langchain_openai import ChatOpenAI
    tool = SQLTool()
    llm = ChatOpenAI(model=Config.LLM_MODEL, api_key=Config.LLM_API_KEY,
                     base_url=Config.LLM_BASE_URL, temperature=0)
    emb = None
    if use_vector:
        try:
            from langchain_openai import OpenAIEmbeddings
            emb = OpenAIEmbeddings(
                model=Config.EMBEDDING_MODEL,
                api_key=Config.EMBEDDING_API_KEY,
                base_url=Config.EMBEDDING_BASE_URL,
            )
        except Exception as e:
            print(f"[警告] 向量不可用（{type(e).__name__}），退化为纯词法\n")
    return SchemaLinker(tool.db, emb, llm)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=6)
    ap.add_argument("--no-vector", action="store_true", help="只测词法")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    lk = make_linker(not args.no_vector)
    tables = lk.list_tables()
    print(f"库中共 {len(tables)} 张表：{tables}")
    print(f"模式：{'词法 + 向量' if not args.no_vector else '仅词法'}   top_k={args.top_k}")
    print("=" * 74)

    top1_ok = recall_ok = 0
    for q, exp in CASES:
        rank = lk.select(q, top_k=args.top_k)
        got = [r[0] for r in rank]
        got_set = set(got)
        hit1 = bool(got) and got[0] in exp
        hitr = bool(exp & got_set)
        top1_ok += hit1
        recall_ok += hitr

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
    return 0 if top1_ok / n >= 0.9 else 1


if __name__ == "__main__":
    sys.exit(main())
