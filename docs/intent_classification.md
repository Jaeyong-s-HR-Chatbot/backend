# 최종 의도 분류 (2026-11-23)

질문이 들어오면 **규칙이 먼저** 분류하고, 규칙이 약한 곳만 **분류 보조 LLM**에 물어요. LLM은 `CLASSIFY_LLM=on`이고 실제 LLM(`LLM_BASE_URL`)이 있을 때만 쓰고, 호출이 실패하면 규칙 결과를 그대로 써요. 테스트는 기본으로 분류 LLM을 꺼요.

```
질문 → guard (로그용 개인정보 가림)
 → classify
   0. 보안문서 판단 (conf_doc)            규칙 → 노사·징계·회의 단어가 있으면 [LLM ①] 회의록 필요 여부 (로컬 LLM만)
   1. 역할 분기 (토큰의 role)
      ├─ employee: 규칙 분류 (_classify_rule)
      │    ├─ 규칙이 sensitive·refuse이거나 질문 내용이 없으면 → 그대로 (LLM 안 부름)
      │    └─ 그 밖 → [LLM ②] 의도 + 계산 값
      │         · LLM이 sensitive·refuse면 → 그 의도 (규칙이 놓친 민감·타인 조회 보완)
      │         · 규칙이 rule·out_of_scope면 → LLM 의도 (lookup·both·no_question 포함)
      │         · 규칙이 lookup·both면 → 규칙 의도 유지
      │         · 계산 값은 LLM 값 사용 (CALC_TOOLS=on일 때 계산에 씀)
      └─ hr_admin: 규칙 분류 (admin_classify) → [LLM ③] 의도·집계 도구
           · 민감 단어 → 끊지 않고 sensitive_topic 표시만 (규칙)
           · 특정인 챗봇 대화·민감 상담자 신원 → refuse (chat_log, 규칙이 정하면 LLM 안 부름)
           · 그 밖은 LLM이 의도(rule·lookup·both·out_of_scope·refuse)와 도구를 고름 (실패하면 규칙 결과)
             LLM이 규칙과 같은 도구면 규칙 인자 유지, 다른 도구면 LLM 인자만 (필수 인자를 못 채우면 규칙 결과)
           · 도구가 정해지면 [LLM] 규칙이 비워 둔 기간·기준값만 채움 (질문에 근거가 있는 값만)
 → 경로: 민감 안내 / 대화 기록 거절 / 기밀문서 답변(conf_chat, 권한 확인) / 거절 / 질문 없음 안내 / 범위 밖 안내 / 조회·집계·검색 → 답변 LLM
```

조회 도구(본인 조회)는 의도와 상관없이 질문 단어로 골라요 (`tools.run_lookups`). 사번은 항상 토큰의 사번만 써요.

## 의도와 경계 기준

| 의도 | 뜻 |
| --- | --- |
| rule | 규정·절차·조건만으로 답함. 가정·일반 조건으로 값을 준 질문 ("통상임금 320만원이면", "직급 4급일 때") |
| lookup | 로그인한 본인의 저장 데이터만 확인 ("내 연차 며칠 남았어?", "내 신청 승인됐어?") |
| both | 본인 데이터 + 규정. **본인 상황을 말하며 값을 준 질문도 both** ("제 통상임금 320만인데", "작년 3월에 입사했는데") |
| sensitive | 괴롭힘·성희롱·폭언 등 피해 호소·도움 요청 (단어가 없어도 피해 호소면). 규정·절차만 물으면 rule |
| refuse | 다른 직원의 개인정보·급여·인사정보 요구 |
| no_question | 인사말·감사·자기소개·접속 확인, 개인정보만 적은 문장 |
| out_of_scope | 인사 규정·본인 인사 데이터와 무관, 다른 조직의 제도 |

- 저장된 신청 상태는 lookup, 앞으로 신청할 방법·서류는 rule.
- 징계 절차·기준 질문은 rule (민감 아님).
- "본인 상황 + 값 → both"는 2026-11-23에 통일했어요. 기존 평가셋 14문항, 처음 보는 질문 2문항, few-shot 예시 2개의 정답을 이 기준으로 고쳤어요.

## 측정 (Colab · Qwen3.5-27B)

| | 규칙만 | 규칙+LLM |
| --- | ---: | ---: |
| 임직원 기존 평가셋 (146문장) | 93.2% | 95.9% |
| 임직원 처음 보는 질문 (103문장, GPT 생성) | 48.5% | 88.3% |
| 인사팀 기존 골든셋 의도·도구 / 인자 (122문항) | 100% · 100% / 98.3% | 100% · 100% / 98.3% |
| 인사팀 처음 보는 질문 의도·도구 / 인자 (89문항) | 77.5% · 77.3% / 43.2% | 의도·도구 100% · 100% (전부 LLM, 비교표) / 인자 59.1% (예전 방식으로 잰 값) |
| 보안문서 판단 재현율 / 오탐 (41문항) | 44% / 3 | 96% / 1 |

- 위 임직원 숫자는 "본인 상황 + 값 → both" 정답 통일 **전**에 잰 값이에요. 통일 후 규칙만은 dev 87.5% · test 95.9% · 처음 보는 질문 48.5%예요. 규칙+LLM은 Colab을 다시 돌려야 해요.
- 분류 LLM은 질문당 약 0.4~0.6초 더 걸려요 (27B, GPU). 로컬 CPU(qwen3.5:4b)는 15초 이상이라 시연 땐 `CLASSIFY_LLM=off`.
- 기존 평가셋 점수는 규칙을 그 문장에 맞춰 만들어서 높아요. 실제 성능은 처음 보는 질문 점수에 가까워요.

## 평가 방법

```
python scripts/eval_intent.py --split test [--llm]
python scripts/eval_intent.py --cases eval/holdout/holdout_employee.jsonl --exclude H-E-016,H-E-096 [--llm]
python scripts/eval_admin_intent.py [--llm]
python scripts/eval_admin_intent.py --cases eval/holdout/holdout_admin.jsonl --exclude H-A-001 [--llm]
python scripts/eval_conf_doc.py [--llm]
```

GPU가 없으면 `notebooks/admin_intent_eval_27b.ipynb`(Colab)로 세 자리와 처음 보는 질문을 한 번에 재요. `eval_intent.py --llm` 결과에는 LLM 의도를 어디까지 쓸지 세 방식(예전·지금·전부 LLM) 비교표도 나와요.

## 남은 문제

- 피해 호소에 "다른 사람·다른 직원"이 들어가면 규칙이 refuse로 정해 LLM을 안 불러요 (H-E-055·059). 고칠 예정.
- 로그인 사번과 다른 사번으로 자기소개만 하면 no_question으로 바꿨어요 (H-E-078·082·088).
- 인사팀은 의도·도구를 전부 LLM이 고르게 바꿨어요(2026-11-23). 인사팀 질문마다 분류 LLM 호출이 한 번 늘어요(약 0.6초).
  임직원은 전부 LLM이 새로 틀리는 문장이 있어 규칙이 rule·out_of_scope일 때만 LLM 의도를 써요.
- 배포할 때 보안문서 판단(①)·기밀문서 답변은 로컬 LLM만 써서, LLM을 다른 서버에 두면 규칙만 남아요. 허용 주소 설정이 필요해요.
