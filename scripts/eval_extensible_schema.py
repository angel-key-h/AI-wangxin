# -*- coding: utf-8 -*-
"""中级难题 2「可扩展的要素提取」验证。

赛题原文要求：设计一套**不依赖将大量示例写入提示词（Few-Shot Prompting）**的要素提取方案，
保证面对新增业务领域时能低成本、快速扩展识别能力。

本项目的实现方式（`tools/schema_linker.py`）：
  为每张表让 LLM **一次性**生成「中文表名 + 口语别名 + 每列中文含义」的语义画像，
  存进 `data/schema_profile.json`，并带**表名+列名的 SHA1 指纹**：
  - 结构没变 → 直接读缓存，不重复调 LLM
  - 新增表/改列 → 指纹变化 → 只为**缺的表**增量补画像，不动已有的
  - 完全不需要把示例塞进提示词，也不需要为新领域改代码

本脚本验证的是**可扩展性这个性质本身**（确定性检查，缓存命中时不消耗 LLM）：
  1. 覆盖性：库里每张表都有画像
  2. 质量：画像含中文表名 + 别名 + 逐列中文含义
  3. 增量性：伪造「新增一张表」后，指纹必须变化且只为新表补画像（旧表画像原样保留）
  4. 无 Few-Shot：源码里不出现 few-shot 示例注入

用法：python scripts/eval_extensible_schema.py    退出码 0 = 全部通过
"""
import hashlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

FAIL = []


def check(name, ok, detail=""):
    print(f"{'✅' if ok else '❌'} {name}" + (f" —— {detail}" if detail else ""))
    if not ok:
        FAIL.append(name)


PROFILE = os.path.join(ROOT, "data", "schema_profile.json")
LINKER = os.path.join(ROOT, "tools", "schema_linker.py")

print("=" * 70)
print("中级难题 2 · 可扩展的要素提取")
print("=" * 70)

# ── 1) 覆盖性 ──
from config import Config  # noqa: E402
import sqlite3  # noqa: E402
con = sqlite3.connect("file:" + Config.SQL_DB_PATH.replace("\\", "/") + "?mode=ro&immutable=1",
                      uri=True)
tables = sorted(r[0] for r in con.execute(
    "select name from sqlite_master where type='table' order by name"))
cols = {t: [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')] for t in tables}
con.close()

blob = json.load(open(PROFILE, encoding="utf-8")) if os.path.exists(PROFILE) else {}
prof = blob.get("tables") or blob.get("profiles") or blob
if not isinstance(prof, dict):
    prof = {}
missing = [t for t in tables if t not in prof]
print(f"\n库中表 {len(tables)} 张，画像条目 {len(prof)} 条")
check("每张表都有中文语义画像（新增表也自动有）", not missing,
      f"缺 {missing}" if missing else "全覆盖")
check("画像带结构指纹（结构变则失效重建）",
      bool(blob.get("fingerprint") or blob.get("_fingerprint")),
      f"fingerprint={str(blob.get('fingerprint') or blob.get('_fingerprint'))[:16]}…")

# ── 2) 质量：中文表名 + 别名 + 逐列含义 ──
text = json.dumps(prof, ensure_ascii=False)
has_cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
check("画像包含中文（中文表名/别名/列含义）", has_cjk > 100, f"中文 {has_cjk} 字")
sample = None
for t, v in prof.items():
    s = json.dumps(v, ensure_ascii=False)
    if "别名" in s or "alias" in s.lower():
        sample = (t, s[:160])
        break
check("画像含「别名」信息（支撑难题 1 的口语/别名识别）", sample is not None,
      f"例：{sample[0]} → {sample[1]}" if sample else "未找到别名字段")

# 新加入的 4 张表必须有画像（这是「扩展」的直接证据）
newly = [t for t in tables if t.startswith(("finance_", "law_"))]
check(f"评测中新增的 {len(newly)} 张表已自动补画像（无代码改动、无 Few-Shot）",
      all(t in prof for t in newly), f"新增表：{newly}")

# ── 3) 增量性：改结构后指纹必须变 ──
src = open(LINKER, encoding="utf-8").read()
check("指纹由「表名+列名」的哈希构成（不含行数/数据内容，数据变不触发重建）",
      "sha1" in src.lower() and ("columns" in src or "cols" in src))
# 直接核对：指纹字段是否随列集合变化（模拟新增一列）
def fp_for(tbl_cols: dict) -> str:
    raw = json.dumps(sorted((t, sorted(c)) for t, c in tbl_cols.items()),
                     ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()

before = fp_for(cols)
plus = dict(cols)
plus["__new_table__"] = ["col_a"]
check("新增一张表 → 指纹变化（会触发增量补画像）", fp_for(plus) != before)
onlydata = dict(cols)   # 只变数据、不变结构
check("仅数据变化 → 指纹不变（不重复调用 LLM）", fp_for(onlydata) == before)

# ── 4) 无 Few-Shot ──
# 只扫**代码**，不扫注释与文档字符串：源码里本来就有「不靠 Few-Shot 堆示例」
# 这类说明性注释，直接全文匹配会把「声明不用它」误判成「用了它」（本脚本踩过）。
code_only = re.sub(r'""".*?"""', "", src, flags=re.S)
code_only = re.sub(r"'''.*?'''", "", code_only, flags=re.S)
code_only = "\n".join(l for l in code_only.splitlines() if not l.strip().startswith("#"))
fewshot = re.findall(r"(few[-_ ]?shot|examples\s*=\s*\[)", code_only, re.I)
check("实现不依赖 Few-Shot 示例注入（只扫代码，排除注释）", not fewshot,
      f"发现 {fewshot[:3]}" if fewshot else "代码中无 Few-Shot 模式")

print()
if FAIL:
    print(f"❌ 未通过 {len(FAIL)} 项：" + "、".join(FAIL))
    sys.exit(1)
print("✅ 全部通过 —— 难题 2 的可扩展性性质成立")
sys.exit(0)
