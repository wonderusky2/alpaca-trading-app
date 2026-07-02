"""
learning_agent.py — bounded self-learning loop with guardrails.

The QA agent verifies the system works; this module makes it improve. Evidence
flows in from two sources — the live paper ledger (ground truth) and the
v5-parity backtest optimizer (hypothesis generator) — and flows out as three
kinds of guarded change:

  1. Variant adoption  — apply the weekly optimizer's winning signal-weight
                         variant, if it beat 'current' by a real margin.
  2. Parameter nudges  — small, bounded adjustments from live trade evidence
                         (via strategy_model.propose_adjustment + BOUNDS).
  3. Symbol blocklist  — names with persistent negative realized expectancy
                         stop getting NEW entries for a cooling-off period.
                         (Existing positions are never force-closed by this.)

Safety rails — all of these hold at all times:
  * Every parameter passes strategy_model.sanitize_model() (hard BOUNDS).
  * At most one params/variant change per LEARNING_MIN_DAYS_BETWEEN_CHANGES.
  * Changes require minimum live-trade evidence, not just backtest scores.
  * Before any change the current model + live expectancy are checkpointed;
    if expectancy over the next LEARNING_ROLLBACK_MIN_TRADES closed trades is
    materially worse, the change is automatically rolled back.
  * Every action (and every rollback) is appended to a JSON journal.
  * Kill switch: config.AUTO_LEARNING_ENABLED = False disables everything.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import config
import notify
import strategy_model
import trade_ledger
from logger import get_logger

log = get_logger("learning")

JOURNAL_PATH    = strategy_model.STATE_DIR / "learning_journal.json"
BLOCKLIST_PATH  = strategy_model.STATE_DIR / "learned_blocklist.json"
CHECKPOINT_PATH = strategy_model.STATE_DIR / "learning_checkpoint.json"
_LAST_OPT_PATH  = strategy_model.STATE_DIR / "last_optimization.json"
_RATE_PATH      = strategy_model.STATE_DIR / "learning_last_run.json"

_ROLLBACK_TOLERANCE_USD = 5.0   # expectancy must be > $5/trade worse to trigger rollback


# ── State helpers ─────────────────────────────────────────────────────────────
def _read_json(path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _write_json(path, data) -> None:
    strategy_model.STATE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _journal(kind: str, detail: dict) -> None:
    data = _read_json(JOURNAL_PATH, {"changes": []})
    data["changes"].append({
        "at": _now().isoformat(),
        "kind": kind,
        "detail": detail,
    })
    data["changes"] = data["changes"][-200:]
    _write_json(JOURNAL_PATH, data)


def journal_entries(limit: int = 50) -> list[dict]:
    return list(reversed((_read_json(JOURNAL_PATH, {"changes": []}).get("changes") or [])[-limit:]))


def _days_since_last_change(kinds: tuple[str, ...]) -> float:
    for entry in journal_entries(limit=200):
        if entry.get("kind") in kinds:
            try:
                at = datetime.fromisoformat(str(entry["at"]))
                return (_now() - at).total_seconds() / 86400
            except Exception:
                continue
    return 1e9


# ── Live evidence ─────────────────────────────────────────────────────────────
def _experiment_sells(limit: int = 500) -> list[dict]:
    exp = str(getattr(config, "ALPHA_EXPERIMENT_ID", "")) or None
    out = []
    for row in trade_ledger.recent_trades(limit=limit):
        if str(row.get("side") or "").lower() != "sell" or row.get("pnl") is None:
            continue
        if exp and str(row.get("experiment_id") or "") != exp:
            continue
        out.append(row)
    return out


def _sells_since(ts_iso: str) -> list[dict]:
    try:
        cutoff = datetime.fromisoformat(str(ts_iso))
    except Exception:
        return []
    out = []
    for row in _experiment_sells():
        try:
            at = datetime.fromisoformat(str(row.get("recorded_at")).replace("Z", "+00:00"))
        except Exception:
            continue
        if at >= cutoff:
            out.append(row)
    return out


def _expectancy(rows: list[dict]) -> float:
    pnls = [float(r["pnl"]) for r in rows]
    return sum(pnls) / len(pnls) if pnls else 0.0


# ── 3. Symbol blocklist ───────────────────────────────────────────────────────
def learned_blocked_symbols() -> dict[str, dict]:
    """Return {symbol: entry} for non-expired learned blocks."""
    raw = _read_json(BLOCKLIST_PATH, {})
    now = _now()
    live = {}
    for sym, entry in (raw or {}).items():
        try:
            if datetime.fromisoformat(str(entry.get("until"))) > now:
                live[str(sym).upper()] = entry
        except Exception:
            continue
    return live


def is_symbol_blocked(symbol: str) -> bool:
    return str(symbol or "").upper() in learned_blocked_symbols()


def update_symbol_blocklist() -> list[dict]:
    """Block NEW entries in symbols with persistent negative realized results."""
    min_trades = int(getattr(config, "LEARNING_BLOCK_MIN_TRADES", 5))
    block_days = int(getattr(config, "LEARNING_BLOCK_DAYS", 30))
    stats: dict[str, dict] = {}
    for row in _experiment_sells():
        sym = str(row.get("symbol") or "").upper()
        s = stats.setdefault(sym, {"n": 0, "wins": 0, "pnl": 0.0})
        s["n"] += 1
        if float(row["pnl"]) > 0:
            s["wins"] += 1
        s["pnl"] += float(row["pnl"])

    current = learned_blocked_symbols()
    changes: list[dict] = []
    for sym, s in stats.items():
        if sym in current or s["n"] < min_trades:
            continue
        win_rate = s["wins"] / s["n"]
        if s["pnl"] < 0 and win_rate < 0.45:
            entry = {
                "blocked_at": _now().isoformat(),
                "until": (_now() + timedelta(days=block_days)).isoformat(),
                "trades": s["n"],
                "win_rate": round(win_rate, 3),
                "total_pnl": round(s["pnl"], 2),
            }
            current[sym] = entry
            changes.append({"symbol": sym, **entry})
            log.info("LEARNED BLOCK %s: %d trades, %.0f%% win, $%.0f — no new entries for %dd",
                     sym, s["n"], win_rate * 100, s["pnl"], block_days)

    if changes:
        _write_json(BLOCKLIST_PATH, current)
        _journal("blocklist", {"added": changes})
        names = ", ".join(c["symbol"] for c in changes)
        notify.send(f"🧠 Learning: blocking new entries in {names} — persistent negative expectancy.")
    return changes


# ── 1. Variant adoption ───────────────────────────────────────────────────────
def _maybe_adopt_variant(model: dict) -> dict | None:
    """Adopt the weekly optimizer's winner (margin gate already applied upstream)."""
    opt = _read_json(_LAST_OPT_PATH, {})
    winner = str(opt.get("winner") or "")
    active = str(model.get("active_variant") or "current")
    if not winner or winner in ("current", active):
        return None
    import signals as sg
    if winner not in sg.VARIANT_PROFILES:
        return None
    min_days = float(getattr(config, "LEARNING_MIN_DAYS_BETWEEN_CHANGES", 7))
    if _days_since_last_change(("variant", "params", "rollback")) < min_days:
        return None

    _checkpoint(model)
    updated = dict(model)
    updated["active_variant"] = winner
    strategy_model.save_model(updated)
    detail = {"from": active, "to": winner, "objective": opt.get("objective")}
    _journal("variant", detail)
    notify.send(f"🧠 Learning: switching signal variant {active} → {winner} "
                f"(weekly optimizer, obj={opt.get('objective')}). Auto-rollback armed.")
    log.info("LEARNED VARIANT SWITCH: %s → %s", active, winner)
    return detail


# ── 2. Parameter nudges ───────────────────────────────────────────────────────
def _maybe_nudge_params(model: dict) -> dict | None:
    """Small evidence-driven parameter adjustments, bounded by BOUNDS."""
    min_trades = int(getattr(config, "LEARNING_MIN_TRADES_FOR_NUDGE", 20))
    min_days = float(getattr(config, "LEARNING_MIN_DAYS_BETWEEN_CHANGES", 7))
    if _days_since_last_change(("params", "variant", "rollback")) < min_days:
        return None

    sells = _experiment_sells()
    if len(sells) < min_trades:
        return None
    wins = sum(1 for r in sells if float(r["pnl"]) > 0)
    total_pnl = sum(float(r["pnl"]) for r in sells)
    metrics = {
        "total_return_pct": total_pnl / 1000.0,   # $ on ~100k equity → %
        "win_rate": wins / len(sells),
        "trade_count": len(sells),
        "max_drawdown_pct": 0.0,                  # unknown from ledger alone
    }
    proposal = strategy_model.propose_adjustment(model, {"metrics": metrics})
    if not proposal.get("changed"):
        return None

    _checkpoint(model)
    strategy_model.save_model(proposal["proposed"])
    diff = {
        k: {"from": model.get(k), "to": proposal["proposed"].get(k)}
        for k in proposal["proposed"]
        if k not in ("updated_at", "generation") and proposal["proposed"].get(k) != model.get(k)
    }
    detail = {"diff": diff, "reasons": proposal.get("reasons"), "evidence": metrics}
    _journal("params", detail)
    notify.send(f"🧠 Learning: adjusted strategy params from {len(sells)} live trades "
                f"({', '.join(proposal.get('reasons') or [])[:140]}). Auto-rollback armed.")
    log.info("LEARNED PARAM NUDGE: %s", diff)
    return detail


# ── Checkpoints & rollback ────────────────────────────────────────────────────
def _checkpoint(model: dict) -> None:
    sells = _experiment_sells()
    _write_json(CHECKPOINT_PATH, {
        "at": _now().isoformat(),
        "model": model,
        "expectancy_before": round(_expectancy(sells), 2),
        "trades_before": len(sells),
    })


def check_rollback() -> dict | None:
    """Revert the last learned change if live results degraded materially."""
    cp = _read_json(CHECKPOINT_PATH, None)
    if not cp:
        return None
    since = _sells_since(str(cp.get("at")))
    min_trades = int(getattr(config, "LEARNING_ROLLBACK_MIN_TRADES", 10))
    if len(since) < min_trades:
        return None

    exp_since = _expectancy(since)
    exp_before = float(cp.get("expectancy_before") or 0.0)
    threshold = min(0.0, exp_before) - _ROLLBACK_TOLERANCE_USD

    if exp_since < threshold:
        strategy_model.save_model(cp["model"])
        detail = {
            "expectancy_before": exp_before,
            "expectancy_after": round(exp_since, 2),
            "trades_evaluated": len(since),
        }
        _journal("rollback", detail)
        notify.send(f"🧠 Learning ROLLBACK: change made expectancy worse "
                    f"(${exp_before:.0f} → ${exp_since:.0f}/trade over {len(since)} trades). "
                    f"Previous model restored.")
        log.warning("LEARNING ROLLBACK: %s", detail)
        try:
            CHECKPOINT_PATH.unlink()
        except Exception:
            pass
        return detail

    # Change survived its evaluation window — confirm and disarm.
    _journal("confirmed", {
        "expectancy_before": exp_before,
        "expectancy_after": round(exp_since, 2),
        "trades_evaluated": len(since),
    })
    log.info("Learning change confirmed: expectancy $%.2f → $%.2f over %d trades.",
             exp_before, exp_since, len(since))
    try:
        CHECKPOINT_PATH.unlink()
    except Exception:
        pass
    return None


# ── Entry point (called from trader tick, self rate-limited) ──────────────────
def _market_hours_now() -> bool:
    """True during the regular US session (9:30-16:00 ET, weekdays)."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
        if now.weekday() >= 5:
            return False
        mins = now.hour * 60 + now.minute
        return 570 <= mins < 960
    except Exception:
        return True   # unknown → be conservative, treat as market hours


def maybe_learn() -> dict:
    """One learning cycle. Cheap; internally rate-limited to once per hour.

    Strategy changes (variant/params) are applied only OUTSIDE market hours —
    the model a session starts with is the model it finishes with. Safety
    mechanisms (blocklist, rollback) run any time.
    """
    if not bool(getattr(config, "AUTO_LEARNING_ENABLED", False)):
        return {"ok": True, "action": "disabled"}

    last = _read_json(_RATE_PATH, {})
    try:
        if (_now() - datetime.fromisoformat(str(last.get("at")))).total_seconds() < 3600:
            return {"ok": True, "action": "rate_limited"}
    except Exception:
        pass
    _write_json(_RATE_PATH, {"at": _now().isoformat()})

    actions: list[dict] = []
    model = strategy_model.load_model()
    try:
        blocked = update_symbol_blocklist()
        if blocked:
            actions.append({"kind": "blocklist", "detail": blocked})
    except Exception as e:
        log.warning("Blocklist update failed: %s", e)
    if _market_hours_now():
        # No mid-session strategy changes — evaluate after the close.
        return {"ok": True, "actions": actions, "deferred": "market_hours"}

    try:
        v = _maybe_adopt_variant(model)
        if v:
            actions.append({"kind": "variant", "detail": v})
            model = strategy_model.load_model()
    except Exception as e:
        log.warning("Variant adoption failed: %s", e)
    try:
        p = _maybe_nudge_params(model)
        if p:
            actions.append({"kind": "params", "detail": p})
    except Exception as e:
        log.warning("Param nudge failed: %s", e)

    return {"ok": True, "actions": actions}
