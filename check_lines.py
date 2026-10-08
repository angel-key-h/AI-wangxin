path = r'D:\中电云\streamlit_env\intelligent-qa-agent\agent\graph.py'
with open(path, 'r', encoding='utf-8') as f:
    for i, line in enumerate(f, 1):
        if 110 <= i <= 120:
            print(f'{i}: {line!r}')
