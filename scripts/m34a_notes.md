# M3.4a — answer quality + InSAR science calibration + frame-selection robustness

## Fixes
1. ANSWER DEDUP (llm.py): collapses a looped/repeated answer, strips splice preamble.
2. VELOCITY SIGNIFICANCE + ROBUST STATS (wrap_licsbas): reads vstd, gates on
   |vel|>1.96*vstd, reports p5/p95 (not raw min/max), significance-gated hotspots.
3. CONFIDENCE CALIBRATION (wrap_licsbas): data-quality vs method-certainty;
   short window OR mostly-noise => low, with prescriptive "use >=1yr / blank dates".
4. COVERAGE PRE-CHECK + CANDIDATE FALLBACK (wrap_licsbas + frames.py):
   the catalog ranks on SPATIAL overlap only, so it can pick a temporally-DEAD
   frame (processing stopped, 0 IFGs in window -> LiCSBAS01 IndexError). Now:
   - resolver returns MULTIPLE candidates per orbit (redundant frames KEPT, not
     pruned — a same-coverage frame may be dead)
   - wrap_licsbas probes each candidate with check_coverage (M3.1) and runs the
     first with IFGs in-window; skips dead ones
   - if ALL candidates are dead: clean no-data result (status=failed, exit 0 ->
     NO 3x retry), honest "no recent InSAR data for this area/period" answer

## Deploy (backend gets frames.py + llm.py; worker gets wrap_licsbas.py)
    cd /geo && tar xzf geohazard-chat-m3.4a.tar.gz
    docker compose up --build -d backend worker

## Test A: Yerevan GUI query (previously picked dead 072A) — should now WORK
    # submit "is the ground moving here?" for the Yerevan AOI, blank dates.
    # backend log: [router] resolved N frame(s): ['072A...','174A...']
    # worker log:  candidate 072A...: no data ... skipping
    #              selected frame 174A_05018_131313 (NNN interferograms in window)
    # then the real ~20-40 min run -> calibrated result.

## Test B: an area with NO recent data (e.g. the lon-21 area) — clean no-data
    # worker log: no candidate frame has data in window (...:0, ...:0)
    # answer: honest "no recent InSAR data", confidence low, NO 3x retry, fast.
