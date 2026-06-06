FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV FITATU_DB_FILE=/data/fitatu_nutrition.db

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

RUN mkdir -p /data

EXPOSE 8000

CMD ["uvicorn", "fitatu_mcp.server:app", "--host", "0.0.0.0", "--port", "8000"]
