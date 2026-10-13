# -*- coding: utf-8 -*-
"""评测脚本的公共前置检查：把「缺依赖」变成可操作提示，而不是崩栈。

为什么需要它：
  评测脚本此前在缺 API Key 或缺外部数据集时，会从第三方库内部抛异常
  （openai.OpenAIError: Missing credentials ... / FileNotFoundError: OHR-Bench.parquet）。
  这些报错既没说明该配哪一项，也没说明数据该放哪，用户拿到只会一头雾水。
  本模块统一处理这三类终止条件，并约定退出码：
      0 = 正常完成（达标与否由各脚本自己决定）
      2 = 缺少 API Key（属「没配环境」，不是「代码坏了」）
      3 = 缺少外部数据集（OHR-Bench，属「数据没就位」）
  这样一键脚本 / CI 能据此区分「跑挂了」和「没配齐」。

用法（在各评测脚本 main() 里，构造 AgentGraph 之前调用）：
    from _preflight import require_keys, require_ohr, EXIT_NO_KEY, EXIT_NO_DATA
    rc = require_keys()               # 返回 0 或 2
    if rc:
        return rc
    rc = require_ohr()                # 返回 0 或 3
    if rc:
        return rc
"""
import os
import sys

EXIT_OK = 0
EXIT_NO_KEY = 2
EXIT_NO_DATA = 3

_BAR = "=" * 78


def _root() -> str:
    # 本文件在 scripts/ 下，仓库根 = 上一级
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def require_keys(purpose: str = "") -> int:
    """检查所需的 API Key；缺则打印可操作提示并返回 EXIT_NO_KEY，否则返回 0。

    purpose 用来在提示里说明「为什么这个评测必须要 Key」，便于用户判断
    是该去配 Key 还是干脆跳过这项。
    """
    try:
        sys.path.insert(0, _root())
        from config import Config  # noqa: WPS433
    except Exception as e:  # pragma: no cover - config 缺失属严重安装问题
        print(_BAR)
        print(f"⏭️  无法导入 config（{type(e).__name__}: {e}），请确认在仓库根目录运行。")
        print(_BAR)
        return EXIT_NO_KEY

    missing = Config.missing_keys()
    if not missing:
        return EXIT_OK
    print(_BAR)
    print("⏭️  缺少必要的 API Key（" + "、".join(missing) + "），本评测无法执行。")
    if purpose:
        print(f"    原因：{purpose}")
    print("    请在项目根目录 .env 中配置后重跑（模板见 .env.example 或 README 第 5 节）：")
    print("      LLM_API_KEY=...            LLM_MODEL=qwen3-max")
    print("      EMBEDDING_API_KEY=...      EMBEDDING_MODEL=text-embedding-v4")
    print(_BAR)
    return EXIT_NO_KEY


def require_ohr(files=("OHR-Bench.parquet",)) -> int:
    """检查 OHR-Bench 数据集是否就位；缺则提示 OHR_DIR 用法并返回 EXIT_NO_DATA。

    OHR-Bench 是公开数据集，体积远大于代码，**故意不纳入版本库**。
    因此新克隆的仓库必然没有它，脚本必须能自己说清楚这一点。

    files 用来指定需要哪些文件，因为不同脚本依赖的文件不同：
      · 评测脚本（eval_doc_recog / eval_doc_quality / eval_correction）
        读 **OHR-Bench.parquet**（默认）；
      · 物化脚本（build_demo_kb / prepare_ohr_kb）读 **retrieval.zip**，
        后者还要 parquet。缺哪个就报哪个，别让用户去猜。
    """
    ohr = os.environ.get("OHR_DIR") or os.path.join(_root(), "data", "ohr")
    missing = [f for f in files if not os.path.exists(os.path.join(ohr, f))]
    if not missing:
        return EXIT_OK
    print(_BAR)
    print("⏭️  未找到 OHR-Bench 数据集，本评测无法执行。")
    for f in missing:
        print(f"    缺少：{os.path.join(ohr, f)}")
    print("    OHR-Bench 是公开数据集（7 领域 1261 份文档），体积大、不入库，")
    print("    新克隆的仓库默认没有。请下载后放到 data/ohr/，或用环境变量覆盖：")
    print("      set OHR_DIR=D:\\path\\to\\ohr          (Windows)")
    print("      export OHR_DIR=/path/to/ohr          (Linux/macOS)")
    print("    素材包需含 retrieval.zip 与 OHR-Bench.parquet")
    print("    （parquet 含 gt_text / formatting_noise_* 字段）。")
    print(_BAR)
    return EXIT_NO_DATA


def model_provenance() -> dict:
    """本次评测实际使用的模型与端点，供报告**自述运行条件**。

    为什么需要：报告此前不记录模型，于是同一个指标在不同配置下测出的值混在
    一起、无法区分来源 —— 例如 NL2SQL 的 34/34 究竟出自 qwen3-max 还是
    deepseek-chat，从产物里看不出来，核对的人只能猜。这与「报告 mode 标错」
    是同一类问题：产物必须如实描述自己，而不是让读者假设。
    """
    try:
        sys.path.insert(0, _root())
        from config import Config  # noqa: WPS433
    except Exception:  # pragma: no cover - config 缺失属严重安装问题
        return {}
    return {
        "llm_model": Config.LLM_MODEL,
        "llm_base_url": Config.LLM_BASE_URL,
        "embedding_model": Config.EMBEDDING_MODEL,
    }
