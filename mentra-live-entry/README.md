# Voice AGI Live (Mentra I/O)

MentraOS is **only** the glasses microphone and speaker. Speech still goes through
the existing Pipecat cascade on your laptop:

`Live mic → Gradium STT → Gemma (General Compute) → Gradium TTS → Live speaker`

## Run

1. Start the bot (same as the browser playground):

   ```bash
   cd general-compute-hackathon/server
   uv run bot.py --host 0.0.0.0
   ```

   Leave it up. Playground is still http://localhost:7860. Glasses use
   `ws://<laptop-lan-ip>:7860/ws-client`.

2. In another terminal:

   ```bash
   cd mentra-live-entry
   bun install
   bun dev
   ```

   The build prints the WebSocket URL it baked from your LAN IP. Override with
   `MENTRA_PUBLIC_PIPECAT_WS_URL` if that IP is wrong.

3. Mentra App → **Settings → Miniapp Developer Settings → Scan Miniapp QR Code**.
   Phone and laptop on the same Wi-Fi, or `bun dev --usb` on Android.

4. Enable the miniapp. It connects in the background. Talk; answers play in the
   glasses speaker. The phone tile shows connection status. Glasses button
   toggles the socket.

## If it does not connect

- `127.0.0.1` on the phone is the phone, not the laptop. Use the LAN IP.
- Windows firewall must allow inbound TCP 7860.
- Mentra App 2.13+ is required for live PCM playback.
