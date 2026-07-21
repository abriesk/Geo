# M3.1 build & test

## 1. Build the worker image (LONG — LiCSBAS conda stack)
This is the highest-uncertainty build in the project (heavy conda stack,
untestable in the author's sandbox). Three build-time GATES fail loudly if
anything is wrong, so a broken image never serves a query.

    cd /geo
    tar xzf geohazard-chat-m3.1.tar.gz
    # build worker alone first so a failure is isolated & the log is readable:
    docker compose build worker 2>&1 | tee /tmp/worker_build.log

Watch for these lines in order:
    base/wrap_ndvi env OK          # GATE 1: optical path intact
    ... (mamba solving licsbas env — several minutes) ...
    LiCSBAS install is OK          # GATE 2: LiCSBAS_check_install passed
    LiCSBAS01 OK                   # GATE 3: downloader invokable

If the build FAILS, paste the tail of /tmp/worker_build.log. Likely spots:
  - mamba env solve conflicts  -> we pin/relax a dep
  - licsar_extra pip install   -> network or repo path
  - LiCSBAS_check_install       -> a missing module we add to the env

## 2. Smoke: coverage check through the installed wrapper
Once built, verify --check-coverage runs INSIDE the image against the live
portal (base env has requests; wrapper is env-agnostic for this mode):

    docker compose run --rm --entrypoint python worker \
      wrappers/wrap_licsbas.py --check-coverage \
      --frame 174A_05018_131313 -s 20240101 -e 20260101

Expect JSON: {"live": true, "frame": "174A_05018_131313", "ifg_in_range": ...,
"epoch_span": "2024...", ...}  (matches the standalone probe from earlier).

## 3. Confirm both wrapper environments coexist
    docker compose run --rm --entrypoint python worker \
      -c "import rasterio; print('ndvi env ok')"
    docker compose run --rm --entrypoint /opt/conda/envs/licsbas/bin/python worker \
      -c "import LiCSBAS_io_lib; print('licsbas env ok')"

If all three pass, M3.1 is done: coverage-check live, both analysis
environments installed and isolated. M3.2 implements the full LiCSBAS run.
