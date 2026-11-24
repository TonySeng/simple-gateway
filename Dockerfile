# 注意：不要加 `# syntax=docker/dockerfile:1`。
# 本文件没有用到任何 BuildKit 专属语法，而该指令会在构建时去拉远程 frontend，
# 国内镜像源偶尔 403，直接导致「明明代码没改却构建失败」。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

# 非 root 运行，降低容器逃逸影响面
RUN groupadd --system appgroup && useradd --system --gid appgroup --create-home appuser

WORKDIR /app

# 依赖层单独缓存；使用国内镜像源加速（国际 PyPI 在部分网络下极慢）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    --trusted-host pypi.tuna.tsinghua.edu.cn

COPY app ./app
COPY run.py ./run.py
COPY tools ./tools

# 数据目录（SQLite + .secrets.json）放到命名卷
RUN mkdir -p /app/data && chown -R appuser:appgroup /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

CMD ["python", "run.py"]
