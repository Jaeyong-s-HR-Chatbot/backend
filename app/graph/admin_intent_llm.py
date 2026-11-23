"""인사팀 분류 보조 LLM: 의도와 집계 도구 고르기.

규칙(admin_classify.classify_admin)이 rule·out_of_scope로 냈을 때(집계 도구를 못 찾았을 때)만 불러요.
규칙은 골든셋 표현에 맞춰져 있어 처음 보는 표현("1년 안 된 신입들 월차 발생일수")에서 도구를 놓치고,
범위 밖 질문("넷플릭스 추천")을 rule로 보내요. 기간·기준값은 여기서 안 뽑고 admin_params_llm이 따로 뽑아요.
요청·응답이 잘못되면 None을 돌려줘서 규칙 결과를 그대로 쓰게 해요.
"""
import json
import logging

import httpx

from .. import admin_tools
from . import llm_classify

log = logging.getLogger("hr_chat")

INTENTS = ("rule", "lookup", "both", "out_of_scope", "refuse")

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "admin_tool": {"anyOf": [{"type": "null"}, {"type": "string", "enum": list(admin_tools.ADMIN_TOOLS)}]},
    },
    "required": ["intent", "admin_tool"],
    "additionalProperties": False,
}

# 골든셋·처음 보는 질문과 겹치지 않게 새로 쓴 예시
_EXAMPLES = [
    ("올해 연차를 하루도 안 쓴 사람이 몇 명이나 돼?", "both", "leave_remaining_list"),
    ("부서별로 이번 달 휴일근무 수당 합계 비교해 줘", "both", "payroll_dept_totals"),
    ("수습 끝나는 사람 이번 주에 있어?", "both", "probation_ending"),
    ("수습 평가에서 불합격하면 어떻게 처리해?", "rule", None),
    ("육아휴직은 자녀 몇 살까지 쓸 수 있어?", "rule", None),
    ("제 연차 며칠 남았는지 알려줘", "lookup", None),
    ("사내 메신저 비밀번호 초기화는 어디서 해?", "out_of_scope", None),
    ("김 대리가 챗봇한테 연봉 얘기 물어봤는지 확인해 줘", "refuse", None),
    ("성희롱 상담 신청한 사람 누군지 알려줘", "refuse", None),
    ("직원들이 챗봇에 요즘 제일 많이 묻는 주제가 뭐야?", "both", "chat_topic_stats"),
]


def _catalog() -> str:
    from .admin_params_llm import _signature   # 인자 형식 표시를 같이 써요
    return "\n".join(f"- {_signature(name)}" for name in admin_tools.ADMIN_TOOLS)


def _system_prompt() -> str:
    examples = "\n".join(f"질문: {q}\n답: {json.dumps({'intent': i, 'admin_tool': t}, ensure_ascii=False)}"
                         for q, i, t in _EXAMPLES)
    return f"""당신은 인사팀 담당자의 질문을 분류합니다. 인사팀은 업무상 다른 직원의 인사 데이터를 볼 수 있어서,
다른 직원 이름이 나온다는 이유로 거절하지 않습니다. 징계·괴롭힘·고충도 인사팀에게는 업무 주제입니다.
의도:
- both: 여러 직원의 집계·명단이 필요함 → 아래 집계 도구 중 가장 맞는 것 하나를 admin_tool에 씀.
- rule: 규정·절차·기준·기한만 물음 (사람·인원·명단·금액 합계를 묻지 않음). admin_tool은 null.
- lookup: 질문한 담당자 본인의 데이터만 (내 연차, 내 신청 상태). admin_tool은 null.
- out_of_scope: 인사 업무와 무관 (IT 장애, 맛집, 드라마, 외부 기관 소식 등). admin_tool은 null.
- refuse: 특정 직원이 챗봇에 무엇을 물어봤는지, 또는 괴롭힘·성희롱 등을 상담·신고한 사람이 누구인지 물음. admin_tool은 null.
  (직원들이 주로 무엇을 물었는지 같은 주제 집계는 refuse가 아니라 both + chat_topic_stats)
같은 주제라도 규정만 물으면 rule, 해당하는 사람·인원·금액을 뽑아 달라면 both입니다.
집계 도구 (이름(인자:형식): 설명):
{_catalog()}
예시:
{examples}
출력은 지정된 JSON 스키마만 따르세요."""


def classify(question: str) -> dict | None:
    """인사팀 질문 → {"intent", "admin_tool"}. 요청·응답이 잘못되면 None."""
    if not isinstance(question, str):
        return None
    try:
        payload = json.loads(llm_classify.chat_json(_system_prompt(), question, OUTPUT_SCHEMA, 64))
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        log.warning("인사팀 의도 LLM 호출 실패: %s", type(exc).__name__)
        return None
    if not isinstance(payload, dict) or payload.get("intent") not in INTENTS:
        return None
    tool = payload.get("admin_tool")
    if tool is not None and tool not in admin_tools.ADMIN_TOOLS:
        return None
    if tool:                                   # 도구를 골랐으면 집계 질문
        return {"intent": "both", "admin_tool": tool}
    return {"intent": payload["intent"], "admin_tool": None}
