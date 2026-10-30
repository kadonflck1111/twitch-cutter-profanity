"""
Высокоточный модуль обнаружения ненормативной лексики.
Исключены любые ложные срабатывания на обычные слова русской речи (тебе, себе, ребята, ребенок, небо, мебель, хлеб и т.д.).
Поддерживает режим мягкой цензуры (оставляет слышимым окончание слова для сохранения контекста и голоса стримера).
"""

import math
import re
from functools import lru_cache
from typing import List, Set, Optional, Dict, Any, Tuple

# Точный список исключений (обычные русские слова, которые ни при каких условиях не должны глушиться)
STRICT_EXCLUSIONS = {
    "тебе", "тебя", "себе", "себя", "ребята", "ребятам", "ребятах", "ребятами",
    "ребенок", "ребёнок", "ребенка", "ребёнка", "небо", "небеса", "небе", "небом",
    "мебель", "мебели", "хлеб", "хлеба", "рубль", "рубля", "рублей", "рублям",
    "колебания", "колебаний", "потребление", "потребления", "употребление",
    "стебель", "стебля", "гребля", "гребли", "лебедь", "лебеди", "сабля", "сабли",
    "ястреб", "ястреба", "оскорблять", "употреблять", "барсук", "барсука", "посуда",
    "посуду", "рассудок", "рассуждать", "суматоха", "сутки", "суток", "сумма",
    "сумерки", "суслик", "сугроб", "сустав", "сухой", "сухие", "суша",
    "команда", "дрозд", "дробь", "рябчик", "теребить", "педикюр", "мудрость",
    "мудрый", "мудрец", "гондола", "гондольер", "шлюз", "страус", "страх",
    "выхухоль", "перегиб", "погиб", "наезд", "отъезд", "подъезд", "заезд",
    "проезд", "съезд", "уезд", "поезд", "выезд", "не был", "не была", "не было"
}

# 1. ХУЙ (все формы и приставки)
RE_XUY = re.compile(
    r'(?i)\b(?:(?:на|по|до|ни|за|от|вы|с|при|пере|под|про)?[хx][уy](?:[йеёяию]|ли|ле|ем|яри|вин|сос|пл[её]т)\w*|[хx][уy][йеёяию]\w*)\b'
)

# 2. ПИЗД (пиздец, пизда, спиздил, распиздяй и т.д.)
RE_PIZD = re.compile(
    r'(?i)\b\w*п[иеё]зд\w*\b'
)

# 3. ЕБ / ЁБ (только с матерными корнями и приставками, БЕЗ совпадений с 'тебе/себе/ребята')
RE_EB = re.compile(
    r'(?i)\b(?:(?:за|до|на|об|от|по|под|пере|при|про|раз|с|съ|у|вы|въ|долбо|в)?(?:еб|ёб|ебл|ёбл)(?:[а-яё]*)|еб[а-яё]+|ёб[а-яё]+)\b'
)

# 4. БЛЯ (строго матерные формы, исключает 'рубля', 'корабля', 'сабля')
RE_BLYA = re.compile(
    r'(?i)\b(?:бля|блять|блядь|блядина|бляди|блядский|блядство|блядская|блядские|блядей|поблядушк\w*)\b'
)

# 5. МУД (мудак, мудила и т.д.)
RE_MUD = re.compile(
    r'(?i)\b(?:мудак\w*|мудил\w*|мудозвон\w*|мудоеб\w*|мудень|муди)\b'
)

# 6. ЗАЛУП, ГАНДОН, ПИДОР
RE_OTHER = re.compile(
    r'(?i)\b(?:залуп\w*|г[ао]ндон\w*|пид[оае]р\w*|пид[ао]рас\w*|п[еи]дрил\w*)\b'
)

# 7. Мягкие ругательства (СУКА, ШЛЮХА, ГОВНО) - только точные формы
RE_SOFT = [
    re.compile(r'(?i)\b(?:сука|суки|суку|сукой|сучке|сучка|сучара|сучий|сучье)\b'),
    re.compile(r'(?i)\b(?:шлюх\w*|дроч\w*|говн\w*|дерьм\w*|ублюд\w*|чмо\w*|мраз\w*|отсос\w*)\b')
]

# Маскированные слова со звездочками (б***ь, п****ц, х**)
RE_MASKED = re.compile(r'(?i)(?:[а-яёa-z]+[*@#$]{2,}[а-яёa-z]*|[*@#$]{2,}[а-яёa-z]+)')

# Двусловные матерные фразы
BIGRAMS = [
    ("на", "хуй"), ("на", "хер"), ("по", "хуй"), ("по", "хер"),
    ("в", "пизду"), ("до", "хуя"), ("ни", "хуя"), ("ни", "хрена")
]


HARD_PATTERNS = (RE_XUY, RE_PIZD, RE_EB, RE_BLYA, RE_MUD, RE_OTHER)
NORMALIZE_RE = re.compile(r'[^\w\s*@#$-]')


@lru_cache(maxsize=8192)
def normalize_word(word: str) -> str:
    return NORMALIZE_RE.sub('', word).strip().casefold().replace("ё", "е")


def _is_profane(cleaned, stopwords, include_soft, use_whitelist):
    if not cleaned or len(cleaned) < 2:
        return False

    # 1. Проверка маскированных слов
    if RE_MASKED.search(cleaned):
        return True

    # 2. Пользовательские стоп-слова
    if cleaned in stopwords:
        return True

    # 3. Строгий белый список
    if use_whitelist and cleaned in STRICT_EXCLUSIONS:
        return False

    # 4. Жесткий мат
    for pat in HARD_PATTERNS:
        if pat.search(cleaned):
            return True

    # 5. Мягкая лексика (только если включена пользователем)
    if include_soft:
        for pat in RE_SOFT:
            if pat.search(cleaned):
                return True

    return False


def is_profane_word(
    word: str, custom_stopwords: Optional[Set[str]] = None, include_soft: bool = False,
    use_whitelist: bool = True,
) -> bool:
    stopwords = {normalize_word(w) for w in custom_stopwords or ()}
    return _is_profane(normalize_word(word), stopwords, include_soft, use_whitelist)


def calculate_gentle_mute_range(
    start: float,
    end: float,
    leave_end_sec: float = 0.10
) -> Tuple[float, float]:
    """
    Рассчитывает интервал глушения:
    - Не глушит речь ДО начала матерного слова (pad_before = 0.0)
    - Начинает глушение строго в начале слова (+0.02 сек)
    - Оставляет окончание слова слышимым (leave_end_sec, по умолч. 0.10 сек),
      чтобы зритель понимал контекст и эмоцию стримера, а голос не пропадал.
    """
    if not all(math.isfinite(v) for v in (start, end, leave_end_sec)) or end <= start:
        raise ValueError("Некорректный интервал слова")
    start = max(0.0, start)
    if end <= start:
        raise ValueError("Интервал слова находится до начала записи")
    if leave_end_sec <= 0:
        return start, end
    duration = end - start
    m_start = start + min(0.02, duration * 0.1)
    m_end = end - min(leave_end_sec, duration * 0.35)
    return m_start, max(m_start, m_end)


def find_profanities_in_words(
    words: list,
    custom_stopwords: Optional[Set[str]] = None,
    include_soft: bool = False,
    leave_end_sec: float = 0.10,
    use_whitelist: bool = True,
) -> List[Dict[str, Any]]:
    """
    Анализирует слова Whisper и вычисляет аккуратные интервалы глушения.
    """
    detected = []
    n = len(words)
    handled_indices = set()
    normalized = [normalize_word(w.word) for w in words]
    stopwords = {normalize_word(w) for w in custom_stopwords or ()}

    for i in range(n):
        if i in handled_indices:
            continue

        w_obj = words[i]
        w_text = w_obj.word
        w_clean = normalized[i]
        if not all(math.isfinite(v) for v in (w_obj.start, w_obj.end)) or w_obj.end <= max(0, w_obj.start):
            continue

        # 1. Биграммы ("на хуй", "в пизду")
        if i + 1 < n and 0 <= words[i + 1].start - w_obj.end <= 0.35:
            next_obj = words[i + 1]
            next_text = next_obj.word
            next_clean = normalized[i + 1]

            is_bi = False
            for b1, b2 in BIGRAMS:
                if w_clean == b1 and (next_clean == b2 or (
                    next_clean.startswith(b2) and _is_profane(next_clean, set(), include_soft, use_whitelist)
                )):
                    is_bi = True
                    break

            # Произвольное склеивание двух слов захватывало соседнюю нормальную речь.
            if is_bi and math.isfinite(next_obj.end) and next_obj.end > next_obj.start:
                m_start, m_end = calculate_gentle_mute_range(w_obj.start, next_obj.end, leave_end_sec)
                detected.append({
                    "word": f"{w_text} {next_text}",
                    "start": m_start,
                    "end": m_end,
                    "orig_start": w_obj.start,
                    "orig_end": next_obj.end
                })
                handled_indices.add(i)
                handled_indices.add(i + 1)
                continue

        # 2. Одиночное слово
        if _is_profane(w_clean, stopwords, include_soft, use_whitelist):
            m_start, m_end = calculate_gentle_mute_range(w_obj.start, w_obj.end, leave_end_sec)
            detected.append({
                "word": w_text,
                "start": m_start,
                "end": m_end,
                "orig_start": w_obj.start,
                "orig_end": w_obj.end
            })
            handled_indices.add(i)

    return detected


def censor_word_display(word: str) -> str:
    clean = normalize_word(word)
    if len(clean) <= 2:
        return "*" * len(word)
    return clean[0] + "*" * (len(clean) - 2) + clean[-1]
