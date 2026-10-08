# -*- coding: utf-8 -*-
"""运行时一键初始化：把「第一次跑起来」需要的产物全部准备好。

由 start.bat 调用，也可以手动执行：

    python scripts/init_runtime.py            # 缺什么补什么（幂等）
    python scripts/init_runtime.py --force    # 强制重建问数库
    python scripts/init_runtime.py --schema-only

做四件事，每件都幂等，重复执行不会破坏已有产物：

  1. 确认基底库 data/chinook.sqlite 存在
     —— 缺了就调用 scripts/init_chinook.py 兜底生成。
        注意那是**精简版**（Track 仅十几行），只保证「能跑」；
        完整版（Track 3503 行）随交付包一起提供，正常不会走到兜底分支。
  2. 构建问数库 data/agent.sqlite（Chinook 基底 + rag_data 表格文件）
  3. 导出 data/schema.sql（赛题硬性交付物）
  4. 打印自检小结

向量索引**不在这里建**：它需要联网调用 Embedding 接口，属于首次提问时
由 RAGTool 惰性构建。把网络依赖塞进启动脚本会让「没配 Key」直接启动失败。
"""
import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from config import Config  # noqa: E402


def _say(msg: str):
    print(msg, flush=True)


def ensure_base_db() -> bool:
    base = Config.SQL_BASE_DB_PATH
    if os.path.exists(base):
        _say(f"[1/4] 基底库已就绪: {os.path.relpath(base, ROOT)}")
        return True
    _say(f"[1/4] 缺少基底库 {os.path.relpath(base, ROOT)}，调用 init_chinook.py 兜底生成…")
    script = os.path.join(ROOT, "scripts", "init_chinook.py")
    r = subprocess.run([sys.executable, script], cwd=ROOT)
    if r.returncode == 0 and os.path.exists(base):
        _say("      ⚠️ 已生成**精简版**示例库（仅够跑通流程）。"
             "完整 Chinook（Track 3503 行）请从交付包 data/ 目录取回。")
        return True
    _say("      ❌ 生成失败，请检查 scripts/init_chinook.py 的输出。")
    return False


def build_db(force: bool) -> bool:
    _say("[2/4] 构建问数库 data/agent.sqlite …")
    try:
        from tools.db_builder import build_database
    except Exception as e:
        _say(f"      ❌ 依赖缺失，无法构建：{type(e).__name__}: {e}")
        _say("         先执行：python -m pip install -r requirements.txt")
        return False
    try:
        dest = build_database(force=force)
    except Exception as e:
        _say(f"      ❌ 构建失败：{type(e).__name__}: {e}")
        return False
    _say(f"      ✅ {os.path.relpath(dest, ROOT)}")
    return True


def export_schema() -> bool:
    _say("[3/4] 导出 schema 定义文件 data/schema.sql …")
    script = os.path.join(ROOT, "scripts", "export_schema.py")
    r = subprocess.run([sys.executable, script], cwd=ROOT)
    if r.returncode != 0:
        _say("      ❌ 导出失败")
        return False
    return True


def summary():
    _say("[4/4] 自检")
    import importlib
    import sqlite3

    db = Config.SQL_DB_PATH
    if os.path.exists(db):
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            names = [r[0] for r in con.execute(
                "select name from sqlite_master where type='table' "
                "and name not like 'sqlite_%'")]
            total = 0
            for t in names:
                total += con.execute(f'select count(*) from "{t}"').fetchone()[0]
            _say(f"      问数库：{len(names)} 张表 / {total} 行")
        finally:
            con.close()

    kb = Config.RAG_DATA_DIR
    docs = [f for f in os.listdir(kb)
            if os.path.isfile(os.path.join(kb, f))] if os.path.isdir(kb) else []
    exts = sorted({os.path.splitext(f)[1].lower() for f in docs})
    _say(f"      知识库：{len(docs)} 份文档 / {len(exts)} 种格式 {exts}")

    missing = Config.missing_keys()
    if missing:
        _say(f"      ⚠️ .env 缺少 {'、'.join(missing)} —— 问数/问答会返回 401，请先补上")
    else:
        _say("      密钥：LLM / Embedding 已配置")
    _say(f"      VLM：{'已配置（' + Config.VLM_MODEL + '）' if Config.vlm_ready() else '未配置，图像识别将退回本地 OCR'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="强制重建问数库")
    ap.add_argument("--schema-only", action="store_true", help="只导出 schema.sql")
    args = ap.parse_args()

    if args.schema_only:
        return 0 if export_schema() else 1

    ok = ensure_base_db()
    ok = build_db(args.force) and ok
    ok = export_schema() and ok
    summary()
    _say("\n初始化完成，可以执行：streamlit run app.py" if ok else "\n初始化未全部成功，请查看上方报错")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
