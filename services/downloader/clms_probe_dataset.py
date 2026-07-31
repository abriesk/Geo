#!/usr/bin/env python3
"""Find where DatasetDownloadInformationID lives (the @search batch omitted it)."""
import sys, json
from clms_client import ClmsClient
UID_BASIC = "00f7264e92d54a70b02bce9b315d7b32"
c = ClmsClient.from_key_file(sys.argv[1] if len(sys.argv)>1 else "/run/secrets/clms_key.json")
for q in [
    f"/api/@search?portal_type=DataSet&UID={UID_BASIC}&metadata_fields=UID&metadata_fields=dataset_download_information",
    f"/api/@search?portal_type=DataSet&UID={UID_BASIC}&fullobjects=1",
]:
    r = c._get(q)
    print(f"\n=== {q[:70]}... -> {r.status_code} ===")
    if r.status_code == 200:
        items = r.json().get("items", [])
        if items:
            it = items[0]
            ddi = it.get("dataset_download_information")
            print("has dataset_download_information:", bool(ddi))
            if ddi:
                print(json.dumps(ddi, indent=1)[:1500])
            else:
                print("keys present:", list(it.keys())[:25])
        else:
            print("no items returned")
