"""strategy_model.py — persisted adaptive strategy parameters."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


STATE_DIR = Path(os.environ.get("STATE_DIR") or (Path.home() / ".robinhood-trader" / "state"))
MODEL_STATE_PATH = STATE_DIR / "strategy_model.json"
POSITION_MEMORY_PATH = STATE_DIR / "position_memory.json"

DEFAULT_MODEL = {
    "version": 1,
    "generation": 0,
    "min_conviction": 75,
    "max_positions": 3,
    "position_size_pct": 0.05,
    "trailing_stop_pct": 3.0,
    "profit_lock_trigger_pct": 2.0,
    "profit_giveback_pct": 1.0,
    "max_holding_days": 2,
    "exit_on_regime_flip": True,
    "daily_loss_kill_pct": -2.0,
    "learning_mode": "paper_safe",
    "updated_at": None,
    "last_backtest": None,
}

BOUNDS = {
    "min_conviction": (65, 90),
    "max_positions": (1, 5),
    "position_size_pct": (0.02, 0.08),
    "trailing_stop_pct": (1.5, 6.0),
    "profit_lock_trigger_pct": (0.75, 6.0),
    "profit_giveback_pct": (0.25, 3.0),
    "max_holding_days": (1, 7),
    "daily_loss_kill_pct": (-4.0, -0.75),
}


def _clamp(value, low, high):
    return max(low, min(high, value))


def sanitize_model(model: dict) -> dict:
    clean = {**DEFAULT_MODEL, **(model or {})}
    clean["min_conviction"] = int(_clamp(int(clean["min_conviction"]), *BOUNDS["min_conviction"]))
    clean["max_positions"] = int(_clamp(int(clean["max_positions"]), *BOUNDS["max_positions"]))
    clean["position_size_pct"] = round(float(_clamp(float(clean["position_size_pct"]), *BOUNDS["position_size_pct"])), 4)
    clean["trailing_stop_pct"] = round(float(_clamp(float(clean["trailing_stop_pct"]), *BOUNDS["trailing_stop_pct"])), 2)
    clean["profit_lock_trigger_pct"] = round(float(_clamp(float(clean["profit_lock_trigger_pct"]), *BOUNDS["profit_lock_trigger_pct"])), 2)
    clean["profit_giveback_pct"] = round(float(_clamp(float(clean["profit_giveback_pct"]), *BOUNDS["profit_giveback_pct"])), 2)
    clean["max_holding_days"] = int(_clamp(int(clean["max_holding_days"]), *BOUNDS["max_holding_days"]))
    clean["daily_loss_kill_pct"] = round(float(_clamp(float(clean["daily_loss_kill_pct"]), *BOUNDS["daily_loss_kill_pct"])), 2)
    clean["exit_on_regime_flip"] = bool(clean.get("exit_on_regime_flip", True))
    clean["generation"] = int(clean.get("generation") or 0)
    return clean


def load_model() -> dict:
    try:
        return sanitize_model(json.loads(MODEL_STATE_PATH.read_text(encoding="utf-8")))
    except Exception:
        return sanitize_model(DEFAULT_MODEL)


def save_model(model: dict) -> dict:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    clean = sanitize_model(model)
    clean["updated_at"] = datetime.now(timezone.utc).isoformat()
    MODEL_STATE_PATH.write_text(json.dumps(clean, indent=2, sort_keys=True), encoding="utf-8")
    return clean


def propose_adjustment(model: dict, backtest: dict) -> dict:
    current = sanitize_model(model)
    optimizer = backtest.get("optimizer") or {}
    best = optimizer.get("best_model")
    if best:
        proposed = sanitize_model({**current, **best})
        proposed["generation"] = current["generation"] + 1 if proposed != current else current["generation"]
        return {
            "current": current,
            "proposed": proposed,
            "changed": proposed != current,
            "confidence": optimizer.get("confidence", "medium"),
            "reasons": optimizer.get("reasons") or ["Daily profit optimizer selected this parameter set."],
            "optimizer": optimizer,
        }

    proposed = dict(current)
    metrics = backtest.get("metrics") or {}
    total_return = float(metrics.get("total_return_pct") or 0)
    max_drawdown = float(metrics.get("max_drawdown_pct") or 0)
    win_rate = float(metrics.get("win_rate") or 0)
    trade_count = int(metrics.get("trade_count") or 0)
    confidence = "low"
    reasons: list[str] = []

    if trade_count < 4:
        reasons.append("Not enough closed trade evidence to adjust aggressively.")
    elif total_return < 0 or max_drawdown > 4:
        proposed["min_conviction"] += 3
        proposed["position_size_pct"] *= 0.85
        proposed["max_positions"] -= 1
        proposed["max_holding_days"] -= 1
        proposed["profit_giveback_pct"] *= 0.8
        confidence = "medium"
        reasons.append("Recent results are weak or drawdown is elevated; reduce aggressiveness.")
    elif total_return > 1 and win_rate >= 0.55 and max_drawdown <= 3:
        proposed["min_conviction"] -= 1
        proposed["position_size_pct"] *= 1.05
        confidence = "medium"
        reasons.append("Recent results are positive with acceptable drawdown; allow a small increase.")
    else:
        reasons.append("Results are mixed; keep current parameters.")

    proposed = sanitize_model(proposed)
    proposed["generation"] = current["generation"] + 1 if proposed != current else current["generation"]
    return {
        "current": current,
        "proposed": proposed,
        "changed": proposed != current,
        "confidence": confidence,
        "reasons": reasons,
    }


def candidate_models(base: dict) -> list[dict]:
    base = sanitize_model(base)
    candidates: list[dict] = []
    conviction_steps = [0, -3, -1, 2, 4]
    size_steps = [1.0, 0.75, 0.9, 1.1, 1.25]
    position_steps = [0, -1, 1]
    stop_steps = [1.0, 0.8, 1.2]
    hold_steps = [0, -1, 1]
    lock_steps = [1.0, 0.75, 1.25]

    for conviction_delta in conviction_steps:
        for size_mult in size_steps:
            for position_delta in position_steps:
                for stop_mult in stop_steps:
                    for hold_delta in hold_steps:
                        for lock_mult in lock_steps:
                            candidate = sanitize_model({
                                **base,
                                "min_conviction": base["min_conviction"] + conviction_delta,
                                "position_size_pct": base["position_size_pct"] * size_mult,
                                "max_positions": base["max_positions"] + position_delta,
                                "trailing_stop_pct": base["trailing_stop_pct"] * stop_mult,
                                "max_holding_days": base["max_holding_days"] + hold_delta,
                                "profit_giveback_pct": base["profit_giveback_pct"] * lock_mult,
                            })
                            if candidate not in candidates:
                                candidates.append(candidate)

    return candidates


def load_position_memory() -> dict:
    try:
        data = json.loads(POSITION_MEMORY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_position_memory(memory: dict) -> dict:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    POSITION_MEMORY_PATH.write_text(json.dumps(memory, indent=2, sort_keys=True), encoding="utf-8")
    return memory


def update_position_memory(positions: dict) -> dict:
    memory = load_position_memory()
    now = datetime.now(timezone.utc).isoformat()
    active = {str(sym).upper() for sym in (positions or {}).keys()}

    for sym, pos in (positions or {}).items():
        sym = str(sym).upper()
        pnl_pct = float(pos.get("unrealized_plpc") or 0) * 100
        entry = float(pos.get("entry") or 0)
        item = memory.get(sym) or {}
        item.setdefault("first_seen_at", now)
        item.setdefault("entry_price", entry)
        item["last_seen_at"] = now
        item["peak_unrealized_pnl_pct"] = max(float(item.get("peak_unrealized_pnl_pct") or pnl_pct), pnl_pct)
        item["last_unrealized_pnl_pct"] = pnl_pct
        memory[sym] = item

    for sym in list(memory.keys()):
        if sym not in active:
            memory.pop(sym, None)

    return save_position_memory(memory)
