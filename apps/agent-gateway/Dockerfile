FROM python:3.12-slim AS builder
WORKDIR /app

RUN pip install --no-cache-dir uv
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv pip install --system --no-cache -e .

FROM python:3.12-slim AS runner
WORKDIR /app

RUN useradd -r -u 1001 appuser

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY src ./src
COPY pyproject.toml README.md ./

ENV PYTHONUNBUFFERED=1
EXPOSE 8081

HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8081/health')" || exit 1

USER appuser
CMD ["python", "-m", "agent_gateway.main"]
