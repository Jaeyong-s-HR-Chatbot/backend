import json
import logging
import os
import re
from collections import Counter
from pathlib import Path

import pdfplumber
import yaml

logging.getLogger("pdfminer").setLevel(logging.ERROR)

HERE = Path(__file__).parent
WORK_DIR = Path(os.environ.get("CONF_WORK_DIR") or HERE.parent / "data" / "conf")
RAW = Path(os.environ.get("CONF_PDF_DIR") or WORK_DIR / "raw")
OUT = WORK_DIR
with (HERE / "rules" / "confidential.yaml").open(encoding="utf-8") as f:
    CONF = yaml.safe_load(f)
COMPANY = "nuri"
NOTE_LINE = re.compile(r"^※")
FORM_FOOT = re.compile(r"^\d+㎜×\d+㎜")
FORM_HEAD = re.compile(r"^(\[별지[^\]]*\]\s*)?(\((앞|뒤)쪽\))?$")  # 양식 머리표시: "[별지 제3호서식] (앞쪽)", "(뒤쪽)"
SPEAKER_HEAD = re.compile(
    r"^(위원장|외부위원|위원|간사|징계대상자|사용자대표|근로자대표|사용자위원|근로자위원)"
    r"\s+([가-힣]{2,4})\s+(.+)$"
)


def norm(s):
    return re.sub(r"\s+", "", s or "")


# ------------------------------------------------------------------ 글자 뽑기 (rag_build.py 와 같은 방식)
def pdf_pages(path):
    with pdfplumber.open(path) as pdf:
        return [(i + 1, page.extract_text() or "") for i, page in enumerate(pdf.pages)]


def pdf_tables_and_rest(path):
    """쪽마다 (표 목록, 표 밖 글자) 반환"""
    out = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages):
            tables = page.find_tables()
            boxes = [t.bbox for t in tables]

            def outside(obj, boxes=boxes):
                x = (obj["x0"] + obj["x1"]) / 2
                y = (obj["top"] + obj["bottom"]) / 2
                return not any(b[0] <= x <= b[2] and b[1] <= y <= b[3] for b in boxes)

            rest = page.filter(outside).extract_text() or ""
            out.append((i + 1, [t.extract() for t in tables], rest))
    return out


# ------------------------------------------------------------------ 녹취록: 발언 단위
def split_transcript(doc, pdf_dir=None):
    speakers = {}
    units, cur, meta_lines = [], None, []
    tables = pdf_tables_and_rest((pdf_dir or RAW) / doc["file"])
    for page, tbls, rest in tables:
        for t in tbls:  # 맨 위 회의 정보 표
            for row in t:
                cells = [c for c in row if c]
                if len(cells) >= 2:
                    meta_lines.append((page, norm(cells[0]), " ".join(c.replace("\n", " ") for c in cells[1:])))
        for ln in rest.split("\n"):
            s = ln.strip()
            if not s or NOTE_LINE.match(s) or s == doc["name"]:
                continue
            m = SPEAKER_HEAD.match(s)
            if m:
                head = f"{m.group(1)} {m.group(2)}"
                if head not in speakers:
                    speakers[head] = f"S{len(speakers) + 1}"
                cur = dict(page=page, head=head, text=m.group(3))
                units.append(cur)
            elif cur:
                cur["text"] += (" " if not cur["text"].endswith(("…", " ")) else "") + s
    chunks = []
    for page, label, val in meta_lines:
        chunks.append(dict(article=label, chunk_type="회의정보", text=val, page=page))
    for n, u in enumerate(units, 1):
        role, _, _name = u["head"].partition(" ")
        chunks.append(dict(article=f"발언 {n}", chunk_type="발언", text=u["text"], page=u["page"],
                           speaker=speakers[u["head"]], speaker_label=u["head"], speaker_role=role))
    return chunks


# ------------------------------------------------------------------ 회의록: 양식 칸 단위
def split_minutes(doc, pdf_dir=None, config=None):
    form = (config or CONF)["forms"][doc["form"]]
    labels = {norm(l) for l in form["labels"]}
    parent = {norm(k): [norm(c) for c in v] for k, v in form.get("parent", {}).items()}
    child_of = {c: p for p, cs in parent.items() for c in cs}
    chunks, rest_lines = [], []
    for page, tbls, rest in pdf_tables_and_rest((pdf_dir or RAW) / doc["file"]):
        for t in tbls:
            flat = [norm(c) for r in t for c in r if c]
            if "참석위원" in flat:  # 노사 회의록 뒤쪽: 참석위원 표
                side = {}
                for r in t:
                    cells = [c or "" for c in r]
                    if norm(cells[0]) == "근로자위원":
                        continue
                    if norm(cells[0]) in ("성명", "참석위원"):
                        continue
                    if len(cells) >= 3:
                        side.setdefault("근로자위원", []).append(cells[0].strip())
                        side.setdefault("사용자위원", []).append(cells[2].strip())
                for k, v in side.items():
                    chunks.append(dict(article=k, chunk_type="항목", text=", ".join(x for x in v if x), page=page))
                continue
            for r in t:
                cells = [c for c in r if c and c.strip()]
                row_labels = {norm(c) for c in cells}
                i = 0
                while i < len(cells):
                    lab = norm(cells[i])
                    if lab in labels and lab not in parent:
                        val = cells[i + 1] if i + 1 < len(cells) and norm(cells[i + 1]) not in labels else ""
                        name = (child_of[lab] + " " if lab in child_of else "") + lab
                        if lab == "출석위원" and "총위원" in row_labels:  # 같은 줄에 총 위원이 있으면 인원수 칸
                            name = "출석위원수"
                        chunks.append(dict(article=name, chunk_type="항목", text=val.replace("\n", " ").strip(), page=page))
                        i += 2 if val else 1
                    elif lab in parent:
                        i += 1
                    elif i == 0 and len(cells) == 1:  # 표 안 제목 줄
                        chunks.append(dict(article="제목", chunk_type="제목", text=cells[0].strip(), page=page))
                        i += 1
                    else:
                        i += 1
        for ln in rest.split("\n"):
            s = ln.strip()
            if s and not FORM_FOOT.match(s) and not FORM_HEAD.match(s):
                rest_lines.append((page, s))
    # 표 밖 글자: 제목, 개회 문구, 맺음 문구, 날짜, 서명
    for page, s in rest_lines:
        if s == doc["name"]:
            chunks.insert(0, dict(article="제목", chunk_type="제목", text=s, page=page))
        elif s.startswith(("위원 :", "위원장 :")):
            for part in re.findall(r"(위원장|위원)\s*:\s*(\S+)", s):
                chunks.append(dict(article="서명 " + part[0], chunk_type="서명", text=part[1], page=page))
        else:
            chunks.append(dict(article="본문", chunk_type="본문", text=s, page=page))
    return chunks


# ------------------------------------------------------------------ 공통 칸 붙이기
def build_doc(doc, pdf_dir=None, config=None):
    raw = split_transcript(doc, pdf_dir) if doc["form"] == "transcript" else split_minutes(doc, pdf_dir, config)
    out = []
    for seq, c in enumerate(raw, 1):
        if not c["text"]:
            continue
        out.append(dict(
            chunk_id=f"{doc['id']}-{seq:04d}", document_id=doc["id"], company_id=COMPANY, doc_kind=doc["doc_kind"],
            doc_name=doc["name"], article=c["article"], article_title=c.get("speaker_role", ""), part=1,
            text=c["text"], effective_date=str(doc["date"]), page=c["page"], source_file=doc["file"],
            chunk_type=c["chunk_type"], categories=[doc["category"]], searchable=False,
            security_level=doc["level"], allowed_roles=doc.get("allowed_roles", []),
            subject_emp_id=doc.get("subject"), participants=doc.get("participants", []),
            speaker=c.get("speaker"), speaker_label=c.get("speaker_label")))
    return out


def main():
    all_chunks, docs = [], []
    for doc in CONF["documents"]:
        ch = build_doc(doc)
        all_chunks += ch
        docs.append(dict(document_id=doc["id"], doc_name=doc["name"], doc_kind=doc["doc_kind"], level=doc["level"],
                         chunks=len(ch), by_type=dict(Counter(c["chunk_type"] for c in ch)), source_file=doc["file"]))
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "chunks.jsonl", "w", encoding="utf-8") as f:
        for c in all_chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    json.dump(docs, open(OUT / "documents.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"{'문서':<34}{'등급':>4}{'조각':>6}  종류")
    for d in docs:
        print(f"{d['doc_name']:<34}{d['level']:>4}{d['chunks']:>6}  {d['by_type']}")
    print(f"\n조각 {len(all_chunks)}개 (모두 searchable=false, 마스킹 전 원문)")


if __name__ == "__main__":
    main()
