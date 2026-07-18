FROM python:3.11-slim
WORKDIR /app
COPY libs/contracts /libs/contracts
COPY services/worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -e /libs/contracts
COPY services/worker/consumer_stub.py .
ENV SERVICE_ROLE=worker
CMD ["python", "consumer_stub.py"]
