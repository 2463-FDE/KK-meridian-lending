FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
COPY combined-ca-bundle.pem /tmp/combined-ca-bundle.pem
RUN PIP_CERT=/tmp/combined-ca-bundle.pem pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
