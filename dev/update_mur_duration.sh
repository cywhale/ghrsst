#!/usr/bin/env bash
set -euo pipefail

PY="/home/odbadmin/.pyenv/versions/py314/bin/python"
ZARR="/home/odbadmin/Data/ghrsst/mur.zarr"
IDX="/home/odbadmin/Data/ghrsst/latest.json"

export ZARR IDX

"$PY" - <<'PYCODE'
import os, json
zarr = os.environ["ZARR"]
idx  = os.environ["IDX"]

dates=[]
for y in sorted(os.listdir(zarr)):
    yp=os.path.join(zarr,y)
    if not (y.isdigit() and len(y)==4 and os.path.isdir(yp)): continue
    for m in sorted(os.listdir(yp)):
        mp=os.path.join(yp,m)
        if not (m.isdigit() and len(m)==2 and os.path.isdir(mp)): continue
        for d in sorted(os.listdir(mp)):
            dp=os.path.join(mp,d)
            if d.isdigit() and len(d)==2 and os.path.isdir(dp):
                dates.append(f"{y}-{m}-{d}")

dates = sorted(set(dates))
if not dates:
    raise SystemExit("No date groups found")

obj  = {"earliest": dates[0], "latest": dates[-1]}
tmp  = idx + ".tmp"
with open(tmp,"w",encoding="utf-8") as f: json.dump(obj,f,ensure_ascii=False)
os.replace(tmp, idx)
print(f"wrote {idx} earliest={obj['earliest']} latest={obj['latest']}")
PYCODE
