"""데모 Keycloak 매니페스트·realm 내보내기·docflow OIDC 설정이 서로 맞는지 (https://github.com/reddol18/docflow/issues/39).
클러스터 없이 파일만 본다. 실제 연결 확인은 배포 뒤 scripts/keycloak.ps1 -Verify와 브라우저(README '데모 Keycloak')."""
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml

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


def test_realm_has_no_secret_values():
    """client secret·비밀번호 칸은 자리표시자만 — 값은 가져올 때 Secret에서 (scripts/mac/keycloak.sh)"""
    secret_keys = {"secret", "value", "password", "hashedSaltedValue", "secretData", "credentialData", "salt"}
    seen = 0
    for path, v in _walk(REALM):
        key = next((p for p in reversed(path) if isinstance(p, str)), "")
        if isinstance(v, bool):  # resetPasswordAllowed 같은 켜고 끄는 칸
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
    (c,) = REALM["clients"]
    assert c["clientId"] == "docflow" and c["publicClient"] is False and c["clientAuthenticatorType"] == "client-secret"
    assert c["attributes"]["pkce.code.challenge.method"] == "S256"  # PKCE S256 필수
    assert c["standardFlowEnabled"] and not c["implicitFlowEnabled"] and not c["directAccessGrantsEnabled"]
    assert not c["serviceAccountsEnabled"]
    assert c["redirectUris"] == [DF_ENV["OIDC_REDIRECT_URI"]]  # 와일드카드 없이 docflow 콜백 하나
    (m,) = [m for m in c["protocolMappers"] if m["protocolMapper"] == "oidc-group-membership-mapper"]
    assert m["config"]["claim.name"] == DF_ENV["OIDC_ROLE_CLAIM"] == "groups"
    assert m["config"]["full.path"] == "false" and m["config"]["id.token.claim"] == "true"  # docflow는 ID 토큰만 본다
    groups = {g["name"] for g in REALM["groups"]}
    assert groups == {"docflow-admin", "docflow-reviewer"}
    assert len(REALM["users"]) == 2
    for u in REALM["users"]:
        assert u["email"].endswith("@example.com")  # 가상 사용자
        assert {g.lstrip("/") for g in u["groups"]} <= groups and u["groups"]


def test_role_map_matches_groups_and_drops_on_removal(monkeypatch):
    """docflow 역할 지도 = realm 그룹. 그룹을 빼면 다음 로그인 때 역할이 내려가고, 다 빼면 거절 (app/auth.py oidc_role)"""
    from app import auth
    role_map = json.loads(DF_ENV["OIDC_ROLE_MAP"])
    assert set(role_map) == {g["name"] for g in REALM["groups"]}
    assert DF_ENV["OIDC_DEFAULT_ROLE"] == "none"
    monkeypatch.setattr(auth, "OIDC", auth.OidcConfig(role_claim=DF_ENV["OIDC_ROLE_CLAIM"], role_map=role_map, default_role=None))
    admin_user = next(u for u in REALM["users"] if "/docflow-admin" in u["groups"])
    groups = [g.lstrip("/") for g in admin_user["groups"]]
    assert auth.oidc_role({"groups": groups}) == "admin"
    assert auth.oidc_role({"groups": [g for g in groups if g != "docflow-admin"]}) == "reviewer"  # admin 그룹을 뺌
    assert auth.oidc_role({"groups": []}) is None  # 지도에 있는 그룹이 없음 → 로그인 거절


def test_docflow_oidc_settings_check():
    """배포 설정 그대로 OidcConfig.check를 통과하고, 비밀값은 파일(Secret 마운트)로만"""
    from app import auth
    cfg = auth.OidcConfig(issuer=DF_ENV["OIDC_ISSUER"], client_id=DF_ENV["OIDC_CLIENT_ID"],
                          redirect_uri=DF_ENV["OIDC_REDIRECT_URI"], role_claim=DF_ENV["OIDC_ROLE_CLAIM"],
                          role_map=json.loads(DF_ENV["OIDC_ROLE_MAP"]), allow_http=DF_ENV["OIDC_ALLOW_HTTP"] == "1")
    cfg.check(cookie_secure=True)
    assert "oidc" in DF_ENV["AUTH_MODES"].split(",") and "local" in DF_ENV["AUTH_MODES"].split(",")
    assert DF_ENV["OIDC_CLIENT_ID"] == REALM["clients"][0]["clientId"]
    assert "OIDC_CLIENT_SECRET" not in DF_ENV and DF_ENV["OIDC_CLIENT_SECRET_FILE"] == "/etc/oidc/client-secret"
    spec = DF_DEP["spec"]["template"]["spec"]
    mount = next(m for m in _container(DF_DEP, "docflow")["volumeMounts"] if m["name"] == "oidc")
    vol = next(v for v in spec["volumes"] if v["name"] == "oidc")
    assert mount["mountPath"] == "/etc/oidc" and mount["readOnly"] and vol["secret"]["secretName"] == "oidc"
    assert not vol["secret"].get("optional")  # 없으면 뜨지 않는다 (맥 deploy.sh가 apply 전에 먼저 막는다)
    assert "kubectl get secret oidc -n docs" in (ROOT / "scripts/mac/deploy.sh").read_text(encoding="utf-8")


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


def test_docflow_egress_has_keycloak_rule():
    eg = _get(DOCS, "NetworkPolicy", "docflow-egress")["spec"]["egress"]
    rules = [r for r in eg if any(t.get("namespaceSelector", {}).get("matchLabels", {}).get("kubernetes.io/metadata.name") == "keycloak"
                                  for t in r.get("to", []))]
    assert len(rules) == 1
    assert rules[0]["to"] == [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "keycloak"}},
                               "podSelector": {"matchLabels": {"app": "keycloak"}}}]
    assert rules[0]["ports"] == [{"protocol": "TCP", "port": 8080}]


def test_keycloak_script_disk_check_and_no_secret_echo():
    """맥 스크립트: 이미지 받기 전에 디스크 확인(1GB), 시험 값 지우기, 비밀값을 찍지 않음"""
    text = (ROOT / "scripts/mac/keycloak.sh").read_text(encoding="utf-8")
    deploy = (ROOT / "scripts/mac/deploy.sh").read_text(encoding="utf-8")
    unset = lambda t: set(re.findall(r"DISK_[A-Z_]+", next(x for x in t.splitlines() if x.startswith("unset DISK_"))))
    assert unset(deploy) - {"PIN_TRIES", "PIN_SLEEP"} <= unset(text)
    lines = text.splitlines()
    disk = next(i for i, x in enumerate(lines) if "disk_check.sh 1" in x and not x.lstrip().startswith("#"))
    apply = next(i for i, x in enumerate(lines) if "kubectl apply" in x and not x.lstrip().startswith("#"))
    assert disk < apply
    assert not re.search(r"\b(echo|printf|cat)\b[^\n]*\$D/(admin|cs|pw1|pw2|cs-docs)", text)
    for m in re.finditer(r"issues/\d+", text):  # 이슈는 전체 URL로
        assert text[:m.start()].endswith("https://github.com/reddol18/docflow/")
