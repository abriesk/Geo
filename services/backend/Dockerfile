FROM python:3.11-slim
WORKDIR /app
COPY libs/contracts /libs/contracts
COPY services/backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -e /libs/contracts
COPY services/backend/app ./app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
