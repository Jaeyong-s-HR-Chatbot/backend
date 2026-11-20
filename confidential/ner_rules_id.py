import re

from . import ner_rules_field as ner_rules_v0

DATE = r"(?:(?:19|20)?\d{2}\s*년\s*\d{1,2}\s*월\s*\d{1,2}\s*일|(?:19|20)\d{2}\s*[.\-/]\s*\d{1,2}\s*[.\-/]\s*\d{1,2}\.?)"
YEAR_BORN = re.compile(r"(?<!\d)(?:19|20)?\d{2}\s*년생")
DATE_SAENG = re.compile(rf"({DATE})(?=\s*생)")
DATE_ANY = re.compile(DATE)
SIX = re.compile(r"(?<![\d-])\d{6}(?![\d-])")
KEY_BIRTH = re.compile(r"생년월일|생일|출생|태어")
KEY_SIX = re.compile(r"생년월일|생일|주민")
WIN = 15


def predict_id(text):
    ents = set()
    for m in YEAR_BORN.finditer(text):
        ents.add(("ID", m.start(), m.end(), m.group()))
    for m in DATE_SAENG.finditer(text):
        ents.add(("ID", m.start(1), m.end(1), m.group(1)))
    for m in DATE_ANY.finditer(text):
        around = text[max(0, m.start() - WIN):m.start()] + " " + text[m.end():m.end() + WIN]
        if KEY_BIRTH.search(around):
            s, e = m.start(), m.end()
            ents.add(("ID", s, e, text[s:e]))
    for m in SIX.finditer(text):
        if KEY_SIX.search(text[max(0, m.start() - WIN):m.start()]):
            ents.add(("ID", m.start(), m.end(), m.group()))
    for rx in (ner_rules_v0.SABUN, ner_rules_v0.PHONE, ner_rules_v0.RRN):
        for m in rx.finditer(text):
            ents.add(("ID", m.start(), m.end(), m.group()))
    # 겹치면 긴 것만
    ents = sorted(ents, key=lambda x: (x[1], -(x[2] - x[1])))
    out, last = [], -1
    for x in ents:
        if x[1] >= last:
            out.append(x)
            last = x[2]
    return out


def predict(text, article="", field="text"):
    """v0와 같은 자리에서 쓰는 함수: 이름은 v0, 식별정보는 v1 (생년월일 칸은 칸 전체)"""
    base = [x for x in ner_rules_v0.predict(text, article, field) if x[0] != "ID"]
    key = article.split(" ", 1)[-1] if article.startswith("징계대상자") else article
    if key in ner_rules_v0.ID_FIELDS and text.strip():
        s = text.strip(); st = text.find(s)
        return base + [("ID", st, st + len(s), s)]
    if field == "speaker_label" or key in ner_rules_v0.NAME_FIELDS:
        return base
    return base + predict_id(text)
