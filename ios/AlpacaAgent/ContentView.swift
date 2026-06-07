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
        .dynamicTypeSize(.small ... .xxLarge)
    }
}

struct DashboardView: View {
    @ObservedObject var vm: AgentViewModel
    let openChat: () -> Void

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 10) {
                TodayHeroCard(vm: vm)
                WhatNextPanel(vm: vm)
                LatestActivity(
                    items: vm.activityItems,
                    isRefreshing: vm.isRefreshingActivity,
                    error: vm.activityError,
                    refresh: vm.refreshActivity
                )
                PositionsNowPanel(vm: vm)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
        }
        .background(Color.appBackground)
        .toolbar(.hidden, for: .navigationBar)
        .safeAreaInset(edge: .top) {
            DashboardHeader(openChat: openChat, refresh: vm.refresh)
                .padding(.horizontal, 14)
                .padding(.top, 2)
                .padding(.bottom, 8)
                .background(Color.appBackground)
        }
    }
}

struct DashboardHeader: View {
    let openChat: () -> Void
    let refresh: () -> Void

    var body: some View {
        HStack(alignment: .center) {
            Text("Performance")
                .font(.title2.weight(.bold))
                .foregroundStyle(.primary)
            Spacer()
            HStack(spacing: 8) {
                HeaderIconButton(systemName: "text.bubble.fill", action: openChat, label: "Open chat")
                HeaderIconButton(systemName: "arrow.clockwise", action: refresh, label: "Refresh")
            }
        }
    }
}

struct HeaderIconButton: View {
    let systemName: String
    let action: () -> Void
    let label: String

    var body: some View {
        Button(action: action) {
            Image(systemName: systemName)
                .font(.system(size: 18, weight: .bold))
                .foregroundStyle(Color.appGreen)
                .frame(width: 38, height: 34)
                .background(Color.appSurface)
                .clipShape(RoundedRectangle(cornerRadius: 8))
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }
}

struct DashboardStatusStrip: View {
    @ObservedObject var vm: AgentViewModel

    private var freshnessText: String {
        if let error = vm.overviewError {
            return error
        }
        guard let last = vm.lastOverviewAt else {
            return "Loading"
        }
        let seconds = max(0, Int(Date().timeIntervalSince(last)))
        if seconds < 60 { return "Updated now" }
        return "Updated \(seconds / 60)m ago"
    }

    private var freshnessTint: Color {
        if vm.overviewError != nil { return .appRed }
        guard let last = vm.lastOverviewAt else { return .appAmber }
        return Date().timeIntervalSince(last) > 90 ? .appAmber : .appGreen
    }

    var body: some View {
        HStack(spacing: 8) {
            StatusChip(title: "Mode", value: "Paper", tint: .appAmber)
            StatusChip(
                title: "Orders",
                value: vm.autoExecuteOrders ? "Auto" : "Review",
                tint: vm.autoExecuteOrders ? .appRed : .appGreen
            )
            StatusChip(title: "Data", value: freshnessText, tint: freshnessTint)
        }
    }
}

struct StatusChip: View {
    let title: String
    let value: String
    let tint: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title)
                .font(.caption2.weight(.bold))
                .foregroundStyle(.secondary)
            Text(value)
                .font(.caption.weight(.semibold))
                .foregroundStyle(tint)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(Color.appSurface)
        .clipShape(RoundedRectangle(cornerRadius: 8))
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

    private var thesisScore: Int {
        var score = 50
        switch vm.overview.regime {
        case "BULL": score += 20
        case "BEAR": score -= 20
        default:     break
        }
        score -= min(vm.exitRecommendations.count * 8, 24)
        switch vm.decision.severity {
        case .danger: score -= 15
        case .alert:  score -= 5
        case .trade:  score += 10
        default:      break
        }
        if !vm.signalInsights.isEmpty {
            let top = vm.signalInsights.prefix(3)
            let avg = top.map(\.score).reduce(0, +) / top.count
            score += (avg - 50) / 6
        }
        return max(5, min(95, score))
    }

    private var thesisLabel: String {
        switch thesisScore {
        case 0..<25:  return "Defensive"
        case 25..<45: return "Cautious"
        case 45..<55: return "Neutral"
        case 55..<75: return "Bullish"
        default:       return "Risk-On"
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            // Header
            HStack(alignment: .firstTextBaseline) {
                Text("Portfolio thesis")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(.secondary)
                Spacer()
                // Show regime transition only if genuinely changing; otherwise just the regime
                let from = vm.portfolioNarrative.sentimentFrom.uppercased()
                let to   = vm.portfolioNarrative.sentimentTo.uppercased()
                let isTransition = from != "MARKET" && from != to && !from.isEmpty
                Text(isTransition ? "\(from) → \(to)" : to)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(tint)
            }

            // Full-width conviction bar
            ConvictionBar(value: Double(thesisScore) / 100.0,
                          score: thesisScore, label: thesisLabel)

            // Primary action
            VStack(alignment: .leading, spacing: 4) {
                Text(primaryAction)
                    .font(.title2.weight(.semibold))
                    .foregroundStyle(.primary)
                    .fixedSize(horizontal: false, vertical: true)
                Text(vm.portfolioNarrative.summary)
                    .font(.body)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            // Narrative strips
            VStack(spacing: 6) {
                NarrativeStrip(title: "Why",  rows: whyRows,  tint: .appBlue)
                NarrativeStrip(title: "Next", rows: nextRows, tint: tint)
            }
        }
        .padding(14)
        .background(Color.appSurface)
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private var primaryAction: String { nextRows.first ?? vm.decision.summary }
    private var whyRows: [String] {
        vm.portfolioNarrative.why.isEmpty ? [vm.decision.summary] : vm.portfolioNarrative.why
    }
    private var nextRows: [String] {
        vm.portfolioNarrative.nextActions.isEmpty
            ? [vm.decision.action.replacingOccurrences(of: "_", with: " ").capitalized]
            : vm.portfolioNarrative.nextActions
    }
}

/// Full-width horizontal conviction bar with 5 colored zones and a thumb marker.
struct ConvictionBar: View {
    let value: Double    // 0.0–1.0
    let score: Int
    let label: String

    private let zones: [(color: Color, label: String)] = [
        (Color(red: 0.83, green: 0.18, blue: 0.18), "Defensive"),
        (Color(red: 0.96, green: 0.49, blue: 0.0),  "Cautious"),
        (Color(red: 0.85, green: 0.75, blue: 0.1),  "Neutral"),
        (Color(red: 0.55, green: 0.76, blue: 0.29), "Bullish"),
        (Color(red: 0.22, green: 0.56, blue: 0.24), "Risk-On"),
    ]

    private var activeColor: Color {
        let idx = min(Int(value * Double(zones.count)), zones.count - 1)
        return zones[idx].color
    }

    var body: some View {
        VStack(spacing: 5) {
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    // Segmented track
                    HStack(spacing: 1.5) {
                        ForEach(0..<zones.count, id: \.self) { i in
                            zones[i].color
                                .opacity(0.80)
                                .frame(maxWidth: .infinity)
                                .cornerRadius(i == 0 ? 5 : (i == zones.count-1 ? 5 : 0),
                                              corners: i == 0
                                                  ? [.topLeft, .bottomLeft]
                                                  : (i == zones.count-1
                                                      ? [.topRight, .bottomRight]
                                                      : []))
                        }
                    }
                    .frame(height: 10)

                    // Thumb marker
                    let thumbX = geo.size.width * CGFloat(value)
                    Circle()
                        .fill(Color(.systemBackground))
                        .overlay(Circle().stroke(activeColor, lineWidth: 2))
                        .frame(width: 18, height: 18)
                        .offset(x: thumbX - 9, y: -4)
                }
            }
            .frame(height: 10)

            // End labels + center score
            HStack {
                Text("Defensive")
                    .font(.caption2).foregroundStyle(.secondary)
                Spacer()
                Text("\(score)  \(label)")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(activeColor)
                Spacer()
                Text("Risk-On")
                    .font(.caption2).foregroundStyle(.secondary)
            }
        }
    }
}

private extension View {
    func cornerRadius(_ radius: CGFloat, corners: UIRectCorner) -> some View {
        clipShape(RoundedCorner(radius: radius, corners: corners))
    }
}

private struct RoundedCorner: Shape {
    var radius: CGFloat
    var corners: UIRectCorner
    func path(in rect: CGRect) -> Path {
        let path = UIBezierPath(roundedRect: rect,
                                byRoundingCorners: corners,
                                cornerRadii: CGSize(width: radius, height: radius))
        return Path(path.cgPath)
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
            ForEach(Array(rows.prefix(2).enumerated()), id: \.offset) { _, row in
                HStack(alignment: .top, spacing: 8) {
                    Rectangle()
                        .fill(tint)
                        .frame(width: 3, height: 15)
                        .clipShape(RoundedRectangle(cornerRadius: 2))
                        .padding(.top, 2)
                    Text(row)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.primary)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                }
            }
        }
        .padding(.vertical, 8)
        .padding(.horizontal, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(.tertiarySystemGroupedBackground))
        .clipShape(RoundedRectangle(cornerRadius: 8))
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



// ── Today Hero Card ───────────────────────────────────────────────────────────
struct TodayHeroCard: View {
    @ObservedObject var vm: AgentViewModel

    // Hero uses the selected portfolio range P&L — matches whatever period the chart shows
    private var heroValue: Double? {
        let v = vm.portfolioRangePnl
        return v != 0 ? v : (vm.overview.hasDailyPnl ? vm.overview.dailyPnl : nil)
    }
    private var heroValuePct: Double {
        vm.portfolioRangePnlPct != 0 ? vm.portfolioRangePnlPct : vm.overview.dailyPnlPct
    }
    private var pnlColor: Color { (heroValue ?? 0) >= 0 ? .appGreen : .appRed }

    private var heroLabel: String {
        switch vm.selectedPortfolioRange {
        case .day:     return vm.overview.isOpen ? "TODAY SO FAR" : "YESTERDAY"
        case .week:    return "THIS WEEK"
        case .month:   return "THIS MONTH"
        case .quarter: return "THIS QUARTER"
        case .ytd:     return "YEAR TO DATE"
        case .custom:  return "CUSTOM RANGE"
        }
    }

    private var narrativeSubtitle: String {
        guard let v = heroValue else {
            return vm.overview.isOpen ? "Live — tracking positions" : "Market closed"
        }
        let up = v >= 0
        switch vm.overview.regime {
        case "BULL": return up ? "Riding the rally" : "Fighting the uptrend"
        case "BEAR": return up ? "Making money while market drops" : "Taking heat in a falling market"
        default:     return up ? "Holding up in a choppy market" : "Choppy tape, watching closely"
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            // Status chips — no OPEN/CLOSED badge (redundant), just regime + variant
            HStack(spacing: 8) {
                PaperBadge()
                VariantBadge(variant: vm.strategyModel.activeVariant)
                Text(vm.overview.regime)
                    .font(.caption2.weight(.bold))
                    .lineLimit(1)
                    .fixedSize(horizontal: true, vertical: false)
                    .foregroundStyle(regimeColor)
                    .padding(.horizontal, 7).padding(.vertical, 3)
                    .background(regimeColor.opacity(0.12))
                    .clipShape(Capsule())
                Spacer()
                // Market status as tiny text, not a huge pill
                Text(vm.overview.isOpen ? "LIVE" : "CLOSED")
                    .font(.caption2.weight(.bold))
                    .lineLimit(1)
                    .fixedSize(horizontal: true, vertical: false)
                    .foregroundStyle(vm.overview.isOpen ? Color.appGreen : Color.appMuted)
            }
            .padding(.bottom, 14)

            // P&L hero
            Text(heroLabel)
                .font(.caption.weight(.bold))
                .foregroundStyle(.secondary)
                .kerning(1.2)
                .padding(.bottom, 4)

            if let v = heroValue {
                Text(signedMoney(v))
                    .font(.system(size: 48, weight: .bold, design: .rounded))
                    .foregroundStyle(pnlColor)
                Text(signedPercent(heroValuePct) + "  ·  " + narrativeSubtitle)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(pnlColor)
                    .multilineTextAlignment(.center)
                    .padding(.top, 2)
            } else {
                Text(money(vm.overview.equity))
                    .font(.system(size: 48, weight: .bold, design: .rounded))
                    .foregroundStyle(.primary)
                Text(narrativeSubtitle)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .padding(.top, 2)
            }

            Text("Total portfolio \(money(vm.overview.equity))")
                .font(.caption)
                .foregroundStyle(.secondary)
                .padding(.top, 6)

            // Chart
            if !vm.portfolioPoints.isEmpty {
                PortfolioChart(points: vm.portfolioPoints,
                               isLoading: vm.isLoadingPortfolioHistory,
                               color: heroValue != nil ? pnlColor : .appGreen)
                    .frame(height: 70)
                    .padding(.top, 14)
                    .padding(.horizontal, 4)
            }

            PortfolioRangePicker(selectedRange: vm.selectedPortfolioRange) { vm.selectPortfolioRange($0) }
                .padding(.top, 10)

            // Cash row — consolidated here, no separate card
            if vm.overview.equity > 0 {
                let cashPct = vm.overview.cash / vm.overview.equity * 100
                Divider().padding(.top, 6)
                HStack {
                    Text("Cash on sidelines")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                    Text(money(vm.overview.cash))
                        .font(.caption.weight(.semibold))
                    Text("· \(Int(cashPct))%")
                        .font(.caption)
                        .foregroundStyle(cashPct > 50 ? Color.appGreen : Color.appAmber)
                }
                .padding(.top, 4)
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity)
        .background(Color.appSurface)
        .clipShape(RoundedRectangle(cornerRadius: 14))
    }

    private var regimeColor: Color {
        switch vm.overview.regime {
        case "BULL": return .appGreen
        case "BEAR": return .appRed
        default: return .appAmber
        }
    }
}

// ── What the algo did ─────────────────────────────────────────────────────────
struct AlgoNarrativeCard: View {
    @ObservedObject var vm: AgentViewModel

    private struct Bullet: Identifiable {
        let id = UUID()
        let dot: Color
        let title: String
        let detail: String
    }

    private var bullets: [Bullet] {
        var result: [Bullet] = []
        for item in vm.activityItems.prefix(5) {
            let color: Color
            switch item.variant {
            case .trade:
                let lower = item.title.lowercased()
                color = (lower.contains("sold") || lower.contains("sell") || lower.contains("exit")) ? .appRed : .appGreen
            case .danger: color = .appRed
            case .alert:  color = .appAmber
            default:      color = .appBlue
            }
            result.append(Bullet(dot: color, title: item.title, detail: item.detail))
        }
        if result.isEmpty {
            for why in vm.portfolioNarrative.why.prefix(3) {
                result.append(Bullet(dot: .appBlue, title: why, detail: ""))
            }
        }
        return result
    }

    var body: some View {
        guard !bullets.isEmpty else { return AnyView(EmptyView()) }
        return AnyView(
            VStack(alignment: .leading, spacing: 14) {
                Text("WHAT THE ALGO DID")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(.secondary)
                    .kerning(0.8)

                VStack(alignment: .leading, spacing: 12) {
                    ForEach(bullets) { b in
                        HStack(alignment: .top, spacing: 12) {
                            Circle().fill(b.dot)
                                .frame(width: 10, height: 10)
                                .padding(.top, 4)
                            VStack(alignment: .leading, spacing: 3) {
                                Text(b.title)
                                    .font(.subheadline.weight(.semibold))
                                    .fixedSize(horizontal: false, vertical: true)
                                if !b.detail.isEmpty {
                                    Text(b.detail)
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                            }
                        }
                    }
                }
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.appSurface)
            .clipShape(RoundedRectangle(cornerRadius: 14))
        )
    }
}

// ── Cash on the sidelines ─────────────────────────────────────────────────────
struct CashPanel: View {
    @ObservedObject var vm: AgentViewModel

    private var cashPct: Double {
        guard vm.overview.equity > 0 else { return 0 }
        return vm.overview.cash / vm.overview.equity * 100
    }

    private var narrative: String {
        switch vm.overview.regime {
        case "BEAR": return cashPct > 50 ? "Safe while market bleeds" : "Still exposed to the downtrend"
        case "BULL": return cashPct > 50 ? "Capital waiting to be deployed" : "Invested in the uptrend"
        default:     return cashPct > 50 ? "Waiting for a clear signal" : "Deployed in uncertain market"
        }
    }

    var body: some View {
        guard vm.overview.equity > 0 else { return AnyView(EmptyView()) }
        return AnyView(
            VStack(alignment: .leading, spacing: 6) {
                Text("Cash on the sidelines")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                HStack(alignment: .firstTextBaseline) {
                    Text(money(vm.overview.cash))
                        .font(.system(size: 28, weight: .bold, design: .rounded))
                    Spacer()
                    Text("\(Int(cashPct))% cash")
                        .font(.subheadline.weight(.bold))
                        .foregroundStyle(cashPct > 50 ? Color.appGreen : Color.appAmber)
                }
                Text(narrative)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .padding(16)
            .background(Color.appSurface)
            .clipShape(RoundedRectangle(cornerRadius: 14))
        )
    }
}

// ── Positions right now ───────────────────────────────────────────────────────
struct PositionsNowPanel: View {
    @ObservedObject var vm: AgentViewModel
    private var exitSymbols: Set<String> { Set(vm.exitRecommendations.map(\.symbol)) }

    var body: some View {
        guard !vm.positions.isEmpty else { return AnyView(EmptyView()) }
        return AnyView(
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .firstTextBaseline) {
                    Label("Exposure", systemImage: "chart.pie.fill")
                        .font(.caption.weight(.bold))
                        .foregroundStyle(.secondary)
                    Spacer()
                    Text(exposureTone)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(exposureColor)
                }

                HStack(spacing: 8) {
                    ExposureMetric(title: "Invested", value: investedText, detail: "\(vm.positions.count) pos", tint: .primary)
                    ExposureMetric(title: "Open P&L", value: signedMoney(totalPnl), detail: exitDetail, tint: totalPnl >= 0 ? .appGreen : .appRed)
                }

                Text(exposureSummary)
                    .font(.body)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                if !vm.exitRecommendations.isEmpty {
                    HStack {
                        Text("Why \(vm.exitRecommendations.count) exits")
                            .font(.subheadline.weight(.bold))
                            .foregroundStyle(.secondary)
                        Spacer()
                        Text("all shown")
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(exposureColor)
                    }

                    VStack(spacing: 0) {
                        ForEach(Array(vm.exitRecommendations.enumerated()), id: \.element.id) { index, rec in
                            ExitSupportRow(item: rec)
                            if index < vm.exitRecommendations.count - 1 {
                                Divider().padding(.leading, 14)
                            }
                        }
                    }
                    .background(Color(.tertiarySystemGroupedBackground))
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                } else if !focusRows.isEmpty {
                    HStack {
                        Text("Top positions")
                            .font(.subheadline.weight(.bold))
                            .foregroundStyle(.secondary)
                        Spacer()
                        Text("showing \(focusRows.count)")
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(exposureColor)
                    }

                    VStack(spacing: 0) {
                        ForEach(Array(focusRows.enumerated()), id: \.element.id) { index, pos in
                            ExposureFocusRow(
                                pos: pos,
                                needsExit: false,
                                insight: vm.heldInsights[pos.symbol]
                            )
                            if index < focusRows.count - 1 {
                                Divider().padding(.leading, 14)
                            }
                        }
                    }
                    .background(Color(.tertiarySystemGroupedBackground))
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                }
            }
            .padding(14)
            .background(Color.appSurface)
            .clipShape(RoundedRectangle(cornerRadius: 8))
        )
    }

    private var totalValue: Double {
        vm.positions.reduce(0) { $0 + $1.currentValue }
    }

    private var totalPnl: Double {
        vm.positions.reduce(0) { $0 + $1.unrealizedPnl }
    }

    private var winners: Int {
        vm.positions.filter { $0.unrealizedPnl >= 0 }.count
    }

    private var losers: Int {
        vm.positions.filter { $0.unrealizedPnl < 0 }.count
    }

    private var investedPct: Double {
        guard vm.overview.equity > 0 else { return 0 }
        return totalValue / vm.overview.equity * 100
    }

    private var investedText: String {
        "\(Int(investedPct.rounded()))%"
    }

    private var exposureTone: String {
        if !vm.exitRecommendations.isEmpty { return "Needs action" }
        if totalPnl >= 0 { return "Working" }
        return "Watch closely"
    }

    private var exposureColor: Color {
        if !vm.exitRecommendations.isEmpty { return .appAmber }
        return totalPnl >= 0 ? .appGreen : .appRed
    }

    private var exposureSummary: String {
        let positionWord = vm.positions.count == 1 ? "position" : "positions"
        let exitText = vm.exitRecommendations.isEmpty
            ? "No exits are queued."
            : "\(vm.exitRecommendations.count) exit\(vm.exitRecommendations.count == 1 ? "" : "s") need review; reasons and data below."
        return "\(vm.positions.count) \(positionWord): \(winners) up, \(losers) down. \(exitText)"
    }

    private var exitDetail: String {
        vm.exitRecommendations.isEmpty
            ? "no exits"
            : "\(vm.exitRecommendations.count) exits"
    }

    private var focusRows: [PositionRow] {
        let exits = vm.positions.filter { exitSymbols.contains($0.symbol) }
        let losers = vm.positions
            .filter { !exitSymbols.contains($0.symbol) && $0.unrealizedPnl < 0 }
            .sorted { abs($0.unrealizedPnl) > abs($1.unrealizedPnl) }
        let winnersAtRisk = vm.positions
            .filter { !exitSymbols.contains($0.symbol) && $0.unrealizedPnl >= 0 }
            .sorted { $0.unrealizedPnl > $1.unrealizedPnl }
        return Array((exits + losers + winnersAtRisk).prefix(3))
    }
}

struct ExitSupportRow: View {
    let item: ExitRecommendation

    private var pnlColor: Color { item.unrealizedPnl >= 0 ? .appGreen : .appRed }

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(item.symbol)
                        .font(.headline.weight(.bold))
                    Text(reasonLabel)
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(Color.appAmber)
                        .padding(.horizontal, 5)
                        .padding(.vertical, 2)
                        .background(Color.appAmber.opacity(0.15))
                        .clipShape(Capsule())
                }
                Text(supportingData)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Spacer(minLength: 8)

            VStack(alignment: .trailing, spacing: 2) {
                Text(signedMoney(item.unrealizedPnl))
                    .font(.subheadline.weight(.bold))
                    .foregroundStyle(pnlColor)
                    .lineLimit(1)
                    .minimumScaleFactor(0.75)
                Text(formatShares(item.quantity) + " sh")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.secondary)
            }
        }
        .padding(10)
    }

    private var reasonLabel: String {
        item.reason
            .replacingOccurrences(of: "_", with: " ")
            .capitalized
    }

    private var supportingData: String {
        "now \(signedPercent(item.unrealizedPnlPct)) | peak \(signedPercent(item.peakUnrealizedPnlPct)) | giveback \(String(format: "%.1f", item.givebackPct))% | held \(item.holdingDays)d"
    }
}

struct ExposureMetric: View {
    let title: String
    let value: String
    let detail: String
    let tint: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title)
                .font(.caption2.weight(.bold))
                .foregroundStyle(.secondary)
            Text(value)
                .font(.title3.weight(.bold))
                .foregroundStyle(tint)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            Text(detail)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
        .background(Color(.tertiarySystemGroupedBackground))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

struct ExposureFocusRow: View {
    let pos: PositionRow
    let needsExit: Bool
    let insight: SignalInsight?

    private var pnlColor: Color { pos.unrealizedPnl >= 0 ? .appGreen : .appRed }

    private var reason: String {
        if needsExit { return "Exit rule triggered" }
        if let insight {
            return insight.topLabel
        }
        return pos.reason.isEmpty ? "Monitoring position" : pos.reason
    }

    var body: some View {
        HStack(alignment: .center, spacing: 10) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                   Text(pos.symbol)
                        .font(.headline.weight(.bold))
                    if needsExit {
                        Text("EXIT")
                            .font(.caption2.weight(.bold))
                            .foregroundStyle(Color.appAmber)
                            .padding(.horizontal, 5)
                            .padding(.vertical, 2)
                            .background(Color.appAmber.opacity(0.15))
                            .clipShape(Capsule())
                    } else if let insight {
                        Text("\(insight.score)")
                            .font(.caption2.weight(.bold))
                            .foregroundStyle(.white)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(insight.score >= 75 ? Color.appGreen : Color.appAmber)
                            .clipShape(Capsule())
                    }
                }
                Text(reason)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Spacer(minLength: 8)

            VStack(alignment: .trailing, spacing: 2) {
                Text(signedMoney(pos.unrealizedPnl))
                    .font(.subheadline.weight(.bold))
                    .foregroundStyle(pnlColor)
                    .lineLimit(1)
                    .minimumScaleFactor(0.75)
                Text("\(formatShares(pos.qty)) sh")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.secondary)
            }
        }
        .padding(10)
    }
}

struct PositionNowRow: View {
    let pos: PositionRow
    let needsExit: Bool
    let insight: SignalInsight?

    private var pnlColor: Color { pos.unrealizedPnl >= 0 ? .appGreen : .appRed }

    private var scoreBadgeColor: Color {
        guard let s = insight?.score else { return .clear }
        return s >= 80 ? .appGreen : s >= 65 ? .appAmber : Color(.systemGray3)
    }

    private func pillColor(_ ind: SignalIndicator) -> Color {
        switch ind.status {
        case "bullish": return .appGreen
        case "bearish": return .appRed
        default:        return Color(.systemGray3)
        }
    }

    private func pillArrow(_ ind: SignalIndicator) -> String {
        switch ind.status {
        case "bullish": return "▲"
        case "bearish": return "▼"
        default:        return "–"
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            // Row 1: Symbol + badges + P&L
            HStack(spacing: 8) {
                Text(pos.symbol)
                    .font(.headline.weight(.bold))

                if needsExit {
                    Text("EXIT")
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(Color.appAmber)
                        .padding(.horizontal, 5).padding(.vertical, 2)
                        .background(Color.appAmber.opacity(0.15))
                        .clipShape(Capsule())
                }

                // Score badge
                if let insight {
                    Text("\(insight.score)")
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(.white)
                        .padding(.horizontal, 6).padding(.vertical, 2)
                        .background(scoreBadgeColor)
                        .clipShape(Capsule())

                    Text("\(insight.bullishCount)/5 aligned")
                        .font(.caption2)
                        .foregroundStyle(scoreBadgeColor)
                }

                Spacer()

                VStack(alignment: .trailing, spacing: 1) {
                    Text(signedMoney(pos.unrealizedPnl))
                        .font(.subheadline.weight(.bold))
                        .foregroundStyle(pnlColor)
                    Text(signedPercent(pos.unrealizedPnlPct))
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(pnlColor)
                }
            }

            // Row 2: shares + 5 indicator pills
            HStack(spacing: 6) {
                Text("\(formatShares(pos.qty)) sh")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                if let insight, !insight.orderedIndicators.isEmpty {
                    Spacer()
                    HStack(spacing: 4) {
                        ForEach(insight.orderedIndicators) { ni in
                            HStack(spacing: 2) {
                                Text(pillArrow(ni.indicator))
                                    .font(.system(size: 9, weight: .bold))
                                Text(ni.name)
                                    .font(.system(size: 9, weight: .semibold))
                            }
                            .foregroundStyle(pillColor(ni.indicator))
                            .padding(.horizontal, 5).padding(.vertical, 2)
                            .background(pillColor(ni.indicator).opacity(0.12))
                            .clipShape(Capsule())
                        }
                    }
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 11)
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
                        VariantBadge(variant: vm.strategyModel.activeVariant)
                        Text(vm.overview.regime)
                            .font(.caption2.weight(.bold))
                            .lineLimit(1)
                            .fixedSize(horizontal: true, vertical: false)
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

                Spacer(minLength: 8)

                VStack(alignment: .trailing, spacing: 8) {
                    MarketBadge(isOpen: vm.overview.isOpen)
                    Text(vm.overview.isOpen ? "Live" : "Next \(vm.overview.nextOpen.isEmpty ? "--" : vm.overview.nextOpen)")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                        .multilineTextAlignment(.trailing)
                }
                .fixedSize(horizontal: true, vertical: false)
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

// ── Positions Panel ───────────────────────────────────────────────────────────
/// Live position rows with P&L health pills and exit flags.
struct PositionsPanel: View {
    @ObservedObject var vm: AgentViewModel
    @State private var expanded = true

    private var exitSymbols: Set<String> { Set(vm.exitRecommendations.map(\.symbol)) }

    var body: some View {
        guard !vm.positions.isEmpty else { return AnyView(EmptyView()) }
        return AnyView(
            VStack(alignment: .leading, spacing: 8) {
                // Header
                Button(action: { withAnimation(.easeInOut(duration: 0.2)) { expanded.toggle() } }) {
                    HStack {
                        Label("Positions", systemImage: "chart.bar.fill")
                            .font(.caption.weight(.bold))
                            .foregroundStyle(.secondary)
                        Spacer()
                        Text("\(vm.positions.count) open")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                        Image(systemName: expanded ? "chevron.up" : "chevron.down")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }
                .buttonStyle(.plain)

                if expanded {
                    VStack(spacing: 0) {
                        ForEach(vm.positions) { pos in
                            PositionHealthRow(pos: pos, needsExit: exitSymbols.contains(pos.symbol))
                            if pos.id != vm.positions.last?.id {
                                Divider().padding(.leading, 12)
                            }
                        }
                    }
                    .background(Color(.tertiarySystemGroupedBackground))
                    .clipShape(RoundedRectangle(cornerRadius: 10))
                }
            }
            .padding(12)
            .background(Color.appSurface)
            .clipShape(RoundedRectangle(cornerRadius: 14))
        )
    }
}

struct PositionHealthRow: View {
    let pos: PositionRow
    let needsExit: Bool

    private var pnlColor: Color  { pos.unrealizedPnl >= 0 ? .appGreen : .appRed }
    private var healthStatus: String {
        if pos.unrealizedPnlPct >= 1.5  { return "bullish" }
        if pos.unrealizedPnlPct <= -1.5 { return "bearish" }
        return "neutral"
    }
    private var healthLabel: String {
        if pos.unrealizedPnlPct >= 1.5  { return "▲ Gaining" }
        if pos.unrealizedPnlPct <= -1.5 { return "▼ Losing" }
        return "– Flat"
    }
    private var healthColor: Color {
        switch healthStatus {
        case "bullish": return .appGreen
        case "bearish": return .appRed
        default:        return .appAmber
        }
    }

    var body: some View {
        HStack(spacing: 10) {
            // Left: symbol + side
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(pos.symbol)
                        .font(.subheadline.weight(.bold))
                    // Exit flag
                    if needsExit {
                        Text("EXIT")
                            .font(.caption2.weight(.bold))
                            .foregroundStyle(Color.appAmber)
                            .padding(.horizontal, 5)
                            .padding(.vertical, 2)
                            .background(Color.appAmber.opacity(0.12))
                            .clipShape(Capsule())
                    }
                }
                Text("\(formatShares(pos.qty)) sh · \(pos.side.capitalized)")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }

            Spacer()

            // Right: health pill + P&L
            VStack(alignment: .trailing, spacing: 4) {
                // Health pill
                HStack(spacing: 3) {
                    Text(healthLabel)
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(healthColor)
                }
                .padding(.horizontal, 7)
                .padding(.vertical, 3)
                .background(healthColor.opacity(0.12))
                .clipShape(Capsule())

                // P&L
                Text(signedPercent(pos.unrealizedPnlPct))
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(pnlColor)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
    }
}

/// Per-stock entry/exit candidates with scores and technical reasons.
// ── Candidates Panel ──────────────────────────────────────────────────────────
/// Shows entry candidates with per-indicator confluence bars and exit recommendations.
struct CandidatesPanel: View {
    @ObservedObject var vm: AgentViewModel

    private var heldSymbols: Set<String> { Set(vm.positions.map(\.symbol)) }

    var body: some View {
        let scanned = Array(vm.signalInsights.prefix(8))
        guard !scanned.isEmpty else { return AnyView(EmptyView()) }

        return AnyView(
            VStack(alignment: .leading, spacing: 10) {
                Label("Market Scan", systemImage: "waveform.path.ecg")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(.secondary)

                VStack(spacing: 0) {
                    ForEach(scanned) { sig in
                        SignalConfluenceRow(sig: sig, isHeld: heldSymbols.contains(sig.symbol))
                        if sig.id != scanned.last?.id {
                            Divider().padding(.leading, 12)
                        }
                    }
                }
                .background(Color(.tertiarySystemGroupedBackground))
                .clipShape(RoundedRectangle(cornerRadius: 10))
            }
            .padding(12)
            .background(Color.appSurface)
            .clipShape(RoundedRectangle(cornerRadius: 14))
        )
    }
}

// ── Signal confluence row — one scored stock ──────────────────────────────────
struct SignalConfluenceRow: View {
    let sig: SignalInsight
    var isHeld: Bool = false

    private var scoreColor: Color {
        if sig.score >= 80 { return .appGreen }
        if sig.score >= 65 { return Color(red: 0.2, green: 0.78, blue: 0.45) }
        return .appAmber
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            // Header: symbol + score badge + held badge + aligned count + change%
            HStack(spacing: 6) {
                Text(sig.symbol)
                    .font(.subheadline.weight(.bold))

                // Score badge
                Text("\(sig.score)")
                    .font(.caption2.weight(.heavy))
                    .foregroundStyle(.white)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(scoreColor)
                    .clipShape(Capsule())

                // Held badge
                if isHeld {
                    Text("HELD")
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(Color.appBlue)
                        .padding(.horizontal, 5)
                        .padding(.vertical, 2)
                        .background(Color.appBlue.opacity(0.12))
                        .clipShape(Capsule())
                }

                // Bullish alignment count
                Text("\(sig.bullishCount)/\(sig.orderedIndicators.count) ↑")
                    .font(.caption2)
                    .foregroundStyle(.secondary)

                Spacer(minLength: 0)

                // Change %
                let chg = sig.changePct
                Text((chg >= 0 ? "+" : "") + String(format: "%.1f%%", chg))
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(chg >= 0 ? Color.appGreen : Color.appRed)
            }

            // Indicator pills — always shown (fallback derives from raw tech fields)
            HStack(spacing: 5) {
                ForEach(sig.orderedIndicators) { entry in
                    IndicatorPill(name: entry.name, indicator: entry.indicator)
                }
            }

            // Top plain-English reason
            Text(sig.topLabel)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
        .padding(12)
    }
}

// ── Indicator pill ─────────────────────────────────────────────────────────────
struct IndicatorPill: View {
    let name: String
    let indicator: SignalIndicator

    private var bg: Color {
        switch indicator.status {
        case "bullish": return Color.appGreen.opacity(0.18)
        case "bearish": return Color.appRed.opacity(0.18)
        default:        return Color(.tertiarySystemFill)
        }
    }
    private var fg: Color {
        switch indicator.status {
        case "bullish": return Color.appGreen
        case "bearish": return Color.appRed
        default:        return Color.secondary
        }
    }
    private var dot: String {
        switch indicator.status {
        case "bullish": return "▲"
        case "bearish": return "▼"
        default:        return "–"
        }
    }

    var body: some View {
        HStack(spacing: 2) {
            Text(dot)
                .font(.system(size: 7, weight: .bold))
                .foregroundStyle(fg)
            Text(name)
                .font(.system(size: 9, weight: .semibold))
                .foregroundStyle(fg)
        }
        .padding(.horizontal, 6)
        .padding(.vertical, 3)
        .background(bg)
        .clipShape(Capsule())
    }
}

// ── Exit recommendation row ───────────────────────────────────────────────────
struct ExitRow: View {
    let item: ExitRecommendation

    private var detail: String {
        let pnl    = item.unrealizedPnlPct
        let pnlStr = (pnl >= 0 ? "+" : "") + String(format: "%.1f", pnl) + "%"
        switch item.reason {
        case "loss_stop":    return "Down \(pnlStr) — cut the loss"
        case "giveback":     return "Gave back gains (\(pnlStr)) — lock in what's left"
        case "max_hold":     return "Held \(item.holdingDays)d — free up the capital"
        case "bear_regime":  return "Market turned against this (\(pnlStr))"
        case "news_risk":    return "News risk hit (\(pnlStr)) — exit to be safe"
        default:
            return pnl < 0 ? "Down \(pnlStr) — exit triggered"
                           : "Up \(pnlStr) — exit triggered"
        }
    }

    var body: some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(item.symbol)
                        .font(.subheadline.weight(.semibold))
                    Text("Exit")
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(Color.appAmber)
                        .padding(.horizontal, 5)
                        .padding(.vertical, 2)
                        .background(Color.appAmber.opacity(0.12))
                        .clipShape(Capsule())
                }
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .padding(10)
    }
}

struct VariantBadge: View {
    let variant: String

    private var isActive: Bool { variant != "current" && !variant.isEmpty }

    var body: some View {
        Text(variant.isEmpty ? "current" : variant)
            .font(.caption2.weight(.bold))
            .lineLimit(1)
            .fixedSize(horizontal: true, vertical: false)
            .foregroundStyle(isActive ? Color.white : Color.appMuted)
            .padding(.horizontal, 6)
            .padding(.vertical, 3)
            .background(isActive ? Color.appAmber : Color(.tertiarySystemGroupedBackground))
            .clipShape(Capsule())
    }
}

struct VariantPanel: View {
    @ObservedObject var vm: AgentViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Label("Strategy variant", systemImage: "dial.medium")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(.secondary)
                Spacer()
                if !vm.lastOptimizedAt.isEmpty {
                    Text("Optimized \(vm.lastOptimizedAt)")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }

            if vm.variantWinRates.isEmpty {
                Text("No variant history yet — runs after first daily optimization.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                VStack(spacing: 6) {
                    ForEach(vm.variantWinRates) { vr in
                        HStack(spacing: 10) {
                            Text(vr.name)
                                .font(.caption.weight(.semibold))
                                .frame(width: 110, alignment: .leading)
                                .foregroundStyle(vr.name == vm.strategyModel.activeVariant ? Color.appAmber : .primary)

                            GeometryReader { proxy in
                                ZStack(alignment: .leading) {
                                    RoundedRectangle(cornerRadius: 3)
                                        .fill(Color(.tertiarySystemGroupedBackground))
                                        .frame(height: 8)
                                    RoundedRectangle(cornerRadius: 3)
                                        .fill(vr.name == vm.strategyModel.activeVariant ? Color.appAmber : Color.appBlue)
                                        .frame(width: proxy.size.width * vr.winRate, height: 8)
                                }
                            }
                            .frame(height: 8)

                            Text("\(Int(vr.winRate * 100))%")
                                .font(.caption2.weight(.bold))
                                .foregroundStyle(.secondary)
                                .frame(width: 32, alignment: .trailing)

                            Text("\(vr.wins)/\(vr.total)")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                                .frame(width: 36, alignment: .trailing)
                        }
                    }
                }
            }
        }
        .padding(12)
        .background(Color.appSurface)
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

// ── Gauge Components ──────────────────────────────────────────────────────────

/// Reusable semicircular gauge. value: 0.0–1.0, left = 0, right = 1, top = 0.5.
struct GaugeArcView: View {
    let value: Double
    let score: String
    let label: String
    var compact: Bool = false

    // Extreme Fear → Fear → Neutral → Greed → Extreme Greed
    // Also works as Strong Sell → Sell → Neutral → Buy → Strong Buy
    private let segments: [(upTo: Double, color: Color)] = [
        (0.25, Color(red: 0.83, green: 0.18, blue: 0.18)),
        (0.45, Color(red: 0.96, green: 0.49, blue: 0.0)),
        (0.55, Color(red: 0.85, green: 0.75, blue: 0.1)),
        (0.75, Color(red: 0.55, green: 0.76, blue: 0.29)),
        (1.00, Color(red: 0.22, green: 0.56, blue: 0.24)),
    ]

    var body: some View {
        let gW: CGFloat = compact ? 100 : 200
        let gH: CGFloat = compact ? 62  : 118

        ZStack {
            Canvas { context, size in
                let cx    = size.width / 2
                let cy    = size.height * 0.90
                let r     = min(size.width * 0.46, size.height * 0.96)
                let arcW  = r * (compact ? 0.21 : 0.19)
                let trackR = r - arcW / 2

                // Background track
                var bg = Path()
                bg.addArc(center: CGPoint(x: cx, y: cy),
                          radius: trackR,
                          startAngle: .degrees(180), endAngle: .degrees(360),
                          clockwise: true)
                context.stroke(bg, with: .color(.secondary.opacity(0.1)), lineWidth: arcW)

                // Colored segments
                var prev = 0.0
                for seg in segments {
                    var path = Path()
                    path.addArc(center: CGPoint(x: cx, y: cy),
                                radius: trackR,
                                startAngle: .degrees(180 + prev * 180),
                                endAngle:   .degrees(180 + seg.upTo * 180),
                                clockwise: true)
                    context.stroke(path, with: .color(seg.color.opacity(0.88)), lineWidth: arcW)
                    prev = seg.upTo
                }

                // Highlight active segment with a brighter inner ring
                let activeColor = activeSegmentColor
                var highlight = Path()
                highlight.addArc(center: CGPoint(x: cx, y: cy),
                                 radius: trackR,
                                 startAngle: .degrees(180 + value * 180 - 8),
                                 endAngle:   .degrees(180 + value * 180 + 8),
                                 clockwise: true)
                context.stroke(highlight, with: .color(activeColor), lineWidth: arcW * 1.35)

                // Needle
                let angle = (180.0 + value * 180.0) * .pi / 180.0
                let needleLen = trackR * 0.80
                let tip = CGPoint(x: cx + cos(angle) * needleLen,
                                  y: cy + sin(angle) * needleLen)
                var needle = Path()
                needle.move(to: CGPoint(x: cx, y: cy))
                needle.addLine(to: tip)
                context.stroke(needle, with: .color(.primary),
                               style: StrokeStyle(lineWidth: compact ? 1.8 : 2.8, lineCap: .round))

                // Center dot
                let dot: CGFloat = compact ? 4 : 5.5
                context.fill(
                    Path(ellipseIn: CGRect(x: cx-dot, y: cy-dot, width: dot*2, height: dot*2)),
                    with: .color(.primary)
                )
            }

            // Score + label text
            VStack(spacing: compact ? 0 : 1) {
                Spacer()
                Text(score)
                    .font(compact ? .caption.weight(.bold) : .title2.weight(.bold))
                    .foregroundStyle(.primary)
                Text(label)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .lineLimit(2)
            }
            .padding(.bottom, compact ? 3 : 5)
        }
        .frame(width: gW, height: gH)
    }

    private var activeSegmentColor: Color {
        switch value {
        case 0..<0.25: return Color(red: 0.83, green: 0.18, blue: 0.18)
        case 0.25..<0.45: return Color(red: 0.96, green: 0.49, blue: 0.0)
        case 0.45..<0.55: return Color(red: 0.85, green: 0.75, blue: 0.1)
        case 0.55..<0.75: return Color(red: 0.55, green: 0.76, blue: 0.29)
        default: return Color(red: 0.22, green: 0.56, blue: 0.24)
        }
    }
}

struct SentimentGaugePanel: View {
    @ObservedObject var vm: AgentViewModel

    private var sentimentScore: Int {
        guard !vm.signalInsights.isEmpty else {
            switch vm.overview.regime {
            case "BULL": return 72
            case "BEAR": return 28
            default:     return 50
            }
        }
        let top = Array(vm.signalInsights.prefix(5))
        return top.map(\.score).reduce(0, +) / top.count
    }

    private var sentimentLabel: String {
        switch sentimentScore {
        case 0..<25:  return "EXTREME\nFEAR"
        case 25..<45: return "FEAR"
        case 45..<55: return "NEUTRAL"
        case 55..<75: return "GREED"
        default:       return "EXTREME\nGREED"
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Market Sentiment", systemImage: "gauge.medium")
                .font(.caption.weight(.bold))
                .foregroundStyle(.secondary)

            HStack(alignment: .center, spacing: 4) {
                GaugeArcView(
                    value: Double(sentimentScore) / 100.0,
                    score: "\(sentimentScore)",
                    label: sentimentLabel
                )

                Spacer()

                VStack(alignment: .trailing, spacing: 10) {
                    GaugeSideRow(label: "Portfolio", value: alignmentLabel, dot: alignmentDot)
                    GaugeSideRow(label: "Positions", value: positionsSummary, dot: nil)
                }
                .padding(.trailing, 4)
            }
        }
        .padding(12)
        .background(Color.appSurface)
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    // Is the portfolio positioned with or against the market?
    private var alignmentLabel: String {
        let regime  = vm.overview.regime
        let posCount = vm.positions.count
        let hasExits = !vm.exitRecommendations.isEmpty
        if posCount == 0 { return "In cash" }
        if hasExits      { return "Needs attention" }
        switch regime {
        case "BULL": return "Aligned ↑"
        case "BEAR": return "Exposed ↓"
        default:     return "Cautious"
        }
    }

    private var alignmentDot: Color? {
        let regime  = vm.overview.regime
        let posCount = vm.positions.count
        let hasExits = !vm.exitRecommendations.isEmpty
        if posCount == 0 { return .appMuted }
        if hasExits      { return .appAmber }
        switch regime {
        case "BULL": return .appGreen
        case "BEAR": return .appRed
        default:     return .appAmber
        }
    }

    private var positionsSummary: String {
        let n = vm.positions.count
        if n == 0 { return "None open" }
        let winners = vm.positions.filter { $0.unrealizedPnl > 0 }.count
        let losers  = vm.positions.filter { $0.unrealizedPnl < 0 }.count
        if losers == 0 { return "\(n) open, all up" }
        if winners == 0 { return "\(n) open, all down" }
        return "\(n) open · \(winners) up, \(losers) down"
    }
}

struct GaugeSideRow: View {
    let label: String
    let value: String
    let dot: Color?

    var body: some View {
        HStack(spacing: 6) {
            VStack(alignment: .trailing, spacing: 1) {
                Text(label).font(.caption2).foregroundStyle(.secondary)
                Text(value).font(.caption.weight(.semibold))
            }
            if let dot {
                Circle().fill(dot).frame(width: 8, height: 8)
            }
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
    let error: String?
    let refresh: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Label("What happened", systemImage: "clock.arrow.circlepath")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(.secondary)
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
                if let error {
                    ActivityLogRow(
                        item: ActivityLogItem(
                            title: "Activity unavailable",
                            detail: error,
                            time: Date(),
                            variant: .danger
                        )
                    )
                    if !items.isEmpty {
                        Divider().padding(.leading, 14)
                    }
                }
                ForEach(Array(displayItems.prefix(4))) { item in
                    ActivityLogRow(item: item)
                    if item.id != displayItems.prefix(4).last?.id {
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

    private var displayItems: [ActivityLogItem] {
        let exits = items.filter { $0.title == "Exit signal triggered" }
        let pendingSells = items.filter { $0.title.hasPrefix("Pending sell") }
        var grouped: [ActivityLogItem] = []

        if !exits.isEmpty {
            let symbols = exits.compactMap { symbol(from: $0.detail) }
            let reason = dominantExitReason(in: exits)
            let shown = symbols.prefix(5).joined(separator: ", ")
            let more = symbols.count > 5 ? " +\(symbols.count - 5) more" : ""
            grouped.append(ActivityLogItem(
                title: "Exit review: \(exits.count) position\(exits.count == 1 ? "" : "s")",
                detail: "\(reason). \(shown.isEmpty ? "The agent is cutting risk." : "\(shown)\(more) need sell review.")",
                time: exits.compactMap(\.time).max(),
                variant: .alert
            ))
        }

        if !pendingSells.isEmpty {
            let symbols = pendingSells.map { $0.title.replacingOccurrences(of: "Pending sell ", with: "") }
            let shown = symbols.prefix(5).joined(separator: ", ")
            grouped.append(ActivityLogItem(
                title: "Sell orders queued",
                detail: shown.isEmpty ? "\(pendingSells.count) sell order\(pendingSells.count == 1 ? "" : "s") waiting." : "\(shown) waiting for broker execution.",
                time: pendingSells.compactMap(\.time).max(),
                variant: .alert
            ))
        }

        let rest = items.filter { item in
            item.title != "Exit signal triggered" && !item.title.hasPrefix("Pending sell")
        }
        return grouped + rest
    }

    private func symbol(from detail: String) -> String? {
        let head = detail.split(separator: ":").first.map(String.init) ?? ""
        return head.isEmpty ? nil : head
    }

    private func dominantExitReason(in exits: [ActivityLogItem]) -> String {
        let text = exits.map(\.detail).joined(separator: " ").lowercased()
        if text.contains("loss stop") {
            return "Loss stops fired"
        }
        if text.contains("macd") {
            return "Momentum turned against the book"
        }
        if text.contains("avwap") {
            return "Prices lost AVWAP support"
        }
        return "Exit rules fired"
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
                .frame(width: 7, height: 7)
                .padding(.top, 6)

            VStack(alignment: .leading, spacing: 4) {
                Text(item.title)
                    .font(.headline.weight(.bold))
                    .fixedSize(horizontal: false, vertical: true)
                Text(item.detail)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                if let time = item.time {
                    Text(time, style: .time)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(10)
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
            .lineLimit(1)
            .fixedSize(horizontal: true, vertical: false)
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

// ── ModelPanel — strategy settings in plain English with dials ────────────────
struct ModelPanel: View {
    @ObservedObject var vm: AgentViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // Header
            HStack {
                Label("How the strategy is tuned", systemImage: "slider.horizontal.3")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(.secondary)
                Spacer()
                if vm.strategyModel.generation > 0 {
                    Text("Auto-tuned \(vm.strategyModel.generation)×")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(.bottom, 14)

            // Three dials
            HStack(spacing: 0) {
                ModelDial(
                    label: "Entry bar",
                    sublabel: convictionLabel,
                    value: Double(vm.strategyModel.minConviction),
                    minVal: 50, maxVal: 95,
                    display: "\(vm.strategyModel.minConviction)"
                )
                Divider().frame(height: 72)
                ModelDial(
                    label: "Bet size",
                    sublabel: "per trade",
                    value: vm.strategyModel.positionSizePct * 100,
                    minVal: 1, maxVal: 20,
                    display: "\(Int(vm.strategyModel.positionSizePct * 100))%"
                )
                Divider().frame(height: 72)
                ModelDial(
                    label: "Max hold",
                    sublabel: "days",
                    value: Double(vm.strategyModel.maxHoldingDays),
                    minVal: 1, maxVal: 10,
                    display: "\(vm.strategyModel.maxHoldingDays)d"
                )
            }
            .frame(maxWidth: .infinity)

            Divider().padding(.vertical, 12)

            // Plain-English settings rows
            VStack(spacing: 8) {
                ModelRow(label: "Takes profits when up", value: "\(String(format: "%.1f", vm.strategyModel.profitLockTriggerPct))%")
                ModelRow(label: "Exits if gain drops by", value: "\(String(format: "%.1f", vm.strategyModel.profitGivebackPct))%")
                ModelRow(label: "Max open trades", value: "\(vm.strategyModel.maxPositions)")
                if !vm.strategyModel.activeVariant.isEmpty && vm.strategyModel.activeVariant != "current" {
                    ModelRow(label: "Current style", value: vm.strategyModel.activeVariant.capitalized)
                }
            }
        }
        .padding(14)
        .background(Color.appSurface)
        .clipShape(RoundedRectangle(cornerRadius: 14))
    }

    private var convictionLabel: String {
        switch vm.strategyModel.minConviction {
        case 85...: return "very strict"
        case 75...: return "strict"
        case 65...: return "moderate"
        default:    return "loose"
        }
    }
}

struct ModelDial: View {
    let label: String
    let sublabel: String
    let value: Double
    let minVal: Double
    let maxVal: Double
    let display: String

    var body: some View {
        VStack(spacing: 4) {
            Gauge(value: max(minVal, min(maxVal, value)), in: minVal...maxVal) {
                EmptyView()
            } currentValueLabel: {
                Text(display)
                    .font(.caption2.weight(.bold))
            }
            .gaugeStyle(.accessoryCircularCapacity)
            .tint(Color.appBlue)
            .frame(width: 58, height: 58)

            Text(label)
                .font(.caption2.weight(.medium))
                .foregroundStyle(.primary)
            Text(sublabel)
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
    }
}

struct ModelRow: View {
    let label: String
    let value: String

    var body: some View {
        HStack {
            Text(label)
                .font(.subheadline)
                .foregroundStyle(.primary)
            Spacer()
            Text(value)
                .font(.subheadline.weight(.semibold))
                .foregroundColor(Color.appBlue)
        }
    }
}

#Preview {
    ContentView()
}
