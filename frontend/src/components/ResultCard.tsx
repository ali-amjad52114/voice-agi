import type { Task } from "../types"

export function ResultCard({
  task,
  onBook,
  onSeeAll,
  booking,
}: {
  task: Task
  onBook: (agentId: string) => void
  onSeeAll: () => void
  booking: boolean
}) {
  const r = task.result!
  const rec = task.agents.find((a) => a.id === r.recommendedAgentId)
  const quotes = task.agents.filter((a) => a.facts?.allInPrice || a.facts?.partPrice).length

  return (
    <div className="card result">
      <div style={{ fontSize: 13, color: "var(--ink-2)" }}>
        {task.userQuote ? `Your quote was $${task.userQuote}. ` : ""}
        {quotes} quotes in. Two ways to do this.
      </div>

      <div className="options">
        {r.options.map((o, i) => (
          <div key={o.label} className={`option ${i === r.recommendedOptionIndex ? "recommended" : ""}`}>
            {i === r.recommendedOptionIndex && <span className="tag">Recommended</span>}
            <div className="label">{o.label}</div>
            <div className="total">${o.total}</div>
            <div className="breakdown">{o.breakdown}</div>
          </div>
        ))}
      </div>

      <p className="why">{r.why}</p>

      <div className="actions">
        <button className="btn accent" disabled={booking} onClick={() => onBook(r.recommendedAgentId)}>
          {booking ? "Booking…" : `Book ${rec?.business.name ?? "it"}`}
        </button>
        <button className="btn" onClick={onSeeAll}>
          See all {quotes} quotes
        </button>
      </div>
    </div>
  )
}
