import json
from pathlib import Path

from . import conf_build, mask_rules, ner_ext, ner_model, ner_rules_field, ner_rules_id

HERE = Path(__file__).parent
FORM_FIELDS = (ner_rules_field.NAME_FIELDS - {"참석"}) | ner_rules_field.ID_FIELDS  # "참석"은 직함과 이름이 섞인 문장이라 모델에 맡김


def _field_key(article):
    return article.split(" ", 1)[-1] if article.startswith("징계대상자") else article


def is_form_field(chunk, field="text"):
    return field == "speaker_label" or _field_key(chunk.get("article", "")) in FORM_FIELDS


def detect_text(text, chunk, field, tok, model, counts):
    """한 칸(본문 또는 발언자 표시)의 탐지 결과"""
    if is_form_field(chunk, field):
        ps_id = ner_rules_id.predict(text, chunk.get("article", ""), field)
        return dict(entities=ps_id, og=[], pos=[], sen=None, qi=[])
    ps_og = ner_model.predict_text(tok, model, text, keep=("PS", "OG"))
    ps = [x for x in ps_og if x[0] == "PS"]
    ids = [x for x in ner_rules_id.predict(text, chunk.get("article", ""), field) if x[0] == "ID"]
    ext = ner_ext.detect(text, [x for x in ps_og if x[0] == "OG"], counts)
    return dict(entities=ps + ids, og=ext["og"], pos=ext["pos"], sen=ext["sen"], qi=ext["qi"])


def mask_chunk(chunk, tok, model, counts):
    """조각 하나 → 찾은 구간·가린 사본·민감정보·간접 식별"""
    d = detect_text(chunk["text"], chunk, "text", tok, model, counts)
    if d["sen"]:
        masked = "[민감정보]"
    elif mask_rules.is_subject_department(chunk):
        masked = "[부서]"
    else:
        masked = ner_ext.mask_all(chunk["text"], d["entities"], d["qi"])
    out = dict(chunk_id=chunk["chunk_id"], entities=[list(x) for x in d["entities"]],
               og=[list(x) for x in d["og"]], pos=[list(x) for x in d["pos"]], sen=d["sen"],
               qi=[[o[3], p[3], n, hide] for o, p, n, hide in d["qi"]],
               masked=masked)
    if chunk.get("speaker_label"):
        s = detect_text(chunk["speaker_label"], chunk, "speaker_label", tok, model, counts)
        out["speaker_masked"] = ner_ext.mask_all(chunk["speaker_label"], s["entities"])
        out["speaker_entities"] = [list(x) for x in s["entities"]]
    return out


def main(src=None, dst=None):
    src = src or conf_build.WORK_DIR / "chunks.jsonl"
    dst = dst or conf_build.WORK_DIR / "masked_chunks.jsonl"
    tok, model = ner_model.load()
    counts = ner_ext.load_counts()
    if not counts:
        print("주의: rules/org_counts.csv 가 없어 간접 식별 조합은 모두 가림 (기본 차단)")
    chunks = [json.loads(l) for l in open(src, encoding="utf-8")]
    n_ent = n_qi = n_sen = 0
    with open(dst, "w", encoding="utf-8") as f:
        for c in chunks:
            r = mask_chunk(c, tok, model, counts)
            n_ent += len(r["entities"]) + len(r.get("speaker_entities", []))
            n_qi += sum(1 for q in r["qi"] if q[3])
            n_sen += bool(r["sen"])
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"조각 {len(chunks)}개 → 이름·식별정보 {n_ent}개, 가린 간접 식별 조합 {n_qi}개, 민감정보 문장 {n_sen}개 → {dst}")


if __name__ == "__main__":
    main()
