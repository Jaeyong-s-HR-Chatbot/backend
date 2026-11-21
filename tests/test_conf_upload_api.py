from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import auth, conf_api, conf_store, config, roles
from app.main import app

DISCIPLINE = "discipline-2026-03-minutes"
LABOR = "labor-2026-q4-minutes"
GRANTS = {
    "E1686": {"EMPLOYEE", "HR_DISCIPLINE"},
    "E1707": {"EMPLOYEE", "HR_LABOR"},
    "E1622": {"EMPLOYEE", "SYS_ADMIN"},
}
USERS = ("E1686", "E1707", "E1622", "E1422", "E1243", "E1545", "E1007")


@pytest.fixture
def api(monkeypatch, tmp_path):
    rows = {}
    monkeypatch.setattr(config, "USE_DB", False)
    monkeypatch.setattr(auth, "USERS", {
        emp: {"employee_id": emp, "name": "가상", "dept": "가상", "grade": "가상", "role": "employee"}
        for emp in USERS
    })
    monkeypatch.setattr(roles, "get_roles", lambda emp: GRANTS.get(emp, {"EMPLOYEE"}))
    monkeypatch.setattr(conf_api, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(conf_api, "_load_model", lambda: (None, None))

    def start(meta, access, temporary, path):
        old = rows.get(meta["id"])
        if old and conf_api.upload_meta.CATEGORY_ROLES[old["category"]] not in access:
            raise conf_store.ForbiddenDocument()
        if old and old["status"] == "processing":
            raise conf_store.BusyDocument()
        temporary.replace(path)
        rows[meta["id"]] = {
            "doc_id": meta["id"], "title": meta["name"], "confidential": True,
            "doc_no": meta.get("doc_no"), "owner_dept": meta.get("owner_dept"),
            "issued_at": meta.get("date"), "source_file": meta["file"],
            "created_at": datetime.now(timezone.utc), "status": "processing",
            "category": meta["category"], "subject": "가상 대상", "participants": [],
            "text": "원문", "masked_text": "가린 사본", **meta["_upload"],
        }

    def complete(path, meta, model):
        assert path.read_bytes().startswith(b"%PDF")
        rows[meta["id"]]["status"] = "ready"

    def listed(access):
        return [
            row for row in rows.values()
            if "SYS_ADMIN" in access or conf_api.upload_meta.CATEGORY_ROLES[row["category"]] in access
        ]

    def delete(doc_id, access, path):
        row = rows.get(doc_id)
        if not row:
            return False
        if conf_api.upload_meta.CATEGORY_ROLES[row["category"]] not in access:
            raise conf_store.ForbiddenDocument()
        if row["status"] == "processing":
            raise conf_store.BusyDocument()
        path.unlink(missing_ok=True)
        del rows[doc_id]
        return True

    monkeypatch.setattr(conf_store, "start_document", start)
    monkeypatch.setattr(conf_store, "list_documents", listed)
    monkeypatch.setattr(conf_store, "delete_document", delete)
    monkeypatch.setattr(conf_store, "mark_failed", lambda doc_id: rows[doc_id].update(status="failed"))
    monkeypatch.setattr(conf_api.ingest, "ingest_document", complete)
    with TestClient(app) as client:
        yield client, rows


def headers(emp, role="employee"):
    return {"Authorization": f"Bearer {auth.create_token(emp, role)}"}


def upload(client, emp, doc=DISCIPLINE, *, content=b"%PDF-1.4 test", fields=None, filename=None):
    data = {"title": "가상 문서", "confidential": "true", "doc_no": "",
            "owner_dept": "", "issued_at": ""}
    data.update(fields or {})
    return client.post(
        "/documents", headers=headers(emp), data=data,
        files={"file": (filename or f"{doc}.pdf", content, "application/pdf")},
    )


@pytest.mark.parametrize(("emp", "allowed", "denied"), [
    ("E1686", DISCIPLINE, LABOR),
    ("E1707", LABOR, DISCIPLINE),
])
def test_category_upload_and_ready(api, emp, allowed, denied):
    client, rows = api
    response = upload(client, emp, allowed)
    assert response.status_code == 200
    assert response.json()["status"] == "processing"
    assert response.json()["uploaded_by"].startswith("P-")
    assert rows[allowed]["status"] == "ready"
    assert upload(client, emp, denied).status_code == 403
    assert [row["doc_id"] for row in client.get("/documents", headers=headers(emp)).json()] == [allowed]


@pytest.mark.parametrize("emp", ["E1422", "E1243", "E1545", "E1007"])
def test_other_employees_are_forbidden_even_with_forged_role(api, emp):
    client, _ = api
    assert upload(client, emp, fields={"role": "HR_DISCIPLINE", "clearance": "4"}).status_code == 403
    assert client.get("/documents", headers=headers(emp, "hr_admin")).status_code == 403


def test_admin_sees_only_metadata_and_cannot_change_documents(api):
    client, _ = api
    assert upload(client, "E1686").status_code == 200
    assert upload(client, "E1707", LABOR).status_code == 200
    response = client.get("/documents", headers=headers("E1622"))
    assert response.status_code == 200
    assert len(response.json()) == 2
    assert set(response.json()[0]) == {
        "doc_id", "title", "confidential", "doc_no", "owner_dept", "issued_at",
        "file_name", "file_size", "uploaded_at", "uploaded_by", "status",
    }
    assert upload(client, "E1622").status_code == 403
    assert client.delete(f"/documents/{DISCIPLINE}", headers=headers("E1622")).status_code == 403
    assert client.delete(f"/documents/{DISCIPLINE}", headers=headers("E1707")).status_code == 403


@pytest.mark.parametrize(("options", "message"), [
    ({"filename": "other.txt"}, None),
    ({"content": b"not a pdf"}, None),
    ({"filename": "unregistered.pdf"}, "문서 정보가 등록되지 않은 파일이에요"),
    ({"fields": {"confidential": "false"}}, "일반 문서 업로드는 아직 지원하지 않아요"),
    ({"fields": {"issued_at": "not-a-date"}}, "시행일"),
])
def test_invalid_uploads(api, options, message):
    client, rows = api
    response = upload(client, "E1686", **options)
    assert response.status_code == 400
    if message:
        assert message in response.json()["detail"]
    assert not rows


def test_size_limit_and_workspace_spool(api, monkeypatch, tmp_path):
    client, rows = api
    dirs = []
    original = conf_api.tempfile.SpooledTemporaryFile

    def spool(*args, **kwargs):
        dirs.append(kwargs.get("dir"))
        return original(*args, **kwargs)

    monkeypatch.setattr(conf_api.tempfile, "SpooledTemporaryFile", spool)
    response = upload(client, "E1686", content=b"%PDF" + b"x" * conf_api.MAX_SIZE)
    assert response.status_code == 400
    assert dirs and all(directory == tmp_path for directory in dirs)
    assert not rows
    assert list(tmp_path.iterdir()) == []


def test_exactly_50mb_is_allowed(api):
    client, rows = api
    content = b"%PDF" + b"x" * (conf_api.MAX_SIZE - 4)
    response = upload(client, "E1686", content=content)
    assert response.status_code == 200
    assert response.json()["file_size"] == conf_api.MAX_SIZE
    assert rows[DISCIPLINE]["status"] == "ready"


def test_reupload_and_delete(api, tmp_path):
    client, rows = api
    assert upload(client, "E1686").status_code == 200
    assert upload(client, "E1686", content=b"%PDF replacement").status_code == 200
    assert (tmp_path / f"{DISCIPLINE}.pdf").read_bytes() == b"%PDF replacement"
    response = client.delete(f"/documents/{DISCIPLINE}", headers=headers("E1686"))
    assert response.status_code == 204 and not response.content
    assert client.get("/documents", headers=headers("E1686")).json() == []
    assert not rows and not (tmp_path / f"{DISCIPLINE}.pdf").exists()
    assert client.delete(f"/documents/{DISCIPLINE}", headers=headers("E1686")).status_code == 404


def test_failed_processing_and_busy_document(api, monkeypatch, caplog):
    client, rows = api

    def fail(*args, **kwargs):
        raise ValueError("원문을 포함할 수 있는 오류")

    monkeypatch.setattr(conf_api.ingest, "ingest_document", fail)
    assert upload(client, "E1686").status_code == 200
    assert rows[DISCIPLINE]["status"] == "failed"
    assert "원문을 포함할 수 있는 오류" not in caplog.text
    rows[DISCIPLINE]["status"] = "processing"
    assert upload(client, "E1686").status_code == 409
    assert client.delete(f"/documents/{DISCIPLINE}", headers=headers("E1686")).status_code == 409


def test_legacy_rows_with_missing_upload_metadata(api):
    client, rows = api
    assert upload(client, "E1686").status_code == 200
    rows[DISCIPLINE].update(file_name=None, file_size=None, uploaded_at=None, uploaded_by=None)
    row = client.get("/documents", headers=headers("E1686")).json()[0]
    assert row["file_name"] == f"{DISCIPLINE}.pdf"
    assert row["file_size"] == 0
    assert row["uploaded_at"]
    assert row["uploaded_by"] == "스크립트 적재"


def test_model_is_loaded_once(monkeypatch):
    loaded = []
    model = (object(), object())
    monkeypatch.setattr(conf_api, "_model", None)
    monkeypatch.setattr(conf_api.ner_model, "load", lambda: loaded.append(True) or model)
    assert conf_api._load_model() is model
    assert conf_api._load_model() is model
    assert len(loaded) == 1


def test_delete_cors_preflight(api):
    client, _ = api
    response = client.options("/documents/test", headers={
        "Origin": "http://localhost:5173",
        "Access-Control-Request-Method": "DELETE",
    })
    assert response.status_code == 200
    assert "DELETE" in response.headers["access-control-allow-methods"]


@pytest.mark.parametrize("operation", ["upload", "delete"])
def test_store_restores_file_if_transaction_fails(tmp_path, monkeypatch, operation):
    final = tmp_path / f"{DISCIPLINE}.pdf"
    final.write_bytes(b"old")
    temporary = tmp_path / "new.tmp"
    temporary.write_bytes(b"new")

    class Result:
        def fetchone(self):
            return {"category": "징계", "status": "ready"}

    class Connection:
        def execute(self, sql, params=()):
            return Result()

        @contextmanager
        def transaction(self):
            yield
            raise RuntimeError("commit failed")

    @contextmanager
    def connect():
        yield Connection()

    monkeypatch.setattr(conf_store, "connection", connect)
    monkeypatch.setattr(conf_store.ingest, "upsert_document", lambda *args: None)
    with pytest.raises(RuntimeError, match="commit failed"):
        if operation == "upload":
            conf_store.start_document({"id": DISCIPLINE}, {"HR_DISCIPLINE"}, temporary, final)
        else:
            conf_store.delete_document(DISCIPLINE, {"HR_DISCIPLINE"}, final)
    assert final.read_bytes() == b"old"
    assert not list(tmp_path.glob(f"{DISCIPLINE}.pdf.*.tmp"))
