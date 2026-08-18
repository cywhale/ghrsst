# P5-S5 Part 3 — the `(e2)` cross-tier question: RESULTS

Status: **PARTIAL.** `(e2)` **protocol design and mechanism are proven**; **production closure
is PARTIAL**, pending ops evidence that the deployed quiescence hook attests a drain it actually
verified. The crash-boundary matrix is **NOT YET DELIVERED**, and **G10 / H4 / G17 are still NOT
claimed.**
`[MUT-STG]`: staging/synthetic only. No VM24 path, no `GHRSST_*` store, no cron, nothing
written outside a temp dir.

Branch `dev2026-p5-s5-part3-crash-proof`, stacked on `3c37193` (Part 2, signed off).

- Harness: [`../tests/test_phase2_p5s5_part3.py`](../tests/test_phase2_p5s5_part3.py) — **30/30 green**
- Changed: [`../ingest/swap_delta.py`](../ingest/swap_delta.py) — §7.9 quiescence is required, must prove itself, and is recorded
- Changed: [`p4s10_production_rollout_runbook.md`](p4s10_production_rollout_runbook.md) — the deployed hook, updated to the new contract
- **Unchanged: [`../store/tiered_cube.py`](../store/tiered_cube.py).** No signed-off P5-S2 behaviour was touched — not even the docstring, which still records R1 as "adjudicated at S5/G10". Amending it to point at this verdict is a one-line follow-up **after** sign-off, not something to slip in alongside the evidence.
- Full local suite: **803 tests OK** (17 skipped), up from 773
- `-W error::ResourceWarning` over S4 + S5 Parts 1–3 + P4-S8a: **366 OK**
- **11 guards mutation-verified**

## The question

`TieredCube.snapshot()` guarantees one stable snapshot **per tier**, not one instant across
both (P5-S2, risk R1). Its docstring names the single pair that loses data — a **pre-publish
base with a post-prune delta** — and asserts it cannot reach a reader, because §7.5 orders
publish → verify → prune and the prune's path switch runs under request quiescence.

The instruction for this round was not to build a fence and not to invoke §14, but to find out
whether the dangerous combination can cross the real production protocol. It was answered
against the real `publish_manifest`, `prune_delta` and `execute_swap_plan` paths, with every
interleaving forced by `threading.Event`. There is no `sleep` in the suite: a failure is a
failure, not a slow machine.

## The four states

| base | delta | verdict | test |
|---|---|---|---|
| old | old | **safe** — delta serves the day | `test_1_old_base_old_delta_is_safe` |
| new | old | **safe** — delta-wins, and delta holds the corrected copy | `test_2_new_base_old_delta_is_safe_and_delta_wins` |
| new | pruned | **safe** — base serves the folded day | `test_3_new_base_pruned_delta_is_safe` |
| **old** | **pruned** | **the only dangerous combination** | `test_4_old_base_pruned_delta_loses_data_in_BOTH_shapes` |

The danger has **two shapes**, and they fail differently, so both are tested:

- a day folded into base for the **first** time sits in neither tier — `point_series` omits
  days nobody has, so the row **vanishes** from the response;
- a day that was **corrected** in delta is still present in the old base at its **stale
  value** — the read succeeds and is silently wrong. This is the worse of the two, and it is
  specific to the corrected-day lifecycle Part 2 built: nothing in the response says the value
  is the one the repair existed to replace.

`test_4` measures both against the value the day was corrected *away from*, rather than
asserting that a bad thing "would" happen.

## Is it reachable? Yes — and that is the point

`TieredCube.refresh()` refreshes base, then delta. Both reads are individually correct, and the
composite lock makes them atomic against other **snapshots** — but not against the
**filesystem**. A swap landing between the two reads hands the reader exactly the losing pair.

`test_the_dangerous_pair_IS_reachable_when_a_refresh_spans_the_swap` parks a real
`cube.refresh()` at that boundary, runs the real refold → publish → prune → swap, then releases
it. The reader emerges holding generation 1 with a pruned delta: the folded day is gone and the
repaired day reads at its stale value.

So the invariant is **not** provided by the snapshot mechanism, exactly as the P5-S2 docstring
says. It rests entirely on **nothing being in flight across the swap**. That is what makes
quiescence load-bearing rather than operational hygiene — and it is why the fix this round is in
the swap protocol, not in the read path.

## The reviewer's four points

| # | requirement | status | evidence |
|---|---|---|---|
| 1 | after publish, before prune, an in-flight request still reads old base + old delta | **PASS** | `test_after_publish_before_prune_an_in_flight_reader_is_unaffected` |
| 2 | quiescence must make that request finish or be terminated **before** the swap | **PASS** | `test_quiescence_refuses_the_swap_while_that_reader_is_still_in_flight` |
| 3 | a request starting after the swap gets new base + pruned delta | **PASS** | `test_a_reader_that_starts_after_the_swap_sees_the_new_pair` |
| 4 | no request ever returns old base + pruned delta | **PASS** | `test_no_read_across_the_whole_protocol_ever_returns_the_losing_pair` |

Point 4 is a standing assertion, not a spot check: a reader loops for the whole
publish → prune → swap sequence, **every** snapshot it takes is recorded, and all of them are
checked afterwards — membership *and* value. It refreshes through the composite `refresh()`
only, which is the single refresh path the serving app actually has
([`../api/app.py`](../api/app.py) `_refresh_loop`); if a second, tier-local refresh path is ever
added to the serving layer, this proof lapses and `(e2)` reopens.

Point 2 is the one that decides the verdict. With a reader parked mid-refresh, the quiescence
hook counts it, cannot attest a drain, and the swap **refuses with the live delta untouched**.
The reader then completes on new base + old delta — safe — and the swap succeeds on the retry.

## Verdict: mechanism proven; production closure still PARTIAL

The dangerous pair is unreachable **when the process is genuinely stopped across the swap**, so
no refresh is in flight to be caught mid-way. That is a real result: no composite
generation/epoch fence is needed for it, and `tiered_cube.py` is unchanged.

It is **not** the same as `(e2)` being closed, and this document does not say it is. What the
executor can check is that the attestation is **present, well-formed and internally consistent**.
It cannot check that the attestation is **true** — a hook that reports `drained: True` without
looking is accepted, by construction, because there is nothing on this side of the boundary that
could contradict it. The closure therefore rests on an operational fact, and an operational fact
needs operational evidence:

| | status |
|---|---|
| the dangerous pair exists and is reachable without quiescence | **PROVEN** (`test_the_dangerous_pair_IS_reachable_when_a_refresh_spans_the_swap`) |
| safety comes from process quiescence, not from `TieredSnapshot` | **PROVEN** |
| the swap refuses to proceed without a proven drain | **PROVEN** (§7.9, mutation-verified) |
| the attestation is durable and auditable after the fact | **PROVEN** (recorded on swap and on rollback) |
| the deployed hook attests a drain it actually verified | **PARTIAL — deployment prerequisite** |

The last row is the one that keeps this PARTIAL. The runbook hook has been updated to measure
the worker count and record what it checked (below), but it has not been run: VM24 is off
limits for this work, so no real execution of it exists to point at.

Nothing here requires changing `TieredCube` or building the composite fence.

## Review round 2 — the production runbook, and a schema that was not enforced

**The one production runbook was broken by this API change.** `p4s10_production_rollout_runbook`
`quiesce()` ended in a bare `return`, so under the new executor it would stop pm2, close the
port, return `None`, be refused, and leave the swap unrun with the app down. A contract that
only the tests satisfy is not a contract. The hook now enumerates PM2 workers — a closed port
says new connections are refused, not that the worker finished the request it was already
serving — returns a real attestation, and writes an ops-side `quiescence_evidence.jsonl` before
the swap so it can be compared with what the executor recorded.
`TestTheProductionRunbookMatchesTheContract` reads the runbook and fails if it drifts again,
including if a core field is ever added.

**`observed_inflight` was documented as required and was not.** It was checked only when
present, and only against `> 0`, so both of these were accepted by the one load-bearing gate:

```
{"drained": True, "evidence": "trust me"}
{"drained": True, "evidence": "trust me", "observed_inflight": -1}
```

All three core fields are now required; `observed_inflight` must be a **raw `int`** equal to
`0`. `bool` is excluded explicitly, because `isinstance(True, int)` is True in Python and
`observed_inflight=False` would otherwise read as a proven zero. A negative count is refused as
"not a measurement" rather than treated as fewer than none. Extra keys remain allowed as
supplementary evidence and are **not** validated.

**The attestation was validated and then discarded.** Requiring `evidence` and throwing it away
made this document's own claim — that a swap suspected of losing a day can be traced to what
quiescence verified — untrue. The validated core is now recorded in `hold_dir/manifest.jsonl` on
the successful swap **and** on a rollback, and returned as `res["quiescence"]`. Only the fixed
validated fields are stored; extra keys are acknowledged by **name** so the record shows what
was supplied without the manifest inheriting an arbitrary caller object. A waived swap records
`{"attested": false, "waived": true}` rather than nothing, so the audit distinguishes "proven"
from "not asked".

## §7.9 — quiescence is required, and must prove itself

`pre_swap_quiesce_fn` defaulted to `None` and was **silently skipped when omitted**. A caller
who simply forgot swapped the delta path with readers alive — the exact combination the design
spec claims is unreachable, reachable by leaving out an argument. Same shape as the
`compaction_lock_path` default fixed in Part 2, and fixed the same way: **required, with a named
waiver** (`unsafe_skip_quiescence`) that has to be typed on purpose.

Presence alone is not enough either. A hook that merely does not raise cannot distinguish a
drained process from one it never looked at, and **"no reader was observed" is not "no reader
exists"** — precisely the assumption the `(e2)` analysis is not allowed to make. The hook must
now return an attestation:

```python
{"drained": True, "evidence": "pm2 stop ghrsst-api; :8080 closed; 0 worker pids",
 "observed_inflight": 0}
```

Refused: no attestation (the forgotten `return` — and the old success-by-silence), a non-dict,
`drained` anything but a raw `True`, a missing or blank `evidence` string, and any
`observed_inflight > 0`. `drained` is checked with `is True` rather than for truthiness because
`bool("false")` is `True`, which is what a shell hook produces when it interpolates a variable
it never set — the same laundering that made `allow_same_filesystem` grant the waiver it was
refusing in Part 2 round 7.

Every refusal is asserted to leave the live delta byte-for-byte as it was.

## Mutation verification

Eleven guards, each disabled in turn; **all eleven fail** — five from round 1, five
in the executor from round 2, and the runbook contract itself.

| guard disabled | result |
|---|---|
| quiescence hook not required | FAILED |
| a missing attestation accepted | FAILED |
| `drained` truthiness instead of raw `True` | FAILED (4) |
| `evidence` not required | FAILED (4) |
| in-flight readers ignored | FAILED |
| every core field required (round 2) | FAILED (4) |
| `observed_inflight` must be a raw int (round 2) | FAILED (5) |
| a negative `observed_inflight` refused (round 2) | FAILED |
| the attestation recorded on success (round 2) | FAILED (3) |
| only validated fields recorded, never the callback object (round 2) | FAILED |
| the runbook hook reverted to a bare `return` (round 2) | FAILED (5) |

The harness's own thread cleanup is verified the same way: a forced failure in the looping-reader
test leaves **zero** stray thread tracebacks, because a parked reader that outlives a failed
assertion reads a directory `tearDown` is deleting and reports as a lurid unrelated traceback
instead of the assertion that actually failed. That was a real leak in the first draft of this
suite, found when a refused plan skipped the join.

## NOT delivered in this round

- **Crash-boundary injection** — kill points between publish, prune, swap and WAL append, and
  the retry / committed-but-unlogged case. This is the rest of Part 3.
- **Snapshot cases (a) (b) (c) (e)** from the design spec's crash matrix.
- **Whole-VM snapshot rollback** — still **PARTIAL — deployment prerequisite** (Part 2).
- **G10 / H4 / G17** — not claimed.

## What would reopen `(e2)`

Stated plainly, because the verdict is conditional:

1. a second refresh path in the serving layer that refreshes **one** tier;
2. a swap performed without quiescence (now refused, waiver aside);
3. a quiescence hook that attests a drain it did not verify — the code can check that the
   attestation is *present and well-formed*, never that it is *true*. That is an operational
   responsibility, it is why `evidence` is mandatory and now durable, and it is the reason
   production closure is recorded above as PARTIAL rather than done.
