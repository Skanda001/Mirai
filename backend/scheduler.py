"""
The scheduling engine.

Algorithm: priority-ordered greedy with earliest-feasible-slot placement.
------------------------------------------------------------------------
Why greedy instead of an ILP/CP-SAT global solve?
  1. It has to re-run in seconds, live, in front of a defense panel, on every
     disruption - a full re-solve of an NP-hard combined room+panel+student
     scheduling problem does not give that guarantee at this scale.
  2. It is inspectable: every placement decision has one clear reason
     ("earliest slot where student, a panel, and a matching room were all
     free"), which matters when you have to defend *why* interview X landed
     where it did.
  3. It composes naturally with "minimal disturbance" replanning (see
     replanner.py): we simply hold every already-placed interview fixed as
     an occupied resource and re-run the same placement routine only for the
     newly affected interviews.

Ordering (most-constrained-first, a standard CSP heuristic):
  - Primary: company tier (Day-1 mass recruiters placed first - they have
    the least slack, being confined to a single day).
  - Secondary: student CGPA (higher first) as a tie-break - stronger
    students sit on more shortlists, so locking their slots early avoids
    discovering a conflict late and cascading a reschedule.

Hard constraints (never violated):
  - A student cannot be in two interviews at once.
  - A room cannot host two interviews at once.
  - A specific company panel cannot run two interviews at once.
  - CGPA cutoff / branch eligibility (enforced upstream at shortlist time).
  - room_requirement ("hitech") is a hard constraint - some interviews
    genuinely need AV/remote-panel equipment.

Soft constraints (bent, in this order, before an interview is dropped):
  1. Preferred earliest slot -> any later slot on an allowed day.
  2. Preferred room -> any matching-type room.
  3. Preferred panel -> any panel of that company.
  If a candidate still cannot be placed within the company's allowed days,
  it is left UNSCHEDULED with a specific reason. The system never
  silently drops an interview.
"""
from collections import defaultdict
from models import (
    SLOTS_PER_DAY, DAYS, global_slot,
)


class ScheduleState:
    """Holds resource-occupancy bitmaps so feasibility checks are O(1)-ish."""

    def __init__(self, rooms, companies):
        self.rooms = {r.id: r for r in rooms}
        self.companies = {c.id: c for c in companies}
        total_slots = DAYS * SLOTS_PER_DAY
        # room_busy[room_id] = set of global slot indices occupied
        self.room_busy = defaultdict(set)
        # panel_busy[(company_id, panel_no)] = set of global slot indices
        self.panel_busy = defaultdict(set)
        # student_busy[student_id] = set of global slot indices
        self.student_busy = defaultdict(set)
        # student_bookings[student_id] = list of (day, start_in_day, end_in_day, room_id)
        # tracked separately from student_busy so we can enforce a walking
        # buffer between different rooms, not just "not literally overlapping"
        self.student_bookings = defaultdict(list)
        self.total_slots = total_slots
        self.interviews_by_id = {}

    def violates_walk_buffer(self, student_id, day, start_in_day, end_in_day, room_id, buffer_slots=1):
        """
        A student cannot teleport between rooms. If they have another
        interview on the same day in a DIFFERENT room ending less than
        `buffer_slots` before this one starts (or starting less than
        buffer_slots after this one ends), this slot is physically
        infeasible even though it doesn't literally overlap. Same-room
        back-to-back is fine (no walking needed).
        """
        for (bday, bstart, bend, broom) in self.student_bookings[student_id]:
            if bday != day or broom == room_id:
                continue
            if bend <= start_in_day and start_in_day - bend < buffer_slots:
                return True
            if start_in_day <= bstart and bstart - end_in_day < buffer_slots and bstart >= end_in_day:
                return True
        return False

    def is_free(self, slots, room_id, company_id, panel_no, student_id):
        if any(s in self.room_busy[room_id] for s in slots):
            return False
        if any(s in self.panel_busy[(company_id, panel_no)] for s in slots):
            return False
        if any(s in self.student_busy[student_id] for s in slots):
            return False
        return True

    def occupy(self, interview):
        slots = list(interview.slot_range())
        self.room_busy[interview.room_id].update(slots)
        self.panel_busy[(interview.company_id, interview.panel_no)].update(slots)
        self.student_busy[interview.student_id].update(slots)
        self.student_bookings[interview.student_id].append(
            (interview.day, interview.start_slot, interview.end_slot, interview.room_id)
        )
        self.interviews_by_id[interview.id] = interview

    def free(self, interview):
        """Release the resources held by a previously-scheduled interview."""
        slots = set(interview.slot_range())
        self.room_busy[interview.room_id] -= slots
        self.panel_busy[(interview.company_id, interview.panel_no)] -= slots
        self.student_busy[interview.student_id] -= slots
        booking = (interview.day, interview.start_slot, interview.end_slot, interview.room_id)
        bookings = self.student_bookings[interview.student_id]
        if booking in bookings:
            bookings.remove(booking)
        self.interviews_by_id.pop(interview.id, None)


def candidate_rooms(rooms, requirement):
    if requirement == "hitech":
        return [r for r in rooms if r.room_type == "hitech"]
    return rooms  # "any" -> standard tried first (see place_interview ordering)


def place_interview(interview, company, rooms, state, allowed_days=None,
                     min_global_slot=0):
    """
    Try to place a single interview, earliest-feasible-first.
    Returns True if placed (mutates `interview` in place and occupies state),
    False otherwise (sets interview.unscheduled_reason).
    """
    days = allowed_days if allowed_days is not None else company.allowed_days
    duration = company.duration_slots

    # room candidates: matching type; for "any" try standard rooms before
    # hitech ones so scarce hitech rooms stay free for the companies that
    # truly require them.
    if company.room_requirement == "hitech":
        room_pool = [r for r in rooms if r.room_type == "hitech"]
    else:
        room_pool = ([r for r in rooms if r.room_type == "standard"] +
                     [r for r in rooms if r.room_type == "hitech"])

    if not room_pool:
        interview.unscheduled_reason = "no room of required type exists"
        return False

    saw_any_capacity = False
    for day in days:
        for start_in_day in range(0, SLOTS_PER_DAY - duration + 1):
            g0 = global_slot(day, start_in_day)
            if g0 < min_global_slot:
                continue  # e.g. before a company's delayed arrival
            slots = list(range(g0, g0 + duration))
            for panel_no in range(company.num_panels):
                if any(s in state.panel_busy[(company.id, panel_no)] for s in slots):
                    continue
                if any(s in state.student_busy[interview.student_id] for s in slots):
                    # student busy at this time regardless of room/panel -
                    # no point trying other panels/rooms for this slot
                    break
                for room in room_pool:
                    saw_any_capacity = True
                    if any(s in state.room_busy[room.id] for s in slots):
                        continue
                    if state.violates_walk_buffer(interview.student_id, day, start_in_day,
                                                   start_in_day + duration, room.id):
                        continue
                    # feasible! commit.
                    interview.day = day
                    interview.start_slot = start_in_day
                    interview.end_slot = start_in_day + duration
                    interview.room_id = room.id
                    interview.panel_no = panel_no
                    interview.status = "scheduled"
                    interview.unscheduled_reason = None
                    state.occupy(interview)
                    return True

    interview.status = "unscheduled"
    if not saw_any_capacity:
        interview.unscheduled_reason = "student double-booked across every allowed slot"
    else:
        interview.unscheduled_reason = (
            "no room/panel combination free in any slot on this company's allowed days"
        )
    return False


def run_full_schedule(companies, students, rooms, interviews):
    """Initial, from-scratch schedule of every candidate interview."""
    state = ScheduleState(rooms, companies)
    company_by_id = {c.id: c for c in companies}
    ordered = sorted(interviews, key=lambda iv: iv.priority_score)
    for iv in ordered:
        company = company_by_id[iv.company_id]
        place_interview(iv, company, rooms, state)
    return state, ordered
