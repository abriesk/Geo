# M3.3 — global LiCSAR frame catalog + automated resolution

Retires the manual DEFAULT_DEFORM_FRAME env var: deformation queries now
resolve their own frame(s) from a shipped global catalog.

## 1. Build the catalog (ONE TIME, offline — heavy, hours for full 175)
Runs in the licsbas env (needs GDAL). Copy the builder into the worker image
first, OR run it via the licsbas python with the script mounted.

Quick pipeline test on a few tracks (~1-2 min) BEFORE the full sweep:
    docker compose run --rm --entrypoint /opt/conda/envs/licsbas/bin/python \
      -v "$PWD/scripts:/scripts" -v "$PWD/data:/data" worker \
      /scripts/build_licsar_catalog.py --out /data/licsar_frames.geojson \
      --tracks 174,175,6 --workers 16
    # expect: data/licsar_frames.geojson with a few dozen frames; check it:
    python3 -c "import json;d=json.load(open('data/licsar_frames.geojson'));print(d['properties'])"

Full global sweep (all 175 tracks — resumable via checkpoints, run when ready):
    docker compose run --rm --entrypoint /opt/conda/envs/licsbas/bin/python \
      -v "$PWD/scripts:/scripts" -v "$PWD/data:/data" worker \
      /scripts/build_licsar_catalog.py --out /data/licsar_frames.geojson --workers 16
    # resumable: re-running skips finished tracks (checkpoints in data/_catalog_ckpt/).
    # Tune --workers up/down based on throughput; JASMIN tolerates concurrency.

Commit data/licsar_frames.geojson so end users get it without crawling.

## 2. Deploy backend with the resolver
    docker compose up --build -d backend      # bakes libs/licsar + pyproj
    # verify resolution works against the built catalog:
    docker compose exec backend python -c "
import sys; sys.path.insert(0,'/libs')
from licsar.frames import find_licsar_frames
aoi={'type':'Polygon','coordinates':[[[44.45,40.25],[44.60,40.25],[44.60,40.12],[44.45,40.12]]]}
print(find_licsar_frames(aoi, catalog_path='/data/licsar_frames.geojson'))"
    # expect: the 174A Yerevan frame (+ any descending frame covering it)

## 3. End-to-end: a deformation query now self-resolves its frame
    # (with DEFAULT_DEFORM_FRAME UNSET, to prove the catalog is doing the work)
    # submit "is the ground moving here?" for the Yerevan AOI -> backend log:
    #   [router] resolved N InSAR frame(s): ['174A_05018_131313', ...]
    # DEFAULT_DEFORM_FRAME remains as a fallback if the catalog misses/absent.
