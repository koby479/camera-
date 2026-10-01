"""Plays PCM16 mono audio pushed from the camera worker threads (GUI thread only)."""
from __future__ import annotations

import sys

from PyQt6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices


class AudioPlayer:
    def __init__(self):
        self._sink: QAudioSink | None = None
        self._io = None
        self._rate = 0

    def _open(self, rate: int):
        self.stop()
        fmt = QAudioFormat()
        fmt.setSampleRate(rate)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        dev = QMediaDevices.defaultAudioOutput()
        if not dev.isFormatSupported(fmt):
            print(f"[audio] output device does not report support for {rate} Hz mono; trying anyway",
                  file=sys.stderr)
        self._sink = QAudioSink(dev, fmt)
        self._sink.setBufferSize(rate * 2)      # ~1 second
        self._io = self._sink.start()
        self._rate = rate

    def write(self, pcm: bytes, rate: int):
        if self._sink is None or rate != self._rate:
            self._open(rate)
        # drop instead of queueing when the buffer is full, so latency never grows
        if self._io is not None and self._sink.bytesFree() >= len(pcm):
            self._io.write(pcm)

    def stop(self):
        if self._sink is not None:
            self._sink.stop()
        self._sink = self._io = None
        self._rate = 0
