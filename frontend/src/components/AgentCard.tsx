import type { Agent } from "../types"
import { ChevronIcon, GlobeIcon, PhoneIcon } from "./Icons"

function fmtSlot(iso?: string) {
  if (!iso) return null
  const d = new Date(iso)
  return d.toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" })
}

function fmtDuration(s: number) {
  const m = Math.floor(s / 60)
  const r = s % 60
  return `${m}:${r.toString().padStart(2, "0")}`
}

export function AgentCard({
  agent,
  open,
  recommended,
  onToggle,
}: {
  agent: Agent
  open: boolean
  recommended: boolean
  onToggle: () => void
}) {
  const typeLabel = agent.business.type === "parts" ? "web" : agent.business.type
  const summary =
    agent.status === "queued"
      ? "Waiting"
      : agent.status === "active"
        ? agent.kind === "call"
          ? "On the phone"
          : "Reading"
        : agent.status === "failed"
          ? "Failed"
          : (agent.summary ?? "Done")
  const muted = agent.status !== "done" || agent.call?.outcome === "voicemail"
  const f = agent.facts

  return (
    <div className={`agent ${agent.status} ${open ? "open" : ""} ${recommended ? "recommended" : ""}`}>
      <button className="agent-row" onClick={onToggle} aria-expanded={open}>
        <span className="kind">{agent.kind === "call" ? <PhoneIcon /> : <GlobeIcon />}</span>
        <span className="main">
          <div>
            <span className="name">{agent.business.name}</span>
            <span className="type">{typeLabel}</span>
          </div>
          <div className={`summary ${muted ? "muted" : ""}`}>
            {agent.status === "active" ? (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
                <span className="waveform" aria-hidden>
                  <i /><i /><i /><i />
                </span>
                {summary}
              </span>
            ) : (
              summary
            )}
          </div>
        </span>
        <span className="chev">
          <ChevronIcon />
        </span>
      </button>

      {open && (
        <div className="agent-detail">
          {agent.status === "done" && (
            <div className="facts">
              {agent.call && <span className="fact">Call {fmtDuration(agent.call.durationS)}</span>}
              {agent.call?.answeredBy && <span className="fact">Answered by {agent.call.answeredBy}</span>}
              {agent.call?.outcome === "voicemail" && <span className="fact">Voicemail</span>}
              {f?.allInPrice && <span className="fact">All-in ${f.allInPrice}</span>}
              {f?.laborRatePerHour && <span className="fact">${f.laborRatePerHour}/h{f.laborHours ? ` × ${f.laborHours}h` : ""}</span>}
              {f?.partPrice && <span className="fact">Part ${f.partPrice}</span>}
              {f?.partsType && <span className="fact">{f.partsType.toUpperCase()}</span>}
              {f?.acceptsCustomerParts === false && <span className="fact">No customer parts</span>}
              {f?.acceptsCustomerParts === true && <span className="fact">Accepts your parts</span>}
              {f?.warrantyMonths && <span className="fact">{f.warrantyMonths}-mo warranty</span>}
              {f?.earliestSlot && <span className="fact">Earliest {fmtSlot(f.earliestSlot)}</span>}
              {agent.business.url && (
                <a className="fact" href={agent.business.url} target="_blank" rel="noreferrer">
                  {new URL(agent.business.url).hostname}
                </a>
              )}
            </div>
          )}
          {agent.transcript?.length ? (
            <div className="transcript">
              {agent.transcript.map((l, i) => (
                <div key={i} className={`line from-${l.role}`}>
                  <span className="who">{l.role === "agent" ? "Agent" : agent.call?.answeredBy ?? agent.business.name}</span>
                  <span>{l.text}</span>
                </div>
              ))}
            </div>
          ) : (
            <div className="transcript-empty">
              {agent.status === "done" ? "No transcript for web lookups." : "Transcript will appear when this agent finishes."}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
