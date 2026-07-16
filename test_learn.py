"""Проверка словаря и обучения. Запуск: python test_learn.py"""

import json
import sys
from pathlib import Path

import dot

TMP = Path(__file__).parent / ".test-tmp"


def setup():
    TMP.mkdir(exist_ok=True)
    dot.CORRECTIONS_FILE = TMP / "corrections.json"
    dot.VOCAB_FILE = TMP / "vocab.txt"
    for f in (dot.CORRECTIONS_FILE, dot.VOCAB_FILE):
        f.unlink(missing_ok=True)


def test_learns_a_name():
    setup()
    n = dot.learn("вчера Тани Алл написал мне", "вчера Дан написал мне")
    assert n == 1, n
    assert dot.load_corrections() == {"Тани Алл": "Дан"}
    assert "Дан" in dot.VOCAB_FILE.read_text("utf-8")


def test_applies_correction_on_word_boundary():
    setup()
    dot.CORRECTIONS_FILE.write_text(json.dumps({"дабл": "Double"}, ensure_ascii=False), "utf-8")
    assert dot.apply_corrections("мой дабл растёт") == "мой Double растёт"
    # "дабл" внутри другого слова трогать нельзя
    assert dot.apply_corrections("даблоид") == "даблоид"


def test_longer_phrase_wins_over_shorter():
    setup()
    dot.CORRECTIONS_FILE.write_text(
        json.dumps({"мем": "MEM", "мем палас": "mempalace"}, ensure_ascii=False), "utf-8"
    )
    assert dot.apply_corrections("открой мем палас") == "открой mempalace"


def test_ignores_a_rewrite():
    setup()
    n = dot.learn(
        "я пошёл в магазин за хлебом",
        "сегодня утром я решил прогуляться до булочной и взять багет",
    )
    assert n == 0, f"переписанную мысль нельзя тащить в словарь, а поймали {n}"


def test_survives_empty_input():
    setup()
    assert dot.learn("", "что-то") == 0
    assert dot.learn("что-то", "") == 0


def test_vocab_is_capped_for_whisper():
    setup()
    dot.add_to_vocab([f"слово{i}" for i in range(200)])
    prompt = dot.read_vocab()
    assert prompt.count(",") == dot.VOCAB_TERMS_KEPT - 1
    assert "слово199" in prompt and "слово0," not in prompt


def test_no_duplicates_in_vocab():
    setup()
    dot.add_to_vocab(["Double", "Дан"])
    dot.add_to_vocab(["double", "Astana"])
    assert dot.VOCAB_FILE.read_text("utf-8").split() == ["Double", "Дан", "Astana"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} проверок прошло")
    sys.exit(0)
