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
  const shopQuotes = task.agents.filter((a) => a.kind === "call" && a.facts?.allInPrice).length
  const partPrices = task.agents.filter((a) => a.kind === "web" && a.facts?.partPrice).length
  const quotes = shopQuotes + partPrices

  if (r.options.length === 0) {
    return (
      <div className="card result">
        <div style={{ fontWeight: 600, marginBottom: 6 }}>No decision yet</div>
        <p className="why" style={{ marginBottom: 0 }}>
          {r.why || "No shop gave a usable quote, so there is nothing to compare."}
          {partPrices > 0 && ` ${partPrices} online part price${partPrices > 1 ? "s were" : " was"} found, but a labor rate from a shop is needed to build an option.`}
        </p>
      </div>
    )
  }

  return (
    <div className="card result">
      <div style={{ fontSize: 13, color: "var(--ink-2)" }}>
        {task.userQuote ? `Your quote was $${task.userQuote}. ` : ""}
        {shopQuotes} shop quote{shopQuotes === 1 ? "" : "s"}, {partPrices} online part price{partPrices === 1 ? "" : "s"}.
        {r.options.length > 1 ? " Two ways to do this." : " One way to do this."}
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

      {r.tradeoffs && r.tradeoffs.length > 0 && (
        <ul className="tradeoffs">
          {r.tradeoffs.map((t, i) => (
            <li key={i}>{t}</li>
          ))}
        </ul>
      )}

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
