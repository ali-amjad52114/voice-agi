import { useMemo, useState } from "react"
import { api } from "../api"
import { useTask } from "../hooks/useTask"
import { AgentCard } from "./AgentCard"
import { ArrowLeftIcon } from "./Icons"
import { ProgressCard } from "./ProgressCard"
import { ResultCard } from "./ResultCard"
import { StatusPill } from "./TaskRow"

export function TaskScreen({ id, onBack }: { id: string; onBack: () => void }) {
  const { task, error } = useTask(id)
  const [openId, setOpenId] = useState<string | null>(null)
  const [booking, setBooking] = useState(false)
  const [toast, setToast] = useState<string | null>(null)

  const agents = useMemo(() => {
    if (!task) return []
    const rank = { active: 0, done: 1, queued: 2, failed: 3 }
    return [...task.agents].sort((a, b) => {
      if (task.status === "complete") {
        const av = a.facts?.allInPrice ?? a.facts?.partPrice ?? Infinity
        const bv = b.facts?.allInPrice ?? b.facts?.partPrice ?? Infinity
        if (a.kind !== b.kind) return a.kind === "web" ? -1 : 1
        return av - bv
      }
      return rank[a.status] - rank[b.status]
    })
  }, [task])

  const book = async (agentId: string) => {
    if (!task) return
    setBooking(true)
    try {
      await api.book(task.id, agentId)
      const name = task.agents.find((a) => a.id === agentId)?.business.name ?? "the shop"
      setToast(`Booking agent is calling ${name}`)
      setTimeout(() => setToast(null), 3200)
    } finally {
      setBooking(false)
    }
  }

  return (
    <>
      <div className="topbar">
        <button className="back" onClick={onBack}>
          <ArrowLeftIcon /> Tasks
        </button>
        {task && <StatusPill status={task.status} />}
      </div>

      {error && <div className="error">{error}</div>}
      {!task && !error && <div className="empty">Loading…</div>}

      {task && (
        <>
          <div className="task-head">
            <div className="eyebrow">
              <span>{new Date(task.createdAt).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}</span>
              <span>·</span>
              <span>{task.agents.length} agents</span>
            </div>
            <h2>{task.title}</h2>
            <p className="request">“{task.request}”</p>
          </div>

          {task.status === "complete" && task.result ? (
            <ResultCard
              task={task}
              booking={booking}
              onBook={book}
              onSeeAll={() => document.getElementById("agents")?.scrollIntoView({ behavior: "smooth" })}
            />
          ) : (
            <ProgressCard task={task} />
          )}

          <div className="section-h" id="agents">
            <span>Agents</span>
            <span>{task.agents.filter((a) => a.status === "done").length} done</span>
          </div>
          <div className="agents">
            {agents.map((a) => (
              <AgentCard
                key={a.id}
                agent={a}
                open={openId === a.id}
                recommended={task.result?.recommendedAgentId === a.id}
                onToggle={() => setOpenId(openId === a.id ? null : a.id)}
              />
            ))}
          </div>
        </>
      )}

      {toast && <div className="toast">{toast}</div>}
    </>
  )
}
