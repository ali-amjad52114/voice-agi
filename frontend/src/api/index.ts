import type { Task, TaskEvent } from "../types"
import { mockApi } from "./mock"
import { liveApi } from "./live"

export interface Api {
  createTask(request: string, location?: string): Promise<Task>
  listTasks(): Promise<Task[]>
  getTask(id: string): Promise<Task>
  book(taskId: string, agentId: string): Promise<Task>
  subscribe(taskId: string, onEvent: (e: TaskEvent) => void): () => void
}

const mode = (import.meta.env.VITE_API_MODE as string | undefined) ?? "mock"

export const api: Api = mode === "live" ? liveApi : mockApi
export const apiMode = mode
