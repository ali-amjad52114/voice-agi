import {networkInterfaces} from "os"
import {rm} from "fs/promises"
import {backgroundRuntimeGuardPlugin, reactSingletonPlugin} from "@mentra/miniapp-cli/build-helpers"

function getLanIp(): string | null {
  const interfaces = networkInterfaces()
  for (const addrs of Object.values(interfaces)) {
    for (const iface of addrs ?? []) {
      if (iface.family === "IPv4" && !iface.internal) return iface.address
    }
  }
  return null
}

const distDir = "./dist"

await rm(distDir, {recursive: true, force: true})

const define: Record<string, string> = {}
for (const [k, v] of Object.entries(process.env)) {
  if (k.startsWith("MENTRA_PUBLIC_") && typeof v === "string") {
    define[`process.env.${k}`] = JSON.stringify(v)
  }
}

const lanIp = getLanIp()
if (!define["process.env.MENTRA_PUBLIC_PIPECAT_WS_URL"]) {
  const baked = process.env.MENTRA_PUBLIC_PIPECAT_WS_URL
    || (lanIp ? `ws://${lanIp}:7860/ws-client` : "ws://127.0.0.1:7860/ws-client")
  define["process.env.MENTRA_PUBLIC_PIPECAT_WS_URL"] = JSON.stringify(baked)
  console.log(`[build] Pipecat WS → ${baked}`)
}

const backgroundResult = await Bun.build({
  entrypoints: ["./src/background/index.ts"],
  outdir: `${distDir}/background`,
  target: "browser",
  format: "iife",
  plugins: [backgroundRuntimeGuardPlugin(import.meta.url)],
  minify: false,
  define,
})

if (!backgroundResult.success) {
  console.error("Background build failed:")
  for (const log of backgroundResult.logs) console.error(log)
  process.exit(1)
}

const uiResult = await Bun.build({
  entrypoints: ["./src/ui/index.html"],
  outdir: `${distDir}/ui`,
  target: "browser",
  plugins: [reactSingletonPlugin(import.meta.url)],
  minify: true,
  define,
})

if (!uiResult.success) {
  console.error("UI build failed:")
  for (const log of uiResult.logs) console.error(log)
  process.exit(1)
}
