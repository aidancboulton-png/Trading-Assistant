"""Kelly Criterion position sizer + risk-limit checks.

Kelly:
    f* = (b * p - q) / b
where p = win prob, q = 1-p, b = net odds (payout / stake).
For binary prediction-market contract priced at `price` in [0,1]:
    payout per $1 staked = (1/price) - 1   (you stake `price`, win $1)
    => b = (1 - price) / price
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Literal

from . import storage


@dataclass
class KellyResult:
    edge: float            # b*p - q       (in odds-ratio units)
    win_prob: float        # p
    full_kelly: float      # fraction of bankroll
    half_kelly: float      # safer
    formula: str
    suggested_size_usd: float


def kelly_pm(true_prob: float, market_price: float, bankroll_usd: float,
             fraction: float = 0.5) -> KellyResult:
    """Prediction-market Kelly. `market_price` is YES price in [0,1]."""
    p = max(0.0, min(1.0, true_prob))
    q = 1.0 - p
    if market_price <= 0 or market_price >= 1:
        raise ValueError("market_price must be in (0,1)")
    b = (1.0 - market_price) / market_price
    full = (b * p - q) / b
    full = max(0.0, full)
    half = full * fraction
    edge = b * p - q
    formula = f"f* = ({b:.2f} * {p:.2f} - {q:.2f}) / {b:.2f} = {full*100:.1f}%"
    return KellyResult(
        edge=edge,
        win_prob=p,
        full_kelly=full,
        half_kelly=half,
        formula=formula,
        suggested_size_usd=round(bankroll_usd * half, 2),
    )


def kelly_equity(expected_return: float, std_dev: float,
                 bankroll_usd: float, fraction: float = 0.5) -> KellyResult:
    """Continuous Kelly for directional equity bets:
       f* = expected_return / variance
       Caller passes expected_return (e.g. 0.02 = 2%) and std_dev (e.g. 0.15)."""
    var = max(1e-9, std_dev ** 2)
    full = max(0.0, expected_return / var)
    full = min(full, 1.0)
    half = full * fraction
    return KellyResult(
        edge=expected_return,
        win_prob=0.5 + expected_return,  # nominal
        full_kelly=full,
        half_kelly=half,
        formula=f"f* = {expected_return:.3f} / {var:.4f} = {full*100:.1f}%",
        suggested_size_usd=round(bankroll_usd * half, 2),
    )


# ---- risk limit gate -----------------------------------------------------

@dataclass
class RiskCheck:
    approved: bool
    reasons: list[str]
    sized_usd: float
    sized_pct: float


def check_limits(suggested_usd: float, bankroll_usd: float, limits: dict,
                 edge_pct: float | None = None) -> RiskCheck:
    reasons: list[str] = []
    cap_pct = limits.get("max_position_pct", 0.05)
    min_edge = limits.get("min_edge_pct", 0.05)
    max_open = limits.get("max_open_positions", 10)
    max_dd = limits.get("max_daily_drawdown_pct", 0.10)

    sized = suggested_usd
    if sized / max(bankroll_usd, 1) > cap_pct:
        sized = bankroll_usd * cap_pct
        reasons.append(f"position capped at {cap_pct*100:.1f}% of bankroll")

    if edge_pct is not None and edge_pct < min_edge:
        return RiskCheck(False,
            [f"edge {edge_pct*100:.1f}% < min {min_edge*100:.1f}%"],
            0.0, 0.0)

    open_n = len(storage.open_predictions())
    if open_n >= max_open:
        return RiskCheck(False,
            [f"max open positions reached ({open_n}/{max_open})"], 0.0, 0.0)

    dd = -storage.daily_pnl() / max(bankroll_usd, 1)
    if dd >= max_dd:
        return RiskCheck(False,
            [f"daily drawdown {dd*100:.1f}% >= cap {max_dd*100:.1f}%"], 0.0, 0.0)

    return RiskCheck(True, reasons, round(sized, 2), sized / bankroll_usd)
