# M4.1c backend edit — results filed under the wrong hazard

SHIPPED AS AN INSTRUCTION, NOT A FILE: my sandbox copy of main.py is stale
(it still had "vegetation": "wrap_dummy" where the repo has "wrap_ndvi"), so
overwriting the file would have regressed NDVI to the dummy wrapper. These two
edits are surgical.

## The bug
A Paris EGMS query wrote its result to  .../results/<qid>/VEGETATION/result.json
The wrapper was right (wrap_egms ran); the output directory was wrong.
_publish_pending_analyses hardcodes  hazard = "vegetation"  because vegetation
used to be the only download-gated hazard (M2.2). EGMS is the second, so it
inherited the wrong hazard.

## Edit 1 — add the reverse map next to HAZARD_TO_WRAPPER
FIND:
    DEPTH_MAX_METHODS = {"quick": 1, "standard": 2, "thorough": len(HAZARD_TO_WRAPPER)}

INSERT IMMEDIATELY AFTER IT:

    # Reverse lookup for the download->analysis handoff. Cannot be derived by
    # inverting HAZARD_TO_WRAPPER: deformation maps to two wrappers depending on
    # which tier the ladder picked (EGMS in Europe, LiCSBAS elsewhere).
    WRAPPER_TO_HAZARD = {
        "wrap_licsbas": "deformation",
        "wrap_egms": "deformation",
        "wrap_ndvi": "vegetation",
        "wrap_floodpy": "flood",
        "wrap_dummy": "vegetation",
    }

## Edit 2 — derive the hazard per task in _publish_pending_analyses
FIND (inside _publish_pending_analyses, just before `import pika`):

    hazard = "vegetation"  # M2.2: sole download-gated hazard; M3 generalizes
    import pika

REPLACE WITH:

    import pika

THEN, inside the `for row in pending:` loop, add as its FIRST line:

        hazard = WRAPPER_TO_HAZARD.get(row["name"], "vegetation")

(the loop body already uses `hazard` for output_dir and params, so nothing else
changes.)

## Verify
    docker compose up --build -d backend
    # re-run a Paris deformation query, then:
    ls data/results/<query_id>/
    # expect:  deformation/    (NOT vegetation/)
