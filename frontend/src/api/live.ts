import type { Task, TaskEvent } from "../types"
import type { Api } from "./index"

const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "http://localhost:7860"
const WS_BASE = BASE.replace(/^http/, "ws")

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return (await res.json()) as T
}

export const liveApi: Api = {
  async createTask(request, location) {
    const res = await fetch(`${BASE}/tasks`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ request, location }),
    })
    return json<Task>(res)
  },
  async listTasks() {
    return json<Task[]>(await fetch(`${BASE}/tasks`))
  },
  async getTask(id) {
    return json<Task>(await fetch(`${BASE}/tasks/${id}`))
  },
  async book(taskId, agentId) {
    const res = await fetch(`${BASE}/tasks/${taskId}/book`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ agentId }),
    })
    return json<Task>(res)
  },
  subscribe(taskId, onEvent) {
    const ws = new WebSocket(`${WS_BASE}/tasks/${taskId}/events`)
    ws.onmessage = (m) => {
      try {
        onEvent(JSON.parse(m.data as string) as TaskEvent)
      } catch {
        /* ignore malformed frames */
      }
    }
    ws.onerror = () => onEvent({ type: "error", message: "Lost connection to the task stream" })
    return () => ws.close()
  },
}
