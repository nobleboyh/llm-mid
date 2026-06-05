FROM python:3.13-slim

# Headroom Rust build dependencies
ENV PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir maturin puccinialin

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY litellm_config.yaml gatemid_callbacks.py .

EXPOSE 4000

ENTRYPOINT ["litellm", "--config", "/app/litellm_config.yaml", "--port", "4000", "--debug"]
