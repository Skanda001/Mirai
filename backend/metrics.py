"""
Defines what a 'good' schedule means for this system, and computes it.

Reported metrics (each chosen to catch a different failure mode a
whiteboard-coordinator would care about):

  1. coverage            - % of candidate interviews actually scheduled.
                            The headline number, but not sufficient alone
                            (100% coverage padded with terrible room
                            utilisation is not "good").
  2. unscheduled_reasons  - breakdown of WHY interviews failed to place, so
                            the coordinator knows which constraint to
                            relax (see README "which constraint bends").
  3. room_utilisation     - % of available room-slot capacity used, overall
                            and per day. Low = wasted rooms; near-100% =
                            no slack left to absorb a disruption.
  4. panel_utilisation    - same idea, for interview panels specifically
                            (rooms can be free while panels are the real
                            bottleneck, or vice versa).
  5. student_wait_time    - average and max idle gap (in minutes) between a
                            student's consecutive interviews on the same
                            day. A schedule that is 100% feasible but leaves
                            a student sitting idle for 5 hours is a bad
                            schedule for that student.
  6. replan_churn         - (only meaningful after a replan) % of
                            previously-scheduled interviews whose
                            room/panel/time changed. This is the number that
                            answers "did we move 200 appointments to fix a
                            2-hour delay?"
"""
from collections import defaultdict
from models import SLOTS_PER_DAY, DAYS, SLOT_MINUTES


def compute_coverage(interviews):
    total = len(interviews)
    scheduled = sum(1 for i in interviews if i.status == "scheduled")
    cancelled = sum(1 for i in interviews if i.status == "cancelled")
    unscheduled = total - scheduled - cancelled
    return {
        "total_candidates": total,
        "scheduled": scheduled,
        "cancelled": cancelled,
        "unscheduled": unscheduled,
        "coverage_pct": round(100 * scheduled / total, 2) if total else 0.0,
    }


def unscheduled_breakdown(interviews):
    reasons = defaultdict(int)
    for i in interviews:
        if i.status == "unscheduled":
            reasons[i.unscheduled_reason or "unknown"] += 1
    return dict(reasons)


def room_utilisation(interviews, rooms):
    capacity = len(rooms) * DAYS * SLOTS_PER_DAY
    used = sum((i.end_slot - i.start_slot) for i in interviews if i.status == "scheduled")
    per_day = defaultdict(int)
    for i in interviews:
        if i.status == "scheduled":
            per_day[i.day] += (i.end_slot - i.start_slot)
    per_day_pct = {
        f"day_{d+1}": round(100 * per_day[d] / (len(rooms) * SLOTS_PER_DAY), 2)
        for d in range(DAYS)
    }
    return {
        "overall_pct": round(100 * used / capacity, 2) if capacity else 0.0,
        "per_day_pct": per_day_pct,
    }


def panel_utilisation(interviews, companies):
    company_by_id = {c.id: c for c in companies}
    cap_by_company = defaultdict(int)
    used_by_company = defaultdict(int)
    for c in companies:
        cap_by_company[c.id] = c.num_panels * len(c.allowed_days) * SLOTS_PER_DAY
    for i in interviews:
        if i.status == "scheduled":
            used_by_company[i.company_id] += (i.end_slot - i.start_slot)
    total_cap = sum(cap_by_company.values())
    total_used = sum(used_by_company.values())
    worst = sorted(
        ((cid, 100 * used_by_company[cid] / cap_by_company[cid])
         for cid in cap_by_company if cap_by_company[cid] > 0),
        key=lambda x: -x[1]
    )[:5]
    return {
        "overall_pct": round(100 * total_used / total_cap, 2) if total_cap else 0.0,
        "top5_busiest_companies": [
            {"company": company_by_id[cid].name, "utilisation_pct": round(u, 2)}
            for cid, u in worst
        ],
    }


def student_wait_times(interviews):
    by_student_day = defaultdict(list)
    for i in interviews:
        if i.status == "scheduled":
            by_student_day[(i.student_id, i.day)].append((i.start_slot, i.end_slot))
    gaps = []
    for (_sid, _day), spans in by_student_day.items():
        spans.sort()
        for a, b in zip(spans, spans[1:]):
            gap_slots = b[0] - a[1]
            if gap_slots > 0:
                gaps.append(gap_slots * SLOT_MINUTES)
    if not gaps:
        return {"avg_wait_min": 0, "max_wait_min": 0, "students_with_gaps": 0}
    return {
        "avg_wait_min": round(sum(gaps) / len(gaps), 1),
        "max_wait_min": max(gaps),
        "gap_count": len(gaps),
    }


def detect_student_clashes(interviews):
    """
    Audit check, not a scheduling step: verify no student is double-booked.
    This should always return 0 given the hard constraint in scheduler.py,
    but the brief explicitly lists 'student clashes' as a metric to report,
    and asserting it rather than assuming it is the more defensible claim
    to make live ("here's proof", not "trust me").
    """
    by_student = defaultdict(list)
    for i in interviews:
        if i.status == "scheduled":
            by_student[i.student_id].append(i)
    clashes = 0
    examples = []
    for sid, ivs in by_student.items():
        ivs_sorted = sorted(ivs, key=lambda i: (i.day, i.start_slot))
        for a, b in zip(ivs_sorted, ivs_sorted[1:]):
            if a.day == b.day and a.end_slot > b.start_slot:
                clashes += 1
                if len(examples) < 5:
                    examples.append({"student_id": sid, "interview_a": a.id, "interview_b": b.id})
    return {"clash_count": clashes, "examples": examples}


def tight_transitions(interviews, max_ok_gap_slots=1):
    """
    'Upcoming conflict' style risk flag: a student whose two consecutive
    interviews are in DIFFERENT rooms with less than max_ok_gap_slots of
    gap between them physically cannot walk there in time. This is not a
    hard constraint the scheduler currently enforces (a real placement cell
    would set a room-to-room walking buffer), so we surface it as a risk
    for the coordinator to see, rather than silently ignoring the realism
    gap.
    """
    by_student_day = defaultdict(list)
    for i in interviews:
        if i.status == "scheduled":
            by_student_day[(i.student_id, i.day)].append(i)
    flags = []
    for (sid, day), ivs in by_student_day.items():
        ivs.sort(key=lambda i: i.start_slot)
        for a, b in zip(ivs, ivs[1:]):
            gap = b.start_slot - a.end_slot
            if a.room_id != b.room_id and 0 <= gap < max_ok_gap_slots:
                flags.append({
                    "student_id": sid, "day": day,
                    "from_room": a.room_id, "to_room": b.room_id,
                    "gap_minutes": gap * SLOT_MINUTES,
                    "interview_a": a.id, "interview_b": b.id,
                })
    return {"count": len(flags), "flags": flags[:20]}


def low_slack_warning(interviews, rooms, threshold_pct=95):
    """Days at or above threshold utilisation have no slack left to absorb
    a disruption - worth flagging proactively, not just after one hits."""
    util = room_utilisation(interviews, rooms)
    tight_days = [d for d, pct in util["per_day_pct"].items() if pct >= threshold_pct]
    return {"threshold_pct": threshold_pct, "tight_days": tight_days}


def replan_churn(before_snapshot, after_interviews):
    """
    before_snapshot: dict interview_id -> (day, start_slot, room_id, panel_no)
                      for interviews that were scheduled BEFORE the replan.
    after_interviews: current interview objects after the replan.
    """
    if not before_snapshot:
        return None
    moved = 0
    unchanged = 0
    now_unscheduled = 0
    after_by_id = {i.id: i for i in after_interviews}
    for iid, before in before_snapshot.items():
        after = after_by_id.get(iid)
        if after is None or after.status != "scheduled":
            now_unscheduled += 1
            continue
        now = (after.day, after.start_slot, after.room_id, after.panel_no)
        if now == before:
            unchanged += 1
        else:
            moved += 1
    total = len(before_snapshot)
    return {
        "previously_scheduled": total,
        "unchanged": unchanged,
        "moved": moved,
        "newly_unscheduled": now_unscheduled,
        "churn_pct": round(100 * moved / total, 2) if total else 0.0,
    }


def full_report(companies, students, rooms, interviews, before_snapshot=None):
    return {
        "coverage": compute_coverage(interviews),
        "unscheduled_reasons": unscheduled_breakdown(interviews),
        "room_utilisation": room_utilisation(interviews, rooms),
        "panel_utilisation": panel_utilisation(interviews, companies),
        "student_wait_times": student_wait_times(interviews),
        "student_clashes": detect_student_clashes(interviews),
        "tight_transitions": tight_transitions(interviews),
        "low_slack_warning": low_slack_warning(interviews, rooms),
        "replan_churn": replan_churn(before_snapshot, interviews) if before_snapshot else None,
    }
