"""
Generates a realistic placement-week dataset.

Design decisions (defend these in the report):
- Companies are split into 4 tiers mirroring real placement seasons:
    Tier 1 (Day-1 mass recruiters): high CGPA cutoff flexibility (i.e. LOW
        cutoff so they shortlist broadly), huge shortlists, many panels,
        confined to Day 1 (real colleges front-load the big names).
    Tier 2: strong mid-size recruiters, Day 1-2, moderate cutoffs.
    Tier 3: selective/product companies, higher cutoffs, smaller shortlists,
        Day 2-3.
    Tier 4: niche/late/dream companies - very high cutoff, tiny shortlist,
        Day 3-4 (they finalize participation late).
- Top students snowball: a student's CGPA drives how many shortlists they
  land on (popular high-CGPA students appear on many overlapping lists -
  exactly the clash-heavy case the scheduler has to survive).
- A minority of companies require "hitech" rooms (video-panel / international
  interviewers) - an extra hard constraint beyond plain room counting.
- Branch eligibility is enforced for a subset of companies (core-branch-only
  recruiters), because real shortlists are not branch-blind.
"""
import random
from models import Company, Student, Room, Interview, DAYS

BRANCHES = ["CSE", "ECE", "EEE", "MECH", "CIVIL", "IT", "CHEM"]

TIER_CONFIG = {
    1: dict(count=6,  cutoff_range=(6.0, 6.5), panels=(4, 6), duration=(15, 15),
            days=[0], shortlist_frac=(0.25, 0.45), room="any"),
    2: dict(count=11, cutoff_range=(6.5, 7.5), panels=(3, 4), duration=(20, 30),
            days=[0, 1], shortlist_frac=(0.10, 0.22), room="any"),
    3: dict(count=12, cutoff_range=(7.5, 8.5), panels=(2, 3), duration=(30, 45),
            days=[1, 2], shortlist_frac=(0.04, 0.10), room="mixed"),
    4: dict(count=6,  cutoff_range=(8.5, 9.3), panels=(1, 2), duration=(30, 45),
            days=[2, 3], shortlist_frac=(0.01, 0.03), room="mixed"),
}

COMPANY_NAME_POOL = [
    "Aravalli Systems", "Bluepeak Analytics", "Cirrus Dynamics", "Delta Forge",
    "Emberlane Tech", "Fenwick & Rao", "Glowstone Robotics", "Helix Cloud",
    "Indus Quant", "Jetstream Logistics", "Kestrel Aerospace", "Lumenary AI",
    "Meridian Fintech", "Novasphere", "Orbital Chip Co", "Pinnacle Retail",
    "Quantico Labs", "Ridgeline Energy", "Solstice Bio", "Tesseract Corp",
    "Umbra Security", "Vertex Motors", "Wavelength Media", "Xylo Networks",
    "Yashoda Health", "Zenith Capital", "Anchorpoint Consulting", "Brightloop",
    "Coral Reef Games", "Driftwood Studios", "Everest Materials", "Falconworks",
    "Granite Cloud", "Harborlight", "Ironclad Defense",
]


def _rand_cutoff(rng, lo, hi):
    return round(rng.uniform(lo, hi), 1)


def _scaled_tier_counts(n_companies):
    """Scale the tier-1..4 company counts to sum to n_companies while
    preserving the original ratio (mass recruiters still dominate at any
    scale) - uses largest-remainder rounding so the total matches exactly."""
    base = {t: cfg["count"] for t, cfg in TIER_CONFIG.items()}
    base_total = sum(base.values())
    raw = {t: n_companies * c / base_total for t, c in base.items()}
    counts = {t: max(1, int(v)) for t, v in raw.items()}  # at least 1 per tier
    remainder = n_companies - sum(counts.values())
    # distribute any leftover (or claw back excess) by largest fractional part
    order = sorted(raw, key=lambda t: raw[t] - int(raw[t]), reverse=True)
    i = 0
    while remainder > 0:
        counts[order[i % len(order)]] += 1
        remainder -= 1
        i += 1
    while remainder < 0:
        t = order[i % len(order)]
        if counts[t] > 1:
            counts[t] -= 1
            remainder += 1
        i += 1
    return counts


def generate_companies(rng, n_companies=35):
    companies = []
    name_pool = COMPANY_NAME_POOL[:]
    rng.shuffle(name_pool)
    tier_counts = _scaled_tier_counts(n_companies)
    idx = 0
    name_uses = {}
    for tier, cfg in TIER_CONFIG.items():
        for _ in range(tier_counts[tier]):
            base_name = name_pool[idx % len(name_pool)]
            name_uses[base_name] = name_uses.get(base_name, 0) + 1
            name = base_name if name_uses[base_name] == 1 else f"{base_name} ({name_uses[base_name]})"
            idx += 1
            duration = rng.choice(list(range(cfg["duration"][0], cfg["duration"][1] + 1, 15)))
            room_req = "any"
            if cfg["room"] == "mixed":
                room_req = rng.choice(["any", "any", "hitech"])
            branch_req = BRANCHES if rng.random() > 0.35 else rng.sample(BRANCHES, k=rng.randint(2, 4))
            companies.append(Company(
                id=f"C{idx:03d}",
                name=name,
                tier=tier,
                cgpa_cutoff=_rand_cutoff(rng, *cfg["cutoff_range"]),
                num_panels=rng.randint(*cfg["panels"]),
                duration_min=duration,
                allowed_days=cfg["days"],
                room_requirement=room_req,
                eligible_branches=branch_req,
            ))
    return companies


def generate_students(rng, n=800):
    students = []
    for i in range(1, n + 1):
        # CGPA distribution skewed realistic: most students 6-8.5, tail to 9.8
        cgpa = round(min(9.8, max(5.0, rng.normalvariate(7.3, 0.9))), 2)
        branch = rng.choice(BRANCHES)
        students.append(Student(
            id=f"S{i:04d}",
            name=f"Student {i}",
            branch=branch,
            cgpa=cgpa,
        ))
    return students


def assign_shortlists(rng, companies, students):
    """Popular high-CGPA students land on many overlapping shortlists;
    each company draws its shortlist from eligible (cutoff+branch) students,
    weighted so higher-CGPA eligible students are more likely to be picked
    (mirrors real placement cells favouring stronger resumes for extra apps)."""
    for c in companies:
        eligible = [s for s in students if s.cgpa >= c.cgpa_cutoff and s.branch in c.eligible_branches]
        if not eligible:
            continue
        cfg = TIER_CONFIG[c.tier]
        frac = rng.uniform(*cfg["shortlist_frac"])
        k = max(1, min(len(eligible), round(frac * len(eligible))))
        weights = [1.0 + (s.cgpa - c.cgpa_cutoff) * 2 for s in eligible]
        chosen = rng.choices(eligible, weights=weights, k=min(k, len(eligible)))
        chosen_ids = list(dict.fromkeys(s.id for s in chosen))  # de-dup, keep order
        for sid in chosen_ids:
            student = next(s for s in students if s.id == sid)
            student.shortlisted_by.append(c.id)


def generate_rooms(n=20, hitech_count=5):
    rooms = []
    for i in range(1, n + 1):
        rtype = "hitech" if i <= hitech_count else "standard"
        rooms.append(Room(id=f"R{i:02d}", room_type=rtype))
    return rooms


def build_interviews(companies, students):
    interviews = []
    company_by_id = {c.id: c for c in companies}
    counter = 0
    for s in students:
        for cid in s.shortlisted_by:
            c = company_by_id[cid]
            counter += 1
            # priority: lower tier number = higher priority (scheduled first);
            # within a tier, higher CGPA students get a slight edge as a
            # tie-break (defensible: they have more competing offers/lists,
            # so locking their slot early reduces future churn).
            priority = c.tier * 100 - s.cgpa
            interviews.append(Interview(
                id=f"I{counter:05d}",
                student_id=s.id,
                company_id=cid,
                priority_score=priority,
            ))
    return interviews


def generate_dataset(seed=42, n_students=800, n_rooms=20, n_companies=35):
    rng = random.Random(seed)
    companies = generate_companies(rng, n_companies=n_companies)
    students = generate_students(rng, n=n_students)
    assign_shortlists(rng, companies, students)
    rooms = generate_rooms(n=n_rooms)
    interviews = build_interviews(companies, students)
    return companies, students, rooms, interviews


if __name__ == "__main__":
    companies, students, rooms, interviews = generate_dataset()
    print(f"Companies: {len(companies)}")
    print(f"Students: {len(students)}")
    print(f"Rooms: {len(rooms)}")
    print(f"Candidate interviews: {len(interviews)}")
    shortlisted_counts = [len(s.shortlisted_by) for s in students]
    print(f"Avg shortlists/student: {sum(shortlisted_counts)/len(students):.2f}, "
          f"max: {max(shortlisted_counts)}")
