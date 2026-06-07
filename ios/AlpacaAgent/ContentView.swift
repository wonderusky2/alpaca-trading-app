import SwiftUI

extension Color {
    static let appBackground = Color(.systemGroupedBackground)
    static let appSurface = Color(.secondarySystemGroupedBackground)
    static let appBorder = Color(.separator).opacity(0.35)
    static let appMuted = Color(.secondaryLabel)
    static let appGreen = Color(red: 0.00, green: 0.63, blue: 0.31)
    static let appRed = Color(red: 0.86, green: 0.18, blue: 0.16)
    static let appAmber = Color(red: 0.88, green: 0.55, blue: 0.08)
    static let appBlue = Color(red: 0.00, green: 0.43, blue: 0.86)
}

struct ContentView: View {
    @StateObject private var vm = AgentViewModel()
    @State private var showingChat = false

    var body: some View {
        NavigationStack {
            DashboardView(vm: vm) {
                showingChat = true
            }
        }
        .tint(.appGreen)
        .sheet(isPresented: $showingChat) {
            NavigationStack {
                ChatView(vm: vm) {
                    showingChat = false
                }
            }
        }
        .sheet(item: $vm.pendingTrade) { proposal in
            OrderReviewSheet(
                proposal: proposal,
                isPlacingOrder: vm.isPlacingOrder,
                confirm: vm.confirmPendingTrade,
                cancel: vm.cancelPendingTrade
            )
            .presentationDetents([.medium, .large])
        }
    }
}

struct DashboardView: View {
    @ObservedObject var vm: AgentViewModel
    let openChat: () -> Void

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 10) {
                PortfolioHeader(vm: vm)
                WhatNextPanel(vm: vm)
                LatestActivity(
                    items: vm.activityItems,
                    isRefreshing: vm.isRefreshingActivity,
                    refresh: vm.refreshActivity
                )
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
        }
        .background(Color.appBackground)
        .navigationTitle("Performance")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button(action: openChat) {
                    Image(systemName: "text.bubble.fill")
                }
                .accessibilityLabel("Open chat")
            }
            ToolbarItem(placement: .topBarTrailing) {
                Button(action: vm.refresh) {
                    Image(systemName: "arrow.clockwise")
                }
                .accessibilityLabel("Refresh")
            }
        }
    }
}

struct WhatNextPanel: View {
    @ObservedObject var vm: AgentViewModel

    private var tint: Color {
        if !vm.exitRecommendations.isEmpty { return .appAmber }
        switch vm.decision.severity {
        case .trade: return .appGreen
        case .alert: return .appAmber
        case .danger: return .appRed
        case .normal: return .appBlue
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .firstTextBaseline) {
                Text("Portfolio thesis")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(.secondary)
                Spacer()
                Text(sentimentLine)
                    .font(.caption.weight(.bold))
                    .foregroundStyle(tint)
            }

            HStack(alignment: .top, spacing: 12) {
                Image(systemName: iconName)
                    .font(.title2)
                    .foregroundStyle(tint)
                    .frame(width: 30)

                VStack(alignment: .leading, spacing: 6) {
                    Text(primaryAction)
                        .font(.title3.weight(.semibold))
                        .foregroundStyle(.primary)
                        .fixedSize(horizontal: false, vertical: true)
                    Text(vm.portfolioNarrative.summary)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            VStack(spacing: 8) {
                NarrativeStrip(title: "Why", rows: whyRows, tint: .appBlue)
                NarrativeStrip(title: "Next", rows: nextRows, tint: tint)
                NarrativeStrip(title: "Model", rows: [vm.portfolioNarrative.modelAdjustment], tint: .appMuted)
            }
        }
        .padding(14)
        .background(Color.appSurface)
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private var iconName: String {
        if !vm.exitRecommendations.isEmpty { return "arrow.down.right.circle.fill" }
        return vm.decision.severity == .trade ? "bolt.circle.fill" : "clock.badge.checkmark"
    }

    private var sentimentLine: String {
        "\(vm.portfolioNarrative.sentimentFrom.capitalized) -> \(vm.portfolioNarrative.sentimentTo.capitalized)"
    }

    private var primaryAction: String {
        if let first = nextRows.first {
            return first
        }
        return vm.decision.summary
    }

    private var whyRows: [String] {
        if !vm.portfolioNarrative.why.isEmpty { return vm.portfolioNarrative.why }
        return [vm.decision.summary]
    }

    private var nextRows: [String] {
        if !vm.portfolioNarrative.nextActions.isEmpty { return vm.portfolioNarrative.nextActions }
        return [vm.decision.action.replacingOccurrences(of: "_", with: " ").capitalized]
    }
}

struct NarrativeStrip: View {
    let title: String
    let rows: [String]
    let tint: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title)
                .font(.caption2.weight(.bold))
                .foregroundStyle(.secondary)
            ForEach(Array(rows.prefix(3).enumerated()), id: \.offset) { _, row in
                HStack(alignment: .top, spacing: 8) {
                    Rectangle()
                        .fill(tint)
                        .frame(width: 3, height: 15)
                        .clipShape(RoundedRectangle(cornerRadius: 2))
                        .padding(.top, 2)
                    Text(row)
                        .font(.caption)
                        .foregroundStyle(.primary)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                }
            }
        }
        .padding(.vertical, 9)
        .padding(.horizontal, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(.tertiarySystemGroupedBackground))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

struct BookSummaryPanel: View {
    @ObservedObject var vm: AgentViewModel

    private var exposureValue: Double {
        vm.positions.reduce(0) { $0 + $1.currentValue }
    }

    private var exposurePct: Double {
        guard vm.overview.equity > 0 else { return 0 }
        return exposureValue / vm.overview.equity * 100
    }

    private var cashPct: Double {
        guard vm.overview.equity > 0 else { return 0 }
        return vm.overview.cash / vm.overview.equity * 100
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Label("Book", systemImage: "chart.bar.xaxis")
                    .font(.headline)
                Spacer()
                Text(vm.overview.regime)
                    .font(.caption.weight(.bold))
                    .foregroundStyle(regimeColor)
            }

            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 10) {
                BookMetric("Exposure", value: "\(String(format: "%.0f", exposurePct))%", detail: money(exposureValue), tint: exposurePct > 80 ? .appAmber : .primary)
                BookMetric("Open P&L", value: signedMoney(vm.overview.displayPnl), detail: "\(vm.overview.posCount) positions", tint: vm.overview.displayPnl >= 0 ? .appGreen : .appRed)
                BookMetric("Cash", value: "\(String(format: "%.0f", cashPct))%", detail: money(vm.overview.cash))
                BookMetric("Market", value: vm.overview.isOpen ? "Open" : "Closed", detail: vm.overview.nextOpen.isEmpty ? "Exchange clock" : "Next \(vm.overview.nextOpen)", tint: vm.overview.isOpen ? .appGreen : .appMuted)
            }
        }
        .padding(12)
        .background(Color.appSurface)
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private var regimeColor: Color {
        switch vm.overview.regime {
        case "BULL": return .appGreen
        case "BEAR": return .appRed
        default: return .appAmber
        }
    }
}

struct BookMetric: View {
    let title: String
    let value: String
    let detail: String
    let tint: Color

    init(_ title: String, value: String, detail: String, tint: Color = .primary) {
        self.title = title
        self.value = value
        self.detail = detail
        self.tint = tint
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.caption2.weight(.medium))
                .foregroundStyle(.secondary)
            Text(value)
                .font(.title3.weight(.semibold))
                .foregroundStyle(tint)
                .lineLimit(1)
                .minimumScaleFactor(0.65)
            Text(detail)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .minimumScaleFactor(0.75)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(Color(.tertiarySystemGroupedBackground))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

struct AgentCommandPanel: View {
    @ObservedObject var vm: AgentViewModel

    private var tint: Color {
        if !vm.exitRecommendations.isEmpty { return .appAmber }
        switch vm.decision.severity {
        case .trade: return .appGreen
        case .alert: return .appAmber
        case .danger: return .appRed
        case .normal: return .appBlue
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: iconName)
                    .font(.title3)
                    .foregroundStyle(tint)
                    .frame(width: 26)

                VStack(alignment: .leading, spacing: 4) {
                    Text(primaryAction)
                        .font(.headline)
                    Text(vm.decision.summary)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }

                Spacer(minLength: 8)

                Toggle("", isOn: $vm.autoExecuteOrders)
                    .labelsHidden()
            }

            HStack(spacing: 8) {
                AgentChip(label: "Auto", value: vm.autoExecuteOrders ? "On" : "Paused", tint: vm.autoExecuteOrders ? .appGreen : .appAmber)
                AgentChip(label: "Model", value: "G\(vm.strategyModel.generation)", tint: .appBlue)
                AgentChip(label: "Max hold", value: "\(vm.strategyModel.maxHoldingDays)d", tint: .appAmber)
            }

            if !vm.exitRecommendations.isEmpty {
                VStack(spacing: 0) {
                    ForEach(vm.exitRecommendations) { item in
                        ExitRecommendationRow(item: item)
                        if item.id != vm.exitRecommendations.last?.id {
                            Divider().padding(.leading, 14)
                        }
                    }
                }
                .background(Color(.tertiarySystemGroupedBackground))
                .clipShape(RoundedRectangle(cornerRadius: 8))
            }
        }
        .padding(12)
        .background(Color.appSurface)
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private var iconName: String {
        if !vm.exitRecommendations.isEmpty { return "exclamationmark.triangle.fill" }
        return vm.autoExecuteOrders ? "bolt.circle.fill" : "pause.circle.fill"
    }

    private var primaryAction: String {
        if !vm.exitRecommendations.isEmpty { return "Reduce risk now" }
        return vm.decision.action.replacingOccurrences(of: "_", with: " ").capitalized
    }
}

struct AgentChip: View {
    let label: String
    let value: String
    let tint: Color

    var body: some View {
        HStack(spacing: 5) {
            Text(label)
                .font(.caption2)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.caption.weight(.bold))
                .foregroundStyle(tint)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 8)
        .background(Color(.tertiarySystemGroupedBackground))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

struct SignalInsightPanel: View {
    let signals: [SignalInsight]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Label("Momentum signals", systemImage: "waveform.path.ecg")
                    .font(.headline)
                Spacer()
                Text(signals.isEmpty ? "No setup" : "\(signals.count) ranked")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
            }

            if signals.isEmpty {
                Text("No indicator-confirmed entries right now.")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            } else {
                VStack(spacing: 0) {
                    ForEach(signals) { signal in
                        SignalInsightRow(signal: signal)
                        if signal.id != signals.last?.id {
                            Divider().padding(.leading, 12)
                        }
                    }
                }
                .background(Color(.tertiarySystemGroupedBackground))
                .clipShape(RoundedRectangle(cornerRadius: 8))
            }
        }
        .padding(12)
        .background(Color.appSurface)
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

struct SignalInsightRow: View {
    let signal: SignalInsight

    private var scoreColor: Color {
        if signal.score >= 80 { return .appGreen }
        if signal.score >= 70 { return .appAmber }
        return .appMuted
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(alignment: .firstTextBaseline) {
                Text(signal.symbol)
                    .font(.subheadline.weight(.semibold))
                Text(signal.regime)
                    .font(.caption2.weight(.bold))
                    .foregroundStyle(.secondary)
                Spacer()
                Text("\(signal.score)")
                    .font(.subheadline.weight(.bold))
                    .foregroundStyle(scoreColor)
            }

            HStack(spacing: 10) {
                SignalMetric("RSI", value: String(format: "%.0f", signal.rsi14))
                SignalMetric("MACD", value: String(format: "%.2f", signal.macdHist))
                SignalMetric("EMA", value: signal.emaTrend.capitalized)
                SignalMetric("Trend", value: signal.trendDirection.capitalized)
            }

            HStack(spacing: 10) {
                SignalMetric("VWAP", value: signedPercent(signal.priceVsVwapPct))
                SignalMetric("AVWAP", value: signedPercent(signal.priceVsAvwapLowPct))
                SignalMetric("Fib", value: fibLabel)
                SignalMetric("Vol", value: String(format: "%.1fx", signal.volumeRatio))
            }

            Text(reasonLine)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(2)
        }
        .padding(10)
    }

    private var reasonLine: String {
        let core = signal.reasons.prefix(4).joined(separator: " | ")
        if core.isEmpty {
            return "Move \(signedPercent(signal.changePct)) | AVWAP \(signedPercent(signal.priceVsAvwapLowPct)) | Vol \(String(format: "%.1fx", signal.volumeRatio))"
        }
        return core
    }

    private var fibLabel: String {
        signal.fibPosition.replacingOccurrences(of: "_", with: " ").capitalized
    }
}

struct SignalMetric: View {
    let label: String
    let value: String

    init(_ label: String, value: String) {
        self.label = label
        self.value = value
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(label)
                .font(.caption2)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.caption.weight(.semibold))
                .lineLimit(1)
                .minimumScaleFactor(0.7)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct DecisionPanel: View {
    @ObservedObject var vm: AgentViewModel

    private var tint: Color {
        switch vm.decision.severity {
        case .trade: return .appGreen
        case .alert: return .appAmber
        case .danger: return .appRed
        case .normal: return vm.exitRecommendations.isEmpty ? .appBlue : .appAmber
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: vm.exitRecommendations.isEmpty ? "target" : "exclamationmark.triangle.fill")
                    .font(.headline)
                    .foregroundStyle(tint)

                VStack(alignment: .leading, spacing: 4) {
                    Text(actionTitle)
                        .font(.headline)
                    Text(vm.decision.summary)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }

                Spacer()
            }

            if !vm.exitRecommendations.isEmpty {
                VStack(spacing: 0) {
                    ForEach(vm.exitRecommendations) { item in
                        ExitRecommendationRow(item: item)
                        if item.id != vm.exitRecommendations.last?.id {
                            Divider().padding(.leading, 14)
                        }
                    }
                }
                .background(Color(.tertiarySystemGroupedBackground))
                .clipShape(RoundedRectangle(cornerRadius: 8))
            }
        }
        .padding(12)
        .background(Color.appSurface)
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private var actionTitle: String {
        if !vm.exitRecommendations.isEmpty { return "Exit check" }
        return vm.decision.action.replacingOccurrences(of: "_", with: " ").capitalized
    }
}

struct ExitRecommendationRow: View {
    let item: ExitRecommendation

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "arrow.down.right.circle.fill")
                .foregroundStyle(Color.appAmber)
                .padding(.top, 2)

            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("Sell \(formatShares(item.quantity)) \(item.symbol)")
                        .font(.subheadline.weight(.semibold))
                    Spacer()
                    Text(signedMoney(item.unrealizedPnl))
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(item.unrealizedPnl >= 0 ? Color.appGreen : Color.appRed)
                }

                Text(exitDetail)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(10)
    }

    private var exitDetail: String {
        "\(reasonLabel(item.reason)) | now \(signedPercent(item.unrealizedPnlPct)) | peak \(signedPercent(item.peakUnrealizedPnlPct)) | giveback \(String(format: "%.1f", item.givebackPct))% | \(item.holdingDays)d"
    }

    private func reasonLabel(_ value: String) -> String {
        value.replacingOccurrences(of: "_", with: " ").capitalized
    }
}

struct AgentStatusPanel: View {
    @ObservedObject var vm: AgentViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 12) {
                Image(systemName: vm.autoExecuteOrders ? "bolt.circle.fill" : "pause.circle.fill")
                    .font(.title2)
                    .foregroundStyle(vm.autoExecuteOrders ? Color.appGreen : Color.appAmber)

                VStack(alignment: .leading, spacing: 3) {
                    Text(vm.autoExecuteOrders ? "Auto trading active" : "Auto trading paused")
                        .font(.headline)
                    Text(vm.autoExecuteOrders ? "Agent orders place immediately." : "Agent proposals wait for review.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                Spacer()

                Toggle("Auto trading", isOn: $vm.autoExecuteOrders)
                    .labelsHidden()
            }

            Divider()

            VStack(spacing: 10) {
                StatusLine(label: "Agent", value: vm.isThinking ? "Working" : "Monitoring", tint: vm.isThinking ? .appAmber : .appGreen)
                StatusLine(label: "Market", value: vm.overview.isOpen ? "Open" : "Closed", tint: vm.overview.isOpen ? .appGreen : .appMuted)
                StatusLine(label: "Holdings", value: "\(vm.overview.posCount) open", tint: vm.overview.posCount > 0 ? .appBlue : .appMuted)
                StatusLine(label: "Model", value: "Gen \(vm.strategyModel.generation) | min \(vm.strategyModel.minConviction)", tint: .appBlue)
                StatusLine(label: "Exits", value: "\(vm.strategyModel.maxHoldingDays)d hold | \(String(format: "%.1f", vm.strategyModel.profitGivebackPct))% giveback", tint: .appAmber)
                StatusLine(label: "Last update", value: lastUpdateText, tint: .appMuted)
            }
        }
        .padding(14)
        .background(Color.appSurface)
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private var lastUpdateText: String {
        guard let date = vm.lastOverviewAt else { return "--" }
        return date.formatted(date: .omitted, time: .shortened)
    }
}

struct PortfolioHeader: View {
    @ObservedObject var vm: AgentViewModel

    private var rangeLine: String {
        "\(vm.selectedPortfolioRange.rawValue): \(signedMoney(vm.portfolioRangePnl)) | \(signedPercent(vm.portfolioRangePnlPct))"
    }

    private var trendColor: Color {
        vm.portfolioRangePnl >= 0 ? .appGreen : .appRed
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 8) {
                        PaperBadge()
                        Text(vm.overview.regime)
                            .font(.caption2.weight(.bold))
                            .foregroundStyle(regimeColor)
                    }

                    Text("Portfolio")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)

                    Text(money(vm.overview.equity))
                        .font(.system(size: 34, weight: .bold, design: .rounded))
                        .minimumScaleFactor(0.72)
                        .lineLimit(1)
                }

                Spacer()

                VStack(alignment: .trailing, spacing: 8) {
                    MarketBadge(isOpen: vm.overview.isOpen)
                    Text(vm.overview.isOpen ? "Live clock" : "Next \(vm.overview.nextOpen.isEmpty ? "--" : vm.overview.nextOpen)")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                }
            }

            HStack(spacing: 8) {
                PnlTile(
                    title: "Today",
                    value: vm.overview.hasDailyPnl ? signedMoney(vm.overview.dailyPnl) : "--",
                    detail: vm.overview.hasDailyPnl ? signedPercent(vm.overview.dailyPnlPct) : "Broker unavailable",
                    tint: vm.overview.dailyPnl >= 0 ? .appGreen : .appRed
                )
                PnlTile(
                    title: vm.selectedPortfolioRange.rawValue,
                    value: signedMoney(vm.portfolioRangePnl),
                    detail: signedPercent(vm.portfolioRangePnlPct),
                    tint: trendColor
                )
            }

            PortfolioChart(points: vm.portfolioPoints, isLoading: vm.isLoadingPortfolioHistory, color: trendColor)
                .frame(height: 56)

            PortfolioRangePicker(selectedRange: vm.selectedPortfolioRange) { range in
                vm.selectPortfolioRange(range)
            }
        }
        .padding(12)
        .background(Color.appSurface)
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private var regimeColor: Color {
        switch vm.overview.regime {
        case "BULL": return .appGreen
        case "BEAR": return .appRed
        default: return .appAmber
        }
    }
}

struct PnlTile: View {
    let title: String
    let value: String
    let detail: String
    let tint: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title)
                .font(.caption2.weight(.bold))
                .foregroundStyle(.secondary)
            Text(value)
                .font(.headline.weight(.bold))
                .foregroundStyle(tint)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            Text(detail)
                .font(.caption2.weight(.semibold))
                .foregroundStyle(tint)
                .lineLimit(1)
                .minimumScaleFactor(0.75)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(Color(.tertiarySystemGroupedBackground))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

struct PortfolioRangePicker: View {
    let selectedRange: PortfolioRange
    let select: (PortfolioRange) -> Void

    var body: some View {
        HStack(spacing: 4) {
            ForEach(PortfolioRange.allCases) { range in
                Button {
                    select(range)
                } label: {
                    Text(label(for: range))
                        .font(.caption2.weight(.bold))
                        .lineLimit(1)
                        .minimumScaleFactor(0.75)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 7)
                        .foregroundStyle(selectedRange == range ? Color.white : Color.appMuted)
                        .background(selectedRange == range ? Color.appGreen : Color.clear)
                        .clipShape(RoundedRectangle(cornerRadius: 6))
                }
                .buttonStyle(.plain)
            }
        }
        .padding(4)
        .background(Color(.tertiarySystemGroupedBackground))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private func label(for range: PortfolioRange) -> String {
        switch range {
        case .day: return "1D"
        case .week: return "1W"
        case .month: return "1M"
        case .quarter: return "QTR"
        case .ytd: return "YTD"
        case .custom: return "Custom"
        }
    }
}

struct PortfolioChart: View {
    let points: [PortfolioPoint]
    let isLoading: Bool
    let color: Color

    var body: some View {
        ZStack {
            if points.count > 1 {
                GeometryReader { proxy in
                    let values = points.map(\.equity)
                    let minValue = values.min() ?? 0
                    let maxValue = values.max() ?? 0
                    let span = max(maxValue - minValue, 1)
                    let width = proxy.size.width
                    let height = proxy.size.height

                    Path { path in
                        for index in points.indices {
                            let x = CGFloat(index) / CGFloat(max(points.count - 1, 1)) * width
                            let normalized = (points[index].equity - minValue) / span
                            let y = height - CGFloat(normalized) * height
                            if index == points.startIndex {
                                path.move(to: CGPoint(x: x, y: y))
                            } else {
                                path.addLine(to: CGPoint(x: x, y: y))
                            }
                        }
                    }
                    .stroke(color, style: StrokeStyle(lineWidth: 3, lineCap: .round, lineJoin: .round))
                }
            }

            if isLoading {
                ProgressView()
            } else if points.isEmpty {
                Text("No portfolio history")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.secondary)
            }
        }
    }
}

struct MetricGrid: View {
    let overview: OverviewData

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionTitle("Account")
            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 10) {
                MetricCard("Cash", value: money(overview.cash), detail: cashPercent)
                MetricCard("Open P&L", value: signedMoney(overview.displayPnl), detail: "\(overview.posCount) holdings", tint: overview.displayPnl >= 0 ? .appGreen : .appRed)
                MetricCard("Regime", value: overview.regime, detail: overview.isOpen ? "Market open" : "Market closed", tint: regimeColor)
                MetricCard("Next open", value: overview.nextOpen.isEmpty ? "--" : overview.nextOpen, detail: "Exchange clock")
            }
        }
    }

    private var cashPercent: String {
        guard overview.equity > 0 else { return "--" }
        return "\(Int(overview.cash / overview.equity * 100))% available"
    }

    private var regimeColor: Color {
        switch overview.regime {
        case "BULL": return .appGreen
        case "BEAR": return .appRed
        default: return .appAmber
        }
    }
}

struct PositionSummary: View {
    @ObservedObject var vm: AgentViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Image(systemName: vm.overview.posCount > 0 ? "briefcase.fill" : "tray.fill")
                    .foregroundStyle(Color.appGreen)
                Text(vm.overview.posCount > 0 ? "Holdings" : "No exposure")
                    .font(.headline)
                Spacer()
                Text(signedMoney(vm.overview.displayPnl))
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(vm.overview.displayPnl >= 0 ? Color.appGreen : Color.appRed)
            }

            if vm.positions.isEmpty {
                Text("No broker holdings.")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            } else {
                VStack(spacing: 0) {
                    ForEach(vm.positions) { position in
                        PositionRowView(position: position)
                        if position.id != vm.positions.last?.id {
                            Divider().padding(.leading, 12)
                        }
                    }
                }
                .background(Color(.tertiarySystemGroupedBackground))
                .clipShape(RoundedRectangle(cornerRadius: 8))
            }
        }
        .padding(12)
        .background(Color.appSurface)
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

struct PositionRowView: View {
    let position: PositionRow

    private var pnlColor: Color {
        position.unrealizedPnl >= 0 ? .appGreen : .appRed
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(position.symbol)
                        .font(.headline)
                    Text("\(position.side.capitalized) | \(formatShares(position.qty)) sh | \(position.status)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }

                Spacer()

                VStack(alignment: .trailing, spacing: 2) {
                    Text(signedMoney(position.unrealizedPnl))
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(pnlColor)
                    Text(signedPercent(position.unrealizedPnlPct))
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(pnlColor)
                }
            }

            HStack {
                DetailPill(label: "Value", value: money(position.currentValue))
                DetailPill(label: "Entry", value: money(position.entryPrice))
                DetailPill(label: "Now", value: money(position.currentPrice))
                DetailPill(label: "Weight", value: "\(String(format: "%.1f", position.currentWeightPct))%")
            }
        }
        .padding(10)
    }
}

struct ChatView: View {
    @ObservedObject var vm: AgentViewModel
    let goToMonitor: () -> Void
    @FocusState private var focused: Bool

    var body: some View {
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 10) {
                        ForEach(vm.messages) { message in
                            ActivityRow(message: message)
                                .id(message.id)
                        }
                        if vm.isThinking {
                            ThinkingRow().id("thinking")
                        }
                        Color.clear.frame(height: 6).id("bottom")
                    }
                    .padding(16)
                }
                .onChange(of: vm.messages.count) { _, _ in
                    withAnimation(.easeOut(duration: 0.16)) {
                        proxy.scrollTo("bottom", anchor: .bottom)
                    }
                }
                .onChange(of: vm.isThinking) { _, _ in
                    if vm.isThinking {
                        withAnimation { proxy.scrollTo("thinking", anchor: .bottom) }
                    }
                }
            }

            Composer(vm: vm, focused: $focused)
        }
        .background(Color.appBackground)
        .navigationTitle("Chat")
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                Button(action: goToMonitor) {
                    Label("Monitor", systemImage: "chevron.left")
                }
            }
        }
    }
}

struct LatestActivity: View {
    let items: [ActivityLogItem]
    let isRefreshing: Bool
    let refresh: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                SectionTitle("What happened")
                Spacer()
                Button(action: refresh) {
                    if isRefreshing {
                        ProgressView()
                    } else {
                        Image(systemName: "arrow.clockwise")
                    }
                }
                .accessibilityLabel("Refresh activity")
            }
            VStack(spacing: 0) {
                ForEach(Array(items.prefix(10))) { item in
                    ActivityLogRow(item: item)
                    if item.id != items.prefix(10).last?.id {
                        Divider().padding(.leading, 14)
                    }
                }
                if items.isEmpty {
                    Text("No agent actions yet.")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(14)
                }
            }
            .background(Color.appSurface)
            .clipShape(RoundedRectangle(cornerRadius: 8))
        }
    }
}

struct ActivityLogRow: View {
    let item: ActivityLogItem

    private var tint: Color {
        switch item.variant {
        case .trade: return .appGreen
        case .alert: return .appAmber
        case .danger: return .appRed
        case .normal: return .appMuted
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Circle()
                .fill(tint)
                .frame(width: 8, height: 8)
                .padding(.top, 7)

            VStack(alignment: .leading, spacing: 4) {
                Text(item.title)
                    .font(.subheadline.weight(.semibold))
                    .fixedSize(horizontal: false, vertical: true)
                Text(item.detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                if let time = item.time {
                    Text(time, style: .time)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(12)
    }
}

struct ActivityRow: View {
    let message: ChatMessage
    var compact = false

    private var tint: Color {
        switch message.variant {
        case .trade: return .appGreen
        case .alert: return .appAmber
        case .danger: return .appRed
        case .normal: return message.role == .user ? .appBlue : .appMuted
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Circle()
                .fill(tint)
                .frame(width: 8, height: 8)
                .padding(.top, 7)
            VStack(alignment: .leading, spacing: 4) {
                Text(message.text)
                    .font(compact ? .subheadline : .body)
                    .foregroundStyle(.primary)
                    .fixedSize(horizontal: false, vertical: true)
                Text(message.time, style: .time)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
        }
        .padding(compact ? 12 : 14)
        .background(compact ? Color.clear : Color.appSurface)
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

struct Composer: View {
    @ObservedObject var vm: AgentViewModel
    var focused: FocusState<Bool>.Binding

    var body: some View {
        HStack(spacing: 10) {
            TextField("Ask about the agent", text: $vm.inputText, axis: .vertical)
                .lineLimit(1...4)
                .textFieldStyle(.plain)
                .focused(focused)
                .submitLabel(.send)
                .onSubmit(vm.send)
                .padding(.horizontal, 14)
                .padding(.vertical, 11)
                .background(Color.appSurface)
                .clipShape(RoundedRectangle(cornerRadius: 8))

            Button(action: vm.send) {
                Image(systemName: "arrow.up.circle.fill")
                    .font(.system(size: 34))
            }
            .disabled(vm.inputText.trimmingCharacters(in: .whitespaces).isEmpty)
        }
        .padding(.horizontal, 14)
        .padding(.top, 10)
        .padding(.bottom, 10)
        .background(.bar)
    }
}

struct OrderReviewSheet: View {
    let proposal: PendingTradeProposal
    let isPlacingOrder: Bool
    let confirm: () -> Void
    let cancel: () -> Void
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 18) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Review order")
                        .font(.title2.weight(.semibold))
                    Text(proposal.summary)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }

                VStack(spacing: 0) {
                    ForEach(proposal.orders) { order in
                        HStack {
                            VStack(alignment: .leading, spacing: 3) {
                                Text(order.symbol)
                                    .font(.headline)
                                Text("\(order.quantity) shares")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            Text(order.side.uppercased())
                                .font(.subheadline.weight(.bold))
                                .foregroundStyle(order.isSell ? Color.appRed : Color.appGreen)
                        }
                        .padding(14)

                        if order.id != proposal.orders.last?.id {
                            Divider().padding(.leading, 14)
                        }
                    }
                }
                .background(Color.appSurface)
                .clipShape(RoundedRectangle(cornerRadius: 8))

                Spacer()

                Button {
                    confirm()
                    dismiss()
                } label: {
                    if isPlacingOrder {
                        ProgressView()
                            .frame(maxWidth: .infinity)
                    } else {
                        Text("Place order")
                            .frame(maxWidth: .infinity)
                    }
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .tint(.appGreen)
                .disabled(isPlacingOrder)

                Button(role: .cancel) {
                    cancel()
                    dismiss()
                } label: {
                    Text("Cancel")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
                .controlSize(.large)
            }
            .padding(18)
            .background(Color.appBackground)
            .navigationTitle("Order")
            .navigationBarTitleDisplayMode(.inline)
        }
    }
}

struct ThinkingRow: View {
    @State private var phase = 0
    private let timer = Timer.publish(every: 0.35, on: .main, in: .common).autoconnect()

    var body: some View {
        HStack(spacing: 12) {
            HStack(spacing: 6) {
                ForEach(0..<3, id: \.self) { index in
                    Circle()
                        .fill(Color.appMuted)
                        .frame(width: 7, height: 7)
                        .scaleEffect(phase == index ? 1.25 : 0.85)
                        .animation(.easeInOut(duration: 0.25), value: phase)
                }
            }

            Text("Working on request")
                .font(.subheadline.weight(.medium))
                .foregroundStyle(.secondary)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.appSurface)
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .onReceive(timer) { _ in phase = (phase + 1) % 3 }
    }
}

struct MetricCard: View {
    let title: String
    let value: String
    let detail: String
    let tint: Color

    init(_ title: String, value: String, detail: String, tint: Color = .primary) {
        self.title = title
        self.value = value
        self.detail = detail
        self.tint = tint
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title)
                .font(.caption.weight(.medium))
                .foregroundStyle(.secondary)
            Text(value)
                .font(.title3.weight(.semibold))
                .foregroundStyle(tint)
                .lineLimit(1)
                .minimumScaleFactor(0.68)
            Text(detail)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(Color.appSurface)
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

struct SectionTitle: View {
    let title: String

    init(_ title: String) {
        self.title = title
    }

    var body: some View {
        Text(title)
            .font(.headline)
            .foregroundStyle(.primary)
    }
}

struct StatusLine: View {
    let label: String
    let value: String
    let tint: Color

    var body: some View {
        HStack {
            Text(label)
                .font(.subheadline)
                .foregroundStyle(.secondary)
            Spacer()
            Text(value)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(tint)
        }
    }
}

struct DetailPill: View {
    let label: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label)
                .font(.caption2)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.caption.weight(.semibold))
                .lineLimit(1)
                .minimumScaleFactor(0.7)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct PaperBadge: View {
    var body: some View {
        Text("PAPER")
            .font(.caption2.weight(.bold))
            .foregroundStyle(Color.appAmber)
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .background(Color.appAmber.opacity(0.12))
            .clipShape(RoundedRectangle(cornerRadius: 6))
    }
}

struct MarketBadge: View {
    let isOpen: Bool

    var body: some View {
        Label(isOpen ? "Open" : "Closed", systemImage: isOpen ? "checkmark.circle.fill" : "moon.fill")
            .font(.caption.weight(.bold))
            .foregroundStyle(isOpen ? Color.appGreen : Color.appMuted)
            .padding(.horizontal, 9)
            .padding(.vertical, 6)
            .background((isOpen ? Color.appGreen : Color.appMuted).opacity(0.12))
            .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

private func money(_ value: Double) -> String {
    let formatter = NumberFormatter()
    formatter.numberStyle = .currency
    formatter.currencySymbol = "$"
    formatter.maximumFractionDigits = value >= 1_000 ? 0 : 2
    return formatter.string(from: NSNumber(value: value)) ?? "$0"
}

private func signedMoney(_ value: Double) -> String {
    let prefix = value >= 0 ? "+" : "-"
    return "\(prefix)\(money(abs(value)))"
}

private func signedPercent(_ value: Double) -> String {
    let prefix = value >= 0 ? "+" : "-"
    return "\(prefix)\(String(format: "%.2f", abs(value)))%"
}

private func formatShares(_ value: Double) -> String {
    if value.rounded() == value {
        return "\(Int(value))"
    }
    return String(format: "%.2f", value)
}

#Preview {
    ContentView()
}
