import type { Task } from "../types"
import { CarIcon, SparkIcon } from "./Icons"

export function StatusPill({ status }: { status: Task["status"] }) {
  const label = status === "running" ? "Running" : status === "complete" ? "Complete" : status === "planning" ? "Planning" : "Failed"
  const live = status === "running" || status === "planning"
  return (
    <span className={`pill ${status}`}>
      {live && <span className="blink" />}
      {label}
    </span>
  )
}

export function TaskRow({ task, onOpen }: { task: Task; onOpen: (id: string) => void }) {
  const done = task.agents.filter((a) => a.status === "done" || a.status === "failed").length
  const calls = task.agents.filter((a) => a.kind === "call").length
  const isCar = /brake|car|tire|oil/i.test(task.title)
  return (
    <button className="task-row" onClick={() => onOpen(task.id)}>
      <span className="icon">{isCar ? <CarIcon /> : <SparkIcon />}</span>
      <span className="meta">
        <div className="title">{task.title}</div>
        <div className="sub">
          {task.agents.length} agents · {calls} calls
          {task.status !== "complete" && ` · ${done} done`}
          {task.result?.savingsVsQuote ? ` · saved $${task.result.savingsVsQuote}` : ""}
        </div>
      </span>
      <StatusPill status={task.status} />
    </button>
  )
}
