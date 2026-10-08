# -*- coding: utf-8 -*-
"""图片 → 结构化表：RapidOCR 识别 + 版面重建。

为什么必须落成「表」而不是「一段文字」：
项目后半段的能力（db_builder 建 SQLite → SQL 排名/聚合；rag_tool 按行入库 → RAG 检索）
全部以 DataFrame 为输入。只把图片转成一段文字，就只剩最弱的全文检索一条路，
问「图里智育前十是谁」这种问题依然答不了。

版面重建思路（针对 Excel / 界面截图这类轴对齐表格）：
1. RapidOCR 给出每个文本块的 4 点框 + 文字 + 置信度
2. 按框中心 y 聚类成行（同一行的文字纵向必然重叠）
3. 把所有框的 x 区间合并，用「投影空隙」切出列 —— 不依赖文字对齐方式，
   左对齐的文字和右对齐的数字都能归到同一列
4. 每个框按中心 x 落进对应列，得到 (行, 列) 矩阵
5. 取单元格最多的一行作为表头（图片上方常有标题，不能直接拿第 1 行）
6. 数值列做类型还原 —— 必须做：全是字符串时 SQLite 里 ORDER BY 会按文本排序，
   "90.7" 会排在 "104.3" 后面，排名直接错
"""
import os
import re

import pandas as pd

# 同一行的纵向容差：相对于框高的比例
_ROW_TOL_RATIO = 0.6

# 识别置信度门槛。RapidOCR 默认 0.5，会把「孤立单字符」全部丢掉 ——
# 实测它对单数字的识别置信度只有 0.26~0.50（单字符对识别模型是分布外输入），
# 恰好卡在 0.5 之下，结果是整个排名列的数字凭空消失。
#
# 两级门限：普通多字符框仍按 0.5 过滤，只有「1~2 个字符的孤立框」放宽到 0.1。
# 不把全局门限一律降到 0.1，是为了避免表格里别处的低置信污点变成伪造单元格；
# 每个单元格都保留 score，界面上按置信度着色供人工核对。
MIN_SCORE = 0.5        # 普通框门限
MIN_SCORE_SHORT = 0.1  # 孤立短数字框门限（单数字实测 0.26~0.50）
SHORT_LEN = 2
# 引擎侧的门限必须是两者中较低的那个，否则短框在引擎里就已经被丢掉了
ENGINE_TEXT_SCORE = MIN_SCORE_SHORT

_NUM_CLEAN = re.compile(r"(?<=\d)\s+(?=\d)")

_engine = None


def get_engine():
    """RapidOCR 引擎单例：首次初始化要加载模型，约 1~3 秒，不能每次调用都建"""
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR()
    return _engine


def keep_box(text: str, score: float) -> bool:
    """短数字框用低门限，其余一字不改用 0.5。

    先按 0.1 向引擎索取结果、再在这里过滤，才能保住孤立单数字；
    若对所有框一概放宽，表格别处的低置信污点会变成伪造单元格。

    只对 ASCII 短串放宽：实测低分项全是 '1'~'9' 这类孤立数字，
    而 2 字中文词（表头、姓名）正常都在 0.55~0.67，个别掉到 0.5 以下
    基本就是脏数据，宁可丢成空单元格并报警，也不要塞进一个错的值。
    """
    if score >= MIN_SCORE:
        return True
    return len(text) <= SHORT_LEN and text.isascii() and score >= MIN_SCORE_SHORT


def ocr_lines(image_path: str) -> list:
    """返回 [{"text","score","box","x0","x1","y0","y1","cx","cy","h"}]"""
    result, _ = get_engine()(str(image_path), text_score=ENGINE_TEXT_SCORE)
    items = []
    for entry in (result or []):
        box, text, score = entry[0], entry[1], entry[2]
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        text = (text or "").strip()
        score = float(score)
        if not text:
            # 框检测到了、但识别结果为空（score 通常为 0.0）—— 属识别级失败，
            # 调阈值救不回来，只能靠下游「空单元格」告警暴露给用户
            continue
        if not keep_box(text, score):
            continue
        items.append({
            "text": text,
            "score": round(score, 4),
            "box": [[float(p[0]), float(p[1])] for p in box],
            "x0": x0, "x1": x1, "y0": y0, "y1": y1,
            "cx": (x0 + x1) / 2, "cy": (y0 + y1) / 2, "h": max(y1 - y0, 1.0),
        })
    return items


def _cluster_rows(items: list) -> list:
    """按中心 y 把文本块聚成行"""
    rows = []
    for it in sorted(items, key=lambda d: d["cy"]):
        if rows:
            cur = rows[-1]
            tol = max(cur["h"], it["h"]) * _ROW_TOL_RATIO
            if abs(it["cy"] - cur["cy"]) <= tol:
                cur["items"].append(it)
                n = len(cur["items"])
                cur["cy"] = sum(x["cy"] for x in cur["items"]) / n
                cur["h"] = sum(x["h"] for x in cur["items"]) / n
                continue
        rows.append({"cy": it["cy"], "h": it["h"], "items": [it]})
    for r in rows:
        r["items"].sort(key=lambda d: d["cx"])
    return rows


def _merge_intervals(intervals: list, gap: float) -> list:
    """把 x 区间按给定空隙阈值合并；空隙 >= gap 就断成新的一段"""
    out = []
    for a, b in sorted(intervals):
        if out and a - out[-1][1] < gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _detect_columns(rows: list, items: list) -> list:
    """切列：先按 x 区间重叠合并出候选块，再合并「从不同时出现在同一行」的相邻块。

    判据为什么是「同一行里是否同时出现」：
    两段文字如果在某一行的同一横行里并排存在，那它们必然属于不同的列 ——
    这是表格的定义，跟列有多宽、空隙有多大都没关系。

    曾经用「每行单元格数的众数」估列数，结果踩了坑：
    OCR 漏掉 9 个单元格 → 众数由 6 变成 5 → 少切一列 → 学号和姓名被并成一列 →
    整张表的列全部错位。改成共现判据后，漏检一个单元格不再影响列结构。
    """
    if not items:
        return []
    blocks = _merge_intervals([(d["x0"], d["x1"]) for d in items], 0.0)
    if len(blocks) <= 1:
        return blocks

    def block_of(cx: float) -> int:
        for i, (a, b) in enumerate(blocks):
            if a <= cx <= b:
                return i
        return -1

    # 每个块出现在哪些行
    occupies = [set() for _ in blocks]
    for ri, row in enumerate(rows):
        for d in row["items"]:
            bi = block_of(d["cx"])
            if bi >= 0:
                occupies[bi].add(ri)

    cols = [list(blocks[0])]
    for i in range(1, len(blocks)):
        if occupies[i] & occupies[i - 1]:        # 同一行里并排出现过 -> 确认是两列
            cols.append(list(blocks[i]))
        else:
            cols[-1][1] = max(cols[-1][1], blocks[i][1])
    return cols


def _column_index(cols: list, cx: float) -> int:
    """落列：优先取包含中心点的列，否则取最近的一列边界"""
    for i, (a, b) in enumerate(cols):
        if a <= cx <= b:
            return i
    best, best_d = 0, None
    for i, (a, b) in enumerate(cols):
        d = 0.0 if a <= cx <= b else min(abs(cx - a), abs(cx - b))
        if best_d is None or d < best_d:
            best, best_d = i, d
    return best


_NUM_RE = re.compile(r"^[-−–—]?\d+(?:[.,]\d+)?%?$")


def _is_numeric_text(text: str) -> bool:
    """是否是「一个纯数字」—— 用来区分数据格与表头/标签格"""
    return bool(_NUM_RE.match((text or "").strip().replace(",", "")))


def _split_header(rows: list):
    """切出表头块，返回 (header_start, first_data)。

    为什么不能用「前几行里单元格最多的一行」当表头：
    学术表格常见**多层表头**（分组名 + 子指标名），分组行只有 4 格、
    子表头 7 格、数据行 8 格 —— 数据行天生格子最多，那条启发式必然选错，
    结果把一整行数据当成列名，还把真表头当「标题」丢弃。

    改用结构判据：
      1. 第一条含 >= 2 个纯数字的行 = 第一条数据行；
      2. 从它往上，连续「不少于 2 格、且一个数字都没有」的行都算表头。
    """
    first_data = None
    for i, r in enumerate(rows):
        nums = sum(1 for d in r["items"] if _is_numeric_text(d["text"]))
        if len(r["items"]) >= 2 and nums >= 2:
            first_data = i
            break
    if first_data is None or first_data == 0:
        # 找不到数据行特征（或第一行就是数据）——退回旧行为：第 1 行当表头
        return 0, min(1, len(rows))

    start = first_data
    while start > 0:
        r = rows[start - 1]
        nums = sum(1 for d in r["items"] if _is_numeric_text(d["text"]))
        if len(r["items"]) >= 2 and nums == 0:
            start -= 1
        else:
            break
    if start == first_data:
        # 紧邻上方那行不像表头（例如表头里本身带数字），至少留一行当表头
        start = max(0, first_data - 1)
    return start, first_data


def _nearest_col(cols: list, x: float) -> int:
    """离 x 最近的那一列"""
    best, best_d = 0, None
    for i, (a, b) in enumerate(cols):
        d = abs(x - (a + b) / 2)
        if best_d is None or d < best_d:
            best, best_d = i, d
    return best


# RapidOCR 会把上箭头 ↑ 稳定认成「个」（中文识别模型的字形先验）：
# 实测 "LCS@1↑"->"LCS@1个"、"EM↑"->"EM个"，而 ↓ 直接被丢掉。
# 只替换「字母开头的纯 ASCII 主体 + 结尾个」这种明显是英文指标名的情况，
# 免得误伤 "参数个数" 这类真中文表头。
_ARROW_CONFUSION = re.compile(r"^([A-Za-z][A-Za-z0-9@._\-]*)个$")


def _fix_glyphs(text: str) -> str:
    """修正已知的字形混淆（目前只有 ↑ 被认成 个 这一条）"""
    return _ARROW_CONFUSION.sub(r"\1↑", text)


def _cols_for(cell: dict, cols: list) -> list:
    """一个表头格覆盖了哪几列。

    先看哪些列的中心点落在格子的 x 范围内 —— 单层表头（以及多层里最细的那层）
    与数据列一一对应，这个判据就够，而且不会因为表头文字比列宽而左右溢出。

    一个列中心都没落进来时，才说明它是**跨列的分组名**：分组名居中于所辖几列
    之上，中心点会掉在两列之间（实测 `Retrieval` 中心 1051 正好卡在 col2/col3
    的分界上），这时改用「左边界最近的列 ~ 右边界最近的列」来定跨度。
    """
    inside = [i for i, (a, b) in enumerate(cols) if cell["x0"] <= (a + b) / 2 <= cell["x1"]]
    if inside:
        return inside
    i0 = _nearest_col(cols, cell["x0"])
    i1 = _nearest_col(cols, cell["x1"])
    return list(range(min(i0, i1), max(i0, i1) + 1))


def _header_names(header_rows: list, cols: list) -> list:
    """多层表头拼列名：跨列的分组名会并进它覆盖的每一列。

    最后做**重名去重**：跨列分组名扇出后，多个列可能得到完全相同的名字
    （分组名 + 该列没有子表头）。重名列会让下游按列名取值拿到 DataFrame 而不是
    Series（`df[col].str` 直接报 AttributeError），所以在这里就消掉。
    """
    names = [""] * len(cols)
    for r in header_rows:
        for d in r["items"]:
            text = _fix_glyphs(d["text"])
            for ci in _cols_for(d, cols):
                names[ci] = (names[ci] + " " + text).strip()
    out, seen = [], {}
    for i, n in enumerate(names):
        base = n if n else f"列{i + 1}"
        if base in seen:
            seen[base] += 1
            base = f"{base}_{seen[base]}"
        else:
            seen[base] = 1
        out.append(base)
    return out


def _coerce_types(df: pd.DataFrame) -> pd.DataFrame:
    """数值列类型还原。

    必须做：OCR 出来的全是字符串，如果原样建表，
    SQLite 里 `ORDER BY 智育测评分 DESC` 会按字典序排，
    "90.7" 会排在 "104.3" 后面，排名整个错掉。

    **按位置、并且整列替换**：两个坑叠在一起才踩得出来 ——
    1. 多层表头合并后可能出现**重名列**（跨列分组名 + 空子表头），
       此时 `df[col]` 返回 DataFrame 而不是 Series，
       `.astype(str).str` 抛 `AttributeError: 'DataFrame' object has no attribute 'str'`；
    2. 只改成 `df.iloc[:, i] = num` 还不够：pandas 3.0 的字符串列是**严格的 `str` dtype**，
       把 float Series 原地赋进去会抛
       `TypeError: Invalid value ... for dtype 'str'`。
       必须**整列替换**（先算出所有列再 concat 回来），不能 in-place 改值。
    """
    out = []
    for i in range(df.shape[1]):
        s = df.iloc[:, i]
        raw = s.astype(str).str.strip()
        # 去掉千分位逗号，以及 OCR 在数字中间插入的空格（"104 . 3" -> "104.3"）
        cand = raw.str.replace(",", "", regex=False).str.replace("，", "", regex=False)
        cand = cand.map(lambda x: _NUM_CLEAN.sub("", x))
        num = pd.to_numeric(cand, errors="coerce")
        meaningful = raw.ne("") & raw.str.lower().ne("nan")
        if meaningful.any() and num[meaningful].notna().mean() >= 0.8:
            out.append(num.reset_index(drop=True))
        else:
            out.append(s.reset_index(drop=True))
    res = pd.concat(out, axis=1) if out else df
    res.columns = list(df.columns)      # 保留原列名（允许重名）
    return res


def ocr_to_table(image_path: str) -> dict:
    """识别图片并重建表。

    返回 {
      "dataframe": DataFrame | None,
      "text": 全部文字按行拼接（非表格图片的兜底形态）,
      "cells": 每个单元格的 text/score/box/row/col（供可解释展示）,
      "mean_score": 平均置信度,
      "warnings": [str],
      "is_table": bool,
    }
    """
    warnings = []          # 硬警告：无论成败都要报
    notes = []             # 软提示：只有真的重建出表时才有意义（否则是噪音）

    items = ocr_lines(image_path)
    plain_text = "\n".join(d["text"] for d in items)

    if not items:
        return {"dataframe": None, "text": "", "cells": [], "mean_score": 0.0,
                "warnings": ["图片里没有识别到任何文字"], "is_table": False}

    mean_score = sum(d["score"] for d in items) / len(items)

    def fail(msg: str) -> dict:
        return {"dataframe": None, "text": plain_text,
                "cells": [dict(d, row=i, col=0) for i, d in enumerate(items)],
                "mean_score": mean_score, "warnings": warnings + [msg], "is_table": False}

    rows = _cluster_rows(items)
    counts = [len(r["items"]) for r in rows]

    if len(rows) < 3 or max(counts) < 2:
        return fail("版面不像表格（行数或列数不足），已按纯文本处理")

    if mean_score < 0.7:
        warnings.append(f"平均置信度偏低（{mean_score:.2f}），识别结果可能有误，建议核对")

    h_start, h_end = _split_header(rows)
    header_rows = rows[h_start:h_end]
    if not header_rows:
        return fail("版面不像表格（找不到表头），已按纯文本处理")
    if h_start > 0:
        notes.append(
            f"第 1~{h_start} 行识别为表格上方的标题（已忽略："
            f"{' / '.join(i['text'] for r in rows[:h_start] for i in r['items'])[:80]}）"
        )
    if len(header_rows) > 1:
        notes.append(f"检测到 {len(header_rows)} 层表头，已按跨列关系合并为列名")

    table_rows = rows[h_end:]
    # 去掉底部零散行（脚注/说明），只保留有 2 个以上单元格的行
    while len(table_rows) > 2 and len(table_rows[-1]["items"]) < 2:
        notes.append(f"忽略疑似脚注行：{table_rows[-1]['items'][0]['text'][:40]}")
        table_rows.pop()

    all_items = [d for r in table_rows for d in r["items"]]
    cols = _detect_columns(table_rows, all_items)
    if len(cols) < 2:
        return fail("只切出 1 列，版面重建失败，已按纯文本处理")

    header = _header_names(header_rows, cols)
    fixed = [d["text"] for r in header_rows for d in r["items"]
             if _fix_glyphs(d["text"]) != d["text"]]
    if fixed:
        notes.append("修正了 %d 处字形混淆：%s" % (
            len(fixed), "、".join(f"{t}→{_fix_glyphs(t)}" for t in fixed)))

    matrix, cells = [], []
    for ri, row in enumerate(table_rows):
        line = [""] * len(cols)
        for cell in row["items"]:
            ci = _column_index(cols, cell["cx"])
            line[ci] = (line[ci] + " " + cell["text"]).strip() if line[ci] else cell["text"]
            cells.append(dict(cell, row=ri, col=ci))
        matrix.append(line)

    # 表头单元格也纳入标注，标注图才能覆盖到列名
    for k, row in enumerate(header_rows):
        for cell in row["items"]:
            i0 = _nearest_col(cols, cell["x0"])
            i1 = _nearest_col(cols, cell["x1"])
            cells.append(dict(cell, row=-(len(header_rows) - k), col=min(i0, i1)))

    # 只占第一列的行 = 分组小标题（如 "Pipeline-based OCR"、"End-to-end OCR"）。
    # 它们是表格自带的分节信息，不是漏识别的空行，不能当缺值报警。
    group_rows = {i for i, r in enumerate(table_rows) if len(r["items"]) == 1}
    if group_rows:
        labels = [matrix[i][0] for i in sorted(group_rows) if matrix[i][0]][:6]
        notes.append(f"检测到 {len(group_rows)} 个分组小标题行（仅第一列有值）："
                     f"{'；'.join(labels)}")

    warnings.extend(notes)          # 到这里已经确认是表格，软提示才有意义

    # 漏检单元格检查：OCR 会稳定漏掉某些窄字符（如单数字排名），
    # 这属于「识别不到」而不是「该格本来就空」，必须显式报出来让人核对，
    # 否则一张静默缺值的表被当成完整数据入库，比报错更危险。
    missing = {}
    for ri, line in enumerate(matrix):
        if ri in group_rows:
            continue
        empty = [header[ci] for ci, v in enumerate(line) if v == ""]
        if empty:
            missing[ri + 2] = empty
    if missing:
        head = "；".join(f"第{r}行缺 {','.join(c)}" for r, c in list(missing.items())[:5])
        warnings.append(
            f"有 {len(missing)} 行存在识别为空的单元格（可能是漏识别，也可能是本来就空）：{head}"
            + ("……" if len(missing) > 5 else "")
        )
    # 低置信单元格（低于 0.6）同样值得核对
    low = [c for c in cells if c["score"] < 0.6]
    if low:
        warnings.append(
            f"有 {len(low)} 个单元格识别置信度低于 0.6，标注图中以黄框标出，建议重点核对"
        )

    df = pd.DataFrame(matrix, columns=header)
    df = df.loc[:, ~(df == "").all(axis=0)]          # 丢掉整列空的
    df = df[(df != "").any(axis=1)].reset_index(drop=True)   # 丢掉整行空的
    df = _coerce_types(df)

    if df.empty:
        warnings.append("重建出的表没有数据行")
        return {"dataframe": None, "text": plain_text, "cells": cells,
                "mean_score": mean_score, "warnings": warnings, "is_table": False}

    return {"dataframe": df, "text": plain_text, "cells": cells,
            "mean_score": mean_score, "warnings": warnings, "is_table": True}


def render_annotated(image_path: str, cells: list) -> "object":
    """在原图上画出识别框和行列网格，用于「可解释」展示 —— 每个数字都能指回图片位置。

    绿框 = 置信度 >= 0.85，黄框 = 低于该值需要人工核对。
    """
    from PIL import Image, ImageDraw

    img = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    if not cells:
        return img

    top = min(c["y0"] for c in cells)
    bottom = max(c["y1"] for c in cells)
    left = min(c["x0"] for c in cells)
    right = max(c["x1"] for c in cells)

    per_col, per_row = {}, {}
    for c in cells:
        per_col.setdefault(c["col"], []).append(c)
        per_row.setdefault(c["row"], []).append(c)

    for cs in per_col.values():                      # 列分隔线
        x = min(c["x0"] for c in cs)
        draw.line([(x, top), (x, bottom)], fill=(130, 130, 220), width=1)
    for cs in per_row.values():                      # 行分隔线
        y = min(c["y0"] for c in cs)
        draw.line([(left, y), (right, y)], fill=(130, 130, 220), width=1)

    for c in cells:                                  # 文本块框，按置信度着色
        box = c["box"]
        pts = [(p[0], p[1]) for p in box] + [(box[0][0], box[0][1])]
        color = (40, 180, 120) if c["score"] >= 0.85 else (220, 160, 40)
        draw.line(pts, fill=color, width=1)
    return img


def image_to_xlsx(image_path: str, out_path: str, sheet_name: str = "Sheet0") -> str:
    """把识别结果写成 xlsx —— 落到 rag_data 后，db_builder 与 rag_tool 会自动接管"""
    res = ocr_to_table(image_path)
    if res["dataframe"] is None:
        raise RuntimeError("未能从图片中重建出表格：" + "；".join(res["warnings"]))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    res["dataframe"].to_excel(out_path, index=False, sheet_name=sheet_name)
    return out_path
