FROM python:3.11-slim
# rasterio's bundled GDAL dynamically links libexpat, absent from slim
# (found live in M2.3b: ImportError libexpat.so.1)
RUN apt-get update \
 && apt-get install -y --no-install-recommends libexpat1 \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY libs/contracts /libs/contracts
COPY services/worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -e /libs/contracts
# Fail the BUILD, not the first task, if native deps are broken:
RUN python -c "import rasterio, numpy, matplotlib, PIL; print('geostack imports OK')"
COPY services/worker/worker_main.py .
COPY services/worker/wrappers ./wrappers
ENV SERVICE_ROLE=worker
CMD ["python", "worker_main.py"]
