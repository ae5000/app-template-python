# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app

ARG GITHUB_ORG

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install platform SDK from private GitHub repo.
# Requires sdk_token build secret (GitHub PAT with repo:read on platform-sdk-python).
RUN --mount=type=secret,id=sdk_token \
    pip install --no-cache-dir \
    "git+https://$(cat /run/secrets/sdk_token)@github.com/${GITHUB_ORG}/platform-sdk-python.git@main"

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
