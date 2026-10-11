# -*- coding: utf-8 -*-
"""难题 9「错别字/繁体字识别**并校正**」的客观验证。

思路：OHR-Bench 自带 `semantic_noise_{mild,moderate,severe}` —— 它们是
对 `gt_text` 做**字符级扰动**得到的（形近/同音错误），而 `gt_text` 就是干净真值。
这正好是一份**带真值的纠错基准**，可以直接量化「校正有没有让文本更接近真值」。

⚠️ 两个必须处理的数据陷阱：
  1. 扰动档里含作者插入的**字面标记**（"The first/second perturbed document."）——
     先剥掉，否则等于在测「能不能删掉这句话」
  2. `gt_text` 里也有 OCR 残留噪声，所以用**字符错误率(CER)的相对变化**判定，
     而不是要求校正后与 gt 完全一致

指标：
  CER 下降   —— 校正后与真值的字符错误率是否降低（主指标）
  改动命中率 —— 每处改动的新词是否真的出现在 gt 里（衡量「改对了」）
  误改率     —— 新词不在 gt 里的改动占比（衡量「改错了」）

用法：python scripts/eval_correction.py [--pages 8] [--tiers mild,moderate]
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import pyarrow.parquet as pq  # noqa: E402
from config import Config  # noqa: E402
from tools.doc_quality import correct_text, to_simplified  # noqa: E402

# OHR-Bench 数据默认放在仓库内 data/ohr/；可用环境变量 OHR_DIR 覆盖。
# （此前写死作者本机 D 盘绝对路径，换机必崩）
OHR = os.environ.get("OHR_DIR") or os.path.join(ROOT, "data", "ohr")
REPORT_DIR = os.path.join(ROOT, "data", "eval", "reports")

# 扰动档里作者插入的字面标记
_MARKER = re.compile(r"The (first|second) perturbed document\.?", re.I)


def strip_markers(t: str) -> str:
    return _MARKER.sub("", t or "").strip()


def lev(a: str, b: str) -> int:
    """Levenshtein 距离（滚动数组，够用且省内存）"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(text: str, ref: str) -> float:
    return lev(text, ref) / max(len(ref), 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=8)
    ap.add_argument("--tiers", default="mild,moderate")
    args = ap.parse_args()
    tiers = [f"semantic_noise_{t.strip()}" for t in args.tiers.split(",") if t.strip()]

    from langchain_openai import ChatOpenAI
    llm = ChatOpenAI(model=Config.LLM_MODEL, api_key=Config.LLM_API_KEY,
                     base_url=Config.LLM_BASE_URL, temperature=0)

    rows = pq.ParquetFile(os.path.join(OHR, "OHR-Bench.parquet")).read().to_pylist()
    picked = []
    for r in rows:
        gt = (r.get("gt_text") or "").strip()
        if 400 < len(gt) < 2500:
            picked.append(r)
        if len(picked) >= args.pages:
            break
    print("=" * 78)
    print(f"错别字/繁体字校正验证 · {len(picked)} 页 × {len(tiers)} 档")
    print("=" * 78)

    tot = {t: {"n": 0, "cer_before": 0.0, "cer_after": 0.0, "chg": 0,
               "hit": 0, "miss": 0, "trad": 0, "rej": 0} for t in tiers}
    for ti, r in enumerate(picked, 1):
        gt = strip_markers(r["gt_text"])
        for t in tiers:
            noisy = strip_markers(r.get(t) or "")
            if not noisy:
                continue
            res = correct_text(noisy, use_llm=True, llm=llm)
            cb = cer(noisy, gt)
            ca = cer(res["text"], gt)
            s = tot[t]
            s["n"] += 1
            s["cer_before"] += cb
            s["cer_after"] += ca
            s["chg"] += len(res["changes"])
            s["trad"] += res["traditional_fixed"]
            s["rej"] += res["rejected"]
            for c in res["changes"]:
                if c["kind"] != "错别字":
                    continue
                if c["after"] in gt:
                    s["hit"] += 1
                else:
                    s["miss"] += 1
            print(f"  p{ti} {t.replace('semantic_noise_',''):9s} "
                  f"CER {cb*100:5.2f}% → {ca*100:5.2f}% ({'↓改善' if ca < cb else '持平/变差'})"
                  f"  改动 {len(res['changes'])} 处（拒 {res['rejected']}）")

    print()
    print("=" * 78)
    print("### 汇总")
    print(f"  {'档位':10s} {'页数':>4} {'CER 校正前':>10} {'CER 校正后':>10} {'相对下降':>9} "
          f"{'改动':>5} {'命中':>5} {'误改':>5} {'繁→简':>6}")
    summary = {}
    for t in tiers:
        s = tot[t]
        if not s["n"]:
            continue
        cb, ca = s["cer_before"] / s["n"], s["cer_after"] / s["n"]
        drop = (cb - ca) / cb * 100 if cb else 0.0
        print(f"  {t.replace('semantic_noise_',''):10s} {s['n']:>4} {cb*100:>9.2f}% "
              f"{ca*100:>9.2f}% {drop:>8.1f}% {s['chg']:>5} {s['hit']:>5} {s['miss']:>5} "
              f"{s['trad']:>6}")
        summary[t] = {"pages": s["n"], "cer_before": cb, "cer_after": ca,
                      "rel_drop_pct": drop, "changes": s["chg"],
                      "hit": s["hit"], "miss": s["miss"],
                      "traditional": s["trad"], "rejected": s["rej"]}

    # 繁体字专项（纯映射，可用合成用例确定性验证）
    trad = "這個學生的綜合素質測評成績排名第一，需要重新核對數據"
    simp, n = to_simplified(trad)
    print(f"\n  繁→简专项：{n} 字改动 → {simp}")
    ok_trad = ("这个学生" in simp and "数据" in simp)

    improved = all(v["cer_after"] <= v["cer_before"] for v in summary.values())
    # 「CER 不升」是**空洞判据**：什么都不做也成立（本脚本第一版就栽在这 ——
    # 打印出「✅ 达标」而改动数是 0，等于把"没生效"判成了"通过"）。
    # 所以必须叠加「确实动了且方向正确」才算达标。
    total_changes = sum(v["changes"] for v in summary.values())
    real_drop = sum(1 for v in summary.values() if v["cer_after"] < v["cer_before"])
    effective = total_changes > 0 and real_drop > 0
    miss_rate = (sum(v["miss"] for v in summary.values())
                 / max(sum(v["hit"] + v["miss"] for v in summary.values()), 1))

    os.makedirs(REPORT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    with open(os.path.join(REPORT_DIR, f"correction-{stamp}.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"summary": summary, "traditional_ok": ok_trad,
                   "miss_rate": miss_rate, "total_changes": total_changes,
                   "pages_with_improvement": real_drop}, fh,
                  ensure_ascii=False, indent=2)

    print()
    print(f"  总改动 {total_changes} 处；CER 真正下降的页数 {real_drop}/{len(summary)}")
    print(f"  误改率（新词不在真值里）: {miss_rate*100:.1f}%")
    if total_changes == 0:
        print("  ⚠️ 一处都没改 —— 不是「达标」，是**校正没生效**（判据必须区分这两者）")
    print("✅ 达标（确实改动了 + CER 全面不升 + 繁简转换正确）"
          if (improved and ok_trad and effective)
          else "❌ 未达标")
    return 0 if (improved and ok_trad and effective) else 1


if __name__ == "__main__":
    sys.exit(main())
