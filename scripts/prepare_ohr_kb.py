# -*- coding: utf-8 -*-
"""从 OHR-Bench 物化一套**独立**的 RAG 评测知识库 + 评测问题集。

赛题官方推荐 OHR-Bench（评估 OCR 对 RAG 的系统级联影响），它的 QA 自带：
  · 可自动判分的短答案（Yes/No、Numeric、Short Answer）
  · 证据来源（text / table / equation）
  · **gold 页码** —— 有页码才能客观算「检索命中率」

产物：
  data/ohr_kb/            评测知识库（每页一个 .md，文件名带域/文档/页码）
  data/eval/rag_cases.json 评测问题集（含 gold 页码与 gold 答案）

为什么按「页」切文件而不是按「文档」：
  RAGTool 的 source 元数据取文件名，按页切文件后「gold 页是否被检索到」
  就是一个精确的集合判断，不需要额外解析 chunk 的页码归属。

用法：
    python scripts/prepare_ohr_kb.py
"""
import ast
import collections
import hashlib
import json
import os
import random
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))   # 同目录的 _preflight

from _preflight import require_ohr  # noqa: E402

# OHR-Bench 数据默认放在仓库内 data/ohr/；可用环境变量 OHR_DIR 覆盖。
# （此前写死作者本机 D 盘绝对路径，换机必崩）
OHR = os.environ.get("OHR_DIR") or os.path.join(ROOT, "data", "ohr")
ZIP = os.path.join(OHR, "retrieval.zip")
PARQUET = os.path.join(OHR, "OHR-Bench.parquet")

KB_DIR = os.path.join(ROOT, "data", "ohr_kb")
CASES_PATH = os.path.join(ROOT, "data", "eval", "rag_cases.json")

PER_DOMAIN = {"finance": 3, "law": 3, "manual": 2, "textbook": 2, "news": 2}
MAX_PAGES_PER_DOC = 25      # 单文档页数上限：避免一份 382 页的年报淹没整个语料
MAX_Q_PER_DOC = 6
SEED = 20260924


def safe_name(doc_name: str) -> str:
    """doc_name -> 文件系统安全的短名（保留可读性，尾部加哈希保证唯一）"""
    base = re.sub(r"[^\w\u4e00-\u9fff.\-]+", "_", doc_name).strip("_")
    base = base[:70]
    h = hashlib.md5(doc_name.encode("utf-8")).hexdigest()[:6]
    return f"{base}__{h}"


def load_qas():
    """展开 parquet 的 qas 列，返回 [{doc, page, question, answer, ...}]"""
    import pyarrow.parquet as pq
    rows = pq.ParquetFile(PARQUET).read().to_pylist()
    out = []
    for r in rows:
        q = r["qas"]
        if isinstance(q, str):
            try:
                q = ast.literal_eval(q)
            except Exception:
                continue
        if not isinstance(q, dict):
            continue
        keys = [k for k in ("doc_name", "questions", "answers", "doc_type",
                            "answer_form", "evidence_source", "evidence_context",
                            "evidence_page_no") if k in q]
        n = max(len(q[k]) for k in keys)
        for i in range(n):
            item = {k: (q[k][i] if i < len(q[k]) else None) for k in keys}
            if not item.get("questions") or item.get("answers") in (None, ""):
                continue
            item["page"] = item.get("evidence_page_no")
            out.append(item)
    return out


def main():
    random.seed(SEED)
    # 本脚本同时要 retrieval.zip（取 gt 原文）与 OHR-Bench.parquet（取 QA 标注），
    # 缺哪个就报哪个。按约定退出码 3（缺外部数据集），别让「没下数据」看起来像「脚本坏了」。
    rc = require_ohr(("retrieval.zip", "OHR-Bench.parquet"))
    if rc:
        return rc

    print("读取 QA 标注 ...")
    qas = load_qas()
    print(f"  QA 总数: {len(qas)}")

    with zipfile.ZipFile(ZIP) as z:
        # gt/<domain>/<doc>.json
        gt_entries = [n for n in z.namelist()
                      if n.startswith("gt/") and n.endswith(".json")]
        by_doc = {}
        for n in gt_entries:
            parts = n.split("/")
            if len(parts) == 3:
                by_doc[f"{parts[1]}/{parts[2][:-5]}"] = n
        print(f"  gt 检索基文件数: {len(by_doc)}")

        qa_by_doc = collections.defaultdict(list)
        for q in qas:
            qa_by_doc[q["doc_name"]].append(q)

        # ── 选文档：每个域按 QA 数量取前 N，且 doc 在 gt 里存在 ──
        chosen = []
        for domain, k in PER_DOMAIN.items():
            cands = [(d, len(v)) for d, v in qa_by_doc.items()
                     if d.startswith(domain + "/") and d in by_doc]
            cands.sort(key=lambda t: -t[1])
            chosen += [d for d, _ in cands[:k]]
            print(f"  {domain:9s} 选 {len(cands[:k])} 份: {[d.split('/')[-1][:34] for d, _ in cands[:k]]}")

        # ── 写知识库 + 收集问题 ──
        if os.path.isdir(KB_DIR):
            for f in os.listdir(KB_DIR):
                os.remove(os.path.join(KB_DIR, f))
        os.makedirs(KB_DIR, exist_ok=True)

        cases, page_index = [], {}
        for doc in chosen:
            entry = by_doc[doc]
            with z.open(entry) as fh:
                pages = json.loads(fh.read().decode("utf-8", "replace"))
            if isinstance(pages, dict):
                pages = pages.get("pages") or []

            usable = []
            for p in pages:
                idx = p.get("page_idx")
                txt = (p.get("text") or "").strip()
                if idx is None or not txt:
                    continue        # 空页不入库（gt 里有 346 页是空的）
                usable.append((int(idx), txt))
            usable.sort()
            usable = usable[:MAX_PAGES_PER_DOC]
            incl_pages = {i for i, _ in usable}

            stem = safe_name(doc)
            for idx, txt in usable:
                fn = f"{stem}__p{idx:04d}.md"
                with open(os.path.join(KB_DIR, fn), "w", encoding="utf-8") as fh:
                    # 头部给出文档与页码，检索命中时来源可直接读，也帮助 chunk 保持上下文
                    fh.write(f"# {doc} — page {idx}\n\n{txt}\n")
                page_index[(doc, idx)] = fn
            print(f"  写入 {doc}  页数 {len(usable)}")

            # ── 该文档的问题：只取 gold 页在库内的，按证据类型分层抽样 ──
            pool = [q for q in qa_by_doc[doc]
                    if q.get("page") is not None and int(q["page"]) in incl_pages]
            buckets = collections.defaultdict(list)
            for q in pool:
                buckets[(q.get("evidence_source"), q.get("answer_form"))].append(q)
            picked, keys = [], sorted(buckets, key=lambda k: str(k))
            i = 0
            while len(picked) < MAX_Q_PER_DOC and any(buckets[k] for k in keys):
                k = keys[i % len(keys)]
                if buckets[k]:
                    picked.append(buckets[k].pop(0))
                i += 1
            for q in picked:
                cases.append({
                    "id": q.get("ID") or "",
                    "doc": doc,
                    "page": int(q["page"]),
                    "kb_file": page_index[(doc, int(q["page"]))],
                    "question": q["questions"],
                    "answer": str(q["answers"]),
                    "answer_form": q.get("answer_form"),
                    "evidence_source": q.get("evidence_source"),
                    "evidence_context": (q.get("evidence_context") or "")[:600],
                })

    out = {
        "meta": {
            "name": "RAG 检索与问答评测集（OHR-Bench 公开基准子集）",
            "source": "https://hf-mirror.com/datasets/opendatalab/OHR-Bench",
            "kb_dir": "data/ohr_kb",
            "note": "知识库按页物化，便于用「gold 页是否被检索到」客观计算检索命中率",
            "seed": SEED,
            "docs": len(chosen),
            "pages": len(page_index),
            "questions": len(cases),
        },
        "cases": cases,
    }
    os.makedirs(os.path.dirname(CASES_PATH), exist_ok=True)
    with open(CASES_PATH, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)

    print()
    print(f"✅ 知识库: {KB_DIR}  文件 {len(os.listdir(KB_DIR))} 个")
    print(f"✅ 评测集: {CASES_PATH}  问题 {len(cases)} 条")
    print("   证据类型分布:", dict(collections.Counter(c["evidence_source"] for c in cases)))
    print("   答案形式分布:", dict(collections.Counter(c["answer_form"] for c in cases)))
    print("   文档分布:", dict(collections.Counter(c["doc"].split("/")[0] for c in cases)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
