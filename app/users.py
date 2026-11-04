"""docflow 자체 계정(local) 관리. 화면에서는 계정을 만들지 않는다 — 서버 안에서 이 명령으로.

  python -m app.users add 이름 --role admin [--display 표시이름]   비밀번호를 두 번 묻는다
  python -m app.users passwd 이름          비밀번호 바꾸기 (그 계정의 세션을 모두 끊는다)
  python -m app.users role 이름 reviewer   역할 바꾸기 (viewer / reviewer / admin)
  python -m app.users disable|enable 이름  끄기·켜기 (끄면 세션도 끊는다)
  python -m app.users unlock 이름          로그인 실패 잠금 풀기
  python -m app.users list

맥 k3s에서는:  kubectl exec -it -n docs deploy/docflow -- python -m app.users add admin --role admin
비밀번호를 파이프로 넣으려면 --password-stdin (스크립트용, 셸 기록에 남지 않게).
"""
import argparse
import getpass
import sys

from app import auth, store

MIN_LEN = 10


def _password(args):
    if args.password_stdin:
        pw = sys.stdin.readline().rstrip("\n")
    else:
        pw = getpass.getpass("비밀번호: ")
        if getpass.getpass("한 번 더: ") != pw:
            sys.exit("두 번 입력한 비밀번호가 다름")
    if len(pw) < MIN_LEN:
        sys.exit(f"비밀번호는 {MIN_LEN}자 이상")
    return pw


def _local(c, username):
    u = c.execute("SELECT id FROM users WHERE username = %s AND issuer IS NULL", (username,)).fetchone()
    if not u:
        sys.exit(f"local 계정 없음: {username}")
    return u["id"]


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m app.users", description="docflow local 계정 관리")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("username")
    a.add_argument("--role", choices=auth.ROLES, required=True)
    a.add_argument("--display", default=None, help="화면에 보일 이름")
    a.add_argument("--password-stdin", action="store_true")
    pw = sub.add_parser("passwd")
    pw.add_argument("username")
    pw.add_argument("--password-stdin", action="store_true")
    r = sub.add_parser("role")
    r.add_argument("username")
    r.add_argument("role", choices=auth.ROLES)
    for name in ("disable", "enable", "unlock"):
        sub.add_parser(name).add_argument("username")
    sub.add_parser("list")
    args = p.parse_args(argv)

    opened_here = store.pool.closed  # 시험처럼 이미 열린 풀을 빌려 쓰면 닫지 않는다
    store.init_db()
    try:
        run(args)
    finally:
        if opened_here:
            store.pool.close()


def run(args):
    with store.pool.connection() as c:
        if args.cmd == "add":
            if c.execute("SELECT 1 FROM users WHERE username = %s AND issuer IS NULL", (args.username,)).fetchone():
                sys.exit(f"이미 있음: {args.username}")
            c.execute("INSERT INTO users (username, name, role, password_hash) VALUES (%s, %s, %s, %s)",
                      (args.username, args.display or args.username, args.role, auth.hash_password(_password(args))))
            print(f"만듦: {args.username} ({args.role})")
        elif args.cmd == "passwd":
            uid = _local(c, args.username)
            c.execute("UPDATE users SET password_hash = %s, failed = 0, locked_until = NULL WHERE id = %s",
                      (auth.hash_password(_password(args)), uid))
            print(f"바꿈: {args.username}, 끊은 세션 {auth.drop_user_sessions(uid)}")
        elif args.cmd == "role":
            c.execute("UPDATE users SET role = %s WHERE id = %s", (args.role, _local(c, args.username)))
            print(f"역할: {args.username} → {args.role}")
        elif args.cmd in ("disable", "enable"):
            uid = _local(c, args.username)
            c.execute("UPDATE users SET disabled = %s WHERE id = %s", (args.cmd == "disable", uid))
            n = auth.drop_user_sessions(uid) if args.cmd == "disable" else 0
            print(f"{args.cmd}: {args.username}" + (f", 끊은 세션 {n}" if n else ""))
        elif args.cmd == "unlock":
            c.execute("UPDATE users SET failed = 0, locked_until = NULL WHERE id = %s", (_local(c, args.username),))
            print(f"잠금 풂: {args.username}")
        elif args.cmd == "list":
            rows = c.execute("SELECT username, name, role, issuer IS NOT NULL AS sso, disabled, locked_until > now() AS locked, "
                             "last_login FROM users ORDER BY id").fetchall()
            for u in rows:
                tags = [t for t, on in (("sso", u["sso"]), ("꺼짐", u["disabled"]), ("잠김", u["locked"])) if on]
                print(f"{u['username']:<20} {u['role']:<9} {u['name'] or '':<16} {' '.join(tags):<10} {u['last_login'] or '-'}")


if __name__ == "__main__":
    main()
