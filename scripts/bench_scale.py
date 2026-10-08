# -*- coding: utf-8 -*-
"""系统效率与伸缩性基准（对应初赛评分表「系统效率设计证明」5 分）。

评分表要求三件事，这个脚本一次给出前两件的**实测数据**：
  ① 各模块时间复杂度理论分析
  ② 至少两种数据规模下的响应性能对比
  ③ 瓶颈与优化策略（脚本里做了「加索引前/后」与「词法召回 O(N) 扫描」的对照实验）

测量对象：
  A. 问数链路 —— schema 体积、SQL 生成（LLM）、SQL 执行（1 千行 / 1 万行 / 10 万行）
  B. RAG 链路 —— 索引构建吞吐、检索延迟随语料规模（200/1000/4000 页）的变化，
                 并把「向量召回」与「词法召回」拆开计时，定位真正的瓶颈

用法：
    python scripts/bench_scale.py --sql            # 只跑问数链路（快）
    python scripts/bench_scale.py --rag            # 只跑 RAG 链路（慢，要建索引）
    python scripts/bench_scale.py --all --save
"""
import argparse
import json
import os
import shutil
import sqlite3
import statistics
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from config import Config  # noqa: E402

BENCH_DIR = os.path.join(ROOT, "data", "bench")
REPORT_DIR = os.path.join(ROOT, "data", "eval", "reports")
OHR = r"D:\硬件数据\deepseek-harness-project\中电子云\data\ohr"

SQL_SCALES = [1000, 10000, 100000]
RAG_SCALES = [200, 1000, 4000]
REPEAT = 3

QUERIES = [
    ("点查（按姓名，无索引则全表扫）",
     "SELECT 综合素质测评成绩 FROM 综合素质测评情况 WHERE 姓名='胡子扬'"),
    ("全表计数",
     "SELECT COUNT(*) FROM 综合素质测评情况"),
    ("排序取前 10（无法走索引）",
     "SELECT 姓名 FROM 综合素质测评情况 ORDER BY 智育测评分 DESC LIMIT 10"),
    ("分组聚合",
     "SELECT 行政班, AVG(综合素质测评成绩) FROM 综合素质测评情况 GROUP BY 行政班"),
]


def timeit(fn, repeat=REPEAT):
    """跑 repeat 次，返回 (中位数, 最小值)"""
    ts = []
    for _ in range(repeat):
        t = time.perf_counter()
        fn()
        ts.append(time.perf_counter() - t)
    return statistics.median(ts), min(ts)


# ==================== A. 问数链路 ====================
def build_scaled_db(n: int) -> str:
    """以 chinook.sqlite 为基底，叠加 n 行的综测表。

    注意：列必须带**声明类型**（从源库 PRAGMA 里取），不能建成无类型列。
    实测无类型列会让 langchain 的 schema 反射拿不到列名，
    `get_table_info()` 会生成 `SELECT  FROM ...` 直接语法报错 ——
    而真实库（pandas to_sql 建的）列类型齐全，所以这里是「让基准库结构
    与生产库一致」而不是绕过问题。
    """
    os.makedirs(BENCH_DIR, exist_ok=True)
    dst = os.path.join(BENCH_DIR, f"agent_{n}.sqlite")
    if os.path.exists(dst):
        os.remove(dst)
    shutil.copyfile(Config.SQL_BASE_DB_PATH, dst)

    src = sqlite3.connect("file:" + Config.SQL_DB_PATH.replace("\\", "/") +
                          "?mode=ro&immutable=1", uri=True)
    info = src.execute('PRAGMA table_info("综合素质测评情况")').fetchall()
    names = [r[1] for r in info]
    types = [r[2] or "TEXT" for r in info]
    base = src.execute('SELECT * FROM 综合素质测评情况').fetchall()
    src.close()

    con = sqlite3.connect(dst)
    con.execute('DROP TABLE IF EXISTS 综合素质测评情况')
    coldef = ", ".join(f'"{c}" {t}' for c, t in zip(names, types))
    con.execute(f'CREATE TABLE 综合素质测评情况 ({coldef})')
    ph = ", ".join("?" * len(names))
    batch, written, k = [], 0, 0
    while written < n:
        for row in base:
            if written >= n:
                break
            r = list(row)
            # 让姓名/学号唯一，否则点查会命中多行、破坏「执行准确率」口径
            r[0] = int(row[0]) + k * 1000000
            r[1] = f"{row[1]}{k}" if k else row[1]
            batch.append(r)
            written += 1
        k += 1
    con.executemany(f"INSERT INTO 综合素质测评情况 VALUES ({ph})", batch)
    con.commit()
    rows = con.execute('SELECT COUNT(*) FROM 综合素质测评情况').fetchone()[0]
    con.close()
    print(f"  [bench] 建库 {os.path.basename(dst)}: {rows} 行")
    return dst


def bench_sql(scales):
    from langchain_community.utilities import SQLDatabase
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(model=Config.LLM_MODEL, api_key=Config.LLM_API_KEY,
                     base_url=Config.LLM_BASE_URL, temperature=0)
    out = []
    for n in scales:
        print(f"\n--- 规模 {n} 行 ---")
        path = build_scaled_db(n)
        db = SQLDatabase.from_uri(f"sqlite:///file:{path}?mode=ro&uri=true")
        schema = db.get_table_info()
        t_schema, _ = timeit(lambda: db.get_table_info(), 3)

        row = {"scale": n, "schema_chars": len(schema), "schema_ms": t_schema * 1000,
               "queries": []}
        print(f"  schema {len(schema)} 字符，取 schema {t_schema*1000:.1f} ms")

        for label, sql in QUERIES:
            med, best = timeit(lambda q=sql: db._execute(q, fetch="all"), REPEAT)
            k = db._execute(f"EXPLAIN QUERY PLAN {sql}", fetch="all")
            row["queries"].append({"label": label, "sql": sql,
                                   "ms": med * 1000, "best_ms": best * 1000,
                                   "plan": " | ".join(str(list(d.values())[-1]) for d in k)})
            print(f"  {label:26s} {med*1000:8.2f} ms   {row['queries'][-1]['plan'][:70]}")

        # LLM 生成一条 SQL 的耗时（受 schema 体积影响）。
        # 容错：接口欠费/限流时不能让整个基准挂掉 —— SQL 执行耗时是本基准的核心，
        # 不依赖外部接口，必须照常产出。
        prompt = (f"根据数据库 schema 和用户问题，生成一条 SQLite 查询语句。\n\n"
                  f"【数据库 Schema】\n{schema}\n\n【用户问题】\n智育测评分前十是谁？\n\n"
                  f"【要求】只输出 SQL 语句")
        try:
            t_gen, _best = timeit(lambda: llm.invoke(prompt), 2)
            row["sql_gen_ms"] = t_gen * 1000
            print(f"  LLM 生成 SQL（prompt {len(schema)} 字符）: {t_gen*1000:.0f} ms")
        except Exception as e:
            row["sql_gen_ms"] = None
            print(f"  LLM 生成 SQL 失败（跳过该项）: {type(e).__name__}: {str(e)[:100]}")

        # 优化对照：加索引后点查
        con = sqlite3.connect(path)
        con.execute('CREATE INDEX IF NOT EXISTS idx_name ON 综合素质测评情况("姓名")')
        con.execute('CREATE INDEX IF NOT EXISTS idx_zy ON 综合素质测评情况("智育测评分")')
        con.commit()
        con.close()
        db2 = SQLDatabase.from_uri(f"sqlite:///file:{path}?mode=ro&uri=true")
        opt = []
        for label, sql in (QUERIES[0], QUERIES[2]):
            med, _ = timeit(lambda q=sql: db2._execute(q, fetch="all"), REPEAT)
            opt.append({"label": label, "ms": med * 1000})
            print(f"  [加索引] {label:20s} {med*1000:8.2f} ms")
        row["indexed"] = opt
        out.append(row)
    return out


# ==================== B. RAG 链路 ====================
def build_rag_corpus(n_pages: int, dest: str) -> str:
    """从 OHR-Bench 的 gt 检索基里抽 n_pages 页，物化成独立知识库"""
    import zipfile
    if os.path.isdir(dest):
        have = len(os.listdir(dest))
        if have == n_pages:
            return dest
        shutil.rmtree(dest)
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(os.path.join(OHR, "retrieval.zip")) as z:
        names = [x for x in z.namelist() if x.startswith("gt/") and x.endswith(".json")]
        names.sort()
        written = 0
        for name in names:
            if written >= n_pages:
                break
            with z.open(name) as fh:
                pages = json.loads(fh.read().decode("utf-8", "replace"))
            for p in (pages or []):
                if written >= n_pages:
                    break
                txt = (p.get("text") or "").strip()
                if not txt:
                    continue
                stem = name[3:-5].replace("/", "_")
                fn = f"{stem}__p{int(p.get('page_idx', 0)):04d}.md"
                with open(os.path.join(dest, fn), "w", encoding="utf-8") as f:
                    f.write(txt + "\n")
                written += 1
    print(f"  [bench] 语料 {n_pages} 页 -> {dest}（实际 {written}）")
    return dest


def bench_rag(scales):
    from tools.rag_tool import RAGTool
    out = []
    for n in scales:
        print(f"\n--- 语料规模 {n} 页 ---")
        kb = build_rag_corpus(n, os.path.join(BENCH_DIR, f"kb_{n}"))
        persist = os.path.join(BENCH_DIR, f"vs_{n}")
        t0 = time.perf_counter()
        rag = RAGTool(data_dir=kb, persist_dir=persist)
        build_s = time.perf_counter() - t0
        chunks = rag.vector_store._collection.count()
        print(f"  索引: {chunks} 块，构建 {build_s:.1f}s"
              f"（{chunks/max(build_s,1e-6):.1f} 块/秒）")

        qs = ["What was the total interest cost on debt balances for the year 2020?",
              "How many shares of common stock were outstanding as of June 30, 2023?",
              "What technology allows 5G service to run simultaneously with 4G?"]
        vec_t, lex_t, tot_t = [], [], []
        for q in qs:
            med, _ = timeit(lambda qq=q: rag.vector_store.similarity_search_with_relevance_scores(
                qq, k=8), 2)
            vec_t.append(med * 1000)
            med, _ = timeit(lambda qq=q: rag._lexical_hits(qq), 2)
            lex_t.append(med * 1000)
            med, _ = timeit(lambda qq=q: rag.search(qq), 2)
            tot_t.append(med * 1000)
        row = {"pages": n, "chunks": chunks, "build_s": build_s,
               "build_per_chunk_ms": build_s / max(chunks, 1) * 1000,
               "vector_ms": statistics.median(vec_t),
               "lexical_ms": statistics.median(lex_t),
               "search_ms": statistics.median(tot_t)}
        print(f"  检索延迟中位数：向量 {row['vector_ms']:.0f} ms | "
              f"词法 {row['lexical_ms']:.0f} ms | search 合计 {row['search_ms']:.0f} ms")
        out.append(row)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sql", action="store_true")
    ap.add_argument("--rag", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()
    do_sql = args.sql or args.all or not (args.sql or args.rag)
    do_rag = args.rag or args.all

    print("=" * 78)
    print("系统效率与伸缩性基准")
    print("=" * 78)
    res = {}
    if do_sql:
        print("\n########## A. 问数链路 ##########")
        res["sql"] = bench_sql(SQL_SCALES)
    if do_rag:
        print("\n########## B. RAG 链路 ##########")
        res["rag"] = bench_rag(RAG_SCALES)

    print()
    print("=" * 78)
    print("### 汇总")
    if res.get("sql"):
        print("\n问数链路")
        print("| 数据规模 | schema 字符 | 取 schema | 点查 | 计数 | 排序Top10 | 分组聚合 | LLM 生成 SQL |")
        print("|---|---|---|---|---|---|---|---|")
        for r in res["sql"]:
            q = {x["label"]: x["ms"] for x in r["queries"]}
            vals = list(q.values())
            gen = f"{r['sql_gen_ms']:.0f} ms" if r.get("sql_gen_ms") else "n/a"
            print(f"| {r['scale']:,} 行 | {r['schema_chars']} | {r['schema_ms']:.1f} ms | "
                  + " | ".join(f"{v:.1f} ms" for v in vals)
                  + f" | {gen} |")
        print("\n加索引后对照")
        print("| 数据规模 | 点查 | 排序Top10 |")
        print("|---|---|---|")
        for r in res["sql"]:
            o = {x["label"]: x["ms"] for x in r["indexed"]}
            print(f"| {r['scale']:,} 行 | " + " | ".join(f"{v:.1f} ms" for v in o.values()) + " |")
    if res.get("rag"):
        print("\nRAG 链路")
        print("| 语料页数 | 索引块数 | 建索引用时 | 向量召回 | 词法召回 | search 合计 |")
        print("|---|---|---|---|---|---|")
        for r in res["rag"]:
            print(f"| {r['pages']} | {r['chunks']} | {r['build_s']:.1f} s | "
                  f"{r['vector_ms']:.0f} ms | {r['lexical_ms']:.0f} ms | {r['search_ms']:.0f} ms |")

    if args.save:
        os.makedirs(REPORT_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        with open(os.path.join(REPORT_DIR, f"bench-{stamp}.json"), "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=2)
        print(f"\n已落盘：{REPORT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
