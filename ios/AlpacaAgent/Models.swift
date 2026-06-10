import Foundation

// ── Account stats (from /api/lab/overview) ────────────────────────────────────
struct OverviewData {
    var equity:    Double = 0
    var lastEquity: Double = 0
    var cash:      Double = 0
    var posCount:  Int    = 0
    var openPnl:   Double = 0
    var regime:    String = "—"
    var isOpen:    Bool   = false
    var nextOpen:  String = ""

    var hasDailyPnl: Bool { lastEquity > 0 && equity > 0 }
    var dailyPnl: Double { hasDailyPnl ? equity - lastEquity : 0 }
    var dailyPnlPct: Double { hasDailyPnl ? (equity - lastEquity) / lastEquity * 100 : 0 }
    var displayPnl: Double { openPnl != 0 ? openPnl : dailyPnl }
}

enum PortfolioRange: String, CaseIterable, Identifiable {
    case day = "Day"
    case week = "Week"
    case month = "Month"
    case quarter = "Qtr"
    case ytd = "YTD"
    case custom = "Custom"

    var id: String { rawValue }
}

struct PortfolioPoint: Identifiable {
    let id = UUID()
    let time: Date
    let equity: Double
}

struct PositionRow: Identifiable {
    var id: String { symbol }
    let symbol: String
    let side: String
    let status: String
    let theme: String
    let qty: Double
    let entryPrice: Double
    let currentPrice: Double
    let currentValue: Double
    let currentWeightPct: Double
    let unrealizedPnl: Double
    let unrealizedPnlPct: Double
    let action: String
    let reason: String
}

struct ExitRecommendation: Identifiable {
    var id: String { symbol + reason }
    let symbol: String
    let side: String
    let quantity: Double
    let reason: String
    let unrealizedPnl: Double
    let unrealizedPnlPct: Double
    let peakUnrealizedPnlPct: Double
    let givebackPct: Double
    let holdingDays: Int
}

struct AgentDecision {
    var action: String = "wait"
    var severity: MessageVariant = .normal
    var summary: String = "Waiting for agent state."
    var riskStatus: String = "--"
    var riskAction: String = "--"
}

struct PortfolioNarrative {
    var sentimentFrom: String = "market"
    var sentimentTo: String = "unclear"
    var summary: String = "Waiting for portfolio thesis."
    var why: [String] = []
    var nextActions: [String] = []
    var modelAdjustment: String = "Waiting for model state."
}

struct StrategyModelState {
    var generation: Int = 0
    var minConviction: Int = 75
    var maxPositions: Int = 3
    var positionSizePct: Double = 0.05
    var trailingStopPct: Double = 3.0
    var profitLockTriggerPct: Double = 2.0
    var profitGivebackPct: Double = 1.0
    var maxHoldingDays: Int = 2
    var exitOnRegimeFlip: Bool = true
    var activeVariant: String = "current"
}

struct VariantWinRate: Identifiable {
    var id: String { name }
    let name: String
    let wins: Int
    let total: Int
    let winRate: Double       // 0–1
    let avgObjective: Double
}

/// Per-indicator result from the 6-signal confluence engine.
struct SignalIndicator {
    let status: String   // "bullish" | "neutral" | "bearish"
    let label: String    // plain-English description from the engine
    let points: Int      // weighted points this indicator contributed
    let weight: Int      // max possible points for this indicator

    var isBullish:  Bool { status == "bullish" }
    var isNeutral:  Bool { status == "neutral" }
    var isBearish:  Bool { status == "bearish" }

    /// Normalized 0–1 fill (relative contribution to total)
    var fillFraction: Double {
        guard weight > 0 else { return 0 }
        return Double(max(0, points)) / Double(weight)
    }
}

/// Indicator + its display name — Identifiable for use in ForEach.
struct NamedIndicator: Identifiable {
    var id: String { name }
    let name: String
    let indicator: SignalIndicator
}

struct SignalInsight: Identifiable {
    var id: String { symbol }
    let symbol: String
    let score: Int
    let regime: String
    let changePct: Double
    let rsi14: Double
    let macdHist: Double
    let emaTrend: String
    let priceVsVwapPct: Double
    let priceVsAvwapLowPct: Double
    let volumeRatio: Double
    let trendDirection: String
    let priceVsTrendPct: Double
    let fibPosition: String
    let lastPrice: Double
    let reasons: [String]
    /// Keyed by indicator name: "rsi" | "macd" | "avwap" | "ema" | "trend" | "price_action"
    var signalBreakdown: [String: SignalIndicator] = [:]

    /// Ordered indicators for display.
    /// Uses server-provided signal_breakdown when available; falls back to raw technical fields.
    var orderedIndicators: [NamedIndicator] {
        let order = ["rsi", "macd", "avwap", "ema", "trend", "price_action"]
        let fromBreakdown = order.compactMap { key in
            signalBreakdown[key].map { NamedIndicator(name: displayName(for: key), indicator: $0) }
        }
        if !fromBreakdown.isEmpty { return fromBreakdown }

        // ── Fallback: derive from raw technical fields until server redeploy ──
        let rsiInd: SignalIndicator = {
            if rsi14 > 55 { return SignalIndicator(status: "bullish", label: "RSI \(Int(rsi14)) — momentum up",   points: 1, weight: 1) }
            if rsi14 < 45 { return SignalIndicator(status: "bearish", label: "RSI \(Int(rsi14)) — momentum weak", points: 0, weight: 1) }
            return SignalIndicator(status: "neutral", label: "RSI \(Int(rsi14)) — neutral", points: 0, weight: 1)
        }()
        let macdInd: SignalIndicator = {
            if macdHist > 0.01  { return SignalIndicator(status: "bullish", label: "MACD hist positive", points: 1, weight: 1) }
            if macdHist < -0.01 { return SignalIndicator(status: "bearish", label: "MACD hist negative", points: 0, weight: 1) }
            return SignalIndicator(status: "neutral", label: "MACD flat", points: 0, weight: 1)
        }()
        let avwapInd: SignalIndicator = {
            if priceVsAvwapLowPct > 0.5  { return SignalIndicator(status: "bullish", label: "Above AVWAP +\(String(format: "%.1f", priceVsAvwapLowPct))%", points: 1, weight: 1) }
            if priceVsAvwapLowPct < -0.5 { return SignalIndicator(status: "bearish", label: "Below AVWAP \(String(format: "%.1f", priceVsAvwapLowPct))%",  points: 0, weight: 1) }
            return SignalIndicator(status: "neutral", label: "Near AVWAP", points: 0, weight: 1)
        }()
        let emaInd: SignalIndicator = {
            switch emaTrend {
            case "bullish": return SignalIndicator(status: "bullish", label: "EMA9 > EMA21", points: 1, weight: 1)
            case "bearish": return SignalIndicator(status: "bearish", label: "EMA9 < EMA21", points: 0, weight: 1)
            default:        return SignalIndicator(status: "neutral", label: "EMA flat",     points: 0, weight: 1)
            }
        }()
        let trendInd: SignalIndicator = {
            switch trendDirection {
            case "up":   return SignalIndicator(status: "bullish", label: "Uptrend",   points: 1, weight: 1)
            case "down": return SignalIndicator(status: "bearish", label: "Downtrend", points: 0, weight: 1)
            default:     return SignalIndicator(status: "neutral", label: "Sideways",  points: 0, weight: 1)
            }
        }()
        return [
            NamedIndicator(name: "RSI",   indicator: rsiInd),
            NamedIndicator(name: "MACD",  indicator: macdInd),
            NamedIndicator(name: "AVWAP", indicator: avwapInd),
            NamedIndicator(name: "EMA",   indicator: emaInd),
            NamedIndicator(name: "TREND", indicator: trendInd),
        ]
    }

    private func displayName(for key: String) -> String {
        key == "price_action" ? "PRICE" : key.uppercased()
    }

    /// Best plain-English label from the strongest bullish indicator
    var topLabel: String {
        let bullish = orderedIndicators.filter { $0.indicator.isBullish }
        return bullish.max(by: { $0.indicator.points < $1.indicator.points })?.indicator.label
            ?? orderedIndicators.first?.indicator.label
            ?? reasons.first
            ?? "Signal triggered"
    }

    /// Count of bullish indicators (0–5)
    var bullishCount: Int { orderedIndicators.filter { $0.indicator.isBullish }.count }
}

struct ActivityLogItem: Identifiable {
    let id = UUID()
    let title: String
    let detail: String
    let time: Date?
    let variant: MessageVariant
}

// ── Chat messages ─────────────────────────────────────────────────────────────
enum MessageRole { case agent, user }
enum MessageVariant { case normal, trade, alert, danger }

struct ChatMessage: Identifiable {
    let id = UUID()
    let role: MessageRole
    let text: String          // plain text (no HTML)
    let variant: MessageVariant
    let time: Date

    init(_ text: String, role: MessageRole, variant: MessageVariant = .normal) {
        self.text = text
        self.role = role
        self.variant = variant
        self.time = Date()
    }
}

struct TradeOrder: Identifiable {
    let id = UUID()
    let side: String
    let quantity: Int
    let symbol: String
    let raw: [String: Any]

    var label: String {
        "\(side.uppercased()) \(quantity) \(symbol)"
    }

    var isSell: Bool {
        side.lowercased() == "sell"
    }
}

struct PendingTradeProposal: Identifiable {
    let id = UUID()
    let orders: [TradeOrder]
    let summary: String

    var orderDescription: String {
        orders.map(\.label).joined(separator: ", ")
    }
}
