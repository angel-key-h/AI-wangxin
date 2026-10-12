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


def require_ohr() -> int:
    """检查 OHR-Bench 数据集是否就位；缺则提示 OHR_DIR 用法并返回 EXIT_NO_DATA。

    OHR-Bench 是公开数据集，体积远大于代码，**故意不纳入版本库**。
    因此新克隆的仓库必然没有它，脚本必须能自己说清楚这一点。
    """
    ohr = os.environ.get("OHR_DIR") or os.path.join(_root(), "data", "ohr")
    parquet = os.path.join(ohr, "OHR-Bench.parquet")
    if os.path.exists(parquet):
        return EXIT_OK
    print(_BAR)
    print("⏭️  未找到 OHR-Bench 数据集，本评测无法执行。")
    print(f"    期望路径：{parquet}")
    print("    OHR-Bench 是公开数据集（7 领域 1261 份文档），体积大、不入库，")
    print("    新克隆的仓库默认没有。请下载后放到 data/ohr/，或用环境变量覆盖：")
    print("      set OHR_DIR=D:\\path\\to\\ohr          (Windows)")
    print("      export OHR_DIR=/path/to/ohr          (Linux/macOS)")
    print("    目录下需含 OHR-Bench.parquet（含 gt_text / formatting_noise_* 字段）。")
    print(_BAR)
    return EXIT_NO_DATA
