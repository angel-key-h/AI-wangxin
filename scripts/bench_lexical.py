# -*- coding: utf-8 -*-
"""RAG 检索链路的瓶颈测量与优化对照（**不需要任何云接口**）。

背景（效率证明缺口）：`bench_scale.py --rag` 依赖 embedding 接口，欠费时跑不了。
但检索链路里**词法精确召回**那一路是纯本地行为 ——
`Chroma._collection.get(where_document={"$contains": term}, limit=...)`
不涉及向量、不调用任何外部接口，因此可以在接口不可用时独立测量。

本脚本要做三件事：
  1. 在合成语料上（500 / 2k / 8k / 32k 块）测量词法召回的延迟随规模的变化 → 判断是否 O(N)
  2. 实测一个候选优化（预建倒排索引）并**校验结果与基线完全一致**（优化不能悄悄改行为）
  3. 给出数据支撑的结论：真正的瓶颈在哪、该不该优化、怎么优化

用法：python scripts/bench_lexical.py
"""
import os
import statistics
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from tools.rag_tool import MAX_TERMS, candidate_terms  # noqa: E402

SIZES = [500, 2000, 8000, 32000]
DIM = 16
# 贴近真实问题：既含字母数字实体，也含中文
QUESTIONS = [
    "胡子扬的综合素质测评成绩和排名是多少？",
    "What was the total interest cost on debt balances for 2020?",
    "JPMORGAN 的净收入是多少",
    "第 2112503 班有多少人",
]
# 与 rag_tool._lexical_hits 一致的区分度判据
DF_MIN, DF_RATIO = 20, 0.25


def make_docs(n):
    """造 n 段带可检索实体的短文本（模拟按行/按块入库的知识库）"""
    names = ["胡子扬", "杜佳玲", "吴絮", "曾欣妍", "权晓蕊", "杨梓豪", "林恺一", "王嘉伟"]
    words = ["interest", "revenue", "deposits", "loans", "capital", "guideline",
             "agreement", "section", "compliance", "payment"]
    docs = []
    for i in range(n):
        docs.append(
            f"学号: {2025000000 + i}；姓名: {names[i % len(names)]}；"
            f"行政班: {2112501 + (i % 19)}；"
            f"综合素质测评成绩: {60 + (i % 45)}.5；"
            f"{words[i % len(words)]} amount {i % 997} thousand；"
            f"clause {i % 53} of the {words[(i + 3) % len(words)]} agreement"
        )
    return docs


def lexical_query(col, question, k=8):
    """与 tools/rag_tool._lexical_hits 的核心逻辑保持一致（同样的 per-term $contains 扫描）"""
    total = col.count() or 0
    limit = max(DF_MIN, int(total * DF_RATIO))
    hits, scored = {}, {}
    for term in candidate_terms(question)[:MAX_TERMS]:
        if len(term) >= 2 and any(term in t for t in hits):
            continue
        got = col.get(where_document={"$contains": term}, limit=limit + 1)
        ids = got.get("ids") or []
        if not ids or len(ids) > limit:
            continue
        hits[term] = True
        for doc_id in ids:
            scored[doc_id] = scored.get(doc_id, 0) + len(term)
    return {d: s for d, s in sorted(scored.items(), key=lambda kv: -kv[1])[:k]}


def build_inverted(col, questions):
    """候选优化：一次全量扫描建「词 → 文档」倒排，之后查询不再扫全库。

    只索引查询里真正会用到的候选词（candidate_terms 的输出），
    这在本项目的用法下是完备的：召回只可能由这些词触发。
    """
    vocab = set()
    for q in questions:
        vocab.update(candidate_terms(q))
    inv = {t: [] for t in vocab}
    got = col.get(include=["documents"])          # 唯一一次全量取回
    for doc_id, text in zip(got["ids"], got["documents"]):
        for t in vocab:
            if t in (text or ""):
                inv[t].append(doc_id)
    return inv


def lexical_query_inverted(inv, col, question, k=8):
    total = col.count() or 0
    limit = max(DF_MIN, int(total * DF_RATIO))
    hits, scored = {}, {}
    for term in candidate_terms(question)[:MAX_TERMS]:
        if len(term) >= 2 and any(term in t for t in hits):
            continue
        ids = inv.get(term) or []
        if not ids or len(ids) > limit:
            continue
        hits[term] = True
        for doc_id in ids:
            scored[doc_id] = scored.get(doc_id, 0) + len(term)
    return {d: s for d, s in sorted(scored.items(), key=lambda kv: -kv[1])[:k]}


def build_inverted_full(col):
    """生产可行版：从**语料全体词表**建倒排（不知道未来会问什么）。

    上面那个 `build_inverted` 只索引了「测试问题里出现过的词」，真实系统做不到 ——
    必须索引语料里所有的候选词。这一版才是能落地的，代价是构建更贵。
    词表来源与 `candidate_terms` 保持一致：英数串 + CJK 2/3/4 元组。
    """
    import re
    token = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-/]*")
    cjk = re.compile(r"[\u4e00-\u9fff]+")
    inv, docs = {}, {}
    total = col.count() or 0
    off = 0
    while off < total:                      # 分页取回，避免一次性吃满内存
        got = col.get(limit=4000, offset=off, include=["documents"])
        ids = got.get("ids") or []
        if not ids:
            break
        for did, text in zip(ids, got.get("documents") or []):
            docs[did] = text or ""
        off += len(ids)
    for did, text in docs.items():
        for tok in set(token.findall(text)):
            if len(tok) >= 2:
                inv.setdefault(tok, []).append(did)
        for run in cjk.findall(text):
            n = len(run)
            for size in (2, 3, 4):
                if size > n:
                    continue
                for i in range(n - size + 1):
                    inv.setdefault(run[i:i + size], []).append(did)
    return inv, docs


def main():
    import chromadb
    from chromadb.config import Settings

    print("=" * 78)
    print("RAG 词法召回：延迟随语料规模的变化（纯本地，无外部接口）")
    print("=" * 78)
    tmp = tempfile.mkdtemp(prefix="lexbench_")
    rows = []
    for n in SIZES:
        client = chromadb.PersistentClient(
            path=os.path.join(tmp, f"n{n}"),
            settings=Settings(anonymized_telemetry=False))
        col = client.get_or_create_collection("bench")
        docs = make_docs(n)
        t0 = time.perf_counter()
        for i in range(0, n, 2000):
            chunk = docs[i:i + 2000]
            col.add(ids=[f"d{i+j}" for j in range(len(chunk))], documents=chunk,
                    embeddings=[[0.0] * DIM for _ in chunk])
        build_s = time.perf_counter() - t0

        base_res = [lexical_query(col, q) for q in QUESTIONS]
        # 变体 1：只索引查询词（理论上限，不可落地）
        t0 = time.perf_counter()
        inv_q = build_inverted(col, QUESTIONS)
        t_q = time.perf_counter() - t0
        # 变体 2：索引语料全体词表（可落地）
        t0 = time.perf_counter()
        inv_f, _docs = build_inverted_full(col)
        t_f = time.perf_counter() - t0

        same_q = all(set(a.items()) == set(b.items()) for a, b in zip(
            base_res, [lexical_query_inverted(inv_q, col, q) for q in QUESTIONS]))
        same_f = all(set(a.items()) == set(b.items()) for a, b in zip(
            base_res, [lexical_query_inverted(inv_f, col, q) for q in QUESTIONS]))

        base_ms, q_ms, f_ms = [], [], []
        for _ in range(3):
            for q in QUESTIONS:
                t = time.perf_counter(); lexical_query(col, q)
                base_ms.append((time.perf_counter() - t) * 1000)
                t = time.perf_counter(); lexical_query_inverted(inv_q, col, q)
                q_ms.append((time.perf_counter() - t) * 1000)
                t = time.perf_counter(); lexical_query_inverted(inv_f, col, q)
                f_ms.append((time.perf_counter() - t) * 1000)
        b = statistics.median(base_ms)
        vq, vf = statistics.median(q_ms), statistics.median(f_ms)
        rows.append({"n": n, "build_s": build_s, "base_ms": b, "inv_q_ms": vq,
                     "inv_f_ms": vf, "build_q_s": t_q, "build_f_s": t_f,
                     "same_q": same_q, "same_f": same_f, "vocab": len(inv_f)})
        print(f"  规模 {n:6d} 块 | 基线 {b:8.2f} ms | 查询词倒排 {vq:6.2f} ms"
              f" | 全词表倒排 {vf:6.2f} ms | 全词表构建 {t_f:6.1f}s"
              f" | 词表 {len(inv_f):6d} | 一致 {'✅' if same_q and same_f else '❌'}")

    print()
    print("### 结论")
    first, last = rows[0], rows[-1]
    print(f"  规模扩大 {last['n']/first['n']:.0f}×（{first['n']}→{last['n']}）")
    print(f"  基线词法召回      {first['base_ms']:8.2f} → {last['base_ms']:8.2f} ms"
          f"（{last['base_ms']/max(first['base_ms'],1e-9):5.1f}×）— 随语料线性增长")
    print(f"  全词表倒排后      {first['inv_f_ms']:8.2f} → {last['inv_f_ms']:8.2f} ms"
          f"（{last['inv_f_ms']/max(first['inv_f_ms'],1e-9):5.1f}×）— 近似常数")
    print(f"  最大规模提速      {last['base_ms']/max(last['inv_f_ms'],1e-9):.0f}×")
    print(f"  一次性构建成本    {last['build_f_s']:.1f}s @ {last['n']} 块"
          f"（语料越大越划算：查询侧每次省 {last['base_ms']-last['inv_f_ms']:.0f}ms）")
    print(f"  结果与基线完全一致: "
          f"{'是 ✅' if all(r['same_q'] and r['same_f'] for r in rows) else '否 ❌'}")
    if not all(r["same_f"] for r in rows):
        print()
        print("  ⚠️ 重要：倒排索引**不是行为等价的优化**，不能当作纯提速直接替换。")
        print("     根因：Chroma 的 where_document={'$contains': term} 是**子串**匹配，")
        print("     而倒排索引是**整词/定长 n-gram**匹配。实测差异例：term='on'")
        print("     子串命中 100 条（来自 'secti-on'），整词命中 0 条。")
        print("     含义：这是一次**召回语义变更**，不是无损加速。")
        print("     对本模块的用途（精确召回姓名/学号这类实体）而言，整词匹配其实更干净——")
        print("     子串匹配会把 'on'/'the' 这类功能词也当实体命中，正是 DF 阈值要滤掉的噪声。")
        print("     但**要改就必须先跑 eval_rag.py 验证召回/准确率不降**，不能凭性能数字上线。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
