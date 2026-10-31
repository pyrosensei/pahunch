import struct

from app.gnani import GnaniResponseError


def decode_mulaw(data: bytes) -> bytes:
    samples = []
    for byte in data:
        value = ~byte & 255
        magnitude = (((value & 15) << 3) + 132) << ((value & 112) >> 4)
        samples.append(132 - magnitude if value & 128 else magnitude - 132)
    return struct.pack(f"<{len(samples)}h", *samples)


def encode_mulaw(pcm: bytes) -> bytes:
    if len(pcm) % 2:
        raise ValueError("PCM16 requires complete samples")
    result = bytearray()
    for (sample,) in struct.iter_unpack("<h", pcm):
        sign = 128 if sample < 0 else 0
        magnitude = min(abs(sample), 32635) + 132
        exponent = max(0, magnitude.bit_length() - 8)
        mantissa = (magnitude >> (exponent + 3)) & 15
        result.append(~(sign | (exponent << 4) | mantissa) & 255)
    return bytes(result)


class TelephoneSpeech:
    def __init__(self, speech):
        self.speech = speech
        self._acks = {}

    async def transcribe(self, audio, **kwargs):
        return await self.speech.transcribe(audio, **kwargs)

    async def stream_pcm(self, text, *, sample_rate=8000, language=None):
        if sample_rate != 8000:
            raise ValueError("Telephone audio requires 8000 Hz")
        async for chunk in self.speech.stream_pcm(
            text, sample_rate=8000, language=language, container="mulaw"
        ):
            yield decode_mulaw(chunk)

    async def synthesize_pcm_blocking(self, text, *, sample_rate=8000, language=None):
        if sample_rate != 8000:
            raise ValueError("Telephone audio requires 8000 Hz")
        data = await self.speech.synthesize_pcm_blocking(
            text, sample_rate=8000, language=language, container="mulaw"
        )
        if not data:
            raise GnaniResponseError("Telephone synthesis returned no audio")
        return decode_mulaw(data)

    async def synthesize_pcm(self, text, *, sample_rate=8000, language=None):
        key = (text, language)
        if key not in self._acks:
            data = b"".join(
                [
                    chunk
                    async for chunk in self.stream_pcm(
                        text, sample_rate=sample_rate, language=language
                    )
                ]
            )
            if not data:
                raise GnaniResponseError("Telephone synthesis returned no audio")
            self._acks[key] = data
        return self._acks[key]
