"""
app.py — Alpaca Paper Trader macOS menu-bar app.

Left-click on icon  → toggle dollar amount (like swing-bot)
Right-click on icon → open full menu
"""
from __future__ import annotations
import base64, json, os, shlex, shutil, subprocess, tempfile, threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import rumps

# ── Paths ─────────────────────────────────────────────────────────────────────
PORTFOLIO_PATH = Path.home() / "Documents" / "paper_trading" / "paper_portfolio.json"
# When running as a py2app bundle, __file__ is inside Contents/Resources/.
# server.py, venv_app, and portfolio_lab.html live in the SOURCE directory.
_bundle_resources = Path(__file__).parent.resolve()
TRADER_DIR = (
    _bundle_resources
    if (_bundle_resources / "server.py").exists()
    else (
        Path.home() / "Code" / "alpaca-trading-app"
        if (Path.home() / "Code" / "alpaca-trading-app" / "server.py").exists()
        else Path.home() / "Code" / "robinhood-trader"
    )
)
LOG_PATH       = TRADER_DIR / "trader.log"
PLIST_SRC      = TRADER_DIR / "com.johnshelest.robinhoodtrader.plist"
PLIST_DEST     = Path.home() / "Library" / "LaunchAgents" / "com.johnshelest.robinhoodtrader.plist"
PLIST_LABEL    = "com.johnshelest.robinhoodtrader"
CONJUR_DIR     = Path(os.environ.get("CONJUR_DIR", str(Path.home() / "Code" / "conjur-secret-manager")))
IVSENTINEL_ENV = Path.home() / "Code" / ".ivsentinel.env"
ET             = ZoneInfo("America/New_York")
START_CAPITAL  = 100_000.0
UI_STATE_FILE  = TRADER_DIR / ".ui_state.json"
FLASK_PORT     = 5001
K8S_SECRET_NAME = os.environ.get("K8S_SECRET_NAME", "alpaca-trader-secrets")
K8S_NAMESPACE   = os.environ.get("K8S_NAMESPACE", "alpaca-trader")


# ── PyObjC click handler — left-click toggles, right-click opens menu ─────────
try:
    import objc
    from Foundation import NSObject

    class _StatusClickHandler(NSObject):
        """Routes left-click → toggle value, right-click → show full menu."""

        def initWithApp_menu_item_(self, app, menu, item):
            self = objc.super(_StatusClickHandler, self).init()
            if self is None:
                return None
            self._py_app  = app
            self._py_menu = menu
            self._py_item = item
            return self

        def handleClick_(self, _sender):
            from AppKit import NSApp, NSEventTypeRightMouseUp
            event = NSApp.currentEvent()
            if event is not None and event.type() == NSEventTypeRightMouseUp:
                self._py_item.setMenu_(self._py_menu)
                self._py_item.button().performClick_(None)
                self._py_item.setMenu_(None)
            else:
                self._py_app.toggle_value(None)

except ImportError:
    _StatusClickHandler = None


# ── Helpers ───────────────────────────────────────────────────────────────────
def _load_portfolio() -> dict | None:
    try:
        return json.loads(PORTFOLIO_PATH.read_text())
    except Exception:
        return None

def _load_ui_state() -> dict:
    try:
        return json.loads(UI_STATE_FILE.read_text())
    except Exception:
        return {}

def _save_ui_state(state: dict) -> None:
    try:
        UI_STATE_FILE.write_text(json.dumps(state))
    except Exception:
        pass

def _load_conjur_env() -> None:
    export_cmd = os.environ.get("CONJUR_EXPORT_CMD", "").strip()
    if export_cmd:
        try:
            proc = subprocess.run(
                ["bash", "-lc", export_cmd],
                capture_output=True, text=True, timeout=15, check=True,
            )
            for line in proc.stdout.splitlines():
                if not line.startswith("export ") or "=" not in line:
                    continue
                key, raw = line[len("export "):].split("=", 1)
                try:
                    val = shlex.split(raw)[0]
                except Exception:
                    val = raw.strip().strip("'\"")
                if key and val:
                    os.environ[key] = val
        except Exception:
            pass

    # Try shutil.which first, then common Homebrew/nvm paths
    npm = (shutil.which("npm")
           or next((p for p in [
               "/opt/homebrew/bin/npm",
               "/usr/local/bin/npm",
               str(Path.home() / ".nvm/versions/node/$(ls ~/.nvm/versions/node | tail -1)/bin/npm"),
           ] if Path(p).exists()), None))
    if not npm or not Path(npm).exists() or not CONJUR_DIR.exists():
        pass
    else:
        try:
            proc = subprocess.run(
                [npm, "run", "--silent", "export"],
                cwd=str(CONJUR_DIR), capture_output=True, text=True, timeout=15, check=True,
            )
            for line in proc.stdout.splitlines():
                if not line.startswith("export ") or "=" not in line:
                    continue
                key, raw = line[len("export "):].split("=", 1)
                try:
                    val = shlex.split(raw)[0]
                except Exception:
                    val = raw.strip().strip("'\"")
                if key and val:
                    os.environ[key] = val
        except Exception:
            pass
    try:
        for line in IVSENTINEL_ENV.read_text().splitlines():
            line = line.strip()
            if line.startswith("export ") and "=" in line:
                key, raw = line[len("export "):].split("=", 1)
                os.environ.setdefault(key.strip(), raw.strip().strip('"\''))
    except Exception:
        pass
    if os.environ.get("ALPACA_PAPER_KEY") and os.environ.get("ALPACA_PAPER_SECRET"):
        return
    kubectl = shutil.which("kubectl")
    if not kubectl:
        return
    try:
        proc = subprocess.run(
            [kubectl, "get", "secret", K8S_SECRET_NAME, "-n", K8S_NAMESPACE, "-o", "json"],
            capture_output=True, text=True, timeout=15, check=True,
        )
        data = (json.loads(proc.stdout).get("data") or {})
        for key in ("ALPACA_PAPER_KEY", "ALPACA_PAPER_SECRET", "GEMINI_API_KEY", "NOTIFY_WEBHOOK_URL"):
            encoded = data.get(key)
            if not encoded:
                continue
            os.environ.setdefault(key, base64.b64decode(encoded).decode("utf-8"))
    except Exception:
        pass

def _is_market_open() -> bool:
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= mins < 15 * 60 + 45

def _trader_is_running() -> bool:
    result = subprocess.run(
        ["launchctl", "list", PLIST_LABEL],
        capture_output=True, text=True,
    )
    return result.returncode == 0

def _hide_dock_icon() -> None:
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory
        )
    except Exception:
        pass

def _start_flask_server() -> None:
    """
    Start server.py as a subprocess using the real Python 3.11 binary.
    We do NOT use the venv symlink because py2app sets PYTHONHOME/__PYVENV_LAUNCHER__
    in the process env, which causes any venv symlink to mis-locate the stdlib.
    Instead we invoke the real interpreter directly and add venv site-packages to
    PYTHONPATH so all installed packages are found.
    """
    source_dir    = TRADER_DIR
    venv_dir      = source_dir / "venv_server"
    server_script = source_dir / "server.py"
    server_log    = source_dir / "server.log"

    # Find the real Python 3.11 binary (not the venv symlink)
    real_python = next(
        (p for p in [
            "/opt/homebrew/bin/python3.11",
            "/opt/homebrew/opt/python@3.11/bin/python3.11",
            "/usr/local/bin/python3.11",
        ] if Path(p).exists()),
        "/opt/homebrew/bin/python3.11",
    )

    # Build site-packages path from the venv
    import sysconfig as _sc
    venv_site = str(venv_dir / "lib" / f"python3.11" / "site-packages")

    def _run() -> None:
        env = os.environ.copy()
        # Strip bundle-injected vars that corrupt stdlib resolution
        for var in ("PYTHONHOME", "__PYVENV_LAUNCHER__", "PYTHONEXECUTABLE"):
            env.pop(var, None)
        # Prepend venv site-packages + source dir so all imports resolve
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{venv_site}:{source_dir}" + (f":{existing}" if existing else "")
        env.setdefault("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
        try:
            with server_log.open("a") as log_fh:
                log_fh.write(f"\n--- starting server with {real_python} ---\n")
                log_fh.flush()
                proc = subprocess.Popen(
                    [real_python, str(server_script)],
                    cwd=str(source_dir),
                    env=env,
                    stdout=log_fh,
                    stderr=log_fh,
                )
                proc.wait()
        except OSError as e:
            print(f"[server] Could not launch Flask subprocess: {e}")
        except Exception as e:
            print(f"[server] Flask subprocess error: {e}")

    threading.Thread(target=_run, daemon=True, name="flask-server").start()


# ── Rumps app ─────────────────────────────────────────────────────────────────
class RobinhoodTraderApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("Alpaca ...", quit_button=None)

        self._ui_state     = _load_ui_state()
        self._show_value   = bool(self._ui_state.get("show_value", True))
        self._last_value: float | None = None
        self._last_daily: float | None = None
        self._last_portfolio: dict | None = None
        self._click_handler = None

        self._status_item    = rumps.MenuItem("Status: starting…",   callback=self._noop)
        self._market_item    = rumps.MenuItem("Market: checking…",   callback=self._noop)
        self._regime_item    = rumps.MenuItem("Regime: —",           callback=self._noop)
        self._value_item     = rumps.MenuItem("Portfolio: —",        callback=self._noop)
        self._cash_item      = rumps.MenuItem("Cash: —",             callback=self._noop)
        self._daily_item     = rumps.MenuItem("Daily P&L: —",        callback=self._noop)
        self._total_item     = rumps.MenuItem("Total P&L: —",        callback=self._noop)
        self._positions_item = rumps.MenuItem("Positions: —",        callback=self._noop)
        self._kill_item      = rumps.MenuItem("Kill switch: 🟢 off", callback=self._noop)
        self._toggle_item    = rumps.MenuItem(self._toggle_label(),   callback=self.toggle_value)
        self._startstop      = rumps.MenuItem("▶︎ Start Trader",       callback=self.toggle_trader)
        self._runnow         = rumps.MenuItem("▶︎ Run Now",            callback=self.run_now)

        self.menu = [
            self._status_item,
            self._market_item,
            self._regime_item,
            None,
            self._value_item,
            self._cash_item,
            self._daily_item,
            self._total_item,
            self._positions_item,
            self._kill_item,
            self._toggle_item,
            None,
            self._startstop,
            self._runnow,
            None,
            rumps.MenuItem("Open Dashboard",      callback=self.open_dashboard),
            rumps.MenuItem("Open Logs",           callback=self.open_logs),
            rumps.MenuItem("Open Portfolio File", callback=self.open_portfolio_file),
            None,
            rumps.MenuItem("Quit Alpaca Paper Trader", callback=self.quit_app),
        ]

        threading.Thread(target=self._do_refresh, daemon=True).start()

    # ── Click-to-toggle handler (installed on first timer tick) ──────────────
    def _install_click_handler(self) -> None:
        if self._click_handler is not None or _StatusClickHandler is None:
            return
        try:
            from AppKit import NSEventMaskLeftMouseUp, NSEventMaskRightMouseUp
            nsapp       = getattr(self, "_nsapp", None)
            status_item = getattr(nsapp, "nsstatusitem", None) if nsapp else None
            if status_item is None:
                return
            button = status_item.button()
            if button is None:
                return
            menu_obj = status_item.menu()
            handler = _StatusClickHandler.alloc().initWithApp_menu_item_(
                self, menu_obj, status_item
            )
            self._click_handler = handler          # strong ref
            status_item.setMenu_(None)             # detach auto-popup
            button.setTarget_(handler)
            button.setAction_("handleClick:")
            button.sendActionOn_(NSEventMaskLeftMouseUp | NSEventMaskRightMouseUp)
        except Exception:
            pass

    # ── Title / toggle ────────────────────────────────────────────────────────
    def _toggle_label(self) -> str:
        return "Hide Value in Menu Bar" if self._show_value else "Show Value in Menu Bar"

    def _render_title(self) -> None:
        if self._last_value is None:
            self.title = "Alpaca ..."
            return
        if not self._show_value:
            self.title = "Alpaca"
            return
        pnl_str = ""
        if self._last_daily is not None:
            pnl_str = f" {self._last_daily:+,.0f}"
        self.title = f"Alpaca ${self._last_value:,.0f}{pnl_str}"

    def toggle_value(self, _) -> None:
        self._show_value = not self._show_value
        self._toggle_item.title = self._toggle_label()
        self._ui_state["show_value"] = self._show_value
        _save_ui_state(self._ui_state)
        self._render_title()

    # ── Refresh — reads from Alpaca via Flask API if server is up ─────────────
    @rumps.timer(30)
    def refresh(self, _) -> None:
        if self._click_handler is None:
            self._install_click_handler()
        threading.Thread(target=self._do_refresh, daemon=True).start()

    def _do_refresh(self) -> None:
        # Try live data from Flask server first; fall back to paper_portfolio.json
        live = self._fetch_live_account()
        running = _trader_is_running()
        market  = _is_market_open()

        self._status_item.title = (
            "Status: ✅ trader running" if running else "Status: ⏸ trader stopped"
        )
        self._market_item.title = "Market: 🟢 open" if market else "Market: 🔴 closed"
        self._startstop.title   = "⏹ Stop Trader" if running else "▶︎ Start Trader"

        if live:
            acct     = live.get("account") or {}
            acctg    = live.get("accounting") or {}
            equity   = float(acct.get("equity") or 0)
            cash     = float(acct.get("cash")   or 0)
            broker_pnl = float(acctg.get("broker_pnl") or 0)
            n_pos    = int(acctg.get("filled_position_count") or 0)
            risk_st  = (acctg.get("risk_state") or {}).get("status", "—")
            regime   = (live.get("live_scores") or {}).get("regime", "—")
            self._last_value       = equity
            self._last_daily       = broker_pnl
            self._value_item.title     = f"Portfolio: ${equity:,.2f}"
            self._cash_item.title      = f"Cash: ${cash:,.2f}"
            self._daily_item.title     = f"Open P&L: {broker_pnl:+,.2f}"
            self._total_item.title     = f"Risk: {risk_st}"
            self._positions_item.title = f"Positions: {n_pos} open"
            self._regime_item.title    = f"Regime: {regime}"
            self._kill_item.title      = "Kill switch: 🟢 off"
        else:
            # Fallback to JSON file
            p = _load_portfolio()
            if p is None:
                self._value_item.title = "Portfolio: no data yet"
                self.title = "🤖 —"
                return
            self._last_portfolio = p
            acct      = p.get("account", {})
            positions = p.get("positions", {})
            total     = float(acct.get("total_value",   START_CAPITAL))
            cash      = float(acct.get("cash",          START_CAPITAL))
            daily_pnl = float(acct.get("daily_pnl",     0))
            daily_pct = float(acct.get("daily_pnl_pct", 0))
            total_pnl = float(acct.get("total_pnl",     0))
            total_pct = float(acct.get("total_pnl_pct", 0))
            kill      = bool(acct.get("kill_switch_triggered", False))
            n_pos     = len(positions)
            strategies = [pos.get("strategy", "—") for pos in positions.values()]
            regime     = strategies[0].split("_")[0] if strategies else "—"
            self._last_value           = total
            self._last_daily           = daily_pnl
            self._value_item.title     = f"Portfolio: ${total:,.2f}"
            self._cash_item.title      = f"Cash: ${cash:,.2f}"
            self._daily_item.title     = f"Daily P&L: {daily_pnl:+,.2f} ({daily_pct:+.2f}%)"
            self._total_item.title     = f"Total P&L: {total_pnl:+,.2f} ({total_pct:+.2f}%)"
            self._positions_item.title = f"Positions: {n_pos}/6 open"
            self._regime_item.title    = f"Regime: {regime}"
            self._kill_item.title      = (
                "⚠️ Kill switch: 🔴 ACTIVE" if kill else "Kill switch: 🟢 off"
            )
        self._render_title()

    def _fetch_live_account(self) -> dict | None:
        """Quick HTTP hit to the local Flask server for live Alpaca data."""
        try:
            import urllib.request
            url = f"http://127.0.0.1:{FLASK_PORT}/api/lab/overview"
            with urllib.request.urlopen(url, timeout=3) as resp:
                return json.loads(resp.read())
        except Exception:
            return None

    # ── Trader control ────────────────────────────────────────────────────────
    def toggle_trader(self, _) -> None:
        if _trader_is_running():
            subprocess.run(["launchctl", "unload", str(PLIST_DEST)], check=False)
            rumps.notification("Alpaca Paper Trader", "Stopped", "Trading loop paused.")
        else:
            if not PLIST_DEST.exists():
                shutil.copy(str(PLIST_SRC), str(PLIST_DEST))
            subprocess.run(["launchctl", "load", str(PLIST_DEST)], check=False)
            rumps.notification("Alpaca Paper Trader", "Started", "Trading loop running every 5 min.")
        threading.Thread(target=self._do_refresh, daemon=True).start()

    def run_now(self, _) -> None:
        def _run():
            subprocess.run(["bash", str(TRADER_DIR / "run.sh")], check=False)
            threading.Thread(target=self._do_refresh, daemon=True).start()
        threading.Thread(target=_run, daemon=True).start()
        rumps.notification("Alpaca Paper Trader", "Running now...", "Check logs for output.")

    # ── Menu actions ──────────────────────────────────────────────────────────
    def open_dashboard(self, _) -> None:
        """Open the live Alpaca dashboard in the default browser."""
        subprocess.run(["open", f"http://127.0.0.1:{FLASK_PORT}/lab"])

    def open_logs(self, _) -> None:
        if LOG_PATH.exists():
            subprocess.run(["open", "-a", "Console", str(LOG_PATH)])
        else:
            subprocess.run(["open", str(TRADER_DIR)])

    def open_portfolio_file(self, _) -> None:
        subprocess.run(["open", str(PORTFOLIO_PATH)])

    def quit_app(self, _) -> None:
        rumps.quit_application()

    def _noop(self, _) -> None:
        pass


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    _load_conjur_env()          # load Alpaca keys from Conjur first
    _hide_dock_icon()
    # Start Flask dashboard server in background (non-blocking)
    threading.Thread(target=_start_flask_server, daemon=True).start()
    RobinhoodTraderApp().run()


if __name__ == "__main__":
    main()
