# -*- coding: utf-8 -*-
"""
RAG 工具：文档加载 → 向量索引 → 检索问答

相比初版的主要修正：
1. 按文件【真实内容】判断类型（zipfile 嗅探），不只看扩展名 —— 修掉伪装成 .csv 的 xlsx
2. 表格类文件按行入库（每行一条 Document 并保留列名），检索精度远高于整表文本切分
3. 分批写入（智谱 embedding 接口 input 上限 64 条），并记录源文件指纹
4. 重建索引用 reset_collection()，不再用 shutil.rmtree（Windows 下必被文件句柄阻塞）
5. 索引是否为空改为实时判断，不再是启动时的快照
6. 返回真实相似度，LLM 调用补异常兜底
7. 向量召回 + 词法精确实体召回双路合并 —— 纯向量检索对「姓名 / 学号」这类短实体
   不可靠（实测问「胡子扬」会召回别的学生，导致 LLM 回答"无法回答"），
   这里用选择性子串匹配补上这一路，再统一重算相似度排序
"""
import hashlib
import json
import math
import os
import re

import pandas as pd

from config import Config
from tools.db_builder import read_frames
from tools.file_utils import fmt_cell as _fmt, read_text, sniff_kind
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnablePassthrough
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

# embedding 单次 input 上限**随提供方不同**：智谱 64 条，阿里云百炼 text-embedding-v4
# 只有 **10 条**（实测 25 条即 HTTP 400）。这个值必须与提供方匹配，
# 否则每一批都失败、索引根本建不起来（本项目在智谱那边已经踩过一次反向的坑）。
# 用环境变量覆盖 → 换提供方只改 .env，不动代码。
BATCH_SIZE = int(os.getenv("EMBEDDING_BATCH_SIZE", "10"))
# 切块参数：OHR-Bench 实测（195 页语料）500/100 会把每页切成约 9.5 块，
# 而「检索命中某一页」并不等于「命中承载答案的那一块」——这正是答案大量
# 退化成「根据已有资料无法回答」的主因。放大到 1000/160 后，单块自带更完整的
# 上下文，证据召回率显著改善，块数也从 1848 降到约 1000。
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 160
META_FILE = "_index_meta.json"

# 索引版本：**切分参数或切分逻辑一变就必须 +1**。
# 否则源文件指纹不变 → 误判「可复用」→ 改了 CHUNK_SIZE 却完全没生效，
# 而且因为块数恰好也可能相等，肉眼还看不出来（本项目踩过这个静默陷阱）。
INDEX_VERSION = 3

# ── 文档处理策略清单（中级难题 8 的落地口）──
# 界面在入库前算出文档复杂度，把策略写到这里，形如：
#   {"导入表.xlsx": {"complexity_tier": "复杂", "chunk_size": 600, "top_k": 12, ...}}
# 它刻意**不放在 rag_data 目录内**：那个目录下所有文件都会进「源文件指纹」，
# 策略一变就全量重建索引；放在外面，靠 _plans_signature() 有选择地参与指纹。
PLAN_FILE = os.path.join(Config.BASE_DIR, "data", "doc_plans.json")
# 策略自适应开关。默认开着，但**没有清单文件时它什么也不做** ——
# 也就是说不启用策略时，切块参数、返回条数、索引指纹逐字节与改造前一致，
# 已有评测的可比性不会被这次改动破坏。
PLAN_ADAPTIVE = os.getenv("RAG_PLAN_ADAPTIVE", "1") not in ("0", "false", "False")
MAX_TOPK = 20           # 策略放宽检索宽度的上限，防止清单里写坏值把上下文撑爆

# 重排的 query 向量复用开关。默认**开启复用**（一次 search 只编码 query 一次）。
# 设为 1 可退回旧路径（召回与重排各自编码一次 query），供 scripts/verify_search_equiv.py
# 做等价性对照 —— 没有这条退路，就没法证明「省了一次调用」之后结果没变。
RERANK_LEGACY = os.getenv("RAG_RERANK_LEGACY", "0") not in ("0", "false", "False")

# ── 双路召回参数 ──
VECTOR_K = 12           # 向量语义召回宽度（最终只留 TOP_K 条）
LEXICAL_K = 8           # 词法精确实体召回条数
TOP_K = 6               # 最终返回条数
MAX_TERMS = 80          # 单次查询最多尝试的候选词数，防止超长问题拖慢检索

# ── 交给生成端的上下文预算 ──
CTX_PER_PAGE = 1800     # 同一来源最多注入多少字符（检索回来的是「块」，同页块会合并）
CTX_TOTAL = 9000        # 上下文总字符上限

# 词条区分度阈值（相对 + 下限）：
# 命中文档数超过 max(DF_MIN, 全库 * DF_RATIO) 的词条视为无区分度而丢弃。
# 用相对阈值而不是固定值，是因为「一个班级号」这种事命中量随数据规模变化 ——
# 固定 40 会把某班 41 条记录误杀，相对比例才不会。
DF_MIN = 20
DF_RATIO = 0.25

# 来源可信度加权：从表格里按行切出来的记录是原始数据，
# 而 PDF 正文是排版回流后的文本（本项目的 PDF 就是同一张表的打印版，
# 抽取后列全错位），两者同时命中时应让结构化行记录排在前面。
STRUCTURED_BONUS = 0.05

CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
ALNUM = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-/]*")


def _cosine(a, b) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def candidate_terms(question: str) -> list:
    """抽取可能有区分度的候选词：字母数字串 + CJK 2~4 元组，长的优先"""
    terms = set()
    for tok in ALNUM.findall(question or ""):
        if len(tok) >= 2:
            terms.add(tok)
    for run in CJK_RUN.findall(question or ""):
        n = len(run)
        for size in (4, 3, 2):
            if size > n:
                continue
            for i in range(n - size + 1):
                terms.add(run[i:i + size])
    return sorted(terms, key=len, reverse=True)[:MAX_TERMS]


def build_context(chunks: list, per_page: int = CTX_PER_PAGE,
                  total: int = CTX_TOTAL) -> str:
    """把检索到的块整理成给 LLM 的上下文：**同一来源的块合并**。

    为什么必须合并：切块是为了**检索精度**，不是为了生成。同一页被切成多块后，
    只命中其中 2~3 块时，生成端看到的就是几百字的残片，很容易判定「资料不足」
    而拒答 —— 实测「检索命中率 86% 但答案准确率只有 55%」的主因就在这里。

    同时做预算控制：per_page 限制单来源注入量，total 限制总长度，
    避免 TOP_K 放大后 prompt 无界膨胀（效率章节的瓶颈控制手段之一）。
    """
    if not chunks:
        return ""
    grouped, order = {}, []
    for c in chunks:
        src = c.get("source") or "（未知来源）"
        if src not in grouped:
            grouped[src] = {"texts": [], "score": c.get("score"),
                            "match": c.get("match")}
            order.append(src)
        t = (c.get("text") or "").strip()
        if t and t not in grouped[src]["texts"]:
            grouped[src]["texts"].append(t)

    parts, used = [], 0
    for i, src in enumerate(order, 1):
        g = grouped[src]
        body = "\n".join(g["texts"])[:per_page]
        block = (f"[资料 {i} | 来源: {src} | 相关度: {g['score']}"
                 f"{' | 精确匹配' if g.get('match') == 'exact' else ''}]\n{body}")
        if used + len(block) > total:
            break
        parts.append(block)
        used += len(block)
    return "\n\n---\n\n".join(parts)


_TABULAR = re.compile(r"\\begin\{tabular\}\{[^}]*\}(.*?)\\end\{tabular\}", re.S)
_ROW_NOISE = re.compile(r"\\(hline|toprule|midrule|bottomrule|endhead|endfirsthead)\b")
_CLINE = re.compile(r"\\cline\{[^}]*\}")
# \multicolumn{2}{c} / \multirow{2}{*}{x} 这类跨格包装：去掉包装但保留内容
_SPAN = re.compile(r"\\(?:multi|)column\{[^}]*\}\{[^}]*\}|\\multirow\{[^}]*\}\{[^}]*\}")


def latex_tables_to_markdown(text: str) -> str:
    """把 LaTeX ``tabular`` 转成 Markdown 表，再交给生成端。

    为什么必须转：OHR-Bench 里「表格类证据」的原文是 LaTeX（``\\begin{tabular}`` +
    ``&`` 分列 + ``\\\\`` 分行）。原样喂给 LLM 时它经常在**相邻行/列之间串读** ——
    实测表格类答题准确率 64.7%，是 text/table/equation 三类里最低的，典型错误
    如把上一行的数当成本行的（493 读成 504、-35% 读成 -32%）。

    转成 Markdown 后行列边界是显式的（有 ``|`` 与分隔行），模型不必再从 LaTeX
    语法里推断结构，串读显著减少。不改单元格内容，只改结构表达。
    """
    def conv(m):
        rows = []
        for raw in re.split(r"\\\\", m.group(1)):
            line = _CLINE.sub("", _ROW_NOISE.sub("", raw)).strip()
            if not line:
                continue
            cells = [(c or "").strip() for c in line.split("&")]
            cells = [_SPAN.sub("", c).strip() for c in cells]
            # 去掉包裹整个单元格的花括号（\text{...} 这类内部花括号保留）
            cells = [c[1:-1].strip() if c.startswith("{") and c.endswith("}") else c
                     for c in cells]
            if any(cells):
                rows.append(cells)
        if not rows:
            return m.group(0)
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
        out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n".join(out)

    return _TABULAR.sub(conv, text or "")


class RAGTool:
    def __init__(self, data_dir: str = None, persist_dir: str = None):
        """data_dir / persist_dir 可覆盖 —— 评测需要一套与演示库**隔离**的知识库。

        为什么必须隔离：OHR-Bench 的几百页金融/法律文档一旦混进演示库，
        问「胡子扬的智育测评分」会被无关英文文档挤出检索结果；
        反过来，评测集也要求检索范围可控，否则命中率无从度量。
        不传参时行为与原来完全一致。
        """
        self.llm = ChatOpenAI(
            model=Config.LLM_MODEL,
            api_key=Config.LLM_API_KEY,
            base_url=Config.LLM_BASE_URL,
            temperature=0,
        )
        self.embeddings = OpenAIEmbeddings(
            model=Config.EMBEDDING_MODEL,
            api_key=Config.EMBEDDING_API_KEY,
            base_url=Config.EMBEDDING_BASE_URL,
            # **必须关掉**：langchain 默认会先用 tiktoken 把文本编码成 token id 再发，
            # 而阿里云百炼只接受字符串（实测报
            # `contents is neither str nor list of str.: input.contents`）。
            # 这个开关对非 OpenAI 官方端点基本都是必需的。
            check_embedding_ctx_length=False,
            # embedding 单批上限随提供方不同（百炼 10 条），显式传入让内部也按此切批
            chunk_size=BATCH_SIZE,
        )
        self.persist_dir = persist_dir or Config.VECTOR_STORE_DIR
        self.data_dir = data_dir or Config.RAG_DATA_DIR
        self._plans = None          # 处理策略清单的惰性缓存（见 _load_plans）
        # 索引降级标记：外部 embedding 接口不可用（欠费/限流/断网）时**不能让应用起不来**。
        # 问数链路完全不需要向量库，因为一个外部接口欠费就把整个页面打死，
        # 是最不该出现的鲁棒性缺陷（实测：embedding 欠费时 AgentGraph() 直接抛
        #   RateLimitError，Streamlit 整页报错）。
        self.degraded_reason = ""
        try:
            self.vector_store = self._load_or_build()
        except Exception as e:
            self.degraded_reason = f"{type(e).__name__}: {str(e)[:200]}"
            print(f"[RAG] ⚠️ 向量索引不可用，RAG 检索进入降级模式：{self.degraded_reason}")
            try:
                self.vector_store = self._open_store()   # 打开空集合即可，不需要调用接口
            except Exception as e2:
                self.vector_store = None
                print(f"[RAG] ⚠️ 连空索引都打不开：{type(e2).__name__}: {e2}")

    @property
    def available(self) -> bool:
        """RAG 检索是否可用（索引非空且未降级）"""
        return not self.degraded_reason and not self.is_empty()

    # ==================== 索引健康度 ====================
    @property
    def meta_path(self) -> str:
        return os.path.join(self.persist_dir, META_FILE)

    def _source_fingerprint(self, data_dir: str = None) -> str:
        """对知识库目录下所有文件的大小+mtime 取指纹，源文件一变索引即失效。

        **索引参数也要进指纹**：只哈希源文件的话，改了 CHUNK_SIZE / 切分逻辑
        指纹不变，就会直接复用旧索引，改动静默失效（见 INDEX_VERSION 注释）。
        """
        items = []
        data_dir = data_dir or self.data_dir
        if os.path.isdir(data_dir):
            for name in sorted(os.listdir(data_dir)):
                fp = os.path.join(data_dir, name)
                if os.path.isfile(fp):
                    st = os.stat(fp)
                    items.append([name, st.st_size, int(st.st_mtime)])
        payload = {"v": INDEX_VERSION,
                   "chunk": [CHUNK_SIZE, CHUNK_OVERLAP],
                   # **必须带 embedding 模型名**：换提供方会改变向量空间与维度
                   # （智谱 embedding-3 = 2048 维 → 百炼 text-embedding-v4 = 1024 维），
                   # 旧索引在新模型下完全不可用。不带它就会出现
                   # 「指纹相同 → 复用旧索引」→ 检索报维度错或返回垃圾。
                   "embed": Config.EMBEDDING_MODEL,
                   "files": items}
        # 策略签名**只在清单存在时**才进指纹。这样两条路都正确：
        #   · 改了策略 → 切块参数可能变 → 索引必须重建（进指纹解决）
        #   · 没有策略 → 指纹与改造前逐字节一致 → 不触发无谓的全量重建
        sig = self._plans_signature()
        if sig:
            payload["plans"] = sig
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return hashlib.sha1(raw).hexdigest()

    # ==================== 文档处理策略（难题 8） ====================
    def _load_plans(self) -> dict:
        """读取处理策略清单。读不到就当「没有策略」，绝不因此让检索挂掉 ——
        策略是优化项，不是依赖项。"""
        if self._plans is not None:
            return self._plans
        self._plans = {}
        if not PLAN_ADAPTIVE:
            return self._plans
        if not os.path.isfile(PLAN_FILE):
            return self._plans
        try:
            with open(PLAN_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._plans = {str(k): v for k, v in data.items()
                               if isinstance(v, dict)}
                print(f"[RAG] 已加载文档处理策略 {len(self._plans)} 条")
        except Exception as e:
            print(f"[RAG] 处理策略清单读取失败（按无策略继续）：{type(e).__name__}: {e}")
        return self._plans

    def plan_of(self, source: str) -> dict:
        """取某个来源的处理策略；没有就返回空 dict，调用方走默认行为"""
        return self._load_plans().get(os.path.basename(str(source or ""))) or {}

    def _plans_signature(self) -> str:
        """策略签名：只把**影响索引结构**的字段（切块粒度）纳入。

        检索宽度 top_k 不进签名 —— 它只影响查询期返回条数，不影响已写入的块，
        纳进来会让「改一下 top_k」也触发全量重建，纯属浪费。
        """
        p = self._load_plans()
        if not p:
            return ""
        raw = json.dumps({k: [v.get("chunk_size"), v.get("chunk_overlap")]
                          for k, v in sorted(p.items())}, ensure_ascii=False)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    def _read_meta(self) -> dict:
        try:
            with open(self.meta_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _write_meta(self, fingerprint: str, chunks: int) -> None:
        try:
            os.makedirs(self.persist_dir, exist_ok=True)
            with open(self.meta_path, "w", encoding="utf-8") as f:
                json.dump({"fingerprint": fingerprint, "chunks": chunks},
                          f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[RAG] 写入索引元信息失败: {e}")

    def _open_store(self) -> Chroma:
        return Chroma(persist_directory=self.persist_dir,
                      embedding_function=self.embeddings)

    def is_empty(self) -> bool:
        """实时判断索引是否为空（不是启动时的快照）"""
        try:
            return self.vector_store is None or self.vector_store._collection.count() == 0
        except Exception:
            return True

    def _load_or_build(self) -> Chroma:
        fingerprint = self._source_fingerprint()
        has_store = os.path.isdir(self.persist_dir) and bool(os.listdir(self.persist_dir))

        if has_store:
            meta = self._read_meta()
            if meta.get("fingerprint") == fingerprint and meta.get("chunks", 0) > 0:
                try:
                    vec = self._open_store()
                    count = vec._collection.count()
                    if count == meta["chunks"]:
                        print(f"[RAG] 复用已有索引，块数: {count}")
                        return vec
                    print(f"[RAG] 索引块数不符（实际 {count} / 记录 {meta['chunks']}），"
                          f"判定为残缺索引，重建")
                    return self.build_index(fingerprint=fingerprint, store=vec)
                except Exception as e:
                    print(f"[RAG] 打开已有索引失败: {e}，重建")
            elif meta:
                print("[RAG] 源文件已变更，重建索引")
            else:
                print("[RAG] 缺少索引元信息（旧版本遗留），重建索引")
            try:
                return self.build_index(fingerprint=fingerprint, store=self._open_store())
            except Exception as e:
                print(f"[RAG] 打开索引失败，将新建: {e}")

        return self.build_index(fingerprint=fingerprint)

    # ==================== 索引构建 ====================
    def build_index(self, data_dir: str = None, fingerprint: str = None,
                    store: Chroma = None) -> Chroma:
        if data_dir is None:
            data_dir = self.data_dir
        os.makedirs(data_dir, exist_ok=True)
        if fingerprint is None:
            fingerprint = self._source_fingerprint(data_dir)

        docs = self._load_documents(data_dir)

        vec = store if store is not None else self._open_store()

        if not docs:
            print("[RAG] 没有可用文档，索引为空")
            self._write_meta(fingerprint, 0)
            return vec

        splitter = RecursiveCharacterTextSplitter(
            chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP
        )
        # 切块：**有处理策略的文档按自己的策略切**，其余走全局默认。
        # 策略是文档级的 —— 同一份资料里，一页密集表格和一页纯文本不该用同一把尺子。
        # 没有策略时这条路与原实现完全等价（默认 splitter 就是原参数，
        # split_documents 本来就是逐文档切、不跨文档合并）。
        plans = self._load_plans()
        splitters = {(CHUNK_SIZE, CHUNK_OVERLAP): splitter}
        chunks = []
        for doc in docs:
            plan = plans.get(os.path.basename(str(doc.metadata.get("source") or ""))) or {}
            size, overlap = CHUNK_SIZE, CHUNK_OVERLAP
            if plan:
                try:
                    size = int(plan.get("chunk_size") or CHUNK_SIZE)
                    overlap = int(plan.get("chunk_overlap") or CHUNK_OVERLAP)
                except (TypeError, ValueError):
                    size, overlap = CHUNK_SIZE, CHUNK_OVERLAP
            sp = splitters.get((size, overlap))
            if sp is None:
                sp = RecursiveCharacterTextSplitter(chunk_size=size, chunk_overlap=overlap)
                splitters[(size, overlap)] = sp
            pieces = sp.split_documents([doc])
            tier = plan.get("complexity_tier")
            if tier:
                # 把策略结论落进块元数据：检索侧据此放宽召回宽度，
                # 界面也据此显示「这段来自复杂文档」—— 来源展示才是完整的
                for pc in pieces:
                    pc.metadata["complexity_tier"] = str(tier)
                    if plan.get("complexity_score") is not None:
                        try:
                            pc.metadata["complexity_score"] = float(plan["complexity_score"])
                        except (TypeError, ValueError):
                            pass
            chunks.extend(pieces)
        if plans:
            print(f"[RAG] 按处理策略切块：{len(plans)} 份文档带策略，"
                  f"共 {len(splitters)} 组切分参数")
        print(f"[RAG] 分割完成，{len(docs)} 段 -> {len(chunks)} 个块")

        try:
            # 用 collection API 清空，不用 shutil.rmtree：
            # Windows 下 Chroma 仍持有 chroma.sqlite3 句柄，rmtree 必报 WinError 32
            vec.reset_collection()
        except Exception as e:
            print(f"[RAG] 清空旧索引失败（将在其基础上追加）: {e}")

        total_batches = (len(chunks) - 1) // BATCH_SIZE + 1
        for i in range(0, len(chunks), BATCH_SIZE):
            batch = chunks[i:i + BATCH_SIZE]
            vec.add_documents(batch)
            print(f"[RAG] 写入批次 {i // BATCH_SIZE + 1}/{total_batches}（{len(batch)} 条）")

        self._write_meta(fingerprint, len(chunks))
        print(f"[RAG] 索引构建完成，块数: {len(chunks)}")
        return vec

    # ==================== 文档加载 ====================
    def _load_documents(self, data_dir: str) -> list:
        docs = []
        for fname in sorted(os.listdir(data_dir)):
            # 与 db_builder 保持一致：元数据文件（清单/缓存）不是知识文档，不入库
            if fname.startswith(("_", ".")):
                continue
            fpath = os.path.join(data_dir, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                docs.extend(self._load_one(fpath, fname))
            except Exception as e:
                # 明确报出文件名与原因，不再静默吞掉
                print(f"[RAG] !! 加载失败 {fname}: {type(e).__name__}: {e}")
        return docs

    def _load_one(self, fpath: str, fname: str) -> list:
        kind = sniff_kind(fpath)

        if kind == "xlsx":
            return self._load_table(fpath, fname)

        if kind == "pdf":
            from langchain_community.document_loaders import PyPDFLoader
            loaded = PyPDFLoader(fpath).load()
            # PyPDFLoader 会把 source 写成绝对路径，这里统一成文件名，
            # 否则引用展示里表格是文件名、PDF 是长路径，口径不一致
            for doc in loaded:
                doc.metadata["source"] = fname
            print(f"[RAG] PDF《{fname}》-> {len(loaded)} 页")
            return loaded

        if kind == "md":
            text = read_text(fpath)
            print(f"[RAG] Markdown《{fname}》-> {len(text)} 字符")
            return [Document(page_content=latex_tables_to_markdown(text),
                             metadata={"source": fname})]

        if kind == "text":
            if fname.lower().endswith(".csv"):
                return self._load_table(fpath, fname)
            text = read_text(fpath)
            print(f"[RAG] 文本《{fname}》-> {len(text)} 字符")
            return [Document(page_content=latex_tables_to_markdown(text),
                             metadata={"source": fname})]

        if kind == "docx":
            # Word：段落 + 表格（表格按行拼成 "a | b | c"，保住行列关系再入库）
            from docx import Document as _Docx
            doc = _Docx(fpath)
            parts = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
            for tb in doc.tables:
                for row in tb.rows:
                    cells = [c.text.strip() for c in row.cells]
                    if any(cells):
                        parts.append(" | ".join(cells))
            text = "\n".join(parts)
            print(f"[RAG] Word《{fname}》-> {len(text)} 字符")
            return [Document(page_content=latex_tables_to_markdown(text),
                             metadata={"source": fname})]

        if kind == "zip" and fname.lower().endswith(".pptx"):
            # PPT：按页取文本框，保留页码便于溯源
            from pptx import Presentation as _Pptx
            prs = _Pptx(fpath)
            parts = []
            for i, slide in enumerate(prs.slides, 1):
                for shp in slide.shapes:
                    if getattr(shp, "has_text_frame", False) and shp.text_frame.text.strip():
                        parts.append(f"[slide {i}] {shp.text_frame.text.strip()}")
            text = "\n".join(parts)
            print(f"[RAG] PPT《{fname}》-> {len(text)} 字符")
            return [Document(page_content=text, metadata={"source": fname})]

        print(f"[RAG] 跳过不支持的类型（{kind}）: {fname}")
        return []

    @staticmethod
    def _read_sheets(fpath: str) -> dict:
        """读取表格文件，返回 {工作表名: DataFrame}（与建库侧共用同一套解析逻辑）"""
        return read_frames(fpath)

    def _load_table(self, fpath: str, fname: str) -> list:
        """表格按行转 Document：每行一条并带列名，比整表文本切分检索精度高得多"""
        sheets = self._read_sheets(fpath)
        docs = []
        for sheet_name, df in sheets.items():
            if df is None or df.empty:
                continue
            columns = [str(c) for c in df.columns]
            docs.append(Document(
                page_content=(f"表《{fname}》工作表[{sheet_name}] 共 {len(df)} 行，"
                              f"字段：{'、'.join(columns)}"),
                metadata={"source": fname, "sheet": str(sheet_name), "kind": "schema"},
            ))
            for idx, row in df.iterrows():
                parts = []
                for col in df.columns:
                    val = row[col]
                    if pd.isna(val):
                        continue
                    parts.append(f"{col}: {_fmt(val)}")
                if parts:
                    docs.append(Document(
                        page_content="；".join(parts),
                        metadata={"source": fname, "sheet": str(sheet_name),
                                  "row": int(idx) + 2},
                    ))
            print(f"[RAG] 表格《{fname}》[{sheet_name}] -> {len(df)} 行记录")
        return docs

    # ==================== 检索问答 ====================
    def _lexical_hits(self, question: str) -> list:
        """全文「包含」召回：负责姓名、学号这类精确实体。

        纯向量检索对短实体词很不可靠 —— 实测问「胡子扬」会优先召回同表里的其他学生。
        这里用选择性子串匹配补一路：
          · 只保留命中文档数不超过 max(DF_MIN, 全库*DF_RATIO) 的词条
            （"电子科学与工程学院"命中 569 条 -> 丢弃；"胡子扬"命中 2 条 -> 保留；
             "2112515"命中 41 条 -> 保留，是一个班的正常规模）
          · 从长词往短词扫，已被更长命中词覆盖的短词直接跳过，
            典型一次查询只需几次本地查询
        """
        try:
            col = self.vector_store._collection
        except Exception:
            return []

        total = col.count() or 0
        limit = max(DF_MIN, int(total * DF_RATIO))

        hits = {}          # 已确认命中的词条
        scored = {}        # doc_id -> {text, meta, score}
        for term in candidate_terms(question):
            if len(term) >= 2 and any(term in t for t in hits):
                continue                      # 已被更长的命中词覆盖
            try:
                got = col.get(where_document={"$contains": term}, limit=limit + 1)
            except Exception:
                continue
            ids = got.get("ids") or []
            if not ids or len(ids) > limit:
                continue                      # 0 条无召回；超过上限则无区分度
            hits[term] = True
            weight = len(term)
            for doc_id, text, meta in zip(ids, got.get("documents") or [],
                                          got.get("metadatas") or []):
                item = scored.setdefault(
                    doc_id, {"text": text, "meta": meta or {}, "score": 0})
                item["score"] += weight

        if not scored:
            return []
        if hits:
            print(f"[RAG] 词法精确命中词条: {'、'.join(sorted(hits))}（阈值 {limit} 条）")
        # 同分时结构化行记录优先（PDF 正文命中同一实体的优先级更低）
        return sorted(scored.values(),
                      key=lambda d: (d["score"], d["meta"].get("row") is not None),
                      reverse=True)[:LEXICAL_K]

    def _rerank(self, question: str, items: list, qv=None) -> list:
        """对合并后的候选集统一重算相似度，保证所有条目的「相关度」口径一致。

        qv 是调用方已经算好的 query 向量。传进来就复用，不再自己编码一次 ——
        改造前一次 search 要编码 query 两次（向量召回一次、重排一次），
        加上候选集一次共 3 次往返；复用后降到 2 次。qv=None 时保持旧行为。
        """
        try:
            if qv is None:
                qv = self.embeddings.embed_query(question)
            dvs = self.embeddings.embed_documents([i["text"] for i in items])
            for item, dv in zip(items, dvs):
                item["sim"] = _cosine(qv, dv)
        except Exception as e:
            print(f"[RAG] 重排失败，沿用向量分数: {e}")
            for item in items:
                item["sim"] = item.get("vec")

        for item in items:
            # 结构化行记录比 PDF 正文更接近原始数据，同分时优先
            if item.get("row") is not None:
                item["sim"] = (item.get("sim") or 0.0) + STRUCTURED_BONUS
        # 精确命中是第一优先级，其次才是语义相似度
        return sorted(items, key=lambda i: (i.get("lex", 0) > 0, i.get("sim") or 0.0),
                      reverse=True)

    def search(self, question: str) -> list:
        """双路召回：向量语义 + 词法精确实体，返回兼容 graph 的格式（含真实相似度）"""
        if self.is_empty():
            why = f"（降级原因：{self.degraded_reason}）" if self.degraded_reason else ""
            print(f"[RAG] 索引不可用，跳过检索{why}")
            return []

        candidates = {}       # 以正文为键去重

        def put(text: str, source: str, row, vec=None, lex=0):
            item = candidates.setdefault(text, {"text": text, "source": source,
                                                "row": row, "vec": None, "lex": 0})
            if vec is not None:
                item["vec"] = max(item["vec"] or 0.0, vec)
            item["lex"] = max(item["lex"], lex)

        # ── 路 1：向量语义召回（宽召回） ──
        # query 向量在这里算**一次**，同时供召回与重排使用。
        # 用的 by_vector_with_relevance_scores 与旧的 with_relevance_scores
        # 是同一个打分函数（都走 store 自己的 relevance score fn），只是入口接受
        # 向量而非文本 —— 所以「省掉一次编码」不改变任何一条分数。
        qv = None
        if not RERANK_LEGACY:
            try:
                qv = self.embeddings.embed_query(question)
            except Exception as e:
                print(f"[RAG] query 向量化失败，退回旧路径: {e}")
                qv = None
        try:
            if qv is not None:
                vec_hits = self.vector_store.similarity_search_by_vector_with_relevance_scores(
                    qv, k=VECTOR_K)
            else:
                vec_hits = self.vector_store.similarity_search_with_relevance_scores(
                    question, k=VECTOR_K)
            for doc, score in vec_hits:
                put(doc.page_content, doc.metadata.get("source", ""),
                    doc.metadata.get("row"),
                    vec=float(score) if score is not None else None)
        except Exception as e:
            print(f"[RAG] 相似度召回失败，降级为普通检索: {e}")
            try:
                if qv is not None:
                    plain = self.vector_store.similarity_search_by_vector(qv, k=VECTOR_K)
                else:
                    plain = self.vector_store.similarity_search(question, k=VECTOR_K)
                for doc in plain:
                    put(doc.page_content, doc.metadata.get("source", ""),
                        doc.metadata.get("row"))
            except Exception as e2:
                print(f"[RAG] 向量召回失败: {e2}")

        # ── 路 2：词法精确实体召回 ──
        for hit in self._lexical_hits(question):
            meta = hit["meta"] or {}
            put(hit["text"], meta.get("source", "") or "（未知来源）",
                meta.get("row"), lex=hit["score"])

        if not candidates:
            return []

        ranked = self._rerank(question, list(candidates.values()), qv=qv)

        # 处理策略（难题 8）在**查询期**的落地：命中的来源里若有标记为「复杂」的
        # 文档，就按它的策略放宽返回条数。理由是信息密度 —— 一页塞满表格与公式时，
        # 承载答案的那一段很容易被挤到 TOP_K 之后；纯文本页则不需要放宽。
        # 没有策略清单时 limit 恒等于 TOP_K，行为与改造前一致。
        limit = TOP_K
        for item in ranked[:TOP_K]:
            try:
                limit = max(limit, min(int(self.plan_of(item["source"]).get("top_k") or 0),
                                       MAX_TOPK))
            except (TypeError, ValueError):
                continue
        ranked = ranked[:limit]

        return [{
            "source": item["source"],
            "text": item["text"],
            "score": round(float(item["sim"]), 4) if item.get("sim") is not None else None,
            "match": "exact" if item.get("lex") else "semantic",
            # 复杂度档位随片段一起回传：界面要显示「这段来自复杂文档，
            # 检索已按策略放宽」，来源展示才说得清「为什么多给了几条」
            "complexity": self.plan_of(item["source"]).get("complexity_tier"),
        } for item in ranked]

    def query(self, question: str) -> dict:
        """检索 + 生成答案。复用 search() 的双路召回，避免两条检索路径口径分叉"""
        if self.is_empty():
            return {"answer": "", "sources": [], "error": "索引为空"}

        chunks = self.search(question)
        if not chunks:
            return {"answer": "", "sources": [], "error": "未检索到相关内容"}

        sources = [{"content": c["text"][:200], "source": c["source"]} for c in chunks]
        # 同源块合并后再给生成端：切块是为检索精度，生成端需要相对完整的证据段
        context = build_context(chunks)

        prompt = ChatPromptTemplate.from_template(
            """你是一个严谨的知识问答助手。请**只依据**下面的资料回答问题。

【资料】
{context}

【问题】
{question}

【作答要求】
1. 先在资料中逐条查找证据；只要能找到支撑结论的内容，就**必须给出明确答案**。
   不要把「没有看到与问题逐字相同的那句话」当成「资料里没有」——
   表格要按行列对齐后再读，公式要按符号读
2. **回答语言与问题语言保持一致**（英文提问就用英文回答）
3. 数值、日期、名称必须与资料**逐字一致**，不要近似、不要换算、不要推算。
   表格资料请先锁定行（时间/分部/主体）与列（指标）的**交点**再读数，
   注意不要跨行、跨列串读（这是最常见的错法）
4. 只使用资料中出现过的事实，不要引入资料之外的知识；
   若资料里确实没有该数值/事实，就明确说「资料中未给出」，**不要用相近的数字顶上**
5. 先给结论，再给依据（标注引用的资料编号）
6. 只有在资料中确实找不到任何相关信息时，才回答「根据已有资料无法回答」

回答："""
        )

        try:
            chain = (
                {"context": lambda _: context,
                 "question": RunnablePassthrough()}
                | prompt
                | self.llm
                | StrOutputParser()
            )
            answer = chain.invoke(question)
        except Exception as e:
            print(f"[RAG] 生成失败: {e}")
            return {"answer": "", "sources": sources, "error": str(e)}

        return {"answer": answer, "sources": sources, "error": None}
