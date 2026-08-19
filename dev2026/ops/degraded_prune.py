"""P4-S10 degraded (candidate-only) delta prune — the day-set partition, as a pure function.

The runbook used to compute the keep set inline, as
``keep = [d for d in live_days if d >= keep_window_start]``. That drops **everything** older
than the window — including the days the audit had marked
`blocked_need_compaction_first`, which are precisely the days base does **not** cover. The
VM24 rehearsal had 4 approved candidates and 18 blocked days; that expression would have
dropped all 22.

`prune_delta`'s own base-coverage gate would have refused the plan, so nothing was lost. But a
runbook whose day set is wrong and is caught downstream is a runbook that is one gate-relaxation
away from data loss, and the arithmetic belongs somewhere it can be tested.

## The rule

- **Normal outcome when `blocked_need_compaction_first` is non-empty is NO-GO.** Degraded
  candidate-only mode is entered only with an explicit, recorded operator approval.
- The dropped set is **exactly** `audit["delta_prune"]["delta_prune_candidates"]` — not "days
  before the keep window", not the operator's own list.
- A blocked day is **never** dropped. Asserted, not assumed: blocked ∩ dropped must be empty.
- `keep = live_delta_days - approved_candidates`, so blocked days stay in delta by construction
  rather than by the keep-window arithmetic happening to include them.
- The base-coverage gate in `prune_delta` is **not** relaxed. This function decides which days to
  offer; that gate still decides whether they may go.
- **Duplicates are a refusal, never a de-duplication.** A day listed twice in the live delta, the
  audit or the approval means one of those is malformed. Quietly collapsing it would launder a
  broken store into a legal-looking day set — and the physical index a prune relies on stops
  being derivable from the day list.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

#: The approval an operator must supply to enter degraded mode.
APPROVAL_FIELDS = ("approved_by", "approval_ref", "approved_candidates")


class DegradedPruneRefused(Exception):
    """The degraded plan is not safe to build. Fail closed; never fall back to a wider drop."""


def partition_delta_days(*, live_delta_days: Sequence[str], audit: dict,
                         approval: Optional[dict] = None) -> Dict[str, object]:
    """Split the live delta into keep / drop for this run, or refuse.

    `audit` is the fresh `p4_retention_audit` JSON. `approval` is `None` for a normal run --
    which is a **refusal** whenever any day is blocked -- or a dict carrying
    `approved_by`, `approval_ref` and `approved_candidates`.

    Returns `{"mode", "keep_days", "dropped_days", "blocked_retained", "approval_ref"}`.
    """
    dp = (audit or {}).get("delta_prune") or {}
    if not dp.get("available"):
        raise DegradedPruneRefused("the audit has no usable delta_prune block; a fresh audit is "
                                   "the only source of this run's day sets")
    live = list(live_delta_days)
    dupes = sorted({d for d in live if live.count(d) > 1})
    if dupes:
        raise DegradedPruneRefused(
            f"the live delta lists day(s) {dupes} more than once. Silently de-duplicating would "
            f"launder a malformed store into a legal-looking day set, and the physical index a "
            f"prune uses would no longer be derivable from the day list. Repair the store.")
    live_set = set(live)
    candidates = list(dp.get("delta_prune_candidates") or [])
    blocked = list(dp.get("blocked_need_compaction_first") or [])

    # Checked HERE, not inside the approval branch: a malformed audit is malformed whether or
    # not anyone approved anything, and inside the branch the "approval must equal the audit"
    # check answered first, so this one could never fire.
    for label, seq in (("candidate", candidates), ("blocked", blocked)):
        dupes_ = sorted({d for d in seq if seq.count(d) > 1})
        if dupes_:
            raise DegradedPruneRefused(
                f"the audit lists {label} day(s) {dupes_} more than once; it is malformed and "
                f"no day set may be derived from it.")

    overlap = sorted(set(candidates) & set(blocked))
    if overlap:
        raise DegradedPruneRefused(
            f"the audit lists {overlap} as BOTH an eligible candidate and blocked. The two sets "
            f"are meant to partition the calendar drop candidates; an overlap means the audit "
            f"itself is inconsistent and no day set may be derived from it.")

    if blocked and approval is None:
        raise DegradedPruneRefused(
            f"{len(blocked)} delta day(s) are blocked_need_compaction_first ({blocked[0]}.."
            f"{blocked[-1]}): base does not cover them. The normal outcome is NO-GO. Entering "
            f"candidate-only degraded mode requires an explicit operator approval naming the "
            f"{len(candidates)} approved candidate(s).")

    mode = "normal"
    approval_ref = None
    if approval is not None:
        missing = [f for f in APPROVAL_FIELDS if f not in approval]
        if missing:
            raise DegradedPruneRefused(
                f"the degraded-mode approval is missing {', '.join(missing)}. An approval that "
                f"does not say who gave it, against what record, and for which days is not an "
                f"approval.")
        if not str(approval["approved_by"]).strip() or not str(approval["approval_ref"]).strip():
            raise DegradedPruneRefused("approved_by and approval_ref must both be non-empty")
        approved = list(approval["approved_candidates"])
        appr_dupes = sorted({d for d in approved if approved.count(d) > 1})
        if appr_dupes:
            raise DegradedPruneRefused(
                f"the approval lists day(s) {appr_dupes} more than once. An approval is a record "
                f"of what a human reviewed; de-duplicating it would change that record.")
        if sorted(approved) != sorted(candidates):
            raise DegradedPruneRefused(
                f"the approval names {len(approved)} day(s) but the fresh audit lists "
                f"{len(candidates)} eligible candidate(s). The dropped set must be EXACTLY the "
                f"audit's `delta_prune_candidates`: an operator-widened list is how a blocked "
                f"day gets dropped, and an operator-narrowed one silently changes what was "
                f"reviewed. Re-run the audit and re-approve.")
        mode = "degraded_candidate_only" if blocked else "normal"
        approval_ref = approval["approval_ref"]

    not_live = sorted(d for d in candidates if d not in live_set)
    if not_live:
        raise DegradedPruneRefused(
            f"candidate day(s) {not_live} are not in the live delta. The audit and the store "
            f"disagree, so the audit is stale; re-run it.")

    dropped = [d for d in live if d in set(candidates)]
    keep = [d for d in live if d not in set(candidates)]

    still_blocked = sorted(set(blocked) & set(dropped))
    if still_blocked:                       # unreachable given the overlap check; asserted anyway
        raise DegradedPruneRefused(
            f"blocked day(s) {still_blocked} ended up in the dropped set. Refusing.")
    missing_blocked = sorted(d for d in blocked if d in live_set and d not in set(keep))
    if missing_blocked:
        raise DegradedPruneRefused(
            f"blocked day(s) {missing_blocked} are in the live delta but not in the keep set. "
            f"Refusing: they are exactly the days base cannot serve.")

    return {
        "mode": mode,
        "approval_ref": approval_ref,
        "keep_days": keep,
        "dropped_days": dropped,
        "blocked_retained": [d for d in keep if d in set(blocked)],
    }
