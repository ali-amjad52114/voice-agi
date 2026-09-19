const TARGET_IN_RATE = 16000

export function parseSampleRate(sampleRate?: number, format?: string): number {
  if (sampleRate && sampleRate > 0) return sampleRate
  if (format) {
    const match = /(\d{4,5})/.exec(format)
    if (match) return Number(match[1])
  }
  return TARGET_IN_RATE
}

export function resamplePcm16Le(input: Uint8Array, fromRate: number, toRate: number): Uint8Array {
  if (fromRate === toRate || input.byteLength < 2) {
    return input
  }
  const inSamples = input.byteLength >> 1
  const outSamples = Math.max(1, Math.round((inSamples * toRate) / fromRate))
  const out = new Uint8Array(outSamples * 2)
  const inView = new DataView(input.buffer, input.byteOffset, input.byteLength)
  const outView = new DataView(out.buffer)
  for (let i = 0; i < outSamples; i++) {
    const src = (i * fromRate) / toRate
    const i0 = Math.floor(src)
    const i1 = Math.min(i0 + 1, inSamples - 1)
    const frac = src - i0
    const s0 = inView.getInt16(i0 * 2, true)
    const s1 = inView.getInt16(i1 * 2, true)
    outView.setInt16(i * 2, Math.round(s0 + (s1 - s0) * frac), true)
  }
  return out
}

export function toPipelinePcm(input: Uint8Array, fromRate: number): Uint8Array {
  return resamplePcm16Le(input, fromRate, TARGET_IN_RATE)
}
