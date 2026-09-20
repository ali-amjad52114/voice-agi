import type { ReactNode } from "react"
import { ChevronIcon, GlobeIcon, PhoneIcon } from "./Icons"

/** A labelled group of agent cards. `collapsible` groups start closed and open on tap. */
export function AgentGroup({
  kind,
  title,
  subtitle,
  count,
  open,
  onToggle,
  children,
}: {
  kind: "web" | "call"
  title: string
  subtitle: string
  count: number
  open: boolean
  onToggle?: () => void
  children: ReactNode
}) {
  const collapsible = typeof onToggle === "function"
  return (
    <section className={`agent-group ${open ? "open" : ""}`}>
      <button
        className="agent-group-h"
        onClick={onToggle}
        aria-expanded={collapsible ? open : undefined}
        disabled={!collapsible}
      >
        <span className="kind">{kind === "web" ? <GlobeIcon /> : <PhoneIcon />}</span>
        <span className="main">
          <div>
            <span className="name">{title}</span>
            <span className="type">{count}</span>
          </div>
          <div className="summary">{subtitle}</div>
        </span>
        {collapsible && (
          <span className="chev">
            <ChevronIcon />
          </span>
        )}
      </button>
      {open && <div className="agent-group-body">{children}</div>}
    </section>
  )
}
