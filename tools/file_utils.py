# -*- coding: utf-8 -*-
"""文件真实类型嗅探与文本读取。

RAG 入库和 SQLite 建表两边都要判断「这个文件到底是什么」，
之前这套逻辑长在 rag_tool 里，现在抽出来共用，避免两份实现走偏。
"""
import os
import zipfile

TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "gb18030", "gbk", "utf-16", "latin-1")


def fmt_cell(value) -> str:
    """把单元格值格式化成干净文本（104.3 而不是 104.30000000000001）"""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def sniff_kind(path: str) -> str:
    """按文件头判断真实类型，而不是相信扩展名。

    本项目的实际教训：rag_data 里那个 .csv 其实是 xlsx 改了扩展名，
    只看扩展名会一路走到解码失败，数据永远进不了库。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return "unknown"

    if head[:4] == b"PK\x03\x04":                      # zip 容器
        try:
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
        except zipfile.BadZipFile:
            return "unknown"
        if any(n.startswith("xl/") for n in names):
            return "xlsx"
        if any(n.startswith("word/") for n in names):
            return "docx"
        return "zip"
    if head[:4] == b"%PDF":
        return "pdf"

    ext = os.path.splitext(path)[1].lower()
    if ext == ".md":
        return "md"
    if ext in (".xlsx", ".xlsm", ".xls"):
        return "xlsx"
    return "text"


def read_text(path: str) -> str:
    """用常见中文编码依次尝试解码纯文本"""
    last = None
    for enc in TEXT_ENCODINGS:
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, UnicodeError) as e:
            last = e
    raise RuntimeError(f"无法用常见编码解码: {last}")
