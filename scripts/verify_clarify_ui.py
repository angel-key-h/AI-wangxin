# -*- coding: utf-8 -*-
"""验证澄清选项按钮是「真的可点、点了真的能继续」——这是「体验好」那 3 分的落点。

只验证行为链路，不重复算指标：
  1. 问「胡子杨」→ 必须出现澄清，且选项里有「胡子扬」
  2. 页面上必须**真的存在**一个 label 为「胡子扬」的按钮（不是一行文字）
  3. 点它 → 必须以「胡子扬」继续作答，且答案里带出真实数据（90.7）
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))   # 同目录的 _preflight
os.chdir(ROOT)

from _preflight import require_keys  # noqa: E402

# 本脚本的每一条断言都依赖真实作答（触发澄清 → 点选项 → 两轮引导给出数据），
# 缺 Key 时「智能体未就绪」会让它们全部失败。若照常返回 1，读日志的人会以为
# **界面链路坏了**，实际只是没配环境 —— 这正是退出码约定要消除的歧义。
# 所以这里提前以 2（缺 API Key）退出，与 verify_app / verify_live 的降级口径一致。
_rc = require_keys("澄清交互链路需要真实调用模型，无 Key 时无法验证")
if _rc:
    sys.exit(_rc)

from streamlit.testing.v1 import AppTest  # noqa: E402

FAIL = []


def check(name, ok, detail=""):
    # detail 只在**失败时**打印：无条件打印会让成功的项也挂一句
    # 「答案里未出现 90.7」这种反向描述，读日志的人会以为它没通过。
    print(f"{'✅' if ok else '❌'} {name}" + (f" —— {detail}" if (detail and not ok) else ""))
    if not ok:
        FAIL.append(name)


at = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=300)
at.run()
check("初始页无异常", not at.exception,
      "; ".join(str(e.value) for e in at.exception[:2]))

at.chat_input[0].set_value("胡子杨").run()
warn = " ".join(str(w.value) for w in at.warning)
check("触发澄清（提示里点名了正确写法）", "胡子扬" in warn and "仅一字之差" in warn,
      warn[:120].replace("\n", " "))

labels = [b.label for b in at.button]
target = [l for l in labels if l.strip() == "胡子扬"]
check("页面上存在可点的「胡子扬」按钮（不是纯文字）", bool(target),
      f"现有按钮：{labels[:8]}")

if target:
    key = next(b.key for b in at.button if b.label.strip() == "胡子扬")
    at.button(key=key).click().run()
    check("点击后无异常", not at.exception,
          "; ".join(str(e.value) for e in at.exception[:2]))
    # 点完「胡子扬」后**还会再问一次**「你想了解哪方面」——这是正确行为：
    # 裸名字本身仍缺「查询维度」。所以这里要断言的是「进入了第二轮引导」，
    # 而不是「立刻给出数据」（早先的断言就是照着旧行为写的，行为一改就误报失败）。
    labels2 = [b.label for b in at.button]
    check("点击实体后进入第二轮引导（问『哪方面』）",
          any("成绩和排名" in l or "各分项测评分" in l for l in labels2),
          f"现有按钮：{labels2[:8]}")
    opt2 = next((b for b in at.button
                 if b.label.strip() in ("成绩和排名", "各分项测评分")), None)
    if opt2 is not None:
        at.button(key=opt2.key).click().run()
        check("点选具体维度后无异常", not at.exception,
              "; ".join(str(e.value) for e in at.exception[:2]))
        body = " ".join(str(m.value) for m in at.markdown)
        check("两轮引导后以真实数据作答（闭环走通）",
              "90.7" in body or "104.3" in body,
              "答案里未出现 90.7 / 104.3")

print()
if FAIL:
    print(f"❌ 未通过 {len(FAIL)} 项：" + "、".join(FAIL))
    sys.exit(1)
print("✅ 全部通过：澄清选项可点、且点了能接着作答")
sys.exit(0)
