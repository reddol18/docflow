# 기반 이미지는 다이제스트로 고정한다 — 스캔한 이미지와 다음 빌드가 받는 이미지가 같게 (https://github.com/reddol18/docflow/issues/46).
# 판을 올리면 다이제스트도 바꾼다 (태그 목록: scripts/mac/image_tags.sh)
FROM python:3.11.17-slim-bookworm@sha256:f06d01d4ba3f8ad5af45a3ec87df19c1d317d8e8961edc0fabda61811af6f7cf AS base
# opencv(paddlex 의존)가 libGL·glib를, paddle이 OpenMP를 쓴다
# upgrade: 기반 이미지 이후 나온 Debian 보안 갱신(openssl 등)을 받는다 (https://github.com/reddol18/docflow/issues/44)
# APT_REFRESH: scripts/mac/build_image.sh가 ISO 주를 넘겨 이 층의 캐시를 주 1회 깬다
ARG APT_REFRESH=none
ARG DEBIAN_FRONTEND=noninteractive
RUN echo "apt refresh $APT_REFRESH" && apt-get update \
    && apt-get upgrade -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 app
WORKDIR /app

# ---- 잠금 파일 갱신 전용 (scripts/lock_refresh.ps1 → scripts/mac/lock_refresh.sh가 --target lock-out으로만 부른다)
# requirements.txt를 이미지 플랫폼(linux/amd64, 위 python)에서 풀어 설치하고 pip freeze --all을 남긴다 → scripts/lock_hashes.py가 해시를 붙인다.
# 보통 빌드(대상 지정 없음)는 이 단계를 건너뛴다 (https://github.com/reddol18/docflow/issues/33)
FROM base AS lock-resolve
COPY requirements.txt .
# setuptools·wheel도 requirements.txt에 고정한다 (기반 이미지에 든 판의 취약점, https://github.com/reddol18/docflow/issues/44)
# freeze --all: 그 둘과 pip도 잠금 파일에 남겨 로컬 검사(osv)가 보게 한다
# pip도 기반 이미지 판(24.0)에 취약점이 있어 먼저 올린다 (osv가 lock에서 잡음). pip 판을 바꾸면 여기를 고치고 lock을 다시 만든다
# --only-binary: 앱 단계와 같은 조건으로 푼다 (휠 있는 판만 고른다)
RUN pip install --no-cache-dir pip==26.3 && pip install --no-cache-dir --only-binary :all: -r requirements.txt \
    && pip freeze --all > /requirements.freeze
# freeze 파일만 꺼내는 단계 (--target lock-out --output type=local) — 이미지 태그를 만들지 않는다
FROM scratch AS lock-out
COPY --from=lock-resolve /requirements.freeze /

# ---- 앱 이미지 (기본 대상: 마지막 단계)
FROM base
# 잠금 파일 그대로 설치한다 — 같은 커밋이면 몇 번 빌드해도 같은 패키지 (https://github.com/reddol18/docflow/issues/33)
#  --require-hashes: lock에 없는 판·해시면 실패. --no-deps: lock에 없는 전이 의존성을 몰래 받지 않는다 → 빠지면 pip check가 실패
#  --only-binary: 휠만. sdist를 빌드하면 그 빌드 의존성(setuptools 등)은 해시 확인 없이 받기 때문
#  pip를 먼저 lock의 판으로 올린다 (기반 이미지 pip 24.0). 마지막에 설치 결과(freeze --all)가 lock 목록과 한 줄이라도 다르면 빌드 실패
#  "lock 일치" 줄은 scripts/mac/build_image.sh가 빌드 로그에서 찾아 배포 출력에 보인다
COPY requirements.lock .
RUN grep -E '^pip==' requirements.lock > /tmp/pip.lock \
    && pip install --no-cache-dir --require-hashes --no-deps --only-binary :all: -r /tmp/pip.lock \
    && pip install --no-cache-dir --require-hashes --no-deps --only-binary :all: -r requirements.lock \
    && pip check \
    && pip freeze --all > /app/requirements.freeze \
    && grep -vE '^(#|$)' requirements.lock | cut -d' ' -f1 | sort -f > /tmp/want \
    && sort -f /app/requirements.freeze | diff /tmp/want - \
    && echo "lock 일치: 이미지 패키지 $(wc -l < /tmp/want)개 = requirements.lock" \
    && rm /tmp/pip.lock /tmp/want
# 모델은 빌드 때 받아 이미지에 넣는다 (실행 중 Pod는 외부 통신이 OpenAI 443뿐)
ENV PADDLE_PDX_CACHE_HOME=/opt/paddlex PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True
COPY app/__init__.py app/ocr.py app/warm.py app/
RUN python -m app.warm && chown -R app /opt/paddlex
COPY app/ app/
# Windows에서 scp로 올린 새 폴더는 700이라 app 사용자가 못 읽는다 (export_templates, https://github.com/reddol18/docflow/issues/12 배포 실패)
RUN chmod -R a+rX app
USER app
ENV DATA_DIR=/data
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
