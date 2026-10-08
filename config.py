# -*- coding: utf-8 -*-
"""
全局配置
"""
import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # ── 提供方：阿里云百炼（DashScope）OpenAI 兼容端点 ──
    # 地域端点必须与 API Key 的地域一致，混用会 401：
    #   北京地域  https://dashscope.aliyuncs.com/compatible-mode/v1
    #   国际地域  https://dashscope-intl.aliyuncs.com/compatible-mode/v1
    #
    # 下面这些默认值只是兜底。**真实配置在 .env，且 .env 优先级更高**
    # （load_dotenv 注入 os.environ 后，os.getenv 的默认值就不再生效）。
    # 从前这里写的是智谱的 open.bigmodel.cn + embedding-3，与 .env 完全不符：
    # 代码读起来像在用智谱，实际跑的是百炼，排查问题时极易被误导。
    #
    # key 不再硬编码（原明文 key 已失效且属于凭据泄露），一律由 .env 提供；
    # 缺失就该直接报错，而不是悄悄退回另一个提供方。
    # ── LLM ──
    LLM_API_KEY = os.getenv("LLM_API_KEY", "")
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    LLM_MODEL = os.getenv("LLM_MODEL", "qwen3-max")

    # ── Embedding ──
    # text-embedding-v4：默认 1024 维，单批上限 10 条，单条约 8192 token。
    # 换模型会改变向量空间，但索引指纹里含 EMBEDDING_MODEL（见 rag_tool
    # ._source_fingerprint），所以换完会自动重建，不会拿旧向量去撞新空间。
    EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "")
    EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-v4")
    EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1024"))

    # ── VLM（视觉语言模型：表格 / 公式 / 图表识别）──
    # 与 LLM 共用百炼的 OpenAI 兼容端点；.env 里没有单独的 VLM_API_KEY，
    # 所以 key 从 LLM_API_KEY 兜底 —— 同一个百炼账号的通用 key 即可。
    #
    # 为什么需要它：纯文本 OCR 有两个天然盲区 —— 公式是二维结构（分式/上下标/
    # 根号），图表的数据序列根本不体现为文字。实测基线（eval_doc_recog.py）
    # 公式字符召回仅 28.2%、图表只能读到坐标轴文字，正是这两个盲区。
    VLM_API_KEY = os.getenv("VLM_API_KEY", "") or os.getenv("LLM_API_KEY", "")
    VLM_BASE_URL = os.getenv("VLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    VLM_MODEL = os.getenv("VLM_MODEL", "qwen-vl-max")

    @classmethod
    def vlm_ready(cls) -> bool:
        """VLM 是否可用。不可用时识别链路应静默退回本地 OCR，而不是报错。"""
        return bool(cls.VLM_API_KEY and cls.VLM_MODEL)

    @classmethod
    def missing_keys(cls) -> list:
        """返回缺失的 API Key 名。

        没有这项自检时，忘了配 .env 的表现是接口返回 401 —— 看起来像
        「key 过期」，实际是根本没读到 key，能白排查半天。
        """
        return [name for name in ("LLM_API_KEY", "EMBEDDING_API_KEY")
                if not getattr(cls, name, "")]

    # ── 目录 ──
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = os.path.join(BASE_DIR, "data")
    RAG_DATA_DIR = os.path.join(BASE_DIR, "rag_data")

    # ── SQLite ──
    # 问数用的库由 tools/db_builder.py 自动构建：
    # 以 chinook.sqlite 为基底，叠加 rag_data 下每个表格文件建成的表，
    # 这样「智育前十是谁」这类问题才能落到真正的数据上。
    SQLITE_DB_FILE = "chinook.sqlite"
    SQL_BASE_DB_PATH = os.path.join(BASE_DIR, "data", "chinook.sqlite")
    SQL_DB_PATH = os.path.join(BASE_DIR, "data", "agent.sqlite")

    # ── 向量库 ──
    VECTOR_STORE_DIR = os.path.join(BASE_DIR, "data", "vector_store")

    # ── 路由 ──
    ROUTE_ENABLE_RAG = os.getenv("ROUTE_ENABLE_RAG", "true").lower() == "true"
    ROUTE_ENABLE_SQL = os.getenv("ROUTE_ENABLE_SQL", "true").lower() == "true"