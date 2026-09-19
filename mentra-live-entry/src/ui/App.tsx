import {useEffect, useState} from "react"

import type {PipelineStatus} from "../shared/channels"

export function App() {
  const [url, setUrl] = useState("")
  const [state, setState] = useState<PipelineStatus>("idle")
  const [error, setError] = useState<string | null>(null)
  const [micFrames, setMicFrames] = useState(0)
  const [lastFormat, setLastFormat] = useState("none")

  useEffect(() => {
    return mentra.on("status", (status) => {
      setUrl(status.url)
      setState(status.state)
      setError(status.error)
      setMicFrames(status.micFrames)
      setLastFormat(status.lastFormat)
    })
  }, [])

  const saveAndConnect = () => {
    mentra.send("connect", {url})
  }

  return (
    <div className="app">
      <h1>Voice AGI Live</h1>
      <p className={`state state-${state}`}>{state}</p>
      <p className="hint">Mentra is only mic + speaker. The laptop runs Gradium + Gemma.</p>
      <label>
        Pipecat WebSocket
        <input
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          placeholder="ws://192.168.1.10:7860/ws-client"
        />
      </label>
      <div className="row">
        <button type="button" onClick={saveAndConnect}>
          Connect
        </button>
        <button type="button" className="ghost" onClick={() => mentra.send("disconnect", {})}>
          Disconnect
        </button>
      </div>
      <p className="meta">
        mic frames {micFrames}
        <br />
        {lastFormat}
      </p>
      {error ? <p className="error">{error}</p> : null}
    </div>
  )
}
