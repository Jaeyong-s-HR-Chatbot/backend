"""팀 DB 덤프(DB-main)를 내 컴퓨터의 PostgreSQL에 올리고, 백엔드 .env에 접속 주소를 넣어 줘요. Docker 없이 실행해요.

사용법 (PowerShell, backend 폴더에서):
    python scripts/load_hrdb.py --db-main "..\\DB-main\\DB-main"

하는 일
  1. psql 찾기 (PATH → C:\\Program Files\\PostgreSQL\\*\\bin)
  2. pgvector 확장이 설치돼 있는지 확인
  3. hrdb 데이터베이스 만들기 (이미 있으면 --reset 을 줘야 지우고 다시 만듦)
  4. 00_extensions.sql → 01_hrdb.sql.gz → 02_grants.sql → 03_categories.sql(있으면) → 대화 로그 테이블 순서로 실행
  5. 건수 확인 (직원 800명, 규정 조각, 임베딩)
  6. backend/.env 의 HRDB_URL 을 채움 (비밀번호가 들어가므로 .env 는 Git에 올리지 않아요)
모든 데이터는 가상 데이터예요.
"""
import argparse
import getpass
import glob
import gzip
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote


def find_psql() -> str:
    p = shutil.which("psql")
    if p:
        return p
    cands = sorted(glob.glob(r"C:\Program Files\PostgreSQL\*\bin\psql.exe"), reverse=True)
    if cands:
        return cands[0]
    sys.exit("psql을 찾지 못했어요. PostgreSQL이 설치돼 있는지 확인하거나, --psql 로 psql.exe 경로를 알려 주세요.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-main", required=True, help="DB-main 폴더 (deploy/initdb 가 들어 있는 곳)")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", default="5432")
    ap.add_argument("--user", default="postgres")
    ap.add_argument("--password", default=None, help="안 주면 물어봐요 (화면에 보이지 않음)")
    ap.add_argument("--dbname", default="hrdb")
    ap.add_argument("--reset", action="store_true", help="hrdb가 이미 있으면 지우고 다시 만들기")
    ap.add_argument("--psql", default=None)
    ap.add_argument("--env", default=str(Path(__file__).resolve().parent.parent / ".env"), help="HRDB_URL을 써 넣을 .env 경로")
    a = ap.parse_args()

    init = Path(a.db_main) / "deploy" / "initdb"
    files = [init / "00_extensions.sql", init / "01_hrdb.sql.gz", init / "02_grants.sql"]
    chat_log_sql = Path(__file__).with_name("create_chat_log.sql")
    for f in [*files, chat_log_sql]:
        if not f.exists():
            sys.exit(f"파일이 없어요: {f}\n--db-main 에 DB-main 폴더 경로를 정확히 넣어 주세요.")
    psql = a.psql or find_psql()
    pw = a.password if a.password is not None else getpass.getpass(f"PostgreSQL '{a.user}' 계정 비밀번호: ")
    env = {**os.environ, "PGPASSWORD": pw, "PGCLIENTENCODING": "UTF8"}
    base = [psql, "-h", a.host, "-p", str(a.port), "-U", a.user, "-v", "ON_ERROR_STOP=1", "-X", "-q"]

    def run(db, *args, check=True):
        r = subprocess.run(base + ["-d", db, *args], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if check and r.returncode != 0:
            sys.exit(f"실행 실패: {' '.join(args)[:80]}\n{r.stderr.strip()[:800]}")
        return r

    r = run("postgres", "-Atc", "select version()", check=False)
    if r.returncode != 0:
        sys.exit("PostgreSQL에 접속하지 못했어요. 서버가 켜져 있는지, 계정·비밀번호·포트가 맞는지 확인해 주세요.\n" + r.stderr.strip()[:400])
    print("1/6 접속:", r.stdout.strip()[:60])

    has_vec = run("postgres", "-Atc", "select count(*) from pg_available_extensions where name='vector'").stdout.strip()
    if has_vec != "1":
        sys.exit("pgvector 확장이 설치돼 있지 않아요. PostgreSQL 버전에 맞는 pgvector를 설치한 뒤 다시 실행해 주세요.\n"
                 "확인: psql 에서  select * from pg_available_extensions where name='vector';")
    print("2/6 pgvector 확장 사용 가능")

    exists = run("postgres", "-Atc", f"select count(*) from pg_database where datname='{a.dbname}'").stdout.strip() == "1"
    if exists and not a.reset:
        sys.exit(f"'{a.dbname}' 데이터베이스가 이미 있어요. 지우고 다시 만들려면 --reset 을 붙여 주세요. (지우면 그 안의 데이터가 사라져요)")
    if exists:
        run("postgres", "-c", f'DROP DATABASE "{a.dbname}"')
    run("postgres", "-c", f'CREATE DATABASE "{a.dbname}" ENCODING \'UTF8\' TEMPLATE template0')
    print(f"3/6 데이터베이스 {a.dbname} 생성")

    run(a.dbname, "-f", str(files[0]))
    with tempfile.TemporaryDirectory() as td:        # psql은 .gz를 못 읽어서 풀어서 실행
        sql = Path(td) / "01_hrdb.sql"
        with gzip.open(files[1], "rb") as src, open(sql, "wb") as dst:
            shutil.copyfileobj(src, dst)
        print("4/6 덤프 적재 중 (1~2분 걸릴 수 있어요)...")
        run(a.dbname, "-f", str(sql))
    run(a.dbname, "-f", str(files[2]))
    cat_sql = init / "03_categories.sql"            # 새 DB 저장소에만 있어요 (조각 카테고리 여러 개)
    if cat_sql.exists():
        run(a.dbname, "-f", str(cat_sql))
        print("    03_categories.sql 적용")
    run(a.dbname, "-f", str(chat_log_sql))

    q = ("select (select count(*) from hr.employees), (select count(*) from hr.users), "
         "(select count(*) from rag.chunks where searchable), (select count(embedding) from rag.chunks), "
         "(select count(*) from rag.documents), (select count(*) from information_schema.tables "
         "where table_schema='app' and table_name='chat_log')")
    emp, users, chunks, emb, docs, logt = run(a.dbname, "-AtF", ",", "-c", q).stdout.strip().split(",")
    print(f"5/6 확인: 직원 {emp}명, 계정 {users}개, 문서 {docs}건, 검색 대상 조각 {chunks}개, 임베딩 {emb}개, "
          f"대화 로그 테이블 {'있음' if logt == '1' else '없음'}")

    url = f"postgresql://{quote(a.user, safe='')}:{quote(pw, safe='')}@{a.host}:{a.port}/{a.dbname}"
    envp = Path(a.env)
    lines = envp.read_text(encoding="utf-8").splitlines() if envp.exists() else []
    lines = [ln for ln in lines if not ln.startswith("HRDB_URL=") and "scripts/load_hrdb.py 가 넣음" not in ln]
    while lines and not lines[-1].strip():
        lines.pop()
    lines += ["", "# 인사·규정 DB (scripts/load_hrdb.py 가 넣음. 비밀번호가 있으니 Git에 올리지 마세요)", f"HRDB_URL={url}"]
    envp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"6/6 {envp} 에 HRDB_URL 을 넣었어요.")
    print("\n다음: 백엔드를 다시 켜고 http://localhost:8000/health 에서 db.ok 가 true 인지 확인하세요.")
    print("테스트 계정: 임직원 e1584 / 인사팀 e1003, 비밀번호는 DB-main README에 적힌 테스트 비밀번호")


if __name__ == "__main__":
    main()
