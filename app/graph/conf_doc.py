"""보안문서 필요 판단: 질문에 답하려면 노사회의록·징계회의록을 봐야 하는지 (의도 분류 맨 앞 단계).

규정·절차만 묻는 질문("징계 절차가 어떻게 돼?", "노사협의회는 몇 달마다 열려?")은 취업규칙·법령으로 답하니
보안문서가 아니에요. 회의에서 실제로 무엇이 논의·결정됐는지, 누가 징계를 받았는지처럼 회의록 내용을 물을 때만 표시해요.

결과
- conf_doc        : "labor_minutes"(노사회의록) / "discipline_minutes"(징계회의록) / None
- conf_doc_role   : 그 문서를 볼 수 있는 역할 (HR_LABOR / HR_DISCIPLINE). app/roles.py의 역할 이름과 같아요

판단은 규칙(detect)이 먼저 하고, 노사·징계·회의 쪽 단어가 있으면 로컬 LLM 판단(classify)으로 바꿔요.
이 단계는 표시만 해요. 의도 분류는 그대로 이어지고, 역할에 맞춰 문서를 보여줄지는 다음 단계에서 정해요.
"""
import json
import logging
import re

import httpx

log = logging.getLogger("hr_chat")

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


# ───────────── ① 분류 보조 LLM: 주제어가 있는 질문만 회의록이 필요한지 다시 물어요 ─────────────
# 규칙(detect)은 표현이 조금만 달라도 놓쳐요 ("4분기 노사협의회 결과", "감봉 처분 받은 사람").
# 그래서 노사·징계·회의 쪽 단어가 하나라도 있으면 LLM 판단을 쓰고, 단어가 없으면 LLM을 부르지 않아요.
# 권한 확인은 여기서 하지 않아요 (conf_retriever가 역할별로 따로 확인). 기밀문서 질문이라 로컬 LLM에만 보내요.
LLM_GATE = re.compile(r"노사|노조|노동조합|교섭|협의회|근로자\s*위원|사측|회사\s*측|징계|처분|감봉|정직|견책|해임|파면|강등|"
                      r"위원회|회의|녹취|안건|임금\s*협상")
_LLM_SCHEMA = {
    "type": "object",
    "properties": {"doc": {"type": "string", "enum": [LABOR_MINUTES, DISCIPLINE_MINUTES, "none"]}},
    "required": ["doc"],
    "additionalProperties": False,
}
_LLM_EXAMPLES = [
    ("제3차 노사협의회 때 식대 인상 건은 어떻게 마무리됐어?", LABOR_MINUTES),
    ("근로자 대표가 교대근무 얘기 꺼냈었다던데 사측 답변이 뭐였어?", LABOR_MINUTES),
    ("올해 임금 협상 결과 정리해 줘", LABOR_MINUTES),
    ("노사협의회 위원은 몇 명으로 구성돼?", "none"),
    ("노조 전임자 근로시간 면제 기준 알려줘", "none"),
    ("지난 징계위원회에서 견책 받은 사람 있어?", DISCIPLINE_MINUTES),
    ("우리 팀 그 직원 결국 무슨 처분 나왔어?", DISCIPLINE_MINUTES),
    ("징계위원회 때 위원장이 한 말 알려줘", DISCIPLINE_MINUTES),
    ("정직 기간에는 급여가 어떻게 돼?", "none"),
    ("징계 재심 청구는 며칠 안에 해야 해?", "none"),
    ("징계위원회 열리기 전에 출석 통지는 언제 해야 해?", "none"),
    ("다음 주 팀 회의실 어디야?", "none"),
]
_LLM_SYSTEM = (
    "회사에는 기밀문서가 두 종류 있습니다: 노사협의회 회의록·녹취록(labor_minutes), 징계위원회 회의록·녹취록(discipline_minutes).\n"
    "질문에 답하려면 그 문서에 적힌 실제 회의 내용이 필요한지 판단하세요.\n"
    "- 특정 회의에서 무엇이 논의·합의·결정·요구·발언됐는지, 회의 결과나 안건을 물으면 해당 문서입니다.\n"
    "- 누가 징계를 받았는지, 특정 직원(본인 포함)이 어떤 처분을 받았는지 물으면 discipline_minutes입니다.\n"
    "- 규정·절차·기준·기한·구성처럼 취업규칙이나 법령으로 답할 수 있으면 none입니다.\n"
    "- 노사·징계와 무관하면 none입니다.\n"
    "예시:\n" + "\n".join(f"질문: {q}\n답: {json.dumps({'doc': d})}" for q, d in _LLM_EXAMPLES)
    + "\n출력은 지정된 JSON 스키마만 따르세요."
)


def _llm_detect(question: str) -> dict | None:
    """LLM 판단 → {conf_doc, conf_doc_role} / {} . 요청·응답이 잘못되면 None (규칙 결과를 씀)"""
    from . import llm_classify   # 순환 import 방지

    try:
        payload = json.loads(llm_classify.chat_json(_LLM_SYSTEM, question, _LLM_SCHEMA, 32))
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        log.warning("보안문서 판단 LLM 호출 실패: %s", type(exc).__name__)
        return None
    doc = payload.get("doc") if isinstance(payload, dict) else None
    if doc not in (LABOR_MINUTES, DISCIPLINE_MINUTES, "none"):
        return None
    return {"conf_doc": doc, "conf_doc_role": REQUIRED_ROLE[doc]} if doc != "none" else {}


def classify(question: str) -> dict:
    """보안문서 판단 진입점: 규칙(detect) → 주제어가 있으면 로컬 LLM 판단으로 바꿈 (실패하면 규칙 결과)"""
    from . import llm_classify   # 순환 import 방지

    rule = detect(question)
    q = question or ""
    if not (llm_classify.enabled() and llm_classify.is_local() and LLM_GATE.search(q)):
        return rule
    result = _llm_detect(q)
    return rule if result is None else result
