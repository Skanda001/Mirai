"""
Replanning engine: takes a disruption event, computes the smallest set of
interviews that MUST be touched, re-places only those (holding everything
else fixed), and returns a structured diff.

Design decision - "how much reshuffling is acceptable?"
---------------------------------------------------------
We never do a global re-solve. Every disruption handler below identifies an
"affected set" - the interviews that are directly invalidated by the event -
and re-runs `place_interview` for only that set, treating every other
already-scheduled interview as an immovable occupied resource. This is what
keeps a 2-hour delay from turning into "200 appointments moved": the blast
radius is bounded by construction, not by hoping a global optimizer finds a
low-churn solution.

The trade-off we accept: this can occasionally leave a slightly lower
coverage than a full re-solve would achieve, because we don't allow bumping
an unrelated, already-placed interview to make room. We treat that as the
correct trade-off for a live event - predictability and a small, explainable
diff beats a theoretically-optimal but disruptive reshuffle. The affected
set can be made deliberately reoptimize-hungry per-disruption where it's
safe (e.g. a full withdrawal frees a slot that we DO try to backfill from
the unscheduled waitlist, since that is a strict improvement with zero
churn to anyone else).

Which constraint bends first?
-------------------------------
For every re-placement in this module we relax, in order: exact slot ->
any slot on an allowed day -> any matching room -> any panel. We do NOT
relax CGPA cutoff, branch eligibility, or the double-booking constraints -
those are business rules, not scheduling preferences, and bending them is a
decision only the human coordinator should make (surfaced via
`unscheduled_reason`, never silently auto-applied).
"""
from dataclasses import dataclass
from typing import List, Dict, Optional
from models import global_slot, SLOTS_PER_DAY
from scheduler import place_interview, ScheduleState


@dataclass
class DiffEntry:
    interview_id: str
    student_id: str
    company_id: str
    change: str          # "moved" | "cancelled" | "newly_scheduled" | "still_unscheduled"
    before: Optional[dict]
    after: Optional[dict]
    reason: str


def _snapshot(iv):
    if iv.status != "scheduled":
        return None
    return {
        "day": iv.day, "start_slot": iv.start_slot, "end_slot": iv.end_slot,
        "room_id": iv.room_id, "panel_no": iv.panel_no,
    }


def rebuild_state(rooms, companies, all_interviews):
    """Rebuild a ScheduleState from the current status of every interview.
    Used so each replan call starts from ground truth."""
    state = ScheduleState(rooms, companies)
    for iv in all_interviews:
        if iv.status == "scheduled":
            state.occupy(iv)
    return state


def _people_to_notify(diffs):
    students = sorted({d.student_id for d in diffs if d.change in ("moved", "cancelled", "newly_scheduled")})
    companies = sorted({d.company_id for d in diffs if d.change in ("moved", "cancelled", "newly_scheduled")})
    return {"students": students, "companies": companies}


def replan_company_late(companies, rooms, all_interviews, company_id, day, delay_slots):
    """
    A company arrives `delay_slots` late on `day`. Every interview of theirs
    already scheduled in the now-blocked window on that day must move -
    first choice: later the same day, else another allowed day.
    """
    company = next(c for c in companies if c.id == company_id)
    min_slot_that_day = global_slot(day, delay_slots)

    affected = [iv for iv in all_interviews
                if iv.company_id == company_id and iv.status == "scheduled"
                and iv.day == day and iv.start_slot < delay_slots]
    before = {iv.id: _snapshot(iv) for iv in affected}

    state = rebuild_state(rooms, companies, all_interviews)
    for iv in affected:
        state.free(iv)

    diffs = []
    # try to re-place on the SAME day first (>= delayed arrival only - the
    # whole point of this handler), then fall back to any OTHER allowed day
    # for this company. We deliberately never fall back to the same day at
    # an earlier slot - that slot is blocked because the company isn't
    # physically present yet, so re-offering it would silently ignore the
    # disruption we were asked to model.
    affected_sorted = sorted(affected, key=lambda iv: iv.priority_score)
    other_days = [d for d in company.allowed_days if d != day]
    for iv in affected_sorted:
        placed = place_interview(iv, company, rooms, state,
                                  allowed_days=[day], min_global_slot=min_slot_that_day)
        if not placed and other_days:
            placed = place_interview(iv, company, rooms, state, allowed_days=other_days)
        if placed:
            diffs.append(DiffEntry(iv.id, iv.student_id, iv.company_id, "moved",
                                    before[iv.id], _snapshot(iv),
                                    f"{company.name} arrived {delay_slots*15}min late on day {day+1}"))
        else:
            diffs.append(DiffEntry(iv.id, iv.student_id, iv.company_id, "still_unscheduled",
                                    before[iv.id], None,
                                    iv.unscheduled_reason or "no capacity after delay"))
    return diffs


def replan_panel_drop(companies, rooms, all_interviews, company_id, panel_no):
    """A named panel of a company drops out entirely (all days)."""
    company = next(c for c in companies if c.id == company_id)
    affected = [iv for iv in all_interviews
                if iv.company_id == company_id and iv.status == "scheduled"
                and iv.panel_no == panel_no]
    before = {iv.id: _snapshot(iv) for iv in affected}

    state = rebuild_state(rooms, companies, all_interviews)
    for iv in affected:
        state.free(iv)
    # block the dropped panel entirely so place_interview never reuses it
    for d in range(4):
        pass  # panel is simply absent from consideration below

    original_num_panels = company.num_panels
    diffs = []
    affected_sorted = sorted(affected, key=lambda iv: iv.priority_score)
    # temporarily reduce available panels by removing the dropped one from
    # rotation: simplest safe way is to mark it permanently busy.
    full_block = set(range(4 * SLOTS_PER_DAY))
    state.panel_busy[(company_id, panel_no)] = full_block

    for iv in affected_sorted:
        placed = place_interview(iv, company, rooms, state)
        if placed:
            diffs.append(DiffEntry(iv.id, iv.student_id, iv.company_id, "moved",
                                    before[iv.id], _snapshot(iv),
                                    f"Panel {panel_no} of {company.name} dropped out"))
        else:
            diffs.append(DiffEntry(iv.id, iv.student_id, iv.company_id, "still_unscheduled",
                                    before[iv.id], None,
                                    iv.unscheduled_reason or "no other panel/slot available"))
    return diffs, full_block


def replan_student_withdraw(companies, rooms, all_interviews, student_id):
    """
    Student withdraws (e.g. accepted an offer). Cancel every one of their
    NOT-YET-HAPPENED interviews and try to backfill each freed slot from the
    unscheduled waitlist for that same company (a strict improvement: zero
    extra churn, one more student gets seen).
    """
    affected = [iv for iv in all_interviews
                if iv.student_id == student_id and iv.status == "scheduled"]
    before = {iv.id: _snapshot(iv) for iv in affected}
    diffs = []

    state = rebuild_state(rooms, companies, all_interviews)
    company_by_id = {c.id: c for c in companies}

    waitlist_by_company: Dict[str, List] = {}
    for iv in all_interviews:
        if iv.status == "unscheduled":
            waitlist_by_company.setdefault(iv.company_id, []).append(iv)
    for lst in waitlist_by_company.values():
        lst.sort(key=lambda iv: iv.priority_score)

    for iv in affected:
        state.free(iv)
        iv.status = "cancelled"
        iv.unscheduled_reason = "student withdrew"
        diffs.append(DiffEntry(iv.id, iv.student_id, iv.company_id, "cancelled",
                                before[iv.id], None, "Student withdrew after receiving an offer"))

        # backfill attempt: freed exact (room, panel, time) slot is offered
        # to the next waiting student for the same company, subject to all
        # normal constraints (their own availability included).
        candidates = waitlist_by_company.get(iv.company_id, [])
        company = company_by_id[iv.company_id]
        for cand in candidates:
            if cand.status != "unscheduled":
                continue
            slots = list(range(global_slot(iv.day, iv.start_slot), global_slot(iv.day, iv.end_slot)))
            if (state.is_free(slots, iv.room_id, iv.company_id, iv.panel_no, cand.student_id)
                    and not state.violates_walk_buffer(cand.student_id, iv.day, iv.start_slot, iv.end_slot, iv.room_id)):
                cand.day, cand.start_slot, cand.end_slot = iv.day, iv.start_slot, iv.end_slot
                cand.room_id, cand.panel_no = iv.room_id, iv.panel_no
                cand.status = "scheduled"
                cand.unscheduled_reason = None
                state.occupy(cand)
                diffs.append(DiffEntry(cand.id, cand.student_id, cand.company_id,
                                        "newly_scheduled", None, _snapshot(cand),
                                        f"Backfilled into slot freed by {student_id}'s withdrawal"))
                break
    return diffs


def replan_room_unavailable(companies, rooms, all_interviews, room_id):
    """A room becomes unavailable for the rest of the event (AC failure, etc.)."""
    affected = [iv for iv in all_interviews
                if iv.room_id == room_id and iv.status == "scheduled"]
    before = {iv.id: _snapshot(iv) for iv in affected}
    company_by_id = {c.id: c for c in companies}

    state = rebuild_state(rooms, companies, all_interviews)
    for iv in affected:
        state.free(iv)
    remaining_rooms = [r for r in rooms if r.id != room_id]
    full_block = set(range(4 * SLOTS_PER_DAY))
    state.room_busy[room_id] = full_block

    diffs = []
    for iv in sorted(affected, key=lambda iv: iv.priority_score):
        company = company_by_id[iv.company_id]
        placed = place_interview(iv, company, remaining_rooms, state)
        if placed:
            diffs.append(DiffEntry(iv.id, iv.student_id, iv.company_id, "moved",
                                    before[iv.id], _snapshot(iv), f"Room {room_id} became unavailable"))
        else:
            diffs.append(DiffEntry(iv.id, iv.student_id, iv.company_id, "still_unscheduled",
                                    before[iv.id], None,
                                    iv.unscheduled_reason or "no other room free at this time"))
    return diffs


def diffs_to_dict(diffs):
    out = [d.__dict__ for d in diffs]
    return {
        "changes": out,
        "summary": {
            "moved": sum(1 for d in diffs if d.change == "moved"),
            "cancelled": sum(1 for d in diffs if d.change == "cancelled"),
            "newly_scheduled": sum(1 for d in diffs if d.change == "newly_scheduled"),
            "still_unscheduled": sum(1 for d in diffs if d.change == "still_unscheduled"),
        },
        "notify": _people_to_notify(diffs),
    }
