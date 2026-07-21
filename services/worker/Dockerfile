# geohazard-chat worker — M3.1
# Two analysis environments in ONE container (user's MVP choice):
#   * system python3.11  -> wrap_ndvi (rasterio/numpy/matplotlib), unchanged
#   * conda env `licsbas` -> wrap_licsbas (LiCSBAS + GDAL/pygmt/netCDF4 stack)
# The subprocess boundary keeps them isolated, so splitting LiCSBAS into its
# own container later is a routing change, not a rewrite.
#
# HIGH-UNCERTAINTY BUILD: the LiCSBAS conda stack (GDAL, pygmt->GMT, netCDF4)
# is heavy and cannot be tested in the author's sandbox. Build-time gates
# below fail LOUDLY and EARLY if either environment is broken, so a bad build
# never reaches a user query. Expect a long first build.

FROM condaforge/miniforge3:24.9.2-0

# --- OS libs: libexpat for rasterio's GDAL (found M2.3); git for pip installs ---
RUN apt-get update \
 && apt-get install -y --no-install-recommends libexpat1 git build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- 1. slim wrap_ndvi stack in BASE env (mirrors the M2.3 worker) ---
COPY services/worker/requirements.txt ./requirements-ndvi.txt
RUN pip install --no-cache-dir -r requirements-ndvi.txt
# contracts package (editable) — shared by both wrappers
COPY libs/contracts /libs/contracts
RUN pip install --no-cache-dir -e /libs/contracts
# pika for the worker loop itself
RUN pip install --no-cache-dir "pika>=1.3"
# GATE 1: wrap_ndvi imports must work in base env
RUN python -c "import rasterio, numpy, matplotlib, PIL, pika, geohazard_contracts; print('base/wrap_ndvi env OK')"

# --- 2. LiCSBAS in its own conda env `licsbas` ---
# Pin the exact commit for reproducibility (default branch is `main`, not
# master). Shallow-fetch just this SHA. To update: bump LICSBAS_REF to a newer
# commit or a release tag.
ARG LICSBAS_REF=a145e6c4217b29eb7348ea95630454a099809098
RUN git init /opt/LiCSBAS \
 && cd /opt/LiCSBAS \
 && git remote add origin https://github.com/comet-licsar/LiCSBAS.git \
 && git fetch --depth 1 origin ${LICSBAS_REF} \
 && git checkout FETCH_HEAD
# Create the env from LiCSBAS's own environment.yml (authoritative dep list).
RUN mamba env create -n licsbas -f /opt/LiCSBAS/environment.yml
# Add the two pip packages in SEPARATE resolves. Co-resolving them in one
# command made pip backtrack indefinitely on the editable local package
# against licsar_extra's scikit-learn/numpy pulls (found M3.1 build).
RUN mamba run -n licsbas pip install --no-cache-dir \
      "git+https://github.com/comet-licsar/licsar_extra.git"
RUN mamba run -n licsbas pip install --no-cache-dir -e /libs/contracts \
 && mamba clean -afy
# LiCSBAS needs its bin/ on PATH and lib on PYTHONPATH.
ENV LiCSBAS=/opt/LiCSBAS
ENV PATH=/opt/LiCSBAS/bin:$PATH
ENV PYTHONPATH=/opt/LiCSBAS/bin:/opt/LiCSBAS/LiCSBAS_lib:$PYTHONPATH
# GATE 2: LiCSBAS install self-check must pass inside the licsbas env
RUN mamba run -n licsbas python /opt/LiCSBAS/bin/LiCSBAS_check_install.py || \
    (echo "LiCSBAS install check FAILED" && exit 1)
# GATE 3: the step-01 downloader is invokable (the piece M3.2 depends on)
RUN mamba run -n licsbas LiCSBAS01_get_geotiff.py -h >/dev/null 2>&1 && echo "LiCSBAS01 OK"

# --- worker code + both wrappers ---
COPY services/worker/worker_main.py .
COPY services/worker/wrappers ./wrappers
ENV SERVICE_ROLE=worker
# Path to the licsbas env python, so the worker can invoke wrap_licsbas under it.
ENV LICSBAS_PYTHON=/opt/conda/envs/licsbas/bin/python
CMD ["python", "worker_main.py"]
