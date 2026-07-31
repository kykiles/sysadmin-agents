"""Голос → текст локально (faster-whisper), без внешних API."""
import asyncio
from functools import lru_cache
from io import BytesIO

from app.config import settings


@lru_cache(maxsize=1)
def _model():
    from faster_whisper import WhisperModel

    # ponytail: int8 на CPU — потолок 2 vCPU сервера; при апгрейде железа
    # поднять WHISPER_MODEL до medium/large-v3 и compute_type до int8_float16
    return WhisperModel(
        settings.whisper_model,
        device="cpu",
        compute_type="int8",
        download_root=settings.whisper_cache_dir,
    )


def _transcribe(audio: BytesIO) -> str:
    segments, _ = _model().transcribe(
        audio, language=settings.whisper_language, vad_filter=True
    )
    return " ".join(s.text.strip() for s in segments).strip()


async def transcribe_voice(message) -> str:
    """Скачивает голосовое из Telegram и возвращает распознанный текст."""
    buf = BytesIO()
    await message.bot.download(message.voice or message.audio, destination=buf)
    buf.seek(0)
    return await asyncio.to_thread(_transcribe, buf)
