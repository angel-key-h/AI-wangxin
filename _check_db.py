import sqlite3
for f in ['data/chinook.db', 'data/chinook.sqlite']:
    conn = sqlite3.connect(f)
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cursor.fetchall()]
    print(f'{f}: {len(tables)} tables -> {tables}')
    conn.close()
