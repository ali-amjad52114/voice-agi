import { useCallback, useEffect, useRef, useState } from "react"
import { api, apiMode } from "../api"
import { useSpeech } from "../hooks/useSpeech"
import type { Task } from "../types"
import { MicIcon, SendIcon } from "./Icons"
import { TaskRow } from "./TaskRow"

const EXAMPLE =
  "One mechanic quoted me $800 for front brakes on my 2019 Camry, I'm in Fremont. Find the part price online, call mechanics and dealers near me, get their all-in price and hourly labor rate, and tell me whether I should bring my own part or let them supply it."

export function Home({ onOpen }: { onOpen: (id: string) => void }) {
  const [text, setText] = useState("")
  const [tasks, setTasks] = useState<Task[]>([])
  const [submitting, setSubmitting] = useState(false)

  const refresh = useCallback(() => {
    api.listTasks().then(setTasks).catch(() => undefined)
  }, [])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 2500)
    return () => clearInterval(t)
  }, [refresh])

  const submit = useCallback(
    async (request: string) => {
      const r = request.trim()
      if (!r || submitting) return
      setSubmitting(true)
      try {
        const task = await api.createTask(r)
        setText("")
        onOpen(task.id)
      } finally {
        setSubmitting(false)
      }
    },
    [onOpen, submitting],
  )

  const { listening, interim, supported, toggle } = useSpeech(submit)

  useEffect(() => {
    if (listening) setText(interim)
  }, [interim, listening])

  // Grow the box to fit whatever is in it, whether typed, dictated, or
  // dropped in by the demo link, so the whole request is readable at once.
  const box = useRef<HTMLTextAreaElement>(null)
  useEffect(() => {
    const el = box.current
    if (!el) return
    el.style.height = "auto"
    el.style.height = `${el.scrollHeight}px`
  }, [text])

  return (
    <>
      <div className="topbar">
        <div className="brand">
          <span className="dot" />
          Voice AGI
        </div>
        <span className="mode">{apiMode === "live" ? "live backend" : "demo mode"}</span>
      </div>

      <section className="hero">
        <h1>What do you need done?</h1>
        <p>Say it once. I'll research, make the calls, and come back with a decision.</p>

        <div className="mic-wrap">
          <button
            className={`mic ${listening ? "listening" : ""}`}
            onClick={toggle}
            aria-label={listening ? "Stop listening" : "Start talking"}
            disabled={!supported}
          >
            <MicIcon />
          </button>
          <div className="mic-hint">
            {!supported ? "Mic needs Chrome. Type below instead." : listening ? "Listening… tap to finish" : "Tap to talk"}
          </div>
        </div>
      </section>

      <div className="composer">
        <textarea
          ref={box}
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Or type it. “Get me 10 quotes for movers on Oct 3.”"
          rows={1}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault()
              void submit(text)
            }
          }}
        />
        <button className="btn primary" aria-label="Send" disabled={!text.trim() || submitting} onClick={() => submit(text)}>
          <SendIcon />
        </button>
      </div>
      <p className="example">
        Try the demo: <button onClick={() => setText(EXAMPLE)}>brake quote in Fremont</button>
      </p>

      <div className="section-h">
        <span>Your tasks</span>
        <span>{tasks.filter((t) => t.status === "running").length} running</span>
      </div>
      <div className="task-list">
        {tasks.length === 0 && <div className="empty">Nothing yet. Say what you need above.</div>}
        {tasks.map((t) => (
          <TaskRow key={t.id} task={t} onOpen={onOpen} />
        ))}
      </div>
    </>
  )
}
