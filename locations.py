# locations.py
import difflib

ALLOWED_LOCATIONS = [
    "Алцак",
    "Армак",
    "Баян",
    "Белоозёрск",
    "Боргой",
    "Боций",
    "Булык",
    "Верхний Бургалтай",
    "Верхний Ёнхор",
    "Верхний Торей",
    "Гэгэтуй",
    "Джида",
    "Додо-Ичётуй",
    "Дырестуй",
    "Дэдэ-Ичётуй",
    "Ёнхор",
    "Желтура",
    "Зарубино",
    "Инзагатуй",
    "Кяхта",
    "Малый Нарын",
    "Мельница",
    "Нарын",
    "Нижний Бургалтай",
    "Нижний Торей",
    "Нюгуй",
    "Оёр",
    "Петропавловка",
    "Подхулдочи",
    "Старый Укырчелон",
    "Тасархай",
    "Тохой",
    "Тэнгэрэк",
    "Улан-Удэ",
    "Улзар",
    "Хужир",
    "Хулдат",
    "Цаган-Усун",
    "Цагатуй",
    "Шартыкей",
    "Гусиноозерск",
]


def norm(s: str) -> str:
    return " ".join((s or "").strip().split()).casefold()

def canonical(user_input: str) -> str | None:
    ni = norm(user_input)
    if not ni:
        return None
    for x in ALLOWED_LOCATIONS:
        if norm(x) == ni:
            return x
    return None

def fuzzy(user_input: str, limit: int = 8) -> list[str]:
    ni = norm(user_input)
    if not ni:
        return []
    # map normalized -> original
    mapping = {norm(x): x for x in ALLOWED_LOCATIONS}
    matches = difflib.get_close_matches(ni, list(mapping.keys()), n=limit, cutoff=0.72)
    return [mapping[m] for m in matches if m in mapping]

def suggestions(user_input: str, limit: int = 12) -> list[str]:
    ni = norm(user_input)
    if not ni:
        return []
    # substring / prefix
    out = []
    for x in ALLOWED_LOCATIONS:
        nx = norm(x)
        if nx.startswith(ni) or ni in nx:
            out.append(x)
    if out:
        return out[:limit]
    # fallback to fuzzy
    return fuzzy(user_input, limit=limit)
