# M4.2b — rainfall event detection + wrap_floodpy

The flood ANALYSIS path, runnable standalone. Queue, consumer and backend
routing are M4.2c — deliberately split, because the wrapper is CLI-invokable
per the §5.3 contract and the first real run takes hours. Better to validate
the science before wiring plumbing around it.

## The design problem, and the answer
FLOODPY is event-driven: it wants a pre-flood baseline window and a flood
window and compares radar backscatter between them. Users ask "was there a
flood here?" over a period and cannot be expected to know the date of an event
they are asking about.

FLOODPY's own notebook resolves this by plotting ERA5 precipitation so a human
can eyeball the storm and type the date in. `floodpy_event.py` automates that
step: find the heaviest rainfall episode in the window, derive both windows
from it, and — crucially — **refuse to invent one**. A dry period returns "no
notable rainfall event", not a flood map of pure noise.

## The step the notebook leaves to a human
`sel_S1_data()` needs ONE date chosen from `flood_candidate_dates`. We pick the
earliest acquisition at or after the rainfall peak, because floodwater drains:
the first pass after the storm sees the most water.

**The latency between peak and pass is then the single most important number in
the answer**, and it drives confidence:

| latency | confidence |
|---|---|
| <= 1.5 days | high |
| <= 3 days | moderate |
| > 3 days | low |

A thin pre-flood baseline (<4 scenes) downgrades one further step: the t-score
reference is a mean over those images, and with three of them ordinary seasonal
variation is hard to distinguish from flooding.

**Why this matters more than it looks:** a radar pass four days after a severe
flood can legitimately find nothing. Reporting that as "no flooding" would be
the worst possible failure of this tool. The wrapper explicitly refuses to:
flooded area is always described as a LOWER BOUND, and a low-confidence zero
carries "this should not be read as evidence that no flooding occurred".

## Permanent water
Radar change detection will happily flag rivers and lakes. ESA WorldCover class
80 (permanent water) is excluded and reported separately as
`permanent_water_km2_excluded`. If the land-cover mask is unavailable the
caveats say rivers may be included and overstate the area — rather than
silently inflating the number.

## Verified in-sandbox
- Event detection on a Storm-Daniel-shaped series: peak 2023-09-06 (210 mm/day,
  425 mm/3-day), windows pre-flood 2023-07-07 -> 09-05, flood 09-05 -> 09-11.
- Dry period (1.2 mm/day for 90 days): correctly refuses, no windows produced.
- Sub-threshold rain (37 mm/3d): refused at the 40 mm default, accepted when
  the threshold is lowered to 30 — the screen is honest and tunable.
- Flood-date selection picks the first pass at/after the peak; when every
  acquisition predates the peak it reports NEGATIVE latency as the warning.
- Confidence matrix verified at all four corners; result.json validates against
  the §6.3 contract in every case.

A test caught a real flaw during the build: a 3-scene baseline with a prompt
pass was still reporting `high`, because the downgrade only handled `moderate`.
Fixed to downgrade a full step, with a caveat naming the scene count.

## Run it standalone on Thessaly (Storm Daniel, Sept 2023)
    cd /geo && tar xzf geohazard-chat-m4.2b.tar.gz
    docker compose build flood-worker
    docker compose up -d flood-worker

    cat > /tmp/thessaly.json <<'JSON'
    {"type":"Polygon","coordinates":[[[21.82,39.35],[22.30,39.35],[22.30,39.65],[21.82,39.65],[21.82,39.35]]]}
    JSON
    docker cp /tmp/thessaly.json $(docker compose ps -q flood-worker):/tmp/thessaly.json

    docker compose exec flood-worker /opt/conda/envs/floodpy/bin/python wrap_floodpy.py \
      --query-id 11111111-1111-1111-1111-111111111111 \
      --aoi /tmp/thessaly.json \
      --dates 2023-07-01,2023-09-30 \
      --input-dir /data/scratch \
      --output-dir /data/results/floodtest/flood/wrap_floodpy \
      --params '{}'

This is the AOI and period from FLOODPY's own notebook, so the run is
comparable to a known-good reference.

**It will take hours.** Several Sentinel-1 GRD scenes at ~1 GB each, then SNAP
preprocessing. Watch the PROGRESS lines; 30 -> 50 is the download, 50 -> 70 the
SNAP stack. Start it when you can leave it.

## Where I expect trouble (all untestable in my sandbox)
1. `flood_candidate_dates` — attribute name and value type are taken from the
   notebook. If it is absent or shaped differently, the run stops with a clear
   message at the "searching Sentinel-1" step.
2. `_scene_count` guesses among a few attribute names; worst case it reports 0,
   which only affects the thin-baseline downgrade.
3. Pixel-area units — handled for both degrees and metres, but worth sanity
   checking the reported km2 against the map.
4. RAM/CPU: FLOODPY's notebook used 20G/8CPU. We pass 6G/4 by default, under
   the 12g container limit alongside the 8G JVM heap. If SNAP is OOM-killed,
   lower `GPT_HEAP` rather than raising the container limit.

## Not in M4.2b
`tasks.flood` queue, the flood-worker consumer, and backend routing
(`HAZARD_TO_WRAPPER["flood"]`). That is M4.2c, with the §5.6/§6.4 doc amendment
for the new queue.

---

## M4.2b fix — two bugs from the first standalone run

The run reached ERA5, retrieved successfully, and then died. Both causes mine.

### 1. ERA5 downloaded fine, then failed to write
    [Errno 2] No such file or directory: '.../_era5/era5_precip.nc'
`cdsapi` writes straight to the target path and does not create parents.
`fetch_era5_daily` created a temp dir only when the caller passed nothing —
and wrap_floodpy always passes a path under the output dir, which did not exist
yet. Note how this presented: `status has been updated to successful`, then a
message reading "Rainfall data could not be retrieved from ERA5". The retrieval
was fine; the write was not. Fixed with `os.makedirs(out_dir, exist_ok=True)`.

### 2. The image never installed the contracts package
    ModuleNotFoundError: No module named 'geohazard_contracts'
Every other service image does `COPY libs/contracts` + `pip install -e`. This
image was built from scratch for SNAP and I omitted it, so wrap_floodpy could
never have written a result.json — success or failure path alike.

### The diagnostic should have caught #2, and now does
A stack that passes five stages and still cannot emit a result.json is not a
working stack. There is now a `contracts` stage that imports the package inside
the floodpy env and round-trips a real floodpy result.json through
`ResultJson.model_validate` and out to disk:

    docker compose exec flood-worker python3 flood_diagnostic.py contracts

That is the whole point of staged diagnostics — catching this in seconds rather
than hours into a Sentinel-1 download.

### Re-run
    cd /geo && tar xzf geohazard-chat-m4.2b-fix.tar.gz
    docker compose build flood-worker
    docker compose up -d flood-worker
    docker compose exec flood-worker python3 flood_diagnostic.py all   # now 6 stages
    # then the Thessaly command above

With the write fixed, the Thessaly window should now show Storm Daniel plainly
in the ERA5 totals — several hundred mm across 5-7 September.

---

## M4.2b fix 2 — the notebook is out of sync with FLOODPY's own constructor

**The event detection works on real data.** Live ERA5 over Thessaly returned:

    Heaviest rainfall centred on 2023-09-05 (128 mm that day, 311 mm over 3 days)

That is Storm Daniel, found automatically. FLOODPY's notebook describes "5-7
September 2023 Thessaly experienced extreme rainfall" — our detection matched a
documented event without being told it existed. The hardest design question of
this milestone is answered.

### The failure
    KeyError: 'flood_event'   (FLOODPYapp.py line 45)

I built `params_dict` from the notebook's cell 12. Parsing the constructor
shows it reads **20 keys** with bracket access — all mandatory — and the
notebook supplies only 19. `flood_event` is missing from it entirely, so
**FLOODPY's own notebook cannot construct FloodwaterEstimation at this commit.**
The key is used solely to name outputs:

    Flooded_regions_<flood_event>_<date>(UTC).nc

We now pass `event_YYYYMMDD` derived from the detected rainfall peak, which is
filesystem-safe and useful when reading a results directory later.

### The guard, because one missing key at a time is a bad way to work
- `wrap_floodpy` declares `FLOODPY_REQUIRED_KEYS` (all 20, extracted by parsing
  the constructor rather than copied from prose) and checks `params_dict`
  against it BEFORE constructing FloodwaterEstimation. A gap now fails with a
  named list instead of a bare KeyError from inside FLOODPY.
- The `floodpy` diagnostic stage **re-derives** the required keys from the
  installed `FLOODPYapp.py` and compares them to ours. If a FLOODPY upgrade
  adds a parameter, that shows up in seconds rather than hours into a run.

Verified: our list covers all 20; the built params_dict passes; removing
`flood_event` makes the guard fire with a clear message.

### Re-run
    cd /geo && tar xzf geohazard-chat-m4.2b-fix2.tar.gz
    docker compose build flood-worker && docker compose up -d flood-worker
    docker compose exec flood-worker python3 flood_diagnostic.py floodpy
    # expect: "params contract matches FLOODPYapp (20 required keys)"
    # then the Thessaly run again

Next stop is `query_S1_data()` / `flood_candidate_dates`, which I also took
from the notebook — so treat it with the same suspicion.

---

## M4.2b fix 3 — the notebook lies about the attribute name too

Third strike for FLOODPY's notebook as a reference.

### The silent exit
The run reached `PROGRESS 20 searching Sentinel-1 acquisitions` and returned to
the prompt with no error. Cause:

    def query_S1_data(self):
        self.query_S1_df, self.flood_datetimes = query_Sentinel_1(self)

The code sets **`flood_datetimes`**. The notebook prints
**`flood_candidate_dates`**, which this FLOODPY never assigns. So our
`getattr(app, "flood_candidate_dates", None)` returned None, the "no Sentinel-1
image" branch fired, wrote its result.json and returned — and because that
branch emitted no PROGRESS line, it looked exactly like a crash.

Two fixes:
- `_candidate_dates()` reads `flood_datetimes` first, still honours
  `flood_candidate_dates` for other versions, and **raises loudly** if neither
  exists rather than quietly concluding there are no images.
- The no-image branch now emits `PROGRESS 100` before returning.

Verified against FLOODPY's real shape (a list of `pd.Timestamp`): the chosen
value comes back as a Timestamp, which is what `sel_S1_data` needs since
`query_S1_df` is datetime-indexed.

### Generalising the guard
Having been caught three times by the same document, I stopped fixing
individually and audited every FLOODPY attribute the wrapper touches against
the installed source. Result: 11 attributes used, `flood_candidate_dates` the
only genuine miss — everything else (all the pipeline methods,
`Flood_map_dataset_filename`, `lc_mosaic_filename`) is real.

The `floodpy` diagnostic stage now checks BOTH contracts:
- **params** — the 20 keys `FloodwaterEstimation.__init__` requires
- **attributes** — every `app.<name>` the wrapper uses must exist in
  `FLOODPYapp`, with `_CANDIDATE_ATTRS` alternatives excluded from the check

So a FLOODPY upgrade that renames something is caught in seconds:

    docker compose exec flood-worker python3 flood_diagnostic.py floodpy
    # expect: "params contract matches FLOODPYapp (20 required keys)"
    #         "attribute contract matches FLOODPYapp (11 used)"

### Re-run
    cd /geo && tar xzf geohazard-chat-m4.2b-fix3.tar.gz
    docker compose build flood-worker && docker compose up -d flood-worker
    docker compose exec flood-worker python3 flood_diagnostic.py floodpy
    # then the Thessaly run

For Thessaly the peak was 2023-09-05 and there should be an acquisition on
09-06 — about 1.2 days later, which lands in "high" confidence. From here the
run continues into the Sentinel-1 download (PROGRESS 30-50) and SNAP
preprocessing (50-70), which is where the real time goes.

---

## M4.2b fix 4 — the 20-hour hang was memory thrashing, not a loop

htop told the whole story: ~15 separate `org.esa.snap...GPT` JVMs, each
`VIRT 20.2G RES 11.6G` (49% MEM), **swap 975M/976M full**, load average 8.5.
Not a loop — the box paging itself to death.

### Three compounding causes, all mine
1. **FLOODPY runs one gpt per interferogram pair IN PARALLEL** (its `CPU`
   param), each a multi-GB JVM. I read `RAM: "6G"` as a cap; it is the
   per-process heap. ~15 pairs x a big heap = far more than 23 GB.
2. **My Dockerfile set a SECOND 8G heap** (`-Xmx8G` in gpt.vmoptions) on the
   same JVMs — redundant and larger.
3. **FLOODPY passes no `-q`/`-c` to gpt**, so each JVM also tried to use every
   core and a huge tile cache, multiplying contention.

### The fix — three levers, no FLOODPY edits
- **`_safe_cpu_ram()`** sizes CPU (parallel gpt count) and RAM (heap) from the
  container's real cgroup memory limit, so `CPU * heap` always fits with
  headroom. On 23 GB it now picks **CPU=2 x 4G ~= 13 GB peak**, versus ~174 GB
  demanded before. Verified across 8/23/64 GB boxes; explicit `params.cpu/ram`
  still honoured but bounded.
- **gpt heap default lowered to 4G** and, crucially, `snap.parallelism=1` +
  a bounded tile cache in `snap.properties`, so each gpt uses one compute
  thread instead of fighting for all 20 cores.
- Removed the redundant second heap setting.

### Compose — set a real memory limit so the cgroup budget is honest
```yaml
  flood-worker:
    mem_limit: 16g        # _safe_cpu_ram reads this; must be < host RAM (23G)
    # optional explicit control, otherwise auto-sized:
    # environment: { FLOODPY_RAM_GB: "4" }
```
With `mem_limit: 16g` on your box the budgeter will pick CPU=2 x 4G (~13 GB
peak), leaving headroom for the OS. Do NOT set it near 23 GB — the point is to
leave room so the host never swaps.

### Re-run
    cd /geo && tar xzf geohazard-chat-m4.2b-fix4.tar.gz
    docker compose build flood-worker && docker compose up -d flood-worker
    # watch it stay sane this time:
    docker compose exec flood-worker python3 wrap_floodpy.py ... (Thessaly cmd)
    # in another shell, confirm at most CPU parallel gpts, no swap growth:
    docker compose exec flood-worker bash -lc 'ps -eo pcpu,rss,cmd|grep GPT|grep -v grep'
    free -g   # on the host: Swap used should stay near 0

The preprocessing genuinely does take a while (a real S1 stack), but you should
now see AT MOST 2 gpt processes at once, RES bounded, and swap flat. If it runs
clean, the flooded-area numbers and the map are the M4.2b finish line.

## Backlog raised by this
- **Per-step wall-clock timeouts** in the wrapper (M4.2c): even sized right, a
  wedged gpt must fail the task, not hang the worker. Same principle as the
  LiCSBAS coverage guard.

---

## M4.2b fix 5 — scene_count was 0 (cosmetic number, real side effect)

The Thessaly run SUCCEEDED — 29.4 km2 flooded, high confidence, valid map. But
`result.json` reported `scene_count: 0`, while the log clearly coregistered the
Sept-6 primary against 5 pre-flood secondaries (6 acquisitions).

`_scene_count` guessed attribute names (`S1_products`, `S1_dates`) that FLOODPY
does not use, and silently returned 0. FLOODPY holds the stack in
**`query_S1_sel_df`** (the query filtered to the chosen orbit). Fixed to read
that, with `query_S1_df` then `flood_datetimes` as fallbacks.

Why it mattered beyond cosmetics: the **thin-baseline confidence downgrade keys
off scene_count**, so a 0 disabled it — a run with too few baseline images
would not have been downgraded. Verified: 6 scenes -> high, 3 scenes ->
moderate, as intended.

This is a result-only change; no rebuild of SNAP/FLOODPY needed, just the
wrapper. The Thessaly numbers are otherwise confirmed sound.

## M4.2b status: COMPLETE
End-to-end on real data (Storm Daniel, Thessaly, Sept 2023):
- rainfall event auto-detected from ERA5 (peak 2023-09-05, 310 mm/3-day)
- flood image chosen 1.2 days after peak (high-confidence latency)
- 6-scene stack preprocessed through SNAP without thrashing
- 29.4 km2 flooded (localised, 2.15% of AOI), permanent water excluded
- contract-valid result.json + flood-extent PNG

Remaining for the flood HAZARD to be user-reachable: M4.2c — tasks.flood queue,
flood-worker consumer, backend routing, per-step timeouts.
