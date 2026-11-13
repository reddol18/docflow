"""데모 Keycloak 매니페스트·realm 내보내기·docflow OIDC 설정이 서로 맞는지, 맥 스크립트 scripts/mac/keycloak.sh의 분기
(https://github.com/reddol18/docflow/issues/39). 클러스터 없이 파일·가짜 kubectl(tests/fixtures/fakekube)로 본다.
실제 연결 확인은 배포 뒤 scripts/keycloak.ps1 -Verify와 브라우저(README '데모 Keycloak')."""
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

from test_disk_check import RUNNER, run_code

ROOT = Path(__file__).resolve().parents[1]
REALM = json.loads((ROOT / "k8s/keycloak/docflow-realm.json").read_text(encoding="utf-8"))
KC = list(yaml.safe_load_all((ROOT / "k8s/keycloak.yaml").read_text(encoding="utf-8")))
DOCS = list(yaml.safe_load_all((ROOT / "k8s/docs.yaml").read_text(encoding="utf-8")))
PLACEHOLDER = re.compile(r"^\$\{[A-Z][A-Z0-9_]*\}$")


def _get(docs, kind, name):
    return next(d for d in docs if d and d["kind"] == kind and d["metadata"]["name"] == name)


def _container(dep, name):
    return next(c for c in dep["spec"]["template"]["spec"]["containers"] if c["name"] == name)


def _env(c):
    return {e["name"]: e for e in c.get("env", [])}


KC_DEP = _get(KC, "Deployment", "keycloak")
KC_C = _container(KC_DEP, "keycloak")
DF_DEP = _get(DOCS, "Deployment", "docflow")
DF_ENV = {k: v.get("value") for k, v in _env(_container(DF_DEP, "docflow")).items()}


def _walk(v, path=()):
    if isinstance(v, dict):
        for k, x in v.items():
            yield from _walk(x, path + (k,))
    elif isinstance(v, list):
        for i, x in enumerate(v):
            yield from _walk(x, path + (i,))
    else:
        yield path, v


# ---- realm 내보내기 ----

def test_realm_has_no_secret_values():
    """client secret·비밀번호 칸은 자리표시자만 — 값은 가져올 때 Secret에서 (scripts/mac/keycloak.sh)"""
    secret_keys = {"secret", "value", "password", "hashedSaltedValue", "secretData", "credentialData", "salt"}
    seen = 0
    for path, v in _walk(REALM):
        key = next((p for p in reversed(path) if isinstance(p, str)), "")
        if isinstance(v, bool) or key == "passwordPolicy":  # resetPasswordAllowed 같은 켜고 끄는 칸, 정책 글자
            continue
        if key in secret_keys or "password" in key.lower() or "secret" in key.lower():
            seen += 1
            assert isinstance(v, str) and PLACEHOLDER.match(v), (path, "자리표시자가 아닌 비밀값 칸")
    assert seen == 3  # client secret 1 + 사용자 비밀번호 2
    for u in REALM["users"]:
        assert all(c["type"] == "password" and not c.get("temporary") for c in u["credentials"])


def test_realm_placeholders_come_from_secrets():
    """JSON의 ${…}는 모두 Keycloak 컨테이너 환경 변수이고, 그 값은 Secret 참조 (매니페스트에 값이 없다)"""
    names = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", json.dumps(REALM)))
    env = _env(KC_C)
    assert names == {"DOCFLOW_CLIENT_SECRET", "DEMO_PW_HANGARAM", "DEMO_PW_SEONURI"}
    for n in names | {"KC_BOOTSTRAP_ADMIN_PASSWORD"}:
        assert "value" not in env[n] and env[n]["valueFrom"]["secretKeyRef"]["name"], n
        assert not n.startswith("KC_") or n == "KC_BOOTSTRAP_ADMIN_PASSWORD"  # KC_는 Keycloak 설정 옵션으로 읽힌다


def test_realm_client_groups_users():
    assert REALM["realm"] == "docflow" and REALM["enabled"] and REALM["sslRequired"] == "external"
    assert REALM["passwordPolicy"] == "length(12)" and REALM["bruteForceProtected"]
    (c,) = REALM["clients"]
    assert c["clientId"] == "docflow" and c["publicClient"] is False and c["clientAuthenticatorType"] == "client-secret"
    assert c["attributes"]["pkce.code.challenge.method"] == "S256"  # PKCE S256 필수
    assert c["standardFlowEnabled"] and not c["implicitFlowEnabled"] and not c["directAccessGrantsEnabled"]
    assert not c["serviceAccountsEnabled"]
    assert c["redirectUris"] == [DF_ENV["OIDC_REDIRECT_URI"]]  # 와일드카드 없이 docflow 콜백 하나
    (m,) = [m for m in c["protocolMappers"] if m["protocolMapper"] == "oidc-group-membership-mapper"]
    assert m["config"]["claim.name"] == DF_ENV["OIDC_ROLE_CLAIM"] == "groups"
    # 전체 경로 (/docflow-admin) — 다른 부모 밑 같은 이름 하위 그룹이 역할을 얻지 않게. docflow는 ID 토큰만 본다
    assert m["config"]["full.path"] == "true" and m["config"]["id.token.claim"] == "true"
    groups = {g["path"] for g in REALM["groups"]}
    assert groups == {"/docflow-admin", "/docflow-reviewer"}
    assert len(REALM["users"]) == 2
    for u in REALM["users"]:
        assert u["email"].endswith("@example.com")  # 가상 사용자
        assert set(u["groups"]) <= groups and u["groups"]


def test_role_map_matches_groups_and_drops_on_removal(monkeypatch):
    """docflow 역할 지도 = realm 그룹 전체 경로. 그룹을 빼면 다음 로그인 때 역할이 내려가고, 다 빼면 거절 (app/auth.py oidc_role)"""
    from app import auth
    role_map = json.loads(DF_ENV["OIDC_ROLE_MAP"])
    assert set(role_map) == {g["path"] for g in REALM["groups"]}
    assert DF_ENV["OIDC_DEFAULT_ROLE"] == "none"
    monkeypatch.setattr(auth, "OIDC", auth.OidcConfig(role_claim=DF_ENV["OIDC_ROLE_CLAIM"], role_map=role_map, default_role=None))
    admin_user = next(u for u in REALM["users"] if "/docflow-admin" in u["groups"])
    groups = list(admin_user["groups"])  # 매퍼가 full.path=true라 클레임 값 = 그룹 경로
    assert auth.oidc_role({"groups": groups}) == "admin"
    assert auth.oidc_role({"groups": [g for g in groups if g != "/docflow-admin"]}) == "reviewer"  # admin 그룹을 뺌
    assert auth.oidc_role({"groups": []}) is None  # 지도에 있는 그룹이 없음 → 로그인 거절
    assert auth.oidc_role({"groups": ["/other/docflow-admin", "docflow-admin"]}) is None  # 하위 그룹·이름만은 아님


# ---- docflow 설정 ----

def test_docflow_oidc_settings_check():
    """배포 설정 그대로 OidcConfig.check(배포 표시 포함)를 통과하고, 비밀값은 파일(Secret 마운트)로만"""
    from app import auth
    cfg = auth.OidcConfig(issuer=DF_ENV["OIDC_ISSUER"], client_id=DF_ENV["OIDC_CLIENT_ID"],
                          redirect_uri=DF_ENV["OIDC_REDIRECT_URI"], role_claim=DF_ENV["OIDC_ROLE_CLAIM"],
                          role_map=json.loads(DF_ENV["OIDC_ROLE_MAP"]), allow_http=DF_ENV["OIDC_ALLOW_HTTP"] == "1")
    cfg.check(cookie_secure=True, require_tls=DF_ENV["DOCFLOW_REQUIRE_TLS"] == "1")
    assert "oidc" in DF_ENV["AUTH_MODES"].split(",") and "local" in DF_ENV["AUTH_MODES"].split(",")
    assert DF_ENV["OIDC_CLIENT_ID"] == REALM["clients"][0]["clientId"]
    assert "OIDC_CLIENT_SECRET" not in DF_ENV and DF_ENV["OIDC_CLIENT_SECRET_FILE"] == "/etc/oidc/client-secret"
    spec = DF_DEP["spec"]["template"]["spec"]
    mount = next(m for m in _container(DF_DEP, "docflow")["volumeMounts"] if m["name"] == "oidc")
    vol = next(v for v in spec["volumes"] if v["name"] == "oidc")
    assert mount["mountPath"] == "/etc/oidc" and mount["readOnly"] and vol["secret"]["secretName"] == "oidc"
    assert vol["secret"]["optional"] is True  # 없으면 OIDC만 꺼진 채 뜬다 (oidc_startup) — 데모가 배포를 막지 않음
    deploy = (ROOT / "scripts/mac/deploy.sh").read_text(encoding="utf-8")
    assert "Secret oidc 없음 — 이번 배포의 docflow는 OIDC(SSO) 꺼짐" in deploy
    assert "scripts/keycloak.ps1 먼저. 배포하지 않음" not in deploy  # 멈추지 않는다


def test_oidc_startup_without_secret_file_disables_oidc(tmp_path, capsys):
    """client secret 파일이 없으면(Secret 아직 없음) local,oidc는 OIDC만 끄고 경고, oidc만이면 시작 거부. 파일이 있으면 그대로"""
    from app import auth
    missing = str(tmp_path / "none")
    cfg = auth.OidcConfig(issuer=DF_ENV["OIDC_ISSUER"], client_id="docflow", redirect_uri=DF_ENV["OIDC_REDIRECT_URI"], allow_http=True)
    env = {"OIDC_CLIENT_SECRET_FILE": missing}
    assert auth.oidc_startup(frozenset({"local", "oidc"}), cfg, env) == {"local"}
    assert "OIDC(SSO)를 끔" in capsys.readouterr().out
    with pytest.raises(RuntimeError):
        auth.oidc_startup(frozenset({"oidc"}), cfg, env)  # 끄면 인증이 없어진다 → 거부
    assert auth.oidc_startup(frozenset({"local"}), cfg, env) == {"local"}  # oidc를 안 켰으면 보지 않음
    f = tmp_path / "client-secret"
    f.write_text("s3cret\n", encoding="utf-8")
    cfg2 = auth.OidcConfig(**{**cfg.__dict__, "client_secret": "s3cret"})
    assert auth.oidc_startup(frozenset({"local", "oidc"}), cfg2, {"OIDC_CLIENT_SECRET_FILE": str(f)}) == {"local", "oidc"}
    bad = auth.OidcConfig(**{**cfg2.__dict__, "issuer": "http://sso.example.com/realms/x"})
    with pytest.raises(RuntimeError):  # 파일이 있으면 설정 확인은 그대로 (http 바깥 발급자 거부)
        auth.oidc_startup(frozenset({"local", "oidc"}), bad, {"OIDC_CLIENT_SECRET_FILE": str(f)})


@pytest.mark.parametrize("host,ok", [
    ("localhost", True), ("keycloak.localhost", True), ("KEYCLOAK.localhost.", True), ("keycloak.keycloak.svc", True),
    ("keycloak.keycloak.svc.cluster.local", True), ("127.0.0.1", True), ("10.43.0.39", True), ("192.168.0.22", True),
    ("::1", True), ("fd00::1", True),
    ("keycloak", False), ("sso.example.com", False), ("localhost.evil.test", False), ("svc.evil.test", False),
    ("8.8.8.8", False), ("169.254.169.254", False), ("0.0.0.0", False), ("", False), (None, False),
])
def test_internal_issuer_host(host, ok):
    from app import auth
    assert auth.internal_issuer_host(host) is ok


def test_allow_http_and_require_tls_need_internal_issuer():
    """OIDC_ALLOW_HTTP=1이면 발급자 호스트가 클러스터 안·루프백만, 배포 표시(DOCFLOW_REQUIRE_TLS=1)에서 http 발급자도 같은 규칙"""
    from app import auth
    base = {"client_id": "docflow", "redirect_uri": "http://localhost:3000/api/auth/oidc/callback"}
    auth.OidcConfig(issuer="http://keycloak.localhost:8180/realms/docflow", allow_http=True, **base).check(require_tls=True)
    for iss in ("http://sso.example.com/realms/x", "https://sso.example.com/realms/x", "http://169.254.169.254/x", "http://keycloak:8080/x"):
        with pytest.raises(RuntimeError):
            auth.OidcConfig(issuer=iss, allow_http=True, **base).check()
    auth.OidcConfig(issuer="https://sso.example.com/realms/x", **base).check(require_tls=True)  # https 바깥 발급자는 그대로
    cfg = auth.OidcConfig(issuer="http://sso.example.com/realms/x", **base)
    cfg.allow_http = True
    with pytest.raises(RuntimeError):
        cfg.check(require_tls=True)


def test_auth_config_exposes_callback_origin(monkeypatch):
    """로그인 화면은 자기 출처가 oidc_origin과 같을 때만 SSO 버튼을 보인다 (frontend/src/app/login/page.tsx)"""
    from app import auth
    assert auth.web_origin(DF_ENV["OIDC_REDIRECT_URI"]) == "http://localhost:3000"
    assert auth.web_origin("https://docflow.localhost:443/api/auth/oidc/callback") == "https://docflow.localhost"
    assert auth.web_origin("https://Docflow.Example:8443/cb") == "https://docflow.example:8443"
    assert auth.web_origin("javascript:alert(1)") is None and auth.web_origin("") is None
    monkeypatch.setattr(auth, "OIDC", auth.OidcConfig(redirect_uri=DF_ENV["OIDC_REDIRECT_URI"], label="Keycloak(데모)"))
    monkeypatch.setattr(auth, "MODES", frozenset({"local", "oidc"}))
    assert auth.auth_config()["oidc_origin"] == "http://localhost:3000"
    monkeypatch.setattr(auth, "MODES", frozenset({"local"}))
    assert auth.auth_config()["oidc_origin"] is None
    page = (ROOT / "frontend/src/app/login/page.tsx").read_text(encoding="utf-8")
    assert "cfg.oidc_origin === window.location.origin" in page


def test_issuer_same_for_browser_and_pod():
    """issuer = Keycloak KC_HOSTNAME + /realms/docflow, 그 호스트를 docflow 파드가 hostAliases로 Keycloak Service 고정 IP에 묶는다"""
    hostname = _env(KC_C)["KC_HOSTNAME"]["value"]
    assert DF_ENV["OIDC_ISSUER"] == hostname + "/realms/" + REALM["realm"]
    u = urlsplit(hostname)
    assert u.hostname.endswith(".localhost")  # 브라우저가 늘 루프백으로 푸는 이름 (ssh 터널)
    svc = _get(KC, "Service", "keycloak")
    (alias,) = DF_DEP["spec"]["template"]["spec"]["hostAliases"]
    assert alias["hostnames"] == [u.hostname] and alias["ip"] == svc["spec"]["clusterIP"]
    assert alias["ip"].startswith("10.43.0.")  # 서비스 대역 아래쪽 정적 몫 (자동 배정과 겹치지 않게)
    (port,) = svc["spec"]["ports"]
    assert port["port"] == u.port and port["targetPort"] == int(_env(KC_C)["KC_HTTP_PORT"]["value"])


# ---- Keycloak 매니페스트 ----

def test_keycloak_image_pinned_and_limited():
    image = KC_C["image"]
    assert re.fullmatch(r"quay\.io/keycloak/keycloak:26\.\d+\.\d+@sha256:[0-9a-f]{64}", image)
    r = KC_C["resources"]
    assert r["limits"]["memory"] == "1Gi" and r["requests"]["memory"] == "512Mi" and r["requests"]["cpu"]
    heap = _env(KC_C)["JAVA_OPTS_KC_HEAP"]["value"]
    assert "MaxRAMPercentage=50" in heap and "MaxMetaspaceSize=" in heap  # 힙 512m + 힙 밖 몫이 1Gi 안에
    assert KC_C["args"][:2] == ["start", "--optimized"] and "--import-realm" in KC_C["args"]
    quota = _get(KC, "ResourceQuota", "keycloak-quota")["spec"]["hard"]
    assert quota["limits.memory"] == "1Gi" and quota["persistentvolumeclaims"] == "1"


def test_keycloak_image_scanned_and_tag_checked():
    """배포 때 이미지 스캔·고정 다이제스트 확인이 keycloak.yaml도 읽는다 (https://github.com/reddol18/docflow/issues/46)"""
    scan = (ROOT / "scripts/mac/scan_image.sh").read_text(encoding="utf-8")
    tags = (ROOT / "scripts/mac/image_tags.sh").read_text(encoding="utf-8")
    assert 'img_of "$HERE/../k8s/keycloak.yaml"' in scan and "따로 올리는 데모 — 건너뜀" in scan
    assert '"$ROOT/k8s/keycloak.yaml"' in tags and "quay.io/*)" in tags
    assert "k8s/keycloak.yaml" in (ROOT / "scripts/deploy.ps1").read_text(encoding="utf-8-sig")  # 맥으로 복사


def test_keycloak_pod_security():
    """docs.yaml과 같은 최소 권한 (https://github.com/reddol18/docflow/issues/29)"""
    spec = KC_DEP["spec"]["template"]["spec"]
    assert spec["securityContext"]["runAsNonRoot"] and spec["securityContext"]["runAsUser"] != 0
    assert spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert spec["automountServiceAccountToken"] is False
    sc = KC_C["securityContext"]
    assert sc["allowPrivilegeEscalation"] is False and sc["readOnlyRootFilesystem"] is True and sc["capabilities"]["drop"] == ["ALL"]
    mounts = {m["mountPath"] for m in KC_C["volumeMounts"]}
    assert mounts == {"/opt/keycloak/data", "/opt/keycloak/data/import", "/tmp"}


def test_keycloak_network_policies():
    """keycloak 네임스페이스는 기본 차단, 들어오는 것은 docs의 docflow API 파드 8080뿐"""
    deny = _get(KC, "NetworkPolicy", "default-deny")["spec"]
    assert deny["podSelector"] == {} and set(deny["policyTypes"]) == {"Ingress", "Egress"}
    pols = [d for d in KC if d and d["kind"] == "NetworkPolicy" and "Ingress" in d["spec"]["policyTypes"] and d["spec"].get("ingress")]
    assert [p["metadata"]["name"] for p in pols] == ["keycloak-from-docflow"]
    (rule,) = pols[0]["spec"]["ingress"]
    assert rule["from"] == [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "docs"}},
                             "podSelector": {"matchLabels": {"app": "docflow"}}}]
    assert rule["ports"] == [{"protocol": "TCP", "port": 8080}]
    egress = [d for d in KC if d and d["kind"] == "NetworkPolicy" and d["spec"].get("egress")]
    assert [p["metadata"]["name"] for p in egress] == ["allow-dns"]  # 바깥으로는 DNS만


def _to_keycloak(rule):
    return any(t.get("namespaceSelector", {}).get("matchLabels", {}).get("kubernetes.io/metadata.name") == "keycloak"
               for t in rule.get("to", []))


def test_keycloak_egress_api_pod_only():
    """Keycloak으로 나가는 규칙은 API 파드(app=docflow)만 고르는 정책 하나 — 워커가 함께 걸리는 docflow-egress에는 없다"""
    pols = [d for d in DOCS if d and d["kind"] == "NetworkPolicy" and any(_to_keycloak(r) for r in d["spec"].get("egress", []))]
    assert [p["metadata"]["name"] for p in pols] == ["docflow-to-keycloak"]
    spec = pols[0]["spec"]
    assert spec["podSelector"] == {"matchLabels": {"app": "docflow"}} and spec["policyTypes"] == ["Egress"]
    (rule,) = spec["egress"]
    assert rule["to"] == [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "keycloak"}},
                           "podSelector": {"matchLabels": {"app": "keycloak"}}}]
    assert rule["ports"] == [{"protocol": "TCP", "port": 8080}]
    assert not any(_to_keycloak(r) for r in _get(DOCS, "NetworkPolicy", "docflow-egress")["spec"]["egress"])


# ---- 맥 스크립트 (글자) ----

def test_keycloak_script_static():
    """이미지 받기 전에 디스크 확인(1GB), 시험 값 지우기, 비밀값을 찍지 않음, put은 kube_put.sh 하나, verify는 check_discovery 규칙"""
    text = (ROOT / "scripts/mac/keycloak.sh").read_text(encoding="utf-8")
    deploy = (ROOT / "scripts/mac/deploy.sh").read_text(encoding="utf-8")
    unset = lambda t: set(re.findall(r"DISK_[A-Z_]+", next(x for x in t.splitlines() if x.startswith("unset DISK_"))))
    assert unset(deploy) - {"PIN_TRIES", "PIN_SLEEP"} <= unset(text)
    assert not re.search(r"\b(echo|printf|cat)\b[^\n]*\$D/(admin|cs|pw1|pw2|cs-kc|cs-docs)", text)
    for f in ("scripts/mac/keycloak.sh", "scripts/mac/tls_secrets.sh"):
        t = (ROOT / f).read_text(encoding="utf-8")
        assert '. "$HERE/kube_put.sh"' in t and "put() {" not in t and "DRY=--dry-run=client" not in t, f
    for k in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        assert k in text and k in __import__("app.auth", fromlist=["DISCOVERY_URLS"]).DISCOVERY_URLS
    assert "sys.exit(0 if" in text
    ps = (ROOT / "scripts/keycloak.ps1").read_text(encoding="utf-8-sig")
    assert ps.count("scp -q @SshOpts") == 3 and "scp -q (" not in ps and "kube_put.sh" in ps
    assert "kube_put.sh" in (ROOT / "scripts/tls_secrets.ps1").read_text(encoding="utf-8-sig")
    for f in ("scripts/mac/keycloak.sh", "scripts/mac/kube_put.sh", "k8s/keycloak.yaml"):
        t = (ROOT / f).read_text(encoding="utf-8")
        for m in re.finditer(r"issues/\d+", t):  # 이슈는 전체 URL로
            assert t[:m.start()].endswith("https://github.com/reddol18/docflow/"), (f, t[m.start() - 30:m.end()])


# ---- 맥 스크립트 (가짜 kubectl로 분기) ----

KC_SETUP = (
    'B=$(mktemp -d); mkdir -p "$B/bin" "$B/doc/scripts" "$B/doc/k8s/keycloak" "$B/state";'
    ' cp tests/fixtures/fakekube/kubectl "$B/bin/"; chmod +x "$B/bin/kubectl";'
    ' cp scripts/mac/keycloak.sh scripts/mac/kube_put.sh "$B/doc/scripts/";'
    ' printf "echo DISK_CHECK \\$*\\nexit 0\\n" > "$B/doc/scripts/disk_check.sh";'
    ' cp k8s/keycloak.yaml "$B/doc/k8s/"; cp k8s/keycloak/docflow-realm.json "$B/doc/k8s/keycloak/";'
    ' export PATH="$B/bin:$PATH" FAKE="$B/state"; echo "[10.43.0.0/16]" > "$FAKE/servicecidrs";'
    ' printf "default/kubernetes 10.43.0.1\\nkube-system/kube-dns 10.43.0.10\\n" > "$FAKE/svcs";\n'
)
KC_RUN = (
    '\nsh "$B/doc/scripts/keycloak.sh" 2>&1; echo "rc=$?"; echo "--calls"; cat "$FAKE/calls"; echo "--state";'
    ' find "$FAKE/secret" -type f 2>/dev/null | sort | while read -r f; do echo "${f#$FAKE/secret/}=$(cat "$f")"; done; rm -rf "$B"'
)
needs_sh = pytest.mark.skipif(RUNNER is None, reason="sh도 WSL도 없음 — 맥에서 손으로 확인 (PR 수동 확인 칸)")


def kc(setup=""):
    """가짜 kubectl 상태를 setup(sh)으로 만들고 keycloak.sh를 한 번 돌린다 → (rc, 출력, 부른 명령, Secret 값{경로: 값})"""
    _, out = run_code(KC_SETUP + setup + KC_RUN)
    main, rest = out.split("--calls", 1)
    calls, state = rest.split("--state", 1)
    secrets = dict(line.split("=", 1) for line in state.strip().splitlines() if "=" in line)
    rc = int(re.search(r"rc=(\d+)", main).group(1))
    return rc, main, calls, secrets


def _no_values_in(out, secrets):
    for v in secrets.values():
        assert v not in out, "비밀값이 출력에 찍힘"


@needs_sh
def test_script_first_run_creates_secrets_once_and_syncs_docs():
    rc, out, calls, sec = kc()
    assert rc == 0, out
    assert set(sec) == {"keycloak/keycloak-admin/password", "keycloak/keycloak-realm-secrets/client-secret",
                        "keycloak/keycloak-realm-secrets/demo-pw-hangaram", "keycloak/keycloak-realm-secrets/demo-pw-seonuri",
                        "docs/oidc/client-secret"}
    assert sec["docs/oidc/client-secret"] == sec["keycloak/keycloak-realm-secrets/client-secret"]
    assert all(re.fullmatch(r"[A-Za-z0-9]{32,}", v) for v in sec.values())  # 글자·숫자만 (HTTP Basic)
    _no_values_in(out, sec)
    assert "secret docs/oidc: Keycloak client secret과 같게 바꿈" in out
    assert "docflow는 아직 Secret oidc를 마운트하지 않음" in out and "rollout restart" not in calls
    assert "== clusterIP 10.43.0.39: 서비스 대역 10.43.0.0/16 안, 비어 있음" in out
    lines = calls.splitlines()
    assert out.index("DISK_CHECK 1") < out.index("applied")  # 이미지를 받기(apply) 전에 디스크 확인
    assert lines.index(next(x for x in lines if x.startswith("get servicecidrs"))) < lines.index(next(x for x in lines if x.startswith("apply")))


@needs_sh
def test_script_rerun_keeps_values_and_does_not_touch_docs():
    """두 번째 실행: Secret은 그대로(돌리지 않음), docs/oidc는 같으니 바꾸지 않고 docflow도 다시 띄우지 않는다"""
    setup = ('sh "$B/doc/scripts/keycloak.sh" >/dev/null 2>&1; touch "$FAKE/pvc"; echo "data oidc redis-auth" > "$FAKE/docflow_volumes";'
             ' : > "$FAKE/calls";\n')
    rc, out, calls, sec = kc(setup + 'true')
    assert rc == 0, out
    assert "secret keycloak/keycloak-admin: 있음 (그대로)" in out and "secret keycloak/keycloak-realm-secrets: 있음 (그대로)" in out
    assert "secret docs/oidc: Keycloak client secret과 같음 (그대로)" in out
    assert "STORED" not in calls.replace("STORED create configmap", "").replace("STORED replace configmap", "")
    assert "rollout restart" not in calls


@needs_sh
@pytest.mark.parametrize("missing", ["keycloak-realm-secrets", "keycloak-admin"])
def test_script_stops_when_secret_missing_but_realm_exists(missing):
    """PVC(realm이 든 H2)가 있는데 Secret이 없으면 새 값을 만들지 않고 멈춘다 — 되살리는 길을 찍는다"""
    other = {"keycloak-realm-secrets": "keycloak-admin", "keycloak-admin": "keycloak-realm-secrets"}[missing]
    setup = (f'mkdir -p "$FAKE/ns/keycloak" "$FAKE/secret/keycloak/{other}"; touch "$FAKE/pvc";'
             f' printf old > "$FAKE/secret/keycloak/{other}/password"; printf old > "$FAKE/secret/keycloak/{other}/client-secret";\n')
    rc, out, calls, sec = kc(setup)
    assert rc == 1, out
    assert f"Secret keycloak/{missing}이 없는데 PVC keycloak/keycloak-data가 있음" in out
    assert "kubectl delete namespace keycloak" in out and f"kubectl create secret generic {missing} -n keycloak" in out
    assert not any(k.startswith(f"keycloak/{missing}/") for k in sec)  # 만들지 않음
    assert "apply" not in calls and "STORED" not in calls


@needs_sh
def test_script_replaces_docs_secret_when_different_and_restarts_docflow():
    setup = ('mkdir -p "$FAKE/ns/keycloak" "$FAKE/secret/keycloak/keycloak-admin" "$FAKE/secret/keycloak/keycloak-realm-secrets" "$FAKE/secret/docs/oidc";'
             ' touch "$FAKE/pvc"; printf a1b2c3 > "$FAKE/secret/keycloak/keycloak-admin/password";'
             ' for k in client-secret demo-pw-hangaram demo-pw-seonuri; do printf Kc0rrect$k > "$FAKE/secret/keycloak/keycloak-realm-secrets/$k"; done;'
             ' printf stale0 > "$FAKE/secret/docs/oidc/client-secret"; echo "data oidc" > "$FAKE/docflow_volumes";\n')
    rc, out, calls, sec = kc(setup)
    assert rc == 0, out
    assert sec["docs/oidc/client-secret"] == "Kc0rrectclient-secret" == sec["keycloak/keycloak-realm-secrets/client-secret"]
    assert sec["keycloak/keycloak-admin/password"] == "a1b2c3"  # 정본은 그대로
    _no_values_in(out, sec)
    assert "rollout restart deploy/docflow -n docs" in calls and "rollout status deploy/docflow -n docs" in calls
    lines = calls.splitlines()
    assert lines.index("rollout status deploy/keycloak -n keycloak --timeout=600s") < lines.index("rollout restart deploy/docflow -n docs")


@needs_sh
@pytest.mark.parametrize("setup,msg", [
    ('echo "[10.44.0.0/16]" > "$FAKE/servicecidrs";', "clusterIP 10.43.0.39가 서비스 대역(10.44.0.0/16) 밖"),
    ('echo "bank-b/intake 10.43.0.39" >> "$FAKE/svcs";', "clusterIP 10.43.0.39를 이미 bank-b/intake가 쓴다"),
    ('rm "$FAKE/servicecidrs";', "서비스 대역(ServiceCIDR)을 읽지 못함"),
])
def test_script_cluster_ip_checked_before_apply(setup, msg):
    rc, out, calls, sec = kc(setup + "\n")
    assert rc == 1 and msg in out, out
    assert "apply" not in calls and not sec


@needs_sh
def test_script_cluster_ip_already_keycloak_is_ok():
    rc, out, calls, sec = kc('echo "keycloak/keycloak 10.43.0.39" >> "$FAKE/svcs";\n')
    assert rc == 0, out
    assert "== clusterIP 10.43.0.39: 서비스 대역 10.43.0.0/16 안, 이미 keycloak/keycloak" in out
