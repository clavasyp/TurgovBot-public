# START OF FILE voice_processor.py

"""
Модуль для асинхронной обработки голосовых сообщений
с использованием Google Cloud Speech-to-Text API.
"""
import logging
import os
import asyncio
from google.cloud import speech_v1 as speech
from google.oauth2 import service_account

# Локальные импорты для получения терминов и централизованного пути
from law_sources_data import LAW_SOURCES_DATA
import config  # Импортируем наш центральный конфиг


def _get_adaptation_phrases() -> list[str]:
    """
    Собирает список специфических фраз для улучшения качества распознавания.
    Это одна из самых важных функций для повышения точности.
    """
    phrases = {
        # Общие термины RP
        "Majestic RP", "Маджестик РП", "ДМ", "ДБ", "СК", "ТК", "РК", "ПГ",
        "OOC", "IC", "ФИБ", "ЛСПД", "ЕМС", "САНГ", "УССС", "Визл Ньюс",
        "ордер", "прокуратура", "адвокат", "губернатор", "капт", "трасса",
        "Форт-Занкудо", "тюрьма Болингброук", "RolePlay", "нон-рп", "non-rp",
        "госка", "крайм",

        # Названия серверов
        "New York", "Detroit", "Chicago", "San Francisco", "Atlanta", "San Diego",
        "Los Angeles", "Miami", "Las Vegas", "Washington", "Dallas", "Boston",
        "Houston", "Seattle", "Phoenix",
    }

    # Динамически добавляем названия всех законов и кодексов из конфига
    for server_id, server_data in LAW_SOURCES_DATA.items():
        for law in server_data.get('law_sources', []):
            try:
                # Извлекаем название из URL. Это грубый, но работающий метод для данных ссылок
                title = law['url'].split('/')[-2].replace('-', ' ').title()
                phrases.add(title)
            except IndexError:
                continue

    # Добавляем основные типы кодексов, которые часто встречаются
    common_codes = {
        "Конституция", "Уголовный кодекс", "Процессуальный кодекс",
        "Административный кодекс", "Дорожный кодекс", "Трудовой кодекс",
        "Этичсекий кодекс", "Судебный кодекс", "Закон о статусе"
    }
    phrases.update(common_codes)

    logging.info(f"Сгенерировано {len(phrases)} фраз для адаптации речи.")
    return list(phrases)


async def transcribe_voice(audio_bytes: bytes) -> str | None:
    """
    Отправляет аудио в Google Cloud Speech-to-Text API и возвращает распознанный текст.

    Args:
        audio_bytes: Аудио-контент в виде байтов.

    Returns:
        Строка с распознанным текстом или None в случае ошибки.
    """
    start_time = asyncio.get_running_loop().time()
    logging.info("[VOICE] Начало процесса распознавания речи.")
    try:
        credentials_path = config.SPEECH_TO_TEXT_CREDENTIALS_FILE

        if not os.path.exists(credentials_path):
            logging.critical(f"[VOICE] Файл с учетными данными для Speech-to-Text не найден по пути: {credentials_path}")
            return None

        credentials = service_account.Credentials.from_service_account_file(credentials_path)
        client = speech.SpeechAsyncClient(credentials=credentials)

        # Собираем фразы для адаптации
        adaptation_phrases = _get_adaptation_phrases()
        
        phrase_set = speech.PhraseSet(
            phrases=[
                speech.PhraseSet.Phrase(value=p, boost=10) for p in adaptation_phrases
            ]
        )

        adaptation = speech.SpeechAdaptation(
            phrase_sets=[phrase_set]
        )

        recognition_config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.OGG_OPUS,
            sample_rate_hertz=48000,
            language_code="ru-RU",
            model="latest_long",
            adaptation=adaptation,
            enable_automatic_punctuation=True,
        )

        audio = speech.RecognitionAudio(content=audio_bytes)

        logging.info(f"[VOICE] Отправка запроса на распознавание в Google Cloud Speech-to-Text... Конфигурация: model='{recognition_config.model}', lang='{recognition_config.language_code}'.")
        response = await client.recognize(config=recognition_config, audio=audio)
        
        duration = (asyncio.get_running_loop().time() - start_time) * 1000
        logging.info(f"[VOICE] Ответ от Speech-to-Text API получен за {duration:.2f} мс.")

        if response and response.results:
            transcript = response.results[0].alternatives[0].transcript
            confidence = response.results[0].alternatives[0].confidence
            logging.info(f"[VOICE] Речь успешно распознана. Уверенность: {confidence:.2%}. Результат: '{transcript}'")
            return transcript
        else:
            logging.warning("[VOICE] Распознавание речи не дало результатов (пустой ответ).")
            return None

    except Exception as e:
        logging.critical("[VOICE] КРИТИЧЕСКАЯ ОШИБКА при обращении к Google Cloud Speech-to-Text API.", exc_info=e)
        return None

# END OF FILE voice_processor.py