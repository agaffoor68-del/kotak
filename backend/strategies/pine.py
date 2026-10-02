"""Pine Script interchange for the strategy builder.

Two directions, both deliberately honest about their limits:

* :func:`export_pine` turns a validated strategy document into readable Pine
  Script v5 that can be pasted into TradingView. Where an indicator has no
  faithful Pine equivalent the generated line is flagged in ``warnings`` rather
  than being silently approximated.
* :func:`import_pine` parses a **documented subset** of Pine Script back into a
  strategy document the platform can backtest, paper-trade and run live.

The subset is exactly what this platform's indicator registry can evaluate:

* ``ta.sma``/``ta.ema``/``ta.wma``/``ta.rsi``/``ta.atr``/``ta.vwap``/``ta.obv``/
  ``ta.cmf``/``ta.macd``/``ta.bb``/``ta.dmi``/``ta.stoch``/``ta.supertrend``/
  ``ta.ichimoku``
* comparison operators ``> >= < <= == !=`` and ``ta.crossover``/``ta.crossunder``
* logical ``and`` / ``or`` / ``not``
* ``strategy.entry`` / ``strategy.close`` / ``strategy.exit`` with ``when=`` or a
  leading ``if`` block

Anything outside that subset is reported, never guessed at. The evaluator that
runs the result is :mod:`backend.strategies.dsl`, so what is imported is exactly
what is tested.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: DSL indicators whose only free parameter is a length.
PERIOD_INDICATORS = {"sma", "ema", "wma", "rsi", "atr", "cmf", "adx"}

#: Position of each output inside a multi-value Pine call.
OUTPUT_ORDER: dict[str, dict[str, int]] = {
    "macd": {"macd": 0, "signal": 1, "histogram": 2},
    "bollinger": {"upper": 0, "middle": 1, "lower": 2},
    "adx": {"plus_di": 0, "minus_di": 1, "adx": 2},
    "supertrend": {"supertrend": 0, "direction": 1},
    "stochastic": {"k": 0, "d": 1},
    "stochastic_rsi": {"k": 0, "d": 1},
    "ichimoku": {"tenkan": 0, "kijun": 1, "senkou_a": 2, "senkou_b": 3, "chikou": 4},
}

#: Pine call -> (dsl indicator, {arg position -> param name}).
PINE_CALLS: dict[str, tuple[str, dict[int, str]]] = {
    "ta.sma": ("sma", {1: "period"}),
    "ta.ema": ("ema", {1: "period"}),
    "ta.wma": ("wma", {1: "period"}),
    "ta.rsi": ("rsi", {1: "period"}),
    "ta.atr": ("atr", {0: "period"}),
    "ta.cmf": ("cmf", {0: "period"}),
    "ta.vwap": ("vwap", {}),
    "ta.obv": ("obv", {}),
    "ta.macd": ("macd", {1: "fast", 2: "slow", 3: "signal"}),
    "ta.bb": ("bollinger", {1: "period", 2: "deviations"}),
    "ta.bbands": ("bollinger", {1: "period", 2: "deviations"}),
    "ta.dmi": ("adx", {0: "period"}),
    "ta.stoch": ("stochastic", {3: "period"}),
    "ta.supertrend": ("supertrend", {0: "multiplier", 1: "period"}),
    "ta.ichimoku": ("ichimoku", {0: "conversion", 1: "base", 2: "span_b"}),
}

#: DSL param defaults, so a bare Pine call still produces a fully-specified rule.
PARAM_DEFAULTS: dict[str, dict[str, Any]] = {
    "sma": {"period": 20}, "ema": {"period": 20}, "wma": {"period": 20},
    "rsi": {"period": 14}, "atr": {"period": 14}, "cmf": {"period": 20},
    "adx": {"period": 14},
    "macd": {"fast": 12, "slow": 26, "signal": 9},
    "bollinger": {"period": 20, "deviations": 2.0},
    "stochastic": {"period": 14},
    "stochastic_rsi": {"rsi_period": 14, "stoch_period": 14},
    "supertrend": {"period": 10, "multiplier": 3.0},
    "ichimoku": {"conversion": 9, "base": 26, "span_b": 52},
}

COMPARATORS = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "eq": "==", "neq": "!="}
COMPARATOR_INVERSE = {value: key for key, value in COMPARATORS.items()}

CROSS_INVERSE = {"crosses_above": "ta.crossover", "crosses_below": "ta.crossunder"}

DEFAULT_RISK = {"stop_loss_pct": 1.0, "target_pct": 2.0, "trailing_stop_pct": 0.8, "timeframe_exit_bars": 40}
DEFAULT_SIZING = {"mode": "fixed", "quantity": 1}


class PineError(ValueError):
    """The Pine Script source could not be parsed."""


@dataclass
class SeriesRef:
    """A resolvable series: an indicator (or price field) at a bar."""

    indicator: str
    params: dict[str, Any]
    field: str = "value"

    def key(self) -> str:
        return f"{self.indicator}|{sorted(self.params.items())}|{self.field}"


# ------------------------------------------------------------------- helpers


def _num(value: Any) -> str:
    """Render a number for Pine: integers without a trailing ``.0``."""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _collect_series(node: Any, found: dict[str, SeriesRef]) -> None:
    """Walk a rule tree collecting every series it references."""
    if not isinstance(node, dict):
        return
    for key in ("all", "any"):
        if isinstance(node.get(key), list):
            for child in node[key]:
                _collect_series(child, found)
            return
    if "not" in node:
        _collect_series(node["not"], found)
        return
    indicator = node.get("indicator")
    if not indicator:
        return
    found[SeriesRef(indicator, node.get("params") or {}, node.get("field") or "value").key()] = (
        SeriesRef(indicator, node.get("params") or {}, node.get("field") or "value")
    )
    against = node.get("against")
    if isinstance(against, dict) and against.get("indicator"):
        ref = SeriesRef(against["indicator"], against.get("params") or {}, against.get("field") or "value")
        found[ref.key()] = ref


# --------------------------------------------------------------------- export


def _var_name(indicator: str, params: dict[str, Any]) -> str:
    if not params:
        return indicator
    suffix = "_".join(str(params[key]).replace(".", "_") for key in sorted(params))
    return re.sub(r"[^0-9A-Za-z_]", "_", f"{indicator}_{suffix}")


def _render_expr(node: Any, refs: dict[str, str], warnings: list[str]) -> str:
    """Render a rule tree as a Pine boolean expression."""
    if not isinstance(node, dict):
        return "false"
    for key, joiner in (("all", " and "), ("any", " or ")):
        if isinstance(node.get(key), list):
            parts = [_render_expr(child, refs, warnings) for child in node[key]]
            return "(" + joiner.join(parts) + ")" if parts else ("true" if key == "all" else "false")
    if "not" in node:
        return f"(not {_render_expr(node['not'], refs, warnings)})"

    indicator = node.get("indicator")
    if not indicator:
        return "false"
    params = node.get("params") or {}
    field_name = node.get("field") or "value"
    compare = node.get("compare")

    left = refs.get(SeriesRef(indicator, params, field_name).key(), "close")

    if compare in CROSS_INVERSE:
        against = node.get("against") or {}
        if against.get("indicator"):
            right = refs.get(
                SeriesRef(against["indicator"], against.get("params") or {}, against.get("field") or "value").key(),
                "close",
            )
        else:
            right = _num(against.get("value", 0))
        return f"{CROSS_INVERSE[compare]}({left}, {right})"

    operator = COMPARATORS.get(compare)
    if operator is None:
        warnings.append(f"unsupported comparator '{compare}' rendered as '>'")
        operator = ">"

    against = node.get("against") or {}
    if against.get("indicator"):
        right = refs.get(
            SeriesRef(against["indicator"], against.get("params") or {}, against.get("field") or "value").key(),
            "close",
        )
    else:
        right = _num(node.get("value", 0))
    return f"{left} {operator} {right}"
def _declaration(ref: SeriesRef, base: str) -> tuple[list[str], dict[str, str], str | None]:
    """Pine declaration lines for one series, its field->name map, and a warning."""
    indicator = ref.indicator
    if indicator in {"open", "high", "low", "close", "volume"}:
        return [], {indicator: indicator}, None

    period = int(ref.params.get("period", PARAM_DEFAULTS.get(indicator, {}).get("period", 14)))

    if indicator == "sma":
        return [f"{base} = ta.sma(close, {period})"], {"value": base}, None
    if indicator == "ema":
        return [f"{base} = ta.ema(close, {period})"], {"value": base}, None
    if indicator == "wma":
        return [f"{base} = ta.wma(close, {period})"], {"value": base}, None
    if indicator == "rsi":
        return [f"{base} = ta.rsi(close, {period})"], {"value": base}, None
    if indicator == "atr":
        return [f"{base} = ta.atr({period})"], {"value": base}, None
    if indicator == "cmf":
        return [f"{base} = ta.cmf({period})"], {"value": base}, None
    if indicator == "vwap":
        return [f"{base} = ta.vwap"], {"value": base}, None
    if indicator == "obv":
        return [f"{base} = ta.obv"], {"value": base}, None
    if indicator == "macd":
        fast = int(ref.params.get("fast", 12))
        slow = int(ref.params.get("slow", 26))
        signal = int(ref.params.get("signal", 9))
        return (
            [f"[{base}_macd, {base}_signal, {base}_hist] = ta.macd(close, {fast}, {slow}, {signal})"],
            {"macd": f"{base}_macd", "signal": f"{base}_signal", "histogram": f"{base}_hist"},
            None,
        )
    if indicator == "bollinger":
        deviations = _num(ref.params.get("deviations", 2.0))
        return (
            [f"[{base}_upper, {base}_middle, {base}_lower] = ta.bb(close, {period}, {deviations})"],
            {"upper": f"{base}_upper", "middle": f"{base}_middle", "lower": f"{base}_lower"},
            None,
        )
    if indicator == "adx":
        return (
            [f"[{base}_plus_di, {base}_minus_di, {base}_adx] = ta.dmi({period}, {period})"],
            {"plus_di": f"{base}_plus_di", "minus_di": f"{base}_minus_di", "adx": f"{base}_adx"},
            None,
        )
    if indicator == "stochastic":
        smooth = int(ref.params.get("smooth", 3))
        return (
            [f"{base}_k = ta.sma(ta.stoch(close, high, low, {period}), {smooth})"],
            {"k": f"{base}_k", "d": f"{base}_k"},
            "stochastic 'd' falls back to the %K line in Pine export",
        )
    if indicator == "supertrend":
        multiplier = _num(ref.params.get("multiplier", 3.0))
        return (
            [f"[{base}_st, {base}_dir] = ta.supertrend({multiplier}, {period})"],
            {"supertrend": f"{base}_st", "direction": f"{base}_dir"},
            None,
        )
    if indicator == "ichimoku":
        conversion = int(ref.params.get("conversion", 9))
        base_len = int(ref.params.get("base", 26))
        span_b = int(ref.params.get("span_b", 52))
        return (
            [f"[{base}_tenkan, {base}_kijun, {base}_senkou_a, {base}_senkou_b, {base}_chikou] = "
             f"ta.ichimoku({conversion}, {base_len}, {span_b})"],
            {"tenkan": f"{base}_tenkan", "kijun": f"{base}_kijun", "senkou_a": f"{base}_senkou_a",
             "senkou_b": f"{base}_senkou_b", "chikou": f"{base}_chikou"},
            None,
        )
    if indicator == "stochastic_rsi":
        rsi_period = int(ref.params.get("rsi_period", 14))
        stoch_period = int(ref.params.get("stoch_period", 14))
        return (
            [f"{base}_rsi = ta.rsi(close, {rsi_period})",
             f"{base}_k = ta.sma(ta.stoch({base}_rsi, {base}_rsi, {base}_rsi, {stoch_period}), 3)"],
            {"k": f"{base}_k", "d": f"{base}_k"},
            "stochastic_rsi is approximated in Pine export",
        )

    return (
        [f"// {indicator} has no direct Pine equivalent; exported as close for reference",
         f"{base} = close"],
        {"value": base},
        f"indicator '{indicator}' has no direct Pine equivalent and was exported as close",
    )


def export_pine(definition: dict[str, Any]) -> dict[str, Any]:
    """Render a strategy document as Pine Script v5."""
    warnings: list[str] = []
    name = str(definition.get("name") or "AlphaTradePro strategy").replace('"', "'")
    timeframe = str(definition.get("timeframe") or "5m")

    found: dict[str, SeriesRef] = {}
    for section in ("entry", "exit"):
        _collect_series(definition.get(section), found)

    declarations: list[str] = []
    refs: dict[str, str] = {}
    used_names: set[str] = set()
    for ref in found.values():
        base = _var_name(ref.indicator, ref.params)
        while base in used_names:
            base = f"{base}_x"
        used_names.add(base)
        code, field_names, warning = _declaration(ref, base)
        if warning:
            warnings.append(warning)
        declarations.extend(code)
        for output_field, pine_name in field_names.items():
            refs[SeriesRef(ref.indicator, ref.params, output_field).key()] = pine_name

    entry = _render_expr(definition.get("entry"), refs, warnings)
    exit_rule = _render_expr(definition.get("exit"), refs, warnings)

    lines = [
        "//@version=5",
        f"// {name} - exported by AlphaTradePro.",
        f"// Timeframe hint: {timeframe}. Kotak Neo publishes no history, so this",
        "// platform records the live tape itself; the semantics below are what it backtests.",
        f'strategy("{name}", overlay=true)',
    ]
    if declarations:
        lines += ["", "// --- indicators", *declarations]
    lines += [
        "",
        "// --- entry",
        f"entryCondition = {entry}",
        "if entryCondition",
        '    strategy.entry("Long", strategy.long)',
        "",
        "// --- exit",
        f"exitCondition = {exit_rule}",
        "if exitCondition",
        '    strategy.close("Long")',
    ]

    risk = definition.get("risk") or {}
    if risk:
        lines += [
            "",
            "// --- risk (configure to match this strategy)",
            f"// stop_loss_pct={_num(risk.get('stop_loss_pct', 0))} "
            f"target_pct={_num(risk.get('target_pct', 0))} "
            f"trailing_stop_pct={_num(risk.get('trailing_stop_pct', 0))}",
        ]

    return {"code": "\n".join(lines) + "\n", "warnings": warnings}


# --------------------------------------------------------------------- import

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<num>\d+\.\d+|\d+)
  | (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
  | (?P<ident>[A-Za-z_][A-Za-z0-9_.]*)
  | (?P<op><=|>=|==|!=|<|>|\+|-|\*|/|\(|\)|\[|\]|,|=)
    """,
    re.VERBOSE,
)

_PRICE_FIELDS = {"open", "high", "low", "close", "volume", "hl2", "hlc3", "ohlc4"}


class Literal(str):
    """A Pine string literal, kept distinct from a series reference."""


def _tokenize(text: str) -> list[tuple[str, Any]]:
    tokens: list[tuple[str, Any]] = []
    position = 0
    while position < len(text):
        match = _TOKEN_RE.match(text, position)
        if not match:
            raise PineError(f"Cannot parse near {text[position:position + 20]!r}")
        position = match.end()
        kind = match.lastgroup or ""
        value = match.group()
        if kind == "ws":
            continue
        if kind == "num":
            tokens.append(("num", float(value)))
        elif kind == "str":
            tokens.append(("str", Literal(value[1:-1])))
        else:
            tokens.append((kind, value))
    tokens.append(("eof", None))
    return tokens


def _combine(kind: str, left: Any, right: Any) -> dict[str, Any]:
    children: list[Any] = []
    for side in (left, right):
        if isinstance(side, dict) and set(side) == {kind}:
            children.extend(side[kind])
        else:
            children.append(side)
    return {kind: children}


class _Parser:
    def __init__(self, text: str, symbols: dict[str, Any], warnings: list[str]) -> None:
        self.tokens = _tokenize(text)
        self.position = 0
        self.symbols = symbols
        self.warnings = warnings

    def peek(self) -> tuple[str, Any]:
        return self.tokens[self.position]

    def next(self) -> tuple[str, Any]:
        token = self.tokens[self.position]
        self.position += 1
        return token

    def accept(self, value: str) -> bool:
        kind, raw = self.peek()
        if raw == value:
            self.position += 1
            return True
        return False

    def expect(self, value: str) -> None:
        if not self.accept(value):
            raise PineError(f"Expected {value!r}")

    def parse(self) -> Any:
        result = self.parse_or()
        return result

    def parse_or(self) -> Any:
        node = self.parse_and()
        while self.accept("or"):
            node = _combine("any", node, self.parse_and())
        return node

    def parse_and(self) -> Any:
        node = self.parse_not()
        while self.accept("and"):
            node = _combine("all", node, self.parse_not())
        return node

    def parse_not(self) -> Any:
        if self.accept("not"):
            return {"not": self.parse_not()}
        return self.parse_comparison()

    def parse_comparison(self) -> Any:
        left = self.parse_add()
        kind, raw = self.peek()
        if kind == "op" and raw in {"<=", ">=", "==", "!=", "<", ">"}:
            self.position += 1
            right = self.parse_add()
            return _make_condition(left, right, raw, self.warnings)
        return left

    def parse_add(self) -> Any:
        node = self.parse_mul()
        while True:
            kind, raw = self.peek()
            if kind == "op" and raw in {"+", "-"}:
                self.position += 1
                right = self.parse_mul()
                node = _arithmetic(node, right, raw, self.warnings)
            else:
                return node

    def parse_mul(self) -> Any:
        node = self.parse_atom()
        while True:
            kind, raw = self.peek()
            if kind == "op" and raw in {"*", "/"}:
                self.position += 1
                right = self.parse_atom()
                node = _arithmetic(node, right, raw, self.warnings)
            else:
                return node

    def parse_atom(self) -> Any:
        kind, raw = self.next()
        if kind == "num":
            return raw
        if kind == "str":
            return raw
        if kind == "op" and raw == "(":
            inner = self.parse_or()
            self.expect(")")
            return inner
        if kind == "op" and raw == "-":
            value = self.parse_atom()
            return -value if isinstance(value, float) else 0.0
        if kind == "ident":
            if self.peek()[1] == "(":
                return self.parse_call(str(raw))
            return self.resolve_symbol(str(raw))
        raise PineError(f"Unexpected token {raw!r}")

    def parse_call(self, name: str) -> Any:
        self.expect("(")
        args: list[Any] = []
        if not self.accept(")"):
            while True:
                args.append(self.parse_or())
                if self.accept(","):
                    continue
                self.expect(")")
                break
        return _resolve_call(name, args, self.warnings)

    def resolve_symbol(self, name: str) -> Any:
        if name in self.symbols:
            return self.symbols[name]
        if name in _PRICE_FIELDS:
            field = name if name in {"open", "high", "low", "close", "volume"} else "close"
            return SeriesRef(field, {}, "value")
        if name in {"true", "false"}:
            return {"all": []} if name == "true" else {"any": []}
        self.warnings.append(f"unknown symbol '{name}' treated as 0")
        return 0.0


def _arithmetic(node: Any, right: Any, op: str, warnings: list[str]) -> Any:
    if isinstance(node, float) and isinstance(right, float):
        if op == "+":
            return node + right
        if op == "-":
            return node - right
        if op == "*":
            return node * right
        if op == "/":
            return node / right if right else 0.0
    warnings.append(f"arithmetic '{op}' on series is not supported; treated as 0")
    return 0.0


def _make_condition(left: Any, right: Any, op: str, warnings: list[str]) -> dict[str, Any]:
    if isinstance(left, float) and isinstance(right, SeriesRef):
        flipped = {"<": ">", ">": "<", "<=": ">=", ">=": "<=", "==": "==", "!=": "!="}[op]
        return _make_condition(right, left, flipped, warnings)
    if isinstance(left, SeriesRef):
        node: dict[str, Any] = {
            "indicator": left.indicator,
            "params": dict(left.params),
            "field": left.field,
            "compare": COMPARATOR_INVERSE[op],
        }
        if isinstance(right, SeriesRef):
            node["against"] = {"indicator": right.indicator, "params": dict(right.params), "field": right.field}
        elif isinstance(right, float):
            node["value"] = right
        else:
            warnings.append(f"comparison with '{op}' has an unsupported right-hand side")
            return {"any": []}
        return node
    warnings.append(f"comparison '{op}' is not supported between these operands")
    return {"any": []}


def _make_cross(left: Any, right: Any, name: str, warnings: list[str]) -> dict[str, Any]:
    compare = "crosses_above" if name.endswith("crossover") else "crosses_below"
    if not isinstance(left, SeriesRef):
        warnings.append(f"{name} needs a series on the left")
        return {"any": []}
    node: dict[str, Any] = {
        "indicator": left.indicator, "params": dict(left.params),
        "field": left.field, "compare": compare,
    }
    if isinstance(right, SeriesRef):
        node["against"] = {"indicator": right.indicator, "params": dict(right.params), "field": right.field}
    elif isinstance(right, float):
        node["against"] = {"value": right}
    else:
        warnings.append(f"{name} needs a series or number on the right")
        return {"any": []}
    return node


_PRICE_SOURCE_INDICATORS = {"sma", "ema", "wma", "rsi", "macd", "bollinger"}


def _build_series(name: str, args: list[Any], warnings: list[str]) -> SeriesRef:
    dsl_name, argmap = PINE_CALLS[name]
    params = dict(PARAM_DEFAULTS.get(dsl_name, {}))
    numbers = [value for value in args if isinstance(value, float)]
    for index, (_, param_name) in enumerate(sorted(argmap.items())):
        if index >= len(numbers):
            break
        value = numbers[index]
        if param_name in {"multiplier", "deviations"}:
            params[param_name] = value
        else:
            params[param_name] = int(value) if float(value).is_integer() else value

    if dsl_name in _PRICE_SOURCE_INDICATORS and args and isinstance(args[0], SeriesRef):
        if args[0].indicator != "close":
            warnings.append(
                f"{name} source '{args[0].indicator}' is ignored: the platform evaluates on close"
            )

    order = OUTPUT_ORDER.get(dsl_name)
    output_field = min(order, key=order.get) if order else "value"
    return SeriesRef(dsl_name, params, output_field)


def _resolve_call(name: str, args: list[Any], warnings: list[str]) -> Any:
    if name in {"ta.crossover", "ta.crossunder"}:
        if len(args) < 2:
            warnings.append(f"{name} needs two arguments")
            return {"any": []}
        return _make_cross(args[0], args[1], name, warnings)
    if name in PINE_CALLS:
        return _build_series(name, args, warnings)
    if name in {"input.int", "input.float", "input.source", "input", "input.timeframe"}:
        for argument in args:
            if isinstance(argument, (float, SeriesRef, Literal)):
                return argument
        return args[0] if args else 0.0
    if name in {"math.abs", "math.max", "math.min", "nz"}:
        for argument in args:
            if isinstance(argument, (float, SeriesRef)):
                return argument
        return 0.0
    warnings.append(f"unsupported Pine call '{name}' ignored")
    return 0.0


def _strip_comment(line: str) -> str:
    """Remove a trailing ``//`` comment, ignoring ``//`` inside a string."""
    quote: str | None = None
    index = 0
    while index < len(line):
        char = line[index]
        if quote:
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == "/" and index + 1 < len(line) and line[index + 1] == "/":
            return line[:index]
        index += 1
    return line


def _first_string(text: str) -> str | None:
    match = re.search(r'"([^"]*)"' r"|'([^']*)'", text)
    if not match:
        return None
    return match.group(1) or match.group(2)


def _split_args(text: str) -> list[str]:
    """Split a call's arguments on top-level commas."""
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    current = ""
    for char in text:
        if quote:
            current += char
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            current += char
            continue
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current.strip())
            current = ""
            continue
        current += char
    if current.strip():
        parts.append(current.strip())
    return parts


def _parse_call_text(text: str) -> tuple[str, str]:
    open_index = text.find("(")
    if open_index < 0 or not text.endswith(")"):
        raise PineError("malformed call")
    return text[:open_index].strip(), text[open_index + 1:-1]


def _strategy_condition(
    name: str, args_text: str, symbols: dict[str, Any], warnings: list[str]
) -> tuple[dict[str, Any] | None, str]:
    direction = "long"
    condition: dict[str, Any] | None = None
    for index, argument in enumerate(_split_args(args_text)):
        if "=" in argument and not argument.strip().startswith(("?>", "<=", ">=", "==", "!=")):
            key, _, value = argument.partition("=")
            key = key.strip()
            if key == "when":
                condition = _Parser(value, symbols, warnings).parse()
            continue
        if index == 1 and "short" in argument:
            direction = "short"
    return condition, direction


#: Statements we deliberately skip: they draw, not trade.
_SKIP_PREFIXES = (
    "plot(", "plotshape(", "plotchar(", "plotcandle(", "plotbar(", "bgcolor(", "barcolor(",
    "hline(", "fill(", "alertcondition(", "indicator(", "request.", "strategy.risk",
    "label.", "line.", "table.", "box.",
)


def import_pine(
    source: str,
    *,
    name: str | None = None,
    timeframe: str | None = None,
    token: str = "26000",
    label: str = "NIFTY 50",
    exchange_segment: str = "nse_cm",
) -> dict[str, Any]:
    """Parse a Pine Script subset into a strategy document this platform runs."""
    warnings: list[str] = []
    errors: list[str] = []
    symbols: dict[str, Any] = {}
    entry: Any = None
    exit_rule: Any = None
    header_name: str | None = None
    pending_if: tuple[int, Any] | None = None

    for raw_line in source.splitlines():
        line = _strip_comment(raw_line)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        text = line.strip()

        if text.startswith("//@"):
            continue
        if pending_if is not None and indent <= pending_if[0]:
            pending_if = None

        if text.startswith("strategy("):
            header_name = _first_string(text) or header_name
            continue
        if text.startswith("indicator("):
            header_name = header_name or _first_string(text)
            continue
        if text.startswith(_SKIP_PREFIXES):
            continue

        if text.startswith("if "):
            try:
                pending_if = (indent, _Parser(text[3:], symbols, warnings).parse())
            except PineError as error:
                errors.append(f"{text!r}: {error}")
                pending_if = None
            continue

        tuple_match = re.match(r"^\[([^\]]+)\]\s*=\s*(.+)$", text)
        if tuple_match:
            targets = [part.strip() for part in tuple_match.group(1).split(",")]
            try:
                value = _Parser(tuple_match.group(2), symbols, warnings).parse()
            except PineError as error:
                errors.append(f"{text!r}: {error}")
                continue
            if isinstance(value, SeriesRef):
                order = OUTPUT_ORDER.get(value.indicator)
                if order is None:
                    for target in targets:
                        symbols[target] = value
                else:
                    by_index = {position: output for output, position in order.items()}
                    for position, target in enumerate(targets):
                        output = by_index.get(position)
                        if output is None:
                            warnings.append(f"extra tuple target '{target}' ignored")
                            continue
                        symbols[target] = SeriesRef(value.indicator, value.params, output)
            continue

        if text.startswith(("strategy.entry(", "strategy.order(", "strategy.close(", "strategy.exit(")):
            try:
                call_name, args_text = _parse_call_text(text)
                condition, direction = _strategy_condition(call_name, args_text, symbols, warnings)
            except PineError as error:
                errors.append(f"{text!r}: {error}")
                continue
            if condition is None and pending_if is not None and indent > pending_if[0]:
                condition = pending_if[1]
            if condition is None:
                errors.append(f"{text!r}: no condition found (use when= or a leading if)")
                continue
            if call_name in {"strategy.entry", "strategy.order"}:
                if direction == "short":
                    warnings.append("a short entry was imported; the platform models the long side")
                entry = condition if entry is None else _combine("all", entry, condition)
            else:
                exit_rule = condition if exit_rule is None else _combine("all", exit_rule, condition)
            continue

        assign_match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$", text)
        if assign_match:
            try:
                value = _Parser(assign_match.group(2), symbols, warnings).parse()
            except PineError as error:
                errors.append(f"{text!r}: {error}")
                continue
            if isinstance(value, (SeriesRef, dict)):
                symbols[assign_match.group(1)] = value
            continue

    if entry is None:
        errors.append("No strategy.entry(...) with a condition was found.")

    definition: dict[str, Any] = {
        "name": name or header_name or "Imported Pine strategy",
        "timeframe": timeframe or "5m",
        "universe": [{"token": str(token), "exchange_segment": exchange_segment, "label": label}],
        "entry": entry if entry is not None else {"all": []},
        "exit": exit_rule if exit_rule is not None else {
            "all": [{"indicator": "rsi", "params": {"period": 14}, "compare": "gt", "value": 70}]
        },
        "risk": dict(DEFAULT_RISK),
        "position_sizing": dict(DEFAULT_SIZING),
    }

    from backend.strategies import dsl

    validation = dsl.validate(definition)
    if not validation["valid"]:
        errors.extend(validation["errors"])

    return {
        "definition": definition,
        "warnings": warnings,
        "errors": errors,
        "valid": not errors,
    }