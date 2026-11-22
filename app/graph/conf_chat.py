"""권한을 확인한 기밀문서 근거로 답변합니다."""
import logging
from urllib.parse import urlsplit

import httpx
import psycopg

from .. import config
from . import conf_retriever, nodes

log = logging.getLogger("hr_chat")
NO_ACCESS = ("요청하신 회의 내용은 열람 권한이 있는 담당자만 확인할 수 있어요. "
             "절차나 규정은 물어보시면 안내해 드릴게요.")
NO_EVIDENCE = "열람 가능한 회의 내용에서 질문에 맞는 근거를 찾지 못했어요."
ERROR = "기밀문서 검색을 처리하지 못했어요. 잠시 뒤 다시 시도해 주세요."


def _hide(text, hidden):
    for value, replacement in sorted(hidden, key=lambda pair: len(pair[0]), reverse=True):
        if value:
            text = text.replace(value, replacement)
    return text


def _hide_values(value, hidden):
    if isinstance(value, str):
        return _hide(value, hidden)
    if isinstance(value, dict):
        return {_hide_values(key, hidden): _hide_values(item, hidden) for key, item in value.items()}
    if isinstance(value, list):
        return [_hide_values(item, hidden) for item in value]
    return value


def _local_llm():
    if config.USE_FAKE_LLM:
        return True
    base = urlsplit(config.LLM_BASE_URL)
    return base.scheme in {"http", "https"} and base.hostname in {"localhost", "127.0.0.1", "::1"}


def answer(state):
    result = {"conf_active": True, "documents": [], "chunks": [], "tools_used": []}
    try:
        found = conf_retriever.search(state["employee_id"], state["conf_doc"], nodes.question_text(state))
        if not found["authorized"]:
            return {**result, "answer": NO_ACCESS, "kind": "refused"}
        chunks = found["chunks"]
        if not chunks or max(chunk["score"] for chunk in chunks) < nodes.VERIFY_MIN_SCORE:
            return {**result, "answer": NO_EVIDENCE, "kind": "refused"}
        if not _local_llm():
            log.warning("기밀문서 답변은 로컬 LLM에서만 생성할 수 있습니다")
            return {**result, "answer": ERROR, "kind": "refused"}
        hidden = found["hidden"]
        is_self, has_tool, future_only, asks_rule = nodes._classify_signals(state)
        question = nodes._intent_question(state["masked_question"])
        needs_rule = asks_rule or "승진" in question
        if needs_rule:
            chunks = chunks[:4]
        doc_ids = {chunk["doc_id"] for chunk in chunks}
        documents = [{**doc, "title": _hide(doc["title"], hidden)} for doc in found["documents"]
                     if doc["doc_id"] in doc_ids]
        chunks = _hide_values(chunks, hidden)
        work = {**state, **result, "chunks": chunks, "conf_hidden": hidden,
                "llm_question": _hide(state["masked_question"], hidden)}
        if is_self and has_tool and not future_only:
            work.update(nodes.lookup(work))
            work["lookup"] = _hide_values(work["lookup"], hidden)
            work["intent"] = "both"
        if needs_rule:
            rule = nodes.retrieve(work)["chunks"]
            work["chunks"] = chunks + _hide_values(rule[:5 - len(chunks)], hidden)
            work.update(nodes.calc(work))
            if "calc" in work:
                work["calc"] = _hide_values(work["calc"], hidden)
        generated = nodes.generate(work)["answer"]
        if not generated:
            return {**result, "answer": NO_EVIDENCE, "kind": "refused"}
        return {**result, "answer": _hide(generated, hidden), "chunks": work["chunks"],
                "documents": documents, "tools_used": work.get("tools_used", []),
                "verified": True, "kind": "answer"}
    except (psycopg.Error, httpx.HTTPError, OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        log.error("기밀문서 검색 실패: %s", type(exc).__name__)
        return {**result, "answer": ERROR, "kind": "refused"}
