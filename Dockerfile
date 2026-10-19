FROM node:22-bookworm-slim AS javascript
FROM python:3.12-slim
COPY --from=javascript /usr/local/bin/node /usr/local/bin/node
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
CMD gunicorn -b 0.0.0.0:${PORT:-10000} -w 1 --threads 6 --timeout 300 app:app
