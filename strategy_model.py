"""strategy_model.py — persisted adaptive strategy parameters."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


STATE_DIR = Path(os.environ.get("STATE_DIR") or (Path.home() / ".robinhood-trader" / "state"))
MODEL_STATE_PATH      = STATE_DIR / "strategy_model.json"
POSITION_MEMORY_PATH  = STATE_DIR / "position_memory.json"
VARIANT_HISTORY_PATH  = STATE_DIR / "variant_history.json"

DEFAULT_MODEL = {
    "version": 5,
    "generation": 0,
    "min_conviction": 78,
    "bear_etf_min_conviction": 50,
    "max_positions": 3,
    "position_size_pct": 0.16,
    "trailing_stop_pct": 2.0,
    # v5: risk/reward rebalanced. The old 0.6/0.4 lock exited winners around
    # +0.2–0.6% while the stop risked -2.0% — structurally negative expectancy
    # (ledger showed avg_win $22 vs avg_loss $44). Winners must be allowed to
    # earn at least as much as a loser costs.
    "profit_target_pct": 3.0,
    "profit_lock_trigger_pct": 1.5,
    "profit_giveback_pct": 0.75,
    "partial_exit_trigger_pct": 2.5,
    "partial_exit_fraction": 0.33,
    "velocity_stop_pct": 1.5,          # exit if drops >1.5% in one tick
    # ── Pyramid / conviction sizing ──────────────────────────────────────────
    "conviction_size_scale": True,     # scale initial buy by conviction score
    "pyramid_trigger_pct": 1.2,        # add to winner only after real confirmation
    "max_pyramid_adds": 1,             # max add-ons per position (one tranche)
    "max_single_position_pct": 0.20,   # hard cap: max 20% equity in one name (paper)
    # ────────────────────────────────────────────────────────────────────────
    "max_holding_days": 1,
    "exit_on_regime_flip": True,
    "daily_loss_kill_pct": -1.5,
    "learning_mode": "paper_safe",
    "active_variant": "alpha_momentum",
    "updated_at": None,
    "last_backtest": None,
}

BOUNDS = {
    "min_conviction": (65, 85),
    "bear_etf_min_conviction": (42, 60), # bear ETFs lag; too low = noise, too high = never enters
    "max_positions": (1, 6),
    "position_size_pct": (0.01, 0.20),
    "trailing_stop_pct": (1.5, 6.0),
    "profit_target_pct": (1.0, 8.0),
    "profit_lock_trigger_pct": (0.2, 6.0),
    "profit_giveback_pct": (0.1, 3.0),
    "partial_exit_trigger_pct": (0.3, 10.0),
    "partial_exit_fraction": (0.25, 0.75),
    "velocity_stop_pct": (0.5, 5.0),
    "pyramid_trigger_pct": (0.2, 5.0),
    "max_pyramid_adds": (0, 3),
    "max_single_position_pct": (0.03, 0.20),
    "max_holding_days": (1, 7),
    "daily_loss_kill_pct": (-2.5, -0.5),
}


def _clamp(value, low, high):
    return max(low, min(high, value))


def _apply_alpha_first_migration(clean: dict) -> dict:
    version = int(clean.get("version") or 0)

    if version < 4:
        clean["min_conviction"] = max(int(clean.get("min_conviction", 78)), 78)
        clean["max_positions"] = min(int(clean.get("max_positions", 3)), 3)
        clean["position_size_pct"] = max(float(clean.get("position_size_pct", 0.16)), 0.16)
        clean["trailing_stop_pct"] = min(float(clean.get("trailing_stop_pct", 2.0)), 2.0)
        clean["profit_lock_trigger_pct"] = min(float(clean.get("profit_lock_trigger_pct", 0.6)), 0.6)
        clean["profit_giveback_pct"] = min(float(clean.get("profit_giveback_pct", 0.4)), 0.4)
        clean["partial_exit_trigger_pct"] = max(float(clean.get("partial_exit_trigger_pct", 2.5)), 2.5)
        clean["partial_exit_fraction"] = min(float(clean.get("partial_exit_fraction", 0.33)), 0.33)
        clean["pyramid_trigger_pct"] = max(float(clean.get("pyramid_trigger_pct", 1.2)), 1.2)
        clean["max_single_position_pct"] = min(float(clean.get("max_single_position_pct", 0.20)), 0.20)
        clean["max_holding_days"] = min(int(clean.get("max_holding_days", 1)), 1)
        clean["daily_loss_kill_pct"] = min(float(clean.get("daily_loss_kill_pct", -1.5)), -1.5)
        clean["active_variant"] = "alpha_momentum"
        version = 4

    if version < 5:
        # v5: fix inverted risk/reward. Old params locked winners at ~+0.2-0.6%
        # while risking -2.0% per trade. Raise the lock trigger and giveback so
        # a winner can pay for at least one loser.
        clean["profit_lock_trigger_pct"] = max(float(clean.get("profit_lock_trigger_pct", 1.5)), 1.5)
        clean["profit_giveback_pct"] = max(float(clean.get("profit_giveback_pct", 0.75)), 0.75)
        clean.setdefault("profit_target_pct", 3.0)
        version = 5

    clean["version"] = version
    return clean


def sanitize_model(model: dict) -> dict:
    clean = {**DEFAULT_MODEL, **(model or {})}
    clean = _apply_alpha_first_migration(clean)
    clean["min_conviction"] = int(_clamp(int(clean["min_conviction"]), *BOUNDS["min_conviction"]))
    clean["bear_etf_min_conviction"] = int(_clamp(int(clean.get("bear_etf_min_conviction", 50)), *BOUNDS["bear_etf_min_conviction"]))
    clean["max_positions"] = int(_clamp(int(clean["max_positions"]), *BOUNDS["max_positions"]))
    clean["position_size_pct"] = round(float(_clamp(float(clean["position_size_pct"]), *BOUNDS["position_size_pct"])), 4)
    clean["trailing_stop_pct"] = round(float(_clamp(float(clean["trailing_stop_pct"]), *BOUNDS["trailing_stop_pct"])), 2)
    clean["profit_target_pct"] = round(float(_clamp(float(clean.get("profit_target_pct", 3.0)), *BOUNDS["profit_target_pct"])), 2)
    clean["profit_lock_trigger_pct"] = round(float(_clamp(float(clean["profit_lock_trigger_pct"]), *BOUNDS["profit_lock_trigger_pct"])), 2)
    clean["profit_giveback_pct"] = round(float(_clamp(float(clean["profit_giveback_pct"]), *BOUNDS["profit_giveback_pct"])), 2)
    clean["partial_exit_trigger_pct"] = round(float(_clamp(float(clean.get("partial_exit_trigger_pct", 1.0)), *BOUNDS["partial_exit_trigger_pct"])), 2)
    clean["partial_exit_fraction"] = round(float(_clamp(float(clean.get("partial_exit_fraction", 0.5)), *BOUNDS["partial_exit_fraction"])), 2)
    clean["velocity_stop_pct"] = round(float(_clamp(float(clean.get("velocity_stop_pct", 1.5)), *BOUNDS["velocity_stop_pct"])), 2)
    clean["conviction_size_scale"] = bool(clean.get("conviction_size_scale", True))
    clean["pyramid_trigger_pct"] = round(float(_clamp(float(clean.get("pyramid_trigger_pct", 0.5)), *BOUNDS["pyramid_trigger_pct"])), 2)
    clean["max_pyramid_adds"] = int(_clamp(int(clean.get("max_pyramid_adds", 1)), *BOUNDS["max_pyramid_adds"]))
    clean["max_single_position_pct"] = round(float(_clamp(float(clean.get("max_single_position_pct", 0.45)), *BOUNDS["max_single_position_pct"])), 2)
    clean["max_holding_days"] = int(_clamp(int(clean["max_holding_days"]), *BOUNDS["max_holding_days"]))
    clean["daily_loss_kill_pct"] = round(float(_clamp(float(clean["daily_loss_kill_pct"]), *BOUNDS["daily_loss_kill_pct"])), 2)
    clean["exit_on_regime_flip"] = bool(clean.get("exit_on_regime_flip", True))
    clean["generation"] = int(clean.get("generation") or 0)
    clean["active_variant"] = str(clean.get("active_variant") or "current")
    return clean


def load_model() -> dict:
    try:
        raw = json.loads(MODEL_STATE_PATH.read_text(encoding="utf-8"))
        clean = sanitize_model(raw)
        if clean != raw:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            clean["updated_at"] = datetime.now(timezone.utc).isoformat()
            MODEL_STATE_PATH.write_text(json.dumps(clean, indent=2, sort_keys=True), encoding="utf-8")
        return clean
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
        proposed["min_conviction"] += 4
        proposed["position_size_pct"] *= 0.80
        proposed["max_positions"] -= 1
        proposed["max_holding_days"] -= 1
        proposed["profit_giveback_pct"] *= 0.8
        confidence = "medium"
        reasons.append("Recent results are weak or drawdown is elevated; reduce aggressiveness.")
    elif total_return > 1 and win_rate >= 0.55 and max_drawdown <= 3:
        proposed["min_conviction"] -= 2
        proposed["position_size_pct"] *= 1.10
        proposed["max_holding_days"] += 1
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


# ── Position memory ────────────────────────────────────────────────────────────
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
        item["last_tick_price"] = item.get("current_price", 0)   # previous tick → for velocity
        item["current_price"]   = float(pos.get("current_price") or entry or 0)
        item["last_seen_at"] = now
        item["peak_unrealized_pnl_pct"] = max(float(item.get("peak_unrealized_pnl_pct") or pnl_pct), pnl_pct)
        item["last_unrealized_pnl_pct"] = pnl_pct
        memory[sym] = item

    for sym in list(memory.keys()):
        if sym not in active:
            memory.pop(sym, None)

    return save_position_memory(memory)


# ── Variant history ────────────────────────────────────────────────────────────
def load_variant_history() -> dict:
    """
    Return {date_str: {winner, scores, recorded_at}} for the last 90 days.
    scores is a dict mapping variant_name → objective float.
    """
    try:
        data = json.loads(VARIANT_HISTORY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def record_variant_result(date: str, winner: str, scores: dict) -> None:
    """
    Persist the winning variant and per-variant objective scores for `date`.
    Keeps the last 90 days; older entries are pruned automatically.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    history = load_variant_history()
    history[date] = {
        "winner": winner,
        "scores": {k: round(float(v), 4) for k, v in scores.items()},
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    # Prune to last 90 days
    if len(history) > 90:
        for old_key in sorted(history.keys())[:-90]:
            history.pop(old_key, None)
    VARIANT_HISTORY_PATH.write_text(
        json.dumps(history, indent=2, sort_keys=True), encoding="utf-8"
    )


def variant_win_rates(history: dict | None = None) -> dict[str, dict]:
    """
    Return per-variant win statistics across all recorded days.
    {variant_name: {wins, total, win_rate, avg_objective}}
    """
    if history is None:
        history = load_variant_history()
    tallies: dict[str, dict] = {}
    for entry in history.values():
        winner = str(entry.get("winner") or "current")
        scores = entry.get("scores") or {}
        for variant, obj in scores.items():
            t = tallies.setdefault(variant, {"wins": 0, "total": 0, "objective_sum": 0.0})
            t["total"] += 1
            t["objective_sum"] += float(obj)
            if variant == winner:
                t["wins"] += 1
    return {
        v: {
            "wins": t["wins"],
            "total": t["total"],
            "win_rate": round(t["wins"] / t["total"], 3) if t["total"] else 0.0,
            "avg_objective": round(t["objective_sum"] / t["total"], 4) if t["total"] else 0.0,
        }
        for v, t in tallies.items()
    }
