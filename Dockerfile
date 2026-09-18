FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
 && chmod a+r /etc/apt/keyrings/docker.asc \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin iptables jq openssh-client \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Только версии из лока с проверкой хешей: образ пересобирается на каждом
# деплое, и незафиксированная зависимость приезжала бы в root-контейнер без ревью.
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY app ./app
COPY agent_memory ./agent_memory
COPY skills ./skills
COPY .env.example ./

CMD ["python", "-m", "app.main"]
