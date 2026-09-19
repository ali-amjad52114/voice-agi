import { useEffect, useState } from "react"
import { api } from "../api"
import type { Task } from "../types"

export function useTask(id: string | null) {
  const [task, setTask] = useState<Task | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!id) return
    let cancelled = false
    setTask(null)
    setError(null)

    api
      .getTask(id)
      .then((t) => {
        if (!cancelled) setTask(t)
      })
      .catch((e: Error) => {
        if (!cancelled) setError(e.message)
      })

    const unsub = api.subscribe(id, (e) => {
      if (cancelled) return
      if (e.type === "task.updated") setTask(e.task)
      else if (e.type === "agent.updated")
        setTask((prev) =>
          prev ? { ...prev, agents: prev.agents.map((a) => (a.id === e.agent.id ? e.agent : a)) } : prev,
        )
      else if (e.type === "task.result")
        setTask((prev) => (prev ? { ...prev, status: "complete", result: e.result } : prev))
      else if (e.type === "error") setError(e.message)
    })

    return () => {
      cancelled = true
      unsub()
    }
  }, [id])

  return { task, error }
}
