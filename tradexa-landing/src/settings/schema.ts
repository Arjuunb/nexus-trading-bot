import { z } from "zod";

/**
 * The complete, typed settings model for the TradeLogX Nexus. Every field
 * is a real, purposeful setting — no placeholders. The schema is the single
 * source of truth: the store validates against it, forms derive from it, and
 * the (optional) backend receives exactly this shape.
 */

export const themeEnum = z.enum(["dark", "light", "system"]);
export const orderTypeEnum = z.enum(["market", "limit", "stop_limit"]);
export const riskModeEnum = z.enum(["conservative", "balanced", "aggressive"]);
export const positionSizingEnum = z.enum(["fixed", "percent_equity", "risk_based", "kelly"]);
export const experienceEnum = z.enum(["beginner", "intermediate", "advanced", "professional"]);
export const aiModelEnum = z.enum(["decision-brain-v3", "decision-brain-v2", "ema-baseline"]);

export const profileSchema = z.object({
  avatarUrl: z.string().default(""),
  fullName: z.string().max(80).default(""),
  username: z.string().max(40).default(""),
  email: z.string().email().or(z.literal("")).default(""),
  phone: z.string().max(24).default(""),
  country: z.string().default(""),
  timezone: z.string().default("UTC"),
  language: z.string().default("en"),
  experience: experienceEnum.default("intermediate"),
  bio: z.string().max(280).default(""),
});

export const notificationsSchema = z.object({
  channels: z.object({
    email: z.boolean().default(true),
    push: z.boolean().default(false),
    desktop: z.boolean().default(true),
    sms: z.boolean().default(false),
    telegram: z.boolean().default(false),
    discord: z.boolean().default(false),
    webhook: z.boolean().default(false),
  }),
  events: z.object({
    botStarted: z.boolean().default(true),
    botStopped: z.boolean().default(true),
    tradeOpened: z.boolean().default(true),
    tradeClosed: z.boolean().default(true),
    slHit: z.boolean().default(true),
    tpHit: z.boolean().default(true),
    dailyReport: z.boolean().default(true),
    weeklyReport: z.boolean().default(false),
    monthlyReport: z.boolean().default(false),
    systemErrors: z.boolean().default(true),
    exchangeErrors: z.boolean().default(true),
    apiErrors: z.boolean().default(true),
  }),
});

export const tradingSchema = z.object({
  defaultLeverage: z.number().min(1).max(125).default(1),
  preferredExchange: z.string().default("kraken"),
  pairs: z.array(z.string()).default(["BTCUSDT", "ETHUSDT", "SOLUSDT"]),
  riskMode: riskModeEnum.default("balanced"),
  positionSizing: positionSizingEnum.default("risk_based"),
  maxSimultaneousTrades: z.number().min(1).max(50).default(3),
  defaultTimeframe: z.string().default("4h"),
  slippageTolerance: z.number().min(0).max(5).default(0.1),
  commission: z.number().min(0).max(2).default(0.04),
  spread: z.number().min(0).max(2).default(0.02),
  orderType: orderTypeEnum.default("limit"),
});

export const riskSchema = z.object({
  dailyLossLimit: z.number().min(0).max(100).default(3),
  weeklyLoss: z.number().min(0).max(100).default(8),
  monthlyLoss: z.number().min(0).max(100).default(15),
  maxDrawdown: z.number().min(0).max(100).default(20),
  riskPerTrade: z.number().min(0).max(10).default(1),
  maxLeverage: z.number().min(1).max(125).default(3),
  maxPositionSize: z.number().min(0).max(100).default(25),
  maxExposure: z.number().min(0).max(100).default(60),
  maxLosingStreak: z.number().min(0).max(50).default(5),
  tradingCooldownMin: z.number().min(0).max(1440).default(30),
  circuitBreaker: z.boolean().default(true),
  emergencyStop: z.boolean().default(false),
  autoCloseAll: z.boolean().default(false),
  autoPauseAfterDrawdown: z.boolean().default(true),
});

export const aiSchema = z.object({
  enabled: z.boolean().default(true),
  model: aiModelEnum.default("decision-brain-v3"),
  confidenceThreshold: z.number().min(0).max(100).default(60),
  tradeExplanation: z.boolean().default(true),
  tradeScoring: z.boolean().default(true),
  signalFiltering: z.boolean().default(true),
  riskSuggestions: z.boolean().default(true),
  learningMode: z.boolean().default(true),
  memory: z.boolean().default(true),
  prompt: z.string().max(1000).default(""),
});

export const automationSchema = z.object({
  autoStart: z.boolean().default(false),
  autoRestart: z.boolean().default(true),
  autoReconnect: z.boolean().default(true),
  autoUpdate: z.boolean().default(false),
  autoBackups: z.boolean().default(true),
  autoSync: z.boolean().default(true),
  autoJournalExport: z.boolean().default(false),
  autoReportGeneration: z.boolean().default(true),
});

export const schedulerSchema = z.object({
  tradingHoursStart: z.number().min(0).max(23).default(0),
  tradingHoursEnd: z.number().min(0).max(24).default(24),
  tradingDays: z.array(z.number().min(0).max(6)).default([0, 1, 2, 3, 4, 5, 6]),
  holidayMode: z.boolean().default(false),
  maintenanceWindow: z.string().default(""),
});

export const portfolioSchema = z.object({
  baseCurrency: z.string().default("USDT"),
  profitTarget: z.number().min(0).default(0),
  maxAllocation: z.number().min(0).max(100).default(100),
});

export const appearanceSchema = z.object({
  theme: themeEnum.default("dark"),
  accent: z.string().default("#EAB54F"),
  chartUp: z.string().default("#22C55E"),
  chartDown: z.string().default("#EF4444"),
  compact: z.boolean().default(false),
  animations: z.boolean().default(true),
});

export const regionSchema = z.object({
  language: z.string().default("en"),
  dateFormat: z.enum(["YYYY-MM-DD", "DD/MM/YYYY", "MM/DD/YYYY"]).default("YYYY-MM-DD"),
  timeFormat: z.enum(["24h", "12h"]).default("24h"),
  currency: z.string().default("USD"),
  numberFormat: z.enum(["1,234.56", "1.234,56", "1 234.56"]).default("1,234.56"),
  timezone: z.string().default("UTC"),
});

export const privacySchema = z.object({
  analytics: z.boolean().default(false),
  functionalCookies: z.boolean().default(true),
  marketingCookies: z.boolean().default(false),
  shareUsageData: z.boolean().default(false),
});

export const advancedSchema = z.object({
  developerMode: z.boolean().default(false),
  debugMode: z.boolean().default(false),
  experimentalFeatures: z.boolean().default(false),
  performanceMode: z.boolean().default(false),
  sandboxMode: z.boolean().default(false),
  // Live trading is hard-locked by design across the whole platform.
  paperTrading: z.literal(true).default(true),
  liveTrading: z.literal(false).default(false),
});

export const settingsSchema = z.object({
  profile: profileSchema,
  notifications: notificationsSchema,
  trading: tradingSchema,
  risk: riskSchema,
  ai: aiSchema,
  automation: automationSchema,
  scheduler: schedulerSchema,
  portfolio: portfolioSchema,
  appearance: appearanceSchema,
  region: regionSchema,
  privacy: privacySchema,
  advanced: advancedSchema,
});

export type Settings = z.infer<typeof settingsSchema>;
export type SettingsSection = keyof Settings;

/** Fully-defaulted settings object (every field resolves to its default). */
export const defaultSettings: Settings = settingsSchema.parse({
  profile: {},
  notifications: { channels: {}, events: {} },
  trading: {},
  risk: {},
  ai: {},
  automation: {},
  scheduler: {},
  portfolio: {},
  appearance: {},
  region: {},
  privacy: {},
  advanced: {},
});
