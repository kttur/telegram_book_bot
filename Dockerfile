FROM python:3.10-slim as base
WORKDIR /app/
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY src/ ./src/