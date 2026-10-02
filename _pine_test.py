"""Ad-hoc check for backend/strategies/pine.py (not part of CI)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.strategies import pine
from backend.strategies.templates import TEMPLATES

print("=== EXPORT (template: %s) ===" % TEMPLATES[0]["name"])
exported = pine.export_pine(TEMPLATES[0]["definition"])
print(exported["code"])
print("warnings:", exported["warnings"])

SAMPLE = '''
//@version=5
strategy("EMA cross", overlay=true)

fast = ta.ema(close, 20)
slow = ta.ema(close, 50)
[macdLine, signalLine, histLine] = ta.macd(close, 12, 26, 9)
r = ta.rsi(close, 14)

longCondition = ta.crossover(fast, slow) and r < 70
if longCondition
    strategy.entry("Long", strategy.long)

if ta.crossunder(fast, slow) or r > 75
    strategy.close("Long")

plot(fast)
'''

print("=== IMPORT (sample) ===")
imported = pine.import_pine(SAMPLE, name="From Pine")
print("valid:", imported["valid"])
print("errors:", imported["errors"])
print("warnings:", imported["warnings"])
print(json.dumps(imported["definition"], indent=2))

print("=== ROUND TRIP (import -> export) ===")
print(pine.export_pine(imported["definition"])["code"])
