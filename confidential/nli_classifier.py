"""NLI rules copied from nli_finetune.ipynb cells 6, 10, and 11."""
import logging
import re
import threading
from pathlib import Path

from app import config

log = logging.getLogger(__name__)

THRESHOLD = 0.99
SHORT = 25
GUARD = 0.5
# 판정 보완 (재학습 없음, v4 개발용으로 조정 · v5 로 확인)
# - 이어받기: 앞 발언이 기밀이고 지금 발언이 "저도·나도·저 역시"로 시작하는 짧은 동조 발언이면 앞 판정을 이어받음
# - 문장 단위: 기밀이 아니라고 나온 여러 문장 발언은 문장별로 다시 판정해 하나라도 기밀이면 기밀
INHERIT_MAX = 40
INHERIT_HEAD = re.compile(r"^\s*(저도|나도|저 역시)")
# - 이어지는 답변: 앞 1~2개 발언 중 기밀이 있고, 지금 발언이 "그건·그분·그리고·네,·저도…"로 이어지며
#   질문이 아니고 진행 발언 점수도 낮으면 앞 판정을 이어받음 (예: "탈퇴가 아니라 다시 들어왔어요")
THREAD_HEAD = re.compile(r"^\s*(?:그건|그거|그게|그분|그 사람|그 친구|그리고|네[,.]|예[,.]|저도|나도|맞아요|맞습니다|아니요|아뇨)")
THREAD_WINDOW = 2
THREAD_MAX = 60
THREAD_PROGRESS_MAX = 0.5
SENTENCE_SPLIT = re.compile(r"(?<=[.?!…])\s+")
BATCH_SIZE = 64

# Cell 11: development-set-selected hypotheses, three for each type.
# 2026-11-28 재학습(ft_model_1128b)에서 개발셋으로 다시 고른 기준 문장과 기준값(0.99)입니다. 모델과 함께 바꿔야 합니다.
HYPOTHESES = {
    "C1": [
        "누군가 질병을 앓고 있다",
        "화자의 가족이 아프다",
        "누군가 가족을 돌봐야 하는 상황이다",
    ],
    "C2": [
        "누군가 상사에게 미리 연락을 했다",
        "누군가 휴가를 길게 쓰기 어려웠다",
        "누군가 자리를 비울 수밖에 없었다",
    ],
    "C3": [
        "특정 직원이 당직을 여러 번 섰다",
        "누군가 표창을 받은 적이 있다",
        "누군가 지각한 적이 있다",
    ],
    "C4": [
        "누군가 견책이 적절하다고 생각한다",
        "누군가 더 가벼운 징계를 원한다",
        "위원 중 한 명이 다른 의견을 냈다",
    ],
    "C5": [
        "예산이 아직 확정되지 않았다",
        "회사가 어떤 요구를 받아들이기 어렵다고 본다",
        "회사가 받아들일 수 있는 인상률의 한도가 있다",
    ],
    "C6": [
        "특정 팀의 인력이 부족하다",
        "업무가 특정 팀에 몰려 있다",
        "특정 팀에 결원이 있다",
    ],
    "C7": [
        "누군가 노동조합에서 역할을 맡고 있다",
        "누군가 노동조합을 탈퇴했다",
        "누군가 노동조합에 가입했다",
    ],
}

# Cell 6: all candidate type hypotheses used by the previous-utterance guard.
GUARD_HYPOTHESES = {
    "C1": ["누군가 건강이 좋지 않다", "화자의 가족이 아프다", "누군가 가족을 돌봐야 하는 상황이다",
           "누군가 질병을 앓고 있다", "누군가의 가족이 정기적으로 치료를 받는다"],
    "C2": ["누군가 상사에게 미리 연락을 했다", "누군가의 팀에 일손이 부족했다",
           "누군가에게 집안 사정이 있었다", "누군가 자리를 비울 수밖에 없었다",
           "누군가 휴가를 길게 쓰기 어려웠다"],
    "C3": ["누군가 과거에 경고나 징계를 받았다", "누군가 표창을 받은 적이 있다",
           "누군가 이전에도 비슷한 문제를 일으켰다", "특정 직원이 당직을 여러 번 섰다",
           "누군가 지각한 적이 있다"],
    "C4": ["누군가 감봉이 적절하다고 생각한다", "누군가 견책이 적절하다고 생각한다",
           "누군가 더 가벼운 징계를 원한다", "누군가 더 무거운 징계를 원한다",
           "위원 중 한 명이 다른 의견을 냈다", "찬성한 사람이 네 명이다"],
    "C5": ["회사가 받아들일 수 있는 인상률의 한도가 있다", "추가 혜택을 주면 합의할 수 있다",
           "예산이 아직 확정되지 않았다", "회사가 어떤 요구를 받아들이기 어렵다고 본다",
           "인건비 한도를 넘을 수 있다"],
    "C6": ["특정 팀에 결원이 있다", "특정 팀의 인력이 부족하다", "업무가 특정 팀에 몰려 있다",
           "한 사람이 빠지면 업무가 멈춘다"],
    "C7": ["누군가 노동조합에 가입했다", "누군가 노동조합을 탈퇴했다",
           "누군가 노동조합에서 역할을 맡고 있다", "화자가 노조 활동을 하고 있다"],
}

# Cell 6: progression comparison hypotheses.
PROGRESSION = [
    "화자가 회의를 시작하거나 마친다",
    "화자가 다음 안건을 소개한다",
    "화자가 합의나 의결 결과를 발표한다",
    "화자가 수고했다고 인사한다",
    "화자가 다음 순서를 안내한다",
    "화자가 공식적인 요구안을 말한다",
]

# Cell 10: remove the same fillers and normalize whitespace before inference.
_FILLER = re.compile(r"(?:(?<=^)|(?<=[\s,.…]))(?:음|어|그|아)(?:…|\.\.\.|,)+\s*")
_COLUMNS = [(kind, hypothesis) for kind, values in HYPOTHESES.items() for hypothesis in values]
_COLUMNS.extend(("PROC", hypothesis) for hypothesis in PROGRESSION)
_GUARD_COLUMNS = [(kind, hypothesis) for kind, values in GUARD_HYPOTHESES.items() for hypothesis in values]
_TYPE_COLUMN_COUNT = sum(len(values) for values in HYPOTHESES.values())

_MODEL = None
_MODEL_LOCK = threading.Lock()


def _clean(text):
    return re.sub(r"\s+", " ", _FILLER.sub(" ", text)).strip()


def _load_model():
    if not config.NLI_MODEL_DIR:
        raise RuntimeError("NLI_MODEL_DIR이 설정되지 않았습니다")
    model_dir = Path(config.NLI_MODEL_DIR).expanduser()
    if not model_dir.is_dir():
        raise FileNotFoundError("NLI 모델 디렉터리를 찾을 수 없습니다")

    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True)
    model = AutoModelForSequenceClassification.from_pretrained(
        str(model_dir), local_files_only=True,
    )
    labels = model.config.id2label
    entailment_ids = [
        int(index) for index, label in labels.items()
        if "entailment" in str(label).lower()
    ]
    if len(entailment_ids) != 1:
        raise ValueError("NLI 모델에서 entailment 라벨을 하나로 확인할 수 없습니다")
    model.to(torch.device("cpu"))
    model.eval()
    return tokenizer, model, entailment_ids[0], int(getattr(model.config, "type_vocab_size", 2)), torch


def _get_model():
    global _MODEL
    if _MODEL is None:
        with _MODEL_LOCK:
            if _MODEL is None:
                _MODEL = _load_model()
    return _MODEL


def _reset_model():
    global _MODEL
    with _MODEL_LOCK:
        _MODEL = None


def _entailment_scores(premises, hypotheses):
    tokenizer, model, entailment_id, type_vocab_size, torch = _get_model()
    scores = []
    for start in range(0, len(premises), BATCH_SIZE):
        end = start + BATCH_SIZE
        encoded = tokenizer(
            premises[start:end], hypotheses[start:end],
            return_tensors="pt", truncation=True, max_length=320, padding=True,
        )
        if type_vocab_size < 2:
            encoded.pop("token_type_ids", None)
        encoded = {key: value.to(torch.device("cpu")) for key, value in encoded.items()}
        with torch.no_grad():
            logits = model(**encoded).logits
            batch_scores = torch.softmax(logits, dim=-1)[:, entailment_id].float().cpu().tolist()
        scores.extend(float(value) for value in batch_scores)
    if len(scores) != len(premises):
        raise ValueError("NLI 추론 결과 개수가 맞지 않습니다")
    if any(not 0 <= score <= 1 for score in scores):
        raise ValueError("NLI 점수가 확률 범위를 벗어났습니다")
    return scores


def _matrix(texts, columns=None):
    if not texts:
        return []
    columns = _COLUMNS if columns is None else columns
    premises = [text for text in texts for _ in columns]
    hypotheses = [hypothesis for _ in texts for _, hypothesis in columns]
    flat = _entailment_scores(premises, hypotheses)
    width = len(columns)
    return [flat[index:index + width] for index in range(0, len(flat), width)]


def _type_scores(row):
    result = {}
    offset = 0
    for kind, hypotheses in HYPOTHESES.items():
        result[kind] = max(row[offset:offset + len(hypotheses)])
        offset += len(hypotheses)
    return result


def classify_utterances(texts, previous_texts=None):
    """Classify original speaker utterances using the notebook's B+P0 rule."""
    if previous_texts is None:
        previous_texts = [""] * len(texts)
    if len(texts) != len(previous_texts):
        raise ValueError("이전 발언 개수가 맞지 않습니다")
    if not texts:
        return []

    current = [_clean(text) for text in texts]
    previous = [_clean(text) for text in previous_texts]
    matrix = _matrix(current)
    context_indices = [
        index for index, text in enumerate(current)
        if previous[index] and len(text) < SHORT
    ]
    if context_indices:
        prior_matrix = _matrix([previous[index] for index in context_indices], _GUARD_COLUMNS)
        eligible = [
            index for index, row in zip(context_indices, prior_matrix)
            if max(row) < GUARD
        ]
        if eligible:
            combined = _matrix([previous[index] + " " + current[index] for index in eligible])
            for index, row in zip(eligible, combined):
                matrix[index] = row

    results = [_decide(row) for row in matrix]

    # 문장 단위: 공개 문장과 기밀 문장이 섞인 발언 (예: 합의 내용 + 위원별 반대 의견)
    sentences = [
        (index, part)
        for index, result in enumerate(results) if not result["confidential"]
        for parts in [SENTENCE_SPLIT.split(texts[index].strip())] if len(parts) >= 2
        for part in parts if len(part.strip()) >= 4
    ]
    if sentences:
        for (index, _), row in zip(sentences, _matrix([_clean(part) for _, part in sentences])):
            found = _decide(row)
            if found["confidential"] and not results[index]["confidential"]:
                results[index] = dict(found, rule="sentence")

    # 이어받기: "저도 1개월 쪽입니다." 처럼 앞 기밀 발언에 동조하는 짧은 발언
    for index in range(1, len(results)):
        text = current[index]
        if (not results[index]["confidential"] and results[index - 1]["confidential"]
                and len(text) <= INHERIT_MAX and INHERIT_HEAD.match(text) and not text.endswith("?")):
            results[index] = dict(results[index], confidential=True,
                                  type=results[index - 1]["type"], rule="inherit")

    # 이어지는 답변: 앞 기밀 발언에 대한 답이나 덧붙임 (진행 발언은 진행 점수로 거름)
    before = list(results)
    for index, text in enumerate(current):
        if (before[index]["confidential"] or text.endswith("?") or len(text) > THREAD_MAX
                or not THREAD_HEAD.match(text) or before[index]["progress_score"] >= THREAD_PROGRESS_MAX):
            continue
        prior = [j for j in range(max(0, index - THREAD_WINDOW), index) if before[j]["confidential"]]
        if prior:
            results[index] = dict(results[index], confidential=True,
                                  type=before[prior[-1]]["type"], rule="thread")
    return results


def _decide(row):
    per_type = _type_scores(row)
    kind = max(per_type, key=per_type.get)
    score = per_type[kind]
    progress_score = max(row[_TYPE_COLUMN_COUNT:])
    return {
        "confidential": score >= THRESHOLD and score > progress_score,
        "score": score,
        "type": kind,
        "progress_score": progress_score,
    }
