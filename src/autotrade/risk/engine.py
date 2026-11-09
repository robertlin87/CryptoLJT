"""風控引擎。

手數公式（使用者指定）：
    張數 = equity × R% ÷ |建倉價 − 止損價| ÷ 合約倍數
R% = 報單「控倉維持 x% 風控」的 x；合約倍數 = 各交易所每張合約的幣數
（ccxt market['contractSize']，各所不同）；向下取整到整數張。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

log = logging.getLogger("autotrade.risk.engine")


def calc_contract_qty(
    equity: float,
    risk_pct: float,
    entry: float,
    stop: float,
    contract_size: float,
) -> int:
    """按 R% 風控與止損距離計算合約張數（向下取整）。"""
    if contract_size <= 0 or entry == stop:
        return 0
    coins = equity * (risk_pct / 100.0) / abs(entry - stop)
    return math.floor(coins / contract_size)


def check_margin_formula(
    margin_pct: float,
    risk_pct: float,
    stop_dist_pct: float,
    leverage: int,
    tol_pct: float = 0.1,
) -> bool:
    """保證金復算：margin_pct ≈ risk_pct / (stop_dist_pct × leverage)。

    只用於判斷要不要寫一行提醒（規則 2026-11-09）；超差不攔單，照常下單。
    """
    if stop_dist_pct <= 0 or leverage <= 0:
        return False
    expected = risk_pct / (stop_dist_pct * leverage) * 100.0
    return abs(margin_pct - expected) <= tol_pct


@dataclass(slots=True)
class RiskDecision:
    accepted: bool
    reason: str = ""
    qty: int = 0


def validate(
    signal,
    equity: float,
    contract_size: float,
    open_positions: list | None = None,
    risk_cfg: dict | None = None,
    analysts_cfg: dict | None = None,
) -> RiskDecision:
    """M2 風控校驗，返回 RiskDecision(accepted, reason, qty)。

    依次檢查：
      1. 分析師白名單（allow_all=false 時生效）
      2. 同 symbol 反向信號拒單
      3. 保證金復算：數字對不上只提醒、不攔單
      4. 手數 = calc_contract_qty（名義值下限已移除）

    同時持有多少幣種不設上限、單日虧損上限均已移除（規則 2026-11-09）。

    手數公式與保證金復算已實現並有測試，此處直接複用。
    open_positions 元素需含 symbol 與 side（"long"/"short"）。
    """
    risk_cfg = risk_cfg or {}
    analysts_cfg = analysts_cfg or {}
    open_positions = open_positions or []

    # 1) 分析師白名單
    if not analysts_cfg.get("allow_all", True):
        allow = analysts_cfg.get("list", [])
        if signal.analyst not in allow:
            return RiskDecision(False, f"分析師不在白名單：{signal.analyst}")

    # 2) 同 symbol 反向信號
    for pos in open_positions:
        if pos.get("symbol") == signal.symbol and pos.get("side") != signal.side:
            return RiskDecision(False, f"同 symbol 反向信號拒單：{signal.symbol}")

    # 3) 保證金復算：數字對不上只提醒，照常下單（規則 2026-11-09）
    tol = float(risk_cfg.get("margin_formula_tol_pct", 0.1))
    if not check_margin_formula(
        signal.margin_pct, signal.risk_pct, signal.stop_dist_pct, signal.leverage, tol
    ):
        log.warning(
            "報單 %s %s 保證金數字對不上（保證金 %s%%，風控行算出 %.4f%%，容差 %s%%），"
            "僅提醒，照常下單",
            signal.order_id, signal.symbol, signal.margin_pct,
            (signal.risk_pct / (signal.stop_dist_pct * signal.leverage) * 100.0
             if signal.stop_dist_pct > 0 and signal.leverage > 0 else float("nan")),
            tol,
        )

    # 4) 手數（規則 2026-11-09：移除程式自訂的名義值下限，交易所自己會把關）
    qty = calc_contract_qty(equity, signal.risk_pct, signal.entry, signal.sl, contract_size)
    # qty=0 不在這裡拒單：按使用者規則「手數低於系統最低手數就按系統最低手數
    # 下單」，0 也屬於低於，交給 TriggerRouter 的 system_min 託底。
    # 若交易所沒給任何下限（system_min 也是 0），執行層會跳過下單並告警。

    return RiskDecision(True, "", qty)
