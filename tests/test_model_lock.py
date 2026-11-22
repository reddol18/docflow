"""app/model_lock.py — OCR 모델 파일 목록·sha256 대조 (https://github.com/reddol18/docflow/issues/102). 네트워크 없음."""
import hashlib
import os
from pathlib import Path

import pytest

from app import model_lock as ml

LOCK = Path(__file__).parent.parent / "app" / "models.lock"


def _tree(root):
    (root / "official_models" / "det").mkdir(parents=True)
    (root / "official_models" / "det" / "inference.pdiparams").write_bytes(b"weights-det")
    (root / "official_models" / "det" / "config.yml").write_bytes(b"a: 1\n")
    (root / "fonts").mkdir()
    (root / "fonts" / "korean.ttf").write_bytes(b"font")
    return root


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def test_scan_lists_every_file_with_sha256(tmp_path):
    have = ml.scan(_tree(tmp_path))
    assert have == {
        "fonts/korean.ttf": _sha(b"font"),
        "official_models/det/config.yml": _sha(b"a: 1\n"),
        "official_models/det/inference.pdiparams": _sha(b"weights-det"),
    }


def test_scan_skips_download_cache_records(tmp_path):
    """huggingface_hub의 .cache 기록(받은 시각이 든 .metadata)은 빌드마다 바뀌므로 목록에서 뺀다."""
    root = _tree(tmp_path)
    cache = root / "official_models" / "det" / ".cache" / "huggingface" / "download"
    cache.mkdir(parents=True)
    (cache / "inference.pdiparams.metadata").write_bytes(b"rev\netag\n1763856000.123\n")
    have = ml.scan(root)
    assert not any(".cache" in p.split("/") for p in have)
    assert len(have) == 3


def _meta(root):
    """받기 부산물: 모델 저장소 README·.gitattributes와 huggingface .cache 기록."""
    det = root / "official_models" / "det"
    (det / "README.md").write_bytes(b"# model card\n")
    (det / ".gitattributes").write_bytes(b"*.pdiparams filter=lfs\n")
    (det / ".cache" / "huggingface").mkdir(parents=True)
    (det / ".cache" / "huggingface" / "CACHEDIR.TAG").write_bytes(b"tag")
    return det


def test_scan_skips_readme_and_gitattributes(tmp_path):
    root = _tree(tmp_path)
    _meta(root)
    have = ml.scan(root)
    assert sorted(have) == ["fonts/korean.ttf", "official_models/det/config.yml", "official_models/det/inference.pdiparams"]
    assert len(ml.scan(root, skip=False)) == 6


def test_upstream_readme_edit_does_not_break_verify(tmp_path):
    """배포처가 모델 설명(README)만 고쳐도 빌드는 통과한다 — 대조 대상이 아니다."""
    root = _tree(tmp_path / "m")
    det = _meta(root)
    lock = tmp_path / "models.lock"
    ml.write(root, lock)
    (det / "README.md").write_bytes(b"# model card v2\n")
    (det / ".gitattributes").write_bytes(b"changed\n")
    assert ml.verify(root, lock) == 0


def test_unknown_new_file_still_fails(tmp_path):
    """빼는 것은 이름을 적은 것뿐 — 그 밖의 새 파일(예: 새 가중치)은 '남음'으로 실패한다."""
    root = _tree(tmp_path / "m")
    lock = tmp_path / "models.lock"
    ml.write(root, lock)
    (root / "official_models" / "det" / "README.txt").write_bytes(b"x")
    assert ml.verify(root, lock) == 1


def test_prune_removes_skipped_and_leaves_only_locked(tmp_path, capsys):
    root = _tree(tmp_path / "m")
    det = _meta(root)
    lock = tmp_path / "models.lock"
    ml.write(root, lock)
    capsys.readouterr()
    assert ml.prune(root, lock) == 0
    assert "대조 안 한 것 3개 지움" in capsys.readouterr().out
    assert not (det / "README.md").exists() and not (det / ".gitattributes").exists() and not (det / ".cache").exists()
    assert ml.scan(root, skip=False) == ml.parse(lock.read_text(encoding="utf-8"))


def test_prune_fails_when_unlisted_file_remains(tmp_path):
    root = _tree(tmp_path / "m")
    lock = tmp_path / "models.lock"
    ml.write(root, lock)
    (root / "fonts" / "other.ttf").write_bytes(b"x")
    assert ml.prune(root, lock) == 1


def test_scan_checks_symlink_before_skipping_dirs(tmp_path, monkeypatch):
    """심볼릭 링크 확인은 SKIP_DIRS로 빼기 전에 — .cache라는 이름의 링크도 멈춘다."""
    root = _tree(tmp_path)
    (root / "official_models" / "det" / ".cache").mkdir()
    real = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == ".cache" or real(self))
    with pytest.raises(SystemExit, match="심볼릭 링크"):
        ml.scan(root)


def test_render_parse_roundtrip_sorted(tmp_path):
    have = ml.scan(_tree(tmp_path))
    text = ml.render(have)
    assert text.startswith("#")
    body = [ln for ln in text.splitlines() if not ln.startswith("#")]
    assert [ln.split("  ", 1)[1] for ln in body] == sorted(have)
    assert ml.parse(text) == have


def test_verify_passes_on_same_tree(tmp_path, capsys):
    root = _tree(tmp_path / "m")
    lock = tmp_path / "models.lock"
    assert ml.write(root, lock) == 0
    assert ml.verify(root, lock) == 0
    assert "모델 일치: OCR 모델 파일 3개" in capsys.readouterr().out


@pytest.mark.parametrize("change, label", [
    (lambda r: (r / "official_models/det/inference.pdiparams").write_bytes(b"tampered"), "해시 다름"),
    (lambda r: (r / "fonts/korean.ttf").unlink(), "빠짐"),
    (lambda r: (r / "official_models/det/extra.bin").write_bytes(b"x"), "남음"),
])
def test_verify_fails_on_any_difference(tmp_path, capsys, change, label):
    root = _tree(tmp_path / "m")
    lock = tmp_path / "models.lock"
    ml.write(root, lock)
    capsys.readouterr()
    change(root)
    assert ml.verify(root, lock) == 1
    err = capsys.readouterr().err
    assert f"!! 모델 {label}" in err and "!! 모델 불일치" in err


def test_verify_fails_on_one_wrong_hash_in_lock(tmp_path):
    root = _tree(tmp_path / "m")
    lock = tmp_path / "models.lock"
    ml.write(root, lock)
    text = lock.read_text(encoding="utf-8").replace(_sha(b"font"), "0" * 64)
    lock.write_text(text, encoding="utf-8")
    assert ml.verify(root, lock) == 1


def test_compare_reports_each_kind():
    want = {"a": "1", "b": "2", "c": "3"}
    have = {"a": "1", "b": "X", "d": "4"}
    assert ml.compare(want, have) == (["c"], ["d"], ["b"])


@pytest.mark.parametrize("text", [
    "",                                       # 빈 lock
    "# 머리말만\n",
    "abc  x\n",                               # 해시 길이
    ("A" * 64) + "  x\n",                     # 대문자
    ("a" * 64) + " x\n",                      # 칸 하나
    ("a" * 64) + "  /etc/passwd\n",           # 절대 경로
    ("a" * 64) + "  ../x\n",                  # 폴더 밖
    ("a" * 64) + "  x\n" + ("b" * 64) + "  x\n",  # 겹침
])
def test_parse_rejects_bad_lock(text):
    with pytest.raises(ValueError):
        ml.parse(text)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink 없음")
def test_scan_rejects_symlink(tmp_path):
    root = _tree(tmp_path / "m")
    try:
        os.symlink(root / "fonts" / "korean.ttf", root / "link.ttf")
    except OSError:
        pytest.skip("이 PC에서 symlink를 만들 권한 없음")
    with pytest.raises(SystemExit):
        ml.scan(root)


def test_main_usage_error():
    assert ml.main([]) == 2
    assert ml.main(["check", "a", "b"]) == 2


def test_committed_lock_is_well_formed():
    """저장소의 app/models.lock — 형식이 맞고 PaddleOCR 모델(official_models)을 담는다."""
    entries = ml.parse(LOCK.read_text(encoding="utf-8"))
    assert any(p.startswith("official_models/") for p in entries)
    names = {p.rsplit("/", 1)[-1] for p in entries}
    assert not names & ml.SKIP_NAMES and not any(set(p.split("/")) & ml.SKIP_DIRS for p in entries)
    assert LOCK.read_bytes() == ml.render(entries).encode("utf-8")  # 손으로 고치지 않은 그대로(정렬·머리말·LF)
