# -*- coding: utf-8 -*-
"""Schema Linking：按问题挑出相关表，只把这几张表的 schema 塞进 prompt。

为什么必须做：
    graph.py 原来把整库 schema 截断到 8000 字符塞进 prompt。当前 12 张表已经
    占到 6797，余量只剩 ~1200 字符 —— 再加任何一张有分量的表，排在后面的表
    就会被切掉，在 LLM 眼里「不存在」，直接复现 NOSQL 死局；表数一多
    （AdventureWorks 几十张）这条路必然崩。

两级信号，各有各的活：
    1. 词法：中文切 2-gram、英文做**词形归一**（复数还原 + CamelCase 拆子词）后
       跟「表名 + 列名」做重叠，再用 IDF 压掉「情况 / 成绩 / 名称」这类满库都是的
       字；样本值做子串精确命中加分。
       对「智育前十」→ 列名「智育测评分排名」这种中文场景，词法是主力；
       对英文直问（"How many customers"）则靠词形归一 —— 少了这一步，
       复数的 customers 与单数的表名 Customer 零命中，所有表同分会退化成
       按表名字母序返回（top1 变 Album，纯属巧合，见 _en_singular 的注释）。
    2. 向量：复用 RAG 那套智谱 embedding，补跨语言 / 近义
       （「有多少客户」→ Customer）。接口挂了就静默降级为纯词法，不阻塞主流程。

兜底原则：**宁多勿少**。全量 schema 装得下就直接给全量，根本不走检索；
检索结果也会附上「库里还有哪些表」，让 LLM 知道自己没看到什么，
从而在答不了时老老实实说，而不是臆造字段。
"""
import hashlib
import json
import math
import os
import re

from config import Config

CJK = re.compile(r"[\u4e00-\u9fff]+")
ASCII_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]*")
# CamelCase / 下划线 / 短横拆子词：CustomerId → Customer, Id；
# BillingPostalCode → Billing, Postal, Code（若无此路，整词只能作为一个 gram
# 参与匹配，「CustomerId」永远命中不了提问里的 customer）
_EN_SUBWORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+|\d+")
_EN_SEP = re.compile(r"[._\-]+")

NGRAM = 2                 # 中文 n-gram 长度
# 全量 schema 短于此值就直接全给，不做检索。可用环境变量调，方便对比实验。
# 参考量级：Chinook(11表)+综测 ≈ 6.8k 字符；AdventureWorks 完整版(70表) 约 40k+
FULL_SCHEMA_LIMIT = int(os.getenv("SCHEMA_FULL_LIMIT", "24000"))
DEFAULT_TOP_K = int(os.getenv("SCHEMA_TOP_K", "6"))   # 默认挑几张表
MAX_TABLES_SCAN = 400     # 保护上限，避免极端库把启动拖死
SAMPLE_ROWS = 5           # 每张表取几行样本值
MAX_SAMPLES = 60          # 每张表最多保留多少个样本值
MAX_SAMPLE_LEN = 24       # 过长的值（大段文本）不参与匹配
SAMPLE_HIT_GAIN = 8.0     # 样本值命中问题的权重（强信号：说明问的就是这张表里的实体）
UNIGRAM_WEIGHT = 0.4      # 单字权重。必须有单字这路：「各班平均分」的「班」
                          # 只能命中列名「行政班」，2-gram 根本切不出来。
                          # 权重压低 + IDF 兜底，避免「的/是/分」这类字乱打分。
VEC_GAIN = 30.0           # 向量分增益：先减基线再放大，见 select() 里的说明
ASCII_WORD_WEIGHT = 2.0   # 英文整词比中文 2-gram 更有信息量
EN_WHOLE_WEIGHT = 1.0     # 英文整词命中（表名 Customer ← 提问 customers）
EN_SUBWORD_WEIGHT = 0.45  # 从 CamelCase 拆出的子词命中（Invoice.CustomerId → customer）

PROFILE_FILE = os.path.join(Config.DATA_DIR, "schema_profile.json")

# 中文里高频的功能字/疑问字，单独出现时不作为匹配依据
CJK_STOP = set("的是了在和与或有多少什么哪吗呢请我他她它们个及以及对于关于怎么如何"
               "一下一个这个那个可以帮我想要知道查一下告诉我")

# 英文功能词，同样不参与匹配。
# 为什么必须有：样本值是**真实数据行**，里面全是自然语言（Chinook 的专辑名就有
# 「Let There Be Rock」）。不挡功能词的话，提问里的 there / are 会命中这首歌名，
# 而且因为「只有这一张表有这个词」IDF 极高 —— 实测 "How many customers are there"
# 让 Album 拿了 5.67 分反超 Customer 的 4.39 分，top1 直接错成 Album。
# 注意这里**不放** total / count / number / name / date 这类词：
# 它们在数据库里是真实字段名（Invoice.Total 等），挡掉反而会漏匹配。
EN_STOP = set("""
a an the this that these those
is are was were be been being am
do does did done doing
have has had having
of in on at to for from by with and or but nor so
if then than as not no yes
there here where when what which who whom whose why how
all any some each every both most more much many few less least other another
i you he she it we they me him her us them my your his its our their
can could will would shall should may might must
please tell show give get find want need know about into over under between up down out off
list let us
""".split())

PROFILE_PROMPT = """下面是数据库的表结构。请为每张表补一份**中文语义画像**，
用于把中文提问定位到正确的表。

每张表输出三个字段：
- "zh"        ：表的中文名（4~12 字，直白即可）
- "aliases"   ：4~8 个中文同义词 / 口语说法（用户可能怎么问这张表）
- "columns_zh"：{{列名: 该列的中文含义(2~6 字)}}

要求：
1. 只输出 JSON 对象，key 为表名，不要任何解释、不要 markdown 代码块
2. 别名要覆盖口语与书面语两种说法，不要只做直译
3. 列名含义要写清业务语义，不要只把英文翻成中文（如 Quantity 是"数量/件数"）

【表结构】
{schema}
"""


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'



def _en_singular(word: str) -> str:
    """英文复数还原（保守的规则化处理，不做完整词干提取）。

    为什么必须做：表名是单数 `Customer`，而用户提问写复数 `customers`，
    `"customers" != "customer"` 直接零命中。一旦所有表都零分，select()
    只能按表名字母序返回 —— 实测 "How many customers are there" 的 top1
    变成了 `Album`（纯属字母序第一张）。补上复数归一后，英文提问才落得回
    正常的词法匹配。规则保守是刻意的：宁可漏还原（少一次命中），
    也不要把 analysis / status 这类词尾误剥成 analysi / statu。
    """
    if len(word) <= 3:
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"                    # companies → company
    if word.endswith(("ches", "shes", "sses", "xes", "zes", "ses")):
        return word[:-2]                          # boxes → box；classes → class
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]                          # customers → customer
    return word


def _en_forms(token: str) -> dict:
    """一个英文词元能贡献的全部匹配形式 → 权重（**只出归一后的规范形**）。

    三个刻意的设计，每一个都对应一次实测踩到的错：

    1. 只保留规范形（单数），不把「原形」和「单数」并列输出。否则提问
       "employees" 会同时产出 employees + employee 两个 gram，而表侧若也有
       一张含这两个形式的表，就会被记两次分 —— 实测 "How many employees"
       让一张财务表拿到 10.06 分、反超 Employee 的 4.39 分，纯粹是重复计数的假象。
    2. 两档权重：整词命中满权，CamelCase 拆出的子词半权。否则
       `Invoice.CustomerId` 拆出的 customer 会和表名 Customer 同分，靠字母序
       决胜负 —— 那正是「有多少客户」被抢给 Invoice 的原因。
    3. 过滤英文功能词。样本值是真实数据行、满是自然语言，不挡 there / are
       这类词会命中专辑名「Let There Be Rock」，且因只有一张表含它而 IDF 极高。
    """
    out = {}
    low = (token or "").lower()
    if low and low not in EN_STOP:
        out[_en_singular(low)] = EN_WHOLE_WEIGHT
    parts = [p for seg in _EN_SEP.split(token or "")
             if seg for p in _EN_SUBWORD.findall(seg)]
    if len(parts) > 1:            # 拆不出多个子词就不拆，免得把整词降权
        for part in parts:
            part = part.lower()
            if len(part) < 2 or part in EN_STOP:
                continue
            out.setdefault(_en_singular(part), EN_SUBWORD_WEIGHT)
    return out


def _grams(text: str) -> set:
    """中文切 2-gram（单字 run 退化为整字），英文/数字做词形归一后取全部形式"""
    out = set()
    for run in CJK.findall(text or ""):
        if len(run) < NGRAM:
            out.add(run)
        for i in range(len(run) - NGRAM + 1):
            out.add(run[i:i + NGRAM])
    for tok in ASCII_TOKEN.findall(text or ""):
        out.update(_en_forms(tok))
    return out


def _unigrams(text: str) -> set:
    """中文单字（去停用字）。单独一路：像「班」这种字只能靠单字命中"""
    return {c for run in CJK.findall(text or "") for c in run if c not in CJK_STOP}


def _ascii_words(text: str) -> set:
    """问题里的英文形式（含复数归一与子词拆分），用于给英文命中加权"""
    out = set()
    for tok in ASCII_TOKEN.findall(text or ""):
        out.update(_en_forms(tok))
    return out


def _en_weights(text: str) -> dict:
    """整段文本里英文形式 → 权重（同一形式取最大）。

    中文 gram 不进这张表（默认权重 1.0），保证中文路径的打分与改动前完全一致。
    """
    out = {}
    for tok in ASCII_TOKEN.findall(text or ""):
        for gram, weight in _en_forms(tok).items():
            if weight > out.get(gram, 0.0):
                out[gram] = weight
    return out


class SchemaLinker:
    """按问题检索相关表。索引、中文语义画像、向量都惰性构建，只建一次。"""

    def __init__(self, db, embeddings=None, llm=None):
        self.db = db
        self.embeddings = embeddings
        self.llm = llm
        self._tables = None          # list[str]
        self._meta = None            # {table: {"columns","samples","grams","unigrams","profile","gweight"}}
        self._df = None              # {gram: 出现该 gram 的表数}
        self._vec = None             # {table: [float]} 或 None（向量不可用）
        self._vec_ready = False

    # ── 中文语义画像 ──
    @staticmethod
    def _fingerprint(tables: list, meta: dict) -> str:
        """表 + 列的指纹：结构一变就让画像缓存失效"""
        raw = "|".join(f"{t}({','.join(str(c) for c in meta[t]['columns'])})"
                       for t in sorted(tables))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _load_profile(self, meta: dict) -> dict:
        """读缓存；结构变了或有新表就调 LLM 补，再把结果写回磁盘。

        为什么要这一层：索引里只有英文列名时，中文提问「公司有多少员工」跟
        Employee 的词法重叠是 0，只能靠向量硬扛，实测 Chinook 那几道题全错。
        给每张表生成一份中文画像（中文名 + 口语别名 + 列的中文含义）并缓存，
        中文提问就能重新落回可靠的词法匹配上。而且这是**一次性的**，
        换库/加表时自动增量补，不靠 Few-Shot 堆示例 —— 对应中级难题 2。
        """
        cache = {}
        if os.path.exists(PROFILE_FILE):
            try:
                with open(PROFILE_FILE, "r", encoding="utf-8") as f:
                    cache = json.load(f)
            except Exception:
                cache = {}
        fp = self._fingerprint(self._tables, meta)
        if cache.get("_fingerprint") == fp and cache.get("tables"):
            return cache["tables"]

        missing = [t for t in self._tables if t not in (cache.get("tables") or {})]
        if not missing or self.llm is None:
            return cache.get("tables") or {}

        print(f"[Schema] 为 {len(missing)} 张表生成中文语义画像（一次性，之后走缓存）")
        lines = []
        for t in missing:
            cols = ", ".join(str(c) for c in meta[t]["columns"])
            lines.append(f"{t}({cols})")
        try:
            raw = self.llm.invoke(PROFILE_PROMPT.format(schema="\n".join(lines))).content
            raw = raw.replace("```json", "").replace("```", "").strip()
            got = json.loads(raw)
        except Exception as e:
            print(f"[Schema] 语义画像生成失败（{type(e).__name__}: {e}），降级为纯词法")
            return cache.get("tables") or {}

        merged = dict(cache.get("tables") or {})
        for t in missing:
            p = got.get(t) or {}
            merged[t] = {
                "zh": str(p.get("zh") or ""),
                "aliases": [str(a) for a in (p.get("aliases") or [])],
                "columns_zh": {str(k): str(v) for k, v in (p.get("columns_zh") or {}).items()},
            }
        try:
            os.makedirs(os.path.dirname(PROFILE_FILE), exist_ok=True)
            with open(PROFILE_FILE, "w", encoding="utf-8") as f:
                json.dump({"_fingerprint": fp, "tables": merged}, f,
                          ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Schema] 画像缓存写入失败（{e}）")
        return merged

    @staticmethod
    def _profile_text(p: dict) -> str:
        if not p:
            return ""
        parts = [p.get("zh", "")]
        parts += list(p.get("aliases") or [])
        parts += [f"{k}{v}" for k, v in (p.get("columns_zh") or {}).items()]
        return " ".join(x for x in parts if x)

    # ── 索引 ──
    def _build(self) -> None:
        if self._meta is not None:
            return
        try:
            names = list(self.db.get_usable_table_names())
        except Exception:
            names = []
        self._tables = names[:MAX_TABLES_SCAN]
        meta, df = {}, {}
        for t in self._tables:
            columns = []
            try:
                rows = self.db._execute(f'PRAGMA table_info({_quote(t)})', fetch="all")
                for r in rows or []:
                    if isinstance(r, dict):
                        columns.append(r.get("name") or list(r.values())[1])
                    else:
                        columns.append(r[1])
            except Exception:
                pass
            meta[t] = {"columns": columns, "samples": self._sample_values(t),
                       "profile": {}, "grams": set(), "unigrams": set(),
                       "gweight": {}}
        # 画像必须早于建索引：画像文本要并进每张表的关键词袋
        profiles = self._load_profile(meta)
        for t in self._tables:
            m = meta[t]
            m["profile"] = profiles.get(t) or {}
            text = " ".join([str(t), self._profile_text(m["profile"]),
                             *[str(c) for c in m["columns"]], *m["samples"]])
            m["grams"], m["unigrams"] = _grams(text), _unigrams(text)
            m["gweight"] = _en_weights(text)
            for g in m["grams"] | m["unigrams"]:
                df[g] = df.get(g, 0) + 1
        self._meta, self._df = meta, df

    def _sample_values(self, table: str) -> list:
        out, seen = [], set()
        try:
            rows = self.db._execute(f'SELECT * FROM {_quote(table)} LIMIT {SAMPLE_ROWS}', fetch="all")
        except Exception:
            return out
        for row in rows or []:
            vals = row.values() if isinstance(row, dict) else (row or [])
            for v in vals:
                if v is None:
                    continue
                s = str(v).strip()
                if not s or len(s) > MAX_SAMPLE_LEN or s in seen:
                    continue
                seen.add(s)
                out.append(s)
                if len(out) >= MAX_SAMPLES:
                    return out
        return out

    def _build_vectors(self) -> None:
        """惰性构建表向量；失败就置 None，让词法单独扛"""
        if self._vec_ready:
            return
        self._vec_ready = True
        if self.embeddings is None:
            return
        try:
            docs = []
            for t in self._tables:
                m = self._meta[t]
                docs.append(f"表 {t}；{self._profile_text(m['profile'])}"
                            f"；字段 {'、'.join(str(c) for c in m['columns'])}"
                            f"；示例值 {'、'.join(m['samples'][:12])}")
            self._vec = dict(zip(self._tables, self.embeddings.embed_documents(docs)))
        except Exception as e:
            print(f"[Schema] 向量索引不可用（{type(e).__name__}），降级为纯词法检索")
            self._vec = None

    # ── 对外 ──
    def list_tables(self) -> list:
        self._build()
        return list(self._tables)

    def vocabulary(self) -> list:
        """库中出现的全部「可指称名词」：表名 + 列名 + 样本值。

        两个下游共用它，都是为了让判断依据来自**真实 schema** 而不是硬编码词表 ——
        换一套库，判断口径自动跟着变：
          · 引用片段高亮（tools/explain）：用户核对来源时最想看的就是这些词
          · 澄清维度校验（agent/clarify）：问「前十」时句子里有没有出现某个指标名，
            靠它判定「指标」这一维到底是缺还是只是口语化
        """
        self._build()
        out = set()
        for t in self._tables:
            out.add(str(t))
            m = self._meta.get(t) or {}
            for c in m.get("columns") or []:
                out.add(str(c))
            for s in m.get("samples") or []:
                out.add(str(s))
        # 长的在前：命中判断用 startswith / in 都是包含语义，长词优先可避免
        # 「成绩」把「综合素质测评成绩」的匹配抢走
        return sorted(out, key=len, reverse=True)

    def sample_values(self) -> list:
        """只返回**样本值**（真实数据里出现的值，如姓名）—— 不含表名/列名。

        与 `vocabulary()` 的区别很关键：澄清里的「实体近邻」判断必须拿值当目标。
        拿全表（含列名）匹配会立刻产生假阳性：「智育测评**分**」里的子串「评分」
        离列名「扣分」只差一字、「**排名**」离列名「姓名」只差一字 ——
        结果每个正常问句都会被判成「实体写错了」。值是真正的可指称实体，没这个问题。
        """
        self._build()
        out = set()
        for t in self._tables:
            for s in (self._meta.get(t) or {}).get("samples") or []:
                s = str(s).strip()
                if len(s) >= 2:          # 单字符值（如 "1"）没有指称意义
                    out.add(s)
        return sorted(out, key=len, reverse=True)

    def full_schema(self) -> str:
        self._build()
        try:
            return self.db.get_table_info() or ""
        except Exception:
            return ""

    def schema_of(self, tables: list) -> str:
        if not tables:
            return ""
        try:
            return self.db.get_table_info(tables) or ""
        except Exception:
            return ""

    def select(self, question: str, top_k: int = None) -> list:
        """返回 [(表名, 分数, 理由)]，按分数降序

        打分是「词法原始分 + 去基线的向量分」，不做按问题长度归一 ——
        归一化会把「胡子扬的智育测评分是多少」这种长问题的有效信号摊薄，
        实测直接把 top1 准确率从 90% 拉到 60%。
        """
        if top_k is None:
            top_k = DEFAULT_TOP_K
        self._build()
        if not self._tables:
            return []
        q_grams = _grams(question)
        q_uni = _unigrams(question)
        q_words = _ascii_words(question)
        if not q_grams and not q_uni:
            return [(t, 0.0, "无可用关键词") for t in self._tables[:top_k]]

        n = len(self._tables)
        if self.embeddings is not None:
            self._build_vectors()
        vec_scores = {}
        if self._vec:
            try:
                qv = self.embeddings.embed_query(question)
                vec_scores = {t: _cosine(qv, tv) for t, tv in self._vec.items()}
            except Exception:
                vec_scores = {}
        # 向量分必须先减基线：智谱 embedding 对「完全不相关的两段中文」余弦
        # 也有 0.4~0.5，不减基线的话每张表都能白拿 0.85 左右的分，
        # 真正的词法命中反而被淹没 —— 实测那时 top1 只有 60%。
        # 用中位数当基线，只保留「明显高于同伴」的语义信号。
        baseline = 0.0
        if vec_scores:
            vals = sorted(vec_scores.values())
            baseline = vals[len(vals) // 2]

        scored = []
        for t in self._tables:
            m = self._meta[t]
            score, hits = 0.0, []
            for g in q_grams:
                if g in m["grams"]:
                    # 权重取「表侧」的档位：整词命中满权、CamelCase 子词半权，
                    # 再乘上「问题侧」的英文增益。中文 gram 不在 gweight 里，
                    # 默认 1.0 —— 中文打分与改动前逐位一致。
                    w = m["gweight"].get(g, 1.0)
                    if g in q_words:
                        w *= ASCII_WORD_WEIGHT
                    idf = math.log(1 + n / self._df.get(g, 1))
                    score += w * idf
                    hits.append(g)
            for c in q_uni:
                if c in m["unigrams"]:
                    idf = math.log(1 + n / self._df.get(c, 1))
                    score += UNIGRAM_WEIGHT * idf
                    hits.append(c)
            # 样本值出现在问题里 —— 说明问的就是这张表里的实体，强信号
            sample_hit = [s for s in m["samples"] if len(s) >= 2 and s in question]
            if sample_hit:
                score += SAMPLE_HIT_GAIN * min(len(sample_hit), 3)
            v = vec_scores.get(t, 0.0) - baseline
            score += VEC_GAIN * v

            why = []
            if sample_hit:
                why.append(f"样本值命中 {'/'.join(sample_hit[:2])}")
            if hits:
                why.append("关键词 " + "/".join(sorted(hits)[:5]))
            if v > 0.05:
                why.append(f"语义 +{v:.2f}")
            if not why:
                # 全零分时排序退化成表名字母序，此时的名次**没有任何语义**。
                # 把这件事写进理由里，免得调用方（尤其是 LLM）把字母序当相关性。
                why.append("无词法命中（排序退化为表名字母序，需向量检索兜底）")
            scored.append((t, score, "；".join(why)))

        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored[:top_k]

    def semantic_notes(self, tables: list) -> str:
        """给这几张表补一段紧凑的中文语义说明，供 LLM 生成 SQL 时参考。

        中文提问 + 英文列名的组合，光给 CREATE TABLE 是很难写出正确 SQL 的；
        把「列的中文含义」摊开，等于把 Schema Linking 的结论顺带交代给生成端。
        """
        self._build()
        lines = []
        for t in tables:
            p = (self._meta.get(t) or {}).get("profile") or {}
            if not p:
                continue
            aliases = "、".join(p.get("aliases") or [])
            cols = "，".join(f"{k}={v}" for k, v in (p.get("columns_zh") or {}).items())
            seg = f"-- {t}"
            if p.get("zh"):
                seg += f"：{p['zh']}"
            if aliases:
                seg += f"（别名 {aliases}）"
            if cols:
                seg += f"；列含义：{cols}"
            lines.append(seg)
        return "\n".join(lines)

    def schema_for(self, question: str, limit: int = None,
                   top_k: int = None) -> dict:
        """给 LLM 用的 schema 文本 + 本次选表信息。

        全量装得下就全量返回（不做任何取舍，零风险）；装不下才检索。

        limit / top_k 不写成默认参数：默认值会在函数定义时绑定到模块常量上，
        之后再改 FULL_SCHEMA_LIMIT 就不生效了（调参时踩过）。
        """
        if limit is None:
            limit = FULL_SCHEMA_LIMIT
        if top_k is None:
            top_k = DEFAULT_TOP_K
        self._build()
        full = self.full_schema()
        if len(full) <= limit:
            notes = self.semantic_notes(self._tables)
            text = full + (f"\n\n-- 表与列的中文语义：\n{notes}" if notes else "")
            return {"text": text, "tables": list(self._tables), "chosen": list(self._tables),
                    "strategy": "full", "note": f"全量 schema {len(full)} 字符，未做裁剪",
                    "ranking": []}

        ranking = self.select(question, top_k=max(top_k, DEFAULT_TOP_K))
        chosen, used = [], 0
        for t, _s, _w in ranking:
            block = self.schema_of([t])
            if chosen and used + len(block) > limit:
                break
            chosen.append(t)
            used += len(block)
        if not chosen and ranking:
            chosen = [ranking[0][0]]

        text = self.schema_of(chosen)
        notes = self.semantic_notes(chosen)
        if notes:
            text += f"\n\n-- 选中表与列的中文语义：\n{notes}"
        others = [t for t in self._tables if t not in chosen]
        if others:
            # 告诉 LLM「库里还有哪些表」：便宜，且能挡住它臆造字段，
            # 也能让它判断该转 RAG 而不是硬编 SQL
            text += ("\n\n-- 库中还有其他未展开的表（本次未选中，字段未列出）：\n-- "
                     + "、".join(str(t) for t in others[:80]))

        return {"text": text, "tables": list(self._tables), "chosen": chosen,
                "strategy": "link", "ranking": ranking,
                "note": f"全量 {len(full)} 字符超限 {limit}，"
                        f"已按相关内容选出 {len(chosen)}/{len(self._tables)} 张表"}


def _cosine(a, b) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
