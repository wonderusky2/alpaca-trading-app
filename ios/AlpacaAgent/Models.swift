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
    let reasons: [String]
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
