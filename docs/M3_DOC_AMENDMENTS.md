# Doc amendments to paste into geohazard-chat-technical-reference-v2.md

Two architectural facts M3 introduced. Paste into the relevant sections.

## §5.2 addition (frame resolution)

> **InSAR frame resolution.** AOI->frame resolution for the deformation path
> uses a static global catalog (`data/licsar_frames.geojson`, ~2,600 frames)
> built once by `scripts/build_licsar_catalog.py` from each frame's geocoded
> `geo.U.tif` bounds (a bounding-box approximation; the true tilted footprint
> lives in COMET's server-side LiCSInfo database and is not publicly
> available). At routing time `libs/licsar/frames.py` computes equal-area
> (EPSG:6933) AOI overlap and returns ranked candidate frame(s), keeping
> spatially-redundant frames because a same-coverage frame may be temporally
> dead (no recent interferograms). `wrap_licsbas` then probes candidates for
> in-window interferograms and runs the first with data, emitting an honest
> "no recent InSAR data" result if none qualify. `DEFAULT_DEFORM_FRAME`
> remains as a fallback when the catalog is absent or misses. Catalog refresh
> is an occasional offline job (frames change slowly).

## §5.3 addition (self-downloading InSAR path)

> **The InSAR/deformation path is a self-downloading wrapper.** `wrap_licsbas`
> acquires LiCSAR products via LiCSBAS step 01 within the worker, rather than
> through the downloader service. The download/analysis split described in
> this section applies to the optical (Sentinel-2) path only. Rationale:
> LiCSBAS is designed as an integrated pipeline whose step 01 owns
> acquisition; forcing it through the downloader would duplicate maintained
> upstream code and fragile portal-URL handling. Consequence: InSAR products
> are not yet shared through the §6.2 cache — see BACKLOG "LiCSBAS
> interferogram caching".

## §6.3 note (InSAR confidence semantics — for when NO_DATA / NDVI cap land)

> The deformation quality block separates DATA QUALITY (coherence, masked
> fraction, scene count) from METHODOLOGICAL CERTAINTY (time span, fraction of
> pixels with statistically significant velocity, |v| > 1.96*vstd). Reported
> confidence is the weaker of the two axes; caveats name which is limiting.
> Velocity spread is reported as robust p5/p95, not raw min/max, and hotspots
> require both magnitude and significance.
