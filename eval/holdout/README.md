# 처음 보는 질문 (holdout)

분류 규칙을 만들 때 쓰지 않은 질문으로 실제 성능을 재는 평가셋이에요. 2026-11-23에 GPT로 만들었어요
(프롬프트: 저장소 밖 `질문셋/gpt_처음보는질문_프롬프트.md`). 정답은 GPT가 단 것이라 검수 전이에요.

| 파일 | 문항 | 형식 |
| --- | ---: | --- |
| `holdout_employee.jsonl` | 105 (의도 7종 × 15) | `{"id","question","intent","tools","calc","note"}` |
| `holdout_admin.jsonl` | 90 (both 45 · rule 20 · out_of_scope 10 · refuse 10 · lookup 5) | `{"id","question","intent","tool","params","note"}` |

- 기준일(오늘)은 2026-11-09예요. 상대 날짜 정답은 이 날짜 기준이에요.
- 기존 평가셋과 거의 같은 문장 3개는 채점에서 빼요: `H-E-016`, `H-E-096`, `H-A-001`.
- 라벨 확인이 필요한 문항: `H-E-078`, `H-E-082`, `H-E-088`. 로그인 사번과 다른 사번을 말하는 문장이라 챗봇은 refuse로 봐요.

```
python scripts/eval_intent.py --cases eval/holdout/holdout_employee.jsonl --exclude H-E-016,H-E-096 [--llm]
python scripts/eval_admin_intent.py --cases eval/holdout/holdout_admin.jsonl --exclude H-A-001 [--llm]
```

이 질문을 보고 규칙을 고치면 더는 "처음 보는 질문"이 아니에요. 규칙을 고친 뒤에는 새 질문셋을 다시 만들어 재세요.
