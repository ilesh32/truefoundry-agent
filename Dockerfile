FROM python:3.13-slim

WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY janitor_agent/ janitor_agent/

# Non-root; no credentials baked in (pass MCP_AUTH_TOKEN / MCP_AUTH_TOKEN_FILE at runtime).
RUN useradd --create-home --shell /usr/sbin/nologin app && chown -R app:app /app
USER app

ENV PYTHONUNBUFFERED=1 \
    TRUEFORGE_BASE_URL=http://host.docker.internal:8791 \
    MCP_URL=http://host.docker.internal:8000/mcp

ENTRYPOINT ["python", "-m", "janitor_agent"]
CMD ["chat"]
