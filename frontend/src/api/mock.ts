import type { Agent, Task, TaskEvent } from "../types"
import { brakeTask } from "../fixtures/brakeTask"
import type { Api } from "./index"

type Listener = (e: TaskEvent) => void

const tasks = new Map<string, Task>()
const listeners = new Map<string, Set<Listener>>()

function clone<T>(v: T): T {
  return JSON.parse(JSON.stringify(v)) as T
}

function emit(taskId: string, e: TaskEvent) {
  listeners.get(taskId)?.forEach((l) => l(e))
}

function stripTranscripts(t: Task): Task {
  return { ...t, agents: t.agents.map(({ transcript, ...a }) => a) }
}

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms))

/** Replays the fixture as a live run: agents start in waves, finish at different times, then the result lands. */
async function simulate(task: Task) {
  const done = clone(brakeTask)
  const finalById = new Map(done.agents.map((a) => [a.id, a]))

  await wait(1400)
  task.status = "running"
  emit(task.id, { type: "task.updated", task: clone(task) })

  const order = [
    ["a_rockauto", "a_autozone", "a_sams", "a_toyota_fremont"],
    ["a_bay_brake", "a_toyota_outlet", "a_elite"],
    ["a_mission", "a_precision"],
  ]
  const durations: Record<string, number> = {
    a_rockauto: 1800,
    a_autozone: 2600,
    a_sams: 6200,
    a_toyota_fremont: 7400,
    a_bay_brake: 5200,
    a_toyota_outlet: 6100,
    a_elite: 2400,
    a_mission: 6800,
    a_precision: 4600,
  }

  const finish = async (id: string) => {
    await wait(durations[id] ?? 4000)
    const final = finalById.get(id)
    if (!final) return
    const idx = task.agents.findIndex((a) => a.id === id)
    task.agents[idx] = clone(final)
    emit(task.id, { type: "agent.updated", agent: clone(final) })
    emit(task.id, { type: "task.updated", task: clone(task) })
  }

  const running: Promise<void>[] = []
  for (let w = 0; w < order.length; w++) {
    if (w > 0) await wait(2200)
    for (const id of order[w]) {
      const a = task.agents.find((x) => x.id === id)
      if (!a) continue
      a.status = "active"
      emit(task.id, { type: "agent.updated", agent: clone(a) })
    }
    emit(task.id, { type: "task.updated", task: clone(task) })
    running.push(...order[w].map(finish))
  }
  await Promise.all(running)

  await wait(1600)
  task.status = "complete"
  task.result = clone(done.result)
  emit(task.id, { type: "task.result", taskId: task.id, result: clone(task.result!) })
  emit(task.id, { type: "task.updated", task: clone(task) })
}

function guessTitle(request: string): string {
  const r = request.toLowerCase()
  if (r.includes("brake")) return "Brake repair"
  if (r.includes("plumb")) return "Plumber"
  if (r.includes("dentist") || r.includes("cleaning")) return "Dentist"
  if (r.includes("mover") || r.includes("moving")) return "Movers"
  if (r.includes("photograph")) return "Wedding photographer"
  return "New task"
}

export const mockApi: Api = {
  async createTask(request) {
    await wait(350)
    const id = `task_${Math.random().toString(36).slice(2, 8)}`
    const base = clone(brakeTask)
    const task: Task = {
      id,
      title: guessTitle(request),
      request,
      createdAt: new Date().toISOString(),
      status: "planning",
      userQuote: extractQuote(request) ?? 800,
      agents: base.agents.map<Agent>((a) => ({
        id: a.id,
        taskId: id,
        kind: a.kind,
        status: "queued",
        business: a.business,
      })),
    }
    tasks.set(id, task)
    void simulate(task)
    return clone(task)
  },
  async listTasks() {
    return [...tasks.values()].map((t) => stripTranscripts(clone(t))).sort((a, b) => b.createdAt.localeCompare(a.createdAt))
  },
  async getTask(id) {
    const t = tasks.get(id)
    if (!t) throw new Error("Task not found")
    return clone(t)
  },
  async book(taskId) {
    await wait(600)
    const t = tasks.get(taskId)
    if (!t) throw new Error("Task not found")
    return clone(t)
  },
  subscribe(taskId, onEvent) {
    if (!listeners.has(taskId)) listeners.set(taskId, new Set())
    listeners.get(taskId)!.add(onEvent)
    return () => listeners.get(taskId)?.delete(onEvent)
  },
}

function extractQuote(text: string): number | undefined {
  const m = /\$\s?(\d{2,5})/.exec(text)
  return m ? Number(m[1]) : undefined
}

/** Seed a finished example so the task list isn't empty on first load. */
const seeded = clone(brakeTask)
seeded.id = "task_seed_brakes"
seeded.createdAt = new Date(Date.now() - 1000 * 60 * 47).toISOString()
tasks.set(seeded.id, seeded)
