path = r'D:\中电云\streamlit_env\intelligent-qa-agent\agent\graph.py'
lines = open(path, 'r', encoding='utf-8').readlines()
lines[64] = '                "trace": [f"\U0001f4ce [\u6587\u6863\u68c0\u7d22] \u2192 \u547d\u4e2d {len(chunks)} \u4e2a\u7247\u6bb5\uff0c\u6765\u81ea {len(sources)} \u4e2a\u6587\u6863"],\n'
open(path, 'w', encoding='utf-8').writelines(lines)
print('done')
