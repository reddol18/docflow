FROM python:3.11.17-slim-bookworm
# opencv(paddlex 의존)가 libGL·glib를, paddle이 OpenMP를 쓴다
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip freeze > /app/requirements.lock
# 모델은 빌드 때 받아 이미지에 넣는다 (실행 중 Pod는 외부 통신이 OpenAI 443뿐)
ENV PADDLE_PDX_CACHE_HOME=/opt/paddlex PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True
COPY app/__init__.py app/ocr.py app/warm.py app/
RUN python -m app.warm && chown -R app /opt/paddlex
COPY app/ app/
USER app
ENV DATA_DIR=/data
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
