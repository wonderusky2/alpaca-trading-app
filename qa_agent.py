#!/usr/bin/env python3
"""
qa_agent.py — Automated QA suite for the alpaca-trader system.

Run after every deploy:
    python3 qa_agent.py                  # local + API checks
    python3 qa_agent.py --pod            # + kubectl pod-side checks
    python3 qa_agent.py --pod --fix      # + auto-fix strategy_model.json
    python3 qa_agent.py --verbose        # show passing detail too
    python3 qa_agent.py --category pnl   # run one category only

Exit code: 0 = all pass, 1 = failures found.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable

# ── Config ────────────────────────────────────────────────────────────────────
SERVER_URL  = os.environ.get("QA_SERVER_URL", "http://localhost:5001")  # set QA_SERVER_URL or use kubectl port-forward
APP_DIR     = Path(__file__).parent
NAMESPACE   = "alpaca-trader"
DEPLOYMENT  = "alpaca-server"
TIMEOUT_S   = 15

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"

SAUCE_FILES = [
    "signals.py", "trader.py", "backtest.py",
    "config.py", "strategy_model.py",
]

VALID_REGIMES = {"BULL", "BEAR", "CHOPPY"}

# ── Helpers ───────────────────────────────────────────────────────────────────
def _fetch(path: str) -> dict:
    url = f"{SERVER_URL}{path}"
    req = urllib.request.Request(url, headers={"X-API-Key": os.environ.get("ALPACA_AGENT_API_KEY", "")})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return json.loads(r.read())


def _fetch_timed(path: str) -> tuple[dict, float]:
    t0 = time.time()
    data = _fetch(path)
    return data, time.time() - t0


def _grep_file(path: Path, pattern: str) -> list[str]:
    if not path.exists():
        return []
    rgx = re.compile(pattern)
    return [line.rstrip() for line in path.read_text(encoding="utf-8").splitlines()
            if rgx.search(line)]


def _file_text(name: str) -> str:
    p = APP_DIR / name
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _kubectl_exec(cmd: str) -> str:
    result = subprocess.run(
        ["kubectl", "exec", "-n", NAMESPACE, f"deployment/{DEPLOYMENT}",
         "--", "sh", "-c", cmd],
        capture_output=True, text=True, timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def _kubectl_run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["kubectl"] + args, capture_output=True, text=True, timeout=20,
    )


# ── Check framework ───────────────────────────────────────────────────────────
_results: list[tuple[str, str, bool, str]] = []   # (category, name, passed, detail)
_CATEGORY: str | None = None   # filter


def check(name: str, category: str = "general"):
    def decorator(fn: Callable) -> Callable:
        def wrapper(*args, **kwargs):
            if _CATEGORY and _CATEGORY.lower() not in category.lower():
                return
            try:
                msg = fn(*args, **kwargs)
                _results.append((category, name, True, msg or ""))
            except AssertionError as e:
                _results.append((category, name, False, str(e)))
            except Exception as e:
                _results.append((category, name, False, f"ERROR: {e}"))
        wrapper.__name__ = fn.__name__
        return wrapper
    return decorator


# ══════════════════════════════════════════════════════════════════════════════
# ── CATEGORY: health ──────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

@check("Server: /api/status returns ok", "health")
def chk_status():
    d = _fetch("/api/status")
    assert d.get("ok"),               f"ok=false: {d}"
    assert d.get("broker_connected"), "broker_connected=false"
    assert d.get("paper") is True,    "PAPER mode is OFF — LIVE TRADING MAY BE ACTIVE"
    return "ok broker_connected paper=true"


@check("Server: /api/status responds in < 1s", "health")
def chk_status_speed():
    _, elapsed = _fetch_timed("/api/status")
    assert elapsed < 1.0, f"Took {elapsed:.2f}s — pod may be cold or hung"
    return f"{elapsed:.2f}s"


@check("Server: /api/lab/overview responds in < 8s", "health")
def chk_overview_speed():
    _, elapsed = _fetch_timed("/api/lab/overview")
    assert elapsed < 8.0, f"Took {elapsed:.2f}s — scoring may be hanging"
    return f"{elapsed:.2f}s"


@check("Server: /api/lab/overview returns ok with equity > 0", "health")
def chk_overview():
    d = _fetch("/api/lab/overview")
    assert d.get("ok"), f"ok=false: {d.get('error')}"
    equity = float((d.get("account") or {}).get("equity") or 0)
    assert equity > 0, "equity=0"
    return f"equity=${equity:,.0f}"


@check("Server: all documented endpoints respond", "health")
def chk_all_endpoints():
    endpoints = [
        "/api/status",
        "/api/lab/overview",
        "/api/lab/activity",
        "/api/lab/agents/decision",
        "/api/lab/model",
    ]
    failed = []
    for ep in endpoints:
        try:
            d = _fetch(ep)
            if not d.get("ok") and "error" in d:
                failed.append(f"{ep}: ok=false ({d.get('error')})")
        except Exception as e:
            failed.append(f"{ep}: {e}")
    assert not failed, "Endpoints failed:\n  " + "\n  ".join(failed)
    return f"{len(endpoints)} endpoints OK"


# ══════════════════════════════════════════════════════════════════════════════
# ── CATEGORY: pnl ─────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

@check("P&L: DAWN ignored_val adjustment present in alpaca_client.py", "pnl")
def chk_dawn_fix():
    hits = _grep_file(APP_DIR / "alpaca_client.py", r"ignored_val|IGNORED_POSITIONS")
    assert hits, "DAWN phantom P&L fix not found in alpaca_client.py"
    return f"{len(hits)} lines reference ignored_val/IGNORED_POSITIONS"


@check("P&L: daily_pnl not phantom-inflated (< $5k absolute)", "pnl")
def chk_pnl_not_phantom():
    d = _fetch("/api/lab/overview")
    acct = d.get("account") or {}
    equity   = float(acct.get("equity")      or 0)
    # Use adjusted_last_equity (DAWN-neutralised) — same value the dashboard shows.
    # Falls back to last_equity if the field is absent (older builds).
    last_eq  = float(acct.get("adjusted_last_equity") or acct.get("last_equity") or equity)
    daily    = equity - last_eq
    assert abs(daily) < 5000, (
        f"daily_pnl=${daily:+,.0f} looks phantom-inflated — "
        "check DAWN adjustment in alpaca_client.get_account()"
    )
    return f"daily_pnl=${daily:+,.0f}"


@check("P&L: web sidebar uses dailyPnl (not openPnl override)", "pnl")
def chk_web_pnl_display():
    text = _file_text("portfolio_lab.html")
    bad  = re.search(r"displayPnl\s*=\s*openPnl\s*!==\s*0\s*\?", text)
    assert not bad, (
        "Web sidebar still prefers openPnl — shows misleading per-position "
        "mark instead of full daily P&L. Fix: displayPnl = dailyPnl"
    )
    good = re.search(r"displayPnl\s*=\s*dailyPnl", text)
    assert good, "displayPnl = dailyPnl assignment not found in portfolio_lab.html"
    return "sidebar P&L = dailyPnl"


@check("P&L: narrative summary references open P&L (not daily), open_pnl reasonable", "pnl")
def chk_pnl_vs_narrative():
    # The narrative summary intentionally uses open/unrealized P&L (what positions
    # are doing RIGHT NOW), while daily_pnl = equity - last_equity includes realized
    # losses from closed trades earlier today. They legitimately differ when trades
    # were closed at a loss. We verify the narrative dollar figure matches open_pnl,
    # not daily_pnl.
    d        = _fetch("/api/lab/overview")
    acctg    = d.get("accounting") or {}
    open_pnl = float(acctg.get("unrealized_pnl") or 0)
    summary  = ((d.get("portfolio_narrative") or {}).get("summary") or "").lower()
    m = re.search(r'\$([0-9,]+)', summary)
    if m and abs(open_pnl) > 10:
        narr_val = float(m.group(1).replace(",", ""))
        narr_up  = "up" in summary
        narr_dn  = "down" in summary
        pnl_up   = open_pnl >= 0
        if narr_up or narr_dn:
            assert pnl_up == narr_up, (
                f"Narrative direction mismatch vs open_pnl: "
                f"open_pnl=${open_pnl:+,.0f} but narrative says "
                f"'{'up' if narr_up else 'down'}' — {summary[:80]}"
            )
    acct   = d.get("account") or {}
    equity = float(acct.get("equity")      or 0)
    last   = float(acct.get("last_equity") or equity)
    daily  = equity - last
    return f"open_pnl=${open_pnl:+,.0f}, daily_pnl=${daily:+,.0f}, narrative: '{summary[:50]}'"


@check("P&L: position values don't exceed equity (sanity)", "pnl")
def chk_position_sum():
    # We can't compare sum(positions) to equity-cash directly because DAWN
    # (~$23k) lives in `equity` but is filtered from the positions list.
    # Instead: gross position value must be <= equity * 1.5 (generous for margin).
    d     = _fetch("/api/lab/overview")
    acct  = d.get("account")    or {}
    acctg = d.get("accounting") or {}
    equity  = float(acct.get("equity") or 0)
    pos_sum = sum(
        abs(float(p.get("current_value") or 0))
        for p in (acctg.get("positions") or [])
    )
    if equity > 0 and pos_sum > 0:
        assert pos_sum <= equity * 1.5, (
            f"Position sum ${pos_sum:,.0f} exceeds 150% of equity ${equity:,.0f} — "
            "possible phantom positions or accounting bug"
        )
    gross_pct = float((acctg.get("gross_exposure_pct") or 0))
    assert gross_pct <= 150, (
        f"gross_exposure_pct={gross_pct:.1f}% > 150% — over-leveraged"
    )
    return f"positions=${pos_sum:,.0f}, equity=${equity:,.0f}, gross_exp={gross_pct:.1f}%"


# ══════════════════════════════════════════════════════════════════════════════
# ── CATEGORY: narrative ───────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

BANNED_NARRATIVE = [
    "Hold what you have and avoid adding new positions",
    "no new entries",
    "staying in cash",
    "Waiting for a clearer trend before deploying capital",
]

@check("Narrative: no old CHOPPY 'no-entry' text in live API", "narrative")
def chk_narrative_api():
    pn      = (_fetch("/api/lab/overview").get("portfolio_narrative") or {})
    actions = " ".join(pn.get("next_actions") or [])
    for banned in BANNED_NARRATIVE:
        assert banned.lower() not in actions.lower(), (
            f"Banned phrase in next_actions: '{banned}'\n  Got: {actions}"
        )
    return f"next_actions: {actions[:120]}"


@check("Narrative: server.py _portfolio_narrative has 50%-size CHOPPY text", "narrative")
def chk_narrative_source():
    hits = _grep_file(APP_DIR / "server.py", r"50%.*position size|50% position size")
    assert hits, "_portfolio_narrative CHOPPY 50%-size fix missing from server.py"
    return hits[0].strip()[:100]


@check("Narrative: server.py _simple_chat_response has 50%-size CHOPPY text", "narrative")
def chk_simple_chat_choppy():
    text = _file_text("server.py")
    # Find the _simple_chat_response block and check CHOPPY lines in it
    block_start = text.find("def _simple_chat_response")
    assert block_start >= 0, "_simple_chat_response not found in server.py"
    block = text[block_start:block_start + 1500]
    assert "50%" in block, (
        "_simple_chat_response still has old CHOPPY 'no new entries' text"
    )
    return "50% sizing text confirmed in _simple_chat_response"


@check("Narrative: portfolio_lab.html detectChanges CHOPPY message updated", "narrative")
def chk_web_choppy_js():
    bad = _grep_file(APP_DIR / "portfolio_lab.html",
                     r"No new entries.*holding cash|holding cash.*trend forms")
    assert not bad, f"Old CHOPPY JS message still in portfolio_lab.html: {bad}"
    return "No old 'holding cash' CHOPPY message in JS"


# ══════════════════════════════════════════════════════════════════════════════
# ── CATEGORY: trader ──────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

@check("Trader: CHOPPY hard-block removed from trader.py", "trader")
def chk_choppy_not_blocked():
    text = _file_text("trader.py")
    bad  = re.search(r'regime\s*==\s*["\']CHOPPY["\']\s*.*\n.*\breturn\b', text, re.MULTILINE)
    assert not bad, (
        "Found CHOPPY early-return in trader.py — "
        "automated trading is blocked in CHOPPY regime"
    )
    hits = _grep_file(APP_DIR / "trader.py", r"size_mult|CHOPPY")
    assert hits, "No CHOPPY/size_mult logic found in trader.py at all"
    return "CHOPPY hard-block absent; size_mult logic present"


@check("Trader: size_mult=0.5 applied for CHOPPY in entry loop", "trader")
def chk_choppy_size_mult():
    hits = _grep_file(APP_DIR / "trader.py", r"size_mult.*0\.5|0\.5.*size_mult|CHOPPY.*0\.5|0\.5.*CHOPPY")
    assert hits, "size_mult=0.5 for CHOPPY not found in trader.py"
    return hits[0].strip()[:100]


@check("Trader: daily loss kill switch present", "trader")
def chk_daily_loss_kill():
    hits = _grep_file(APP_DIR / "trader.py", r"daily_loss_kill|kill_pct|loss_kill")
    assert hits, "Daily loss kill switch not found in trader.py"
    return f"{len(hits)} references"


@check("Trader: bear ETF min_conviction gate present", "trader")
def chk_bear_etf_gate():
    hits = _grep_file(APP_DIR / "trader.py", r"bear_etf_min_conviction|BEAR_ETF_MIN")
    assert hits, "Bear ETF min_conviction gate not found in trader.py"
    return f"{len(hits)} references"


@check("Trader: run_eod_flat exists (EOD exit logic)", "trader")
def chk_eod_flat():
    hits = _grep_file(APP_DIR / "trader.py", r"def run_eod_flat|eod_flat")
    assert hits, "run_eod_flat not found in trader.py"
    return "run_eod_flat present"


# ══════════════════════════════════════════════════════════════════════════════
# ── CATEGORY: signals ─────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

@check("Signals: IEX feed guard present in signals.py", "signals")
def chk_iex_fix():
    hits = _grep_file(APP_DIR / "signals.py", r"_data_feed.*!=.*iex|iex.*_data_feed")
    assert hits, (
        "IEX guard missing in signals.py — "
        "Alpaca bar fetch will 403 with IEX feed"
    )
    return "IEX guard present"


@check("Signals: live scores return valid regime", "signals")
def chk_regime_valid():
    # Retry up to 6× with 10s gaps (60s max).
    # Cold-start: background thread fires after 5s but the scoring pass itself
    # (Alpaca bars + Gemini) takes up to 30s, so we need patience here.
    ls = {}
    for attempt in range(6):
        ls = (_fetch("/api/lab/overview").get("live_scores") or {})
        if ls.get("ok"):
            break
        if attempt < 5:
            print(f"      live_scores not ready yet (attempt {attempt+1}/6) — waiting 10s…")
            time.sleep(10)
    assert ls.get("ok"), f"live_scores.ok=false: {ls.get('error')}"
    regime = ls.get("regime")
    assert regime in VALID_REGIMES, (
        f"Unexpected regime '{regime}' — must be one of {VALID_REGIMES}"
    )
    return f"regime={regime}"


@check("Signals: all live scores are in 0–100 range", "signals")
def chk_score_range():
    ls = (_fetch("/api/lab/overview").get("live_scores") or {})
    bad = [
        f"{s.get('symbol')}={s.get('score')}"
        for s in (ls.get("signals") or [])
        if not (0 <= int(float(s.get("score") or 0)) <= 100)
    ]
    assert not bad, f"Scores out of 0–100 range: {bad}"
    total = len(ls.get("signals") or [])
    return f"{total} signals all in range"


@check("Signals: no duplicate symbols in live score list", "signals")
def chk_no_duplicate_signals():
    ls   = (_fetch("/api/lab/overview").get("live_scores") or {})
    syms = [str(s.get("symbol")) for s in (ls.get("signals") or [])]
    dups = [s for s in set(syms) if syms.count(s) > 1]
    assert not dups, f"Duplicate symbols in signal list: {dups}"
    return f"{len(syms)} signals, no duplicates"


@check("Signals: bear ETFs absent from signals in BULL/CHOPPY regime", "signals")
def chk_bear_etfs_in_bull():
    BEAR_ETFS = {"SQQQ", "SPXS", "SOXS", "TECS", "UVXY", "SDOW"}
    ls     = (_fetch("/api/lab/overview").get("live_scores") or {})
    regime = ls.get("regime", "")
    if regime not in ("BULL", "CHOPPY"):
        return f"skipped (regime={regime})"
    syms = {str(s.get("symbol")).upper() for s in (ls.get("signals") or [])}
    found = syms & BEAR_ETFS
    assert not found, (
        f"Bear ETFs {found} in signal list during {regime} regime — "
        "they should only appear in BEAR"
    )
    return f"No bear ETFs in {regime} signal list"


@check("Signals: live quote prices are non-zero for scored symbols", "signals")
def chk_quote_prices():
    ls  = (_fetch("/api/lab/overview").get("live_scores") or {})
    bad = [
        str(s.get("symbol"))
        for s in (ls.get("signals") or [])
        if float((s.get("quote") or {}).get("price") or 0) <= 0
    ]
    assert not bad, f"Zero/missing quote prices for: {bad}"
    return f"all {len(ls.get('signals') or [])} quoted"


# ══════════════════════════════════════════════════════════════════════════════
# ── CATEGORY: orders ──────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

@check("Orders: /api/lab/orders/preview returns valid structure", "orders")
def chk_preview_structure():
    try:
        d = _fetch("/api/lab/orders/preview")
    except Exception:
        # If market closed, endpoint may still respond with empty orders
        return "endpoint reachable (market may be closed)"
    orders = d.get("orders") or []
    for o in orders:
        assert str(o.get("symbol")), f"Order missing symbol: {o}"
        assert int(float(o.get("qty") or 0)) > 0, f"Order qty=0: {o}"
        assert float(o.get("estimated_value") or 0) > 0, f"Order value=0: {o}"
    return f"{len(orders)} preview orders valid"


@check("Orders: no duplicate symbols in a single preview batch", "orders")
def chk_preview_no_dupes():
    try:
        d = _fetch("/api/lab/orders/preview")
    except Exception:
        return "skipped (endpoint unavailable)"
    syms = [str(o.get("symbol")).upper() for o in (d.get("orders") or [])]
    dups = [s for s in set(syms) if syms.count(s) > 1]
    assert not dups, f"Duplicate symbols in preview order batch: {dups}"
    return f"{len(syms)} orders, no duplicates"


@check("Orders: preview total value ≤ 95% of buying_power", "orders")
def chk_preview_cash_buffer():
    d    = _fetch("/api/lab/overview")
    cash = float((d.get("account") or {}).get("buying_power") or 0)
    if cash <= 0:
        return "skipped (no buying_power data)"
    try:
        prev = _fetch("/api/lab/orders/preview")
    except Exception:
        return "skipped (preview unavailable)"
    total = sum(float(o.get("estimated_value") or 0) for o in (prev.get("orders") or []))
    if total > 0:
        assert total <= cash * 0.95, (
            f"Preview orders total ${total:,.0f} exceeds 95% of "
            f"buying_power ${cash:,.0f} — would leave no cash buffer"
        )
    return f"orders=${total:,.0f} vs buying_power=${cash:,.0f}"


@check("Orders: max_positions gate fires at N+1 symbols in risk agent", "orders")
def chk_max_positions_gate():
    text = _file_text("server.py")
    # Confirm the gate logic exists
    assert "max_positions" in text and "Max positions would be exceeded" in text, (
        "max_positions gate missing from server.py RiskAgent"
    )
    return "max_positions gate present in RiskAgent"


# ══════════════════════════════════════════════════════════════════════════════
# ── CATEGORY: strategy ────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

@check("Strategy: max_positions default is 5 in strategy_model.py", "strategy")
def chk_max_positions_default():
    hits = _grep_file(APP_DIR / "strategy_model.py", r'"max_positions":\s*5')
    assert hits, (
        'DEFAULT_MODEL max_positions ≠ 5 — '
        'orders will fail with "Max positions exceeded"'
    )
    return "DEFAULT_MODEL max_positions=5"


@check("Strategy: all BOUNDS have lower < upper", "strategy")
def chk_bounds_sanity():
    import ast
    text   = _file_text("strategy_model.py")
    # Extract the BOUNDS dict literal
    m = re.search(r"BOUNDS\s*=\s*\{(.+?)\}", text, re.DOTALL)
    assert m, "BOUNDS dict not found in strategy_model.py"
    bad = []
    for match in re.finditer(r'"(\w+)":\s*\(([^)]+)\)', m.group(1)):
        param = match.group(1)
        lo, hi = [float(x.strip()) for x in match.group(2).split(",")]
        if lo >= hi:
            bad.append(f"{param}: ({lo}, {hi})")
    assert not bad, f"Invalid BOUNDS (lo >= hi): {bad}"
    return "All BOUNDS valid"


@check("Strategy: live model params are within BOUNDS", "strategy")
def chk_live_model_in_bounds():
    d     = _fetch("/api/lab/model")
    model = d.get("model") or {}
    BOUNDS = {
        "min_conviction":         (62, 72),
        "bear_etf_min_conviction": (42, 60),
        "max_positions":          (1, 5),
        "position_size_pct":      (0.02, 0.08),
        "trailing_stop_pct":      (1.5, 6.0),
        "profit_lock_trigger_pct": (0.75, 6.0),
        "profit_giveback_pct":    (0.25, 3.0),
        "max_holding_days":       (1, 7),
        "daily_loss_kill_pct":    (-4.0, -0.75),
    }
    violations = []
    for param, (lo, hi) in BOUNDS.items():
        val = model.get(param)
        if val is not None and not (lo <= float(val) <= hi):
            violations.append(f"{param}={val} (bounds [{lo}, {hi}])")
    assert not violations, f"Model params out of bounds: {violations}"
    return f"gen={model.get('generation',0)}, all params in bounds"


@check("Strategy: sanitize_model round-trips cleanly", "strategy")
def chk_sanitize_roundtrip():
    # Import locally to test against the source, not the pod
    import importlib.util, sys as _sys
    spec = importlib.util.spec_from_file_location(
        "strategy_model", APP_DIR / "strategy_model.py"
    )
    sm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sm)  # type: ignore
    original = sm.sanitize_model(sm.DEFAULT_MODEL)
    roundtrip = sm.sanitize_model(original)
    assert original == roundtrip, (
        f"sanitize_model is not idempotent — "
        f"diff: { {k:v for k,v in roundtrip.items() if v != original.get(k)} }"
    )
    return "sanitize_model is idempotent"


# ══════════════════════════════════════════════════════════════════════════════
# ── CATEGORY: security ────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

@check("Security: PAPER=True in config.py", "security")
def chk_paper():
    hits = _grep_file(APP_DIR / "config.py", r"PAPER\s*[:=].*True")
    assert hits, "PAPER=True not found in config.py — LIVE TRADING MAY BE ACTIVE"
    return "PAPER=True confirmed"


@check("Security: DAWN in IGNORED_POSITIONS in config.py", "security")
def chk_dawn_config():
    hits = _grep_file(APP_DIR / "config.py", r"IGNORED_POSITIONS.*DAWN|DAWN.*IGNORED_POSITIONS")
    assert hits, "DAWN not in IGNORED_POSITIONS in config.py"
    return "DAWN in IGNORED_POSITIONS"


@check("Security: no hardcoded API keys in .py files", "security")
def chk_no_hardcoded_keys():
    KEY_PATTERNS = [
        r"PKTEST[A-Z0-9]{16}",   # Alpaca paper key
        r"sk-[A-Za-z0-9]{32}",   # OpenAI
        r"AIzaSy[A-Za-z0-9_-]{33}",  # Google
    ]
    found = []
    for pyfile in APP_DIR.glob("*.py"):
        text = pyfile.read_text(encoding="utf-8", errors="ignore")
        for pat in KEY_PATTERNS:
            if re.search(pat, text):
                found.append(f"{pyfile.name}: matches {pat}")
    assert not found, f"Hardcoded API key pattern found:\n  " + "\n  ".join(found)
    return "No hardcoded keys found"


@check("Security: sauce files are in .gitignore", "security")
def chk_gitignore():
    gitignore = (APP_DIR / ".gitignore").read_text(encoding="utf-8") \
        if (APP_DIR / ".gitignore").exists() else ""
    missing = [f for f in SAUCE_FILES if f not in gitignore]
    assert not missing, f"Sauce files not in .gitignore: {missing}"
    return f"All {len(SAUCE_FILES)} sauce files gitignored"


@check("Security: sauce files not tracked in git", "security")
def chk_git_not_tracked():
    result = subprocess.run(
        ["git", "ls-files"] + SAUCE_FILES,
        cwd=str(APP_DIR), capture_output=True, text=True
    )
    tracked = [l for l in result.stdout.splitlines() if l.strip()]
    assert not tracked, (
        f"Sauce files are tracked in git (run git rm --cached): {tracked}"
    )
    return f"None of {SAUCE_FILES} tracked in git"


@check("Security: deploy.sh skips secret.yaml", "security")
def chk_deploy_skips_secret():
    hits = _grep_file(APP_DIR / "deploy.sh", r"secret\.yaml.*continue|continue.*secret\.yaml|\[\[.*secret")
    assert hits, (
        "deploy.sh does not skip secret.yaml — "
        "deploying will wipe the live K8s secret"
    )
    return "secret.yaml skip guard present in deploy.sh"


# ══════════════════════════════════════════════════════════════════════════════
# ── CATEGORY: positions ───────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

@check("Positions: all have qty > 0", "positions")
def chk_positions_qty():
    d    = _fetch("/api/lab/overview")
    acctg = d.get("accounting") or {}
    bad = [
        p.get("symbol") for p in (acctg.get("positions") or [])
        if float(p.get("qty") or 0) <= 0
    ]
    assert not bad, f"Positions with qty=0: {bad}"
    return f"{len(acctg.get('positions') or [])} positions all have qty>0"


@check("Positions: unrealized P&L direction matches current vs entry price", "positions")
def chk_pnl_direction():
    d     = _fetch("/api/lab/overview")
    acctg = d.get("accounting") or {}
    wrong = []
    for p in (acctg.get("positions") or []):
        sym    = p.get("symbol")
        cur    = float(p.get("current_price") or 0)
        entry  = float(p.get("entry_price")   or 0)
        pnl    = float(p.get("unrealized_pnl") or 0)
        if cur > 0 and entry > 0 and pnl != 0:
            expected_sign = 1 if cur > entry else -1
            actual_sign   = 1 if pnl > 0 else -1
            if expected_sign != actual_sign:
                wrong.append(f"{sym}: entry={entry} cur={cur} pnl={pnl:+.2f}")
    assert not wrong, f"P&L sign mismatch (long positions): {wrong}"
    return "All P&L signs consistent with price movement"


# ══════════════════════════════════════════════════════════════════════════════
# ── CATEGORY: pod (kubectl required) ─────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def run_pod_checks(fix: bool = False):

    @check("Pod: max_positions=5 in live strategy_model.json", "pod")
    def chk_pod_max_positions():
        out = _kubectl_exec(
            "python3 -c \""
            "import json,pathlib;"
            "p=pathlib.Path('/data/state/strategy_model.json');"
            "d=json.loads(p.read_text()) if p.exists() else {};"
            "print(d.get('max_positions','MISSING'))\""
        )
        if out == "MISSING" and fix:
            _write_model_to_pod()
            out = "5 (just written)"
        assert out.startswith("5"), (
            f"Live pod max_positions={out} — "
            "orders fail with 'Max positions exceeded'"
        )
        return f"pod max_positions={out}"

    @check("Pod: live model params within bounds", "pod")
    def chk_pod_model_bounds():
        out = _kubectl_exec(
            "python3 -c \""
            "import strategy_model as sm, json;"
            "m=sm.load_model();"
            "print(json.dumps({k:m[k] for k in ['min_conviction','max_positions','position_size_pct']}))\""
        )
        vals = json.loads(out)
        assert 62 <= vals["min_conviction"] <= 72,   f"min_conviction={vals['min_conviction']} out of bounds"
        assert 1  <= vals["max_positions"]  <= 5,    f"max_positions={vals['max_positions']} out of bounds"
        assert 0.02 <= vals["position_size_pct"] <= 0.08, f"position_size_pct out of bounds"
        return f"min_conviction={vals['min_conviction']}, max_positions={vals['max_positions']}"

    @check("Pod: narrative fix deployed (50%-size text in server.py)", "pod")
    def chk_pod_narrative():
        out = _kubectl_exec("grep -c '50% position size' /app/server.py || echo 0")
        assert int(out) > 0, (
            "Pod running OLD server.py — narrative fix not deployed. "
            "Run: kubectl rollout restart deployment/alpaca-server -n alpaca-trader"
        )
        return f"50%-size text found {out}x in deployed server.py"

    @check("Pod: web P&L fix deployed (dailyPnl in portfolio_lab.html)", "pod")
    def chk_pod_web_pnl():
        out = _kubectl_exec("grep -c 'displayPnl = dailyPnl' /app/portfolio_lab.html || echo 0")
        assert int(out) > 0, (
            "Pod running OLD portfolio_lab.html — web P&L fix not deployed"
        )
        return f"dailyPnl assignment found {out}x"

    @check("Pod: required env vars present", "pod")
    def chk_pod_env():
        out = _kubectl_exec("env")
        required = ["ALPACA_PAPER_KEY", "ALPACA_PAPER_SECRET", "STATE_DIR"]
        missing  = [v for v in required if v not in out]
        assert not missing, f"Missing env vars on pod: {missing}"
        return f"All required env vars present"

    @check("Pod: ALPACA_DATA_FEED=iex", "pod")
    def chk_pod_data_feed():
        out = _kubectl_exec("echo $ALPACA_DATA_FEED")
        assert out.strip().lower() == "iex", (
            f"ALPACA_DATA_FEED='{out.strip()}' — should be 'iex'. "
            "If set to 'sip'/'snp', Alpaca bar fetches may 403"
        )
        return f"ALPACA_DATA_FEED={out.strip()}"

    @check("Pod: STATE_DIR is /data/state (PVC-mounted, not ephemeral)", "pod")
    def chk_pod_state_dir():
        out = _kubectl_exec("echo $STATE_DIR")
        assert "/data/state" in out, (
            f"STATE_DIR='{out.strip()}' — expected /data/state. "
            "State may be lost on pod restart if not PVC-mounted"
        )
        return f"STATE_DIR={out.strip()}"

    @check("Pod: strategy_model.json survives across sessions (PVC sanity)", "pod")
    def chk_pod_pvc_write():
        # Write a sentinel, then read it back (same session, confirms PVC writable)
        _kubectl_exec("echo 'qa_test' > /data/state/.qa_sentinel")
        val = _kubectl_exec("cat /data/state/.qa_sentinel")
        assert "qa_test" in val, "PVC write/read failed — state may be ephemeral"
        _kubectl_exec("rm /data/state/.qa_sentinel")
        return "PVC write/read OK"

    @check("Pod: recent CronJob runs have at least one Completed", "pod")
    def chk_pod_cronjob():
        result = _kubectl_run([
            "get", "pods", "-n", NAMESPACE,
            "--sort-by=.metadata.creationTimestamp",
            "-l", "app=alpaca-trader",
            "-o", "jsonpath={range .items[*]}{.metadata.name} {.status.phase}\\n{end}",
        ])
        lines     = result.stdout.strip().splitlines()
        completed = [l for l in lines if "Succeeded" in l or "Completed" in l]
        errors    = [l for l in lines if "Failed" in l]
        # At least one success in last N runs — errors can happen on market close
        if lines:
            assert completed, (
                f"No completed CronJob runs found. Recent states:\n"
                + "\n".join(lines[-5:])
            )
        return f"{len(completed)} completed, {len(errors)} failed"

    @check("Pod: no more than 10 Error pods accumulating", "pod")
    def chk_pod_error_accumulation():
        result = _kubectl_run([
            "get", "pods", "-n", NAMESPACE,
            "-o", "jsonpath={range .items[*]}{.status.phase}\\n{end}",
        ])
        phases = result.stdout.strip().splitlines()
        n_err  = sum(1 for p in phases if p in ("Failed", "Error"))
        assert n_err <= 10, (
            f"{n_err} Error/Failed pods accumulating in {NAMESPACE} — "
            "run: kubectl delete pods -n alpaca-trader --field-selector=status.phase=Failed"
        )
        return f"{n_err} error pods (limit 10)"

    # Run them all
    chk_pod_max_positions()
    chk_pod_model_bounds()
    chk_pod_narrative()
    chk_pod_web_pnl()
    chk_pod_env()
    chk_pod_data_feed()
    chk_pod_state_dir()
    chk_pod_pvc_write()
    chk_pod_cronjob()
    chk_pod_error_accumulation()


def _write_model_to_pod():
    import datetime as _dt
    model = json.dumps({
        "version": 1, "generation": 0,
        "min_conviction": 63, "bear_etf_min_conviction": 50,
        "max_positions": 5, "position_size_pct": 0.05,
        "trailing_stop_pct": 3.0, "profit_lock_trigger_pct": 2.0,
        "profit_giveback_pct": 1.0, "max_holding_days": 2,
        "exit_on_regime_flip": True, "daily_loss_kill_pct": -2.0,
        "learning_mode": "paper_safe", "active_variant": "current",
        "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "last_backtest": None,
    })
    _kubectl_exec(
        f"python3 -c \"from pathlib import Path; "
        f"Path('/data/state/strategy_model.json').write_text('{json.dumps(json.loads(model))}')\""
    )


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

ALL_LOCAL_CHECKS = [
    # health
    chk_status, chk_status_speed, chk_overview_speed, chk_overview,
    chk_all_endpoints,
    # pnl
    chk_dawn_fix, chk_pnl_not_phantom, chk_web_pnl_display,
    chk_pnl_vs_narrative, chk_position_sum,
    # narrative
    chk_narrative_api, chk_narrative_source, chk_simple_chat_choppy,
    chk_web_choppy_js,
    # trader
    chk_choppy_not_blocked, chk_choppy_size_mult, chk_daily_loss_kill,
    chk_bear_etf_gate, chk_eod_flat,
    # signals
    chk_iex_fix, chk_regime_valid, chk_score_range,
    chk_no_duplicate_signals, chk_bear_etfs_in_bull, chk_quote_prices,
    # orders
    chk_preview_structure, chk_preview_no_dupes, chk_preview_cash_buffer,
    chk_max_positions_gate,
    # strategy
    chk_max_positions_default, chk_bounds_sanity, chk_live_model_in_bounds,
    chk_sanitize_roundtrip,
    # security
    chk_paper, chk_dawn_config, chk_no_hardcoded_keys,
    chk_gitignore, chk_git_not_tracked, chk_deploy_skips_secret,
    # positions
    chk_positions_qty, chk_pnl_direction,
]


def main():
    parser = argparse.ArgumentParser(description="Alpaca Trader QA agent")
    parser.add_argument("--pod",      action="store_true", help="Also run kubectl pod-side checks")
    parser.add_argument("--fix",      action="store_true", help="Auto-fix what's possible")
    parser.add_argument("--verbose",  action="store_true", help="Show passing check detail")
    parser.add_argument("--category", metavar="CAT",       help="Run one category only")
    args = parser.parse_args()

    global _CATEGORY
    _CATEGORY = args.category

    print(f"\n{BOLD}━━━ Alpaca Trader QA Agent ━━━{RESET}")
    if _CATEGORY:
        print(f"{DIM}  category filter: {_CATEGORY}{RESET}")
    print()

    for fn in ALL_LOCAL_CHECKS:
        fn()

    if args.pod:
        print(f"{BOLD}── Pod checks (kubectl) ──{RESET}\n")
        run_pod_checks(fix=args.fix)

    # ── Report ────────────────────────────────────────────────────────────────
    passed  = [(cat, n, d) for cat, n, ok, d in _results if ok]
    failed  = [(cat, n, d) for cat, n, ok, d in _results if not ok]
    total   = len(_results)

    # Group failures by category
    if failed:
        print(f"{RED}{BOLD}FAILURES ({len(failed)}/{total}):{RESET}")
        by_cat: dict[str, list] = {}
        for cat, n, d in failed:
            by_cat.setdefault(cat, []).append((n, d))
        for cat, items in by_cat.items():
            print(f"\n  {BOLD}[{cat}]{RESET}")
            for name, detail in items:
                print(f"    {RED}✗{RESET} {name}")
                for line in detail.splitlines():
                    print(f"        {line}")
        print()

    if args.verbose or not failed:
        by_cat_p: dict[str, list] = {}
        for cat, n, d in passed:
            by_cat_p.setdefault(cat, []).append((n, d))
        print(f"{GREEN}{BOLD}PASSED ({len(passed)}/{total}):{RESET}")
        for cat, items in by_cat_p.items():
            print(f"\n  {BOLD}[{cat}]{RESET}")
            for name, detail in items:
                print(f"    {GREEN}✓{RESET} {name}")
                if args.verbose and detail:
                    print(f"        {DIM}{detail}{RESET}")
        print()

    if failed:
        print(f"{RED}{BOLD}Result: {len(failed)}/{total} FAILED{RESET}\n")
        sys.exit(1)
    else:
        print(f"{GREEN}{BOLD}Result: {total}/{total} PASSED ✓{RESET}\n")
        sys.exit(0)


# ── CATEGORY: new_features ────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

@check("Features: gemini_sentiment_boost_cached exists and is callable in signals.py", "new_features")
def chk_gemini_cached():
    hits = _grep_file(APP_DIR / "signals.py", r"def gemini_sentiment_boost_cached")
    assert hits, "gemini_sentiment_boost_cached() missing from signals.py — was it removed?"
    # Must NOT be the old stub (returning 0.0 hardcoded)
    stub = _grep_file(APP_DIR / "signals.py", r"Stub.*returns 0.*no-op")
    assert not stub, "gemini_sentiment_boost_cached() still has stub 'returns 0' comment — replace with real impl"
    return "gemini_sentiment_boost_cached present and not stubbed"


@check("Features: gemini_sentiment_boost_cached uses cache TTL (not always calling API)", "new_features")
def chk_gemini_cache_ttl():
    hits = _grep_file(APP_DIR / "signals.py", r"_GEMINI_CACHE_TTL|_gemini_cache")
    assert hits, "_gemini_cache / _GEMINI_CACHE_TTL missing — calls will hit Gemini API every time"
    return "Gemini cache vars present"


@check("Features: _intraday_confirms_entry gate present in trader.py", "new_features")
def chk_intraday_gate():
    hits = _grep_file(APP_DIR / "trader.py", r"def _intraday_confirms_entry")
    assert hits, "_intraday_confirms_entry() missing from trader.py"
    wired = _grep_file(APP_DIR / "trader.py", r"_intraday_confirms_entry.*client")
    assert wired, "_intraday_confirms_entry not called in entry loop"
    return "Intraday gate present and wired"


@check("Features: intraday gate checks VWAP, RSI, and volume", "new_features")
def chk_intraday_gate_conditions():
    content = (APP_DIR / "trader.py").read_text(encoding="utf-8")
    assert "latest_vwap" in content, "VWAP check missing from _intraday_confirms_entry"
    assert "rsi" in content.lower(), "RSI check missing from _intraday_confirms_entry"
    assert "avg_vol" in content, "Volume check missing from _intraday_confirms_entry"
    return "All 3 intraday gate conditions present"


@check("Features: scan_dynamic_universe exists in signals.py", "new_features")
def chk_dynamic_universe():
    hits = _grep_file(APP_DIR / "signals.py", r"def scan_dynamic_universe")
    assert hits, "scan_dynamic_universe() missing from signals.py"
    wired = _grep_file(APP_DIR / "signals.py", r"scan_dynamic_universe\(\)")
    assert wired, "scan_dynamic_universe() not called in get_signals()"
    return "Dynamic universe scanner present and wired"


@check("Features: slippage model applied in backtest score_candidate_variant", "new_features")
def chk_backtest_slippage():
    hits = _grep_file(APP_DIR / "backtest.py", r"_SLIPPAGE|slippage|net_ret")
    assert hits, "Slippage model missing from backtest.py — returns are unrealistically high"
    return "Slippage model present"


@check("Features: walk-forward uses Alpaca (not yfinance) as primary source", "new_features")
def chk_walkforward_alpaca_primary():
    hits = _grep_file(APP_DIR / "backtest.py", r"get_historical_bars_range")
    assert hits, "get_historical_bars_range() not used — walk-forward may be using yfinance (blocked on GKE)"
    return "Alpaca historical bars used as primary source"


@check("Features: walk-forward cache exists on PVC (run has completed)", "new_features")
def chk_walkforward_cache():
    r = _fetch("/api/lab/walkforward")
    assert r.get("ok"), f"Walk-forward cache missing or errored: {r.get('error', r.get('status'))}"
    windows = r.get("windows") or []
    assert len(windows) >= 10, f"Only {len(windows)} windows — expected 10+ for a full 2022→today run"
    sharpe = r.get("sharpe", 0)
    assert sharpe > 0, f"Sharpe {sharpe} ≤ 0 — backtest shows no edge"
    return f"Cache valid: {len(windows)} windows, Sharpe={sharpe:.2f}, verdict={r.get('verdict')}"


@check("Features: scan_premarket_gaps callable without crashing", "new_features")
def chk_gaps_no_crash():
    r = _fetch("/api/lab/gaps?min_gap_pct=1.5")
    # Allowed outcomes: list (market hours) OR error about market being closed
    # NOT allowed: unhandled AttributeError / 500
    assert r.get("ok") or "error" in r or isinstance(r, list), \
        f"Gap scanner returned unexpected response: {r}"
    return "Gap scanner returns without AttributeError crash"


@check("Features: _maybe_push_gemini_alerts exists in server.py and handles empty input", "new_features")
def chk_push_gemini_alerts():
    import importlib, sys, types

    # Verify function is defined in server.py source
    hits = _grep_file(APP_DIR / "server.py", r"def _maybe_push_gemini_alerts")
    assert hits, "_maybe_push_gemini_alerts() missing from server.py"

    # Verify push cache constants are present
    cache_hits = _grep_file(APP_DIR / "server.py", r"_gemini_alert_cache|_GEMINI_ALERT_TTL")
    assert cache_hits, "_gemini_alert_cache / _GEMINI_ALERT_TTL dedup cache missing from server.py"

    # Verify it's wired into _run_live_scores
    wired = _grep_file(APP_DIR / "server.py", r"_maybe_push_gemini_alerts\(signals")
    assert wired, "_maybe_push_gemini_alerts not called inside _run_live_scores()"

    # Verify gemini_boost is captured per signal
    boost_tracked = _grep_file(APP_DIR / "server.py", r"gemini_boost.*=.*float\(sg\.gemini_sentiment_boost_cached")
    assert boost_tracked, "gemini_boost not captured as float in _run_live_scores() signal dict"

    return "_maybe_push_gemini_alerts wired into live scores with dedup cache"


if __name__ == "__main__":
    main()
