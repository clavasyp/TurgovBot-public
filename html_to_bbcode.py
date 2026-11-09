# html_to_bbcode.py
"""
Утилита для конвертации HTML-разметки Telegram в BB-код для форумов.

Поддерживаемые теги:
- <b> → [B]
- <i> → [I]
- <u> → [U]
- <s> → [S]
- <pre> → [CODE]
- <a href=""> → [URL]

Также включает функцию санитизации HTML для удаления неподдерживаемых Telegram тегов.
"""

import re
from html import unescape
from html.parser import HTMLParser


class HTMLTagFixer(HTMLParser):
    """Класс для автоматического исправления незакрытых HTML-тегов."""

    def __init__(self):
        super().__init__()
        self.result = []
        self.tag_stack = []

    def handle_starttag(self, tag, attrs):
        # Только для парных тегов
        if tag in ['b', 'i', 'u', 's', 'code', 'pre', 'a']:
            self.tag_stack.append(tag)
            attrs_str = ''.join([f' {k}="{v}"' for k, v in attrs])
            self.result.append(f'<{tag}{attrs_str}>')

    def handle_endtag(self, tag):
        if tag in ['b', 'i', 'u', 's', 'code', 'pre', 'a']:
            # Если тег есть в стеке - закрываем его
            if tag in self.tag_stack:
                # Закрываем все теги до нужного (вложенность)
                while self.tag_stack and self.tag_stack[-1] != tag:
                    closed_tag = self.tag_stack.pop()
                    self.result.append(f'</{closed_tag}>')

                if self.tag_stack and self.tag_stack[-1] == tag:
                    self.tag_stack.pop()
                    self.result.append(f'</{tag}>')
            else:
                # Если тега нет в стеке - просто добавляем закрывающий
                self.result.append(f'</{tag}>')

    def handle_data(self, data):
        self.result.append(data)

    def get_fixed_html(self):
        # Закрываем все незакрытые теги
        while self.tag_stack:
            tag = self.tag_stack.pop()
            self.result.append(f'</{tag}>')

        return ''.join(self.result)


def fix_unclosed_tags(html_text: str) -> str:
    """
    Автоматически исправляет незакрытые HTML-теги.

    Args:
        html_text: HTML текст с возможными незакрытыми тегами

    Returns:
        Исправленный HTML текст
    """
    if not html_text:
        return ""

    try:
        fixer = HTMLTagFixer()
        fixer.feed(html_text)
        return fixer.get_fixed_html()
    except Exception as e:
        # Если парсер не смог обработать, возвращаем оригинал
        import logging
        logging.warning(f"Не удалось исправить HTML теги: {e}")
        return html_text


def sanitize_telegram_html(html_text: str) -> str:
    """
    Удаляет неподдерживаемые Telegram HTML-теги, оставляя только разрешенные.
    Также автоматически закрывает незакрытые теги.

    Разрешенные теги: b, i, u, s, code, pre, a

    Args:
        html_text: Текст с HTML-тегами

    Returns:
        Текст только с поддерживаемыми Telegram тегами
    """
    if not html_text:
        return ""

    text = html_text

    # Список неподдерживаемых тегов для удаления
    unsupported_tags = [
        'div', 'span', 'p', 'br', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
        'ul', 'ol', 'li', 'table', 'tr', 'td', 'th', 'thead', 'tbody',
        'strong', 'em', 'del', 'ins', 'mark', 'small', 'big',
        'section', 'article', 'header', 'footer', 'nav', 'aside'
    ]

    # Удаляем неподдерживаемые теги, сохраняя их содержимое
    for tag in unsupported_tags:
        # Удаляем открывающий и закрывающий теги
        text = re.sub(rf'<{tag}[^>]*>', '', text, flags=re.IGNORECASE)
        text = re.sub(rf'</{tag}>', '', text, flags=re.IGNORECASE)

    # Заменяем <br>, <br/>, <br /> на перенос строки
    text = re.sub(r'<br\s*/?\s*>', '\n', text, flags=re.IGNORECASE)

    # Заменяем <strong> на <b>
    text = re.sub(r'<strong>', '<b>', text, flags=re.IGNORECASE)
    text = re.sub(r'</strong>', '</b>', text, flags=re.IGNORECASE)

    # Заменяем <em> на <i>
    text = re.sub(r'<em>', '<i>', text, flags=re.IGNORECASE)
    text = re.sub(r'</em>', '</i>', text, flags=re.IGNORECASE)

    # Удаляем любые оставшиеся HTML-комментарии
    text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)

    # ВАЖНО: Исправляем незакрытые теги перед отправкой в Telegram
    text = fix_unclosed_tags(text)

    # Очистка: убираем лишние пустые строки (более 2 подряд)
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()



def html_to_bbcode(html_text: str) -> str:
    """
    Конвертирует HTML-разметку Telegram в BB-код для форумов.

    Args:
        html_text: Текст с HTML-тегами

    Returns:
        Текст с BB-кодом
    """
    if not html_text:
        return ""

    text = html_text

    # Декодируем HTML-сущности (&lt; → <, &gt; → >, &amp; → & и т.д.)
    text = unescape(text)

    # Конвертация тегов (порядок важен!)

    # 1. Жирный текст: <b> → [B]
    text = re.sub(r'<b>(.*?)</b>', r'[B]\1[/B]', text, flags=re.IGNORECASE | re.DOTALL)

    # 2. Курсив: <i> → [I]
    text = re.sub(r'<i>(.*?)</i>', r'[I]\1[/I]', text, flags=re.IGNORECASE | re.DOTALL)

    # 3. Подчеркивание: <u> → [U]
    text = re.sub(r'<u>(.*?)</u>', r'[U]\1[/U]', text, flags=re.IGNORECASE | re.DOTALL)

    # 4. Зачеркнутый: <s> → [S]
    text = re.sub(r'<s>(.*?)</s>', r'[S]\1[/S]', text, flags=re.IGNORECASE | re.DOTALL)

    # 5. Код/цитата: <pre> → [CODE]
    text = re.sub(r'<pre>(.*?)</pre>', r'[CODE]\1[/CODE]', text, flags=re.IGNORECASE | re.DOTALL)

    # 6. Ссылки: <a href="URL"> → [URL=URL]
    text = re.sub(
        r'<a\s+href=["\'](.*?)["\']>(.*?)</a>',
        r'[URL=\1]\2[/URL]',
        text,
        flags=re.IGNORECASE | re.DOTALL
    )

    # 7. Удаляем оставшиеся HTML-теги (если есть)
    text = re.sub(r'<[^>]+>', '', text)

    # 8. Очистка: убираем лишние пустые строки (более 2 подряд)
    text = re.sub(r'\n{3,}', '\n\n', text)

    # 9. Убираем пробелы в начале и конце
    text = text.strip()

    return text


def save_bbcode_to_file(bbcode_text: str, filename: str = "complaint.txt") -> str:
    """
    Сохраняет BB-код в текстовый файл и возвращает путь к файлу.

    Args:
        bbcode_text: Текст с BB-кодом
        filename: Имя файла для сохранения

    Returns:
        Путь к сохраненному файлу
    """
    import os
    from config import BASE_DIR

    # Создаем директорию для временных файлов, если её нет
    temp_dir = os.path.join(BASE_DIR, "temp_files")
    os.makedirs(temp_dir, exist_ok=True)

    # Полный путь к файлу
    file_path = os.path.join(temp_dir, filename)

    # Сохраняем BB-код в файл
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(bbcode_text)

    return file_path


# Пример использования (для тестирования)
if __name__ == "__main__":
    # Тест санитизации
    dirty_html = """
<div>
    <h1>Заголовок</h1>
    <p>Параграф с <strong>жирным</strong> и <em>курсивом</em></p>
    <span>Текст в span</span><br>
    <b>Разрешенный тег</b>
</div>
"""

    print("=== ТЕСТ САНИТИЗАЦИИ ===")
    print("ДО:")
    print(dirty_html)
    print("\nПОСЛЕ:")
    clean_html = sanitize_telegram_html(dirty_html)
    print(clean_html)

    print("\n\n=== ТЕСТ КОНВЕРТАЦИИ В BB-КОД ===")
    test_html = """
<b>═══════════════════════════════</b>
<b>ЖАЛОБА В ПРОКУРАТУРУ ШТАТА SAN ANDREAS</b>
<b>═══════════════════════════════</b>

<b>Кому:</b> Прокурору штата San Andreas
<b>От кого:</b> Джон Доу, тел. 555-1234

<b>СУТЬ ОБРАЩЕНИЯ:</b>

Прошу привлечь к ответственности офицера LSPD <i>Майкла Смита</i> за превышение должностных полномочий.

<b>ПРАВОВОЕ ОБОСНОВАНИЕ:</b>

• <i>Уголовный кодекс, статья 5.1, пункт 2</i>: превышение полномочий
• <i>Процессуальный кодекс, статья 2.3</i>: незаконный обыск

<b>Дата:</b> 29.10.2025
<b>Подпись:</b> Джон Доу
"""

    bbcode = html_to_bbcode(test_html)
    print("BB-CODE OUTPUT:")
    print(bbcode)

