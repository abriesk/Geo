# M4.2c — doc amendments (§5.4, §5.6, §6.4, §7) — FLOOD ROUTING

Doc-first per project rule. These amend geohazard-chat-technical-reference-v2.md.
The wrapper `wrap_floodpy.py` is already listed in §5.4's MVP wrappers; this
slice makes flood tasks actually REACH it, via a dedicated queue and the
separate flood-worker container built in M4.2a/b.

## Why a dedicated queue (same rationale as the M2.2 analysis/download split)
The flood-worker is a SEPARATE container (SNAP ~2 GB; M4.2a). If it consumed
`tasks.analysis`, it and the main worker would compete for each other's
messages — the main worker would grab flood tasks it cannot run (no SNAP/
FLOODPY), and the flood-worker would grab NDVI/InSAR tasks it cannot run. The
M2.2 precedent already established kind-split queues for exactly this reason.
So: a third task queue `tasks.flood`, consumed ONLY by flood-worker.

## §5.4 Worker — amend
Add after the existing tasks.analysis / tasks.download paragraph:

> The flood path runs in a SEPARATE flood-worker container (§4.1), because
> FLOODPY requires ESA SNAP (~2 GB) that would bloat the main worker image.
> flood-worker consumes a dedicated `tasks.flood` queue (single consumer,
> prefetch 1, dead-letters to tasks.dlq). Rationale mirrors the M2.2 kind-split:
> a container may only consume task kinds it can actually run. `wrap_floodpy`
> self-downloads its Sentinel-1 and ERA5 inputs (like wrap_licsbas self-
> downloads via LiCSBAS step 01), so no separate download task is enqueued for
> flood; the analysis task carries straight to flood-worker.

## §5.6 RabbitMQ policies — amend
Replace the queue list line with:

> - Queues `tasks.analysis`, `tasks.download`, `tasks.flood`, `progress`,
>   `results`: durable, manual ack. All three task queues dead-letter to
>   `tasks.dlq`.
> - Per-message retry limit 3 → dead-letter queue `tasks.dlq`.
> - Prefetch 1 on worker / downloader / flood-worker consumers.
> - `tasks.flood` has a SINGLE consumer (the flood-worker); flood runs are
>   heavy (SNAP preprocessing) and must not be parallelised within one box.

## §6.4 Task routing — amend
The backend routes an analysis task to a queue by wrapper name:

> Wrapper → queue routing:
> - `wrap_floodpy`      → `tasks.flood`
> - all other wrappers  → `tasks.analysis`
>
> This is the ONLY wrapper-name-dependent routing. The AnalysisTaskMessage
> contract (§6.4) is unchanged — the same message shape is published to a
> different queue. HAZARD_TO_WRAPPER["flood"] flips from the wrap_dummy
> placeholder to "wrap_floopdy" [sic — see note], enabling the flood hazard.

## §7 Error path — amend (per-step flood timeout)
Add:

> Flood tasks carry a wall-clock ceiling. Because SNAP preprocessing can wedge
> (observed M4.2b: a mis-sized run thrashed for 20 h before diagnosis), the
> flood-worker enforces FLOOD_STEP_TIMEOUT_S per FLOODPY phase and an overall
> FLOOD_TASK_TIMEOUT_S. On timeout the task fails deterministically → DLQ with
> NO retry (a wedged run will re-wedge; retrying 3× wastes hours). The failure
> is surfaced as a normal "failed" result so the query finalises with an honest
> message rather than hanging.

## §4.1 compose — amend
flood-worker gains the consumer role + broker/db env (built in M4.2a/b, was
CMD sleep infinity):

> flood-worker: consumes tasks.flood; env AMQP_URL, DATABASE_URL, RESULTS_ROOT,
> CDSE_*, GPTBIN_PATH, FLOODPY_PYTHON, FLOODPY_HOME, CDSAPI_RC; mem_limit 16g;
> depends_on broker healthy. CMD runs flood_worker_main.py.

## NOT changing
- AnalysisTaskMessage / ResultMessage / ProgressMessage schemas (§6.4) — reused as-is.
- result.json schema (§6.3).
- The /result endpoint path logic (already path-agnostic, M4.1.1).
