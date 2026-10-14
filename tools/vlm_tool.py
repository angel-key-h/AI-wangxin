# -*- coding: utf-8 -*-
"""视觉语言模型识别通道（任意 OpenAI 兼容的多模态模型）。

模型名由 `Config.VLM_MODEL` 决定，**本模块不绑定具体提供方**：
  · 默认值 `qwen-vl-max`（阿里云百炼）；
  · 本仓库实测也在 `deepseek-flash` 上跑通（该模型输入模态含 image），
    且复用 `LLM_API_KEY` 即可，无需单独申请 VLM key。
报告里的模型名一律以 `Config.VLM_MODEL` / 报告溯源字段为准，不要写死。

## 为什么需要第二条通道

本地 RapidOCR 是「文本行检测 + 文本识别」：它把一张图理解成**一堆文本行**。
两类内容因此天然落在它的能力之外：

  · **公式** —— 分式、上下标、根号、矩阵是二维结构。读成一行文本，语义就没了。
    基线实测字符召回仅 28.2%（eval_doc_recog.py 的 part_b）。
  · **图表** —— 数据序列根本不体现为文字，图上只有坐标轴标签和标题。
    基线只能读到轴文字，读不到「哪个值属于哪个类别」。

这两类必须交给真正「看图」的模型。

## 设计约束：返回结构与 image_tool.ocr_to_table() 保持一致

上层（eval_doc_recog.py / app.py）因此可以无差别切换通道 —— 这是能做
**同口径前后对比**的前提。多返回一个 `engine` 字段标明来源，便于报告里归因。

## 降级原则

VLM 不可用（没配 key / 网络失败 / 额度耗尽）时**不能抛异常**，而是返回一份
带 `warnings` 的空结果，让调用方决定是否退回本地 OCR。识别链路不该因为
一个外部接口抖动就整条断掉。
"""
import base64
import io
import json
import os
import re

import pandas as pd

from config import Config

MAX_SIDE = 1600          # 送图前的最长边：过大既慢又容易触发接口尺寸上限
REQUEST_TIMEOUT = 120

_TABLE_PROMPT = """请把这张图片的内容**完整、准确地**转写出来。

【如果图中是表格】输出 Markdown 表格，并遵守：
1. 只输出表格本身，不要任何解释、标题或前后缀。
2. 逐格照抄，**不要翻译、不要改写、不要补全、不要四舍五入**。
3. 列名照抄（包括单位、上下标里的字符）；层级表头用「父级_子级」形式合并到同一列名。
4. 空单元格保留为空，不要填 0 或 -。
5. 合并单元格：把内容重复填在该单元格覆盖的每一列上。

【如果图中**不是**表格（纯文本 / 公式 / 图表标题与标注）】逐字转写：
1. 数学公式用 LaTeX 表示（行内 $...$，独立成行 $$...$$）。
2. 不要翻译、不要改写、不要补全。
3. 保留原有的换行结构。

无论哪种情况，都只输出内容本身，不要任何说明性文字。"""

_TEXT_PROMPT = """请把这张图片里的文字**逐字**转写出来。

严格要求：
1. 只输出文字内容本身，不要任何解释。
2. 数学公式用 LaTeX 表示（行内用 $...$，独立成行用 $$...$$）。
3. 不要翻译、不要改写、不要补全。
4. 保留原有的换行结构。"""

_CHART_PROMPT = """这张图是一个图表。请读出它表达的数据。

只输出一个 JSON 对象，不要任何解释、不要 markdown 代码块围栏：
{"chart_type": "bar|line|pie|scatter|other",
 "categories": ["类别1", "类别2"],
 "values": [数值1, 数值2],
 "title": "图表标题",
 "x_label": "横轴名", "y_label": "纵轴名"}

要求：
1. categories 与 values 必须**一一对应**且长度相同。
2. 数值照抄图上标注或按刻度读出，不要臆造。读不准的用 null。
3. X 轴上没有文字标签时，categories 用 "1","2","3"... 占位。
4. 若图中确实没有可读的数据序列，返回 {"chart_type":"none"}。"""


def available() -> bool:
    """VLM 通道是否可用（有 key 且配了模型名）。"""
    return Config.vlm_ready()


def _data_url(image_path: str, max_side: int = MAX_SIDE) -> str:
    """图片 → data URL。超过 max_side 先等比缩放。

    缩放有实际意义：图表/公式图在本项目里都是我们自己渲染的，动辄 2000px 宽，
    直接送会明显变慢且无助于识别（文字本身不需要那么大）。
    """
    from PIL import Image
    img = Image.open(image_path)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > max_side:
        k = max_side / max(w, h)
        img = img.resize((max(1, int(w * k)), max(1, int(h * k))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


_client = None


def _llm():
    """懒加载：模块级建实例会在 import 期就要求 key，而 VLM 是可选能力。"""
    global _client
    if _client is None:
        from langchain_openai import ChatOpenAI
        _client = ChatOpenAI(
            model=Config.VLM_MODEL,
            api_key=Config.VLM_API_KEY,
            base_url=Config.VLM_BASE_URL,
            temperature=0,
            timeout=REQUEST_TIMEOUT,
        )
    return _client


def _ask(image_path: str, prompt: str) -> str:
    """发一张图 + 一段指令，返回模型文本。异常一律转成 RuntimeError 由上层收拾。"""
    from langchain_core.messages import HumanMessage
    msg = HumanMessage(content=[
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": _data_url(image_path)}},
    ])
    out = _llm().invoke([msg])
    return (getattr(out, "content", "") or "").strip()


def _strip_fence(text: str) -> str:
    """模型偶尔仍会包 ```markdown 围栏，去掉。"""
    t = text.strip()
    m = re.match(r"^```[a-zA-Z]*\s*\n(.*?)\n?```$", t, re.S)
    return m.group(1).strip() if m else t


_SEP_CELL = re.compile(r"^:?-{2,}:?$")


def md_table_to_df(text: str):
    """Markdown 表 → DataFrame。解析不出表就返回 None。

    只认以 ``|`` 开头的行；分隔行（``|---|---|``）跳过。行长度不齐时按最宽的补齐，
    因为模型偶尔会漏掉行尾的空格子 —— 那属于格式抖动，不该让整张表作废。
    """
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and all(_SEP_CELL.match(c) for c in cells if c):
            continue
        rows.append(cells)
    if len(rows) < 2:
        return None
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    header = [h if h else f"col{i+1}" for i, h in enumerate(rows[0])]
    df = pd.DataFrame(rows[1:], columns=header)
    # 只清掉整行皆空的行；**列不删** —— 图上有的列就是有的，
    # 删掉会让「漏识别」看起来像「本来就没有这一列」，对不上评测口径。
    df = df[(df.astype(str) != "").any(axis=1)].reset_index(drop=True)
    return df if not df.empty else None


def _degrade(reason: str, text: str = "") -> dict:
    return {"dataframe": None, "text": text, "cells": [], "mean_score": None,
            "warnings": [reason], "is_table": False, "engine": "vlm"}


def recognize_table(image_path: str) -> dict:
    """表格识别：返回与 image_tool.ocr_to_table() 同构的字典。"""
    if not available():
        return _degrade("VLM 未配置（缺 VLM_API_KEY / VLM_MODEL），本项跳过")
    try:
        raw = _strip_fence(_ask(image_path, _TABLE_PROMPT))
    except Exception as e:
        return _degrade(f"VLM 调用失败：{type(e).__name__}: {e}")

    warnings = []
    df = md_table_to_df(raw)
    if df is None:
        # 不是表格（或模型没按表格式输出）→ 原文本身就是要的转写结果。
        # 于是本函数同时服务表格评测与公式评测，调用方不必切换入口，
        # 也避免「换个入口 = 换套解析逻辑」把通道差异混进分数里。
        return {"dataframe": None, "text": raw, "cells": [], "mean_score": None,
                "warnings": [], "is_table": False, "engine": "vlm"}

    empty = int((df.astype(str) == "").sum().sum())
    if empty:
        warnings.append(f"有 {empty} 个空单元格（可能本来为空，也可能模型漏抄）")
    return {"dataframe": df, "text": raw, "cells": [], "mean_score": None,
            "warnings": warnings, "is_table": True, "engine": "vlm"}


def recognize_text(image_path: str) -> dict:
    """纯文本 / 公式识别：text 字段给 LaTeX 或原文。"""
    if not available():
        return _degrade("VLM 未配置（缺 VLM_API_KEY / VLM_MODEL），本项跳过")
    try:
        raw = _strip_fence(_ask(image_path, _TEXT_PROMPT))
    except Exception as e:
        return _degrade(f"VLM 调用失败：{type(e).__name__}: {e}")
    return {"dataframe": None, "text": raw, "cells": [], "mean_score": None,
            "warnings": [], "is_table": False, "engine": "vlm"}


def recognize_chart(image_path: str) -> dict:
    """图表识别：读出 类别 → 数值 的对应关系。

    这是本地 OCR 做不到的部分（它只拿得到轴上的文字，拿不到「谁对应谁」）。
    返回 {"chart_type", "categories", "values", "title", ...}；
    解析失败时给 {"chart_type": "none", "error": ...}。
    """
    if not available():
        return {"chart_type": "none", "error": "VLM 未配置"}
    try:
        raw = _strip_fence(_ask(image_path, _CHART_PROMPT))
    except Exception as e:
        return {"chart_type": "none", "error": f"{type(e).__name__}: {e}"}

    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {"chart_type": "none", "error": "未返回 JSON", "raw": raw[:200]}
    try:
        blob = json.loads(m.group(0))
    except Exception as e:
        return {"chart_type": "none", "error": f"JSON 解析失败：{e}", "raw": raw[:200]}

    cats = blob.get("categories") or []
    vals = blob.get("values") or []
    if len(cats) != len(vals):
        blob["length_mismatch"] = [len(cats), len(vals)]
    blob.setdefault("chart_type", "other")
    blob["raw"] = raw[:400]
    return blob
