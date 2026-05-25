FROM python:3.12-slim

# patch: apply rule-change diffs
# git:   commit accepted rule changes to the rules repo
RUN apt-get update \
    && apt-get install -y --no-install-recommends patch git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ ./bot/

# /app/rules  — bind-mounted from nomic-rules repo (contains rules.py)
# /app/data   — named volume for SQLite database
VOLUME ["/app/rules", "/app/data"]

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "bot.bot"]
