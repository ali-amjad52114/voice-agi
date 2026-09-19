export type PipelineStatus = "idle" | "connecting" | "live" | "error"

export interface Channels {
  "set-url": {url: string}
  "connect": {url?: string}
  "disconnect": Record<string, never>
  "status": {
    state: PipelineStatus
    url: string
    error: string | null
    micFrames: number
    lastFormat: string
  }
}

declare global {
  // eslint-disable-next-line no-var
  var mentra: import("@mentra/miniapp/ui").MentraTyped<Channels>
}
