"""Run a REAL one-shot compaction in a clean process and report the first-party closure.

No test module is imported, so nothing another suite happens to load can be mistaken for
something the operation runs. Only `p5_fixtures` is borrowed to build synthetic stores, and it
lives under `tests/` which the filter drops.
"""
import datetime
import hashlib
import json
import os
import shutil
import socket
import sys
import tempfile
from unittest import mock

DEV = os.environ["DEV"]
sys.path.insert(0, DEV)
sys.path.insert(0, os.path.join(DEV, "tests"))

import p5_fixtures as fx  # noqa: E402
from ops import p5_oneshot_compaction as oc  # noqa: E402
from ops import p5_oneshot_exception as ex  # noqa: E402
from ops import p5_parity_producers as ppd  # noqa: E402
from ops import p5_prod_provision as prod  # noqa: E402
from ops import p5_readiness as rd  # noqa: E402
from ops import p5s8_evidence as evd  # noqa: E402
from store import repair_wal as rw  # noqa: E402

BASELINE = set(sys.modules)


def iso(offset=0):
    when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=offset)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


class _FakeDev:
    def __init__(self, real, dev):
        self._real, self.st_dev = real, dev

    def __getattr__(self, name):
        return getattr(self._real, name)


def dev_map(mapping):
    real_stat = os.stat
    resolved = {os.path.realpath(k): v for k, v in mapping.items()}

    def fake(path, *a, **kw):
        st = real_stat(path, *a, **kw)
        try:
            key = os.path.realpath(path)
        except (TypeError, ValueError):
            return st
        return _FakeDev(st, resolved[key]) if key in resolved else st

    return mock.patch.object(prod.os, "stat", side_effect=fake)


def main():
    tmp = tempfile.mkdtemp(prefix="p5-closure-")
    try:
        live = os.path.join(tmp, "G")
        anchor = os.path.join(tmp, "anchor")
        artifacts = os.path.join(tmp, "artifacts")
        for d in (live, anchor, artifacts):
            os.makedirs(d)
        rw.declare_anchor_domain(anchor, domain_id="closure-probe", at_utc=iso(),
                                 operator="probe", note="closure probe; not a real domain")
        evd.MINIMUM_FREE_GIB = 0.001

        base_days = fx.days_from("2026-01-01", 6)
        monolith = os.path.join(live, "mur_timecube_s8_t90_sh128.zarr")
        fx.build_block(monolith, base_days, ny=8, nx=8)
        grid = fx.days_from("2026-01-07", 90)
        days = grid[:5]
        delta = os.path.join(live, "mur_timecube_s8_t90_sh128.delta.zarr")
        fx.build_block(delta, days, ny=8, nx=8)

        locks = prod.canonical_lock_paths(live)
        manifest_root = os.path.join(live, prod.MANIFEST_DIRNAME)
        wal_root = os.path.join(live, prod.WAL_DIRNAME)
        host = socket.gethostname()
        sha = os.environ["APPROVED_OP_SHA"]

        def measured():
            return ex.measure_monolith(monolith)

        def record(run_id, operation):
            m = measured()
            return {
                "scope": ex.ONESHOT_SCOPE, "operation": operation, "host": host,
                "run_id": run_id, "monolith_realpath": m["monolith_realpath"],
                "monolith_metadata_fingerprint": m["monolith_metadata_fingerprint"],
                "monolith_day_digest": m["monolith_day_digest"],
                "monolith_content_ledger": dict(m["monolith_content_ledger"]),
                "backup_id": "probe", "backup_completed_utc": iso(-7200),
                "backup_verified": True, "backup_retention_ref": "probe retention ref",
                "expires_utc": iso(3000), "approved_by": "probe", "approved_utc": iso(-600),
                "reason": "closure probe run over synthetic stores only",
                "residual_writer_risk": {
                    "acknowledged": True, "unrecoverable_outcome_possible": True,
                    "statement": ("an unfenced writer can change the store during the "
                                  "interval and the outcome may be unrecoverable"),
                    "accepted_by": "probe"},
            }

        def fence():
            return {"method": "probe", "services_stopped": [{"name": "x", "posture": "stopped"}],
                    "cron_disabled": "disabled", "flock_is_advisory": True,
                    "arbitrary_writer_policy": "probe policy", "observed_utc": iso(-30),
                    "verified": True}

        def pre(run_id):
            m = measured()
            return {
                "host": host, "run_id": run_id, "collected_utc": iso(-60),
                "capacity": {"free_now_gib": 100.0, "filesystem_dev": os.stat(live).st_dev,
                             "measured_utc": iso(-60)},
                "healthz": {"status": "ok", "latest": days[-1], "route": "/point"},
                "identity": {"reviewed_sha": "d" * 40, "worktree_clean": True,
                             "verified": True, "approved": True,
                             "module_fingerprints": {"ops/p5_readiness.py": "e" * 64}},
                "quiescence": {"pm2": "stopped", "cron": "disabled", "readers": 0,
                               "reader_policy": "probe", "verified": True},
                "monolith_writers": {"count": 0, "method": "probe"},
                "writer_fence": fence(),
                "locks": {"compaction": "free", "ingest": "free", "verified": True,
                          "canonical_paths": {"compaction": os.path.realpath(locks["compaction"]),
                                              "ingest": os.path.realpath(locks["ingest"])},
                          "acquisition_probe": {"compaction": "acquired_and_released",
                                                "ingest": "acquired_and_released"}},
                "monolith_state": {k: m[k] for k in ex.MONOLITH_STATE_FIELDS},
                "backup": {"backup_id": "probe", "verified": True, "method": "probe",
                           "verified_utc": iso(-3600)},
                "plan": {"source_map": {d: "delta" for d in days},
                         "destination_manifest": "gen000002", "days": list(days),
                         "paths": {"monolith": os.path.realpath(monolith),
                                   "delta_path": os.path.realpath(delta),
                                   "daily_root": None,
                                   "artifacts_dir": os.path.realpath(artifacts)}},
            }

        os.chmod(monolith, 0o755)
        with dev_map({anchor: 4242}):
            prod.provision_production(
                live_root=live, anchor_root=anchor,
                approval={"scope": prod.PRODUCTION_SCOPE, "approved_by": "probe",
                          "approved_utc": iso(-600), "live_root": live, "run_id": "PROV"},
                monolith_path=monolith, compaction_lock_path=locks["compaction"],
                ingest_lock_path=locks["ingest"], run_id="PROV", operator="probe",
                at_utc=iso(), min_free_gib=0.001,
                one_shot_exception=record("PROV", ex.OP_PROVISION),
                one_shot_pre_compaction=pre("PROV"))

        identity = {
            "host": host, "run_id": "RUN", "deploy_sha": sha, "head_sha": sha,
            "worktree_clean": True,
            "module_fingerprints": {
                m: oc._file_digest(os.path.join(DEV, m))
                for m in sorted(set(oc.OPERATION_INVENTORY) | set(rd.INVENTORY_MODULES))
            },
            "collected_utc": iso(-30), "verified_by": "probe",
        }

        # A synthetic approval record in the probe's own tempdir -- outside the repository,
        # removed with it. The probe cannot measure the runtime closure of an operation that
        # refuses before it starts.
        approved_sha = os.environ["APPROVED_OP_SHA"]
        approved_repo = os.environ["APPROVED_OP_REPO"]
        approval = os.path.join(tmp, "operation_approval.json")
        with open(approval, "w") as fh:
            json.dump({"scope": oc.OPERATION_APPROVAL_SCOPE,
                       "approved_sha": approved_sha,
                       "approved_by": "closure probe",
                       "approved_utc": iso(-600),
                       "approved_modules": list(oc.OPERATION_INVENTORY),
                       "note": "PROBE FIXTURE. Not an operational approval."}, fh)
        real_inv, real_id = oc.operation_inventory_problem, oc.identity_problem

        def bound_inv(**kw):
            kw.setdefault("repo_root", approved_repo)
            kw.setdefault("approval_path", approval)
            return real_inv(**kw)

        def bound_id(artifact, **kw):
            kw.setdefault("repo_root", approved_repo)
            return real_id(artifact, **kw)

        before = set(sys.modules)
        with dev_map({anchor: 4242}), \
                mock.patch.object(oc, "OPERATION_APPROVAL_PATH", approval), \
                mock.patch.object(oc, "operation_inventory_problem", side_effect=bound_inv), \
                mock.patch.object(oc, "identity_problem", side_effect=bound_id), \
                mock.patch.object(rd, "REVIEWED_DEPLOY_SHAS", (approved_sha,)):
            oc.compact_one_shot(
                live_root=live, manifest_root=manifest_root, wal_root=wal_root,
                anchor_root=anchor, monolith_path=monolith, delta_path=delta,
                compaction_lock_path=locks["compaction"], ingest_lock_path=locks["ingest"],
                approval={"scope": oc.COMPACTION_SCOPE, "approved_by": "probe",
                          "approved_utc": iso(-600), "live_root": live, "run_id": "RUN"},
                one_shot_exception=record("RUN", ex.OP_COMPACTION),
                one_shot_pre_compaction=pre("RUN"),
                run_id="RUN", operator="probe", at_utc=iso(), min_free_gib=0.001,
                start_day=grid[0], end_day=grid[-1], classification_target=list(days),
                artifacts_dir=artifacts, hard_reserve_bytes=1024,
                block_out_path=os.path.join(manifest_root, "block.zarr"),
                identity_artifact=identity,
                writer_fence_probe=fence,
                point_series_parity=ppd.point_series_parity,
                api_parity=ppd.api_parity)
        after = set(sys.modules)

        dev_real = os.path.realpath(DEV)
        venv = os.path.join(dev_real, ".venv")

        def rel(name):
            f = getattr(sys.modules.get(name), "__file__", None)
            if not f:
                return None
            r = os.path.realpath(f)
            if not r.startswith(dev_real + os.sep) or r.startswith(venv):
                return None
            p = os.path.relpath(r, dev_real)
            return None if p.startswith("tests/") else p

        print(json.dumps({
            "closure": sorted({p for m in after if (p := rel(m))}),
            "lazy_during_run": sorted({p for m in (after - before) if (p := rel(m))}),
        }))
    finally:
        try:
            os.chmod(os.path.join(tmp, "G", "mur_timecube_s8_t90_sh128.zarr"), 0o755)
        except OSError:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


main()
