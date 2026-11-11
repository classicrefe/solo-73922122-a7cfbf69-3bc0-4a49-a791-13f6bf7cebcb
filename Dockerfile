FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SCHEDULE_DATA_DIR=/data

WORKDIR /app

RUN groupadd --system app && \
    useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 持久化数据目录：SQLite 已发布库、发布锁与导入临时文件均放在这里，
# 通过 compose 命名卷挂载，容器重建后已发布版本仍然存在。
RUN mkdir -p /data && chown -R app:app /data

USER app

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).read()"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
