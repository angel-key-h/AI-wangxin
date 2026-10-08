# -*- coding: utf-8 -*-
"""实验：识别前放大图像能否提升表格重建质量？

动机：目标⑥ 的表格基线只有 61.3% 单元格召回（OHR 真实表格）。从失败样本看，
主因疑似**文字太小**（我的合成渲染字号 17px，真实扫描件往往更小）——
OCR 对小字号的分辨能力显著下降，而放大是经典补偿手段。
本脚本把「字号 × 放大倍数」做成网格，用数据决定要不要在生产里加放大预处理。

只测**干净**图（先隔离「放大」这一个变量），并额外加一档小字号（11px）
模拟真实低 DPI 扫描件。

用法：python scripts/exp_upscale.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
os.chdir(ROOT)

from PIL import Image  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

from tools import image_tool  # noqa: E402
from eval_doc_recog import norm, parse_tabular, render_table, OHR  # noqa: E402

N_TABLE = 8
FONT_SIZES = [17, 11]
UPSCALES = [1.0, 1.5, 2.0, 3.0]
OUT = os.path.join(ROOT, "data", "eval", "recog_exp")


def pick_tables(n):
    rows = pq.ParquetFile(os.path.join(OHR, "OHR-Bench.parquet")).read().to_pylist()
    out = []
    for r in rows:
        t = parse_tabular(r.get("gt_text") or "")
        if len(t) >= 3 and 2 <= len(t[0]) <= 7 and sum(len(x) for x in sum(t, [])) > 30:
            out.append(t)
        if len(out) >= n:
            break
    return out


def evaluate(tables, font_size, upscale):
    hdr_t = hdr_h = cell_t = cell_h = 0
    for i, gt in enumerate(tables, 1):
        p = os.path.join(OUT, f"f{font_size}_u{upscale}_t{i}.png")
        rendered = render_table(gt, p, font_size=font_size)
        if upscale != 1.0:
            img = Image.open(p)
            img = img.resize((int(img.width * upscale), int(img.height * upscale)),
                             Image.LANCZOS)
            img.save(p)
        hdr = [norm(c) for c in rendered[0]]
        cells = [norm(c) for row in rendered[1:] for c in row if norm(c)]
        try:
            res = image_tool.ocr_to_table(p)
        except Exception:
            hdr_t += len(hdr); cell_t += len(cells)
            continue
        df = res.get("dataframe")
        cols = [norm(c) for c in df.columns] if df is not None else []
        recon = ({norm(x) for x in df.values.ravel() if norm(x)} if df is not None
                 else {norm(x) for x in (res.get("text") or "").split("\n") if norm(x)})
        hdr_t += len(hdr); cell_t += len(cells)
        hdr_h += sum(1 for h in hdr if h and any(c == h or (len(h) >= 2 and h in c) for c in cols))
        cell_h += sum(1 for c in cells if c in recon)
    return hdr_h / max(hdr_t, 1), cell_h / max(cell_t, 1)


def main():
    os.makedirs(OUT, exist_ok=True)
    tables = pick_tables(N_TABLE)
    print(f"用 {len(tables)} 张 OHR 真实表格\n")
    print(f"{'字号':>6} {'放大':>6} {'列名准确率':>12} {'单元格召回率':>14}")
    best = None
    for fs in FONT_SIZES:
        for u in UPSCALES:
            h, c = evaluate(tables, fs, u)
            print(f"{fs:>6} {u:>6.1f} {h*100:>11.1f}% {c*100:>13.1f}%")
            if best is None or c > best[2]:
                best = (fs, u, c, h)
        print()
    print("### 结论")
    fs, u, c, h = best
    print(f"  最佳组合：字号 {fs}px + 放大 {u}× → 单元格召回 {c*100:.1f}%、列名 {h*100:.1f}%")
    base_c = evaluate(tables, 11, 1.0)[1]
    print(f"  小字号(11px)不放大时：{base_c*100:.1f}% → 放大 {u}× 后 {c*100:.1f}%"
          f"（提升 {(c-base_c)*100:+.1f} 个百分点）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
