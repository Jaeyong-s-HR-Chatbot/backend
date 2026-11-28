import csv
import re
from pathlib import Path

HERE = Path(__file__).parent
H = "[가-힣]"
K = 3  # 이 인원 미만이면 특정될 수 있다고 봄 (1~2명이면 가림)

POS_WORDS = ["본부장", "팀장", "실장", "사원", "대리", "과장", "차장", "부장", "주임"]
POS_RX = re.compile(rf"(?<!{H})({'|'.join(POS_WORDS)})")
OG_RX = re.compile(rf"(?<!{H})({H}[가-힣0-9]*?(?:팀|본부))(?=[은는이가을를의도와과랑에께들]|[^가-힣]|$)")
OG_SIL = {"비서실", "감사실", "기획실", "홍보실", "안전실"}          # "…실" 부서는 목록으로만 (회의실·소명하실 제외)
OG_STOP = {"이번팀", "그팀", "우리팀", "팀"}
DEPT_SUFFIX = re.compile(r"(팀|본부|실|부|처|센터|사업소)$")

# 부서 바로 뒤에 특정한 한 사람을 가리키는 말이 오면 부서만으로도 그 사람이 드러날 수 있음 ("건축설비팀 작업자 한 명")
PERSON_REF = re.compile(r"작업자|직원|정비원|기관사|승무원|역무원|위원님?|반장|담당자|한 명|한 분|한 사람|1명")
PERSON_GAP = 8  # 부서 끝과 사람 지칭 사이 최대 글자 수

SEN_HEALTH = re.compile(r"투석|디스크|수술|입원|진단|우울증|질환|암 |치료|병가를|몸이 안 좋|몸도 안 좋|아프|통원|약을 먹|장애")
SEN_UNION = re.compile(r"노조에 가입|노조 가입|노조 활동|조합원|노조를 탈퇴")
SEN_NOT = re.compile(r"규정|제도|일정|공지|취업규칙|설문")


def load_counts(path=HERE / "rules/org_counts.csv"):
    out = {}
    if Path(path).exists():
        for r in csv.DictReader(open(path, encoding="utf-8")):
            out[(r["dept"], r["position"])] = int(r["count"])
    return out


def dept_names(counts=None):
    """DB에 있는 부서 이름 (rules/org_counts.csv). "…센터", "…사업소"처럼 접미사 규칙에 안 걸리는 부서도 잡기 위함"""
    counts = load_counts() if counts is None else counts
    return sorted({d for d, _ in counts}, key=len, reverse=True)


def find_og(text, model_og=(), depts=None):
    ents = set()
    for d in (dept_names() if depts is None else depts):  # DB 부서 이름 사전
        for m in re.finditer(rf"(?<!{H}){re.escape(d)}", text):
            ents.add(("OG", m.start(), m.end(), d))
    for m in OG_RX.finditer(text):
        if m.group(1) not in OG_STOP:
            ents.add(("OG", m.start(1), m.end(1), m.group(1)))
    for s in OG_SIL:
        for m in re.finditer(rf"(?<!{H}){s}", text):
            ents.add(("OG", m.start(), m.end(), s))
    for t, s, e, w in model_og:  # 공개 모델 OG 중 부서처럼 끝나는 것
        if t == "OG" and DEPT_SUFFIX.search(w) and not w.endswith("회의실"):
            ents.add(("OG", s, e, w))
    # 겹치면 긴 쪽만
    ents = sorted(ents, key=lambda x: (x[1], -(x[2] - x[1])))
    out, last = [], -1
    for x in ents:
        if x[1] >= last:
            out.append(x)
            last = x[2]
    return out


def find_pos(text):
    return [("POS", m.start(1), m.end(1), m.group(1)) for m in POS_RX.finditer(text)]


def find_sen(text):
    """문장 단위 민감정보 종류: '건강' / '노조' / None"""
    if SEN_UNION.search(text):
        return "노조"
    if SEN_HEALTH.search(text) and not SEN_NOT.search(text):
        return "건강"
    return None


def find_qi(text, og, pos, counts, k=K):
    """[(부서개체, 직급개체, 인원 or None, 가림여부)]"""
    out = []
    for o in og:
        for p in pos:
            if 0 <= p[1] - o[2] <= 12 and not re.search(r"[.?!]", text[o[2]:p[1]]):
                n = counts.get((o[3], p[3]))
                out.append((o, p, n, n is None or n < k))
                break
    return out


def find_dept_ref(text, og):
    """사람 지칭이 바로 뒤따르는 부서 개체 → 가릴 부서 목록 [("OG", 시작, 끝, 부서)]"""
    out = []
    for o in og:
        window = text[o[2]:o[2] + PERSON_GAP + 6]
        m = PERSON_REF.search(window)
        if m and m.start() <= PERSON_GAP and not re.search(r"[.?!]", window[:m.start()]):
            out.append(tuple(o))
    return out


def detect(text, model_og=(), counts=None):
    counts = load_counts() if counts is None else counts
    og, pos = find_og(text, model_og, sorted({d for d, _ in counts}, key=len, reverse=True)), find_pos(text)
    return dict(og=og, pos=pos, sen=find_sen(text), qi=find_qi(text, og, pos, counts))


def mask_qi(text, qi):
    """가려야 할 조합의 부서·직급을 [부서]/[직급]으로"""
    spans = []
    for o, p, _, hide in qi:
        if hide:
            spans += [(o[1], o[2], "[부서]"), (p[1], p[2], "[직급]")]
    for s, e, rep in sorted(set(spans), key=lambda x: -x[0]):
        text = text[:s] + rep + text[e:]
    return text


def mask_all(text, ps_id=(), qi=(), sen=None, hide_sen=False):
    """이름·식별정보([이름]/[식별정보]) + 간접 식별 조합([부서]/[직급])을 원문 위치 기준으로 한 번에 가림.
    hide_sen=True면 민감정보 문장은 통째로 [민감정보]로 바꿈 (권한 없는 사람에게 보여 줄 때)."""
    if hide_sen and sen:
        return f"[민감정보: {sen}]"
    rep = {"PS": "[이름]", "ID": "[식별정보]", "OG": "[부서]"}
    spans = [(s, e, rep[t]) for t, s, e, *_ in ps_id if t in rep]
    for o, p, _, hide in qi:
        if hide:
            spans += [(o[1], o[2], "[부서]"), (p[1], p[2], "[직급]")]
    out, last = [], 0
    for s, e, r in sorted(set(spans)):
        if s < last:
            continue
        out.append(text[last:s] + r)
        last = e
    return "".join(out) + text[last:]
