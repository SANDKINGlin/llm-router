# Dockerfile — llm-router 数据面(Phase1)。
# 多阶段:builder 按 hash-lock 装依赖(供应链安全),runner 精简 + 非 root。
# 构建:docker build -t llm-router .
# 运行:见 docker-compose.yml(或 docker run -p 8789:8789 -v $PWD/data:/app/data --env-file .env llm-router)
# 详见 OpenSpec change add-docker-packaging。

# ── builder:hash-locked 依赖 ──
FROM python:3.12-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir --require-hashes -r requirements.txt

# ── runner:精简运行时,非 root ──
FROM python:3.12-slim AS runner
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app/src
# 源码 + 路由 policy + Scanner 清单(路径:app.py/config.py/mnfst.py parents[N] 均解析到 /app)
COPY src/ ./src/
COPY router-policy.yaml ./
COPY mnfst/ ./mnfst/
# 非 root 运行 + data 目录(运行时 DB,挂 volume 覆盖)
RUN useradd -r -u 1000 app && mkdir -p /app/data && chown -R app:app /app
USER app
EXPOSE 8789
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8789/healthz',timeout=3).status==200 else 1)"
CMD ["uvicorn", "llm_router.app:app", "--host", "0.0.0.0", "--port", "8789"]
