/**
 * Mentra Live = glasses I/O only.
 * Mic PCM → Pipecat /ws-client → Gradium STT → Gemma → Gradium TTS → speaker PCM.
 */

import {base64ToBytes, registerMiniapp} from "@mentra/miniapp/background"
import type {Channels, PipelineStatus} from "../shared/channels"
import {parseSampleRate, toPipelinePcm} from "../shared/pcm"

const STORAGE_URL_KEY = "pipecatWsUrl"
const SPEAKER_RATE = 24000
const DEFAULT_URL = process.env.MENTRA_PUBLIC_PIPECAT_WS_URL || "ws://127.0.0.1:7860/ws-client"

type SpeakerWriter = {
  write(chunk: Uint8Array | ArrayBuffer): Promise<{bufferedMs: number}>
  writeBase64(b64: string): Promise<{bufferedMs: number}>
  close(): Promise<{durationMs?: number}>
  abort(): Promise<void>
}

registerMiniapp<Channels>(async (session) => {
  let url = (await session.storage.get(STORAGE_URL_KEY)) || DEFAULT_URL
  let state: PipelineStatus = "idle"
  let error: string | null = null
  let micFrames = 0
  let lastFormat = "none"
  let socket: WebSocket | null = null
  let writer: SpeakerWriter | null = null
  let writerGeneration = 0
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  let wantLive = true

  const pushStatus = () => {
    session.ui.send("status", {state, url, error, micFrames, lastFormat})
  }

  const setState = (next: PipelineStatus, nextError: string | null = null) => {
    state = next
    error = nextError
    pushStatus()
  }

  const abortSpeaker = async () => {
    writerGeneration += 1
    if (!writer) return
    const current = writer
    writer = null
    try {
      await current.abort()
    } catch {
      /* already closed */
    }
  }

  const ensureSpeaker = async (): Promise<SpeakerWriter | null> => {
    if (writer) return writer
    const generation = writerGeneration
    try {
      const created = await session.speaker.createStream({
        sampleRate: SPEAKER_RATE,
        volume: 1,
        stopOtherAudio: true,
      })
      if (generation !== writerGeneration) {
        await created.abort()
        return null
      }
      writer = created
      return writer
    } catch (err) {
      setState("error", `speaker: ${err instanceof Error ? err.message : String(err)}`)
      return null
    }
  }

  const playPcm = async (pcm: Uint8Array) => {
    const stream = await ensureSpeaker()
    if (!stream || pcm.byteLength < 2) return
    await stream.write(pcm)
  }

  const handleServerMessage = (raw: unknown) => {
    if (raw instanceof ArrayBuffer) {
      void playPcm(new Uint8Array(raw))
      return
    }
    if (typeof raw !== "string") return
    try {
      const msg = JSON.parse(raw) as {type?: string; data?: string}
      if (msg.type === "interrupt") {
        void abortSpeaker()
        return
      }
      if (msg.type === "audio" && msg.data) {
        void playPcm(base64ToBytes(msg.data))
      }
    } catch {
      /* ignore non-JSON text */
    }
  }

  const stopMic = () => {
    try {
      session.mic.stop()
    } catch {
      /* ignore */
    }
  }

  const startMic = () => {
    stopMic()
    void session.mic.setVoiceActivityDetectionEnabled(false).catch(() => undefined)
    void session.mic.setLoudnessGateEnabled(false).catch(() => undefined)
    session.mic.onAudioChunk((chunk) => {
      if (!socket || socket.readyState !== WebSocket.OPEN) return
      const format = chunk.format ?? "pcm"
      if (format.toLowerCase().includes("lc3")) {
        lastFormat = format
        setState("error", "Glasses sent LC3. Mentra App must deliver decoded PCM.")
        return
      }
      const fromRate = parseSampleRate(chunk.sampleRate, chunk.format)
      const pcm = toPipelinePcm(base64ToBytes(chunk.data), fromRate)
      lastFormat = `${fromRate}→16000 ${format}`
      micFrames += 1
      const payload =
        pcm.byteOffset === 0 && pcm.byteLength === pcm.buffer.byteLength
          ? pcm.buffer
          : pcm.slice().buffer
      try {
        socket.send(payload)
      } catch (err) {
        setState("error", `mic send: ${err instanceof Error ? err.message : String(err)}`)
      }
      if (micFrames === 1 || micFrames % 25 === 0) pushStatus()
    })
  }

  const disconnect = async (keepWanted = false) => {
    if (!keepWanted) wantLive = false
    if (reconnectTimer) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    stopMic()
    await abortSpeaker()
    if (socket) {
      const current = socket
      socket = null
      try {
        current.close()
      } catch {
        /* ignore */
      }
    }
    if (!keepWanted) setState("idle")
  }

  const connect = async (nextUrl?: string) => {
    wantLive = true
    if (nextUrl && nextUrl !== url) {
      url = nextUrl
      await session.storage.set(STORAGE_URL_KEY, url)
    }
    await disconnect(true)
    setState("connecting")
    try {
      const ws = new WebSocket(url)
      socket = ws
      try {
        ws.binaryType = "arraybuffer"
      } catch {
        /* JSContext may ignore this */
      }
      ws.onopen = () => {
        ws.send(
          JSON.stringify({
            type: "hello",
            sampleRate: 16000,
            format: "pcm_s16le",
          }),
        )
        startMic()
        setState("live")
      }
      ws.onmessage = (event) => {
        handleServerMessage(event.data)
      }
      ws.onerror = () => {
        setState("error", "WebSocket error — check laptop IP and that bot.py is running")
      }
      ws.onclose = () => {
        if (socket !== ws) return
        socket = null
        stopMic()
        void abortSpeaker()
        if (!wantLive) {
          setState("idle")
          return
        }
        setState("error", "Pipeline socket closed; retrying")
        reconnectTimer = setTimeout(() => {
          if (wantLive) void connect()
        }, 1500)
      }
    } catch (err) {
      setState("error", err instanceof Error ? err.message : String(err))
    }
  }

  session.ui.onOpen(() => {
    pushStatus()
  })
  session.ui.on("set-url", ({url: next}) => {
    void (async () => {
      url = next
      await session.storage.set(STORAGE_URL_KEY, url)
      pushStatus()
    })()
  })
  session.ui.on("connect", ({url: next}) => {
    void connect(next)
  })
  session.ui.on("disconnect", () => {
    void disconnect()
  })

  session.input.onButtonPress(() => {
    if (state === "live") {
      void disconnect()
      return
    }
    void connect()
  })

  void connect()
})
