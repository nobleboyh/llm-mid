FROM python:3.11-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# entrypoint registers Headroom ASGI middleware + guardrails + skills, then starts LiteLLM
COPY entrypoint.py .
COPY guardrails ./guardrails/
COPY skills ./proxy/skills/
COPY __init__.py ./proxy/
COPY callback.py ./proxy/
COPY capture_original.py ./proxy/
COPY skill_injector.py ./proxy/

# litellm_config.yaml is bind-mounted at runtime so it can be edited
# without rebuilding the image

EXPOSE 4000

CMD ["python", "/app/entrypoint.py"]
