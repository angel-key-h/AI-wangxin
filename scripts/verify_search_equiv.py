# -*- coding: utf-8 -*-
"""验证「重排复用 query 向量」这一次优化没有改变任何检索结果。

背景：改造前一次 `RAGTool.search()` 会编码 query **两次** ——
向量召回内部一次（`similarity_search_with_relevance_scores`），
重排里又一次（`_rerank` 自己 `embed_query`），再加上候选集一次，共 3 次往返。
改造后召回改用 `similarity_search_by_vector_with_relevance_scores(qv)`，与重排共用 qv，
降到 2 次。

为什么必须单独写一个脚本证明，而不是「看代码觉得没问题」：
  · 省调用是**性能**改动，只有「结果完全一致 + 调用次数下降」同时成立才算成功。
    少了任何一半，这个改动就不该合。

⚠️ 关于「结果完全一致」怎么测才成立（2026-10-13 实测修正）：
  最初的版本直接拿两种模式的分数比对，容差设 1e-06，结果 5/8 条问题判为不一致。
  逐层排查后的结论是 —— **这不是本次优化造成的，而是 embedding 服务自身的性质**：

    · `embed_query`（单条）实测**完全确定**：同文本连编 4 次，最大分量差 0.000e+00；
    · `embed_documents`（批次）实测**不确定**：同一批次、同样输入连编两次，
      分量差可达 3.9e-04（12 条批次实测全批最大 6.4e-04），折成余弦差约 1e-04；
      二分实测：批次 1 条稳定，批次 2 / 4 / 8 / 12 条**全部不稳定**。

  也就是说，只要重排那一批文档被编码两次，分数就会漂 ~1e-04 ——
  这跟「query 向量复用与否」无关，容差 1e-06 落在服务噪声地板之下，
  这条断言**任何实现都不可能通过**，因此它当时证明不了任何东西。

  正确的实验设计是：把「服务噪声」这个无关变量**固定住**，只让被测变量
  （召回与重排是否共用同一个 query 向量）变化。于是本脚本给 embeddings 套一层
  **批次级记忆化**（`_CountingStableEmbeddings`）：同一批次文本第一次编码后缓存，
  再次出现时直接返回首次结果。这样
      · 两模式批次**逐字相同** → 分数**逐位相同**（断言才有意义）；
      · 两模式批次一旦不同 → 缓存不命中，差异会被如实暴露（这正是要抓的）。
  同时在报告末尾**实测并打印服务自身的抖动幅度**，让上面的结论可被复查，
  而不是靠本文件的一句自述。

做法：同一个进程内、同一个索引、同一批问题，分别以
`RAG_RERANK_LEGACY=0/1` 跑一遍，逐条比对返回的
(来源, 正文, 命中方式) 三元组是否逐字相同、分数是否在容差内，
同时统计 embeddings 的调用次数。

用法：
    python scripts/verify_search_equiv.py
    python scripts/verify_search_equiv.py --atol 1e-9     # 分数容差
退出码 0 = 结果等价且调用次数确实下降；2 = 缺 API Key。

注意：本脚本要统计 embedding 的真实调用次数，**必须联网 且 需要 EMBEDDING_API_KEY**，
没有离线降级路径。缺 Key 时给可操作提示并以退出码 2 退出，不抛 openai 的崩栈。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
os.chdir(ROOT)

from _preflight import require_keys  # noqa: E402
from tools import rag_tool  # noqa: E402
from tools.rag_tool import _cosine  # noqa: E402

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


class _CountingStableEmbeddings:
    """包住真实 embeddings：统计调用次数 + 批次级记忆化，不改单次编码行为。

    计数变量不能叫 embed_query / embed_documents —— 那会覆盖同名方法
    （实例属性遮蔽类方法），调用时直接 TypeError。用 n_* 前缀隔开。

    `_doc_cache` 的键是**整批文本的元组**（不是单条文本）：只有「这一批
    一字不差地出现过」才复用。批次是智谱接口的不确定单位（见模块 docstring），
    按批次缓存才既消掉噪声、又不掩盖「候选集变了」这类真问题。
    """

    def __init__(self, inner):
        self._inner = inner
        self.n_query = 0
        self.n_docs = 0
        self.n_texts = 0
        self.n_doc_hits = 0        # 其中命中缓存（未真正打服务）的次数
        self._doc_cache = {}
        self.batches = []          # 记录真正打过服务的批次，供事后实测服务抖动

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def embed_query(self, text):
        self.n_query += 1
        return self._inner.embed_query(text)

    def embed_documents(self, texts):
        texts = list(texts)
        self.n_docs += 1
        self.n_texts += len(texts)
        key = tuple(texts)
        cached = self._doc_cache.get(key)
        if cached is None:
            cached = self._inner.embed_documents(texts)
            self._doc_cache[key] = cached
            self.batches.append(key)
        else:
            self.n_doc_hits += 1
        return cached


def measure_service_noise(wrapper, probe_query, sample=3):
    """用真实批次实测 embedding 服务的抖动，作为「为什么需要记忆化」的客观依据。

    只对**已缓存过的批次**重编两次，不额外引入新批次；再拿一个真实 query
    向量算「同一文本两次编码会带来多大的分数漂移」，这是最能说明问题的数。
    """
    batch = next((b for b in wrapper.batches if len(b) >= 2), None)
    if batch is None:
        return None
    a = wrapper._inner.embed_documents(list(batch))
    b = wrapper._inner.embed_documents(list(batch))
    q = wrapper._inner.embed_query(probe_query)
    comp = max(max(abs(x - y) for x, y in zip(u, v)) for u, v in zip(a, b))
    drift = max(abs(_cosine(q, u) - _cosine(q, v)) for u, v in zip(a, b))
    return {"batch_size": len(batch), "max_component_delta": comp,
            "max_score_drift": drift}


def run_once(tool, questions, legacy: bool, wrapper):
    """跑一轮，返回每条问题的结果。

    计数与缓存都挂在**同一个** wrapper 上、跨两轮共用：
      · 只包 tool.embeddings 的话，旧模式（召回+重排各编码一次）也只会数到
        1 次/题，「省了调用」这件事根本观察不到 —— 第一版脚本就踩了这个坑；
      · 缓存若每轮新建，模式 B 就会重新编码同一批次、重新吃一次服务噪声，
        等价性断言又变回不可通过。所以两者都必须共用实例。
    因此 wrapper 要同时挂到 tool.embeddings 与 store._embedding_function 两处：
    前者是 _rerank 自编码 query 走的路径，后者是向量召回内部编码走的路径。
    """
    rag_tool.RERANK_LEGACY = legacy
    store = tool.vector_store
    prev_fn = getattr(store, "_embedding_function", None)
    tool.embeddings = wrapper
    try:
        store._embedding_function = wrapper
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
        tool.embeddings = wrapper._inner
        if prev_fn is not None:
            store._embedding_function = prev_fn
    return results


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
                    help="分数允许的绝对误差。服务噪声已被批次级记忆化固定，"
                         "同一批次应给出逐位相同的分数，故该容差可取得很小。")
    ap.add_argument("--questions", type=int, default=len(QUESTIONS))
    args = ap.parse_args()
    questions = QUESTIONS[:args.questions]

    print("=" * 78)
    print("重排复用 query 向量 —— 等价性验证")
    print("=" * 78)

    rc = require_keys("本脚本要统计 embedding 调用次数，必须真实调用向量接口。")
    if rc:
        return rc

    tool = rag_tool.RAGTool()
    if tool.is_empty():
        print("❌ 索引为空或不可用，先用 scripts/verify_live.py 建一次索引")
        return 1
    print(f"索引就绪。用 {len(questions)} 个问题分别跑两种模式\n")

    # 整个脚本**共用一个** wrapper：计数跨轮累加，批次缓存跨轮复用。
    wrapper = _CountingStableEmbeddings(tool.embeddings)

    print("── 模式 A：复用 query 向量（新，RERANK_LEGACY=0）──")
    new_res = run_once(tool, questions, legacy=False, wrapper=wrapper)
    n_new_q = wrapper.n_query
    n_new_d, n_new_hit = wrapper.n_docs, wrapper.n_doc_hits
    print(f"   embed_query 调用 {n_new_q} 次，"
          f"embed_documents 调用 {n_new_d} 次"
          f"（{wrapper.n_texts} 条文本，打服务 {len(wrapper.batches)} 个批次）")

    print("\n── 模式 B：各自编码（旧，RERANK_LEGACY=1）──")
    old_res = run_once(tool, questions, legacy=True, wrapper=wrapper)
    n_old_q = wrapper.n_query - n_new_q
    n_old_d = wrapper.n_docs - n_new_d
    n_old_hit = wrapper.n_doc_hits - n_new_hit
    print(f"   embed_query 调用 {n_old_q} 次，"
          f"embed_documents 调用 {n_old_d} 次（其中命中批次缓存 {n_old_hit} 次，"
          f"真正打服务 {n_old_d - n_old_hit} 次）")

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
    print(f"embed_query ：旧 {n_old_q} 次 → 新 {n_new_q} 次"
          f"（省 {n_old_q - n_new_q} 次）")
    print(f"embed_documents ：旧 {n_old_d} 次 → 新 {n_new_d} 次"
          f"（应完全相同，等价性要求候选集编码次数不变）")
    print(f"   其中命中批次缓存：旧 {n_old_hit} 次 → 新 {n_new_hit} 次"
          f"（模式 B 两轮批次完全一致时，全部命中即说明候选集没变）")
    saved = n_old_q - n_new_q
    expected = n
    if saved <= 0:
        print("❌ 没有省下任何 query 编码调用 —— 优化未生效")
        return 1
    if saved != expected:
        print(f"⚠️ 省下 {saved} 次，预期 {expected} 次（每题恰好省 1 次）")
    else:
        print(f"✅ 每题恰好省 1 次 query 编码，共建 {n} 次")

    print()
    print("=" * 78)
    print("附：embedding 服务自身的批次抖动（本脚本的记忆化正是在消除它）")
    print("=" * 78)
    noise = measure_service_noise(wrapper, questions[0])
    if noise is None:
        print("（本轮没有 ≥2 条的候选批次，跳过实测）")
    else:
        print(f"取自本轮真实候选批次（{noise['batch_size']} 条），同一批次连编两次：")
        print(f"  · 最大分量差   {noise['max_component_delta']:.3e}")
        print(f"  · 最大分数漂移 {noise['max_score_drift']:.3e}"
              f"  ← 这就是未固定服务时分数会漂的量级")
        print("  这说明：不加记忆化时，1e-06 的容差落在服务噪声地板之下，")
        print("  断言恒不通过、也就恒不说明问题。固定批次后此噪声对两模式等价，")
        print("  残余差异才真正归属被测的代码路径。")

    print()
    ok = not diffs and saved == expected and n_old_d == n_new_d
    print("✅ 等价性验证通过" if ok else "❌ 等价性验证未通过")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
