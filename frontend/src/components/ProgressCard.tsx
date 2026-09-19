import type { Task } from "../types"
import { useCountUp } from "../hooks/useCountUp"

function bestSoFar(task: Task): number | null {
  const parts = task.agents.filter((a) => a.status === "done" && a.facts?.partPrice).map((a) => a.facts!.partPrice!)
  const labor = task.agents
    .filter((a) => a.status === "done" && a.facts?.laborRatePerHour && a.facts.acceptsCustomerParts)
    .map((a) => a.facts!.laborRatePerHour! * (a.facts!.laborHours ?? 2.5))
  const allIn = task.agents.filter((a) => a.status === "done" && a.facts?.allInPrice).map((a) => a.facts!.allInPrice!)
  const candidates: number[] = [...allIn]
  if (parts.length && labor.length) candidates.push(Math.min(...parts) + Math.min(...labor))
  return candidates.length ? Math.round(Math.min(...candidates)) : null
}

export function ProgressCard({ task }: { task: Task }) {
  const total = task.agents.length
  const done = task.agents.filter((a) => a.status === "done").length
  const active = task.agents.filter((a) => a.status === "active").length
  const queued = task.agents.filter((a) => a.status === "queued").length
  const calling = task.agents.filter((a) => a.status === "active" && a.kind === "call").length
  const best = bestSoFar(task)
  const bestAnim = useCountUp(best ?? 0)

  const headline =
    task.status === "planning"
      ? "Planning the work"
      : active > 0
        ? `${calling > 0 ? `On ${calling} call${calling > 1 ? "s" : ""}` : "Reading the web"}, ${queued} waiting`
        : "Wrapping up"

  return (
    <div className="card">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <div style={{ fontWeight: 600 }}>{headline}</div>
        <div style={{ fontSize: 13, color: "var(--ink-3)" }}>
          {done} of {total} done
        </div>
      </div>
      <div className="progress-line" aria-hidden>
        {task.agents.map((a) => (
          <span key={a.id} className={a.status} />
        ))}
      </div>
      <div className="stats">
        <div className="stat">
          <div className="label">Best so far</div>
          <div className={`value ${best ? "accent" : ""}`}>{best ? `$${bestAnim}` : "—"}</div>
        </div>
        {task.userQuote && (
          <div className="stat">
            <div className="label">Your quote</div>
            <div className="value">${task.userQuote}</div>
          </div>
        )}
        {best && task.userQuote && best < task.userQuote && (
          <div className="stat">
            <div className="label">Potential saving</div>
            <div className="value">${task.userQuote - best}</div>
          </div>
        )}
      </div>
    </div>
  )
}
