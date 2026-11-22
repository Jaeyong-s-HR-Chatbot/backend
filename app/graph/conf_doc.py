"""보안문서 필요 판단: 질문에 답하려면 노사회의록·징계회의록을 봐야 하는지 (의도 분류 맨 앞 단계).

규정·절차만 묻는 질문("징계 절차가 어떻게 돼?", "노사협의회는 몇 달마다 열려?")은 취업규칙·법령으로 답하니
보안문서가 아니에요. 회의에서 실제로 무엇이 논의·결정됐는지, 누가 징계를 받았는지처럼 회의록 내용을 물을 때만 표시해요.

결과
- conf_doc        : "labor_minutes"(노사회의록) / "discipline_minutes"(징계회의록) / None
- conf_doc_role   : 그 문서를 볼 수 있는 역할 (HR_LABOR / HR_DISCIPLINE). app/roles.py의 역할 이름과 같아요

이 단계는 표시만 해요. 의도 분류는 그대로 이어지고, 역할에 맞춰 문서를 보여줄지는 다음 단계에서 정해요.
"""
import re

LABOR_MINUTES = "labor_minutes"
DISCIPLINE_MINUTES = "discipline_minutes"
REQUIRED_ROLE = {LABOR_MINUTES: "HR_LABOR", DISCIPLINE_MINUTES: "HR_DISCIPLINE"}

# 강한 신호: 회의록이나 실제 회의·사건을 직접 가리킴 → 규정 질문 모양이어도 보안문서
_STRONG = (r"회의록|녹취|회의\s*내용|회의에서|안건|발언|심의\s*(?:내용|결과)|소명\s*내용|"
           r"(?:지난|이번|최근|올해|작년|\d+\s*차|\d+\s*월)\s*\S{0,6}?(?:회의|협의회|위원회|교섭|징계)")
# 약한 신호: 회의 내용일 수도, 일반 규정일 수도 ("징계 받은 사람 보수 얼마나 깎여?"는 규정) → 규정 질문 모양이면 무시
_WEAK = r"논의|의결|합의(?:된|한|했|\s*내용|\s*사항)|결정(?:된|한|했|\s*사항)|사례"
_RULE_ASK = re.compile(r"얼마|몇\s*(?:년|일|달|개월|번|회)|며칠|언제까지|어떻게|해야|기한|규정|절차|기준|가능|수\s*있|되나|돼\?")

_LABOR_TOPIC = re.compile(r"노사|노조|노동조합|단체\s*교섭|임금\s*교섭|근로자\s*위원")
_LABOR_STRONG = re.compile(_STRONG + r"|(?:노사협의회|근로자\s*위원).{0,30}(?:뭐라고|말했|말한|발언)|(?:노조|노동조합|근로자\s*위원)\s*(?:이|가|는|측)?\s*(?:요구|주장|제안)(?:한|했|하는)|쟁점")
_LABOR_WEAK = re.compile(_WEAK + r"|요구\s*사항")
_DISCIPLINE_TOPIC = re.compile(r"징계")
# 누가·명단은 특정인을 묻는 말이라 강한 신호 (징계회의록의 징계 대상자)
_DISCIPLINE_STRONG = re.compile(_STRONG + r"|누가|누구|명단|(?:내|나의|제|저의)\s*징계\s*결과")
_DISCIPLINE_WEAK = re.compile(_WEAK + r"|대상자|받은\s*(?:사람|직원)|징계\s*받은|징계받은")


def _needs(q: str, topic, strong, weak) -> bool:
    if not topic.search(q):
        return False
    return bool(strong.search(q)) or (bool(weak.search(q)) and not _RULE_ASK.search(q))


def detect(question: str) -> dict:
    """질문 → {conf_doc, conf_doc_role}. 보안문서가 필요 없으면 빈 dict"""
    q = question or ""
    doc = None
    if _needs(q, _DISCIPLINE_TOPIC, _DISCIPLINE_STRONG, _DISCIPLINE_WEAK):
        doc = DISCIPLINE_MINUTES
    elif _needs(q, _LABOR_TOPIC, _LABOR_STRONG, _LABOR_WEAK):
        doc = LABOR_MINUTES
    return {"conf_doc": doc, "conf_doc_role": REQUIRED_ROLE[doc]} if doc else {}
