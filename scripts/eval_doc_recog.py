# -*- coding: utf-8 -*-
"""表格 / 公式 / 图表识别能力评测（对应目标⑥，也是高级任务点名的「扭曲表格识别」指标）。

为什么要先建评测再谈模型：
  高级任务申报要求「与公开最优结果的详细对比实验」，没有可复现的评测就没有对比可言。
  本项目此前只在一个**自造单层表头**上测过 98.9%，样本太小、太干净，不足以支撑结论。

本脚本用 OHR-Bench 的真实表格/公式内容合成图像，测**三条能力线**：

  A. 表格识别 —— 从 OHR 的 LaTeX tabular 渲染成表格图，再走项目自己的
     `image_tool.ocr_to_table()` 重建，比对列名与单元格。
     含**干净 / 倾斜 / 模糊 / 低分辨率**四档 —— 「扭曲表格」正是高级任务点名的场景。
  B. 公式识别 —— 把 OHR 的公式文本渲染成图，看 OCR 能恢复多少字符。
     预期很差：公式是二维结构，纯文本 OCR 天然无能为力 → 这正是必须上 VLM 的理由。
  C. 图表识别 —— 用 matplotlib 画已知数值的柱状图，看现有链路能否还原数据。
     预期只能读到坐标轴文字、读不到数据序列 → 对应 OHR-Bench 里 CHA 类掉分最狠。

三条线都是**能力基线**：不依赖任何云接口（RapidOCR 本地），
将来接上视觉大模型后，用同一套脚本即可得出前后对比。

用法：python scripts/eval_doc_recog.py        退出码 0 = 脚本自身跑通（不是「达标」）
"""
import json
import os
import random
import re
import sys
import zipfile
from datetime import datetime

import pyarrow.parquet as pq

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
os.chdir(ROOT)

from tools import image_tool  # noqa: E402
from tools.rag_tool import latex_tables_to_markdown  # noqa: E402

# OHR-Bench 数据默认放在仓库内 data/ohr/；可用环境变量 OHR_DIR 覆盖。
# （此前写死作者本机 D 盘绝对路径，换机必崩）
OHR = os.environ.get("OHR_DIR") or os.path.join(ROOT, "data", "ohr")
OUT = os.path.join(ROOT, "data", "eval", "recog")
REPORT_DIR = os.path.join(ROOT, "data", "eval", "reports")
SEED = 20260925


def _find_font():
    """挑一个可用的中文字体：优先环境变量 EVAL_FONT，其次各系统常见位置。

    此前写死 C:\\Windows\\Fonts\\msyh.ttc —— 在 Linux/macOS 上直接抛异常，
    本脚本的表格/公式渲染会整段崩掉。
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


def _load_font(size):
    """按字号取字体；找不到字体或加载失败时退回 PIL 默认字体，保证不崩。"""
    from PIL import ImageFont
    if FONT:
        try:
            return ImageFont.truetype(FONT, size)
        except Exception:
            pass
    return ImageFont.load_default()

TABULAR = re.compile(r"\\begin\{tabular\}\{[^}]*\}(.*?)\\end\{tabular\}", re.S)
_ROW_NOISE = re.compile(r"\\(hline|toprule|midrule|bottomrule|endhead|endfirsthead)\b")
_CLINE = re.compile(r"\\cline\{[^}]*\}")
_SPAN = re.compile(r"\\(?:multi|)column\{[^}]*\}\{[^}]*\}|\\multirow\{[^}]*\}\{[^}]*\}")


def norm(s) -> str:
    return re.sub(r"\s+", "", str(s or "")).lower().strip("|")


def _prf(tp: int, npred: int, ngt: int) -> tuple:
    """由 TP / 预测数 / 真值数算 (precision, recall, f1)。

    三处都用同一个函数，避免各节各写一份算法、口径悄悄漂开。
    分母为 0 时返回 0.0 而不是抛异常 —— 一张表都没预测出来，
    该得 0 分，不该让脚本崩掉。
    """
    p = tp / npred if npred else 0.0
    r = tp / ngt if ngt else 0.0
    f = 2 * p * r / (p + r) if (p + r) else 0.0
    return p, r, f


def _greedy_1to1(gt_items, pred_items, ok) -> int:
    """一对一贪心匹配，返回 TP。

    为什么不能直接数「有多少个真值被命中」当 TP：宽松子串规则下
    一个预测列名可能同时命中多个真值列名，TP 会超过预测总数，
    算出来的 precision 能大于 1 —— 那是坏指标，不是好成绩。
    一对一匹配保证 TP ≤ min(|真值|, |预测|)，P/R/F1 才落在 [0,1]。
    """
    used = set()
    tp = 0
    for g in gt_items:
        for j, p in enumerate(pred_items):
            if j in used:
                continue
            if ok(g, p):
                used.add(j)
                tp += 1
                break
    return tp


def parse_tabular(text):
    m = TABULAR.search(text or "")
    if not m:
        return []
    rows = []
    for raw in re.split(r"\\\\", m.group(1)):
        line = _CLINE.sub("", _ROW_NOISE.sub("", raw)).strip()
        if not line:
            continue
        cells = [_SPAN.sub("", c).strip() for c in line.split("&")]
        cells = [c[1:-1].strip() if c.startswith("{") and c.endswith("}") else c for c in cells]
        # 去掉残留 LaTeX 命令，只留可读内容
        cells = [re.sub(r"\\[A-Za-z]+\s*", "", re.sub(r"[{}$]", "", c)).strip() for c in cells]
        if any(cells):
            rows.append(cells)
    if len(rows) < 2:
        return []
    ncol = max(len(r) for r in rows)
    return [r + [""] * (ncol - len(r)) for r in rows]


def render_table(rows, path, font_size=17, max_cols=7, skew=0, blur=0.0, scale=1.0):
    from PIL import Image, ImageDraw, ImageFilter
    font = _load_font(font_size)
    rows = [r[:max_cols] for r in rows[:14]]
    ncol = max(len(r) for r in rows)
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    pad = 8
    colw = []
    for c in range(ncol):
        w = max(font.getlength(str(r[c])) for r in rows)
        colw.append(int(min(max(w + 2 * pad, 62), 260)))
    rowh = font_size + 2 * pad
    W, H = sum(colw), rowh * len(rows)
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    y = 0
    for r in rows:
        x = 0
        for c in range(ncol):
            d.rectangle([x, y, x + colw[c], y + rowh], outline=(110, 110, 110), width=1)
            txt = str(r[c])
            while font.getlength(txt) > colw[c] - 2 * pad and len(txt) > 1:
                txt = txt[:-1]
            d.text((x + pad, y + pad // 2 + 1), txt, fill="black", font=font)
            x += colw[c]
        y += rowh
    if scale != 1.0:
        img = img.resize((int(W * scale), int(H * scale)), Image.LANCZOS)
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    if skew:
        img = img.rotate(skew, expand=True, fillcolor="white", resample=Image.BICUBIC)
    img.save(path)
    return rows


# ---------------- A. 表格识别 ----------------
def part_a(n_tables=12):
    print("=" * 78)
    print("A. 表格识别：干净 / 倾斜 / 模糊 / 低分辨率")
    print("=" * 78)
    rows_pq = pq.ParquetFile(os.path.join(OHR, "OHR-Bench.parquet")).read().to_pylist()
    random.seed(SEED)
    random.shuffle(rows_pq)
    picked = []
    for r in rows_pq:
        t = parse_tabular(r.get("gt_text") or "")
        if len(t) >= 3 and 2 <= len(t[0]) <= 7 and sum(len(x) for x in sum(t, [])) > 30:
            picked.append(t)
        if len(picked) >= n_tables:
            break
    print(f"从 OHR-Bench 选出 {len(picked)} 张真实表格\n")

    variants = [("clean", {}), ("skew2", {"skew": 2}), ("blur", {"blur": 1.2}),
                ("lowres", {"scale": 0.55})]
    # 两套口径并存，互不影响：
    #   legacy（*_total / *_hit）—— 逐出现次数统计，用于保持 header_acc / cell_recall 与历史一致
    #   f1_*                    —— 集合/一对一匹配统计，用于 precision / recall / f1
    agg = {v: {"hdr_total": 0, "hdr_hit": 0, "cell_total": 0, "cell_hit": 0,
               "f1_cell_tp": 0, "f1_cell_pred": 0, "f1_cell_gt": 0,
               "f1_hdr_tp": 0, "f1_hdr_pred": 0, "f1_hdr_gt": 0,
               "struct_ok": 0, "n": 0, "errors": 0, "nopred": 0} for v, _ in variants}
    for ti, gt in enumerate(picked, 1):
        for vname, kw in variants:
            p = os.path.join(OUT, f"t{ti}_{vname}.png")
            # 真值必须取**真正画上去的那部分行**（render_table 会截断到 7 列 × 14 行）。
            # 早先用未截断的 gt 做分母，等于拿「没画上去的列」去要求识别出来 ——
            # 指标恒偏低（本脚本踩过），这属于测量错误，不是能力问题。
            rendered = render_table(gt, p, **kw)
            hdr = [norm(c) for c in rendered[0]]
            cells = [norm(c) for row in rendered[1:] for c in row if norm(c)]
            a = agg[vname]
            a["n"] += 1
            a["hdr_total"] += len(hdr)
            a["cell_total"] += len(cells)
            gt_set = {c for c in cells}
            a["f1_cell_gt"] += len(gt_set)
            a["f1_hdr_gt"] += len({h for h in hdr})

            try:
                res = image_tool.ocr_to_table(p)
            except Exception as e:
                # 崩溃必须**计为全错**，不能跳过 —— 跳过等于让失败不进分母，
                # 分数反而被抬高（这正是本项目刚修过的那个真实 bug 造成的场景）
                a["errors"] += 1
                print(f"  t{ti} {vname:7s} 识别异常 {type(e).__name__}（计为全错）")
                continue

            df = res.get("dataframe")
            cols = [norm(c) for c in df.columns] if df is not None else []
            if df is not None:
                recon = {norm(x) for x in df.values.ravel() if norm(x)}
            else:
                # 退回纯文本：整行当成了一个 token，天然对不上任何单元格。
                # 这会让 precision 掉到很低 —— 那不是指标坏了，而是这个通道
                # 确实没能重建出结构，本来就该扣分。
                recon = {norm(x) for x in (res.get("text") or "").split("\n") if norm(x)}
                a["nopred"] += 1

            def hdr_hit(h):
                """列名命中：允许子串（多层表头会把分组名与子表头合并成一个列名），
                但只对长度 ≥2 的表头放宽，避免单字符表头到处乱命中。"""
                if not h:
                    return False
                return any(c == h or (len(h) >= 2 and h in c) for c in cols)

            a["hdr_hit"] += sum(1 for h in hdr if hdr_hit(h))
            a["cell_hit"] += sum(1 for c in cells if c in recon)

            # F1 口径：单元格用集合交集（无重复计数歧义）；
            # 表头用一对一贪心（宽松子串下 TP 会超过预测数，见 _greedy_1to1 注释）
            a["f1_cell_pred"] += len(recon)
            a["f1_cell_tp"] += len(gt_set & recon)
            uniq_cols = sorted(set(cols))
            a["f1_hdr_pred"] += len(uniq_cols)
            a["f1_hdr_tp"] += _greedy_1to1(
                [h for h in hdr if h], uniq_cols,
                lambda h, c: c == h or (len(h) >= 2 and h in c))

            a["struct_ok"] += 1 if (df is not None and len(df) >= len(rendered) - 2) else 0
            print(f"  t{ti} {vname:7s} 重建={'表' if df is not None else '纯文本'}"
                  f" 列名 {a['hdr_hit']}/{a['hdr_total']} 单元格 {a['cell_hit']}/{a['cell_total']}")
    print()
    summary_a = {}
    for vname, _ in variants:
        a = agg[vname]
        if not a["n"]:
            continue
        hr = a["hdr_hit"] / max(a["hdr_total"], 1)
        cr = a["cell_hit"] / max(a["cell_total"], 1)
        sr = a["struct_ok"] / a["n"]
        cell_p, cell_r, cell_f = _prf(
            a["f1_cell_tp"], a["f1_cell_pred"], a["f1_cell_gt"])
        hdr_p, hdr_r, hdr_f = _prf(
            a["f1_hdr_tp"], a["f1_hdr_pred"], a["f1_hdr_gt"])
        summary_a[vname] = {"header_acc": hr, "cell_recall": cr, "struct_rate": sr,
                            "cell_precision": cell_p, "cell_f1": cell_f,
                            "header_precision": hdr_p, "header_recall": hdr_r,
                            "header_f1": hdr_f,
                            "errors": a["errors"], "n": a["n"]}
        print(f"  {vname:8s} 列名准确率 {hr*100:5.1f}% | 单元格召回率 {cr*100:5.1f}%"
              f" | 结构还原率 {sr*100:5.1f}% | 崩溃 {a['errors']}/{a['n']}")
        print(f"  {'':8s} 单元格  P {cell_p*100:5.1f}%  R {cell_r*100:5.1f}%"
              f"  **F1 {cell_f*100:5.1f}%**"
              f"   | 表头  P {hdr_p*100:5.1f}%  R {hdr_r*100:5.1f}%  F1 {hdr_f*100:5.1f}%")
    return summary_a


# ---------------- B. 公式识别 ----------------
def render_text(text, path, font_size=22, width=1000):
    from PIL import Image, ImageDraw
    font = _load_font(font_size)
    lines, cur = [], ""
    for ch in text:
        if font.getlength(cur + ch) > width - 60:
            lines.append(cur); cur = ch
        else:
            cur += ch
    lines.append(cur)
    H = (font_size + 12) * len(lines) + 40
    img = Image.new("RGB", (width, H), "white")
    d = ImageDraw.Draw(img)
    y = 20
    for ln in lines:
        d.text((30, y), ln, fill="black", font=font)
        y += font_size + 12
    img.save(path)
    return path


def part_b(n=10):
    print()
    print("=" * 78)
    print("B. 公式识别（预期很差 —— 这正是需要视觉大模型的原因）")
    print("=" * 78)
    rows_pq = pq.ParquetFile(os.path.join(OHR, "OHR-Bench.parquet")).read().to_pylist()
    random.seed(SEED + 7)
    random.shuffle(rows_pq)
    eqs = []
    for r in rows_pq:
        q = r.get("qas")
        if not isinstance(q, dict):
            continue
        src = q.get("evidence_source") or []
        ctx = q.get("evidence_context") or []
        for s, c in zip(src, ctx):
            if s == "equation" and c and 12 < len(c) < 160:
                eqs.append(c.strip())
        if len(eqs) >= n:
            break
    eqs = eqs[:n]
    print(f"取 {len(eqs)} 条 OHR 公式证据文本\n")
    tot_chars = tot_hit = exact = 0
    samples = []
    for i, eq in enumerate(eqs, 1):
        p = os.path.join(OUT, f"f{i}.png")
        render_text(eq, p)
        res = image_tool.ocr_to_table(p)
        got = norm(res.get("text"))
        want = norm(eq)
        # 字符召回：期望字符里有多少出现在了 OCR 结果中（顺序与结构不要求，已足够宽松）
        hit = sum(1 for ch in set(want) if ch in got)
        tot_chars += len(set(want)); tot_hit += hit
        exact += 1 if want and want == got else 0
        if i <= 3:
            samples.append({"want": eq[:70], "got": (res.get("text") or "")[:70]})
    cr = tot_hit / max(tot_chars, 1)
    print(f"  字符级召回率（宽松，忽略二维结构）: {cr*100:.1f}%")
    print(f"  完全正确条数（含结构与顺序）    : {exact}/{len(eqs)}")
    for s in samples:
        print(f"    期望: {s['want']!r}")
        print(f"    实得: {s['got']!r}")
    return {"char_recall": cr, "exact": exact, "n": len(eqs)}


# ---------------- C. 图表识别 ----------------
def part_c(engine="ocr"):
    print()
    print("=" * 78)
    print(f"C. 图表识别（engine={engine}）")
    print("=" * 78)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    values = [2273, 919, 2143, 349, 3272]
    labels = ["Table", "Chart", "Formula", "Hand-written", "Text"]
    p = os.path.join(OUT, "chart_bar.png")
    fig, ax = plt.subplots(figsize=(7, 4.2), dpi=150)
    ax.bar(labels, values, color="#4a7ebb")
    for i, v in enumerate(values):
        ax.text(i, v + 40, str(v), ha="center", fontsize=10)
    ax.set_title("OHR-Bench evidence type distribution")
    ax.set_ylabel("count")
    fig.tight_layout(); fig.savefig(p); plt.close(fig)

    if engine == "vlm":
        # 图表是本地 OCR 的结构性盲区：数据序列在图上不是「文字」。
        # VLM 才是唯一能还原「哪个值属于哪个类别」的通道 —— 所以这一节
        # 要**真正验证映射**，而不是只数「数值有没有出现」。
        from tools import vlm_tool
        got = vlm_tool.recognize_chart(p)
        cats = got.get("categories") or []
        vals = got.get("values") or []
        want = dict(zip(labels, values))
        hits, total = 0, len(values)
        for c, v in zip(cats, vals):
            if v is None:
                continue
            for lbl, wv in want.items():
                if norm(c) == norm(lbl) or norm(lbl) in norm(c) or norm(c) in norm(lbl):
                    try:
                        if abs(float(v) - wv) <= max(1.0, abs(wv) * 0.02):
                            hits += 1
                    except (TypeError, ValueError):
                        pass
                    break
        txt = json.dumps(got, ensure_ascii=False)
        found = sum(1 for v in values if str(v) in txt)
        print(f"  模型判定的图表类型: {got.get('chart_type')}")
        print(f"  读出类别: {cats}")
        print(f"  读出数值: {vals}")
        print(f"  柱值召回: {found}/{len(values)}")
        print(f"  **类别→数值映射正确: {hits}/{total}**（本地 OCR 读不出来的正是这个）")
        if got.get("length_mismatch"):
            print(f"  ⚠️ 类别与数值长度不一致: {got['length_mismatch']}")
        if got.get("error"):
            print(f"  ⚠️ {got['error']}")
        return {"is_table": False, "value_recall": found / len(values),
                "blocks": 0, "mapping_verified": True,
                "mapping_hit": hits, "mapping_total": total,
                "chart_type": got.get("chart_type"), "raw": got.get("raw", "")}

    res = image_tool.ocr_to_table(p)
    txt = res.get("text") or ""
    found = sum(1 for v in values if str(v) in norm(txt))
    print(f"  is_table={res['is_table']}  文本块={len(res.get('cells') or [])}")
    print(f"  数值召回：{found}/{len(values)} 个柱值出现在 OCR 文本里")
    print(f"  注意：这里**只测到「数值出现了」**，并没有验证「哪个值属于哪个类别」"
          f"的对应关系是否还原正确。")
    print(f"        本地 OCR 结构上就做不到这件事（数据序列不是文字）——"
          f"要验证映射请走 --engine vlm。")
    print(f"        可确认的是：现有链路把图表判成了表格（is_table={res['is_table']}），"
          f"而图表本来不是表格结构。")
    return {"is_table": res["is_table"], "value_recall": found / len(values),
            "blocks": len(res.get("cells") or []),
            "mapping_verified": False}


def _latest_report(engine: str):
    """取指定通道最新一份报告（文件名形如 recog-<engine>-<时间戳>.json）。"""
    import glob
    files = sorted(glob.glob(os.path.join(REPORT_DIR, f"recog-{engine}-*.json")))
    if not files:
        return None, None
    try:
        with open(files[-1], encoding="utf-8") as fh:
            return json.load(fh), files[-1]
    except Exception:
        return None, None


def compare_reports():
    """把「本地 OCR」与「qwen-vl-max」两条通道的 F1 摆在一起。

    为什么要单独做成一个模式而不是在跑评测时顺手比：
    两次识别要各自调用外部接口，必须在**同一批图像、同一份比对代码**下分别跑完
    （见 main() 里就地替换 ocr_to_table 的做法），再离线对比。
    把对比耦合进评测流程，就等于让「跑 A 通道」依赖「B 通道的历史结果」。
    """
    ocr, f_ocr = _latest_report("ocr")
    vlm, f_vlm = _latest_report("vlm")
    print("=" * 78)
    print("表格识别 F1 —— 本地 OCR vs qwen-vl-max（前后对比）")
    print("=" * 78)
    if not ocr or not vlm:
        print("缺少报告，先分别运行：")
        print("  python scripts/eval_doc_recog.py --engine ocr --save")
        print("  python scripts/eval_doc_recog.py --engine vlm --save")
        return 1
    print(f"  OCR 报告: {os.path.basename(f_ocr)}")
    print(f"  VLM 报告: {os.path.basename(f_vlm)}")
    print()
    print(f"  {'干扰档':<8} {'通道':<6} {'单元格P':>9} {'单元格R':>9} {'单元格F1':>10}"
          f" {'表头F1':>8} {'结构率':>8}")
    print("  " + "-" * 68)
    gains = []
    for v in ("clean", "skew2", "blur", "lowres"):
        a = (ocr.get("table") or {}).get(v)
        b = (vlm.get("table") or {}).get(v)
        if not a or not b:
            continue
        for tag, s in (("OCR", a), ("VLM", b)):
            print(f"  {v:<8} {tag:<6} {s.get('cell_precision', 0)*100:8.1f}%"
                  f" {s.get('cell_recall', 0)*100:8.1f}%"
                  f" {s.get('cell_f1', 0)*100:9.1f}%"
                  f" {s.get('header_f1', 0)*100:7.1f}%"
                  f" {s.get('struct_rate', 0)*100:7.1f}%")
        gains.append((v, b.get("cell_f1", 0) - a.get("cell_f1", 0)))
        print()
    if gains:
        avg = sum(g for _, g in gains) / len(gains)
        print(f"  平均单元格 F1 提升：{avg*100:+.1f} 个百分点")
        worst = min(gains, key=lambda x: x[1])
        best = max(gains, key=lambda x: x[1])
        print(f"  提升最大：{best[0]} {best[1]*100:+.1f}pt；最小：{worst[0]} {worst[1]*100:+.1f}pt")
    fo = ocr.get("formula") or {}
    fv = vlm.get("formula") or {}
    print()
    print(f"  公式字符召回：OCR {fo.get('char_recall', 0)*100:.1f}%"
          f"  →  VLM {fv.get('char_recall', 0)*100:.1f}%")
    print(f"  公式完全正确：OCR {fo.get('exact', 0)}/{fo.get('n', 0)}"
          f"  →  VLM {fv.get('exact', 0)}/{fv.get('n', 0)}")
    return 0


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["ocr", "vlm"], default="ocr",
                    help="ocr = 本地 RapidOCR（基线）；vlm = 百炼 qwen-vl-max")
    ap.add_argument("--save", action="store_true", help="落盘报告")
    ap.add_argument("--compare", action="store_true",
                    help="不跑识别，直接对比最近两份 ocr / vlm 报告")
    args = ap.parse_args()

    if args.compare:
        return compare_reports()

    # part A/B/C 都要从 OHR-Bench 取页面对比识别结果；缺数据集时给可操作提示。
    from _preflight import require_ohr
    rc = require_ohr()
    if rc:
        return rc

    if args.engine == "vlm":
        # 就地替换识别入口，让 part_a / part_b 完全不感知通道差异 ——
        # 两条通道走**同一份比对代码**，前后对比才是同口径的。
        # 若各自写一套解析逻辑，分数差异里就会混进「谁解析得更宽松」的噪声。
        from tools import vlm_tool
        if not vlm_tool.available():
            print("❌ VLM 未配置（缺 VLM_API_KEY / VLM_MODEL），无法运行 vlm 通道")
            return 2
        image_tool.ocr_to_table = vlm_tool.recognize_table

    os.makedirs(OUT, exist_ok=True)
    a = part_a()
    b = part_b()
    c = part_c(engine=args.engine)
    print()
    print("=" * 78)
    print(f"### 汇总（engine = {args.engine}）")
    for v, s in a.items():
        print(f"  表格[{v:8s}] 列名 {s['header_acc']*100:5.1f}%  单元格 {s['cell_recall']*100:5.1f}%")
        print(f"  {'':14s} 单元格 P {s['cell_precision']*100:5.1f}%  R {s['cell_recall']*100:5.1f}%"
              f"  **F1 {s['cell_f1']*100:5.1f}%**")
    print(f"  公式         字符召回 {b['char_recall']*100:5.1f}%  完全正确 {b['exact']}/{b['n']}")
    print(f"  图表         识别为表={c['is_table']}  柱值召回 {c['value_recall']*100:.0f}%")
    if c.get("mapping_verified"):
        print(f"                类别→数值映射正确 {c.get('mapping_hit', 0)}/{c.get('mapping_total', 0)}")
    else:
        print("                类别→数值映射未验证（本通道读不出数据序列）")

    if args.save:
        os.makedirs(REPORT_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(REPORT_DIR, f"recog-{args.engine}-{stamp}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"engine": args.engine, "table": a, "formula": b, "chart": c},
                      fh, ensure_ascii=False, indent=2)
        print(f"\n报告已落盘：{path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
