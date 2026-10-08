path = r'D:\中电云\streamlit_env\intelligent-qa-agent\agent\graph.py'
lines = open(path, 'r', encoding='utf-8').readlines()
lines[132] = '\u9519\u8bef: {state.get(\"sql_error\", \"\u65e0\")}\"\n'
open(path, 'w', encoding='utf-8').writelines(lines)
print('Line 133 fixed')
