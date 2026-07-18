FROM python:3.11-slim
WORKDIR /app
COPY libs/contracts /libs/contracts
COPY services/downloader/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -e /libs/contracts
COPY services/downloader/consumer_stub.py .
ENV SERVICE_ROLE=downloader
CMD ["python", "consumer_stub.py"]
