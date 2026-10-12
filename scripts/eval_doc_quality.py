# -*- coding: utf-8 -*-
"""文档复杂度（难题 8）与质量检测（难题 9）的客观验证。

两条互相独立的证据链，都**不依赖任何云接口**：

A. 复杂度评估 → 用 OHR-Bench 官方标注的 `formatting_noise_{mild,moderate,severe}`
   三档「排版噪声」做外部效度检验：噪声越重，复杂度分应当越高。
   这是一个**有标注、可复现**的外部验证，而不是自己说自己准。

B. 质量检测 → 用**已知真值的合成退化**做验证：把干净页面分别做
   旋转 180°、倾斜 5°、高斯模糊、降采样，看检测器是否如实报出对应问题；
   同时看干净页面会不会被误报（假阳性）。
   为什么用合成退化而不用 OHR-Bench 的 `semantic_noise_*`：那三档里含有
   作者插入的字面标记（"The first/second perturbed document."），
   拿它当质检真值等于让检测器去认标记，是无效验证。

用法：python scripts/eval_doc_quality.py        退出码 0 = 两条链都达标
"""
import json
import os
import random
import sys
import zipfile
from datetime import datetime

import pyarrow.parquet as pq

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
os.chdir(ROOT)

from tools.doc_quality import (  # noqa: E402
    assess_document, complexity_plan, complexity_score, complexity_tier,
    quality_score_text, text_features,
)

# OHR-Bench 数据默认放在仓库内 data/ohr/；可用环境变量 OHR_DIR 覆盖。
# （此前写死作者本机 D 盘绝对路径，换机必崩）
OHR = os.environ.get("OHR_DIR") or os.path.join(ROOT, "data", "ohr")
OUT = os.path.join(ROOT, "data", "eval", "doc_quality")
REPORT_DIR = os.path.join(ROOT, "data", "eval", "reports")
SEED = 20260925
TIERS = ["gt_text", "formatting_noise_mild",
         "formatting_noise_moderate", "formatting_noise_severe"]


def _find_font():
    """挑一个可用的中文字体：优先环境变量 EVAL_FONT，其次各系统常见位置。

    此前写死 C:\\Windows\\Fonts\\msyh.ttc —— 在 Linux/macOS 上直接抛异常。
    找不到时返回 None，渲染层退回 PIL 默认字体（中文会显示为方框，但不崩）。
    """
    cands = [os.environ.get("EVAL_FONT"), r"C:\Windows\Fonts\msyh.ttc",
             r"C:\Windows\Fonts\simhei.ttf",
             "/System/Library/Fonts/PingFang.ttc",
             "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
             "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"]
    for p in cands:
        if p and os.path.exists(p):
            return p
    return None


FONT = _find_font()


# ---------------- A. 复杂度 vs 官方排版噪声档 ----------------
def part_a(n_pages=40):
    print("=" * 78)
    print("A. 复杂度评估 vs OHR-Bench 官方排版噪声三档")
    print("=" * 78)
    rows = pq.ParquetFile(os.path.join(OHR, "OHR-Bench.parquet")).read().to_pylist()
    random.seed(SEED)
    random.shuffle(rows)
    usable = [r for r in rows if all((r.get(t) or "").strip() for t in TIERS)]
    sample = usable[:n_pages]
    print(f"抽样 {len(sample)} 页（可用 {len(usable)} 页）\n")

    means = {t: [] for t in TIERS}
    mono_ok = 0
    details = []
    # 策略分布：赛题对难题 8 的要求不止「打分」，还有「指导后续处理策略」。
    # 这里把「分数 → 策略」的映射统计出来，证明分档真的导出了**不同**的处理路径，
    # 而不是所有文档拿到同一套参数（那等于没有策略）。
    plan_stats = {}
    for r in sample:
        scores = {}
        for t in TIERS:
            f = text_features(r[t])
            s, _ = complexity_score(f)
            scores[t] = s
            means[t].append(s)
        chain = [scores[t] for t in TIERS]
        # 允许 5 分容差内的持平（噪声档之间确实存在整页无变化的情况）
        ok = all(chain[i + 1] >= chain[i] - 5.0 for i in range(len(chain) - 1))
        mono_ok += ok
        # 策略取最复杂的那一档文档（severe）：就是「这份文档最坏情况下该怎么处理」
        plan = complexity_plan(text_features(r[TIERS[-1]]))
        key = (plan["complexity_tier"], plan["parse_path"],
               f"{plan['chunk_size']}/{plan['chunk_overlap']}", plan["top_k"])
        st = plan_stats.setdefault(key, {"n": 0, "triggers": set()})
        st["n"] += 1
        st["triggers"].update(plan["triggers"])
        details.append({"doc": r["doc_name"], "page": r["page_idx"],
                        "scores": scores, "monotonic": ok,
                        "plan_tier": plan["complexity_tier"],
                        "parse_path": plan["parse_path"],
                        "chunk": [plan["chunk_size"], plan["chunk_overlap"]],
                        "top_k": plan["top_k"], "triggers": plan["triggers"]})

    for t in TIERS:
        v = means[t]
        print(f"  {t:28s} 均值 {sum(v)/len(v):7.2f}   档位 "
              f"{complexity_tier(sum(v)/len(v))}")
    rate = mono_ok / len(sample)
    print(f"\n  单调性（噪声越重 → 复杂度分不降）通过率: {mono_ok}/{len(sample)}"
          f" = {rate*100:.1f}%")
    print(f"  层次是否整体递增: "
          f"{'是 ✅' if means[TIERS[-1]][0] is not None and
             sum(means[TIERS[0]])/len(means[TIERS[0]]) <= sum(means[TIERS[-1]])/len(means[TIERS[-1]]) else '否'}"
          f"（gt {sum(means[TIERS[0]])/len(means[TIERS[0]]):.1f} → "
          f"severe {sum(means[TIERS[-1]])/len(means[TIERS[-1]]):.1f}）")

    print()
    print("  策略分布（复杂度分 → 处理策略；取每页最复杂的 severe 档）")
    print(f"  {'档位':6s} {'解析通道':10s} {'切块/重叠':12s} {'top_k':6s} 页数  触发项")
    for key, st in sorted(plan_stats.items(), key=lambda kv: -kv[1]["n"]):
        tier, path, chunk, topk = key
        print(f"  {tier:6s} {path:10s} {chunk:12s} {topk:<6d} {st['n']:<4d}  "
              + "、".join(sorted(st["triggers"])) or "—")
    nondefault = sum(st["n"] for k, st in plan_stats.items() if k != ("简单", "ocr", "1000/160", 6))
    print(f"\n  策略分化率：{nondefault}/{len(sample)} = {nondefault/len(sample)*100:.1f}%"
          f"   （拿到非默认处理策略的页占比；0% 说明策略层形同虚设）")

    return rate, details, {t: sum(v) / len(v) for t, v in means.items()}, plan_stats


# ---------------- B. 质量检测 vs 已知合成退化 ----------------
def _render(text, path, size=(1240, 1754), rotate=0, blur=0, scale=1.0):
    from PIL import Image, ImageDraw, ImageFont
    base = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(base)
    try:
        font = ImageFont.truetype(FONT, 18)
        font_b = ImageFont.truetype(FONT, 26)
    except Exception:
        font = font_b = ImageFont.load_default()
    d.text((60, 40), "OHR-Bench page (synthetic render)", fill="black", font=font_b)
    y = 100
    d.line([(60, y), (size[0] - 60, y)], fill="black", width=2)
    y += 20
    for line in (text or "").split("\n"):
        for chunk in [line[i:i + 105] for i in range(0, max(len(line), 1), 105)]:
            if y > size[1] - 60:
                break
            d.text((60, y), chunk, fill="black", font=font)
            y += 24
        if y > size[1] - 60:
            break
    img = base
    if scale != 1.0:
        img = img.resize((int(size[0] * scale), int(size[1] * scale)), Image.LANCZOS)
    if blur:
        from PIL import ImageFilter
        img = img.filter(ImageFilter.GaussianBlur(blur))
    if rotate:
        img = img.rotate(rotate, expand=True, fillcolor="white")
    img.save(path)
    return path


def part_b(n_base=3):
    print()
    print("=" * 78)
    print("B. 质量检测 vs 已知合成退化（有真值）")
    print("=" * 78)
    os.makedirs(OUT, exist_ok=True)
    rows = pq.ParquetFile(os.path.join(OHR, "OHR-Bench.parquet")).read().to_pylist()
    random.seed(SEED + 1)
    random.shuffle(rows)
    bases = [r for r in rows if len((r.get("gt_text") or "")) > 1200][:n_base]

    variants = [("clean", {}), ("rot180", {"rotate": 180}), ("rot5", {"rotate": 5}),
                ("blur", {"blur": 3.0}), ("lowres", {"scale": 0.28})]
    expect = {"clean": None, "rot180": "orientation", "rot5": "skew",
              "blur": "blur", "lowres": "resolution"}
    key = {"orientation": lambda r: r["orientation"]["flipped"],
           "skew": lambda r: r["skew"]["skewed"],
           "blur": lambda r: r["blur"]["blurry"],
           "resolution": lambda r: r["resolution"]["low_res"]}

    res = {v: {"hit": 0, "n": 0} for v, _ in variants}
    fp_clean = 0
    for bi, base in enumerate(bases, 1):
        for vname, kw in variants:
            p = os.path.join(OUT, f"p{bi}_{vname}.png")
            _render(base["gt_text"], p, **kw)
            rep = assess_document(p)
            exp = expect[vname]
            if exp is None:
                anyflag = any(key[k](rep) for k in key)
                fp_clean += 1 if anyflag else 0
                tag = "误报 ❌" if anyflag else "正确放行 ✅"
                print(f"  p{bi} {vname:7s} 质量分 {rep['quality_score']:5.1f} "
                      f"[{rep['quality_tier']}]  {tag}")
            else:
                got = key[exp](rep)
                res[vname]["n"] += 1
                res[vname]["hit"] += 1 if got else 0
                print(f"  p{bi} {vname:7s} 质量分 {rep['quality_score']:5.1f} "
                      f"[{rep['quality_tier']}]  检出 {exp}: {'✅' if got else '❌ 漏检'}"
                      + (f"  倾斜={rep['skew']['skew_deg']}°" if exp == "skew" else "")
                      + (f"  模糊={rep['blur']['blur_var']}" if exp == "blur" else "")
                      + (f"  短边={rep['resolution']['short_side']}" if exp == "resolution" else ""))

    print()
    for vname, _ in variants:
        if res[vname]["n"]:
            r = res[vname]["hit"] / res[vname]["n"]
            print(f"  {vname:8s} 检出率 {res[vname]['hit']}/{res[vname]['n']} = {r*100:.0f}%")
    print(f"  clean    假阳性 {fp_clean}/{len(bases)}")

    # 文本侧的繁体字检测（不需要渲染中文，直接测特征层）
    simp = "这个学生的综合素质测评成绩排名第一，需要重新核对数据"
    trad = "這個學生的綜合素質測評成績排名第一，需要重新核對數據"
    f_s, f_t = text_features(simp), text_features(trad)
    q_s, _ = quality_score_text(f_s)
    q_t, _ = quality_score_text(f_t)
    print(f"\n  繁体字检测：简体 {f_s['traditional']} 个（质量 {q_s}） vs "
          f"繁体 {f_t['traditional']} 个（质量 {q_t}） → "
          f"{'✅ 可区分' if f_t['traditional'] > f_s['traditional'] + 5 else '❌ 不可区分'}")
    return res, fp_clean, len(bases), (f_s, f_t)


def main():
    # 这个脚本此前**没有 argparse**，而 README 里写的是
    # `python scripts/eval_doc_quality.py --save` —— 参数会被静默忽略。
    # 这里补上参数解析（顺带获得 --help），并明确：报告**总是落盘**，
    # `--save` 只是与其它 eval_*.py 的调用习惯保持一致的兼容写法。
    import argparse
    ap = argparse.ArgumentParser(
        description="文档复杂度（难题 8）与质量检测（难题 9）的离线验证")
    ap.add_argument("--save", action="store_true",
                    help="兼容项：两条证据链的报告默认就会落盘")
    args = ap.parse_args()

    # part A（复杂度 vs 官方排版噪声）与 part B（质量检测 vs 合成退化）
    # 都要从 OHR-Bench 取 gt_text 当基底，缺数据集时直接给可操作提示。
    from _preflight import require_ohr
    rc = require_ohr()
    if rc:
        return rc

    random.seed(SEED)
    rate_a, det_a, mean_a, plan_stats = part_a()
    res_b, fp, n_clean, (f_s, f_t) = part_b()

    rec_b = {v: (res_b[v]["hit"] / res_b[v]["n"] if res_b[v]["n"] else 0)
             for v in res_b}
    # 只统计「有真值退化」的档位：clean 档 n=0（它是假阳性对照），
    # 把它算进最小值会让「最差检出率」恒为 0（本脚本自己踩过）
    graded = [res_b[v]["hit"] / res_b[v]["n"] for v in res_b if res_b[v]["n"]]
    worst_b = min(graded) if graded else 0.0
    trad_ok = f_t["traditional"] > f_s["traditional"] + 5

    print()
    print("=" * 78)
    print("### 结论")
    print(f"  A 复杂度单调性 : {rate_a*100:.1f}%  （目标 ≥ 70%）")
    print(f"  B 退化检出率   : 最差 {worst_b*100:.0f}%  （目标 = 100%）")
    print(f"  B 干净假阳性   : {fp}/{n_clean}  （目标 = 0）")
    print(f"  繁体字可区分   : {'是' if trad_ok else '否'}")

    os.makedirs(REPORT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    with open(os.path.join(REPORT_DIR, f"docquality-{stamp}.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"A_monotonic_rate": rate_a, "A_tier_means": mean_a,
                   "A_plans": [{"tier": k[0], "parse_path": k[1], "chunk": k[2],
                                "top_k": k[3], "pages": v["n"],
                                "triggers": sorted(v["triggers"])}
                               for k, v in plan_stats.items()],
                   "B_recall": rec_b, "B_clean_fp": fp, "B_clean_n": n_clean,
                   "traditional": {"simple": f_s["traditional"],
                                   "traditional": f_t["traditional"]},
                   "A_details": det_a}, fh, ensure_ascii=False, indent=2)

    ok = rate_a >= 0.7 and worst_b >= 1.0 and fp == 0 and trad_ok
    print()
    print("✅ 达标" if ok else "❌ 未达标")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
