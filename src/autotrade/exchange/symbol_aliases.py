"""幣種別名：同一個幣在不同交易所／現貨與永續市場掛不同名字時的雙向兜底。

分析師報單只會寫一個名字（例：PLAY / PLAYSOUT、PUMP / PUMPFUN），
但各家交易所上架的合約名可能不同。動作前先解析一次：報單原名優先，
查不到才依序試同組別名，讓進場、全平、改單、撤單都用同一個正確幣種。

與 bybit.py 的 _SYMBOL_ALIASES 並存：那邊是單向的現貨名→永續名映射，
這裡負責其餘交易所與「反過來報」的場景。
"""
from __future__ import annotations

import logging

log = logging.getLogger("autotrade.exchange.symbol_aliases")

# 每組為同一個幣的不同名字（不含 USDT 後綴）。加新幣只需在這裡加一組。
_ALIAS_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"PUMP", "PUMPFUN"}),
    frozenset({"PLAY", "PLAYSOUT"}),
)


def alias_candidates(symbol: str) -> list[str]:
    """回傳嘗試順序：報單原名第一，其後為同組別名；無別名時只回原名。"""
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    for group in _ALIAS_GROUPS:
        if base in group:
            others = sorted(group - {base})
            return [symbol] + [f"{o}USDT" for o in others]
    return [symbol]


async def resolve_symbol(adapter, symbol: str) -> str:
    """解析成該交易所實際存在的幣種名。

    只把 ValueError（各家適配器「無此交易對／已下架」）視為找不到；
    網路錯誤等其它異常原樣外拋，不靜默換幣掩蓋故障。
    全部候選都找不到時回傳原名，讓原本的錯誤處理流程接手。
    """
    candidates = alias_candidates(symbol)
    if len(candidates) == 1:
        return symbol  # 無別名的幣：零額外請求，行為完全不變
    for candidate in candidates:
        try:
            await adapter.fetch_market(candidate)
        except ValueError:
            continue
        if candidate != symbol:
            log.info("幣種別名：%s 查無交易對，改用 %s", symbol, candidate)
        return candidate
    return symbol
