# flood-worker — ESA SNAP + FLOODPY (M4.2a)
#
# Deliberately a SEPARATE image from the main worker. The worker already
# carries two conda envs (base + licsbas); adding a ~2 GB Java toolbox on top
# would push it past 5 GB and make every unrelated rebuild painful.
#
# Everything below was read off real sources rather than assumed:
#   - the SNAP installer URL and its unattended flags (ESA download page)
#   - the SNAP operators FLOODPY's graphs actually invoke (its XML graphs)
#   - the numpy/pandas ceilings FLOODPY's code requires (its source)
FROM condaforge/miniforge3:24.9.2-0

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget ca-certificates git procps unzip \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------- ESA SNAP
# SNAP 10.0.0 is pinned ON PURPOSE. FLOODPY documents 10.0.0 and drives SNAP
# through XML graphs, whose operators change across major versions — the
# toolboxes were even renamed after 10 ("Microwave Toolbox" replaced S1TBX).
# Current SNAP is 13.0.0; moving up is a deliberate experiment, not a default.
#
# PATH QUIRK, verified on ESA's download page: SNAP 10 lives under
#   /step/snap/10_0/     (underscore)
# while 9, 11 and 12 use 9.0 / 11.0 / 12.0 (dot). Getting this wrong 404s.
ARG SNAP_URL=https://download.esa.int/step/snap/10_0/installers/esa-snap_all_linux-10.0.0.sh
# Mirror, if download.esa.int is unhappy:
#   https://step.esa.int/downloads/10.0/installers/esa-snap_all_linux-10.0.0.sh
ARG SNAP_DIR=/opt/snap
RUN wget -q --show-progress --progress=dot:giga -O /tmp/esa-snap.sh "$SNAP_URL" \
 && chmod +x /tmp/esa-snap.sh \
 && /tmp/esa-snap.sh -q -dir "$SNAP_DIR" \
 && rm -f /tmp/esa-snap.sh \
 && test -x "$SNAP_DIR/bin/gpt"

ENV SNAP_HOME=${SNAP_DIR}
ENV PATH=${SNAP_DIR}/bin:${PATH}
ENV GPTBIN_PATH=${SNAP_DIR}/bin/gpt

# gpt memory + parallelism.
#
# HARD LESSON (M4.2b): FLOODPY runs one gpt process PER interferogram pair in
# parallel (CPU param), and passes each one a heap via its RAM param. It does
# NOT cap gpt's internal thread pool or tile cache, so each gpt also tries to
# use every core. On a 23 GB / 20-core box that produced ~15 JVMs each holding
# ~11 GB, swap exhausted, 20+ hours of thrashing.
#
# Fixes here, all belt-and-braces so no single knob failing recreates the pileup:
#   * a MODEST default heap in vmoptions (FLOODPY overrides per call, but this
#     bounds anything that reads the file);
#   * snap.parallelism=1 so each gpt uses ONE compute thread, not all cores;
#   * a bounded tile cache so gpt does not balloon RES.
ARG GPT_HEAP=4G
RUN sed -i -E "s/^-Xmx.*/-Xmx${GPT_HEAP}/" ${SNAP_DIR}/bin/gpt.vmoptions \
 && grep -q -- "-Xmx${GPT_HEAP}" ${SNAP_DIR}/bin/gpt.vmoptions
# Constrain gpt's internal JAI/engine so N parallel gpts do not each fan out
# across all cores and fight for the same tile cache.
RUN CFG=${SNAP_DIR}/etc/snap.properties \
 && printf '\nsnap.parallelism=1\nsnap.jai.tileCacheSize=1024\nsnap.jai.defaultTileSize=512\n' >> "$CFG" \
 && grep -q "snap.parallelism=1" "$CFG"

# Optional module updates. OFF by default: `snap --modules --update-all` is
# well known for never exiting, which would hang the build forever. The
# timeout makes it safe to switch on with --build-arg SNAP_UPDATE=true.
ARG SNAP_UPDATE=false
RUN if [ "$SNAP_UPDATE" = "true" ]; then \
      (timeout 900 ${SNAP_DIR}/bin/snap --nosplash --nogui --modules --update-all || true); \
    fi

# ---------------------------------------------------------------- FLOODPY
# Pinned to a commit for reproducibility, exactly as LiCSBAS is in the worker.
ARG FLOODPY_COMMIT=f7c51afab737e0f38de560e2067f0c7acfd28d03
RUN git init -q /opt/FLOODPY \
 && cd /opt/FLOODPY \
 && git remote add origin https://github.com/kleok/FLOODPY.git \
 && git fetch -q --depth 1 origin ${FLOODPY_COMMIT} \
 && git checkout -q FETCH_HEAD \
 && git rev-parse HEAD > /opt/FLOODPY_COMMIT.txt

COPY services/flood-worker/floodpy_env.yml /tmp/floodpy_env.yml
RUN mamba env create -f /tmp/floodpy_env.yml \
 && mamba clean -afy \
 && rm -f /tmp/floodpy_env.yml

ENV FLOODPY_HOME=/opt/FLOODPY
ENV PYTHONPATH=/opt/FLOODPY
ENV FLOODPY_PYTHON=/opt/conda/envs/floodpy/bin/python
# FLOODPY downloads precise orbits into this directory via sentineleof.
ENV SNAP_ORBIT_DIR=/data/scratch/floodpy/orbits

# cdsapi resolves credentials as: CDSAPI_URL/CDSAPI_KEY env vars, else the
# CDSAPI_RC path, else ~/.cdsapirc (verified in cdsapi/api.py:get_url_key_verify).
# Mounting the rc file somewhere sensible is NOT enough on its own — without
# this, cdsapi looks only at /root/.cdsapirc and fails with
# "Missing/incomplete configuration file".
ENV CDSAPI_RC=/run/secrets/cdsapirc

# The §6 contracts package, exactly as the worker/downloader/backend images do.
# wrap_floodpy validates its result.json against it, so without this the
# wrapper cannot write a result at all.
COPY libs/contracts /libs/contracts
RUN /opt/conda/envs/floodpy/bin/pip install --no-cache-dir -e /libs/contracts \
 && /opt/conda/envs/floodpy/bin/python -c "import geohazard_contracts; print('contracts', geohazard_contracts.__version__)"

# The queue CONSUMER runs under the image's base python, so it also needs pika
# and the contracts package there (the floodpy env above is for wrap_floodpy).
RUN pip install --no-cache-dir pika -e /libs/contracts \
 && python3 -c "import pika, geohazard_contracts; print('base env: pika + contracts OK')"

COPY services/flood-worker/flood_diagnostic.py /app/flood_diagnostic.py
COPY services/flood-worker/floodpy_event.py /app/floodpy_event.py
COPY services/flood-worker/wrap_floodpy.py /app/wrap_floodpy.py
COPY services/flood-worker/flood_worker_main.py /app/flood_worker_main.py
WORKDIR /app

ENV SERVICE_ROLE=flood-worker
# M4.2c: consume tasks.flood. The consumer runs under the image's base python
# (pika + contracts installed there); it spawns wrap_floodpy under
# FLOODPY_PYTHON, which carries SNAP/FLOODPY/numpy-1.23 etc.
CMD ["python3", "/app/flood_worker_main.py"]
