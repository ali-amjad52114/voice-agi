import { useEffect, useState } from "react"
import { Home } from "./components/Home"
import { TaskScreen } from "./components/TaskScreen"

function readRoute(): string | null {
  const m = /^#\/task\/(.+)$/.exec(window.location.hash)
  return m ? m[1] : null
}

export default function App() {
  const [taskId, setTaskId] = useState<string | null>(readRoute)

  useEffect(() => {
    const onHash = () => setTaskId(readRoute())
    window.addEventListener("hashchange", onHash)
    return () => window.removeEventListener("hashchange", onHash)
  }, [])

  const open = (id: string) => {
    window.location.hash = `#/task/${id}`
  }
  const back = () => {
    window.location.hash = ""
  }

  return <main className="shell">{taskId ? <TaskScreen id={taskId} onBack={back} /> : <Home onOpen={open} />}</main>
}
