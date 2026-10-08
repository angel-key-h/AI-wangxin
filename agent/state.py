from typing import Any, Dict, TypedDict, List, Optional

class AgentState(TypedDict):
    question: str
    # 指代消解后的自足问题：检索 / Schema Linking / SQL 生成都用它，
    # 原始 question 只用于界面回显，避免用户看到被改写过的提问而困惑
    resolved_question: str
    # 本轮是否真的改写过（用于可解释性展示与「多轮准确率」评测统计）
    rewritten: bool
    # 历史对话 [{"role": "user"|"assistant", "content": str}]，由 app.py 注入。
    # 注意：LangGraph 的 checkpointer 只保存 state，不会自动把历轮提问喂给 LLM，
    # 所以必须显式带上——否则「他／那第二名呢」这类追问只能靠模型瞎猜。
    history: List[Dict[str, str]]
    # 语义校验与澄清（中级难题 3）：问题缺关键维度时不硬答，先反问
    needs_clarification: bool
    clarification: str
    clarify_options: List[str]
    clarify_reason: str
    # 缺的是**哪几维**（受控词表，见 agent.clarify.DIMENSIONS）。
    # 之前只有一个 20 字自由理由，说不清缺什么；结构化之后澄清消息能列出
    # 具体维度，评测也能按维度聚合出「待补维度分布」。
    missing_dimensions: List[str]
    # 本次维度判定是否由**规则兜底**产出（LLM 没给维度 / LLM 调用失败）。
    # 界面上要如实标注依据来源，否则用户无从判断这次澄清可不可信。
    clarify_by_rule: bool
    intent: str
    sql: Optional[str]
    sql_result: Optional[list]
    sql_error: Optional[str]
    sql_reasoning: str
    sql_columns: List[str]
    # Schema Linking 本次选表的结果（text/chosen/tables/strategy/note/ranking），
    # 供界面展示「为什么只给它这几张表」
    schema_picked: Dict[str, Any]
    retrieved_chunks: List[dict]
    final_answer: str
    trace: List[str]
