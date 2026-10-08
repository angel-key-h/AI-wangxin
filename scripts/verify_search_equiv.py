# -*- coding: utf-8 -*-
"""验证「重排复用 query 向量」这一次优化没有改变任何检索结果。

背景：改造前一次 `RAGTool.search()` 会编码 query **两次** ——
向量召回内部一次（`similarity_search_with_relevance_scores`），
重排里又一次（`_rerank` 自己 `embed_query`），再加上候选集一次，共 3 次往返。
改造后召回改用 `similarity_search_by_vector_with_relevance_scores(qv)`，与重排共用 qv，
降到 2 次。

为什么必须单独写一个脚本证明，而不是「看代码觉得没问题」：
  · 两次编码理论上应给出同一个向量，但 embedding 接口**不保证**确定性；
    如果它不返回同一向量，召回与重排就会用两个略微不同的 query 向量，
    结果排序可能悄悄变掉 —— 这类变化在人工点几次界面上是看不出来的。
  · 省调用是**性能**改动，只有「结果完全一致 + 调用次数下降」同时成立才算成功。
    少了任何一半，这个改动就不该合。

做法：同一个进程内、同一个索引、同一批问题，分别以
`RAG_RERANK_LEGACY=0/1` 跑一遍，逐条比对返回的
(来源, 正文, 分数, 命中方式) 四元组，同时统计 embeddings 的调用次数。

用法：
    python scripts/verify_search_equiv.py
    python scripts/verify_search_equiv.py --atol 1e-9     # 分数容差
退出码 0 = 结果等价且调用次数确实下降。
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from tools import rag_tool  # noqa: E402

QUESTIONS = [
    "智育前十是谁",
    "一共有多少位客户？",
    "公司有多少员工",
    "Differential Geometry 讲了什么",
    "概率论教材里怎么定义条件期望",
    "JPMORGAN 2023 年第二季度的净收入是多少",
    "苹果产品手册里有哪些型号",
    "劳动合同里关于知识产权的条款怎么说",
]


class _Counter:
    """包住真实 embeddings，只统计调用次数，不改行为。

    计数变量不能叫 embed_query / embed_documents —— 那会覆盖同名方法
    （实例属性遮蔽类方法），调用时直接 TypeError。用 n_* 前缀隔开。
    """

    def __init__(self, inner):
        self._inner = inner
        self.n_query = 0
        self.n_docs = 0
        self.n_texts = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def embed_query(self, text):
        self.n_query += 1
        return self._inner.embed_query(text)

    def embed_documents(self, texts):
        texts = list(texts)
        self.n_docs += 1
        self.n_texts += len(texts)
        return self._inner.embed_documents(texts)


def run_once(tool, questions, legacy: bool, atol: float):
    """跑一轮，返回 (每条问题的结果, 调用计数)。

    计数必须同时挂在**两处** embeddings 上：
      · tool.embeddings              —— _rerank 自己编码 query 走这里
      · store._embedding_function    —— 向量召回内部编码 query 走这里
    只包前者的话，旧模式（召回+重排各编码一次）也只会数到 1 次/题，
    「省了调用」这件事根本观察不到 —— 第一版脚本就踩了这个坑。
    """
    rag_tool.RERANK_LEGACY = legacy
    counter = _Counter(tool.embeddings)
    store = tool.vector_store
    prev_fn = getattr(store, "_embedding_function", None)
    tool.embeddings = counter
    try:
        store._embedding_function = counter
    except Exception:
        pass
    results = []
    try:
        for q in questions:
            try:
                hits = tool.search(q)
            except Exception as e:
                hits = [{"error": f"{type(e).__name__}: {e}"}]
            results.append(hits)
    finally:
        tool.embeddings = counter._inner
        if prev_fn is not None:
            store._embedding_function = counter._inner
    return results, counter


def _key(hit: dict):
    return (hit.get("source"), hit.get("text"), hit.get("match"))


def compare(a_list, b_list, questions, atol: float):
    diffs = []
    for q, ra, rb in zip(questions, a_list, b_list):
        if len(ra) != len(rb):
            diffs.append((q, f"条数不同 {len(ra)} vs {len(rb)}"))
            continue
        for i, (x, y) in enumerate(zip(ra, rb)):
            if _key(x) != _key(y):
                diffs.append((q, f"第 {i + 1} 条不同：{(x.get('source'), x.get('match'))}"
                                 f" vs {(y.get('source'), y.get('match'))}"))
                break
            sx, sy = x.get("score"), y.get("score")
            if sx is None or sy is None:
                if sx is not sy:
                    diffs.append((q, f"第 {i + 1} 条分数一侧为空：{sx} vs {sy}"))
                    break
            elif abs(float(sx) - float(sy)) > atol:
                diffs.append((q, f"第 {i + 1} 条分数差 {abs(float(sx) - float(sy)):.3e}"
                                 f"（{sx} vs {sy}），超出容差 {atol:.0e}"))
                break
    return diffs


def main():
    import argparse
    import warnings

    # Chroma 默认 l2 距离，其 relevance 函数会给出负分，于是每个 query 都刷一条
    # UserWarning，并把整批命中文档全量打印出来 —— 淹没本脚本的输出。
    # 这是既有的、与本次改动无关的现象，这里只做脚本内静音，不改库行为。
    warnings.filterwarnings("ignore", message="Relevance scores must be between")

    ap = argparse.ArgumentParser()
    ap.add_argument("--atol", type=float, default=1e-6,
                    help="分数允许的绝对误差（浮点求和顺序不同会有极小差异）")
    ap.add_argument("--questions", type=int, default=len(QUESTIONS))
    args = ap.parse_args()
    questions = QUESTIONS[:args.questions]

    print("=" * 78)
    print("重排复用 query 向量 —— 等价性验证")
    print("=" * 78)
    tool = rag_tool.RAGTool()
    if tool.is_empty():
        print("❌ 索引为空或不可用，先用 scripts/verify_live.py 建一次索引")
        return 1
    print(f"索引就绪。用 {len(questions)} 个问题分别跑两种模式\n")

    print("── 模式 A：复用 query 向量（新，RERANK_LEGACY=0）──")
    new_res, c_new = run_once(tool, questions, legacy=False, atol=args.atol)
    print(f"   embed_query 调用 {c_new.n_query} 次，"
          f"embed_documents 调用 {c_new.n_docs} 次（合计 {c_new.n_texts} 条文本）")

    print("\n── 模式 B：各自编码（旧，RERANK_LEGACY=1）──")
    old_res, c_old = run_once(tool, questions, legacy=True, atol=args.atol)
    print(f"   embed_query 调用 {c_old.n_query} 次，"
          f"embed_documents 调用 {c_old.n_docs} 次（合计 {c_old.n_texts} 条文本）")

    print()
    print("=" * 78)
    print("比对")
    print("=" * 78)
    diffs = compare(old_res, new_res, questions, args.atol)
    n = len(questions)
    if diffs:
        print(f"❌ {len(diffs)}/{n} 条问题结果不一致：")
        for q, why in diffs[:10]:
            print(f"   · {q} → {why}")
    else:
        print(f"✅ {n}/{n} 条问题返回**完全一致**"
              f"（来源 / 正文 / 命中方式逐字相同，分数差 ≤ {args.atol:.0e}）")

    print()
    print(f"embed_query ：旧 {c_old.n_query} 次 → 新 {c_new.n_query} 次"
          f"（省 {c_old.n_query - c_new.n_query} 次）")
    print(f"embed_documents ：旧 {c_old.n_docs} 次 → 新 {c_new.n_docs} 次"
          f"（应完全相同，等价性要求候选集编码次数不变）")
    saved = c_old.n_query - c_new.n_query
    expected = n
    if saved <= 0:
        print("❌ 没有省下任何 query 编码调用 —— 优化未生效")
        return 1
    if saved != expected:
        print(f"⚠️ 省下 {saved} 次，预期 {expected} 次（每题恰好省 1 次）")
    else:
        print(f"✅ 每题恰好省 1 次 query 编码，共建 {n} 次")

    print()
    ok = not diffs and saved == expected and c_old.n_docs == c_new.n_docs
    print("✅ 等价性验证通过" if ok else "❌ 等价性验证未通过")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
