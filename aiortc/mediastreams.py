import asyncio
import math

from pyee import EventEmitter

from .utils import uint32_add

AUDIO_PTIME = 0.020  # 20ms audio packetization


class AudioFrame:
    """
    Audio frame, 16-bit PCM.
    """
    def __init__(self, channels, data, sample_rate, timestamp):
        self.channels = channels
        self.data = data
        self.sample_rate = sample_rate
        self.sample_width = 2
        self.timestamp = timestamp


class VideoFrame:
    """
    Video frame in YUV420 format.
    """
    def __init__(self, width, height, timestamp, data=None):
        self.height = height
        self.width = width
        self.timestamp = timestamp
        if data is None:
            self.data = b'\x00' * math.ceil(width * height * 12 / 8)
        else:
            self.data = data


class MediaStreamTrack(EventEmitter):
    pass


class AudioStreamTrack(MediaStreamTrack):
    """
    The base implementation just reads silence.

    Subclass it to provide a useful implementation.
    """
    kind = 'audio'

    async def recv(self):
        timestamp = getattr(self, '_timestamp', 0)
        self._timestamp = uint32_add(timestamp, 160)
        await asyncio.sleep(AUDIO_PTIME)
        return AudioFrame(channels=1, data=b'\x00\x00' * 160, sample_rate=8000, timestamp=timestamp)


class VideoStreamTrack(MediaStreamTrack):
    """
    The base implementation just reads a 640x480 green frame at 30fps.

    Subclass it to provide a useful implementation.
    """
    kind = 'video'

    async def recv(self):
        timestamp = getattr(self, '_timestamp', 0)
        self._timestamp = uint32_add(timestamp, 3000)
        await asyncio.sleep(1/30)
        return VideoFrame(width=640, height=480, timestamp=timestamp)
