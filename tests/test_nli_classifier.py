from types import SimpleNamespace

import pytest
import torch

from confidential import nli_classifier


def _row(kind_score=0.0, progress_score=0.0):
    row = [0.0] * len(nli_classifier._COLUMNS)
    row[0] = kind_score
    row[nli_classifier._TYPE_COLUMN_COUNT] = progress_score
    return row


def test_b_plus_p0_requires_threshold_and_beats_progress(monkeypatch):
    monkeypatch.setattr(nli_classifier, "_matrix", lambda texts: [_row(0.98, 0.97) for _ in texts])
    result = nli_classifier.classify_utterances(["가상 발언"])[0]
    assert result == {
        "confidential": True, "score": 0.98, "type": "C1", "progress_score": 0.97,
    }

    monkeypatch.setattr(nli_classifier, "_matrix", lambda texts: [_row(0.98, 0.98) for _ in texts])
    assert not nli_classifier.classify_utterances(["가상 발언"])[0]["confidential"]
    monkeypatch.setattr(nli_classifier, "_matrix", lambda texts: [_row(0.979, 0.1) for _ in texts])
    assert not nli_classifier.classify_utterances(["가상 발언"])[0]["confidential"]


def test_short_utterance_context_matches_notebook_guard(monkeypatch):
    calls = []
    long_text = "다음 긴 발언입니다. " * 3

    def matrix(texts, columns=None):
        calls.append(list(texts))
        rows = []
        for text in texts:
            if text == "앞 발언":
                rows.append(_row(0.49))
            elif text == "앞 발언 짧은 말":
                rows.append(_row(0.99, 0.1))
            elif text == "민감한 앞 발언":
                rows.append(_row(0.5))
            else:
                rows.append(_row(0.99, 0.1))
        return rows

    monkeypatch.setattr(nli_classifier, "_matrix", matrix)
    results = nli_classifier.classify_utterances(
        ["짧은 말", long_text],
        ["앞 발언", "민감한 앞 발언"],
    )
    assert results[0]["score"] == 0.99
    assert results[1]["score"] == 0.99
    assert calls == [
        ["짧은 말", long_text.strip()],
        ["앞 발언"],
        ["앞 발언 짧은 말"],
    ]


@pytest.mark.parametrize(("guard_score", "use_context"), [(0.5, False), (0.499, True)])
def test_context_guard_checks_unselected_candidates(monkeypatch, guard_score, use_context):
    guard_columns = []
    calls = []

    def matrix(texts, columns=None):
        calls.append(list(texts))
        if columns is not None:
            guard_columns.extend(columns)
            row = [0.0] * len(columns)
            unselected = next(
                i for i, (_, hypothesis) in enumerate(columns)
                if hypothesis not in {h for values in nli_classifier.HYPOTHESES.values() for h in values}
            )
            row[unselected] = guard_score
            return [row]
        return [_row(0.99 if text.startswith("앞 발언 ") else 0.1) for text in texts]

    monkeypatch.setattr(nli_classifier, "_matrix", matrix)
    result = nli_classifier.classify_utterances(["짧은 말"], ["앞 발언"])[0]
    assert len(guard_columns) == 34
    assert result["confidential"] is use_context
    assert (["앞 발언 짧은 말"] in calls) is use_context


def test_whitespace_and_fillers_are_cleaned_before_length_and_scoring(monkeypatch):
    seen = []

    def matrix(texts):
        seen.append(list(texts))
        return [_row() for _ in texts]

    monkeypatch.setattr(nli_classifier, "_matrix", matrix)
    nli_classifier.classify_utterances(["음,   다음 안건입니다."])
    assert seen == [["다음 안건입니다."]]


def test_model_is_loaded_once(monkeypatch):
    nli_classifier._reset_model()
    calls = []
    model = object()
    monkeypatch.setattr(nli_classifier, "_load_model", lambda: calls.append(1) or model)
    try:
        assert nli_classifier._get_model() is model
        assert nli_classifier._get_model() is model
        assert calls == [1]
    finally:
        nli_classifier._reset_model()


def test_token_type_ids_are_omitted_when_model_has_no_segment_embeddings(monkeypatch):
    class Tokenizer:
        def __call__(self, _premises, _hypotheses, **_kwargs):
            return {
                "input_ids": torch.tensor([[1, 2]]),
                "token_type_ids": torch.tensor([[0, 0]]),
            }

    class Model:
        def __init__(self):
            self.received = None

        def __call__(self, **inputs):
            self.received = inputs
            assert not torch.is_grad_enabled()
            return SimpleNamespace(logits=torch.tensor([[0.0, 2.0, 0.0]]))

    model = Model()
    monkeypatch.setattr(
        nli_classifier, "_MODEL", (Tokenizer(), model, 1, 0, torch),
    )
    try:
        scores = nli_classifier._entailment_scores(["발언"], ["기준"])
        assert "token_type_ids" not in model.received
        assert scores[0] > 0.7
    finally:
        nli_classifier._reset_model()
