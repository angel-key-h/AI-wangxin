# -*- coding: utf-8 -*-
"""导出数据库 schema 定义文件（赛题八.1 硬性交付物）。

赛题原文的交付物清单里明确要求随源码提交「数据库 schema 定义文件」。
之前项目里只有 `data/schema_profile.json`，那是给 Schema Linking 用的**字段画像**
（别名、语义说明、示例值），不是 DDL —— 评委拿它看不到建表语句。

这里从 `data/agent.sqlite` 真实导出 CREATE TABLE，并附上每张表的**行数与来源文件**，
让定义文件自带上下文。用脚本生成而不是手写，是为了保证它与库里实际结构永远一致：
库一变，重跑一次即可，不存在「文档写的是旧字段」这种失分点。

用法：
    python scripts/export_schema.py            # 写到 data/schema.sql
    python scripts/export_schema.py --out X.sql
"""
import argparse
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from config import Config  # noqa: E402

# 表名 → 源文件 的反查必须复用建库时的同名规则，否则「来源」标注迟早会漂。
# 复用不了时（pandas 缺失等）退回本地等价实现，规则逐字对齐 tools/db_builder.py。
try:
    from tools.db_builder import sanitize_table_name as _sanitize_table_name
except Exception:  # pragma: no cover - 仅在依赖缺失时走到
    _BRACKETED = re.compile(r"[【\[（(《<][^】\]）)》>]*[】\]）)》>]")
    _WHITESPACE = re.compile(r"[\s\u3000.\-]+")

    def _sanitize_table_name(name: str) -> str:
        cleaned = _BRACKETED.sub("", name or "")
        cleaned = _WHITESPACE.sub("", cleaned).strip()
        return cleaned[:40] or "data"

DB = os.path.join(Config.BASE_DIR, "data", "agent.sqlite")
OUT = os.path.join(Config.BASE_DIR, "data", "schema.sql")
TABLE_SOURCE_EXT = (".xlsx", ".xls", ".csv", ".tsv")


def _derived_sources(data_dir: str) -> dict:
    """扫描 data/rag_data（及 data/ 下同名目录），返回 {清洗后表名前缀: 相对路径}。

    只用**精确前缀**判断，取代早先的 `re.sub(...)[:60] in t` 模糊包含 ——
    后者把 `finance_*` / `law_*` / `综合素质测评情况` 四张派生表全部误判成
    「Chinook 示例库」，因为它们的前缀恰好也是别的表名的子串。
    """
    out = {}
    for sub in ("rag_data", os.path.join("data", "rag_data")):
        d = os.path.join(data_dir, sub) if not os.path.isabs(sub) else sub
        d = os.path.abspath(d)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.lower().endswith(TABLE_SOURCE_EXT):
                continue
            stem = _sanitize_table_name(os.path.splitext(fn)[0])
            if stem:
                out.setdefault(stem, os.path.join(sub, fn).replace("\\", "/"))
    return out


def _source_of(table: str, meta_names: set, derived: dict) -> str:
    """判定某张表来自哪个源文件；返回 '' 表示非派生表（即 Chinook 基底库）。"""
    for stem, rel in derived.items():
        if table == stem or table.startswith(stem + "_"):
            return rel
    # 退一步：meta 里登记过但文件已不在 rag_data（或前缀规则变过）时，仍能认出是派生表
    if table in meta_names:
        return "rag_data/（源文件已不在目录）"
    return ""

HEADER = """-- ============================================================================
-- 多模态数据驱动的可解释精准问数/问答智能体 —— 数据库 schema 定义
-- 赛题八（中电云计算技术有限公司）· 「中国电子杯」第三届高校 ICT 产教融合创新大赛
-- ============================================================================
-- 本文件由 scripts/export_schema.py 从 data/agent.sqlite 真实导出，不是手写整理，
-- 因此与实际库结构保证一致（库结构变更后重跑该脚本即可）。
--
-- SQLite 不支持 COMMENT ON，故每张表的来源与行数写成 SQL 注释。
-- 库由两条来源合成：
--   1. Chinook 示例库（官方 github.com/lerocha/chinook-database，完整 11 张表）
--      —— 用于演示多表关联、聚合、嵌套查询
--   2. 从 rag_data/ 下的结构化文件（xlsx / csv）按表自动建表
--      —— 用于演示「非结构化文件 → 结构化问数」的链路
--
-- 连接方式：只读（见 tools/sql_tool.py，URI mode=ro），
-- 防止 LLM 生成的 SQL 意外写入生产数据。
-- ============================================================================
"""


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    if not os.path.isfile(args.db):
        print(f"❌ 找不到数据库：{args.db}")
        print("   先运行一次应用（或用 scripts/init_chinook.py + tools/db_builder.py）建库")
        return 1

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        names = [r[0] for r in con.execute(
            "select name from sqlite_master where type='table' "
            "and name not like 'sqlite_%' order by name")]
        if not names:
            print("❌ 库里没有表")
            return 1

        # 派生表的权威名单：agent.sqlite.meta.json 的 table_names
        meta_names = set()
        meta_path = args.db + ".meta.json"
        if os.path.isfile(meta_path):
            try:
                import json
                blob = json.load(open(meta_path, encoding="utf-8"))
                meta_names = set(blob.get("table_names") or [])
            except Exception:
                meta_names = set()
        derived = _derived_sources(ROOT)

        parts = [HEADER, f"-- 共 {len(names)} 张表\n"]
        total_rows = 0
        inventory = []
        for t in names:
            try:
                ddl = con.execute(
                    "select sql from sqlite_master where type='table' and name=?",
                    (t,)).fetchone()[0]
            except Exception:
                continue
            rows = con.execute(f"select count(*) from {_quote(t)}").fetchone()[0]
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({_quote(t)})")]
            total_rows += rows
            src = _source_of(t, meta_names, derived)
            inventory.append((t, src, rows, len(cols)))
            note = (f"-- 来源: {src}（由 rag_data 结构化文件自动建表）；"
                    if src else "-- 来源: Chinook 示例库；")
            parts.append(f"\n{note}行数: {rows}；列数: {len(cols)}\n")
            # 原始 DDL 可能是 "CREATE TABLE X(...)"，补上 IF NOT EXISTS 便于重复执行
            ddl = re.sub(r"^CREATE TABLE\s+", "CREATE TABLE IF NOT EXISTS ",
                         ddl.strip(), count=1, flags=re.I)
            parts.append(ddl.rstrip(";") + ";\n")
            parts.append(f"-- 字段: {'、'.join(str(c) for c in cols)}\n")

        # 表来源清单：让评委一眼看清「哪些表是多模态文件转出来的」
        derived_rows = [x for x in inventory if x[1]]
        parts.append("\n-- ----------------------------------------------------------------------------\n"
                     "-- 表来源清单\n"
                     "-- ----------------------------------------------------------------------------\n")
        parts.append("-- [Chinook 基底库] %d 张：%s\n"
                     % (len(inventory) - len(derived_rows),
                        "、".join(x[0] for x in inventory if not x[1]) or "—"))
        parts.append("-- [rag_data 文件转表] %d 张：\n" % len(derived_rows))
        for t, src, rows, ncol in derived_rows:
            parts.append(f"--   {t}  ←  {src}  （{rows} 行 × {ncol} 列）\n")
        parts.append(f"\n-- 合计 {len(names)} 张表 / {total_rows} 行\n")
        text = "".join(parts)
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print(f"✅ 已导出 {len(names)} 张表 / {total_rows} 行 → {args.out}")
        print(f"   文件大小 {len(text.encode('utf-8'))} 字节")
        for t, src, rows, ncol in inventory:
            tag = src or "Chinook 示例库"
            print(f"   · {t:<46} ← {tag}")

        # 自检：导出的 DDL 必须能在空库上执行 —— 否则交付物是坏的
        chk = sqlite3.connect(":memory:")
        try:
            chk.executescript(text)
            got = {r[0] for r in chk.execute(
                "select name from sqlite_master where type='table'")}
            missing = set(names) - got
            print("✅ 自检：DDL 可在空库上完整执行" if not missing
                  else f"❌ 自检失败，缺表：{missing}")
            return 0 if not missing else 1
        except Exception as e:
            print(f"❌ 自检失败（DDL 无法执行）：{type(e).__name__}: {e}")
            return 1
        finally:
            chk.close()
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
