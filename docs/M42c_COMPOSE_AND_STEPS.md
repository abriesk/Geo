# M4.2c — compose service + apply steps

## flood-worker compose service (replace the M4.2a version)
The M4.2a service ran `sleep infinity`. It now consumes tasks.flood and needs
broker + db + results access. Full block:

```yaml
  flood-worker:
    build:
      context: .
      dockerfile: services/flood-worker/Dockerfile
      args:
        GPT_HEAP: 4G
    environment:
      SERVICE_ROLE: flood-worker
      AMQP_URL: amqp://geohazard:${RABBITMQ_DEFAULT_PASS}@broker:5672/%2F
      DATABASE_URL: postgresql://geohazard:${POSTGRES_PASSWORD}@db:5432/geohazard
      RESULTS_ROOT: /data/results
      CDSE_USERNAME: ${CDSE_USERNAME:-}
      CDSE_PASSWORD: ${CDSE_PASSWORD:-}
      GPTBIN_PATH: /opt/snap/bin/gpt
      FLOODPY_PYTHON: /opt/conda/envs/floodpy/bin/python
      FLOODPY_HOME: /opt/FLOODPY
      CDSAPI_RC: /run/secrets/cdsapirc
      FLOOD_TIMEOUT_S: ${FLOOD_TIMEOUT_S:-7200}
      # FLOODPY_RAM_GB: "4"        # optional; else auto-sized from mem_limit
    volumes:
      - ./data:/data
      - ./secrets/cdsapirc:/run/secrets/cdsapirc:ro
    mem_limit: 16g                 # < host 23G; _safe_cpu_ram budgets from this
    depends_on:
      broker:
        condition: service_healthy
    restart: unless-stopped
```

Note: no ports, and it does NOT depend on backend. It only needs broker (queue)
and db is passed for symmetry/future use. The `output_dir` in each task already
lands under /data/results/{query_id}/flood/wrap_floodpy (backend sets it).

## Apply order
1. Contracts (adds FLOOD_QUEUE = tasks.flood, declared with DLQ):
       tar xzf geohazard-chat-m4.2c.tar.gz     # includes libs/contracts/...queues.py
   Every service picks it up on rebuild; the queue is declared idempotently by
   whichever service starts first.

2. Backend routing (self-verifying patch — NEVER ship whole main.py):
       python3 scripts/m42c_backend_patch.py services/backend/app/main.py
   Expect: "Patched ... routing sites updated: 2". Re-running says "Already
   patched". It edits: import FLOOD_QUEUE; flood->wrap_floodpy;
   wrap_floodpy->tasks.flood at both publish sites.

3. Rebuild the changed services:
       docker compose build backend flood-worker
       docker compose up -d backend flood-worker

4. Verify the queue exists and flood-worker consumes it:
       docker compose exec broker rabbitmqctl list_queues name messages consumers | grep flood
       # tasks.flood should show 1 consumer
       docker compose logs --tail 5 flood-worker
       # "connected; consuming tasks.flood (single consumer, prefetch 1)"

## End-to-end test (the whole point)
Ask a flood question over the AOI that already works standalone:

```bash
curl -s -X POST http://localhost:8000/query -H 'content-type: application/json' -d '{
  "question": "Was there flooding here?",
  "aoi": {"type":"Polygon","coordinates":[[[21.82,39.35],[22.30,39.35],[22.30,39.65],[21.82,39.65],[21.82,39.35]]]},
  "dates": {"start":"2023-07-01","end":"2023-09-30"},
  "depth": "quick",
  "expert_raw": false
}'
# -> returns a query_id; poll /status/<id>
```

Watch it route to flood-worker, not the main worker:
    docker compose logs -f flood-worker    # should show the flood task + PROGRESS
    docker compose logs -f worker          # should NOT pick up this task

Success = status done, answer mentions the ~29 km2 flooded, artifact PNG served.

## Deterministic failure, by design (§7)
If wrap_floodpy times out (FLOOD_TIMEOUT_S) or exits nonzero, flood-worker
writes an honest failed result.json, publishes a failed result so the query
finalises, sends the task to tasks.dlq, and ACKs it — NO retry. Verified
offline across success / nonzero-exit / timeout / unparseable. Rationale: a
wedged SNAP run re-wedges; three retries burn hours (the M4.2b lesson).

## Backlog raised
- Per-PHASE flood timeouts (currently one whole-run ceiling).
