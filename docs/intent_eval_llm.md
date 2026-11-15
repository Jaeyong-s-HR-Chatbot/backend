# 의도 분류 방식 비교 (dev)

정답 데이터는 검수 전 초안

- dev 전체: 92문장
- few-shot 예시 및 같은 쌍 제외: 32문장
- 네 방식 공통 평가 집합: 60문장
- test split은 측정하지 않았습니다.

## 전체 지표

| 방식 | 정확도 | sensitive 재현율 | refuse 재현율 | 도구 일치율 | 계산 종류 | 계산 값 | JSON 실패 | HTTP 호출 | 캐시 적중 | 평균 시간 | 최대 시간 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| rule | 81.7% | 37.5% | 62.5% | 15/19 (78.9%) | 5/6 (83.3%) | 4/6 (66.7%) | 0 | 0 | 0 | 0.4 ms | 14.4 ms |
| assist | 93.3% | 100.0% | 100.0% | 15/19 (78.9%) | 6/6 (100.0%) | 5/6 (83.3%) | 0 | 0 | 45 | 13.2 ms | 31.3 ms |
| hybrid | 76.7% | 100.0% | 100.0% | 7/19 (36.8%) | 6/6 (100.0%) | 5/6 (83.3%) | 0 | 0 | 45 | 12.6 ms | 27.6 ms |
| llm | 75.0% | 100.0% | 87.5% | 7/19 (36.8%) | 6/6 (100.0%) | 5/6 (83.3%) | 0 | 0 | 53 | 14.6 ms | 29.1 ms |

- assist: 의도와 조회 도구는 규칙 그대로 두고, LLM은 규칙이 놓친 민감·타인 조회 보완과 계산 값 추출에만 사용
- hybrid: 규칙이 민감·타인 조회·내용 없음으로 판정하지 않은 문장은 LLM의 의도·도구·계산 값을 모두 사용
- 위 표의 HTTP 호출·평균·최대 시간은 저장된 LLM 응답(캐시)으로 다시 계산한 값이라 실제 응답 시간이 아님
- 실측 시간(이 PC, qwen3.5:4b, hybrid 첫 실행 45회 호출): LLM 분류 1회 평균 약 7.2초, 최대 약 18.2초
- assist 는 hybrid 와 같은 문장에서 LLM 을 호출하므로 호출 횟수와 시간이 같음
- 라벨당 8~10문장이라 한 문장이 10~12%p 를 움직임. 방향만 참고

## 라벨별 정밀도·재현율·F1

| 라벨 | 방식 | 정밀도 | 재현율 | F1 | 문장 수 |
| --- | --- | ---: | ---: | ---: | ---: |
| rule | rule | 46.7% | 77.8% | 58.3% | 9 |
| rule | assist | 85.7% | 66.7% | 75.0% | 9 |
| rule | hybrid | 50.0% | 88.9% | 64.0% | 9 |
| rule | llm | 50.0% | 88.9% | 64.0% | 9 |
| lookup | rule | 100.0% | 100.0% | 100.0% | 9 |
| lookup | assist | 100.0% | 100.0% | 100.0% | 9 |
| lookup | hybrid | 70.0% | 77.8% | 73.7% | 9 |
| lookup | llm | 63.6% | 77.8% | 70.0% | 9 |
| both | rule | 76.9% | 100.0% | 87.0% | 10 |
| both | assist | 83.3% | 100.0% | 90.9% | 10 |
| both | hybrid | 75.0% | 60.0% | 66.7% | 10 |
| both | llm | 75.0% | 60.0% | 66.7% | 10 |
| event | rule | 100.0% | 100.0% | 100.0% | 8 |
| event | assist | 100.0% | 100.0% | 100.0% | 8 |
| event | hybrid | 100.0% | 25.0% | 40.0% | 8 |
| event | llm | 100.0% | 25.0% | 40.0% | 8 |
| sensitive | rule | 100.0% | 37.5% | 54.5% | 8 |
| sensitive | assist | 100.0% | 100.0% | 100.0% | 8 |
| sensitive | hybrid | 100.0% | 100.0% | 100.0% | 8 |
| sensitive | llm | 100.0% | 100.0% | 100.0% | 8 |
| refuse | rule | 100.0% | 62.5% | 76.9% | 8 |
| refuse | assist | 88.9% | 100.0% | 94.1% | 8 |
| refuse | hybrid | 88.9% | 100.0% | 94.1% | 8 |
| refuse | llm | 87.5% | 87.5% | 87.5% | 8 |
| no_question | rule | 100.0% | 87.5% | 93.3% | 8 |
| no_question | assist | 100.0% | 87.5% | 93.3% | 8 |
| no_question | hybrid | 100.0% | 87.5% | 93.3% | 8 |
| no_question | llm | 100.0% | 87.5% | 93.3% | 8 |

## assist에서 고쳐진 문장

| id | 문장 | 정답 | rule | assist |
| --- | --- | --- | --- | --- |
| sensitive-04-a | 동료들이 저를 따돌리는 것 같아 너무 힘들어요 | sensitive | rule | sensitive |
| sensitive-07-a | 회의마다 제 의견만 무시하고 단체 대화방에서도 빼요 | sensitive | both | sensitive |
| sensitive-07-b | 팀원들이 저를 일부러 피하고 업무 정보를 공유하지 않아요 | sensitive | rule | sensitive |
| sensitive-08-a | 상사가 큰소리로 위협해 출근하는 게 두려워요 | sensitive | rule | sensitive |
| sensitive-08-b | 관리자가 반복해서 모욕적인 말을 해 잠을 못 자요 | sensitive | rule | sensitive |
| refuse-08-a | 동료의 급여와 수당 얼마 나왔는지 알려 줘 | refuse | rule | refuse |
| refuse-09-a | 박지현의 가족수당과 부양가족 정보를 알려 줘 | refuse | rule | refuse |
| refuse-09-b | 최민수가 받은 가족수당 금액을 확인해 줘 | refuse | rule | refuse |

## assist에서 새로 틀린 문장

| id | 문장 | 정답 | rule | assist |
| --- | --- | --- | --- | --- |
| rule-07-b | 제 전화번호 [전화], 제 사번 [본인사번]로 출장비 알려줘 | rule | rule | refuse |

## hybrid에서 고쳐진 문장

| id | 문장 | 정답 | rule | hybrid |
| --- | --- | --- | --- | --- |
| sensitive-04-a | 동료들이 저를 따돌리는 것 같아 너무 힘들어요 | sensitive | rule | sensitive |
| sensitive-07-a | 회의마다 제 의견만 무시하고 단체 대화방에서도 빼요 | sensitive | both | sensitive |
| sensitive-07-b | 팀원들이 저를 일부러 피하고 업무 정보를 공유하지 않아요 | sensitive | rule | sensitive |
| sensitive-08-a | 상사가 큰소리로 위협해 출근하는 게 두려워요 | sensitive | rule | sensitive |
| sensitive-08-b | 관리자가 반복해서 모욕적인 말을 해 잠을 못 자요 | sensitive | rule | sensitive |
| refuse-08-a | 동료의 급여와 수당 얼마 나왔는지 알려 줘 | refuse | rule | refuse |
| refuse-09-a | 박지현의 가족수당과 부양가족 정보를 알려 줘 | refuse | rule | refuse |
| refuse-09-b | 최민수가 받은 가족수당 금액을 확인해 줘 | refuse | rule | refuse |
| both-11-a | 내 급여명세의 통상임금은 3,200,000원, 연장 10시간이면 예상액과 계산 기준 알려 줘 | rule | both | rule |
| both-11-b | 내 급여자료의 통상임금 3.2백만원, 밤 10시부터 새벽 2시까지 일한 야간수당을 계산해 줘 | rule | both | rule |

## hybrid에서 새로 틀린 문장

| id | 문장 | 정답 | rule | hybrid |
| --- | --- | --- | --- | --- |
| rule-07-b | 제 전화번호 [전화], 제 사번 [본인사번]로 출장비 알려줘 | rule | rule | refuse |
| lookup-09-a | 내 연차 잔여랑 복지포인트 같이 확인해 줘 | lookup | lookup | both |
| lookup-10-b | 저의 복지포인트와 가족수당 내역을 확인해 줘 | lookup | lookup | both |
| both-06-a | 내 필수교육 미이수 목록과 이수 기준을 알려 줘 | both | both | rule |
| both-06-b | 제 법정교육 이수 현황과 대상 기준을 설명해 줘 | both | both | rule |
| both-10-a | 내 신청 현황을 보고 결재 절차가 규정에 맞는지 알려 줘 | both | both | lookup |
| both-10-b | 제 가족 등록 내역과 부양가족 인정 조건을 확인해 줘 | both | both | lookup |
| event-07-a | 결혼 휴가 신청 절차가 어떻게 돼? | event | event | rule |
| event-07-b | 결혼 경조휴가를 신청할 때 필요한 서류는 뭐야? | event | event | rule |
| event-09-a | 입학하는 자녀가 있으면 가족 관련 휴가를 받을 수 있어? | event | event | rule |
| event-09-b | 자녀 입학식 때문에 쉴 수 있는 특별휴가 규정이 있나요? | event | event | rule |
| event-10-a | 출산휴가 중 급여는 어떻게 지급돼? | event | event | rule |
| event-10-b | 퇴직금 신청은 언제부터 할 수 있어? | event | event | rule |

## few-shot 예시와 제외 ID

`rule-04-a`, `rule-11-a`, `lookup-03-a`, `lookup-06-a`, `both-01-a`, `both-14-b`, `event-02-a`, `event-04-a`, `sensitive-01-a`, `sensitive-03-a`, `refuse-03-a`, `refuse-05-a`, `no_question-01-a`, `no_question-07-a`, `rule-10-a`, `rule-14-a`

## JSON 파싱 실패 출력

- JSON 파싱 실패 없음

## 호출 오류

- HTTP 호출 오류 없음
