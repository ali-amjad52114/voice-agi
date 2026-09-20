export type TaskStatus = "planning" | "running" | "complete" | "failed"
export type AgentKind = "call" | "web"
export type AgentStatus = "queued" | "active" | "done" | "failed"
export type BusinessType = "mechanic" | "dealer" | "parts"
export type CallOutcome = "quote" | "voicemail" | "refused" | "error"

export interface Facts {
  allInPrice?: number
  laborRatePerHour?: number
  laborHours?: number
  acceptsCustomerParts?: boolean
  partsType?: "oem" | "aftermarket"
  warrantyMonths?: number
  earliestSlot?: string
  partPrice?: number // web agent: online price; call agent: the shop's own part price
  confidence: number
}

export interface TranscriptLine {
  role: "agent" | "business"
  text: string
  t: number
}

export interface Agent {
  id: string
  taskId: string
  kind: AgentKind
  status: AgentStatus
  business: { name: string; type: BusinessType; phone?: string; url?: string }
  summary?: string
  facts?: Facts
  call?: { durationS: number; answeredBy?: string; outcome: CallOutcome }
  transcript?: TranscriptLine[]
}

export interface ResultOption {
  label: string
  total: number
  breakdown: string
  agentIds: string[]
}

export interface Result {
  options: ResultOption[]
  recommendedOptionIndex: number
  recommendedAgentId: string
  why: string
  savingsVsQuote?: number
  tradeoffs?: string[] // short bullets: warranty, hassle, timing
}

export interface Task {
  id: string
  title: string
  request: string
  createdAt: string
  status: TaskStatus
  userQuote?: number
  agents: Agent[]
  result?: Result
}

export type TaskEvent =
  | { type: "task.updated"; task: Task }
  | { type: "agent.updated"; agent: Agent }
  | { type: "task.result"; taskId: string; result: Result }
  | { type: "result.partial"; taskId: string; whyDelta: string } // live why while synthesize streams
  | { type: "error"; message: string }
