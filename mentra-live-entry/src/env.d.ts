declare namespace NodeJS {
  interface ProcessEnv {
    MENTRA_PUBLIC_PIPECAT_WS_URL?: string
  }
}

declare const process: {env: NodeJS.ProcessEnv}
