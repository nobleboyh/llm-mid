FROM python:3.11-slim

WORKDIR /app

# Install build dependencies for numpy/pandas
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-eval.txt .
RUN pip install --no-cache-dir -r requirements-eval.txt

COPY . .

CMD ["python", "-m", "eval.worker_main"]
