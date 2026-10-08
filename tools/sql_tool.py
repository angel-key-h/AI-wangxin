# -*- coding: utf-8 -*-
"""
SQL 工具：连接数据库并执行只读查询

返回结构化数据 list[dict]，供 app.py 直接构建 DataFrame 表格。

数据库不是固定文件，而是启动时由 tools/db_builder.py 构建：
以 chinook.sqlite 为基底，叠加 rag_data 下每个表格文件建成表 ——
否则问「智育前十是谁」时 SQL 侧只有音乐商店的表，必然无解。
"""
import ast
import os

from config import Config
from tools.db_builder import build_database
from tools.schema_linker import SchemaLinker
from langchain_community.utilities import SQLDatabase
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

# 只读语句白名单前缀，防止 LLM 生成破坏性 SQL
_ALLOWED_PREFIXES = ("select", "with", "explain")


class SQLTool:
    def __init__(self):
        # 构建（或复用）问数用库；数据文件一变就自动重建
        self.db_path = build_database()
        if not os.path.exists(self.db_path):
            raise FileNotFoundError(f"数据库文件不存在: {self.db_path}")

        self.db = self._connect()
        self.linker = self._build_linker()

    def _build_linker(self) -> SchemaLinker:
        """Schema Linking 器：把整库 schema 按问题裁剪成相关表。

        惰性构建（索引/画像/向量都在第一次 select 时才建），所以不会拖慢启动。
        LLM 与 embedding 只用于「生成一次性的中文语义画像」和语义召回，
        任一不可用都只是降级，不影响主流程。
        """
        llm = emb = None
        try:
            llm = ChatOpenAI(model=Config.LLM_MODEL, api_key=Config.LLM_API_KEY,
                             base_url=Config.LLM_BASE_URL, temperature=0)
        except Exception as e:
            print(f"[Schema] LLM 不可用（{type(e).__name__}），将退回无画像的词法检索")
        try:
            emb = OpenAIEmbeddings(model=Config.EMBEDDING_MODEL,
                                   api_key=Config.EMBEDDING_API_KEY,
                                   base_url=Config.EMBEDDING_BASE_URL,
                                   # 非 OpenAI 官方端点必须关掉 token 预编码，见 rag_tool 注释
                                   check_embedding_ctx_length=False)
        except Exception:
            emb = None
        return SchemaLinker(self.db, emb, llm)

    def _connect(self) -> SQLDatabase:
        """优先建立只读连接；不可用时回退普通连接（语句白名单仍然生效）"""
        ro_uri = f"sqlite:///file:{self.db_path}?mode=ro&uri=true"
        try:
            db = SQLDatabase.from_uri(ro_uri)
            db._execute("SELECT 1", fetch="all")   # 探活，确认只读连接真的可用
            print("[SQL] 已启用只读连接")
            return db
        except Exception as e:
            print(f"[SQL] 只读连接不可用（{e}），回退为普通连接")
            return SQLDatabase.from_uri(f"sqlite:///{self.db_path}")

    def get_schema(self) -> str:
        """返回数据库表结构信息（用于 LLM 理解 schema）"""
        return self.db.get_table_info()

    def get_schema_for(self, question: str) -> dict:
        """按问题挑出相关表，返回 {"text", "chosen", "tables", "strategy", "note", "ranking"}

        取代原来的「整库 schema 直接截断」：截断会把排在后面的表整张切掉，
        在 LLM 眼里那些表等于不存在（本项目就踩过「NOSQL 死局」）。
        """
        try:
            return self.linker.schema_for(question)
        except Exception as e:
            # 检索层出任何问题都不能让问数挂掉：退回全量 schema
            print(f"[Schema] 检索失败（{type(e).__name__}: {e}），退回全量 schema")
            return {"text": self.get_schema(), "chosen": [], "tables": [],
                    "strategy": "fallback", "ranking": [],
                    "note": f"检索异常已兜底：{type(e).__name__}"}

    @staticmethod
    def is_read_only(query: str) -> bool:
        """仅放行 SELECT / WITH / EXPLAIN 开头的语句"""
        return bool(query) and query.lstrip().lower().startswith(_ALLOWED_PREFIXES)

    def run_query(self, query: str) -> dict:
        """执行只读 SQL，返回 {"result": list[dict], "error": str|None}"""
        if not query or not query.strip():
            return {"result": None, "error": "SQL 语句为空"}

        if not self.is_read_only(query):
            return {
                "result": None,
                "error": f"已拒绝非只读语句（仅允许 SELECT / WITH / EXPLAIN）: {query[:120]}",
            }

        try:
            # fetch="all" 返回 list[dict] —— 这是拿到结构化数据的唯一途径：
            # SQLDatabase.run() 无论 include_columns 取值如何都会 return str(res)
            return {"result": self.db._execute(query, fetch="all"), "error": None}
        except AttributeError:
            pass                                  # 私有接口变更，走下面的回退
        except Exception as e:
            return {"result": None, "error": str(e)}

        try:
            raw = self.db.run(query)
            rows = ast.literal_eval(raw) if raw and raw.strip() else []
            if rows and isinstance(rows[0], (list, tuple)):
                width = len(rows[0])
                rows = [dict(zip((f"列{i + 1}" for i in range(width)), r)) for r in rows]
            return {"result": list(rows), "error": None}
        except Exception as e:
            return {"result": None, "error": str(e)}
