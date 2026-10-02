"""Indian brokerage and charges.

Rates follow the standard published retail slabs: STT on delivery and on equity
intraday above the exemption, exchange transaction charges, SEBI turnover fees,
GST, and stamp duty on buy side. Rates are data, not buried constants, so an
account with a different plan can override them.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class BrokeragePlan:
    """One brokerage slab.

    Exactly one pricing style applies per leg, so a customer is never charged
    twice for the same trade:

    * ``per_order`` — a flat rupee charge that *replaces* the percentage.
      Used by futures/options plans ("flat Rs 20 per executed order").
    * ``rate`` with ``cap_per_order`` — "x% or Rs N, whichever is lower".
      ``min_per_order`` instead expresses a floor ("x% or Rs N, whichever is
      higher").
    """

    name: str
    segment: str          # nse_cm, bse_cm, nse_fo, bse_fo, cde_fo, mcx_fo
    product: str          # CNC, MIS, NRML
    rate: float = 0.0
    per_order: float = 0.0
    min_per_order: float = 0.0
    cap_per_order: float = 0.0


#: Default cash delivery / intraday and F&O slabs, matching common retail plans.
DEFAULT_PLANS: list[BrokeragePlan] = [
    # Cash delivery is zero brokerage under most plans.
    BrokeragePlan("Equity delivery", "nse_cm", "CNC", rate=0.0),
    BrokeragePlan("Equity delivery", "bse_cm", "CNC", rate=0.0),
    # Intraday is "0.03% or Rs 20, whichever is lower".
    BrokeragePlan("Equity intraday", "nse_cm", "MIS", rate=0.0003, cap_per_order=20.0),
    BrokeragePlan("Equity intraday", "bse_cm", "MIS", rate=0.0003, cap_per_order=20.0),
    BrokeragePlan("Equity intraday NRML", "nse_cm", "NRML", rate=0.0003, cap_per_order=20.0),
    # Futures and options are charged a flat Rs 20 per executed order.
    BrokeragePlan("Equity futures", "nse_fo", "NRML", per_order=20.0),
    BrokeragePlan("Equity futures", "nse_fo", "MIS", per_order=20.0),
    BrokeragePlan("Equity futures", "bse_fo", "NRML", per_order=20.0),
    BrokeragePlan("Equity futures", "bse_fo", "MIS", per_order=20.0),
    BrokeragePlan("Currency futures", "cde_fo", "NRML", per_order=20.0),
    BrokeragePlan("Currency futures", "cde_fo", "MIS", per_order=20.0),
    BrokeragePlan("Commodity futures", "mcx_fo", "NRML", per_order=20.0),
    BrokeragePlan("Commodity futures", "mcx_fo", "MIS", per_order=20.0),
]

#: Regulatory and statutory rates as decimals.
STT_DELIVERY = 0.001         # 0.1% on both sides
STT_INTRADAY = 0.00025       # 0.025% on sell
STT_FUTURES = 0.0002         # 0.02% on sell, non-equity
STT_OPTIONS_PREMIUM = 0.001  # 0.1% on sell premium
EXCHANGE_TXN_EQUITY = 0.0000297
EXCHANGE_TXN_FUTURES = 0.00000173
SEBI_TXN = 0.0000001
STAMP_DUTY_BUY = 0.00003     # 0.003% on buy
STAMP_DUTY_FUTURES_BUY = 0.00002
STAMP_DUTY_OPTIONS_BUY = 0.00003
GST_RATE = 0.18

DP_CHARGE_PER_ORDER = 15.49


@dataclass
class Charges:
    brokerage: float = 0.0
    stt: float = 0.0
    exchange_txn: float = 0.0
    sebi: float = 0.0
    stamp_duty: float = 0.0
    gst: float = 0.0
    dp_charges: float = 0.0
    total: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {key: round(value, 4) for key, value in asdict(self).items()}


def brokerage_for(plans: list[BrokeragePlan], segment: str, product: str) -> BrokeragePlan:
    for plan in plans:
        if plan.segment == segment and plan.product == product:
            return plan
    for plan in plans:
        if plan.segment == segment:
            return plan
    return BrokeragePlan("Default", segment, product, rate=0.0005, cap_per_order=20.0)


def compute(
    *,
    side: str,
    segment: str,
    product: str,
    quantity: float,
    price: float,
    is_option: bool = False,
    plans: list[BrokeragePlan] | None = None,
) -> Charges:
    """Charges for one leg of a trade.

    `side` is B or S. Charges are computed per leg, so a round trip sums both.
    """
    plans = plans or DEFAULT_PLANS
    is_buy = str(side).upper() in {"B", "BUY"}
    turnover = quantity * price
    charges = Charges()

    plan = brokerage_for(plans, segment, product)
    if turnover <= 0:
        charges.brokerage = 0.0
    elif plan.per_order:
        # A flat slab: the percentage does not also apply.
        charges.brokerage = plan.per_order
    else:
        charges.brokerage = turnover * plan.rate
        if plan.cap_per_order:
            charges.brokerage = min(charges.brokerage, plan.cap_per_order)
        if plan.min_per_order:
            charges.brokerage = max(charges.brokerage, plan.min_per_order)

    if is_option:
        # Options are charged on premium turnover, not the (much larger) notional.
        charges.stt = (turnover * STT_OPTIONS_PREMIUM) if not is_buy else 0.0
        charges.exchange_txn = turnover * EXCHANGE_TXN_EQUITY
        charges.stamp_duty = turnover * STAMP_DUTY_OPTIONS_BUY if is_buy else 0.0
    elif product == "CNC":
        charges.stt = turnover * STT_DELIVERY
        charges.exchange_txn = turnover * EXCHANGE_TXN_EQUITY
        charges.stamp_duty = turnover * STAMP_DUTY_BUY if is_buy else 0.0
    elif segment in {"nse_fo", "bse_fo", "cde_fo", "mcx_fo"}:
        charges.stt = (0.0 if is_buy else turnover * STT_FUTURES)
        charges.exchange_txn = turnover * EXCHANGE_TXN_FUTURES
        charges.stamp_duty = turnover * STAMP_DUTY_FUTURES_BUY if is_buy else 0.0
    else:
        charges.stt = (0.0 if is_buy else turnover * STT_INTRADAY)
        charges.exchange_txn = turnover * EXCHANGE_TXN_EQUITY
        charges.stamp_duty = turnover * STAMP_DUTY_BUY if is_buy else 0.0

    charges.sebi = turnover * SEBI_TXN
    # GST applies to brokerage + exchange + SEBI + stamp, but not STT.
    taxable = charges.brokerage + charges.exchange_txn + charges.sebi + charges.stamp_duty
    charges.gst = taxable * GST_RATE
    # A DP charge is billed for a scrip that was actually held; a zero-value
    # order must not attract one.
    charges.dp_charges = DP_CHARGE_PER_ORDER if (product == "CNC" and turnover > 0) else 0.0
    charges.total = (
        charges.brokerage + charges.stt + charges.exchange_txn + charges.sebi
        + charges.stamp_duty + charges.gst + charges.dp_charges
    )
    return charges


def round_trip_cost(
    *, segment: str, product: str, quantity: float, price: float, is_option: bool = False
) -> float:
    """Combined buy and sell cost — what a break-even move must absorb."""
    buy = compute(side="B", segment=segment, product=product, quantity=quantity, price=price, is_option=is_option)
    sell = compute(side="S", segment=segment, product=product, quantity=quantity, price=price, is_option=is_option)
    return round(buy.total + sell.total, 4)
