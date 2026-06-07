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
    @Published var autoExecuteOrders = false
    @Published var lastOverviewAt: Date?
    @Published var overviewError: String?
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
    @Published var heldInsights: [String: SignalInsight] = [:]
    @Published var activityItems: [ActivityLogItem] = []
    @Published var isRefreshingActivity = false
    @Published var activityError: String?
    @Published var variantWinRates: [VariantWinRate] = []
    @Published var todayVariantWinner: String = ""
    @Published var lastOptimizedAt: String = ""

    private var pollTask: Task<Void, Never>?
    private var prevRegime: String = ""
    private var prevPosCount: Int  = -1
    private var prevVariant: String = ""
    private var initialized        = false

    init() {
        startPolling()
        Task {
            await fetchPortfolioHistory(for: selectedPortfolioRange)
            await fetchActivity()
            await fetchVariants()
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
            let (data, response) = try await URLSession.shared.data(for: Config.request("/api/lab/overview"))
            if let http = response as? HTTPURLResponse, http.statusCode >= 400 {
                overviewError = "Overview \(http.statusCode)"
                return
            }
            guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let ok = json["ok"] as? Bool, ok else {
                overviewError = "Overview unavailable"
                return
            }

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
            // Parse held-position scores keyed by symbol (always populated, even on weekends)
            let heldRows = scores["held_scores"] as? [[String: Any]] ?? []
            heldInsights = Dictionary(
                uniqueKeysWithValues: parseHeldInsights(heldRows).map { ($0.symbol, $0) }
            )
            portfolioNarrative = parsePortfolioNarrative(narrativeJson)
            lastOverviewAt = Date()
            overviewError = nil

            if !initialized {
                let greetingPnl = overview.hasDailyPnl ? overview.dailyPnl : openPnl
                postGreeting(regime: regime, isOpen: isOpen, equity: eq,
                             pnl: greetingPnl, posCount: posCount, newsRisks: newsRisks)
                prevRegime   = regime
                prevPosCount = posCount
                initialized  = true
            } else {
                detectChanges(regime: regime, posCount: posCount,
                              openPnl: openPnl, newsRisks: newsRisks,
                              variant: strategyModel.activeVariant)
            }
        } catch {
            overviewError = error.localizedDescription
        }
    }

    private func postGreeting(regime: String, isOpen: Bool, equity: Double,
                               pnl: Double, posCount: Int, newsRisks: [[String: Any]]) {
        let mktStr  = isOpen ? "open" : "closed"
        let pnlSign = pnl >= 0 ? "+" : ""
        let pnlLabel = overview.hasDailyPnl ? "daily P&L" : "open P&L"
        var text = "Agent online. Market \(mktStr). Equity \(fmt$(equity)), \(pnlLabel) \(pnlSign)\(fmt$(abs(pnl))). Regime: \(regime). "
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
                                openPnl: Double, newsRisks: [[String: Any]],
                                variant: String) {
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

        if !prevVariant.isEmpty && variant != prevVariant && variant != "current" {
            let msg = "Strategy optimized → \(variant). Indicator weights adjusted for today."
            addMessage(ChatMessage(msg, role: .agent, variant: .alert))
            notify(title: "Strategy Updated", body: msg)
        }
        prevVariant = variant
    }

    private func fetchVariants() async {
        do {
            let (data, _) = try await URLSession.shared.data(for: Config.request("/api/lab/variants"))
            guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let ok = json["ok"] as? Bool, ok else { return }

            // Today's winner
            let todayJson = json["today"] as? [String: Any] ?? [:]
            todayVariantWinner = anyString(todayJson["winner"], fallback: "")

            // Last optimized timestamp
            if let recordedAt = todayJson["recorded_at"] as? String,
               let date = ISO8601DateFormatter().date(from: recordedAt) {
                let fmt = DateFormatter()
                fmt.dateFormat = "MMM d, h:mm a"
                lastOptimizedAt = fmt.string(from: date)
            }

            // Win rates
            let winRatesJson = json["win_rates"] as? [String: Any] ?? [:]
            variantWinRates = winRatesJson.map { name, val -> VariantWinRate in
                let d = val as? [String: Any] ?? [:]
                return VariantWinRate(
                    name: name,
                    wins: anyInt(d["wins"]),
                    total: anyInt(d["total"]),
                    winRate: anyDouble(d["win_rate"]),
                    avgObjective: anyDouble(d["avg_objective"])
                )
            }
            .sorted { $0.winRate > $1.winRate }
        } catch { /* silent */ }
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
            await fetchVariants()
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
        activityError = nil
        defer { isRefreshingActivity = false }

        do {
            let (data, response) = try await URLSession.shared.data(for: Config.request("/api/lab/activity"))
            if let http = response as? HTTPURLResponse, http.statusCode >= 400 {
                activityError = "Activity \(http.statusCode)"
                return
            }
            guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let ok = json["ok"] as? Bool, ok else {
                activityError = "Activity unavailable"
                return
            }

            let orders = json["recent_orders"] as? [[String: Any]] ?? []
            let events = json["events"] as? [[String: Any]] ?? []
            var items = events.map(parseEvent) + orders.map(parseOrder)
            items.sort { lhs, rhs in
                (lhs.time ?? .distantPast) > (rhs.time ?? .distantPast)
            }
            activityItems = Array(items.prefix(30))
            activityError = nil
        } catch {
            activityError = error.localizedDescription
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
        let regime = overview.regime
        let positions = positions
        let posCount = positions.count
        let openPnl = positions.reduce(0.0) { $0 + $1.unrealizedPnl }
        let winners = positions.filter { $0.unrealizedPnl > 0 }.count
        let losers  = positions.filter { $0.unrealizedPnl < 0 }.count
        let exitCount = exitRecommendations.count
        let riskAction = decision.riskAction == "--" ? "" : decision.riskAction
        let totalValue = positions.reduce(0.0) { $0 + $1.currentValue }
        let equity = overview.equity
        let grossExposure = equity > 0 ? min(100.0, totalValue / equity * 100.0) : 0.0
        let cashPct = max(0.0, 100.0 - grossExposure)

        let regimeDesc: String
        switch regime {
        case "BULL":   regimeDesc = "trending up"
        case "BEAR":   regimeDesc = "trending down"
        case "CHOP", "CHOPPY": regimeDesc = "choppy — no clear direction"
        default:       regimeDesc = "unclear"
        }

        // Summary — plain English
        let summary: String
        if posCount > 0 {
            let pnlWord = openPnl >= 0 ? "up" : "down"
            let pnlAbs  = Int(abs(openPnl)).formatted()
            summary = "\(Int(cashPct))% in cash, \(Int(grossExposure))% invested across \(posCount) position\(posCount == 1 ? "" : "s"). You're \(pnlWord) $\(pnlAbs)."
        } else {
            summary = "You're fully in cash. Market is \(regimeDesc)."
        }

        // Why — plain English
        var why: [String] = ["The market is \(regimeDesc)."]
        if posCount > 0 {
            let pnlWord = openPnl >= 0 ? "up" : "down"
            let pnlAbs  = Int(abs(openPnl)).formatted()
            if winners > 0 && losers > 0 {
                why.append("You have \(winners) position\(winners == 1 ? "" : "s") making money and \(losers) losing — portfolio is \(pnlWord) $\(pnlAbs) overall.")
            } else if losers > 0 {
                why.append("All \(losers) open position\(losers == 1 ? " is" : "s are") losing money. Portfolio is down $\(pnlAbs).")
            } else {
                why.append("All \(winners) open position\(winners == 1 ? " is" : "s are") in the green. Portfolio is up $\(pnlAbs).")
            }
        }
        if riskAction == "reduce_risk" {
            why.append("The portfolio has taken on too much risk. Time to pull back.")
        } else if exitCount > 0 {
            why.append("\(exitCount) position\(exitCount == 1 ? " is" : "s are") triggering exit rules — acting on them protects what you've made.")
        }
        if posCount == 0 { why.append("You're fully in cash.") }

        // Next — plain English
        var next: [String] = []
        if riskAction == "reduce_risk" {
            next.append("Pull back — reduce positions and don't add new ones until conditions improve.")
        } else if exitCount > 0 {
            next.append("Sell the \(exitCount) position\(exitCount == 1 ? "" : "s") that are triggering exit rules.")
        }
        if next.isEmpty {
            switch (regime, posCount) {
            case ("BULL", _) where cashPct > 25:
                next.append("Market is trending up and you have \(Int(cashPct))% in cash — good time to look for new entries.")
            case ("BEAR", let n) where n > 0:
                next.append("Market is heading down — tighten your stops and consider reducing exposure.")
            case ("BEAR", _):
                next.append("Market is heading down. Stay in cash and wait for the trend to reverse.")
            case (_, let n) where n > 0:
                next.append("Market is choppy. Hold what you have and avoid adding new positions.")
            default:
                next.append("Stay in cash. Wait for a clear market trend before investing.")
            }
        }

        return PortfolioNarrative(
            sentimentFrom: "market",
            sentimentTo: regime.lowercased(),
            summary: summary,
            why: why,
            nextActions: next,
            modelAdjustment: modelLine
        )
    }

    private var modelLine: String {
        "G\(strategyModel.generation) · \(strategyModel.activeVariant.isEmpty ? "current" : strategyModel.activeVariant) · conviction \(strategyModel.minConviction)+ · hold ≤\(strategyModel.maxHoldingDays)d · giveback ≤\(String(format: "%.0f", strategyModel.profitGivebackPct))%"
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
            exitOnRegimeFlip: row["exit_on_regime_flip"] as? Bool ?? true,
            activeVariant: anyString(row["active_variant"], fallback: "current")
        )
    }

    private func parseSignalInsights(_ row: [String: Any]) -> [SignalInsight] {
        let rows = (row["top"] as? [[String: Any]]) ?? (row["signals"] as? [[String: Any]]) ?? []
        return rows.prefix(8).map { item in
            let quote = item["quote"] as? [String: Any] ?? [:]
            let technicals = (item["technicals"] as? [String: Any]) ?? (quote["technicals"] as? [String: Any]) ?? [:]

            // Parse per-indicator signal_breakdown
            var breakdown: [String: SignalIndicator] = [:]
            if let bd = item["signal_breakdown"] as? [String: Any] {
                for (key, val) in bd {
                    guard let indDict = val as? [String: Any] else { continue }
                    breakdown[key] = SignalIndicator(
                        status: anyString(indDict["status"], fallback: "neutral"),
                        label:  anyString(indDict["label"],  fallback: ""),
                        points: anyInt(indDict["points"]),
                        weight: anyInt(indDict["weight"])
                    )
                }
            }

            var insight = SignalInsight(
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
            insight.signalBreakdown = breakdown
            return insight
        }
    }

    private func parseHeldInsights(_ rows: [[String: Any]]) -> [SignalInsight] {
        // Same shape as live_scores top entries — reuse the same parsing logic
        let wrapper: [String: Any] = ["top": rows]
        return parseSignalInsights(wrapper)
    }

    private func parseOrder(_ order: [String: Any]) -> ActivityLogItem {
        let side = anyString(order["side"], fallback: "").lowercased()
        let symbol = anyString(order["symbol"], fallback: "?").uppercased()
        let qty = anyDouble(order["qty"])
        let filledQty = anyDouble(order["filled_qty"])
        let status = anyString(order["status"], fallback: "unknown").lowercased()
        let price = anyDouble(order["filled_avg_price"])
        let useQty = filledQty > 0 ? filledQty : qty
        let value = useQty * price

        // Human title: "Bought NVDA" / "Sold NVDA" / "Pending buy NVDA"
        let verb: String
        switch (side, status) {
        case ("buy", "filled"):   verb = "Bought"
        case ("sell", "filled"):  verb = "Sold"
        case ("buy", _):          verb = "Pending buy"
        case ("sell", _):         verb = "Pending sell"
        default:                  verb = side.capitalized
        }
        let title = "\(verb) \(symbol)"

        // Map Alpaca internal statuses to human labels
        let statusLabel: String
        switch status {
        case "filled":                          statusLabel = "filled"
        case "partially_filled":                statusLabel = "partial fill"
        case "accepted", "new", "pending_new",
             "accepted_for_bidding", "held":    statusLabel = "queued"
        case "canceled", "cancelled":           statusLabel = "canceled"
        case "expired":                         statusLabel = "expired"
        case "replaced":                        statusLabel = "replaced"
        default:                                statusLabel = "placed"
        }

        // Detail: qty + value if known
        let qtyStr = formatQuantity(useQty)
        let detail: String
        if price > 0 && value > 0 {
            detail = "\(qtyStr) shares @ \(fmt$(price)) · \(fmt$(value))"
        } else if useQty > 0 {
            detail = "\(qtyStr) shares · \(statusLabel)"
        } else {
            detail = statusLabel.capitalized
        }

        let variant: MessageVariant = status == "filled" ? .trade : .alert
        return ActivityLogItem(
            title: title,
            detail: detail,
            time: anyDate(order["filled_at"]) ?? anyDate(order["submitted_at"]),
            variant: variant
        )
    }

    private func parseEvent(_ event: [String: Any]) -> ActivityLogItem {
        let kind = anyString(event["type"] ?? event["event"] ?? event["name"], fallback: "agent_event")
        let payload = event["payload"] as? [String: Any] ?? event

        // Human-readable titles by event type
        let title: String
        let detail: String
        let variant: MessageVariant

        switch kind {
        case "regime_change":
            let from = anyString(payload["from"], fallback: "").uppercased()
            let to   = anyString(payload["to"], fallback: "").uppercased()
            title  = "Sentiment changed"
            detail = from.isEmpty ? "Market regime updated to \(to)." : "Regime shifted \(from) → \(to). Strategy universe updated."
            variant = .alert

        case "news_risk_detected":
            let sym   = anyString(payload["symbol"], fallback: "position")
            let delta = anyInt(payload["delta"] as Any)
            let head  = anyString(payload["headline"], fallback: "")
            title  = "Negative news detected"
            detail = head.isEmpty ? "Sentiment drop on \(sym) (Δ\(delta))." : "\(sym): \(head.prefix(80))"
            variant = .danger

        case "exit_triggered":
            let sym    = anyString(payload["symbol"], fallback: "position")
            let reason = cleanReason(anyString(payload["reason"], fallback: "exit signal"))
            let pnl    = anyDouble(payload["pnl_pct"])
            title  = "Exit signal triggered"
            detail = "\(sym): \(reason.lowercased()) · \(signedPercentText(pnl)) P&L"
            variant = .alert

        case "risk_gate_active":
            title  = "Risk gate activated"
            detail = anyString(payload["message"], fallback: "Exposure limit reached. No new entries.")
            variant = .danger

        case "risk_gate_cleared":
            title  = "Risk gate cleared"
            detail = anyString(payload["message"], fallback: "Exposure limits normalised. Entries permitted.")
            variant = .normal

        case "paper_orders_submitted":
            let submitted = payload["submitted"] as? [[String: Any]] ?? []
            let count = submitted.count
            let syms  = submitted.compactMap { $0["symbol"] as? String }.prefix(3).joined(separator: ", ")
            title  = "Orders placed"
            detail = count == 0 ? "Paper orders submitted." : "\(count) paper order\(count == 1 ? "" : "s") placed\(syms.isEmpty ? "" : ": \(syms)")."
            variant = .trade

        case "paper_orders_failed":
            let errors = payload["errors"] as? [[String: Any]] ?? []
            let msg = errors.first.flatMap { $0["error"] as? String } ?? "Order rejected by risk gate."
            title  = "Orders blocked"
            detail = msg
            variant = .danger

        case "model_updated":
            let gen = anyInt(payload["generation"] as Any)
            title  = "Strategy optimised"
            detail = gen > 0 ? "Model updated to generation \(gen)." : "Strategy model parameters updated."
            variant = .normal

        default:
            // Generic fallback
            let msg = anyString(payload["reason"] ?? payload["message"], fallback: "")
            title  = kind.replacingOccurrences(of: "_", with: " ").capitalized
            detail = msg.isEmpty ? "Agent activity recorded." : msg
            variant = kind.lowercased().contains("error") || kind.lowercased().contains("fail") ? .danger : .normal
        }

        return ActivityLogItem(
            title: title,
            detail: detail,
            time: anyDate(event["ts"]) ?? anyDate(event["time"]) ?? anyDate(event["created_at"]),
            variant: variant
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
