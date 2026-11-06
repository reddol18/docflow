FROM python:3.11.17-slim-bookworm
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
COPY requirements.txt .
# setuptools·wheel도 requirements.txt에 고정한다 (기반 이미지에 든 판의 취약점, https://github.com/reddol18/docflow/issues/44)
# freeze --all: 그 둘과 pip도 잠금 파일에 남겨 로컬 검사(osv)가 보게 한다
# pip도 기반 이미지 판(24.0)에 취약점이 있어 먼저 올린다 (osv가 lock에서 잡음)
RUN pip install --no-cache-dir pip==26.3 && pip install --no-cache-dir -r requirements.txt \
    && pip freeze --all > /app/requirements.lock
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
