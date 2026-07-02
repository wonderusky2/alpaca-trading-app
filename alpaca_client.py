"""
alpaca_client.py — All Alpaca API interaction for robinhood-trader.
Adapted from swing-bot; uses local config/logger.
"""
from __future__ import annotations
import time
from typing import Optional
from datetime import date, timedelta

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, LimitOrderRequest,
    GetOrdersRequest, ClosePositionRequest, GetPortfolioHistoryRequest,
)
from alpaca.trading.enums import (
    OrderSide, TimeInForce, OrderClass, OrderType, QueryOrderStatus,
)
from alpaca.common.enums import Sort
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import (
    StockBarsRequest, NewsRequest,
    StockLatestQuoteRequest, StockSnapshotRequest,
)
from alpaca.data.enums import DataFeed
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

import config
from logger import get_logger

log = get_logger("alpaca_client")

_ALPACA_HOSTS = (
    "data.alpaca.markets",
    "paper-api.alpaca.markets",
    "api.alpaca.markets",
    "localhost",
    "127.0.0.1",
)


def _configure_alpaca_no_proxy() -> None:
    """Route Alpaca API traffic direct — corporate HTTP proxies often block market data."""
    import os
    for key in ("NO_PROXY", "no_proxy"):
        existing = os.environ.get(key, "")
        parts = [part.strip() for part in existing.split(",") if part.strip()]
        for host in _ALPACA_HOSTS:
            if host not in parts:
                parts.append(host)
        os.environ[key] = ",".join(parts)


_configure_alpaca_no_proxy()


def _make_data_feed(feed_str: str) -> DataFeed:
    mapping = {
        "iex": DataFeed.IEX,
        "sip": DataFeed.SIP,
        "otc": DataFeed.OTC,
    }
    feed = (feed_str or "iex").strip().lower()
    return mapping.get(feed, DataFeed.IEX)


class AlpacaClient:
    """Thin wrapper around alpaca-py."""

    def __init__(self):
        if not config.ALPACA_API_KEY or not config.ALPACA_API_SECRET:
            raise EnvironmentError(
                "Alpaca API keys not set. "
                "Run: eval $(cd ~/Code/conjur-secret-manager && npm run --silent export)"
            )
        self.trading = TradingClient(
            config.ALPACA_API_KEY,
            config.ALPACA_API_SECRET,
            paper=config.PAPER,
        )
        self.data = StockHistoricalDataClient(
            config.ALPACA_API_KEY,
            config.ALPACA_API_SECRET,
        )
        self.news = NewsClient(
            config.ALPACA_API_KEY,
            config.ALPACA_API_SECRET,
        )
        self._option_data = None
        log.info("AlpacaClient initialized — PAPER mode")

    @staticmethod
    def _require_submission_enabled() -> None:
        if not config.fund_manager_order_submission_enabled():
            raise EnvironmentError(
                "Order submission is locked. "
                "Set FUND_MANAGER_ORDER_SUBMISSION_ENABLED=true to enable."
            )

    # ── Account ───────────────────────────────────────────────────────────────

    def get_account(self) -> dict:
        acct = self.trading.get_account()
        equity      = float(acct.equity)
        last_equity = float(getattr(acct, "last_equity", 0) or 0)

        # Adjust last_equity to cancel phantom daily P&L from IGNORED_POSITIONS (e.g. DAWN).
        # These stuck/delisted positions have current market value in `equity` but were $0 at
        # prior close, so `equity - last_equity` inflates daily P&L by their full value.
        # Adding their current market value to last_equity neutralises this for all consumers.
        ignored_val = 0.0
        try:
            ignore = {s.upper() for s in (config.IGNORED_POSITIONS or [])}
            if ignore:
                for p in self.trading.get_all_positions():
                    if p.symbol.upper() in ignore:
                        ignored_val += float(getattr(p, "market_value", 0) or 0)
        except Exception:
            pass

        # Do NOT mutate last_equity here — it is used raw by the kill-switch,
        # risk sizing, and iOS P&L display. Adjusting it globally was breaking
        # daily_pnl math for all consumers (issue #4 in security audit).
        # Instead expose adjusted_last_equity as a separate key so the dashboard
        # can display a clean P&L while trade logic still uses the broker value.
        return {
            "equity":                equity,
            "last_equity":           last_equity,           # raw broker value — used by kill-switch
            "adjusted_last_equity":  last_equity + ignored_val,  # for display only (DAWN-adjusted)
            "ignored_position_val":  ignored_val,
            "cash":                  float(acct.cash),
            "buying_power":          float(acct.buying_power),
            "options_buying_power":  float(getattr(acct, "options_buying_power", 0) or 0),
            "options_approved_level": int(getattr(acct, "options_approved_level", 0) or 0),
            "options_trading_level": int(getattr(acct, "options_trading_level", 0) or 0),
            "daytrade_count":        int(acct.daytrade_count),
            "pdt":                   bool(acct.pattern_day_trader),
            "status":                acct.status.value,
        }

    def get_options_capability(self) -> dict:
        """Return account-level options capability without implying suitability."""
        try:
            acct = self.trading.get_account()
            cfg = self.trading.get_account_configurations()
            approved = int(getattr(acct, "options_approved_level", 0) or 0)
            trading = int(getattr(acct, "options_trading_level", 0) or 0)
            max_level = int(getattr(cfg, "max_options_trading_level", 0) or 0)
            buying_power = float(getattr(acct, "options_buying_power", 0) or 0)
            return {
                "ok": True,
                "paper": bool(config.PAPER),
                "enabled": approved > 0 or trading > 0 or bool(config.PAPER),
                "approved_level": approved,
                "trading_level": trading,
                "max_options_trading_level": max_level,
                "options_buying_power": buying_power,
                "paper_note": "Paper accounts generally support options testing; live accounts require approval.",
            }
        except Exception as e:
            log.warning("options capability check failed: %s", e)
            return {"ok": False, "enabled": False, "error": str(e), "paper": bool(config.PAPER)}

    def get_portfolio_history(self, period: str = "1M", timeframe: str = "1D") -> dict:
        req = GetPortfolioHistoryRequest(period=period, timeframe=timeframe)
        history = self.trading.get_portfolio_history(req)

        def _list(attr):
            value = getattr(history, attr, None)
            return list(value or [])

        return {
            "timestamp":       _list("timestamp"),
            "equity":          _list("equity"),
            "profit_loss":     _list("profit_loss"),
            "profit_loss_pct": _list("profit_loss_pct"),
            "base_value":      getattr(history, "base_value", None),
            "timeframe":       timeframe,
            "period":          period,
        }

    def get_clock(self) -> dict:
        clock = self.trading.get_clock()

        def _iso(value):
            return value.isoformat() if hasattr(value, "isoformat") else value

        return {
            "is_open":    bool(getattr(clock, "is_open", False)),
            "timestamp":  _iso(getattr(clock, "timestamp", None)),
            "next_open":  _iso(getattr(clock, "next_open", None)),
            "next_close": _iso(getattr(clock, "next_close", None)),
        }

    # ── Positions ─────────────────────────────────────────────────────────────

    def get_positions(self) -> dict[str, dict]:
        """Return {symbol: position_dict}, ignoring IGNORED_POSITIONS."""
        positions = self.trading.get_all_positions()
        ignore = {s.upper() for s in (config.IGNORED_POSITIONS or [])}
        out = {}
        for p in positions:
            if p.symbol.upper() in ignore:
                continue
            out[p.symbol] = {
                "qty":             float(p.qty),
                "side":            p.side.value,
                "entry":           float(p.avg_entry_price),
                "market_val":      float(p.market_value),
                "unrealized_pl":   float(p.unrealized_pl),
                "unrealized_plpc": float(p.unrealized_plpc),
            }
        return out

    def close_position(self, symbol: str) -> dict:
        self._require_submission_enabled()
        try:
            req = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=50)
            for o in self.trading.get_orders(filter=req):
                if o.symbol.upper() == symbol.upper():
                    try:
                        self.trading.cancel_order_by_id(o.id)
                    except Exception as e:
                        log.warning(f"{symbol}: cancel of {o.id} failed — {e}")
        except Exception as e:
            log.warning(f"{symbol}: could not list orders before close — {e}")
        resp = self.trading.close_position(symbol)
        log.info(f"Closed position: {symbol} → order {resp.id}")
        return {"order_id": str(resp.id), "symbol": symbol}

    # ── Orders ────────────────────────────────────────────────────────────────

    @staticmethod
    def _order_to_dict(o) -> dict:
        def _value(attr, default=None):
            value = getattr(o, attr, default)
            return getattr(value, "value", value)

        def _iso(attr):
            value = getattr(o, attr, None)
            return value.isoformat() if hasattr(value, "isoformat") else value

        return {
            "id":               str(getattr(o, "id", "")),
            "symbol":           getattr(o, "symbol", ""),
            "side":             _value("side"),
            "qty":              float(getattr(o, "qty", 0) or 0),
            "filled_qty":       float(getattr(o, "filled_qty", 0) or 0),
            "status":           _value("status"),
            "type":             _value("order_type"),
            "time_in_force":    _value("time_in_force"),
            "submitted_at":     _iso("submitted_at"),
            "filled_at":        _iso("filled_at"),
            "filled_avg_price": (
                float(getattr(o, "filled_avg_price"))
                if getattr(o, "filled_avg_price", None) is not None else None
            ),
            "limit_price": (
                float(getattr(o, "limit_price"))
                if getattr(o, "limit_price", None) is not None else None
            ),
        }

    def get_open_orders(self) -> list[dict]:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=50)
        return [self._order_to_dict(o) for o in self.trading.get_orders(filter=req)]

    def get_recent_orders(self, limit: int = 100) -> list[dict]:
        """Return recent orders (open + closed combined)."""
        try:
            req = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=limit)
            return [self._order_to_dict(o) for o in self.trading.get_orders(filter=req)]
        except Exception:
            orders_by_id = {}
            for qs in (QueryOrderStatus.OPEN, QueryOrderStatus.CLOSED):
                req = GetOrdersRequest(status=qs, limit=limit)
                for o in self.trading.get_orders(filter=req):
                    item = self._order_to_dict(o)
                    orders_by_id[item["id"]] = item
            return list(orders_by_id.values())[:limit]

    def cancel_order(self, order_id: str) -> bool:
        self._require_submission_enabled()
        try:
            self.trading.cancel_order_by_id(order_id)
            log.info(f"Cancelled order {order_id}")
            return True
        except Exception as e:
            log.warning(f"Could not cancel order {order_id}: {e}")
            return False

    def place_market_order(self, symbol: str, qty: int, side: str) -> dict:
        """Plain market order — used by the dashboard for paper trades."""
        self._require_submission_enabled()
        if qty <= 0:
            raise ValueError(f"Invalid qty {qty} for {symbol}")
        side_enum = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side_enum,
            time_in_force=TimeInForce.DAY,
        )
        order = self.trading.submit_order(req)
        log.info(f"LAB ORDER | {side_enum.value.upper()} {symbol} {qty}sh → {order.id}")
        return {
            "order_id": str(order.id),
            "symbol":   symbol,
            "qty":      qty,
            "side":     side_enum.value,
        }

    def _get_option_data_client(self):
        if self._option_data is None:
            from alpaca.data.historical.option import OptionHistoricalDataClient
            self._option_data = OptionHistoricalDataClient(
                config.ALPACA_API_KEY,
                config.ALPACA_API_SECRET,
            )
        return self._option_data

    @staticmethod
    def _contract_to_dict(contract) -> dict:
        def _value(attr, default=None):
            value = getattr(contract, attr, default)
            return getattr(value, "value", value)

        expiration = getattr(contract, "expiration_date", None)
        if hasattr(expiration, "isoformat"):
            expiration_s = expiration.isoformat()
        else:
            expiration_s = str(expiration or "")
        return {
            "id": str(getattr(contract, "id", "") or ""),
            "symbol": str(getattr(contract, "symbol", "") or ""),
            "underlying_symbol": str(getattr(contract, "underlying_symbol", "") or ""),
            "type": str(_value("type", "") or "").lower(),
            "status": str(_value("status", "") or "").lower(),
            "expiration_date": expiration_s,
            "strike_price": float(getattr(contract, "strike_price", 0) or 0),
            "root_symbol": str(getattr(contract, "root_symbol", "") or ""),
            "open_interest": (
                int(float(getattr(contract, "open_interest", 0) or 0))
                if getattr(contract, "open_interest", None) is not None else None
            ),
        }

    def get_option_contracts(
        self,
        underlying: str,
        contract_type: str,
        min_dte: int = 21,
        max_dte: int = 60,
        strike_gte: float | None = None,
        strike_lte: float | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Return active option contracts for an underlying, filtered by DTE/strike."""
        from alpaca.trading.requests import GetOptionContractsRequest
        from alpaca.trading.enums import AssetStatus, ContractType

        ctype = ContractType.CALL if str(contract_type).lower().startswith("c") else ContractType.PUT
        today = date.today()
        req = GetOptionContractsRequest(
            underlying_symbols=[underlying.upper()],
            status=AssetStatus.ACTIVE,
            type=ctype,
            expiration_date_gte=today + timedelta(days=max(1, int(min_dte))),
            expiration_date_lte=today + timedelta(days=max(int(min_dte), int(max_dte))),
            strike_price_gte=str(round(float(strike_gte), 2)) if strike_gte is not None else None,
            strike_price_lte=str(round(float(strike_lte), 2)) if strike_lte is not None else None,
            limit=max(1, min(int(limit or 100), 1000)),
        )
        resp = self.trading.get_option_contracts(req)
        contracts = getattr(resp, "option_contracts", None)
        if contracts is None:
            contracts = getattr(resp, "contracts", None)
        if contracts is None and isinstance(resp, dict):
            contracts = resp.get("option_contracts") or resp.get("contracts") or []
        return [self._contract_to_dict(c) for c in (contracts or [])]

    def get_option_put_contracts(
        self,
        underlying: str,
        min_dte: int = 21,
        max_dte: int = 60,
        strike_gte: float | None = None,
        strike_lte: float | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Return active put contracts for an underlying, filtered by DTE/strike."""
        return self.get_option_contracts(
            underlying,
            "put",
            min_dte=min_dte,
            max_dte=max_dte,
            strike_gte=strike_gte,
            strike_lte=strike_lte,
            limit=limit,
        )

    def get_option_call_contracts(
        self,
        underlying: str,
        min_dte: int = 7,
        max_dte: int = 21,
        strike_gte: float | None = None,
        strike_lte: float | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Return active call contracts for an underlying, filtered by DTE/strike."""
        return self.get_option_contracts(
            underlying,
            "call",
            min_dte=min_dte,
            max_dte=max_dte,
            strike_gte=strike_gte,
            strike_lte=strike_lte,
            limit=limit,
        )

    @staticmethod
    def _option_quote_to_dict(quote) -> dict:
        bid = (
            getattr(quote, "bid_price", None)
            if getattr(quote, "bid_price", None) is not None
            else getattr(quote, "bp", None)
        )
        ask = (
            getattr(quote, "ask_price", None)
            if getattr(quote, "ask_price", None) is not None
            else getattr(quote, "ap", None)
        )
        bid = float(bid or 0)
        ask = float(ask or 0)
        mid = round((bid + ask) / 2, 4) if bid > 0 and ask > 0 else None
        return {
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "bid_size": int(getattr(quote, "bid_size", 0) or getattr(quote, "bs", 0) or 0),
            "ask_size": int(getattr(quote, "ask_size", 0) or getattr(quote, "as_", 0) or 0),
        }

    def get_option_latest_quotes(self, contract_symbols: list[str]) -> dict[str, dict]:
        clean = sorted({str(s).upper() for s in (contract_symbols or []) if str(s).strip()})
        if not clean:
            return {}
        from alpaca.data.requests import OptionLatestQuoteRequest
        client = self._get_option_data_client()
        req = OptionLatestQuoteRequest(symbol_or_symbols=clean)
        resp = client.get_option_latest_quote(req)
        out: dict[str, dict] = {}
        for sym in clean:
            quote = resp.get(sym) if isinstance(resp, dict) else None
            if quote:
                out[sym] = self._option_quote_to_dict(quote)
        return out

    def place_option_market_order(self, contract_symbol: str, qty: int, side: str = "buy") -> dict:
        """Paper option market order. Kept separate from equity order path."""
        self._require_submission_enabled()
        if not config.PAPER or not bool(getattr(config, "OPTIONS_HEDGE_PAPER_ONLY", True)):
            raise EnvironmentError("Option execution is paper-only in this app.")
        if qty <= 0:
            raise ValueError(f"Invalid option qty {qty} for {contract_symbol}")
        side_enum = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        req = MarketOrderRequest(
            symbol=contract_symbol,
            qty=qty,
            side=side_enum,
            time_in_force=TimeInForce.DAY,
        )
        order = self.trading.submit_order(req)
        log.info("LAB OPTION ORDER | %s %s %s contracts → %s", side_enum.value.upper(), contract_symbol, qty, order.id)
        return {
            "order_id": str(order.id),
            "symbol": contract_symbol,
            "qty": qty,
            "side": side_enum.value,
            "asset_class": "option",
        }

    # ── News ──────────────────────────────────────────────────────────────────

    def get_news(
        self,
        symbols: list[str],
        start=None,
        end=None,
        limit: int = 50,
    ) -> list[dict]:
        """
        Return recent Alpaca/Benzinga news for a symbol basket.
        Articles are sorted newest-first.
        """
        clean = sorted({str(s).upper() for s in (symbols or []) if str(s).strip()})
        if not clean:
            return []
        try:
            req = NewsRequest(
                symbols=",".join(clean[:50]),
                start=start,
                end=end,
                limit=max(1, min(int(limit or 50), 50)),
                sort="desc",
                include_content=False,
                exclude_contentless=False,
            )
            news_set = self.news.get_news(req)
            articles = getattr(news_set, "news", None)
            if articles is None and hasattr(news_set, "data"):
                articles = (getattr(news_set, "data") or {}).get("news")
        except Exception as e:
            log.warning(f"get_news failed for {clean}: {e}")
            return []

        out = []
        for item in articles or []:
            def _iso(attr):
                value = getattr(item, attr, None)
                return value.isoformat() if hasattr(value, "isoformat") else value

            out.append({
                "id":         str(getattr(item, "id", "")),
                "headline":   getattr(item, "headline", "") or "",
                "summary":    getattr(item, "summary", "") or "",
                "source":     getattr(item, "source", "") or "",
                "url":        getattr(item, "url", None),
                "created_at": _iso("created_at"),
                "updated_at": _iso("updated_at"),
                "symbols":    [str(s).upper() for s in (getattr(item, "symbols", None) or [])],
            })
        return out

    # ── Market data ───────────────────────────────────────────────────────────

    def get_current_price(self, symbol: str) -> Optional[float]:
        """Latest trade price; None on failure."""
        try:
            from alpaca.data.requests import StockLatestTradeRequest
            req = StockLatestTradeRequest(
                symbol_or_symbols=symbol,
                feed=_make_data_feed(config.ALPACA_DATA_FEED),
            )
            resp = self.data.get_stock_latest_trade(req)
            trade = resp.get(symbol) if isinstance(resp, dict) else None
            return float(trade.price) if trade else None
        except Exception as e:
            log.debug(f"{symbol}: latest trade lookup failed — {e}")
            return None

    def get_market_movers(self, top: int = 25) -> list[dict]:
        """Top % gainers today from Alpaca's screener endpoint.

        Returns [{symbol, price, percent_change}] sorted by move size.
        Fails open (empty list) — the trading loop must never depend on it.
        """
        try:
            from alpaca.data.historical.screener import ScreenerClient
            from alpaca.data.requests import MarketMoversRequest
        except Exception as e:
            log.debug("Screener API unavailable in this alpaca-py version: %s", e)
            return []
        try:
            sc = ScreenerClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET)
            resp = sc.get_market_movers(MarketMoversRequest(top=max(1, min(int(top), 50))))
            out = []
            for m in (getattr(resp, "gainers", None) or []):
                sym = str(getattr(m, "symbol", "") or "").upper()
                if not sym:
                    continue
                out.append({
                    "symbol": sym,
                    "price": float(getattr(m, "price", 0) or 0),
                    "percent_change": float(getattr(m, "percent_change", 0) or 0),
                })
            return out
        except Exception as e:
            log.warning("get_market_movers failed: %s", e)
            return []

    def get_snapshots(self, symbols: list[str]) -> dict[str, dict]:
        """Return {sym: {price, prev_close, change_pct, vwap}} for a symbol basket.

        Uses Alpaca's snapshot endpoint — works 24/7, returns last known prices
        even on weekends and after hours. No alternate market-data vendor.
        """
        clean = sorted({str(s).upper() for s in (symbols or []) if str(s).strip()})
        if not clean:
            return {}
        feed = _make_data_feed(config.ALPACA_DATA_FEED)
        out: dict[str, dict] = {}
        # Batch in groups of 100 (Alpaca limit)
        for i in range(0, len(clean), 100):
            batch = clean[i:i + 100]
            try:
                req = StockSnapshotRequest(
                    symbol_or_symbols=batch,
                    feed=feed,
                )
                resp = self.data.get_stock_snapshot(req)
                for sym, snap in (resp or {}).items():
                    sym = sym.upper()
                    try:
                        # latest_trade has current price; daily_bar has vwap
                        latest_trade = getattr(snap, "latest_trade", None)
                        latest_quote = getattr(snap, "latest_quote", None)
                        daily_bar    = getattr(snap, "daily_bar", None)
                        prev_bar     = getattr(snap, "prev_daily_bar", None)

                        price = None
                        trade_ts = None
                        if latest_trade:
                            price = float(getattr(latest_trade, "price", 0) or 0)
                            _ts = getattr(latest_trade, "timestamp", None)
                            trade_ts = _ts.isoformat() if hasattr(_ts, "isoformat") else None

                        bid = ask = 0.0
                        if latest_quote:
                            bid = float(getattr(latest_quote, "bid_price", 0) or 0)
                            ask = float(getattr(latest_quote, "ask_price", 0) or 0)
                        if not price and daily_bar:
                            price = float(getattr(daily_bar, "close", 0) or 0)

                        prev_close = None
                        if prev_bar:
                            prev_close = float(getattr(prev_bar, "close", 0) or 0)
                        # Do NOT fall back to today's open — that gives intraday move,
                        # not the daily move vs yesterday's close (breaks P&L benchmark).
                        if not prev_close:
                            prev_close = price  # flat change if we have no prior close

                        vwap = 0.0
                        daily_open = 0.0
                        if daily_bar:
                            vwap = float(getattr(daily_bar, "vwap", 0) or 0)
                            daily_open = float(getattr(daily_bar, "open", 0) or 0)

                        if price and price > 0:
                            change_pct = (price - prev_close) / prev_close * 100 if prev_close else 0
                            out[sym] = {
                                "price":      round(price, 4),
                                "prev_close": round(prev_close, 4),
                                "change_pct": round(change_pct, 4),
                                "vwap":       round(vwap, 4),
                                "daily_open": round(daily_open, 4),
                                # timestamp/bid/ask feed the stale-quote and
                                # spread kill gates in trader.py — without
                                # these fields both gates were dead code.
                                "timestamp":  trade_ts,
                                "bid":        round(bid, 4),
                                "ask":        round(ask, 4),
                            }
                    except Exception as se:
                        log.debug("Snapshot parse failed for %s: %s", sym, se)
            except Exception as e:
                log.warning("get_snapshots batch failed (%s): %s", batch, e)
        log.info("get_snapshots: got data for %d/%d symbols", len(out), len(clean))
        return out

    def get_historical_bars(
        self,
        symbols: list[str],
        timeframe: str = "15min",
        limit: int = 200,
    ) -> dict[str, "pd.DataFrame"]:
        """Return {sym: DataFrame(open,high,low,close,volume,vwap)} for indicator computation.

        timeframe: '1min'|'5min'|'15min'|'1hour'|'1day'
        Works 24/7 — returns last N bars regardless of whether market is open.
        """
        import pandas as pd

        clean = sorted({str(s).upper() for s in (symbols or []) if str(s).strip()})
        if not clean:
            return {}

        _tf_map = {
            "1min":  TimeFrame(1,  TimeFrameUnit.Minute),
            "5min":  TimeFrame(5,  TimeFrameUnit.Minute),
            "15min": TimeFrame(15, TimeFrameUnit.Minute),
            "1hour": TimeFrame(1,  TimeFrameUnit.Hour),
            "1day":  TimeFrame(1,  TimeFrameUnit.Day),
        }
        tf = _tf_map.get(timeframe.lower(), TimeFrame(15, TimeFrameUnit.Minute))
        feed = _make_data_feed(config.ALPACA_DATA_FEED)
        out: dict[str, pd.DataFrame] = {}

        for i in range(0, len(clean), 50):
            batch = clean[i:i + 50]
            try:
                # IEX free feed ignores `limit` without an explicit start date.
                # Compute start from timeframe + limit so historical data is returned.
                import datetime as _dt
                _mins_per_bar = {
                    "1min": 1, "5min": 5, "15min": 15, "1hour": 60, "1day": 390
                }.get(timeframe.lower(), 15)
                _trading_mins = limit * _mins_per_bar
                _calendar_days = max(7, int(_trading_mins / 390 * 1.6) + 3)  # 60% buffer
                _bar_start = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=_calendar_days)
                req = StockBarsRequest(
                    symbol_or_symbols=batch,
                    timeframe=tf,
                    start=_bar_start,
                    limit=limit,
                    feed=feed,
                    adjustment="raw",
                )
                resp = self.data.get_stock_bars(req)
                bars_dict = resp.data if hasattr(resp, "data") else (resp if isinstance(resp, dict) else {})
                for sym, bars in bars_dict.items():
                    sym = sym.upper()
                    if not bars:
                        continue
                    rows = []
                    for b in bars:
                        ts = getattr(b, "timestamp", None)
                        rows.append({
                            "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else ts,
                            "open":   float(getattr(b, "open",   0) or 0),
                            "high":   float(getattr(b, "high",   0) or 0),
                            "low":    float(getattr(b, "low",    0) or 0),
                            "close":  float(getattr(b, "close",  0) or 0),
                            "volume": float(getattr(b, "volume", 0) or 0),
                            "vwap":   float(getattr(b, "vwap",   0) or 0),
                        })
                    if rows:
                        out[sym] = pd.DataFrame(rows)
            except Exception as e:
                log.warning("get_historical_bars batch failed (%s): %s", batch, e)

        log.info("get_historical_bars: got bars for %d/%d symbols", len(out), len(clean))
        return out

    def get_historical_bars_range(
        self,
        symbols: list[str],
        start_date: str,
        end_date: str,
        timeframe: str = "1day",
    ) -> dict[str, list[dict]]:
        """
        Fetch daily OHLCV bars for an explicit date range.
        Returns {SYM: [bar_dicts]} — same shape as load_historical_data_yf().
        Used by run_walk_forward() as a cloud-safe alternative to yfinance.
        """
        import datetime as _dt
        _tf_map = {
            "1min":  TimeFrame(1,  TimeFrameUnit.Minute),
            "5min":  TimeFrame(5,  TimeFrameUnit.Minute),
            "15min": TimeFrame(15, TimeFrameUnit.Minute),
            "1hour": TimeFrame(1,  TimeFrameUnit.Hour),
            "1day":  TimeFrame(1,  TimeFrameUnit.Day),
        }
        tf = _tf_map.get(timeframe.lower(), TimeFrame(1, TimeFrameUnit.Day))
        feed = _make_data_feed(config.ALPACA_DATA_FEED)

        def _parse_date(s: str) -> _dt.datetime:
            d = _dt.datetime.strptime(s[:10], "%Y-%m-%d")
            return d.replace(tzinfo=_dt.timezone.utc)

        clean = sorted({str(s).upper() for s in (symbols or []) if str(s).strip()})
        out: dict[str, list[dict]] = {}

        for i in range(0, len(clean), 50):
            batch = clean[i:i + 50]
            try:
                req = StockBarsRequest(
                    symbol_or_symbols=batch,
                    timeframe=tf,
                    start=_parse_date(start_date),
                    end=_parse_date(end_date),
                    feed=feed,
                    adjustment="split",
                )
                resp = self.data.get_stock_bars(req)
                bars_dict = resp.data if hasattr(resp, "data") else (resp if isinstance(resp, dict) else {})
                for sym, bars in bars_dict.items():
                    sym = sym.upper()
                    if not bars:
                        continue
                    rows = []
                    for b in bars:
                        ts = getattr(b, "timestamp", None)
                        rows.append({
                            "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else ts,
                            "open":   float(getattr(b, "open",   0) or 0),
                            "high":   float(getattr(b, "high",   0) or 0),
                            "low":    float(getattr(b, "low",    0) or 0),
                            "close":  float(getattr(b, "close",  0) or 0),
                            "volume": float(getattr(b, "volume", 0) or 0),
                            "vwap":   float(getattr(b, "vwap",   0) or 0),
                        })
                    if rows:
                        out[sym] = rows
            except Exception as e:
                log.warning("get_historical_bars_range batch failed (%s): %s", batch, e)

        log.info("get_historical_bars_range: got bars for %d/%d symbols", len(out), len(clean))
        return out
