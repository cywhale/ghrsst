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
- **Nothing is coerced.** `list(...)` accepts a string and yields its characters; `str(x)` turns
  `True` into a plausible approver name. Every day list must be a raw `list` of `YYYY-MM-DD`
  strings, and `approved_by` / `approval_ref` must be raw non-empty strings. Input that has to be
  converted before it can be checked was never the input the checker was written for.
- **The audit's own sets must partition.** `delta_prune_candidates ∪
  blocked_need_compaction_first` must equal `drop_candidates_by_calendar` exactly. Without that,
  a tampered audit can introduce a base-covered day that was never a calendar candidate at all,
  and an approval naming it would drop a day nobody's keep-window arithmetic ever considered.
- **Duplicates are a refusal, never a de-duplication.** A day listed twice in the live delta, the
  audit or the approval means one of those is malformed. Quietly collapsing it would launder a
  broken store into a legal-looking day set — and the physical index a prune relies on stops
  being derivable from the day list.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Sequence

#: The approval an operator must supply to enter degraded mode.
APPROVAL_FIELDS = ("approved_by", "approval_ref", "approved_candidates")


class DegradedPruneRefused(Exception):
    """The degraded plan is not safe to build. Fail closed; never fall back to a wider drop."""


def _day_list(value, what: str) -> List[str]:
    """A raw list of ISO dates, or a refusal. No coercion of any kind.

    `list("2026-06-01")` is `['2', '0', '2', ...]` -- a perfectly well-formed day list as far as
    every downstream check is concerned, and completely wrong. So the type is checked before the
    contents, and the contents are checked as dates rather than as strings that look date-ish."""
    if not isinstance(value, list):
        raise DegradedPruneRefused(
            f"{what} must be a list of 'YYYY-MM-DD' strings, got {type(value).__name__}. It is "
            f"not converted: a string would iterate into characters and pass every later check.")
    for item in value:
        if not isinstance(item, str):
            raise DegradedPruneRefused(
                f"{what} contains a {type(item).__name__} ({item!r}); every entry must be a "
                f"'YYYY-MM-DD' string")
        try:
            datetime.strptime(item, "%Y-%m-%d")
        except ValueError:
            raise DegradedPruneRefused(
                f"{what} contains {item!r}, which is not a 'YYYY-MM-DD' date") from None
    return list(value)


def _required_text(value, what: str) -> str:
    """A raw non-empty string. `str(True)` is 'True', which reads like an approver."""
    if not isinstance(value, str):
        raise DegradedPruneRefused(
            f"{what} must be a string, got {type(value).__name__} ({value!r}). It is not "
            f"converted: an approval record that had to be stringified is not a record.")
    if not value.strip():
        raise DegradedPruneRefused(f"{what} must not be blank")
    return value


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
    live = _day_list(live_delta_days, "live_delta_days")
    dupes = sorted({d for d in live if live.count(d) > 1})
    if dupes:
        raise DegradedPruneRefused(
            f"the live delta lists day(s) {dupes} more than once. Silently de-duplicating would "
            f"launder a malformed store into a legal-looking day set, and the physical index a "
            f"prune uses would no longer be derivable from the day list. Repair the store.")
    live_set = set(live)
    candidates = _day_list(dp.get("delta_prune_candidates") or [],
                           "audit delta_prune_candidates")
    blocked = _day_list(dp.get("blocked_need_compaction_first") or [],
                        "audit blocked_need_compaction_first")
    if "drop_candidates_by_calendar" not in dp:
        raise DegradedPruneRefused(
            "the audit has no `drop_candidates_by_calendar`. It is the set the other two are "
            "meant to partition, and without it there is nothing to check them against.")
    calendar = _day_list(dp["drop_candidates_by_calendar"], "audit drop_candidates_by_calendar")

    # Checked HERE, not inside the approval branch: a malformed audit is malformed whether or
    # not anyone approved anything, and inside the branch the "approval must equal the audit"
    # check answered first, so this one could never fire.
    for label, seq in (("candidate", candidates), ("blocked", blocked),
                       ("calendar candidate", calendar)):
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

    # The two sets must PARTITION the calendar candidates -- not merely be disjoint. A tampered
    # audit that adds a base-covered day which was never a calendar candidate would otherwise
    # sail through: it is eligible, it is not blocked, and an approval naming it would drop a day
    # no keep-window arithmetic ever considered.
    union = set(candidates) | set(blocked)
    if union != set(calendar):
        extra = sorted(union - set(calendar))
        missing = sorted(set(calendar) - union)
        raise DegradedPruneRefused(
            f"the audit's eligible and blocked sets do not partition its calendar drop "
            f"candidates. Not in the calendar set: {extra or 'none'}; in the calendar set but "
            f"classified as neither: {missing or 'none'}. A day that was never a calendar "
            f"candidate must never become a drop candidate.")

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
        _required_text(approval["approved_by"], "approval approved_by")
        _required_text(approval["approval_ref"], "approval approval_ref")
        approved = _day_list(approval["approved_candidates"], "approval approved_candidates")
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
