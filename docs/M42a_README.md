# M4.2a — flood-worker: ESA SNAP + FLOODPY dependency stack

Proves the heaviest, least-testable dependency BEFORE writing the wrapper —
same order that de-risked LiCSBAS in M3.1. No queue consumer yet; that is M4.2b.

## Why a separate container
The main worker already carries two conda envs. SNAP is a ~2 GB Java toolbox;
adding it would push that image past 5 GB and make every unrelated rebuild
painful. `flood-worker` keeps it isolated.

## What was verified from real sources (not assumed)
| Thing | Source |
|---|---|
| SNAP installer URL + unattended flags `-q -dir` | ESA download page + 3 independent Dockerfiles |
| SNAP 10 path quirk `/step/snap/10_0/` (underscore, unlike 9/11/12) | ESA previous-versions page |
| The 11 SNAP operators FLOODPY needs | its own `Preprocessing_S1_data/Graphs/*.xml` |
| numpy < 1.24 required | `Thresholding_methods.py` uses `np.float` (removed in 1.24), called from `Adaptive_thresholding.py:184` |
| pandas < 2.0 required | `Visualization/plotting.py` uses `DataFrame.append` |
| torch needed only for an import | `FLOODPYapp.py` imports the ViT module at module level |

**Note on FLOODPY's own env file:** `FLOODPY_gpu_env.yml` pins numpy 1.24.3,
which its own `threshold_Kittler` would crash on, and drags in CUDA + GPU
pytorch we never use. Hence the hand-built CPU env with the two real ceilings.

## SNAP version choice
Pinned to **10.0.0** because FLOODPY documents it and drives SNAP through XML
graphs whose operators shift between major versions (the toolboxes were renamed
after 10). Current SNAP is 13.0.0 — moving up is a deliberate experiment:
    docker compose build --build-arg SNAP_URL=<12 or 13 url> flood-worker

## Install
Add to `docker-compose.yml`:

```yaml
  flood-worker:
    build:
      context: .
      dockerfile: services/flood-worker/Dockerfile
      args:
        GPT_HEAP: 8G          # keep below mem_limit below
    env_file: .env
    environment:
      GPTBIN_PATH: /opt/snap/bin/gpt
      FLOODPY_PYTHON: /opt/conda/envs/floodpy/bin/python
      FLOODPY_HOME: /opt/FLOODPY
    volumes:
      - ./data:/data
      - ./secrets/cdsapirc:/run/secrets/cdsapirc:ro
    mem_limit: 12g            # gpt heap 8G + JVM overhead + python
    restart: unless-stopped
```

Put the CDS token at `./secrets/cdsapirc`:

```
url: https://cds.climate.copernicus.eu/api
key: <Personal Access Token from your CDS profile>
```

Then (this build is LONG — ~1 GB SNAP download plus a conda solve):

    docker compose build flood-worker
    docker compose up -d flood-worker

## Bring it up in stages — a failure names ONE layer
    docker compose exec flood-worker python3 flood_diagnostic.py snap
    docker compose exec flood-worker python3 flood_diagnostic.py env
    docker compose exec flood-worker python3 flood_diagnostic.py floodpy
    docker compose exec flood-worker python3 flood_diagnostic.py creds
    docker compose exec flood-worker python3 flood_diagnostic.py era5
    # or: ... flood_diagnostic.py all

- `snap` — gpt runs on its bundled JRE and has all 11 operators FLOODPY's
  graphs invoke. Missing operators = wrong installer variant.
- `env` — asserts the numpy/pandas ceilings actually hold, by testing for
  `np.float` and `DataFrame.append` directly rather than trusting version
  strings.
- `floodpy` — statistical modules first (what we depend on), then FLOODPYapp
  (which needs torch present merely to import).
- `creds` — `.cdsapirc` and CDSE credentials.
- `era5` — a real, tiny ERA5 request. **Expect friction here:** CDS makes you
  accept each dataset's licence once, while logged in, before the API will
  serve it; requests can also queue.

## Where I expect iteration (all untestable in my sandbox)
1. **The conda solve.** LiCSBAS needed pins relaxed and pip installs split.
   numpy 1.23 + numba 0.57 + scikit-image 0.20 is a coherent set, but
   conda-forge may still argue. If it does, paste the solver output.
2. **The SNAP installer.** `-q -dir` is well attested, but install4j sometimes
   wants `-varfile`. If `-q` fails, that is the fallback.
3. **ERA5 licence acceptance** — near-certain one-time friction, not a bug.
4. **gpt heap vs container memory** — 8 G heap under a 12 G limit. If the JVM
   is OOM-killed mid-graph, lower `GPT_HEAP` rather than raising the limit.

## Runtime network egress this container will need (M4.2b)
CDSE (Sentinel-1), CDS (ERA5), ESA/Copernicus (precise orbits via sentineleof),
a DEM source for Terrain-Correction, and ESA WorldCover. Worth knowing before
it runs on a restricted network.

## Not in M4.2a
Queue consumer, `wrap_floodpy`, contract, routing. The container currently
sleeps; it exists to prove the stack. That is M4.2b.

---

## M4.2a fix — env completion (after first run on the box)

First run: SNAP passed with all 11 operators, both version ceilings held
(numpy 1.23.5 with np.float, pandas 1.5.3 with DataFrame.append), statistical
modules imported. `import FLOODPYapp` failed on `No module named 'xrspatial'`.

Cause: I hand-built the env and missed packages. Rather than fix one and hit
the next, I walked FLOODPY's **transitive import graph** from FLOODPYapp and
compared it against the env. Exactly six were missing:

| import | conda package | why |
|---|---|---|
| `xrspatial` | **xarray-spatial** | slope masking in DEM_funcs — the actual failure |
| `torchvision` | **torchvision** | ViT module imports it alongside torch |
| `branca`, `xyzservices` | same | folium's real deps, not guaranteed transitively |
| `dateutil` | python-dateutil | pandas dep, made explicit |
| `mpl_toolkits` | (matplotlib) | false positive — part of matplotlib |

Deliberately NOT added:
- **pygrib** — used only by `Download_GFS_precipitation` (GFS forecasts). We
  use ERA5 via cdsapi, so it would pull eccodes for nothing.
- **seaborn** was added anyway (validation plotting, cheap).

## Two changes to the diagnostic
1. It now **names the missing module** by parsing the ModuleNotFoundError and
   maps it to its conda package name. The first version guessed "usually
   torch", which was wrong here and would have sent you down the wrong path.
2. `import FLOODPYapp` is now a **WARNING, not a failure**. Everything we
   actually depend on lives in the statistical modules, which imported cleanly.
   FLOODPYapp is only FLOODPY's orchestration class; if torch/torchvision turn
   out to drag in CUDA builds, we drop both and drive the statistical functions
   directly in M4.2b. That decision stays open rather than blocking the stack.

## Re-run
    cd /geo && tar xzf geohazard-chat-m4.2a-fix.tar.gz
    docker compose build flood-worker      # conda solve only; SNAP layer is cached
    docker compose up -d flood-worker
    docker compose exec flood-worker python3 flood_diagnostic.py all

Watch the image size: if `torchvision` pulls a CUDA build, the image will jump
by ~2 GB. If so, say the word and we drop torch entirely — it costs us nothing
except FLOODPY's orchestration class.

---

## M4.2a fix 2 — cdsapi could not see the mounted credentials

Second run: SNAP, env, and ALL FLOODPY imports passed (xarray-spatial +
torchvision resolved it, and FLOODPYapp now imports). ERA5 failed with:

    Exception: Missing/incomplete configuration file: /root/.cdsapirc

This was a wiring bug of mine, not a CDS problem. `cdsapi` resolves credentials
in this order (verified in `cdsapi/api.py:get_url_key_verify`):

1. `CDSAPI_URL` + `CDSAPI_KEY` environment variables
2. the path in `CDSAPI_RC`
3. `~/.cdsapirc`

We mounted the file at `/run/secrets/cdsapirc` and never told cdsapi about it,
so it looked at `/root/.cdsapirc` and found nothing.

**Fix:** `ENV CDSAPI_RC=/run/secrets/cdsapirc` is now baked into the image, so
the existing compose mount just works. No compose change needed — though
setting it explicitly there too is harmless and self-documenting.

**The worse half of this bug was my diagnostic.** Its creds stage reported
`OK  .cdsapirc found at /run/secrets/cdsapirc` — checking a path *I* had
chosen rather than the one the library actually reads. A false pass sent the
failure one stage downstream where it looked like a CDS problem. The stage now
replicates cdsapi's resolution order and then asks `cdsapi.Client()` itself to
confirm, so it can only pass if the real library is genuinely satisfied.

## Re-run
    cd /geo && tar xzf geohazard-chat-m4.2a-fix2.tar.gz
    docker compose build flood-worker     # only the ENV layer changes; fast
    docker compose up -d flood-worker
    docker compose exec flood-worker python3 flood_diagnostic.py creds
    docker compose exec flood-worker python3 flood_diagnostic.py era5

If `creds` passes but `era5` still fails, THAT is where the real CDS friction
lives: the licence for `reanalysis-era5-single-levels` must be accepted once,
while logged in, at cds.climate.copernicus.eu before the API will serve it.
