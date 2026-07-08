FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends docker.io docker-compose-plugin \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY app ./app
COPY .env.example ./

CMD ["python", "-m", "app.main"]
