# M4.1c — EGMS science correction + routing fix

The first live EGMS run completed end-to-end but produced a WRONG answer:
"subsiding, high confidence, 99% of points significant" for central Paris.
Every number was inside the service's own accuracy envelope.

## Root cause
`mean_velocity_std` in the L3 CSV is the FORMAL error of the linear trend fit.
With ~300 epochs it is tiny, and the file quantises it to one decimal — measured
on the real Paris tile: min=0, median=0, p95=0.1, max=0.2. So the test
`|v| > 1.96*std` passed for nearly every point: an artifact of dividing by
approximately nothing, not evidence of ground motion.

## Fix: accuracy floor
Published validation of EGMS against levelling/GNSS finds agreement with ground
truth typically within 1-2 mm/yr, but explicitly not universally — differences
of 5-10 mm/yr occur at some sites. So:

    sigma_eff = sqrt(fit_std^2 + EGMS_ACCURACY_MM_YR^2)     # default 1.5 mm/yr
    significant  <=>  |v| > 1.96 * sigma_eff

and a trend direction is only claimed when |AOI mean| exceeds that floor.
On the real Paris data (-1.04 mm/yr, range -1.6..-0.12) this now reports
STABLE with zero significant points, and says so plainly.

## Fix: "stable" vs "cannot tell" vs "mixed"
Three states that were previously collapsed:
- Dense data, everything inside the floor -> CONFIDENT statement of stability.
  (Low significance must NOT tank confidence — a well-measured null is a real
  finding.)
- Points moving in BOTH directions with a near-zero mean -> trend="mixed", with
  a caveat, because differential motion across a small area matters more than a
  uniform trend and an average of zero hides it completely.
- Wide scatter with nothing individually resolvable -> confidence downgraded.

## Verified (4 scenarios, synthetic tiles with REAL quantised std values)
| scenario | trend | sig_frac | confidence |
|---|---|---|---|
| Paris real range (-1.6..-0.1) | stable | 0.00 | high |
| genuine subsidence -12 mm/yr | subsiding | 1.00 | high |
| differential +/-10 mm/yr, mean 0 | mixed | 0.62 | high |
| sparse (900 m spacing, 45 pts) | stable | 0.00 | low |

Real subsidence is still detected; the correction is not a blanket suppression.

## Also fixed
- Results filed under the wrong hazard directory (see m41c_backend_edit.md).

## Still open (backlog)
- Tile-level caching: the cache key is AOI-hash + date range, but EGMS returns a
  FIXED release (2020-2024) regardless of the requested window, so every distinct
  window re-downloads the same 81 MB tile. Key by tile filename instead.
- The zip ships a 4 MB GeoTIFF that could render the map far more cheaply than
  scatter-plotting ~4k points from the 340 MB CSV.
- EGMS allows only 2 concurrent downloads (429); handled by retry, but a
  dedicated semaphore would be cleaner.
