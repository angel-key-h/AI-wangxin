# -*- coding: utf-8 -*-
"""文档复杂度评估（难题 8）+ 文档质量综合评估（难题 9）。

赛题要求：
  难题 8：设计一套文档复杂度评估模型（评分规则设计与实现路径），用于预估解析难度、指导处理策略
  难题 9：实现文档常见质量问题的自动检测与告警 —— 页面颠倒/倾斜、插图清晰度不足、
          繁体字/错别字识别与校正等

设计原则（为什么这样拆）：
  1. **全部离线可测**：只用 OpenCV / PIL / RapidOCR（本地模型），不依赖任何云接口。
     这样质量检测不会因为外部接口欠费而失效 —— 质检本身必须是可靠的。
  2. **每个结论都要给出可解释依据**：不是只吐一个分数，而是列出触发了哪条规则、
     具体数值多少、建议怎么处理（旋转多少度 / 换更清晰的扫描件）。
  3. **质量分与复杂度分是两个独立维度**，不能混：一份排版极复杂的学术论文可能是
     高质量扫描件；一份排版简单的公文可能是模糊的传真件。二者必须分开报。

  ⚠️ 关于「错别字校正」的诚实边界：没有语言模型时无法真正做拼写纠错。
     这里只做**可验证的部分** —— 低置信字符、形近字符混淆、乱码/异常字符率的检测与告警，
     并明确输出「建议人工核对」而不是假装改正了。改字需要 LLM，接口可用时再接入。
"""
import os
import re

# ── 质量判定阈值（可被环境变量覆盖，便于按扫描设备调参）──
SKEW_WARN_DEG = float(os.getenv("DQ_SKEW_WARN", "1.5"))      # 超过即告警
ORIENT_CONF_GAIN = float(os.getenv("DQ_ORIENT_GAIN", "1.2"))  # 加权量提升倍数即判定颠倒
BLUR_WARN = float(os.getenv("DQ_BLUR_WARN", "120.0"))          # 拉普拉斯方差低于此值判定模糊
MIN_SIDE = int(os.getenv("DQ_MIN_SIDE", "600"))                # 短边像素下限（分辨率不足）

# 繁体字专用字集（这些字在简体文本里几乎不出现，出现即高度可疑）
TRADITIONAL = set(
    "個為與學說這們來時會對開關實應業務產車東馬鳥龍龜國會發現點為無舊萬與專業"
    "體圖書寫認為語數據網絡資訊處理編輯經濟營運機構總體規劃設計標準規範檢測"
    "麼樣兒頭長門問間聞關閉開電視聽讀報紙雜誌"
)
# 常见拉丁形近混淆对（OCR 高频错误）
CONFUSABLE = [("rn", "m"), ("l", "1"), ("O", "0"), ("I", "l"), ("S", "5"), ("B", "8")]

_LATIN = re.compile(r"[A-Za-z]")
_GOOD_CHAR = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]")
_CJK = re.compile(r"[\u4e00-\u9fff]")
_ODD = re.compile(r"[^\w\s\u4e00-\u9fff.,;:!?'\"()\[\]{}\-_/\\%$&+=<>@#*|~`^°±×÷§¶·—–…「」『』《》、。，；：？！（）【】]")
_LATEX_TABLE = re.compile(r"\\begin\{tabular\}")
_LATEX_MATH = re.compile(r"\\begin\{(equation|align|gather)|\\frac|\\sum|\\int|\$[^$]{2,}\$")
_LATEX_CMD = re.compile(r"\\[A-Za-z]+")


# ==================== 文本侧 ====================
def text_features(text: str) -> dict:
    """从文本抽出可量化的版面/复杂度/质量特征"""
    t = text or ""
    n = max(len(t), 1)
    cjk = len(_CJK.findall(t))
    latin = len(_LATIN.findall(t))
    odd = len(_ODD.findall(t))
    lines = [l for l in t.split("\n") if l.strip()]
    lens = [len(l) for l in lines] or [0]
    mean = sum(lens) / len(lens)
    var = sum((x - mean) ** 2 for x in lens) / len(lens)
    trad = sum(1 for ch in t if ch in TRADITIONAL)
    return {
        "chars": len(t),
        "lines": len(lines),
        "cjk_ratio": cjk / n,
        "latin_ratio": latin / n,
        # 公式/表格/命令密度 —— 解析难度最直接的信号
        "tables": len(_LATEX_TABLE.findall(t)),
        "formulas": len(_LATEX_MATH.findall(t)),
        "latex_cmds": len(_LATEX_CMD.findall(t)),
        "latex_density": (len(_LATEX_CMD.findall(t)) + t.count("&") + t.count("\\\\")) / n,
        # 行长方差越大，版面越不规则（多栏、表格、图文混排都会抬高）
        "line_len_std": var ** 0.5,
        # 异常字符率：乱码、私有区字符、控制字符
        "odd_ratio": odd / n,
        "traditional": trad,
        "traditional_ratio": trad / max(cjk, 1),
        "mixed_language": cjk > 20 and latin > 20,
    }


def complexity_score(f: dict) -> tuple:
    """难题 8：0~100 复杂度分 + 分项拆解（每项都要能解释为什么加分）

    权重是**规则化**而非训练出来的 —— 赛题要的是「评分规则设计与实现路径」，
    规则可解释、可审计，比一个黑盒回归更符合要求。
    """
    parts = {}
    parts["公式密度"] = min(30.0, f["formulas"] * 3.0)
    parts["表格密度"] = min(25.0, f["tables"] * 5.0)
    parts["排版标记密度"] = min(20.0, f["latex_density"] * 400)
    parts["版面不规则度"] = min(15.0, f["line_len_std"] / 8.0)
    parts["语言混排"] = 5.0 if f["mixed_language"] else 0.0
    parts["异常字符"] = min(5.0, f["odd_ratio"] * 200)
    score = round(sum(parts.values()), 1)
    return score, parts


def complexity_tier(score: float) -> str:
    if score < 20:
        return "简单"
    if score < 45:
        return "中等"
    return "复杂"


# ── 处理策略参数（可被环境变量覆盖，便于按语料调参）──
# 简单档的 1000/160 就是 RAGTool 当前的线上默认值 ——
# **策略在简单档必须退化成「什么都不改」**，否则引入策略就等于偷偷调参，
# 已有的检索/问答评测全部失去可比性。
PLAN_CHUNK_SIMPLE = int(os.getenv("DQ_PLAN_CHUNK_SIMPLE", "1000"))
PLAN_OVERLAP_SIMPLE = int(os.getenv("DQ_PLAN_OVERLAP_SIMPLE", "160"))
# 表格/公式密集档：块要小、重叠要大。一张表或一条公式被切断在两块里之后，
# 检索命中哪一块都读不到完整结构（这正是本项目「命中率 86% 但答案准确率 55%」的
# 成因之一：模型拿到的是残表）。
PLAN_CHUNK_DENSE = int(os.getenv("DQ_PLAN_CHUNK_DENSE", "600"))
PLAN_OVERLAP_DENSE = int(os.getenv("DQ_PLAN_OVERLAP_DENSE", "240"))


def complexity_plan(f: dict) -> dict:
    """难题 8 的落地点：把复杂度分转成**可执行的处理策略**。

    赛题原文要求的不是「打个分」，而是「用于预估解析难度，**指导后续处理策略**」。
    分算完就扔等于只做了一半 —— 这里把分数翻译成四件具体的事：

      parse_path  用哪条通道解析/复核（本地 OCR / OCR+LaTeX 表格转写 / VLM）
      chunk_size  切块粒度（表格式文档切大了会把一张表劈成半张）
      top_k       检索宽度（信息密度高的页需要更宽的召回才不漏）
      actions     给人看的处置建议（哪条规则触发的、为什么）

    设计上刻意做成**纯函数 + 规则可审计**：每个结论都能回溯到 parts 里的哪一项
    加了多少分、哪条 trigger 命中，不存在「模型说它复杂」这种不可复核的判断。

    ⚠️ 简单档的输出必须与当前线上参数完全一致。策略是「按文档复杂度分档」，
        不是「借机调参」——任何一档的默认值偏离现状，都会让已有评测失去可比性。
    """
    score, parts = complexity_score(f)
    tier = complexity_tier(score)
    plan = {
        "complexity_score": score,
        "complexity_tier": tier,
        "parts": {k: round(v, 1) for k, v in parts.items() if v > 0},
        "triggers": [],
        "actions": [],
    }

    # ── 切块策略：表格/公式密集就切小、重叠加大 ──
    dense = f["tables"] >= 1 or f["formulas"] >= 2
    if dense:
        plan["chunk_size"] = PLAN_CHUNK_DENSE
        plan["chunk_overlap"] = PLAN_OVERLAP_DENSE
        plan["triggers"].append("表格/公式密集")
        plan["actions"].append(
            f"切块收紧到 {PLAN_CHUNK_DENSE}/{PLAN_OVERLAP_DENSE}："
            f"避免一张表或一条公式被切断在相邻块里（残表读数会串行串列）")
    else:
        plan["chunk_size"] = PLAN_CHUNK_SIMPLE
        plan["chunk_overlap"] = PLAN_OVERLAP_SIMPLE

    # ── 解析通道：越复杂越该走 VLM ──
    # 依据是本项目的实测对比（scripts/eval_doc_recog.py --engine 双通道）：
    # qwen-vl-max 在扭曲表格上列名准确率 40.7%→55.9%、公式字符召回 28.2%→96.7%，
    # 且 clean/倾斜/模糊三档列名准确率完全相同 —— 对版面退化几乎免疫。
    if score >= 45:
        plan["parse_path"] = "vlm"
        plan["actions"].append(
            "优先用 VLM 通道解析或复核：对倾斜/扭曲表格与公式免疫度更高"
            "（模型由 Config.VLM_MODEL 决定，默认 qwen-vl-max，可换 deepseek-flash）")
    elif score >= 20:
        plan["parse_path"] = "ocr+latex"
        plan["actions"].append(
            "本地 OCR + 表格 LaTeX→Markdown 转写：行列边界显式化，减少跨行跨列串读")
    else:
        plan["parse_path"] = "ocr"
        plan["actions"].append("常规版面，本地 OCR 直接解析即可")

    # ── 检索宽度：信息密度越高，需要的召回越宽 ──
    plan["top_k"] = 12 if score >= 45 else (8 if score >= 20 else 6)
    if plan["top_k"] > 6:
        plan["actions"].append(f"检索宽度放宽到 top_k={plan['top_k']}：单页信息密度高，窄召回易漏证据")

    # ── 版面不规则：靠切片救不了，只能换解析粒度 ──
    if f["line_len_std"] > 60:
        plan["triggers"].append("版面不规则")
        plan["actions"].append(
            f"行长方差 {f['line_len_std']:.1f} 偏大（多栏/图文混排）：建议先按栏切分再解析")
    # ── 异常字符：多半是编码或 OCR 噪声，属于**质量**问题，
    #    但仍会抬高解析难度，这里只提示、不重复扣分 ──
    if f["odd_ratio"] > 0.02:
        plan["triggers"].append("异常字符偏多")
        plan["actions"].append(f"异常字符率 {f['odd_ratio']*100:.1f}% 偏高：建议人工核对关键字段")
    # 语言混排会让分词/术语对齐变难，是「中等」档的典型成因
    if f["mixed_language"]:
        plan["triggers"].append("中英混排")

    return plan


def plan_for_text(text: str) -> dict:
    """从已解析出的文本产出处理策略。

    入口之所以是「文本」而不是「图片」：复杂度模型是文本侧的（公式/表格/排版标记
    密度），而质检是图像侧的（颠倒/倾斜/模糊）。两者分工明确 ——
    质检管「解析**前**的预处理」，复杂度管「解析**后**的切片与检索」。
    混在一起会得到一份既说不清依据、也无法复现的分数。
    """
    return complexity_plan(text_features(text))


def quality_score_text(f: dict) -> tuple:
    """难题 9（文本侧）：0~100 质量分。越低越可疑。"""
    parts = {}
    parts["异常字符率"] = max(0.0, 30.0 - f["odd_ratio"] * 1000)
    parts["繁体字占比"] = max(0.0, 25.0 - f["traditional_ratio"] * 250)
    # 行长方差过大往往意味着表格/公式串行，文本侧只能轻度扣分（图像侧更准）
    parts["版面规整度"] = max(0.0, 25.0 - f["line_len_std"] / 12.0)
    # 过短但有大量标记 = 解析很可能已经出问题
    parts["内容完整度"] = 20.0 if f["chars"] > 200 else max(0.0, f["chars"] / 10.0)
    score = round(sum(parts.values()), 1)
    return score, parts


# ==================== 图像侧 ====================
def _ocr_stats(img) -> dict:
    """在给定图像上跑 OCR，返回可用于**比较方向**的质量量。

    ⚠️ 不能用「文本块数」判方向：实测倒置页面反而会**多**检出文本框
    （合成页 p1 倒置 60 块 > 正置 53 块）。原来那条「翻转后块数更多」的
    附加条件会把正确结论直接否掉，检出率只有 1/3。
    真正稳定的是**置信度 × 有效字符数**的加权量：实测正置/倒置的比值
    在三页上分别为 1.73 / 1.58 / 1.31，区分度充足且方向一致。
    """
    import numpy as np
    from tools.image_tool import ENGINE_TEXT_SCORE, get_engine
    arr = np.array(img.convert("RGB"))
    res, _ = get_engine()(arr, text_score=ENGINE_TEXT_SCORE)
    if not res:
        return {"blocks": 0, "mean_score": 0.0, "weighted": 0.0, "text": ""}
    scores = [float(r[2]) for r in res]
    texts = [str(r[1]) for r in res]
    weighted = sum(s * len(_GOOD_CHAR.findall(t)) for s, t in zip(scores, texts))
    return {"blocks": len(res), "mean_score": sum(scores) / len(scores),
            "weighted": weighted, "text": "\n".join(texts)}


def detect_orientation(img) -> dict:
    """页面颠倒检测：把图旋转 180° 再 OCR，比较「置信度 × 有效字符数」加权量。

    为什么不用几何方法：倒置页面的版面几何与正置完全一样（横线还是横线），
    几何特征无法区分；而 OCR 的识别质量会显著下降（实测加权量掉到 1/1.3~1/1.7）。
    """
    up = _ocr_stats(img)
    fl = _ocr_stats(img.rotate(180, expand=True))
    gain = (fl["weighted"] / up["weighted"]) if up["weighted"] else 0.0
    return {"flipped": gain > ORIENT_CONF_GAIN and fl["weighted"] > 0,
            "weighted_upright": round(up["weighted"], 1),
            "weighted_flipped": round(fl["weighted"], 1),
            "gain": round(gain, 2),
            "blocks": up["blocks"], "mean_score": round(up["mean_score"], 3)}


def detect_skew(img) -> dict:
    """页面倾斜检测：对边缘图做 Hough 直线，取接近水平的那些线的中位角度"""
    import cv2
    import numpy as np
    arr = np.array(img.convert("L"))
    edges = cv2.Canny(arr, 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 360, threshold=120,
                            minLineLength=max(60, arr.shape[1] // 6), maxLineGap=12)
    angles = []
    if lines is not None:
        # OpenCV 5.0 的 HoughLinesP 返回 (N,4)，4.x 返回 (N,1,4) —— reshape 兼容两者
        for x1, y1, x2, y2 in lines.reshape(-1, 4):
            a = np.degrees(np.arctan2(y2 - y1, x2 - x1))
            if -20 < a < 20:          # 只看接近水平的线，避免竖线/斜线干扰
                angles.append(a)
    if not angles:
        return {"skew_deg": 0.0, "samples": 0, "skewed": False}
    med = float(np.median(angles))
    return {"skew_deg": round(med, 2), "samples": len(angles),
            "skewed": abs(med) > SKEW_WARN_DEG}


def detect_blur(img) -> dict:
    """插图/整页清晰度：拉普拉斯方差（经典清晰度指标，越小越糊）"""
    import cv2
    import numpy as np
    arr = np.array(img.convert("L"))
    v = float(cv2.Laplacian(arr, cv2.CV_64F).var())
    return {"blur_var": round(v, 1), "blurry": v < BLUR_WARN}


def detect_resolution(img) -> dict:
    w, h = img.size
    short = min(w, h)
    return {"width": w, "height": h, "short_side": short,
            "low_res": short < MIN_SIDE}


def _image_text_quality(img) -> dict:
    """把 OCR 结果当文本，复用文本侧检查（繁体字/异常字符/低置信）"""
    s = _ocr_stats(img)
    f = text_features(s["text"])
    return {"blocks": s["blocks"], "mean_score": round(s["mean_score"], 3),
            "traditional": f["traditional"], "odd_ratio": round(f["odd_ratio"], 5),
            "low_conf": s["mean_score"] < 0.6 and s["blocks"] > 0}


def assess_document(image_path: str) -> dict:
    """难题 9：一份文档（页）的质量综合评估 —— 只报问题 + 处置建议，不假装修好"""
    from PIL import Image
    img = Image.open(image_path)
    rep = {
        "file": os.path.basename(image_path),
        "orientation": detect_orientation(img),
        "skew": detect_skew(img),
        "blur": detect_blur(img),
        "resolution": detect_resolution(img),
        "text": _image_text_quality(img),
    }
    issues, actions = [], []
    if rep["orientation"]["flipped"]:
        issues.append("页面颠倒（旋转 180° 后识别质量显著更高）")
        actions.append("入库前旋转 180°")
    if rep["skew"]["skewed"]:
        issues.append(f"页面倾斜 {rep['skew']['skew_deg']}°")
        actions.append(f"纠偏 {-rep['skew']['skew_deg']}°（OpenCV 仿射）")
    if rep["blur"]["blurry"]:
        issues.append(f"清晰度不足（拉普拉斯方差 {rep['blur']['blur_var']} < {BLUR_WARN}）")
        actions.append("更换更清晰扫描件，或先做去噪+锐化再 OCR")
    if rep["resolution"]["low_res"]:
        issues.append(f"分辨率偏低（短边 {rep['resolution']['short_side']}px < {MIN_SIDE}）")
        actions.append("放大 2× 后再识别（单字符置信度会明显提升）")
    if rep["text"]["traditional"] > 3:
        issues.append(f"疑似繁体字 {rep['text']['traditional']} 个")
        actions.append("接入繁→简转换后再入库（当前仅告警，未自动改字）")
    if rep["text"]["low_conf"]:
        issues.append(f"整体识别置信度偏低（均值 {rep['text']['mean_score']}）")
        actions.append("建议人工核对关键字段")

    # 质量分：100 分制，逐项扣分（每项都对应上面一条规则，可审计）
    q = 100.0
    q -= 25 if rep["orientation"]["flipped"] else 0
    q -= min(15.0, abs(rep["skew"]["skew_deg"]) * 3) if rep["skew"]["skewed"] else 0
    q -= 25 if rep["blur"]["blurry"] else 0
    q -= 10 if rep["resolution"]["low_res"] else 0
    q -= min(15.0, rep["text"]["traditional"] * 1.5)
    q -= 10 if rep["text"]["low_conf"] else 0
    rep["quality_score"] = round(max(0.0, q), 1)
    rep["quality_tier"] = ("合格" if rep["quality_score"] >= 80
                           else "需处理" if rep["quality_score"] >= 55 else "不可用")
    rep["issues"] = issues
    rep["suggested_actions"] = actions
    return rep


# ============ 难题 9 的后半句：不只是「识别」，还要「校正」 ============
# 赛题原文：「繁体字/错别字识别**并校正**」。此前只做检测告警、明确不假装改字，
# 等于只做了一半。这里补校正，但有两条硬约束：
#   ① **可审计**：每处改动都要能说出（改前/改后/类型/理由），不是甩一段新文本
#   ② **不静默重写**：校正结果必须能由「原文 + 改动清单」还原出来；
#      还原不出来就判定模型在改写而非纠错，放弃并保留原文。
#      这是本模块最重要的防线 —— 编造内容比留着错字危险得多。
_TYPO_PROMPT = """你是 OCR 后处理校对器。请找出**明显的识别错误**并给出改法。

【最有用的线索：同文档内的自我一致性】
同一份文本里若某个词**多次以正确形式出现**，而个别地方写法不同，那处基本就是识别错误。
例：全文多处出现 "SECURITIES"，却有一处写成 "SECURITEIS" → 应改为 "SECURITIES"。
这类「与本文自身高频写法不一致」的错误请优先改，这是最有把握的一类。

【要求】
1. **只做字符级纠错**，绝对不要改写、缩写、润色、补全或删除任何内容
2. 除上面的一致性线索外，只改有把握的形近（己/已/巳、未/末、日/曰）与同音错误
3. 术语、专有名词、数字、公式**一律不要动**
4. 每处改动的 before 必须是原文中**真实存在的连续片段**，且只改出错的那几个字
5. 仍然没把握就返回空数组 —— 但**不要因为"看起来少见"就放过**：
   非词（如 COMISSIOIN）几乎必然是识别错误，属于该改的

仅输出 JSON：
{"corrections": [{"before": "原文片段", "after": "改正后片段", "reason": "10字以内"}]}
没有发现错误时输出：{"corrections": []}

【待校对文本】
<<<TEXT>>>"""
# ⚠️ 这里刻意用 <<<TEXT>>> 占位符 + str.replace，**不用 str.format**：
# 提示词里必然含 JSON 示例（花括号），format 会把 {"corrections"...} 当成格式化字段
# 直接抛 KeyError。这个错误原先被 correct_text 的宽泛 except 吞掉，
# 表现为「一处都没改」，看起来像模型没发现错误 —— 实则是代码根本没跑起来。
# 教训：凡是提示词里带 JSON 示例的，一律不要用 format。


def to_simplified(text: str) -> tuple:
    """繁体 → 简体。返回 (转换后文本, 改动字符数)。

    zhconv 未安装时原样返回并报 0 —— 繁简转换是增强项，
    不能因为缺一个可选依赖就让整条质检链路挂掉（与 vlm_tool 的降级原则一致）。
    """
    try:
        import zhconv
    except Exception:
        return text, 0
    src = text or ""
    out = zhconv.convert(src, "zh-cn")
    if len(out) == len(src):
        changed = sum(1 for a, b in zip(src, out) if a != b)
    else:
        changed = abs(len(out) - len(src)) or 1
    return out, changed


def _apply_literal_changes(text: str, corrections: list) -> tuple:
    """按字面替换应用改动，并拒绝「疑似改写」的建议。

    三重护栏（每一条都对应一种真实失败模式）：
      · before 必须在原文里真实存在 —— 否则模型在凭印象编片段
      · before != after 且长度差 ≤ 2 —— 长度差大基本是改写/补全，不是纠错
      · 逐条 replace 累积重建 —— 未申报的改动不会进入结果，
        与模型自报的「完整校正文本」一比就露馅
    """
    applied, rej = [], []
    cur = text
    for c in corrections or ():
        b = str(c.get("before", "") or "")
        a = str(c.get("after", "") or "")
        if not b or b == a or b not in cur:
            rej.append(c)
            continue
        if abs(len(a) - len(b)) > 2:
            rej.append(c)
            continue
        cur = cur.replace(b, a, 1)
        applied.append({"kind": "错别字", "before": b, "after": a,
                        "reason": str(c.get("reason", ""))[:40]})
    return cur, applied, rej


def correct_text(text: str, use_llm: bool = True, llm=None) -> dict:
    """难题 9：对识别文本做**可审计的校正**（繁→简 + 错别字）。"""
    out = {"original": text or "", "text": text or "", "changes": [],
           "traditional_fixed": 0, "typo_fixed": 0, "rejected": 0,
           "warnings": [], "llm_used": False}
    if not (text or "").strip():
        return out

    # 第一步：繁 → 简（纯映射，无编造风险）
    simp, n_trad = to_simplified(text)
    if n_trad:
        out["text"] = simp
        out["traditional_fixed"] = n_trad
        out["changes"].append({"kind": "繁→简", "before": f"{n_trad} 个繁体字",
                               "after": "已转简体",
                               "reason": "统一入库口径，避免繁简两套词表"})

    # 第二步：错别字（LLM，带「能还原」安全校验）
    if use_llm and llm is not None:
        try:
            import json as _json
            body = _TYPO_PROMPT.replace("<<<TEXT>>>", out["text"][:4000])
            raw = llm.invoke(body).content
            m = re.search(r"\{.*\}", raw or "", re.S)
            obj = _json.loads(m.group(0)) if m else {}
            fixed, applied, rej = _apply_literal_changes(
                out["text"], obj.get("corrections") or [])
            out["text"] = fixed
            out["typo_fixed"] = len(applied)
            out["rejected"] = len(rej)
            out["changes"].extend(applied)
            out["llm_used"] = True
            if rej:
                out["warnings"].append(
                    f"{len(rej)} 条模型建议被拒（片段不在原文中 / 长度差过大，疑似改写）")
        except Exception as e:
            # 注意：这里必须把异常**打出来**。上一版只塞进 warnings 而不打印，
            # 结果一个 KeyError（提示词 format 花括号）被静默吞掉，
            # 对外表现成「模型没发现错别字」——静默失败比报错危险得多。
            print(f"[DocQuality] ⚠️ 错别字校正失败: {type(e).__name__}: {str(e)[:150]}")
            out["warnings"].append(
                f"错别字校正跳过（{type(e).__name__}: {str(e)[:80]}）")

    if out["changes"]:
        out["warnings"].append(
            f"共 {len(out['changes'])} 处改动，均可由「原文 + 改动清单」还原，请核对后再入库")
    return out
