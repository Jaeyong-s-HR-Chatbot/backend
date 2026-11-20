import re

H = "[가-힣]"
TITLES = ("본부장|위원장|팀장|과장|차장|부장|대리|사원|주임|간사|위원님|위원|대표|씨|님")
ROLES = ("위원장|외부위원|위원|간사|징계대상자|대상자|사용자대표|근로자대표|사용자위원|근로자위원|팀장|과장")
NOT_NAME = re.compile(r"(팀|실|부|과|회|청|원|님|분|들)$")  # 부서·기관·일반 명사 끝말
STOP = {"간사님", "위원님", "대상자", "본인", "요구권자", "다음", "그때", "작년", "올해", "회사", "저희", "근로자", "사용자"}
JOSA = "(?=[은는이가을를의도와과께랑]|[^가-힣]|$)"

NAME_TITLE = re.compile(rf"(?<!{H})({H}{{2,4}}?)\s?(?:{TITLES})(?!{H}{{2}})")
ROLE_NAME = re.compile(rf"(?:{ROLES})\s+({H}{{3}})(?!{H})")
BIRTH = re.compile(r"(\d{2,4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일(?=\s*생)")
SABUN = re.compile(r"(?<![A-Za-z0-9])E\d{4}(?!\d)")
PHONE = re.compile(r"01[016789]-?\d{3,4}-?\d{4}")
RRN = re.compile(r"\d{6}-[1-4]\d{6}")
NAME_FIELDS = {"위원장", "출석위원", "성명", "서명 위원", "서명 위원장", "근로자위원", "사용자위원", "참석"}
ID_FIELDS = {"생년월일"}


def _ok_name(s):
    return 2 <= len(s) <= 4 and s not in STOP and not NOT_NAME.search(s)


def predict(text, article="", field="text"):
    """조각 하나의 글자에서 개체 목록 [(type, start, end, text)] 반환"""
    ents = set()
    key = article.split(" ", 1)[-1] if article.startswith("징계대상자") else article
    # 칸 규칙
    if field == "speaker_label" or key in NAME_FIELDS:
        for m in re.finditer(rf"{H}{{2,4}}", text):
            s = m.group()
            if field == "speaker_label" and m.start() == 0:
                continue
            if key == "참석" and re.fullmatch(ROLES, s):
                continue
            if _ok_name(s) and not re.fullmatch(ROLES, s):
                ents.add(("PS", m.start(), m.end(), s))
        return sorted(ents, key=lambda x: x[1])
    if key in ID_FIELDS and text.strip():
        s = text.strip()
        st = text.find(s)
        ents.add(("ID", st, st + len(s), s))
        return sorted(ents, key=lambda x: x[1])
    # 문맥 규칙
    for m in NAME_TITLE.finditer(text):
        s = m.group(1)
        if _ok_name(s):
            ents.add(("PS", m.start(1), m.end(1), s))
    for m in ROLE_NAME.finditer(text):
        s = m.group(1)
        if _ok_name(s):
            ents.add(("PS", m.start(1), m.end(1), s))
    for rx in (BIRTH,):
        for m in rx.finditer(text):
            ents.add(("ID", m.start(), m.end(), m.group()))
    for rx in (SABUN, PHONE, RRN):
        for m in rx.finditer(text):
            ents.add(("ID", m.start(), m.end(), m.group()))
    return sorted(ents, key=lambda x: x[1])


def mask(text, ents):
    """찾은 개체를 [이름]/[식별정보]로 가림 (뒤에서부터 바꿔 위치가 안 밀리게)"""
    rep = {"PS": "[이름]", "ID": "[식별정보]"}
    for t, s, e, _ in sorted(ents, key=lambda x: -x[1]):
        text = text[:s] + rep[t] + text[e:]
    return text
