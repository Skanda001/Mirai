"""
Core data model for the Placement Week Scheduler.

Time representation
--------------------
Each day runs 09:00-17:00 (8 hours) split into 15-minute slots -> 32 slots/day.
Across 4 placement days that's 128 discrete slot indices (0-127), which is the
common granularity every company's interview duration (15/30/45 min) divides
into evenly. This keeps the scheduler a plain integer-interval problem instead
of needing continuous time math.
"""
from dataclasses import dataclass, field, asdict
from typing import List, Optional

DAYS = 4
SLOTS_PER_DAY = 32          # 09:00-17:00 in 15-min steps
SLOT_MINUTES = 15
DAY_START_MIN = 9 * 60      # 09:00 in minutes-from-midnight


def slot_to_clock(day: int, slot_in_day: int) -> str:
    minutes = DAY_START_MIN + slot_in_day * SLOT_MINUTES
    h, m = divmod(minutes, 60)
    return f"Day {day + 1} {h:02d}:{m:02d}"


def global_slot(day: int, slot_in_day: int) -> int:
    return day * SLOTS_PER_DAY + slot_in_day


def day_of(global_slot_idx: int) -> int:
    return global_slot_idx // SLOTS_PER_DAY


def slot_in_day_of(global_slot_idx: int) -> int:
    return global_slot_idx % SLOTS_PER_DAY


@dataclass
class Room:
    id: str
    room_type: str  # "standard" | "hitech"


@dataclass
class Company:
    id: str
    name: str
    tier: int                 # 1 = Day-1 mass recruiter ... 4 = niche/late
    cgpa_cutoff: float
    num_panels: int
    duration_min: int         # 15 / 30 / 45
    allowed_days: List[int]   # 0-indexed day numbers this company interviews on
    room_requirement: str     # "any" | "hitech"
    eligible_branches: List[str]
    arrival_delay_slots: int = 0   # set >0 by a "late arrival" disruption

    @property
    def duration_slots(self) -> int:
        return self.duration_min // SLOT_MINUTES


@dataclass
class Student:
    id: str
    name: str
    branch: str
    cgpa: float
    shortlisted_by: List[str] = field(default_factory=list)  # company ids
    withdrawn: bool = False


@dataclass
class Interview:
    """One (student, company) interview to be scheduled."""
    id: str
    student_id: str
    company_id: str
    priority_score: float
    status: str = "unscheduled"     # unscheduled | scheduled | cancelled
    day: Optional[int] = None
    start_slot: Optional[int] = None   # slot-in-day
    end_slot: Optional[int] = None     # exclusive, slot-in-day
    room_id: Optional[str] = None
    panel_no: Optional[int] = None     # which of the company's panels (0-indexed)
    unscheduled_reason: Optional[str] = None

    def slot_range(self):
        g0 = global_slot(self.day, self.start_slot)
        return range(g0, g0 + (self.end_slot - self.start_slot))

    def to_dict(self):
        return asdict(self)


def to_dict_list(objs):
    return [asdict(o) for o in objs]
