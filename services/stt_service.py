# START OF FILE services/stt_service.py

"""
STT для голосового оверлея (Фаза 3): потоковое распознавание речи через
Google Cloud Speech-to-Text v2, модель chirp_3 (ru-RU GA в потоке).

Даёт async-генератор транскрипта по async-потоку аудио-чанков (PCM LINEAR16 16kHz mono).
Одна streaming-сессия живёт ≤5 мин (лимит API) — «endless streaming» реализуется на стороне
вызывающего (WS-обработчик переоткрывает сессию каждые config.STT_STREAM_REOPEN_SECONDS).

Требования окружения:
  * pip: google-cloud-speech (в requirements);
  * включённый Speech-to-Text API на проекте;
  * роль roles/speech.client у сервис-аккаунта (config.SPEECH_TO_TEXT_CREDENTIALS_FILE).

ВНИМАНИЕ: модуль импортирует google-cloud-speech ЛЕНИВО (внутри функций), поэтому его
можно импортировать даже там, где либа ещё не установлена. Реальная валидация — с живым аудио.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator, Dict, List, Optional, Any

import config

logger = logging.getLogger(__name__)


def _speech_endpoint(region: str) -> str:
    """Региональный эндпоинт Speech v2 (chirp_3 GA в eu/us)."""
    return f"{region}-speech.googleapis.com"


def _active_stt_project_id() -> str:
    """Project для recognizer берём ИЗ активного SA-файла (он же VERTEX-файл, перезаписывается
    при горячей смене сервис-аккаунта), а НЕ из статичного config.GCP_PROJECT_ID. Иначе после
    свопа на новый проект STT продолжает бить recognizer в старый проект (который мог быть
    заблокирован/удалён) → 403 CONSUMER_SUSPENDED. Фолбэк — config.GCP_PROJECT_ID."""
    try:
        from services.sa_admin import project_id_for
        return project_id_for(config.SPEECH_TO_TEXT_CREDENTIALS_FILE)
    except Exception:
        return config.GCP_PROJECT_ID


def _build_client():
    """Создаёт SpeechAsyncClient на сервис-аккаунте + региональном эндпоинте."""
    from google.cloud.speech_v2 import SpeechAsyncClient
    from google.api_core.client_options import ClientOptions
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(
        config.SPEECH_TO_TEXT_CREDENTIALS_FILE,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    opts = ClientOptions(api_endpoint=_speech_endpoint(config.STT_REGION))
    return SpeechAsyncClient(credentials=creds, client_options=opts)


def _build_streaming_config(language: str, phrases: Optional[List[str]]):
    """StreamingRecognitionConfig под chirp_3: ru-RU, LINEAR16 16kHz, interim, пунктуация,
    SUPERSHORT endpointing, опциональный inline PhraseSet (доменный словарь)."""
    from google.cloud.speech_v2.types import cloud_speech

    features = cloud_speech.RecognitionFeatures(
        enable_automatic_punctuation=True,
    )

    rec_config_kwargs: Dict[str, Any] = dict(
        explicit_decoding_config=cloud_speech.ExplicitDecodingConfig(
            encoding=cloud_speech.ExplicitDecodingConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=config.STT_SAMPLE_RATE,
            audio_channel_count=1,
        ),
        language_codes=[language],
        model=config.STT_MODEL,
        features=features,
    )

    # Доменный словарь (термины, номера статей, сленг) — inline PhraseSet с boost.
    if phrases:
        rec_config_kwargs["adaptation"] = cloud_speech.SpeechAdaptation(
            phrase_sets=[
                cloud_speech.SpeechAdaptation.AdaptationPhraseSet(
                    inline_phrase_set=cloud_speech.PhraseSet(
                        phrases=[
                            cloud_speech.PhraseSet.Phrase(value=p, boost=12.0)
                            for p in phrases[:1000]
                        ]
                    )
                )
            ]
        )

    # Эндпоинтинг по голосовой активности (VAD). Без него chirp_3 финализирует только по
    # «естественной» тишине в конце — поэтому ДЛИННЫЙ НЕПРЕРЫВНЫЙ вопрос (без пауз) не получал
    # is_final, и клиент «не слышал» вопрос. С VAD сервер сам финализирует через speech_end_timeout
    # после того, как речь смолкла.
    # ⚠️ КРИТИЧНО: таймауты ДОЛЖНЫ быть НЕнулевыми — пустой VoiceActivityTimeout() сервер трактует
    # как мгновенный таймаут и рвёт поток (499 CANCELLED до результатов, проверено живьём). Поэтому
    # задаём реальный speech_end_timeout (1.2 c — баланс: финализирует после паузы, не режет речь).
    from google.protobuf import duration_pb2
    streaming_features = cloud_speech.StreamingRecognitionFeatures(
        interim_results=True,
        enable_voice_activity_events=True,
        voice_activity_timeout=cloud_speech.StreamingRecognitionFeatures.VoiceActivityTimeout(
            speech_end_timeout=duration_pb2.Duration(seconds=1, nanos=200_000_000),  # 1.2 c
        ),
    )

    return cloud_speech.StreamingRecognitionConfig(
        config=cloud_speech.RecognitionConfig(**rec_config_kwargs),
        streaming_features=streaming_features,
    )


async def stream_transcribe(
    audio_chunks: AsyncIterator[bytes],
    *,
    language: Optional[str] = None,
    phrases: Optional[List[str]] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """Распознаёт ОДИН streaming-сегмент (≤5 мин).

    Принимает async-итератор аудио-чанков (PCM LINEAR16 16kHz mono),
    отдаёт события: {"type": "partial"|"final", "text": str}.
    Для endless streaming вызывающий переоткрывает сессию каждые ~4 минуты.
    """
    from google.cloud.speech_v2.types import cloud_speech

    language = language or config.STT_LANGUAGE
    if not phrases:
        # Доменный словарь по умолчанию (термины/кодексы/сленг) — повышает точность на RP-юр-домене.
        from services.stt_phrases import default_phrases
        phrases = default_phrases()
    client = _build_client()
    recognizer = f"projects/{_active_stt_project_id()}/locations/{config.STT_REGION}/recognizers/_"
    streaming_config = _build_streaming_config(language, phrases)

    async def request_gen():
        # Первый запрос — только конфиг.
        yield cloud_speech.StreamingRecognizeRequest(
            recognizer=recognizer, streaming_config=streaming_config
        )
        async for chunk in audio_chunks:
            if chunk:
                yield cloud_speech.StreamingRecognizeRequest(audio=chunk)

    try:
        responses = await client.streaming_recognize(requests=request_gen())
        async for response in responses:
            for result in response.results:
                if not result.alternatives:
                    continue
                text = result.alternatives[0].transcript or ""
                if not text:
                    continue
                yield {"type": "final" if result.is_final else "partial", "text": text}
    except Exception:
        logger.exception("[STT] streaming_recognize error")
        raise


# END OF FILE services/stt_service.py
