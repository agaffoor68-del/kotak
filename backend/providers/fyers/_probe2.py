import ast
path = "c:/Users/SEMISTAR/Downloads/kotak/backend/providers/fyers/feed.py"
src = open(path, encoding="utf-8").read()
print("chars:", len(src))
print("lines:", src.count("\n") + 1)
try:
    tree = ast.parse(src)
    print("AST OK, defs:", [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef))])
except SyntaxError as e:
    print("SYNTAX ERROR:", e)
    lines = src.splitlines()
    for i in range(max(0, e.lineno - 6), min(len(lines), (e.lineno or 1) + 2)):
        print(f"{i+1:4d}| {lines[i]}")
