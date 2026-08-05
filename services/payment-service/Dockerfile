FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# combined-ca-bundle.pem is gitignored (dev-machine TLS-inspection workaround,
# see loan-assistant's docker-compose.yml comment) and not present on a clean
# checkout. The trailing "*" makes this COPY a no-op glob match instead of a
# hard failure when the file is absent; the RUN below only sets PIP_CERT if it
# actually landed in the build context.
COPY combined-ca-bundle.pem* /tmp/
RUN if [ -f /tmp/combined-ca-bundle.pem ]; then \
      PIP_CERT=/tmp/combined-ca-bundle.pem pip install --no-cache-dir -r requirements.txt; \
    else \
      pip install --no-cache-dir -r requirements.txt; \
    fi

COPY app ./app

EXPOSE 8006
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8006"]
