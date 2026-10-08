path = r'D:\中电云\streamlit_env\intelligent-qa-agent\agent\graph.py'
lines = open(path, 'r', encoding='utf-8').readlines()
lines[114] = '                trace_msg += f"\\n\U0001f4ca [SQL \u7ed3\u679c] \u2192 \u8fd4\u56de {row_count} \u884c\"\n'
open(path, 'w', encoding='utf-8').writelines(lines)
print('Line 115 fixed')
