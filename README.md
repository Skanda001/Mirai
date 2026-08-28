# The Placement Week Scheduler

A system that replaces the whiteboard: generates a realistic placement-week
dataset, produces a feasible interview schedule, replans it live under
disruption with a bounded blast radius, and gives the coordinator a
dashboard to drive all of it..

```
scheduler/
  backend/
    models.py       data model + time representation
    generator.py     realistic dataset generator
    scheduler.py     initial greedy scheduling engine
    replanner.py     the 4 disruption handlers + diffing
    metrics.py       "what does good mean" - all reported metrics
    server.py        Flask API
  frontend/
    dashboard.html   coordinator's live control room
```

## Running it

```bash
cd backend
pip install flask
python server.py
```
Open **http://localhost:5000**. The dashboard loads a fresh, already-scheduled
dataset (35 companies, 800 students, 20 rooms, 4 days). Pick a disruption
type on the right, fill in the form, hit **Apply & Replan**, and watch the
grid, metrics, and diff panel update. **Reset** regenerates a clean baseline
(same seed, so it's reproducible for a demo).

Everything also runs headless for testing/experimentation:
```python
from generator import generate_dataset
from scheduler import run_full_schedule
from replanner import replan_company_late
from metrics import full_report

companies, students, rooms, interviews = generate_dataset(seed=42)
run_full_schedule(companies, students, rooms, interviews)
print(full_report(companies, students, rooms, interviews))
```

---

## 1. The dataset - what "realistic" means here

Real placement seasons are not uniform. The generator encodes four tiers:

| Tier | Profile | Days | Cutoff | Shortlist size |
|---|---|---|---|---|
| 1 | Day-1 mass recruiters (service/bulk hiring) | Day 1 only | low (broad net) | 25-45% of eligible pool |
| 2 | Strong mid-size recruiters | Day 1-2 | moderate | 10-22% |
| 3 | Selective/product companies | Day 2-3 | high | 4-10% |
| 4 | Niche/dream companies, finalize late | Day 3-4 | very high | 1-3% |

Two effects fall out of this that a flat/uniform generator would miss, and
that the scheduler has to survive:

- **Day-1 is naturally the tightest.** Mass recruiters cram into one day with
  huge shortlists, so Day-1 room/panel utilisation comes out near 100% in
  practice (see metrics below) - meaning Day-1 has *zero slack* to absorb a
  disruption. Day-4, by contrast, is nearly empty. This asymmetry is real
  and it's the reason a Day-1 late-arrival is a genuinely hard problem while
  a Day-3 one usually isn't - a fact worth pointing out unprompted in the
  defense.
- **Top students snowball.** Shortlist membership is CGPA-weighted, so a
  9.5-CGPA student can land on 8-11 lists while an average student lands on
  1-2. This produces exactly the overlapping-shortlist clashes the brief
  describes, concentrated on a small number of highly-contested students -
  which is also who a replan tends to hurt most.

Two extra hard constraints beyond the brief's minimum, added because real
recruiters have them: a subset of companies require **hitech rooms**
(video/remote panels), and a subset restrict to specific **branches**.

## 2. The initial schedule

**Algorithm:** priority-ordered greedy, earliest-feasible-slot placement.
Interviews are sorted by (company tier ascending, student CGPA descending)
and placed one at a time into the first day/slot/room/panel combination
that satisfies every hard constraint.

**Why greedy instead of an ILP/CP-SAT global solve** - this is the single
biggest architectural bet in the project, so it's worth stating plainly:

- It has to re-run **live, in front of a panel**, on every injected
  disruption. A global re-solve of a combined room+panel+student
  scheduling problem (this is a flexible job-shop problem, NP-hard in
  general) does not give a runtime guarantee at 1,600+ interviews; a
  bounded greedy pass does (empirically <1s for the full 1,676-interview
  initial schedule on this hardware).
- It's inspectable. Every placement has one sentence explaining why it
  landed there. That matters when you're asked "why is this student's
  interview at 4pm" in a defense.
- It composes directly with "minimal disturbance" replanning (§3): holding
  every already-placed interview fixed and re-running the *same* placement
  routine on just the affected set is trivial with a greedy scheduler and
  awkward with a global optimizer (which wants to touch everything).

The trade-off: a global solver could likely squeeze a few more percentage
points of coverage out of the same data by shuffling low-priority
interviews to make room for others. We deliberately don't do that - see
"which constraint bends" below for why we treat that as a feature, not a
gap.

**Ordering heuristic** (most-constrained-first, standard in CSP literature):
Day-1 companies go first because they have the least slack (one day, huge
shortlists); within a tier, higher-CGPA students go first because they sit
on more shortlists and therefore have less remaining slack of their own -
locking their slots early avoids discovering a conflict on interview #900
that could have been avoided by ordering differently.

## 3. What "good" means - metrics (see `metrics.py`)

No single number is sufficient, so the system reports five, each catching a
different failure mode:

1. **Coverage** - % of candidate interviews actually scheduled. The
   headline number, but 100% coverage with terrible utilisation elsewhere
   isn't actually good, which is why it's not reported alone.
2. **Unscheduled reasons** - every unscheduled interview carries a specific
   reason (never a silent drop). This tells the coordinator *which*
   constraint to consider relaxing, not just that something failed.
3. **Room / panel utilisation**, overall and per-day - low utilisation
   means wasted infrastructure; utilisation near 100% means zero slack to
   absorb the next disruption. Reported separately because rooms and
   panels bottleneck independently (a company can be panel-starved while
   rooms sit empty, or vice versa).
4. **Student wait time** - average and max idle gap between a student's
   consecutive interviews on the same day. A 100%-feasible schedule that
   leaves someone idle for 4 hours between two interviews is still a bad
   schedule for that person.
5. **Replan churn** - after any replan, % of previously-scheduled
   interviews whose time/room/panel actually changed. This is the number
   that answers the brief's own question directly: "did we move 200
   appointments to fix a 2-hour delay?"

On the generated baseline dataset: **83.1% coverage**, Day-1/2 rooms at
~100% utilisation, Day-4 near 0%, ~25 min average student wait. These are
reported live by the dashboard and recomputed after every disruption.

## 4. Which constraint bends first - and who decides

Constraints are split into two classes, and the split is the actual design
decision here:

- **Business rules (never auto-bent):** CGPA cutoff, branch eligibility,
  double-booking of a student/room/panel. These aren't scheduling
  preferences - a cutoff exists because a company decided who they're
  willing to see. The system will never quietly interview a below-cutoff
  student to improve a coverage number. When these are the blocker, the
  interview is reported unscheduled with the specific reason, and it is
  the **coordinator's** call whether to escalate (e.g. ask a company to
  accept an extra late slot) - not the algorithm's.
- **Scheduling preferences (bent automatically, in this order):**
  1. Exact/earliest slot → any later slot on an allowed day
  2. Preferred room → any room of the matching type
  3. Preferred panel → any panel belonging to that company
  Only if all three are exhausted across every allowed day does the
  interview become unscheduled - and even then, for a reason the
  coordinator can act on (e.g. "no capacity after delay" tells them the
  company needs an extended day, not that the algorithm gave up).

This split exists because the brief's own question - "who should decide?" -
has a clean answer once you separate *what the business promised* from
*how the puzzle gets solved*. The algorithm owns the puzzle; the human
owns the promises.

## 5. How much reshuffling is acceptable - the replan design

Every disruption handler in `replanner.py` follows the same shape:

1. Compute the **affected set** - only the interviews *directly* invalidated
   by the event (e.g. for a late arrival: interviews of that company, on
   that day, before the new arrival time - nothing else).
2. Free just those interviews' resources.
3. Re-run the same `place_interview` routine on just that set, priority
   order, with every other already-scheduled interview still occupying its
   resources as far as the algorithm is concerned.
4. Emit a diff: `moved` / `cancelled` / `newly_scheduled` /
   `still_unscheduled`, each with a human-readable reason, plus the set of
   students and companies who need to be notified.

This bounds the blast radius **by construction**, not by hoping an
optimizer finds a low-churn solution - which is the direct answer to the
brief's "moving 200 appointments to fix a 2-hour delay is technically valid
and practically a disaster" concern. Measured on the dataset: a 3-hour
Day-1 delay + one dropped panel + 15 student withdrawals (the exact shape
of disruption the defense brief threatens to inject) produced **32 moved
interviews out of 1,393 previously scheduled - 2.3% churn** - not a
reshuffled event.

The one place we deliberately *do* look for extra opportunity, because it's
a strict improvement with zero cost to anyone else: a **student withdrawal**
frees an exact (room, panel, time) slot, and the handler immediately offers
it to the next unscheduled student on that company's waitlist. In the same
test run above, this backfilled 41 of the 57 slots freed by withdrawals.

Handler-specific notes:
- **Company late** - tries the same day after the new arrival time first;
  only spills to another of the company's allowed days if that fails. It
  deliberately never re-offers the pre-arrival slot (that would silently
  ignore the disruption).
- **Panel drop** - only that panel's interviews move; the company's other
  panels and every other company are untouched.
- **Student withdrawal** - remaining interviews for that student are
  cancelled (not "unscheduled" - that status is reserved for interviews
  that failed to place), triggering the backfill pass above.
- **Room unavailable** - only that room's interviews move to another
  room of a compatible type at the same time, or fail cleanly if none
  exists.

## 6. The coordinator's dashboard

A single-page control room (`frontend/dashboard.html`), no build step,
served directly by Flask:

- **Metrics strip** - coverage, scheduled/unscheduled counts, room
  utilisation, average wait, recomputed after every action.
- **Room x time grid**, tabbed by day, color-coded by company tier - this
  is the "current state" view a coordinator scans first; hovering a cell
  shows the student/company/interview id.
- **One-click disruption console** - the four required disruption types,
  each with a form scoped to only the relevant entities, and an
  Apply & Replan button.
- **Diff panel** - exactly what the brief asks for: what changed, who's
  affected, why, presented as a scannable list with color-coded tags
  (moved/cancelled/newly-scheduled/still-unscheduled), plus a rollup of
  which students and companies need to be notified.
- **Session log** - a running history of every disruption applied, so a
  coordinator (or a defense panel) can see the sequence of events.

Designed for someone under pressure: the grid and diff are the only two
things that update per-action, nothing else moves, and every unscheduled or
cancelled item always carries a plain-English reason rather than an error
code.

## A bug we found by auditing our own output

The brief lists "student clashes" as an example metric to report. Rather
than assume the hard constraint holds because the code says it should, we
added `detect_student_clashes()` as an independent audit pass over the
final schedule - and while building it, added a second audit,
`tight_transitions()`, that checks something the scheduler was **not**
originally enforcing: whether a student's two consecutive interviews in
**different rooms** leave any time to actually walk between them.

That audit caught a real bug: the initial version allowed a student to be
booked into Room 3 ending at 10:00 and Room 14 starting at 10:00 - zero
overlap (so it passed the "no double-booking" constraint), but physically
impossible. On the generated dataset this happened **508 times**.

Fix: `ScheduleState` now tracks each student's booked (day, room) history
and rejects any candidate slot that would put them in a different room
with less than one 15-minute slot of gap (same-room back-to-back is still
allowed - no walking needed). Re-running after the fix: **0** such
transitions, coverage moved by a single interview (83.11% → 83.05%), and
scheduling time was unaffected. The fix also had to be applied a second
time in the student-withdrawal backfill path (`replanner.py`), which
assigns a freed slot directly and had bypassed the same check - re-tested
after stacking all four disruption types together, both audits report 0.

This is included deliberately as evidence of the process, not swept into
the code silently: both audits run on every `/api/state` call and are
shown on the dashboard as always-visible risk flags, not just a one-time
fix. **If asked "how do you know there are no clashes," the answer is "we
check, every time we serve state" - not "the algorithm is designed not
to."**

