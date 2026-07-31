#!/usr/bin/env python3
"""flood_diagnostic.py — prove the SNAP + FLOODPY stack, one layer at a time.

None of this is testable in the author's sandbox (no ESA download, no CDS
account, no 2 GB Java toolbox), so it is built from real sources and verified
here on the box. Stages are ordered cheapest-first so a failure names ONE
layer instead of "flood doesn't work".

    python3 flood_diagnostic.py snap      # gpt runs and has the operators
    python3 flood_diagnostic.py env       # numpy/pandas ceilings hold
    python3 flood_diagnostic.py floodpy   # FLOODPY imports
    python3 flood_diagnostic.py creds     # CDS + CDSE credentials present
    python3 flood_diagnostic.py era5      # a real (tiny) ERA5 request
    python3 flood_diagnostic.py all       # everything, stopping at first failure

Run `snap` and `creds` first: they are fast and catch most setup problems.
"""
from __future__ import annotations

import os
import subprocess
import sys

# Every operator FLOODPY's own SNAP graphs invoke, extracted from
# floodpy/Preprocessing_S1_data/Graphs/*.xml. If any is missing, the installed
# SNAP lacks the Microwave toolbox and preprocessing will fail at runtime
# rather than at build time.
REQUIRED_OPERATORS = [
    "Apply-Orbit-File",
    "Calibration",
    "CreateStack",
    "Cross-Correlation",
    "Remove-GRD-Border-Noise",
    "SliceAssembly",
    "Speckle-Filter",
    "Subset",
    "Terrain-Correction",
    "ThermalNoiseRemoval",
    "Warp",
]

GPT = os.environ.get("GPTBIN_PATH", "/opt/snap/bin/gpt")
FLOODPY_PYTHON = os.environ.get("FLOODPY_PYTHON", "/opt/conda/envs/floodpy/bin/python")
FLOODPY_HOME = os.environ.get("FLOODPY_HOME", "/opt/FLOODPY")


def _missing_module(stderr: str):
    """Pull the module name out of a ModuleNotFoundError, so we report what is
    actually missing rather than guessing."""
    import re as _re
    m = _re.search(r"No module named '([^']+)'", stderr or "")
    return m.group(1) if m else None


# import name -> conda package name, where they differ
_CONDA_NAME = {
    "xrspatial": "xarray-spatial", "osgeo": "gdal", "skimage": "scikit-image",
    "sklearn": "scikit-learn", "dateutil": "python-dateutil",
    "netCDF4": "netcdf4", "eof": "sentineleof", "cv2": "opencv",
    "torch": "pytorch-cpu",
}


def ok(msg: str) -> None:
    print(f"  OK   {msg}")


def bad(msg: str) -> None:
    print(f"  FAIL {msg}")


def stage_snap() -> bool:
    print("== SNAP / gpt ==")
    if not os.path.exists(GPT):
        bad(f"gpt not found at {GPT}")
        return False
    ok(f"gpt present at {GPT}")

    try:
        r = subprocess.run([GPT, "-h"], capture_output=True, text=True, timeout=300)
    except Exception as e:  # noqa: BLE001
        bad(f"could not run gpt: {e}")
        return False
    if r.returncode != 0:
        bad(f"gpt -h exited {r.returncode}: {(r.stderr or '')[:300]}")
        return False
    ok("gpt runs (its bundled JRE works)")

    # `gpt -h` lists available operators; check the ones FLOODPY needs.
    listing = (r.stdout or "") + (r.stderr or "")
    missing = [op for op in REQUIRED_OPERATORS if op not in listing]
    if missing:
        bad(f"operators missing from this SNAP install: {missing}")
        print("       -> the Microwave/S1 toolbox is not installed. Rebuild with")
        print("          the 'all toolboxes' installer (esa-snap_all_linux-*.sh).")
        return False
    ok(f"all {len(REQUIRED_OPERATORS)} operators FLOODPY needs are available")

    vm = os.path.join(os.path.dirname(GPT), "gpt.vmoptions")
    if os.path.exists(vm):
        heap = [l.strip() for l in open(vm) if l.strip().startswith("-Xmx")]
        ok(f"gpt heap setting: {heap or 'default'}")
        print("       (must stay below the container's memory limit, or the JVM")
        print("        is OOM-killed mid-graph with a confusing error)")
    return True


def stage_env() -> bool:
    print("== FLOODPY python environment ==")
    if not os.path.exists(FLOODPY_PYTHON):
        bad(f"env python not found at {FLOODPY_PYTHON}")
        return False
    probe = r'''
import sys, numpy, pandas
print("python", sys.version.split()[0])
print("numpy", numpy.__version__)
print("pandas", pandas.__version__)
# The two ceilings FLOODPY's source requires.
print("HAS_NP_FLOAT", hasattr(numpy, "float"))
print("HAS_DF_APPEND", hasattr(pandas.DataFrame, "append"))
import scipy, skimage, xarray, rasterio, geopandas
print("scipy", scipy.__version__, "skimage", skimage.__version__)
print("xarray", xarray.__version__, "rasterio", rasterio.__version__)
'''
    r = subprocess.run([FLOODPY_PYTHON, "-c", probe], capture_output=True, text=True)
    if r.returncode != 0:
        bad(f"env probe failed: {(r.stderr or '')[:400]}")
        return False
    info = dict(
        line.split(" ", 1) for line in r.stdout.strip().splitlines() if " " in line
    )
    for k, v in info.items():
        print(f"       {k}: {v}")

    good = True
    if info.get("HAS_NP_FLOAT") != "True":
        bad("numpy has no np.float -> FLOODPY's threshold_Kittler WILL crash "
            "on the statistical path. numpy must be <1.24.")
        good = False
    else:
        ok("np.float available (threshold_Kittler can run)")
    if info.get("HAS_DF_APPEND") != "True":
        bad("pandas has no DataFrame.append -> FLOODPY plotting will crash. "
            "pandas must be <2.0.")
        good = False
    else:
        ok("DataFrame.append available")
    return good


def stage_floodpy() -> bool:
    print("== FLOODPY imports ==")
    if not os.path.isdir(FLOODPY_HOME):
        bad(f"FLOODPY not found at {FLOODPY_HOME}")
        return False
    commit = "unknown"
    cpath = "/opt/FLOODPY_COMMIT.txt"
    if os.path.exists(cpath):
        commit = open(cpath).read().strip()[:12]
    ok(f"FLOODPY present at {FLOODPY_HOME} (commit {commit})")

    # Statistical path first — it is what we actually depend on.
    stat = r'''
from floodpy.Floodwater_delineation.Statistical_approach.Classification import Calc_flood_map
from floodpy.Floodwater_delineation.Statistical_approach.calc_t_scores import Calculate_t_scores
from floodpy.Floodwater_delineation.Statistical_approach.Adaptive_thresholding import Adapt_local_thresholding
print("STAT_OK")
'''
    r = subprocess.run([FLOODPY_PYTHON, "-c", stat], capture_output=True, text=True,
                       cwd=FLOODPY_HOME)
    if "STAT_OK" not in r.stdout:
        bad(f"statistical modules failed to import: {(r.stderr or '')[:400]}")
        return False
    ok("statistical modules import (this is the path we use)")

    # FLOODPYapp is the orchestration layer. It pulls in the ViT module (hence
    # torch + torchvision) and slope masking (xrspatial) even though we use
    # neither. Treated as a WARNING, not a failure: everything we actually
    # depend on is in the statistical modules above, and M4.2b can drive those
    # directly if this import proves expensive to satisfy.
    app = "from floodpy.FLOODPYapp import FloodwaterEstimation; print('APP_OK')"
    r = subprocess.run([FLOODPY_PYTHON, "-c", app], capture_output=True, text=True,
                       cwd=FLOODPY_HOME)
    if "APP_OK" in r.stdout:
        ok("FLOODPYapp imports (orchestration usable)")
        return _check_param_contract()

    miss = _missing_module(r.stderr or "")
    if miss:
        pkg = _CONDA_NAME.get(miss, miss)
        print(f"  WARN FLOODPYapp cannot be imported: missing module {miss!r} "
              f"(conda package '{pkg}')")
    else:
        print(f"  WARN FLOODPYapp cannot be imported:\n"
              f"       {(r.stderr or '').strip()[-400:]}")
    print("       This is NOT fatal: the statistical modules we depend on")
    print("       imported fine. It only means M4.2b must call those directly")
    print("       instead of reusing FLOODPY's orchestration class.")
    return True


def stage_contracts() -> bool:
    """Can the wrapper actually WRITE a result?

    Added after a real run got all the way through ERA5 and then died on
    `ModuleNotFoundError: geohazard_contracts` — the image had never installed
    the contracts package. Every other service image does; this one was built
    from scratch and missed it. A stack that passes five stages and still
    cannot emit a result.json is not a working stack, so this now gets checked
    up front rather than discovered hours into a run.
    """
    print("== contracts (can we emit result.json?) ==")
    probe = r"""
import tempfile, os, json
from geohazard_contracts import ResultJson
import geohazard_contracts as gc
print("VERSION", gc.__version__)
r = ResultJson.model_validate({
    "query_id": "11111111-1111-1111-1111-111111111111",
    "method": "floodpy", "status": "failed", "summary_stats": {},
    "quality": {"scene_count": 0, "date_coverage": ["2023-07-01", "2023-09-30"],
                "coherence_mean": None, "masked_fraction": None,
                "cloud_fraction": None, "confidence": "low", "caveats": ["probe"]},
    "artifacts": [], "attribution": ["probe"]})
d = tempfile.mkdtemp()
open(os.path.join(d, "result.json"), "w").write(r.model_dump_json())
print("WRITE_OK", os.path.getsize(os.path.join(d, "result.json")))
"""
    r = subprocess.run([FLOODPY_PYTHON, "-c", probe], capture_output=True, text=True)
    if "WRITE_OK" in r.stdout:
        ver = ""
        for line in r.stdout.splitlines():
            if line.startswith("VERSION"):
                ver = line.split(" ", 1)[1].strip()
        ok(f"geohazard_contracts {ver} importable; a floodpy result.json validates")
        return True
    miss = _missing_module(r.stderr or "")
    if miss:
        bad(f"cannot import {miss!r} in the floodpy env")
        if miss == "geohazard_contracts":
            print("       -> the image must COPY libs/contracts and pip install it")
            print("          into the floodpy env, as the other services do.")
    else:
        bad(f"contracts probe failed:\n       {(r.stderr or '').strip()[-400:]}")
    return False


def _check_param_contract() -> bool:
    """Does our params_dict still match what FLOODPYapp actually requires?

    FLOODPY's notebook omits `flood_event`, which its constructor demands —
    following the notebook produces a bare KeyError deep inside FLOODPY. Rather
    than trusting either document, this parses FLOODPYapp.__init__ and compares
    the mandatory keys against the list wrap_floodpy supplies, so an upstream
    change shows up here instead of hours into a run.
    """
    import ast
    app_py = os.path.join(FLOODPY_HOME, "floodpy", "FLOODPYapp.py")
    wrap_py = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "wrap_floodpy.py")
    if not (os.path.exists(app_py) and os.path.exists(wrap_py)):
        print("  WARN could not locate FLOODPYapp.py / wrap_floodpy.py to compare")
        return True
    try:
        needed = []
        for node in ast.walk(ast.parse(open(app_py).read())):
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) \
               and node.value.id == "params_dict":
                try:
                    needed.append(ast.literal_eval(node.slice))
                except Exception:  # noqa: BLE001
                    pass
        needed = list(dict.fromkeys(needed))
        wrap_src = open(wrap_py).read()
        block = wrap_src.split("FLOODPY_REQUIRED_KEYS = (")[1].split(")")[0]
        ours = set(_re_findall_strings(block))
        missing = [k for k in needed if k not in ours]
        if missing:
            bad(f"FLOODPY now requires params we do not supply: {missing}")
            print("       -> add them to FLOODPY_REQUIRED_KEYS and params_dict")
            print("          in wrap_floodpy.py")
            return False
        ok(f"params contract matches FLOODPYapp ({len(needed)} required keys)")
    except Exception as e:  # noqa: BLE001
        print(f"  WARN could not verify the params contract ({e})")

    # Same idea for ATTRIBUTES. wrap_floodpy calls methods and reads fields on
    # the FLOODPY app object; the notebook advertises `flood_candidate_dates`,
    # which this FLOODPY never sets, and reading the notebook instead of the
    # source produced a run that silently did nothing. Verify every attribute
    # we touch actually exists.
    try:
        import re as _re
        provided = set()
        for node in ast.walk(ast.parse(open(app_py).read())):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
               and node.value.id == "self":
                provided.add(node.attr)
            if isinstance(node, ast.FunctionDef):
                provided.add(node.name)
        wrap_src = open(wrap_py).read()
        used = set(_re.findall(r"\bapp\.([A-Za-z_][A-Za-z_0-9]*)", wrap_src))
        # Names listed as tolerated alternatives are expected to be absent.
        tolerated = set(_re.findall(r'"([A-Za-z_0-9]+)"',
                                    wrap_src.split("_CANDIDATE_ATTRS = (")[1].split(")")[0])) \
            if "_CANDIDATE_ATTRS = (" in wrap_src else set()
        missing = sorted(a for a in used if a not in provided and a not in tolerated)
        if missing:
            bad(f"wrap_floodpy uses attributes this FLOODPY does not provide: {missing}")
            return False
        ok(f"attribute contract matches FLOODPYapp ({len(used)} used)")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  WARN could not verify the params contract ({e})")
        return True


def _re_findall_strings(block: str):
    import re as _re
    return _re.findall(r'"([A-Za-z_0-9]+)"', block)


def stage_creds() -> bool:
    """Check credentials the way the LIBRARIES resolve them, not the way we
    happen to have mounted them.

    The first version of this stage checked that a file existed at the path we
    chose and reported OK — while cdsapi was looking somewhere else entirely
    and failing. A credentials check that validates the wrong path is worse
    than no check, so this now replicates cdsapi's own resolution order and
    then asks the library itself to confirm.
    """
    print("== credentials ==")
    good = True

    # cdsapi/api.py:get_url_key_verify — CDSAPI_URL/KEY, else CDSAPI_RC, else ~/.cdsapirc
    probe = r"""
import os
url = os.environ.get("CDSAPI_URL")
key = os.environ.get("CDSAPI_KEY")
dotrc = os.environ.get("CDSAPI_RC", os.path.expanduser("~/.cdsapirc"))
print("RESOLVED_RC", dotrc)
print("RC_EXISTS", os.path.exists(dotrc))
print("ENV_URL", bool(url), "ENV_KEY", bool(key))
try:
    import cdsapi
    c = cdsapi.Client()
    print("CLIENT_OK", c.url)
except Exception as e:
    print("CLIENT_ERR", type(e).__name__, str(e)[:200])
"""
    r = subprocess.run([FLOODPY_PYTHON, "-c", probe], capture_output=True, text=True)
    out = r.stdout or ""
    for line in out.strip().splitlines():
        print(f"       {line}")
    if "CLIENT_OK" in out:
        ok("cdsapi resolves its credentials (client constructs)")
    else:
        rc = ""
        for line in out.splitlines():
            if line.startswith("RESOLVED_RC"):
                rc = line.split(" ", 1)[1].strip()
        bad(f"cdsapi cannot find credentials; it looks at: {rc or '~/.cdsapirc'}")
        print("       -> either mount the rc file AT that path, or point cdsapi at")
        print("          it with CDSAPI_RC. In compose:")
        print("             environment:")
        print("               CDSAPI_RC: /run/secrets/cdsapirc")
        print("             volumes:")
        print("               - ./secrets/cdsapirc:/run/secrets/cdsapirc:ro")
        good = False

    user = os.environ.get("CDSE_USERNAME")
    pwd = os.environ.get("CDSE_PASSWORD")
    if user and pwd:
        ok(f"CDSE credentials present in env (user {user[:3]}***)")
    else:
        bad("CDSE_USERNAME / CDSE_PASSWORD not set -> cannot download Sentinel-1")
        good = False
    return good


def stage_era5() -> bool:
    print("== ERA5 (small real request) ==")
    probe = r'''
import cdsapi, tempfile, os
c = cdsapi.Client()
out = os.path.join(tempfile.mkdtemp(), "era5.nc")
c.retrieve(
    "reanalysis-era5-single-levels",
    {"product_type": "reanalysis", "variable": "total_precipitation",
     "year": "2023", "month": "09", "day": "05",
     "time": ["00:00", "12:00"],
     "area": [40.0, 22.0, 39.8, 22.2],
     "format": "netcdf"},
    out)
print("ERA5_BYTES", os.path.getsize(out))
'''
    r = subprocess.run([FLOODPY_PYTHON, "-c", probe], capture_output=True, text=True)
    if "ERA5_BYTES" in r.stdout:
        size = r.stdout.split("ERA5_BYTES")[1].strip().split()[0]
        ok(f"ERA5 retrieval works ({size} bytes)")
        return True
    bad("ERA5 retrieval failed")
    print((r.stdout or "")[-600:])
    print((r.stderr or "")[-900:])
    print("       Common causes: token not yet activated; the dataset's licence")
    print("       has not been accepted on the CDS website (you must click")
    print("       'accept terms' once per dataset while logged in); or the")
    print("       request is queued (CDS can be slow at busy times).")
    return False


STAGES = {
    "snap": stage_snap,
    "env": stage_env,
    "floodpy": stage_floodpy,
    "contracts": stage_contracts,
    "creds": stage_creds,
    "era5": stage_era5,
}


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which != "all" and which not in STAGES:
        print(f"unknown stage {which!r}; choose from {list(STAGES)} or 'all'")
        return 2
    names = list(STAGES) if which == "all" else [which]
    for name in names:
        passed = STAGES[name]()
        print()
        if not passed:
            print(f"STOPPED at stage '{name}'. Fix this before the later stages "
                  "mean anything.")
            return 1
    print("All requested stages passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
