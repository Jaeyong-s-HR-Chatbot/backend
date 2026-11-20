import os
from pathlib import Path

import torch
from transformers import AutoModelForTokenClassification, AutoTokenizer

DEFAULT_MODEL = "soddokayo/klue-roberta-base-ner"
PUBLIC_MODEL = os.environ.get("NER_MODEL_DIR") or DEFAULT_MODEL
MAX_LEN = 256


def load(name_or_dir=PUBLIC_MODEL):
    work_dir = Path(os.environ.get("CONF_WORK_DIR") or Path(__file__).parent.parent / "data" / "conf")
    cache_dir = work_dir / "models"
    local_model = cache_dir / "klue-ner"
    if name_or_dir == DEFAULT_MODEL and (local_model / "config.json").is_file() and (local_model / "pytorch_model.bin").is_file():
        name_or_dir = str(local_model)
    tok = AutoTokenizer.from_pretrained(name_or_dir, cache_dir=cache_dir)
    model = AutoModelForTokenClassification.from_pretrained(name_or_dir, cache_dir=cache_dir)
    model.eval()
    if torch.cuda.is_available():
        model.cuda()
    return tok, model


def predict_text(tok, model, text, keep=("PS",)):
    """글자 → [(type, start, end, 글자)]  (keep 종류만)"""
    if not text.strip():
        return []
    enc = tok(text, return_offsets_mapping=True, truncation=False, return_tensors="pt")
    if enc["input_ids"].shape[1] > MAX_LEN:
        raise ValueError("NER 입력이 최대 토큰 수를 초과해 적재를 중단합니다")
    offsets = enc.pop("offset_mapping")[0].tolist()
    enc = {k: v.to(model.device) for k, v in enc.items()}
    with torch.no_grad():
        ids = model(**enc).logits[0].argmax(-1).tolist()
    id2label = model.config.id2label
    spans, cur = [], None
    for (s, e), i in zip(offsets, ids):
        if s == e:  # 특수 토큰
            continue
        lab = id2label[i]
        tag, typ = (lab.split("-", 1) + [""])[:2] if lab != "O" else ("O", "")
        if tag == "B" or (tag == "I" and (cur is None or cur[0] != typ)):
            if cur:
                spans.append(cur)
            cur = [typ, s, e]
        elif tag == "I" and cur and cur[0] == typ:
            cur[2] = e
        else:
            if cur:
                spans.append(cur)
            cur = None
    if cur:
        spans.append(cur)
    out = []
    for typ, s, e in spans:
        while s < e and text[s].isspace():
            s += 1
        while e > s and text[e - 1].isspace():
            e -= 1
        if typ in keep and e > s:
            out.append((typ, s, e, text[s:e]))
    return out
