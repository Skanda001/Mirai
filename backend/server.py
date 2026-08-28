"""
Flask API for the Placement Scheduler dashboard.

Run:  python server.py
Then open http://localhost:5000 in a browser.

All state lives in memory in this process (companies/students/rooms/
interviews). /api/reset regenerates a fresh dataset and re-runs the initial
schedule - use it to get back to a clean baseline before a demo.
"""
import json
import copy
import csv
import io
from flask import Flask, jsonify, request, send_from_directory, Response

from models import to_dict_list, slot_to_clock
from generator import generate_dataset
from scheduler import run_full_schedule
from metrics import full_report
from replanner import (
    replan_company_late, replan_panel_drop, replan_student_withdraw,
    replan_room_unavailable, diffs_to_dict,
)

app = Flask(__name__, static_folder="../frontend", static_url_path="")

STATE = {}


def init_state(seed=42, n_students=800, n_rooms=20, n_companies=35):
    companies, students, rooms, interviews = generate_dataset(
        seed=seed, n_students=n_students, n_rooms=n_rooms, n_companies=n_companies
    )
    run_full_schedule(companies, students, rooms, interviews)
    STATE["companies"] = companies
    STATE["students"] = students
    STATE["rooms"] = rooms
    STATE["interviews"] = interviews
    STATE["log"] = []  # history of disruptions applied this session


def snapshot_scheduled():
    return {iv.id: (iv.day, iv.start_slot, iv.room_id, iv.panel_no)
             for iv in STATE["interviews"] if iv.status == "scheduled"}


def _find_company(company_id):
    c = next((c for c in STATE["companies"] if c.id == company_id), None)
    if c is None:
        raise ValueError(f"No such company: {company_id!r}")
    return c


def _find_room(room_id):
    r = next((r for r in STATE["rooms"] if r.id == room_id), None)
    if r is None:
        raise ValueError(f"No such room: {room_id!r}")
    return r


def _find_student(student_id):
    s = next((s for s in STATE["students"] if s.id == student_id), None)
    if s is None:
        raise ValueError(f"No such student: {student_id!r}")
    return s


def _require(data, *fields):
    missing = [f for f in fields if f not in data or data[f] in (None, "")]
    if missing:
        raise ValueError(f"Missing required field(s): {', '.join(missing)}")


@app.errorhandler(ValueError)
def handle_value_error(e):
    return jsonify({"error": str(e)}), 400


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    app.logger.exception("Unexpected error handling request")
    return jsonify({"error": f"Unexpected server error: {e}"}), 500


@app.route("/api/export/csv")
def api_export_csv():
    company_by_id = {c.id: c for c in STATE["companies"]}
    student_by_id = {s.id: s for s in STATE["students"]}
    interviews = sorted(
        STATE["interviews"],
        key=lambda iv: (
            iv.day if iv.day is not None else 999,
            iv.start_slot if iv.start_slot is not None else 999,
            iv.room_id or "",
        )
    )

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "interview_id", "student_id", "student_name", "company_id", "company_name",
        "status", "day", "start_time", "room_id", "panel_no", "unscheduled_reason",
    ])
    for iv in interviews:
        student = student_by_id.get(iv.student_id)
        company = company_by_id.get(iv.company_id)
        start_time = slot_to_clock(iv.day, iv.start_slot) if iv.day is not None and iv.start_slot is not None else ""
        writer.writerow([
            iv.id, iv.student_id, student.name if student else "",
            iv.company_id, company.name if company else "",
            iv.status,
            (iv.day + 1) if iv.day is not None else "",
            start_time,
            iv.room_id or "", iv.panel_no if iv.panel_no is not None else "",
            iv.unscheduled_reason or "",
        ])

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=placement_schedule.csv"},
    )


@app.route("/")
def index():
    resp = send_from_directory(app.static_folder, "dashboard.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/state")
def api_state():
    companies, students, rooms, interviews = (
        STATE["companies"], STATE["students"], STATE["rooms"], STATE["interviews"]
    )
    return jsonify({
        "companies": to_dict_list(companies),
        "students": to_dict_list(students),
        "rooms": to_dict_list(rooms),
        "interviews": to_dict_list(interviews),
        "report": full_report(companies, students, rooms, interviews),
        "log": STATE["log"],
    })


@app.route("/api/reset", methods=["POST"])
def api_reset():
    data = request.get_json(silent=True) or {}
    seed = int(data.get("seed", 42))

    def _bounded(key, default, lo, hi):
        val = data.get(key, default)
        try:
            val = int(val)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a whole number")
        if not (lo <= val <= hi):
            raise ValueError(f"{key} must be between {lo} and {hi}, got {val}")
        return val

    n_students = _bounded("n_students", 800, 10, 5000)
    n_rooms = _bounded("n_rooms", 20, 1, 100)
    n_companies = _bounded("n_companies", 35, 4, 150)

    init_state(seed=seed, n_students=n_students, n_rooms=n_rooms, n_companies=n_companies)
    return api_state()


@app.route("/api/disrupt/company_late", methods=["POST"])
def api_company_late():
    data = request.get_json(force=True) or {}
    _require(data, "company_id", "day", "delay_slots")
    company = _find_company(data["company_id"])
    day = int(data["day"])
    if not (0 <= day < 4):
        raise ValueError(f"Day must be 0-3, got {day}")
    delay_slots = int(data["delay_slots"])
    if delay_slots <= 0:
        raise ValueError("delay_slots must be positive")
    if day not in company.allowed_days:
        raise ValueError(f"{company.name} does not interview on day {day+1} "
                          f"(allowed: {[d+1 for d in company.allowed_days]})")

    before = snapshot_scheduled()
    diffs = replan_company_late(
        STATE["companies"], STATE["rooms"], STATE["interviews"],
        company.id, day, delay_slots,
    )
    result = diffs_to_dict(diffs)
    result["report"] = full_report(STATE["companies"], STATE["students"],
                                    STATE["rooms"], STATE["interviews"], before)
    STATE["log"].append({"type": "company_late", "params": data, "summary": result["summary"]})
    return jsonify(result)


@app.route("/api/disrupt/panel_drop", methods=["POST"])
def api_panel_drop():
    data = request.get_json(force=True) or {}
    _require(data, "company_id", "panel_no")
    company = _find_company(data["company_id"])
    panel_no = int(data["panel_no"])
    if not (0 <= panel_no < company.num_panels):
        raise ValueError(f"{company.name} only has panels 0-{company.num_panels-1}, got {panel_no}")

    before = snapshot_scheduled()
    diffs, _ = replan_panel_drop(
        STATE["companies"], STATE["rooms"], STATE["interviews"],
        company.id, panel_no,
    )
    result = diffs_to_dict(diffs)
    result["report"] = full_report(STATE["companies"], STATE["students"],
                                    STATE["rooms"], STATE["interviews"], before)
    STATE["log"].append({"type": "panel_drop", "params": data, "summary": result["summary"]})
    return jsonify(result)


@app.route("/api/disrupt/student_withdraw", methods=["POST"])
def api_student_withdraw():
    data = request.get_json(force=True) or {}
    _require(data, "student_id")
    student = _find_student(data["student_id"])
    if student.withdrawn:
        raise ValueError(f"{student.id} has already withdrawn")

    before = snapshot_scheduled()
    diffs = replan_student_withdraw(
        STATE["companies"], STATE["rooms"], STATE["interviews"], student.id,
    )
    student.withdrawn = True
    result = diffs_to_dict(diffs)
    result["report"] = full_report(STATE["companies"], STATE["students"],
                                    STATE["rooms"], STATE["interviews"], before)
    STATE["log"].append({"type": "student_withdraw", "params": data, "summary": result["summary"]})
    return jsonify(result)


@app.route("/api/disrupt/room_unavailable", methods=["POST"])
def api_room_unavailable():
    data = request.get_json(force=True) or {}
    _require(data, "room_id")
    room = _find_room(data["room_id"])

    before = snapshot_scheduled()
    diffs = replan_room_unavailable(
        STATE["companies"], STATE["rooms"], STATE["interviews"], room.id,
    )
    result = diffs_to_dict(diffs)
    result["report"] = full_report(STATE["companies"], STATE["students"],
                                    STATE["rooms"], STATE["interviews"], before)
    STATE["log"].append({"type": "room_unavailable", "params": data, "summary": result["summary"]})
    return jsonify(result)


if __name__ == "__main__":
    init_state()
    app.run(debug=False, use_reloader=False, port=5000)
