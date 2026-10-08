# -*- coding: utf-8 -*-
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_openai import ChatOpenAI
from config import Config
from tools.sql_tool import SQLTool

llm = ChatOpenAI(model=Config.LLM_MODEL, api_key=Config.LLM_API_KEY, base_url=Config.LLM_BASE_URL, temperature=0)
schema = SQLTool().get_schema()[:2000]
question = '一共有多少客户？'

prompt = f'''根据数据库 schema 和用户问题，生成一条 SQLite 查询语句。

【数据库 Schema】
{schema}

【用户问题】
{question}

【要求】
- 只输出 SQL 语句，不要多余内容
- 如果是 MySQL 特有语法，请转为 SQLite 兼容写法
- 如果不能生成，输出：NOSQL'''

sql = llm.invoke(prompt).content.strip()
sql = sql.replace('`sql', '').replace('`', '').strip()
print('=== LLM 原始输出 ===')
print(repr(sql))
print()
print('=== 判断结果 ===')
print('以 select/with/explain 开头?', sql.lower().startswith(('select', 'with', 'explain')))