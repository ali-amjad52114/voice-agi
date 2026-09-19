import { useCallback, useEffect, useRef, useState } from "react"

/* Browser speech recognition as the demo mic. Swap for the Pipecat /ws-client audio stream when the backend is up. */

type RecognitionCtor = new () => SpeechRecognitionLike
interface SpeechRecognitionLike {
  lang: string
  continuous: boolean
  interimResults: boolean
  onresult: ((e: { resultIndex: number; results: ArrayLike<ArrayLike<{ transcript: string }> & { isFinal: boolean }> }) => void) | null
  onend: (() => void) | null
  onerror: ((e: { error: string }) => void) | null
  start(): void
  stop(): void
}

function getCtor(): RecognitionCtor | null {
  const w = window as unknown as { SpeechRecognition?: RecognitionCtor; webkitSpeechRecognition?: RecognitionCtor }
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null
}

export function useSpeech(onFinal: (text: string) => void) {
  const [listening, setListening] = useState(false)
  const [interim, setInterim] = useState("")
  const [supported, setSupported] = useState(true)
  const recRef = useRef<SpeechRecognitionLike | null>(null)
  const finalRef = useRef("")

  useEffect(() => {
    setSupported(getCtor() !== null)
  }, [])

  const stop = useCallback(() => {
    recRef.current?.stop()
  }, [])

  const start = useCallback(() => {
    const Ctor = getCtor()
    if (!Ctor) {
      setSupported(false)
      return
    }
    const rec = new Ctor()
    rec.lang = "en-US"
    rec.continuous = true
    rec.interimResults = true
    finalRef.current = ""
    setInterim("")
    rec.onresult = (e) => {
      let interimText = ""
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i]
        const text = r[0].transcript
        if (r.isFinal) finalRef.current += text + " "
        else interimText += text
      }
      setInterim((finalRef.current + interimText).trim())
    }
    rec.onerror = () => {
      setListening(false)
    }
    rec.onend = () => {
      setListening(false)
      const text = finalRef.current.trim()
      setInterim("")
      if (text) onFinal(text)
    }
    recRef.current = rec
    rec.start()
    setListening(true)
  }, [onFinal])

  const toggle = useCallback(() => {
    if (listening) stop()
    else start()
  }, [listening, start, stop])

  return { listening, interim, supported, toggle }
}
