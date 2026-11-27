"""수신 비전 비교 도구(eval/recipient_vision.py)의 막기·메시지 꼴 — LLM 호출 없음 (https://github.com/reddol18/docflow/issues/1)"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
import recipient_vision as rv  # noqa: E402

from app import llm  # noqa: E402


@pytest.mark.parametrize("url", ["https://api.openai.com/v1", "http://10.0.0.5:11434/v1", "https://abc-11434.proxy.runpod.net/v1"])
def test_only_this_pc(url, tmp_path):
    with pytest.raises(SystemExit, match="127.0.0.1"):
        rv.main(["--images", str(tmp_path), "--out", str(tmp_path / "o"), "--base-url", url])


def test_client_ignores_proxy_env(monkeypatch):
    """프록시 환경 변수가 있어도 127.0.0.1 요청을 프록시로 보내지 않는다 (네트워크 호출 없음 — 클라이언트만 만든다)"""
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(k, "http://10.0.0.9:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    c = rv.make_client("http://127.0.0.1:11434/v1")
    http = c._client
    assert http.trust_env is False
    assert not http._mounts  # 환경 변수에서 읽은 프록시 전송이 없다


def test_out_outside_repo(tmp_path):
    with pytest.raises(SystemExit, match="저장소 밖"):
        rv.main(["--images", str(tmp_path), "--out", str(Path(rv.common.APP_ROOT) / "tmp_vision")])


def test_messages_follow_production():
    lines = ["수신 가상시 가상동 1-2", "제목 가상 통보"]
    prod = llm.extract_messages(rv.SCHEMA, lines)
    text = rv.messages_of("text", lines)
    assert text[0] == prod[0] and text[1]["content"].split("\n")[1:-1] == prod[1]["content"].split("\n")[1:-1]  # 꼬리만 다름
    img = rv.messages_of("image", lines, "QUJD")
    assert img[0]["content"].startswith(prod[0]["content"]) and img[0]["content"].endswith(rv.IMAGE_NOTE)
    parts = img[1]["content"]
    assert parts[0]["type"] == "text" and "가상시 가상동 1-2" in parts[0]["text"]
    assert parts[1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}
