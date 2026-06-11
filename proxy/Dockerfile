FROM python:3.11-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# entrypoint registers Headroom ASGI middleware + guardrails, then starts LiteLLM
COPY entrypoint.py .
COPY guardrails ./guardrails/

# litellm_config.yaml is bind-mounted at runtime so it can be edited
# without rebuilding the image

EXPOSE 4000

CMD ["python", "/app/entrypoint.py"]
