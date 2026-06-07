import Foundation
import Combine
@preconcurrency import UserNotifications

@MainActor
class AgentViewModel: ObservableObject {
    @Published var messages:  [ChatMessage] = []
    @Published var overview:  OverviewData  = OverviewData()
    @Published var isThinking = false
    @Published var inputText  = ""
    @Published var pendingTrade: PendingTradeProposal?
    @Published var isPlacingOrder = false
    @Published var autoExecuteOrders = true
    @Published var lastOverviewAt: Date?
    @Published var selectedPortfolioRange: PortfolioRange = .day
    @Published var portfolioPoints: [PortfolioPoint] = []
    @Published var portfolioRangePnl: Double = 0
    @Published var portfolioRangePnlPct: Double = 0
    @Published var isLoadingPortfolioHistory = false
    @Published var portfolioHistoryError: String?
    @Published var positions: [PositionRow] = []
    @Published var exitRecommendations: [ExitRecommendation] = []
    @Published var decision = AgentDecision()
    @Published var portfolioNarrative = PortfolioNarrative()
    @Published var strategyModel = StrategyModelState()
    @Published var signalInsights: [SignalInsight] = []
    @Published var activityItems: [ActivityLogItem] = []
    @Published var isRefreshingActivity = false

    private var pollTask: Task<Void, Never>?
    private var prevRegime: String = ""
    private var prevPosCount: Int  = -1
    private var initialized        = false

    init() {
        startPolling()
        Task {
            await fetchPortfolioHistory(for: selectedPortfolioRange)
            await fetchActivity()
        }
    }

    // ── Notifications ─────────────────────────────────────────────────────────
    private func requestNotificationPermission() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
    }

    private func notify(title: String, body: String, sound: UNNotificationSound = .default) {
        UNUserNotificationCenter.current().getNotificationSettings { settings in
            guard settings.authorizationStatus == .authorized ||
                    settings.authorizationStatus == .provisional ||
                    settings.authorizationStatus == .ephemeral else { return }

            let content = UNMutableNotificationContent()
            content.title = title
            content.body  = body
            content.sound = sound
            let req = UNNotificationRequest(
                identifier: UUID().uuidString,
                content: content,
                trigger: nil
            )
            UNUserNotificationCenter.current().add(req, withCompletionHandler: nil)
        }
    }
    deinit { pollTask?.cancel() }

    // ── Polling ───────────────────────────────────────────────────────────────
    func startPolling() {
        pollTask?.cancel()
        pollTask = Task {
            while !Task.isCancelled {
                await fetchOverview()
                try? await Task.sleep(nanoseconds: 30_000_000_000)
            }
        }
    }

    private func fetchOverview() async {
        do {
            let (data, _) = try await URLSession.shared.data(for: Config.request("/api/lab/overview"))
            guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let ok = json["ok"] as? Bool, ok else { return }

            let acct   = json["account"]    as? [String: Any] ?? [:]
            let acctg  = json["accounting"] as? [String: Any] ?? [:]
            let clock  = json["clock"]      as? [String: Any] ?? [:]
            let scores = json["live_scores"] as? [String: Any] ?? [:]
            let decisionJson = json["decision"] as? [String: Any] ?? [:]
            let narrativeJson = json["portfolio_narrative"] as? [String: Any] ?? [:]
            let modelJson = json["model"] as? [String: Any] ?? [:]
            let newsRisks = json["news_risk"] as? [[String: Any]] ?? []

            let eq      = anyDouble(acct["equity"])
            let lastEq  = anyDouble(acct["last_equity"])
            let cash    = anyDouble(acct["cash"])
            let openPnl = anyDouble(acctg["unrealized_pnl"])
            let posCount = anyInt(acctg["filled_position_count"])
            let positionRows = acctg["positions"] as? [[String: Any]] ?? []
            let exitRows = acctg["exit_recommendations"] as? [[String: Any]] ?? []
            let regime  = scores["regime"] as? String ?? "CHOPPY"
            let isOpen  = clock["is_open"] as? Bool ?? false
            var nextOpenStr = ""
            if let nextOpenISO = clock["next_open"] as? String,
               let nextDate = ISO8601DateFormatter().date(from: nextOpenISO) {
                let fmt = DateFormatter()
                fmt.dateFormat = "EEE h:mm a"
                nextOpenStr = fmt.string(from: nextDate)
            }

            overview = OverviewData(
                equity: eq, lastEquity: lastEq, cash: cash,
                posCount: posCount, openPnl: openPnl,
                regime: regime, isOpen: isOpen, nextOpen: nextOpenStr
            )
            positions = positionRows.map(parsePosition)
            exitRecommendations = exitRows.map(parseExitRecommendation)
            decision = parseDecision(decisionJson)
            strategyModel = parseStrategyModel(modelJson)
            signalInsights = parseSignalInsights(scores)
            portfolioNarrative = parsePortfolioNarrative(narrativeJson)
            lastOverviewAt = Date()

            if !initialized {
                postGreeting(regime: regime, isOpen: isOpen, equity: eq,
                             pnl: eq - lastEq, posCount: posCount, newsRisks: newsRisks)
                prevRegime   = regime
                prevPosCount = posCount
                initialized  = true
            } else {
                detectChanges(regime: regime, posCount: posCount,
                              openPnl: openPnl, newsRisks: newsRisks)
            }
        } catch { /* silent retry */ }
    }

    private func postGreeting(regime: String, isOpen: Bool, equity: Double,
                               pnl: Double, posCount: Int, newsRisks: [[String: Any]]) {
        let mktStr  = isOpen ? "open" : "closed"
        let pnlSign = pnl >= 0 ? "+" : ""
        var text = "Agent online. Market \(mktStr). Equity \(fmt$(equity)), daily P&L \(pnlSign)\(fmt$(abs(pnl))). Regime: \(regime). "
        text += posCount > 0 ? "\(posCount) holding\(posCount > 1 ? "s" : "") open." : "No exposure — cash ready."
        addMessage(ChatMessage(text, role: .agent))

        for nr in newsRisks {
            if let sym = nr["symbol"] as? String, let hed = nr["headline"] as? String {
                addMessage(ChatMessage("⚠ News risk on \(sym): \"\(hed)\"", role: .agent, variant: .alert))
                notify(title: "⚠ News Risk: \(sym)", body: hed)
            }
        }

        if isOpen && regime != "CHOPPY" && posCount == 0 {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) {
                self.addMessage(ChatMessage("\(regime) regime, no exposure. Agent can scan for entries.", role: .agent, variant: .trade))
            }
        }
    }

    private func detectChanges(regime: String, posCount: Int,
                                openPnl: Double, newsRisks: [[String: Any]]) {
        if regime != prevRegime && !prevRegime.isEmpty {
            let msgs: [String: String] = [
                "BULL":   "Regime → BULL. Momentum active — running signal scan.",
                "BEAR":   "Regime → BEAR. Inverse ETFs now eligible.",
                "CHOPPY": "Regime → CHOPPY. Going to cash — no new entries."
            ]
            let variants: [String: MessageVariant] = ["BULL": .trade, "BEAR": .danger, "CHOPPY": .alert]
            if let text = msgs[regime] {
                addMessage(ChatMessage(text, role: .agent, variant: variants[regime] ?? .normal))
                notify(title: "Regime Change", body: text)
            }
            prevRegime = regime
        }

        if prevPosCount >= 0 && posCount != prevPosCount {
            if posCount > prevPosCount {
                let n = posCount - prevPosCount
                let msg = "\(n) holding\(n > 1 ? "s" : "") opened. \(posCount) total."
                addMessage(ChatMessage(msg, role: .agent, variant: .trade))
                notify(title: "Trade Executed", body: msg, sound: .defaultCritical)
            } else if prevPosCount > 0 {
                let sign = openPnl >= 0 ? "+" : ""
                let msg = "Holding closed. Open P&L: \(sign)\(fmt$(abs(openPnl)))"
                addMessage(ChatMessage(msg, role: .agent))
                notify(title: "Position Closed", body: msg)
            }
            prevPosCount = posCount
        }
    }

    func send() {
        let text = inputText.trimmingCharacters(in: .whitespaces)
        guard !text.isEmpty else { return }
        inputText = ""
        submit(text, visibleUserMessage: true)
    }

    func quickSend(_ text: String) {
        submit(text, visibleUserMessage: false)
    }

    func refresh() {
        refreshAll()
    }

    func refreshAll() {
        Task {
            await fetchOverview()
            await fetchPortfolioHistory(for: selectedPortfolioRange)
            await fetchActivity()
        }
    }

    func refreshActivity() {
        Task { await fetchActivity() }
    }

    func selectPortfolioRange(_ range: PortfolioRange) {
        guard range != selectedPortfolioRange else { return }
        selectedPortfolioRange = range
        Task { await fetchPortfolioHistory(for: range) }
    }

    private func fetchPortfolioHistory(for range: PortfolioRange) async {
        isLoadingPortfolioHistory = true
        portfolioHistoryError = nil
        defer { isLoadingPortfolioHistory = false }

        let requestRange = historyRequest(for: range)

        do {
            var request = Config.request("/api/lab/portfolio/history")
            var components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!
            components.queryItems = [
                URLQueryItem(name: "period", value: requestRange.period),
                URLQueryItem(name: "timeframe", value: requestRange.timeframe)
            ]
            request.url = components.url

            let (data, _) = try await URLSession.shared.data(for: request)
            guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let ok = json["ok"] as? Bool, ok,
                  let history = json["history"] as? [String: Any] else {
                portfolioHistoryError = "History unavailable"
                portfolioPoints = []
                return
            }

            let timestamps = history["timestamp"] as? [Any] ?? []
            let equities = history["equity"] as? [Any] ?? []
            let count = min(timestamps.count, equities.count)
            let points = (0..<count).compactMap { index -> PortfolioPoint? in
                guard let date = anyDate(timestamps[index]) else { return nil }
                let equity = anyDouble(equities[index])
                guard equity > 0 else { return nil }
                return PortfolioPoint(time: date, equity: equity)
            }

            portfolioPoints = points

            if let first = points.first?.equity, let last = points.last?.equity {
                portfolioRangePnl = last - first
            } else {
                portfolioRangePnl = 0
            }

            if let first = points.first?.equity, first > 0, let last = points.last?.equity {
                portfolioRangePnlPct = (last - first) / first * 100
            } else {
                portfolioRangePnlPct = 0
            }
        } catch {
            portfolioHistoryError = error.localizedDescription
            portfolioPoints = []
        }
    }

    private func fetchActivity() async {
        isRefreshingActivity = true
        defer { isRefreshingActivity = false }

        do {
            let (data, _) = try await URLSession.shared.data(for: Config.request("/api/lab/activity"))
            guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let ok = json["ok"] as? Bool, ok else { return }

            let orders = json["recent_orders"] as? [[String: Any]] ?? []
            let events = json["events"] as? [[String: Any]] ?? []
            var items = events.map(parseEvent) + orders.map(parseOrder)
            items.sort { lhs, rhs in
                (lhs.time ?? .distantPast) > (rhs.time ?? .distantPast)
            }
            activityItems = Array(items.prefix(30))
        } catch {
            let item = ActivityLogItem(
                title: "Activity refresh failed",
                detail: error.localizedDescription,
                time: Date(),
                variant: .danger
            )
            activityItems = [item] + activityItems
        }
    }

    private func submit(_ text: String, visibleUserMessage: Bool) {
        let text = text.trimmingCharacters(in: .whitespaces)
        guard !text.isEmpty else { return }
        if visibleUserMessage {
            addMessage(ChatMessage(text, role: .user))
        } else {
            addMessage(ChatMessage("Requested: \(text)", role: .agent))
        }
        isThinking = true

        Task {
            defer { isThinking = false }
            do {
                let body: [String: Any] = ["text": text, "auto_execute": false]
                let (data, _) = try await URLSession.shared.data(for: Config.request("/api/lab/chat/message", method: "POST", body: body))
                guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }

                if let proposal = json["trade_proposal"] as? [String: Any] {
                    prepareTrade(proposal: proposal)
                } else if let reply = json["reply"] as? String {
                    let variant = variantFrom(json["variant"] as? String)
                    addMessage(ChatMessage(reply, role: .agent, variant: variant))
                }
                await fetchOverview()
            } catch {
                addMessage(ChatMessage("Error: \(error.localizedDescription)", role: .agent, variant: .danger))
            }
        }
    }

    private func prepareTrade(proposal: [String: Any]) {
        let orders = proposal["orders"] as? [[String: Any]] ?? []
        guard !orders.isEmpty else { return }

        let parsedOrders = orders.map { o -> TradeOrder in
            let side   = (o["side"] as? String ?? "?").uppercased()
            let qty    = o["qty"] as? Int ?? (Int(o["qty"] as? String ?? "0") ?? 0)
            let symbol = o["symbol"] as? String ?? "?"
            return TradeOrder(side: side, quantity: qty, symbol: symbol, raw: o)
        }
        let summary = proposal["summary"] as? String ?? "Review the generated order before placing it."
        let pending = PendingTradeProposal(orders: parsedOrders, summary: summary)

        if autoExecuteOrders {
            addMessage(ChatMessage("Auto mode accepted order: \(pending.orderDescription)", role: .agent, variant: .trade))
            Task { await placeTrade(proposal: pending) }
        } else {
            pendingTrade = pending
            addMessage(ChatMessage("Order ready for review: \(pending.orderDescription)", role: .agent, variant: .alert))
        }
    }

    func cancelPendingTrade() {
        if let pendingTrade {
            addMessage(ChatMessage("Order canceled: \(pendingTrade.orderDescription)", role: .agent))
        }
        pendingTrade = nil
    }

    func confirmPendingTrade() {
        guard let proposal = pendingTrade, !proposal.orders.isEmpty else { return }
        isPlacingOrder = true

        Task {
            await placeTrade(proposal: proposal)
            isPlacingOrder = false
        }
    }

    private func placeTrade(proposal: PendingTradeProposal) async {
        let orders = proposal.orders.map(\.raw)
        let desc = proposal.orderDescription

        addMessage(ChatMessage("Placing order: \(desc)", role: .agent, variant: .trade))

        do {
            let body: [String: Any] = ["orders": orders]
            let (data, _) = try await URLSession.shared.data(for: Config.request("/api/lab/orders/place", method: "POST", body: body))
            if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let ok = json["ok"] as? Bool, ok {
                let msg = "✓ Executed: \(desc)"
                addMessage(ChatMessage(msg, role: .agent, variant: .trade))
                pendingTrade = nil
                notify(title: "Order Placed", body: desc, sound: .defaultCritical)
            } else {
                let err = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["errors"] as? [[String: Any]]
                let msg = err?.first?["error"] as? String ?? "unknown error"
                addMessage(ChatMessage("Order failed: \(msg)", role: .agent, variant: .danger))
            }
        } catch {
            addMessage(ChatMessage("Order error: \(error.localizedDescription)", role: .agent, variant: .danger))
        }
    }

    // ── Helpers ───────────────────────────────────────────────────────────────
    private func addMessage(_ msg: ChatMessage) {
        messages.append(msg)
    }

    private func variantFrom(_ s: String?) -> MessageVariant {
        switch s {
        case "trade":  return .trade
        case "alert", "warning": return .alert
        case "danger": return .danger
        default:       return .normal
        }
    }

    private func fmt$(_ n: Double) -> String {
        let formatter = NumberFormatter()
        formatter.numberStyle = .currency
        formatter.currencySymbol = "$"
        formatter.maximumFractionDigits = 0
        return formatter.string(from: NSNumber(value: n)) ?? "$\(Int(n))"
    }

    private func historyRequest(for range: PortfolioRange) -> (period: String, timeframe: String) {
        switch range {
        case .day:
            return ("1D", "5Min")
        case .week:
            return ("7D", "15Min")
        case .month:
            return ("1M", "1D")
        case .quarter:
            return ("3M", "1D")
        case .ytd:
            return ("1A", "1D")
        case .custom:
            return ("1A", "1D")
        }
    }

    private func parsePosition(_ row: [String: Any]) -> PositionRow {
        PositionRow(
            symbol: anyString(row["symbol"], fallback: "?"),
            side: anyString(row["side"], fallback: "LONG"),
            status: anyString(row["status"], fallback: "--"),
            theme: anyString(row["theme"], fallback: "Position"),
            qty: anyDouble(row["qty"]),
            entryPrice: anyDouble(row["entry_price"]),
            currentPrice: anyDouble(row["current_price"]),
            currentValue: anyDouble(row["current_value"]),
            currentWeightPct: anyDouble(row["current_weight_pct"]),
            unrealizedPnl: anyDouble(row["unrealized_pnl"]),
            unrealizedPnlPct: anyDouble(row["unrealized_pnl_pct"]),
            action: anyString(row["action"], fallback: "hold"),
            reason: anyString(row["reason"], fallback: "")
        )
    }

    private func parseExitRecommendation(_ row: [String: Any]) -> ExitRecommendation {
        ExitRecommendation(
            symbol: anyString(row["symbol"], fallback: "?"),
            side: anyString(row["side"], fallback: "sell"),
            quantity: anyDouble(row["qty"]),
            reason: anyString(row["reason"], fallback: "exit"),
            unrealizedPnl: anyDouble(row["unrealized_pnl"]),
            unrealizedPnlPct: anyDouble(row["unrealized_pnl_pct"]),
            peakUnrealizedPnlPct: anyDouble(row["peak_unrealized_pnl_pct"]),
            givebackPct: anyDouble(row["giveback_pct"]),
            holdingDays: anyInt(row["holding_days"])
        )
    }

    private func parseDecision(_ row: [String: Any]) -> AgentDecision {
        AgentDecision(
            action: anyString(row["action"], fallback: "wait"),
            severity: variantFrom(anyString(row["severity"], fallback: "normal")),
            summary: anyString(row["summary"], fallback: "Waiting for agent state."),
            riskStatus: anyString(row["risk_status"], fallback: "--"),
            riskAction: anyString(row["risk_action"], fallback: "--")
        )
    }

    private func parsePortfolioNarrative(_ row: [String: Any]) -> PortfolioNarrative {
        if row.isEmpty {
            return fallbackPortfolioNarrative()
        }
        return PortfolioNarrative(
            sentimentFrom: anyString(row["sentiment_from"], fallback: "market"),
            sentimentTo: anyString(row["sentiment_to"], fallback: overview.regime),
            summary: anyString(row["summary"], fallback: fallbackPortfolioNarrative().summary),
            why: row["why"] as? [String] ?? [],
            nextActions: row["next_actions"] as? [String] ?? [],
            modelAdjustment: anyString(row["model_adjustment"], fallback: modelLine)
        )
    }

    private func fallbackPortfolioNarrative() -> PortfolioNarrative {
        let firstExit = exitRecommendations.first
        let topSignal = signalInsights.first
        let summary: String
        if let firstExit {
            summary = "Market posture is \(overview.regime). The agent is prioritizing capital protection because \(firstExit.symbol) hit \(cleanReason(firstExit.reason).lowercased())."
        } else if let topSignal {
            summary = "Market posture is \(overview.regime). The agent is scanning for portfolio rotation because \(topSignal.symbol) is the strongest current setup."
        } else {
            summary = decision.summary
        }

        var why: [String] = []
        if let firstExit {
            why.append("\(firstExit.symbol): \(cleanReason(firstExit.reason)), now \(signedPercentText(firstExit.unrealizedPnlPct)), peak \(signedPercentText(firstExit.peakUnrealizedPnlPct)), giveback \(String(format: "%.1f", firstExit.givebackPct))%.")
        }
        if let topSignal {
            let reasons = topSignal.reasons.prefix(2).joined(separator: " | ")
            let detail = reasons.isEmpty
                ? "\(topSignal.symbol): score \(topSignal.score), \(topSignal.trendDirection) trend, AVWAP \(signedPercentText(topSignal.priceVsAvwapLowPct))."
                : "\(topSignal.symbol): score \(topSignal.score), \(reasons)."
            why.append(detail)
        }
        if why.isEmpty {
            why.append(decision.summary)
        }

        var next: [String] = []
        if let firstExit {
            next.append("Sell \(formatQuantity(firstExit.quantity)) \(firstExit.symbol) if the risk gate allows; do not let the loser sit.")
        }
        if let topSignal, firstExit == nil {
            next.append("Prepare buy candidate \(topSignal.symbol) only if conviction stays above \(strategyModel.minConviction) and price confirms trend.")
        }
        if next.isEmpty {
            next.append(decision.action.replacingOccurrences(of: "_", with: " ").capitalized)
        }

        return PortfolioNarrative(
            sentimentFrom: "market",
            sentimentTo: overview.regime,
            summary: summary,
            why: why,
            nextActions: next,
            modelAdjustment: modelLine
        )
    }

    private var modelLine: String {
        "Model G\(strategyModel.generation): conviction \(strategyModel.minConviction)+, max hold \(strategyModel.maxHoldingDays)d, giveback stop \(String(format: "%.1f", strategyModel.profitGivebackPct))%."
    }

    private func cleanReason(_ value: String) -> String {
        value.replacingOccurrences(of: "_", with: " ").capitalized
    }

    private func signedPercentText(_ value: Double) -> String {
        let prefix = value >= 0 ? "+" : "-"
        return "\(prefix)\(String(format: "%.1f", abs(value)))%"
    }

    private func parseStrategyModel(_ row: [String: Any]) -> StrategyModelState {
        let defaults = StrategyModelState()
        return StrategyModelState(
            generation: row["generation"] == nil ? defaults.generation : anyInt(row["generation"]),
            minConviction: row["min_conviction"] == nil ? defaults.minConviction : anyInt(row["min_conviction"]),
            maxPositions: row["max_positions"] == nil ? defaults.maxPositions : anyInt(row["max_positions"]),
            positionSizePct: row["position_size_pct"] == nil ? defaults.positionSizePct : anyDouble(row["position_size_pct"]),
            trailingStopPct: row["trailing_stop_pct"] == nil ? defaults.trailingStopPct : anyDouble(row["trailing_stop_pct"]),
            profitLockTriggerPct: row["profit_lock_trigger_pct"] == nil ? defaults.profitLockTriggerPct : anyDouble(row["profit_lock_trigger_pct"]),
            profitGivebackPct: row["profit_giveback_pct"] == nil ? defaults.profitGivebackPct : anyDouble(row["profit_giveback_pct"]),
            maxHoldingDays: row["max_holding_days"] == nil ? defaults.maxHoldingDays : anyInt(row["max_holding_days"]),
            exitOnRegimeFlip: row["exit_on_regime_flip"] as? Bool ?? true
        )
    }

    private func parseSignalInsights(_ row: [String: Any]) -> [SignalInsight] {
        let rows = (row["top"] as? [[String: Any]]) ?? (row["signals"] as? [[String: Any]]) ?? []
        return rows.prefix(5).map { item in
            let quote = item["quote"] as? [String: Any] ?? [:]
            let technicals = (item["technicals"] as? [String: Any]) ?? (quote["technicals"] as? [String: Any]) ?? [:]
            return SignalInsight(
                symbol: anyString(item["symbol"], fallback: "?"),
                score: anyInt(item["score"]),
                regime: anyString(item["regime"], fallback: overview.regime),
                changePct: anyDouble(quote["change_pct"]),
                rsi14: anyDouble(technicals["rsi14"]),
                macdHist: anyDouble(technicals["macd_hist"]),
                emaTrend: anyString(technicals["ema_trend"], fallback: "--"),
                priceVsVwapPct: anyDouble(technicals["price_vs_vwap_pct"]),
                priceVsAvwapLowPct: anyDouble(technicals["price_vs_avwap_low_pct"]),
                volumeRatio: anyDouble(technicals["volume_ratio"]),
                trendDirection: anyString(technicals["trend_direction"], fallback: "--"),
                priceVsTrendPct: anyDouble(technicals["price_vs_trend_pct"]),
                fibPosition: anyString(technicals["fib_position"], fallback: "--"),
                reasons: item["technical_reasons"] as? [String] ?? []
            )
        }
    }

    private func parseOrder(_ order: [String: Any]) -> ActivityLogItem {
        let side = anyString(order["side"], fallback: "").uppercased()
        let symbol = anyString(order["symbol"], fallback: "?")
        let qty = anyDouble(order["qty"])
        let filledQty = anyDouble(order["filled_qty"])
        let status = anyString(order["status"], fallback: "unknown")
        let price = anyDouble(order["filled_avg_price"])
        let title = "\(side) \(formatQuantity(filledQty > 0 ? filledQty : qty)) \(symbol) \(status)"
        let detail = price > 0 ? "Filled at \(fmt$(price))" : "Order status \(status)"
        let variant: MessageVariant = status.lowercased() == "filled" ? .trade : .alert
        return ActivityLogItem(
            title: title.trimmingCharacters(in: .whitespaces),
            detail: detail,
            time: anyDate(order["filled_at"]) ?? anyDate(order["submitted_at"]),
            variant: variant
        )
    }

    private func parseEvent(_ event: [String: Any]) -> ActivityLogItem {
        let kind = anyString(event["type"] ?? event["event"] ?? event["name"], fallback: "agent_event")
        let payload = event["payload"] as? [String: Any] ?? event
        let symbol = anyString(payload["symbol"], fallback: "")
        let action = anyString(payload["action"] ?? payload["side"], fallback: "")
        let status = anyString(payload["status"], fallback: "")
        let detail = [
            symbol.isEmpty ? nil : symbol,
            action.isEmpty ? nil : action.uppercased(),
            status.isEmpty ? nil : status
        ].compactMap { $0 }.joined(separator: " | ")

        return ActivityLogItem(
            title: kind.replacingOccurrences(of: "_", with: " ").capitalized,
            detail: detail.isEmpty ? anyString(payload["reason"], fallback: "Agent activity recorded") : detail,
            time: anyDate(event["ts"]) ?? anyDate(event["time"]) ?? anyDate(event["created_at"]),
            variant: kind.lowercased().contains("error") ? .danger : .normal
        )
    }

    private func formatQuantity(_ qty: Double) -> String {
        if qty.rounded() == qty {
            return "\(Int(qty))"
        }
        return String(format: "%.2f", qty)
    }
}

// ── Helpers: parse numeric fields that may be Float, Int, or String ───────────
private func anyDouble(_ v: Any?) -> Double {
    switch v {
    case let d as Double: return d
    case let f as Float:  return Double(f)
    case let i as Int:    return Double(i)
    case let s as String: return Double(s) ?? 0
    default:              return 0
    }
}

private func anyInt(_ v: Any?) -> Int {
    switch v {
    case let i as Int:    return i
    case let d as Double: return Int(d)
    case let s as String: return Int(s) ?? 0
    default:              return 0
    }
}

private func anyString(_ v: Any?, fallback: String = "") -> String {
    switch v {
    case let s as String: return s
    case let n as NSNumber: return n.stringValue
    default: return fallback
    }
}

private func anyDate(_ v: Any?) -> Date? {
    switch v {
    case let d as Date:
        return d
    case let i as Int:
        return Date(timeIntervalSince1970: TimeInterval(i))
    case let d as Double:
        return Date(timeIntervalSince1970: d)
    case let s as String:
        let iso = ISO8601DateFormatter()
        if let date = iso.date(from: s) {
            return date
        }
        if let unix = Double(s) {
            return Date(timeIntervalSince1970: unix)
        }
        return nil
    default:
        return nil
    }
}
