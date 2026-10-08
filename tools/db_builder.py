# -*- coding: utf-8 -*-
"""把 rag_data 下的表格文件自动建成 SQLite 表。

解决的具体死局：综测数据只躺在 rag_data 的 xlsx 里，SQL 侧只有 chinook
（音乐商店，Customer/Invoice 之类），用户问「智育前十是谁」时：
  路由判成 sql → LLM 在 schema 里找不到任何相关表 → 输出 NOSQL → 直接失败，
  而且因为意图是 sql，它连 RAG 那条路都不会走。
把 xlsx 建成表之后，这个问句就是一条普通的 ORDER BY ... LIMIT 10。

做法：以 chinook.sqlite 为基底复制一份 agent.sqlite（纯文件复制，不涉及
连接句柄），再把 rag_data 里每个表格建成表，用源文件指纹决定是否重建。
"""
import hashlib
import json
import os
import re
import shutil
import sqlite3

import pandas as pd

from config import Config
from tools.file_utils import TEXT_ENCODINGS, sniff_kind

META_SUFFIX = ".meta.json"
MAX_TABLE_NAME = 40
# 连括号带内容一起删掉（文件名里的【电子学院2025级…】这类前缀对问数没有信息量，
# 只删括号字符会留下又长又没用的表名）
_BRACKETED = re.compile(r"[【\[（(《<][^】\]）)》>]*[】\]）)》>]")
_WHITESPACE = re.compile(r"[\s\u3000.\-]+")


def sanitize_table_name(name: str) -> str:
    """清理表名。

    SQLite 允许中文标识符（码点 > 127 的字符天然合法），LLM 生成 SQL 时
    直接写 `SELECT 姓名 FROM 综合素质测评情况` 即可，不用额外引号。
    """
    cleaned = _BRACKETED.sub("", name or "")
    cleaned = _WHITESPACE.sub("", cleaned).strip()
    return cleaned[:MAX_TABLE_NAME] or "data"


def table_name_for(fname: str, sheet: str, multi_sheet: bool) -> str:
    base = sanitize_table_name(os.path.splitext(fname)[0])
    if multi_sheet:
        return f"{base}_{sanitize_table_name(str(sheet))}"
    return base


def read_frames(path: str) -> dict:
    """读**表格类**文件，返回 {工作表/文件名: DataFrame}；不是表格就返回 {}。

    判据必须「真实类型 + 扩展名」两道，不能只看 sniff_kind：
    sniff_kind 把 .txt / .json / .csv 统统归为 "text"，于是纯文本资料和清单文件
    也会被丢给 pd.read_csv —— 实测后果是新闻稿 .txt 抛「CSV 无法解析」、
    清单 .json 被当成「1 列 16 行」的表建进问数库（污染 schema，让 LLM 看到假表）。
    """
    kind = sniff_kind(path)
    if kind == "xlsx":
        try:
            return pd.read_excel(path, sheet_name=None) or {}
        except ImportError as e:
            raise RuntimeError("读取 Excel 需要 openpyxl，请执行：pip install openpyxl") from e
    if kind == "text" and os.path.splitext(path)[1].lower() in (".csv", ".tsv"):
        sep = "\t" if path.lower().endswith(".tsv") else ","
        last = None
        for enc in TEXT_ENCODINGS:
            try:
                return {os.path.basename(path): pd.read_csv(path, encoding=enc, sep=sep)}
            except Exception as e:
                last = e
        raise RuntimeError(f"CSV 无法解析: {last}")
    return {}


def source_files() -> list:
    """参与建库的全部源文件：基底库 + rag_data 下所有文件"""
    files = []
    base = Config.SQL_BASE_DB_PATH
    if os.path.exists(base):
        files.append(base)
    data_dir = Config.RAG_DATA_DIR
    if os.path.isdir(data_dir):
        for name in sorted(os.listdir(data_dir)):
            # 下划线/点开头的是清单、缓存等元数据，不是知识文档 —— 不能建表也不能入库
            if name.startswith(("_", ".")):
                continue
            fp = os.path.join(data_dir, name)
            if os.path.isfile(fp):
                files.append(fp)
    return files


def _fingerprint(files: list) -> str:
    items = []
    for p in files:
        try:
            st = os.stat(p)
        except OSError:
            continue
        items.append([os.path.basename(p), st.st_size, int(st.st_mtime)])
    return hashlib.sha1(json.dumps(items, ensure_ascii=False).encode("utf-8")).hexdigest()


def _read_meta(meta_path: str) -> dict:
    try:
        with open(meta_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def build_database(force: bool = False) -> str:
    """构建（或复用）供 SQL 问数使用的库，返回文件路径"""
    dest = Config.SQL_DB_PATH
    base = Config.SQL_BASE_DB_PATH
    meta_path = dest + META_SUFFIX

    files = source_files()
    fingerprint = _fingerprint(files)
    meta = _read_meta(meta_path)

    if not force and os.path.exists(dest) and meta.get("fingerprint") == fingerprint:
        print(f"[SQL] 复用已有数据库 {os.path.basename(dest)}，"
              f"含表 {len(meta.get('table_names') or [])} 张（来自数据文件）")
        return dest

    os.makedirs(os.path.dirname(dest), exist_ok=True)

    if os.path.exists(base):
        # 纯文件复制，不打开连接，避免 Windows 下句柄占用
        shutil.copy2(base, dest)
        print(f"[SQL] 以 {os.path.basename(base)} 为基底重建 {os.path.basename(dest)}")
    else:
        if os.path.exists(dest):
            os.remove(dest)
        print("[SQL] 未找到基底库，从空库开始")

    old_names = set(meta.get("table_names") or [])
    new_names = []
    detail = []

    con = sqlite3.connect(dest)
    try:
        # 先删掉上一轮建过、这次不再产生的表（只动我们建的表，不碰基底库自带的）
        for t in old_names:
            con.execute(f'DROP TABLE IF EXISTS "{t}"')

        for fp in files:
            if fp == base:
                continue
            fname = os.path.basename(fp)
            try:
                frames = read_frames(fp)
            except Exception as e:
                print(f"[SQL] !! 跳过 {fname}: {type(e).__name__}: {e}")
                continue
            if not frames:
                print(f"[SQL] 跳过非表格文件: {fname}")
                continue
            multi = len(frames) > 1
            for sheet, df in frames.items():
                if df is None or df.empty:
                    continue
                tname = table_name_for(fname, sheet, multi)
                if tname in new_names:
                    tname = f"{tname}_{len(new_names)}"
                df.to_sql(tname, con, if_exists="replace", index=False)
                new_names.append(tname)
                detail.append(f"{tname}（{len(df)} 行 × {len(df.columns)} 列）")
                print(f"[SQL] 建表 {tname} <- {fname}[{sheet}]  {len(df)} 行 × {len(df.columns)} 列")
        con.commit()
    finally:
        con.close()

    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({"fingerprint": fingerprint,
                   "table_names": new_names,
                   "tables": detail}, f, ensure_ascii=False, indent=2)

    if detail:
        print(f"[SQL] 数据库构建完成，来自数据文件的表: {'、'.join(detail)}")
    else:
        print("[SQL] 数据库构建完成，但 rag_data 下没有可建表的文件")
    return dest
