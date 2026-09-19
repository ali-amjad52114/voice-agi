"""PCM serializer for Mentra Live over Pipecat's plain WebSocket transport.

MentraOS is only I/O: glasses mic PCM in, glasses speaker PCM out.
The cascade (Gradium STT → Gemma → Gradium TTS) stays in bot.py.
"""

from __future__ import annotations

import base64
import json

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameProcessorSetup
from pipecat.serializers.base_serializer import FrameSerializer


class MentraPcmSerializer(FrameSerializer):
    """Binary s16le PCM plus a small JSON control channel."""

    def __init__(self, sample_rate: int = 16000):
        super().__init__()
        self._in_rate = sample_rate

    async def setup(self, setup: FrameProcessorSetup):
        self._in_rate = setup.audio_in_sample_rate or self._in_rate

    async def serialize(self, frame: Frame) -> str | bytes | None:
        if isinstance(frame, OutputAudioRawFrame):
            return frame.audio
        if isinstance(frame, InterruptionFrame):
            return json.dumps({"type": "interrupt"})
        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if isinstance(data, (bytes, bytearray)):
            if not data:
                return None
            return InputAudioRawFrame(
                audio=bytes(data),
                sample_rate=self._in_rate,
                num_channels=1,
            )

        if not isinstance(data, str) or not data:
            return None

        try:
            msg = json.loads(data)
        except json.JSONDecodeError:
            logger.warning("Mentra WS: ignored non-JSON text frame")
            return None

        kind = msg.get("type")
        if kind == "hello":
            rate = int(msg.get("sampleRate") or self._in_rate)
            if rate > 0:
                self._in_rate = rate
                logger.info(f"Mentra glasses hello: {rate} Hz {msg.get('format', 'pcm')}")
            return None

        if kind == "audio" and msg.get("data"):
            raw = base64.b64decode(msg["data"])
            return InputAudioRawFrame(
                audio=raw,
                sample_rate=self._in_rate,
                num_channels=1,
            )

        return None
