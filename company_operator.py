#!/usr/bin/env python3
"""
company_operator.py - an autonomous "AI employee" prototype (single file, stdlib only, Python 3.9+)

WHAT IT DOES
    Takes a short company request such as
        "Process the October 2026 expense reports: submit the compliant claims to the finance
         portal, get manager approval for anything over the threshold, flag policy violations,
         and give me a summary."
    and turns it into COMPLETED, VERIFIED work across several company systems:
        - documents      : policy.md (rules), employees.json (context), expenses.csv (the work)
        - file system    : receipts/ folder (evidence checks), outbox/ + reports/ (deliverables)
        - internal system: a real HTTP "Finance Portal" (started locally) with realistic failures

THE LOOP (Goal -> Understand -> Plan -> Execute -> Observe -> Adapt -> Verify -> Complete)
    Understand : goal -> structured intent; asks the human if month/year is missing
    Plan       : reads company context first (policy, employees, rows, receipts), then classifies
                 every claim BEFORE touching any system (compliant / needs approval / violation /
                 duplicate / out of period)
    Execute    : the planner proposes candidate actions; a permission GUARD vets each one
    Observe    : every tool result updates explicit state (facts, per-claim records, events)
    Adapt      : transient 503s -> backoff retry; stale cost-center -> look up portal reference
                 data and remap; denied approval -> hold the claim; blocked action -> never run
    Verify     : re-reads the portal independently and compares ids/amounts/cost centers.
                 (The portal silently loses one write on purpose - only verification catches it.)
    Complete   : writes a summary to outbox/, reads it back, returns evidence + a JSON trace

DESIGN CHOICES (honest notes)
    * The default planner is deterministic and rule-based so the prototype runs offline and the
      safety/verification logic is testable. `--planner llm` plugs a Claude model into the
      decisions where judgement helps (goal understanding, ordering of feasible next steps) via
      the Anthropic API (needs ANTHROPIC_API_KEY). The LLM only chooses among actions the
      system already considers feasible, and every action still passes the guard. Any LLM
      error falls back to the deterministic planner.
    * The `http` tool is the integration seam. In production you would register more tools
      (Playwright browser, desktop computer-use, Gmail/Slack/ERP APIs) in ToolBox with the same
      Action -> Result contract; planner, guard, retry, verification and tracing stay unchanged.
    * Hard policy (e.g. "no payment above the approval threshold without a recorded human
      approval") is enforced in code by the Guard, not by prompting.

USAGE
    python company_operator.py                       # interactive: asks you for approvals
    python company_operator.py --approver yes        # auto-approve (demo)
    python company_operator.py --approver no         # auto-deny
    python company_operator.py --goal "Review October 2026 expenses, don't submit anything"
    python company_operator.py --planner llm         # optional, needs ANTHROPIC_API_KEY
    python company_operator.py --selftest            # runs scenario tests
"""

from __future__ import annotations

import argparse
import calendar
import csv
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

MAX_ATTEMPTS = 4          # max submit attempts per claim before it is escalated as failed
MAX_TRANSIENT_RETRIES = 3  # automatic retries for transient tool errors
MAX_STEPS = 80
DEFAULT_GOAL = (
    "Process the October 2026 expense reports: submit the compliant claims to the finance "
    "portal, get manager approval for anything over the threshold, flag policy violations, "
    "and give me a summary."
)


def money(x: float) -> str:
    return f"${x:,.2f}"


# ======================================================================================
# 1. SIMULATED COMPANY ENVIRONMENT (documents + an internal web system with real faults)
# ======================================================================================

POLICY_MD = """# Expense Policy (v3)

Category limits per claim (USD):
- meals: 75
- travel: 400
- lodging: 700
- software: 300
- other: 100

Thresholds (USD):
- receipt_required_over: 25
- manager_approval_over: 500

Rules:
- A receipt file is required for any claim over the receipt threshold.
- Claims above the manager approval threshold need explicit manager approval before filing.
- Claims must be filed in the period in which they were incurred.
- Duplicate claims must not be filed twice.
"""

# NOTE: erin's cost center is STALE (MKT-100 was renamed MKT-200). The portal will reject it.
EMPLOYEES = {
    "alice": {"dept": "Engineering", "cost_center": "ENG-100", "manager": "morgan"},
    "bob": {"dept": "Operations", "cost_center": "OPS-300", "manager": "morgan"},
    "carol": {"dept": "Sales", "cost_center": "SAL-400", "manager": "morgan"},
    "dave": {"dept": "Operations", "cost_center": "OPS-300", "manager": "morgan"},
    "erin": {"dept": "Marketing", "cost_center": "MKT-100", "manager": "morgan"},
    "frank": {"dept": "Engineering", "cost_center": "ENG-100", "manager": "morgan"},
}

EXPENSE_COLUMNS = ["id", "employee", "date", "vendor", "category", "amount", "receipt", "description"]
EXPENSE_ROWS = [
    ["E001", "alice", "2026-10-02", "Uber", "travel", "42.50", "e001.pdf", "Airport ride"],
    ["E002", "bob", "2026-10-03", "Starbucks", "meals", "18.00", "", "Coffee with client"],
    ["E003", "carol", "2026-10-05", "Marriott", "lodging", "640.00", "e003.pdf", "Conference hotel"],
    ["E004", "dave", "2026-10-06", "Prime Steakhouse", "meals", "210.00", "e004.pdf", "Team dinner"],
    ["E005", "erin", "2026-10-08", "Canva", "software", "120.00", "e005.pdf", "Design subscription"],
    ["E006", "alice", "2026-10-02", "Uber", "travel", "42.50", "e006.pdf", "Airport ride (again)"],
    ["E007", "bob", "2026-10-12", "Delta", "travel", "380.00", "", "Flight to Austin"],
    ["E008", "frank", "2026-09-28", "Office Depot", "other", "60.00", "e008.pdf", "Printer paper"],
    ["E009", "carol", "2026-10-15", "AWS", "software", "280.00", "e009.pdf", "Sandbox hosting"],
]


def seed_workspace(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for d in ("receipts", "outbox", "reports"):
        (root / d).mkdir(exist_ok=True)
    (root / "policy.md").write_text(POLICY_MD, encoding="utf-8")
    (root / "employees.json").write_text(json.dumps(EMPLOYEES, indent=2), encoding="utf-8")
    with open(root / "expenses.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(EXPENSE_COLUMNS)
        writer.writerows(EXPENSE_ROWS)
    for row in EXPENSE_ROWS:
        if row[6]:
            (root / "receipts" / row[6]).write_text(f"[scanned receipt for {row[0]}]\n", encoding="utf-8")


class FinancePortal:
    """A small real HTTP service standing in for an internal finance system."""

    COST_CENTERS = [
        {"code": "ENG-100", "department": "Engineering"},
        {"code": "MKT-200", "department": "Marketing"},
        {"code": "OPS-300", "department": "Operations"},
        {"code": "SAL-400", "department": "Sales"},
    ]

    def __init__(self, inject_faults: bool = True):
        self.inject_faults = inject_faults
        self.expenses: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.post_calls = 0
        self.lost_once: set[str] = set()
        self.server: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self.port = 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        portal = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # silence default logging
                pass

            def _send(self, code: int, obj: Any) -> None:
                body = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path == "/api/cost-centers":
                    self._send(200, {"cost_centers": portal.COST_CENTERS})
                elif self.path == "/api/expenses":
                    with portal.lock:
                        items = list(portal.expenses.values())
                    self._send(200, {"count": len(items), "expenses": items})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self) -> None:
                if self.path != "/api/expenses":
                    self._send(404, {"error": "not found"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("payload must be an object")
                except (ValueError, UnicodeDecodeError):
                    self._send(400, {"error": "invalid JSON body"})
                    return
                with portal.lock:
                    portal.post_calls += 1
                    if portal.inject_faults and portal.post_calls == 1:
                        self._send(503, {"error": "service temporarily unavailable"})
                        return
                    missing = [k for k in ("id", "employee", "date", "vendor", "amount", "cost_center")
                               if k not in payload or payload[k] in (None, "")]
                    if missing:
                        self._send(422, {"error": f"missing fields: {missing}", "field": missing[0]})
                        return
                    try:
                        amount = float(payload["amount"])
                        if amount <= 0:
                            raise ValueError
                    except (TypeError, ValueError):
                        self._send(422, {"error": "amount must be a positive number", "field": "amount"})
                        return
                    valid = [c["code"] for c in portal.COST_CENTERS]
                    if payload["cost_center"] not in valid:
                        self._send(422, {"error": f"unknown cost_center '{payload['cost_center']}'",
                                         "field": "cost_center", "valid": valid})
                        return
                    if payload["id"] in portal.expenses:
                        self._send(409, {"error": "claim id already exists", "code": "duplicate_id"})
                        return
                    key = (str(payload["employee"]).lower(), str(payload["vendor"]).lower(),
                           round(amount, 2), str(payload["date"]))
                    for other in portal.expenses.values():
                        okey = (other["employee"].lower(), other["vendor"].lower(),
                                round(float(other["amount"]), 2), str(other["date"]))
                        if okey == key:
                            self._send(409, {"error": "duplicate claim content", "code": "duplicate_content"})
                            return
                    ref = f"FIN-{1000 + portal.post_calls}"
                    if portal.inject_faults and payload["id"] == "E009" and payload["id"] not in portal.lost_once:
                        # Silent data loss: acknowledges success but does NOT persist the claim.
                        portal.lost_once.add(payload["id"])
                        self._send(201, {"ref": ref, "status": "created"})
                        return
                    record = dict(payload)
                    record["amount"] = amount
                    record["ref"] = ref
                    portal.expenses[payload["id"]] = record
                    self._send(201, {"ref": ref, "status": "created"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


# ======================================================================================
# 2. CORE TYPES, TOOLS, GUARD, HUMAN-IN-THE-LOOP
# ======================================================================================

@dataclass
class Action:
    tool: str
    args: dict = field(default_factory=dict)
    rationale: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class Result:
    ok: bool
    data: Any = None
    error: str = ""
    transient: bool = False
    blocked: bool = False
    status: Optional[int] = None

    def brief(self, n: int = 110) -> str:
        if self.blocked:
            return f"BLOCKED: {self.error}"[:n]
        if not self.ok:
            return f"FAILED: {self.error}"[:n]
        tag = f"OK {self.status}" if self.status else "OK"
        d = self.data
        if isinstance(d, str):
            return f"{tag} ({len(d)} chars)"
        if isinstance(d, (dict, list)):
            return f"{tag} {json.dumps(d, default=str)}"[:n]
        return tag


def resolve_in(root: Path, rel: str) -> Path:
    p = (root / rel).resolve()
    if p != root and not p.is_relative_to(root):
        raise PermissionError(f"path escapes workspace: {rel}")
    return p


class Approver:
    """Human-in-the-loop channel. mode: ask (interactive) | yes | no. Optional scripted answers."""

    def __init__(self, mode: str = "ask", script: Optional[list] = None, verbose: bool = True):
        self.mode = mode
        self.script = list(script) if script else []
        self.verbose = verbose
        self.log: list = []

    def ask(self, question: str, kind: str) -> str:
        if self.script:
            ans = self.script.pop(0)
            src = "scripted"
        elif self.mode == "ask":
            try:
                ans = input(f"\n[HUMAN INPUT NEEDED] {question}\n> ")
            except EOFError:
                ans = "no"
            src = "human"
        elif kind == "clarify":
            ans, src = "", "auto (cannot answer open questions)"
        else:
            ans, src = ("yes" if self.mode == "yes" else "no"), f"auto (--approver {self.mode})"
        if self.verbose and src != "human":
            print(f"      [HUMAN INPUT] {question}\n      -> answer: {ans!r} [{src}]")
        self.log.append((kind, question, ans))
        return ans


class ToolBox:
    """Tools the operator can use. Each returns a Result. Add new tools as t_<name> methods."""

    def __init__(self, workspace: Path, portal_url: str, approver: Approver):
        self.root = workspace.resolve()
        self.portal_url = portal_url.rstrip("/")
        self.approver = approver

    def run(self, action: Action, state: "State") -> Result:
        fn = getattr(self, "t_" + action.tool, None)
        if fn is None:
            return Result(False, error=f"unknown tool '{action.tool}'")
        try:
            return fn(state, **action.args)
        except TypeError as e:
            return Result(False, error=f"bad arguments: {e}")
        except Exception as e:  # tools must never crash the loop
            return Result(False, error=f"{type(e).__name__}: {e}")

    def t_list_dir(self, state: "State", path: str) -> Result:
        p = resolve_in(self.root, path)
        if not p.is_dir():
            return Result(False, error=f"not a directory: {path}")
        return Result(True, {"entries": sorted(x.name for x in p.iterdir())})

    def t_read_file(self, state: "State", path: str) -> Result:
        p = resolve_in(self.root, path)
        if not p.is_file():
            return Result(False, error=f"file not found: {path}")
        return Result(True, p.read_text(encoding="utf-8")[:200_000])

    def t_write_file(self, state: "State", path: str, content: str) -> Result:
        p = resolve_in(self.root, path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return Result(True, {"path": path, "bytes": len(content.encode("utf-8"))})

    def t_http(self, state: "State", method: str, path: str, body: Optional[dict] = None) -> Result:
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.portal_url + path, data=data, method=method.upper(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw, code = resp.read().decode("utf-8", "replace"), resp.status
        except urllib.error.HTTPError as e:
            raw, code = e.read().decode("utf-8", "replace"), e.code
        except (urllib.error.URLError, OSError) as e:
            return Result(False, error=f"network error: {e}", transient=True)
        try:
            parsed: Any = json.loads(raw)
        except ValueError:
            parsed = raw
        if 200 <= code < 300:
            return Result(True, parsed, status=code)
        err = parsed.get("error") if isinstance(parsed, dict) else str(parsed)[:100]
        return Result(False, parsed, error=f"HTTP {code}: {err}",
                      transient=code in (429, 502, 503, 504), status=code)

    def t_ask_human(self, state: "State", question: str, kind: str = "approval") -> Result:
        return Result(True, {"answer": self.approver.ask(question, kind)})

    def t_analyze_claims(self, state: "State") -> Result:
        records = analyze(state)
        counts: dict[str, int] = {}
        for r in records.values():
            counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        return Result(True, {"records": records, "counts": counts})


class Guard:
    """Deterministic permission layer. The planner (rules OR an LLM) can never bypass it."""

    WRITE_DIRS = ("outbox", "reports")

    def __init__(self, workspace: Path):
        self.root = workspace.resolve()

    def check(self, action: Action, state: "State") -> Optional[str]:
        t, a = action.tool, action.args
        if t in ("read_file", "list_dir"):
            try:
                resolve_in(self.root, a.get("path", ""))
            except PermissionError as e:
                return str(e)
        elif t == "write_file":
            try:
                p = resolve_in(self.root, a.get("path", ""))
            except PermissionError as e:
                return str(e)
            rel = p.relative_to(self.root)
            if not rel.parts or rel.parts[0] not in self.WRITE_DIRS:
                return "writes are only allowed inside outbox/ and reports/"
        elif t == "http":
            method, path = str(a.get("method", "GET")).upper(), str(a.get("path", ""))
            if method not in ("GET", "POST"):
                return f"HTTP method {method} not permitted"
            if not path.startswith("/api/"):
                return "only the finance portal /api/ is reachable"
            if method == "POST":
                if path != "/api/expenses":
                    return "POST allowed only to /api/expenses"
                if not state.intent.get("submit"):
                    return "this task did not authorize filing claims (review-only)"
                body = a.get("body") or {}
                threshold = state.facts.get("policy", {}).get("manager_approval_over")
                if threshold is None:
                    return "policy not loaded; refusing financial write"
                try:
                    amount = float(body.get("amount", 0))
                except (TypeError, ValueError):
                    return "invalid amount in payload"
                if amount > threshold and state.approvals.get(body.get("id")) != "approved":
                    return "amount above manager threshold requires a recorded human approval"
        elif t in ("ask_human", "analyze_claims"):
            pass
        else:
            return f"tool '{t}' is not permitted"
        return None


# ======================================================================================
# 3. STATE, DOMAIN LOGIC, PLANNER
# ======================================================================================

@dataclass
class State:
    goal: str
    intent: dict = field(default_factory=dict)
    facts: dict = field(default_factory=dict)       # policy, employees, rows, receipts, ...
    expenses: dict = field(default_factory=dict)    # claim id -> record
    approvals: dict = field(default_factory=dict)   # claim id -> approved | denied
    events: list = field(default_factory=list)      # human-readable recovery / safety events
    verify_state: str = "todo"
    analyzed: bool = False
    fatal: str = ""


class NeedInfo(Exception):
    def __init__(self, question: str):
        super().__init__(question)
        self.question = question


class Unsupported(Exception):
    pass


def parse_policy(text: str) -> dict:
    vals = {k: float(v) for k, v in
            re.findall(r"^\s*-\s*([a-z_]+):\s*([0-9]+(?:\.[0-9]+)?)\s*$", text, re.M)}
    for req in ("receipt_required_over", "manager_approval_over"):
        if req not in vals:
            raise ValueError(f"policy is missing '{req}'")
    return vals


def make_intent(month: int, year: int, submit: bool) -> dict:
    if not (1 <= month <= 12) or not (1990 <= year <= 2100):
        raise ValueError("invalid period")
    last = calendar.monthrange(year, month)[1]
    return {"type": "expense_processing",
            "label": f"{calendar.month_name[month]} {year}",
            "start": date(year, month, 1).isoformat(),
            "end": date(year, month, last).isoformat(),
            "submit": bool(submit)}


def find_month(text: str) -> Optional[int]:
    for i in range(1, 13):
        name = calendar.month_name[i]
        pat = r"\bMay\b" if i == 5 else rf"(?i)\b{name}\b"
        if re.search(pat, text):
            return i
    return None


def analyze(s: State) -> dict:
    """Classify every claim against company policy. Pure function of state facts."""
    pol, emps, rows = s.facts["policy"], s.facts["employees"], s.facts["rows"]
    receipts = set(s.facts["receipts"])
    start, end = date.fromisoformat(s.intent["start"]), date.fromisoformat(s.intent["end"])
    items = []
    for idx, row in enumerate(rows, 1):
        rid = (row.get("id") or f"ROW{idx}").strip()
        try:
            d: Optional[date] = date.fromisoformat((row.get("date") or "").strip())
        except ValueError:
            d = None
        items.append((d or date.max, rid, row, d))
    items.sort(key=lambda t: (t[0], t[1]))

    recs: dict = {}
    seen: dict = {}
    limit_keys = {"receipt_required_over", "manager_approval_over"}
    for _, rid, row, d in items:
        emp = (row.get("employee") or "").strip().lower()
        vendor = (row.get("vendor") or "").strip()
        category = (row.get("category") or "").strip().lower()
        reasons: list = []
        try:
            amount = float(row.get("amount", ""))
        except (TypeError, ValueError):
            amount = 0.0
            reasons.append("invalid amount")
        if d is None:
            reasons.append("invalid date")
        rec = {"id": rid, "row": row, "employee": emp, "vendor": vendor, "category": category,
               "amount": amount, "date": str(d) if d else (row.get("date") or ""),
               "reasons": reasons, "attempts": 0, "cost_center": None, "note": "",
               "verified": False, "portal_ref": None}
        if d is not None and not (start <= d <= end):
            rec.update(verdict="out_of_period", status="skipped",
                       reasons=[f"dated {d}, outside {s.intent['label']}"])
        else:
            key = (emp, vendor.lower(), round(amount, 2), str(d))
            if not reasons and key in seen:
                rec.update(verdict="duplicate", status="skipped",
                           reasons=[f"same employee/vendor/amount/date as {seen[key]}"])
            else:
                if not reasons:
                    seen[key] = rid
                if emp not in emps:
                    reasons.append(f"unknown employee '{emp}'")
                limit = pol.get(category) if category not in limit_keys else None
                if limit is None:
                    reasons.append(f"unknown category '{category}'")
                elif amount > limit:
                    reasons.append(f"{category} claim {money(amount)} exceeds limit {money(limit)}")
                if amount > pol["receipt_required_over"]:
                    rcpt = (row.get("receipt") or "").strip()
                    if not rcpt:
                        reasons.append(f"receipt required over {money(pol['receipt_required_over'])} but none attached")
                    elif rcpt not in receipts:
                        reasons.append(f"receipt file '{rcpt}' not found")
                if reasons:
                    rec.update(verdict="violation", status="flagged", reasons=reasons)
                elif amount > pol["manager_approval_over"]:
                    rec.update(verdict="needs_approval", status="awaiting_approval",
                               reasons=[f"over manager approval threshold {money(pol['manager_approval_over'])}"])
                else:
                    rec.update(verdict="compliant", status="pending")
                if rec["verdict"] in ("compliant", "needs_approval"):
                    rec["cost_center"] = emps[emp]["cost_center"]
        recs[rid] = rec
    return recs


def ordered_ids(s: State) -> list:
    return sorted(s.expenses, key=lambda i: (s.expenses[i]["date"], i))


def outcome(s: State) -> tuple:
    recs = s.expenses.values()
    sub = [r for r in recs if r["status"] == "submitted"]
    failed = [r for r in recs if r["status"] == "failed"]
    held = [r for r in recs if r["status"] in ("held", "awaiting_approval")]
    flagged = [r for r in recs if r["status"] == "flagged"]
    skipped = [r for r in recs if r["status"] == "skipped"]
    ready = [r for r in recs if r["status"] == "pending"]
    total = sum(r["amount"] for r in sub)
    verified = sum(1 for r in sub if r["verified"])
    if not s.intent.get("submit"):
        text = (f"Review only (nothing filed): {len(ready)} compliant, {len(held)} need approval, "
                f"{len(flagged)} violations, {len(skipped)} duplicate/out-of-period.")
        return "completed", text
    status = "partial" if (failed or ready) else "completed"
    text = (f"{len(sub)} claims filed ({money(total)}), {verified} independently verified on the portal; "
            f"{len(flagged)} flagged as policy violations; {len(held)} held for approval; "
            f"{len(skipped)} skipped (duplicate/out of period); {len(failed)} failed and need a human.")
    return status, text


def build_report(s: State) -> str:
    recs = s.expenses
    ids = ordered_ids(s)
    status, summary = outcome(s)
    L = [f"# Expense processing summary - {s.intent['label']}", "",
         f"**Status:** {status.upper()}", f"**Result:** {summary}", ""]

    def table(title: str, pred, cols) -> None:
        rows = [recs[i] for i in ids if pred(recs[i])]
        if not rows:
            return
        L.append(f"## {title} ({len(rows)})")
        L.append("| " + " | ".join(c[0] for c in cols) + " |")
        L.append("|" + "---|" * len(cols))
        for r in rows:
            L.append("| " + " | ".join(str(c[1](r)) for c in cols) + " |")
        L.append("")

    base = [("Claim", lambda r: r["id"]), ("Employee", lambda r: r["employee"]),
            ("Vendor", lambda r: r["vendor"]), ("Amount", lambda r: money(r["amount"]))]
    table("Filed on finance portal", lambda r: r["status"] == "submitted",
          base + [("Cost center", lambda r: r["cost_center"]), ("Portal ref", lambda r: r["portal_ref"]),
                  ("Verified", lambda r: "yes" if r["verified"] else "NO"),
                  ("Attempts", lambda r: r["attempts"])])
    table("Held - approval denied or pending", lambda r: r["status"] in ("held", "awaiting_approval"),
          base + [("Why", lambda r: "; ".join(r["reasons"]) + (f" {r['note']}" if r["note"] else ""))])
    table("Flagged policy violations (not filed)", lambda r: r["status"] == "flagged",
          base + [("Reasons", lambda r: "; ".join(r["reasons"]))])
    table("Skipped", lambda r: r["status"] == "skipped",
          base + [("Reason", lambda r: "; ".join(r["reasons"]) + (f" {r['note']}" if r["note"] else ""))])
    table("FAILED - needs a human", lambda r: r["status"] == "failed",
          base + [("Problem", lambda r: r["note"])])
    if s.intent.get("submit") is False:
        table("Ready to file (review only)", lambda r: r["status"] == "pending", base)

    L.append("## Recovery and safety events")
    L.extend([f"- {e}" for e in s.events] or ["- none"])
    L.append("")
    L.append("## Verification evidence")
    if "portal_count" in s.facts:
        L.append(f"- Portal re-read after filing: {s.facts['portal_count']} claim(s) on record, "
                 f"total {money(s.facts['portal_total'])}.")
    else:
        L.append("- No portal verification was needed or possible.")
    L.append("- Policy, employee, expense and receipt data were read from the workspace "
             "before any action was taken.")
    return "\n".join(L) + "\n"


class RulePlanner:
    """Deterministic planner: proposes feasible next actions; interprets observations."""

    name = "rule"

    # ---------------- understanding ----------------
    def understand(self, goal: str) -> dict:
        g = goal.lower()
        if "expense" not in g:
            raise Unsupported("This prototype is specialised in expense-report processing; "
                              "I cannot safely do that request yet.")
        month = find_month(goal)
        ym = re.search(r"\b(20\d{2})\b", goal)
        if month is None or ym is None:
            raise NeedInfo("Which month and year of expenses should I process? (e.g. 'October 2026')")
        review_only = re.search(r"dry[- ]run|only review|just review|don't submit|do not submit|"
                                r"without submitting|review only", g)
        wants = re.search(r"\b(submit|file|enter|process|book|post)\b", g)
        return make_intent(month, int(ym.group(1)), bool(wants) and not review_only)

    # ---------------- planning ----------------
    def candidates(self, s: State) -> list:
        f = s.facts
        if s.fatal:
            return [Action("finish", {"status": "failed", "summary": s.fatal}, "Cannot continue safely")]

        setup = []
        if "policy" not in f:
            setup.append(Action("read_file", {"path": "policy.md"},
                                "Load expense policy: category limits and approval thresholds", {"kind": "read_policy"}))
        if "employees" not in f:
            setup.append(Action("read_file", {"path": "employees.json"},
                                "Load employee context: departments and cost centers", {"kind": "read_employees"}))
        if "rows" not in f:
            setup.append(Action("read_file", {"path": "expenses.csv"},
                                "Load the claims that need processing", {"kind": "read_rows"}))
        if "receipts" not in f:
            setup.append(Action("list_dir", {"path": "receipts"},
                                "See which receipt files actually exist", {"kind": "list_receipts"}))
        if setup:
            return setup
        if not s.analyzed:
            return [Action("analyze_claims", {},
                           "Classify every claim against policy before touching any system", {"kind": "analyze"})]

        ids = ordered_ids(s)
        if s.intent.get("submit"):
            asks = []
            for i in ids:
                r = s.expenses[i]
                if r["status"] == "awaiting_approval":
                    q = (f"Claim {i} ({r['employee']}, {r['vendor']}, {r['category']}, {money(r['amount'])}) is over the "
                         f"manager-approval threshold. Approve filing it? [y/N]")
                    asks.append(Action("ask_human", {"question": q, "kind": "approval"},
                                       f"{i} needs explicit manager approval before filing",
                                       {"kind": "approval", "expense_id": i}))
            if asks:
                return asks
            if any(r.get("needs_cc_refresh") for r in s.expenses.values()) and "cost_centers" not in f:
                return [Action("http", {"method": "GET", "path": "/api/cost-centers"},
                               "Portal rejected a cost center; fetch authoritative reference data", {"kind": "cc_lookup"})]
            subs = []
            for i in ids:
                r = s.expenses[i]
                if r["status"] == "pending" and not r.get("needs_cc_refresh"):
                    body = {"id": i, "employee": r["employee"], "date": r["date"], "vendor": r["vendor"],
                            "category": r["category"], "amount": r["amount"], "cost_center": r["cost_center"],
                            "description": r["row"].get("description", ""), "receipt": r["row"].get("receipt", "")}
                    subs.append(Action("http", {"method": "POST", "path": "/api/expenses", "body": body},
                                       f"File compliant claim {i} on the finance portal",
                                       {"kind": "submit", "expense_id": i}))
            if subs:
                return subs
            if s.verify_state == "todo" and any(r["status"] == "submitted" for r in s.expenses.values()):
                return [Action("http", {"method": "GET", "path": "/api/expenses"},
                               "Independently re-read the portal to confirm every filed claim really exists",
                               {"kind": "verify"})]

        if "report_written" not in f:
            return [Action("write_file", {"path": f"outbox/expense_summary_{s.intent['label'].replace(' ', '_')}.md",
                                          "content": build_report(s)},
                           "Write the summary deliverable for the requester", {"kind": "write_report"})]
        if "report_verified" not in f:
            return [Action("read_file", {"path": f["report_written"]},
                           "Read the deliverable back to confirm it was written", {"kind": "check_report"})]
        status, summary = outcome(s)
        return [Action("finish", {"status": status, "summary": summary}, "All required outcomes verified")]

    def choose(self, s: State, cands: list) -> Action:
        return cands[0]

    # ---------------- observing / adapting ----------------
    def observe(self, s: State, a: Action, r: Result) -> None:
        k = a.meta.get("kind")
        setup_kinds = ("read_policy", "read_employees", "read_rows", "list_receipts")
        if k in setup_kinds and not r.ok:
            s.fatal = f"Could not load required context via {a.tool} {a.args.get('path', '')}: {r.error}"
            return
        try:
            if k == "read_policy":
                s.facts["policy"] = parse_policy(r.data)
            elif k == "read_employees":
                s.facts["employees"] = json.loads(r.data)
            elif k == "read_rows":
                reader = csv.DictReader(io.StringIO(r.data))
                missing = [c for c in ("id", "employee", "date", "vendor", "category", "amount")
                           if c not in (reader.fieldnames or [])]
                if missing:
                    raise ValueError(f"expenses.csv is missing columns {missing}")
                s.facts["rows"] = list(reader)
            elif k == "list_receipts":
                s.facts["receipts"] = r.data["entries"]
        except (ValueError, KeyError, TypeError) as e:
            s.fatal = f"Context file for '{k}' is unusable: {e}"
            return

        if k == "analyze":
            if not r.ok:
                s.fatal = f"Analysis failed: {r.error}"
                return
            s.expenses = r.data["records"]
            s.analyzed = True
            s.events.append("Classified claims: " + ", ".join(f"{n} {v}" for v, n in sorted(r.data["counts"].items())))
        elif k == "approval":
            self._observe_approval(s, a, r)
        elif k == "submit":
            self._observe_submit(s, a, r)
        elif k == "cc_lookup":
            self._observe_cc_lookup(s, r)
        elif k == "verify":
            self._observe_verify(s, r)
        elif k == "write_report":
            if r.ok:
                s.facts["report_written"] = a.args["path"]
            else:
                s.fatal = f"Could not write the summary: {r.error}"
        elif k == "check_report":
            if r.ok and r.data:
                s.facts["report_verified"] = True
            else:
                s.fatal = f"Summary could not be read back: {r.error or 'empty file'}"

    def _observe_approval(self, s: State, a: Action, r: Result) -> None:
        eid = a.meta["expense_id"]
        rec = s.expenses[eid]
        ans = ((r.data or {}).get("answer", "") if r.ok else "").strip().lower()
        if ans in ("y", "yes", "approve", "approved", "ok"):
            s.approvals[eid] = "approved"
            rec["status"] = "pending"
            s.events.append(f"{eid}: human approved filing {money(rec['amount'])}")
        else:
            s.approvals[eid] = "denied"
            rec["status"] = "held"
            rec["note"] = "approval denied or not given"
            s.events.append(f"{eid}: approval NOT granted; claim held and not filed")

    def _remap(self, s: State, eid: str) -> None:
        rec = s.expenses[eid]
        dept = s.facts["employees"].get(rec["employee"], {}).get("dept", "")
        code = next((c["code"] for c in s.facts["cost_centers"]
                     if c["department"].lower() == dept.lower()), None)
        rec["needs_cc_refresh"] = False
        if code is None or code == rec.get("rejected_cc"):
            rec["status"], rec["note"] = "failed", f"no valid cost center found for department '{dept}'"
            s.events.append(f"{eid}: could not resolve a valid cost center for '{dept}'; escalating")
            return
        s.events.append(f"{eid}: portal rejected cost center {rec['cost_center']}; looked up portal reference "
                        f"data and remapped '{dept}' -> {code}")
        rec["cost_center"] = code

    def _observe_cc_lookup(self, s: State, r: Result) -> None:
        stale = [i for i, x in s.expenses.items() if x.get("needs_cc_refresh")]
        if not r.ok:
            for i in stale:
                s.expenses[i].update(status="failed", note=f"cost-center lookup failed: {r.error}",
                                     needs_cc_refresh=False)
            return
        s.facts["cost_centers"] = r.data["cost_centers"]
        for i in stale:
            self._remap(s, i)

    def _observe_submit(self, s: State, a: Action, r: Result) -> None:
        eid = a.meta["expense_id"]
        rec = s.expenses[eid]
        rec["attempts"] += 1
        data = r.data if isinstance(r.data, dict) else {}
        if r.ok:
            rec["status"], rec["portal_ref"] = "submitted", data.get("ref")
            s.verify_state = "todo"
        elif r.status == 409 and data.get("code") == "duplicate_id":
            rec["status"], rec["note"] = "submitted", "portal already had this claim id (idempotent resubmission)"
            s.verify_state = "todo"
        elif r.status == 409:
            rec["status"], rec["note"] = "skipped", "portal says an identical claim already exists"
            rec["reasons"] = ["portal reported duplicate claim content"]
        elif r.status == 422 and data.get("field") == "cost_center":
            rec["rejected_cc"] = rec["cost_center"]
            if "cost_centers" in s.facts:
                self._remap(s, eid)
            else:
                rec["needs_cc_refresh"] = True
        elif r.blocked:
            rec["status"], rec["note"] = "failed", r.error
        elif not r.transient:
            rec["status"], rec["note"] = "failed", r.error
        if rec["status"] == "pending" and rec["attempts"] >= MAX_ATTEMPTS:
            rec["status"], rec["note"] = "failed", f"gave up after {rec['attempts']} attempts: {r.error}"
            s.events.append(f"{eid}: gave up after {rec['attempts']} attempts")

    def _observe_verify(self, s: State, r: Result) -> None:
        s.verify_state = "done"
        if not r.ok:
            s.events.append(f"Verification read failed ({r.error}); filed claims remain UNVERIFIED")
            return
        portal = {e["id"]: e for e in r.data.get("expenses", [])}
        s.facts["portal_count"] = len(portal)
        s.facts["portal_total"] = sum(float(e["amount"]) for e in portal.values())
        for i, rec in s.expenses.items():
            if rec["status"] != "submitted":
                continue
            p = portal.get(i)
            if p is None:
                s.events.append(f"{i}: portal acknowledged the write but the claim is MISSING on re-read "
                                f"(silent data loss); re-filing")
                rec["verified"] = False
                if rec["attempts"] >= MAX_ATTEMPTS:
                    rec["status"], rec["note"] = "failed", "claim missing on portal after repeated attempts"
                else:
                    rec["status"] = "pending"
            elif abs(float(p["amount"]) - rec["amount"]) > 0.005 or p.get("cost_center") != rec["cost_center"]:
                rec["status"], rec["note"] = "failed", "portal record differs from what was filed"
                rec["verified"] = False
                s.events.append(f"{i}: portal record differs from what was filed; escalating")
            else:
                rec["verified"] = True


def _extract_json(text: str) -> Any:
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("no JSON object in model output")
    return json.loads(text[a:b + 1])


class LLMPlanner(RulePlanner):
    """Optional: lets a Claude model decide ambiguous steps. Falls back to rules on any error."""

    name = "llm"

    def __init__(self, model: Optional[str] = None):
        self.key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model or os.environ.get("OPERATOR_MODEL", "claude-sonnet-5-5")
        self.enabled = bool(self.key)
        self.failures = 0
        if not self.enabled:
            print("      [llm planner] ANTHROPIC_API_KEY not set - using deterministic planner.")

    def _call(self, purpose: str, system: str, user: str) -> str:
        body = {"model": self.model, "max_tokens": 300, "system": system,
                "messages": [{"role": "user", "content": user}]}
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=json.dumps(body).encode("utf-8"),
            headers={"content-type": "application/json", "x-api-key": self.key,
                     "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")

    def _fail(self, e: Exception) -> None:
        self.failures += 1
        print(f"      [llm planner] call failed ({type(e).__name__}); falling back to rules.")
        if self.failures >= 2:
            self.enabled = False

    def understand(self, goal: str) -> dict:
        if self.enabled:
            try:
                system = ("Extract the intent of a company request. Reply with JSON only: "
                          '{"intent":"expense_processing" or "other","month":1-12 or null,"year":integer or null,'
                          '"submit":true if the request asks to file/submit/process claims, false if review only}')
                d = _extract_json(self._call("understand", system, goal))
                if d.get("intent") == "expense_processing" and isinstance(d.get("month"), int) \
                        and isinstance(d.get("year"), int):
                    return make_intent(d["month"], d["year"], bool(d.get("submit", True)))
            except Exception as e:
                self._fail(e)
        return super().understand(goal)

    def choose(self, s: State, cands: list) -> Action:
        if len(cands) <= 1 or not self.enabled:
            return cands[0]
        try:
            opts = "\n".join(f"{i}: {a.tool} {json.dumps(a.args, default=str)[:140]} -- {a.rationale}"
                             for i, a in enumerate(cands))
            system = ("You are the planning module of an autonomous operator. Pick the best next action among the "
                      "feasible options (order matters only for efficiency; all are safe). "
                      'Reply with JSON only: {"choice": <index>, "reason": "<short>"}')
            d = _extract_json(self._call("choose", system, f"Goal: {s.goal}\nOptions:\n{opts}"))
            idx = d.get("choice")
            if isinstance(idx, int) and 0 <= idx < len(cands):
                chosen = cands[idx]
                chosen.rationale += f" [LLM: {str(d.get('reason', ''))[:80]}]"
                return chosen
        except Exception as e:
            self._fail(e)
        return cands[0]


# ======================================================================================
# 4. THE AGENT LOOP
# ======================================================================================

PHASES = {"read_policy": "GATHER", "read_employees": "GATHER", "read_rows": "GATHER", "list_receipts": "GATHER",
          "analyze": "PLAN", "approval": "ASK HUMAN", "clarify": "ASK HUMAN", "submit": "EXECUTE",
          "cc_lookup": "ADAPT", "verify": "VERIFY", "write_report": "DELIVER", "check_report": "VERIFY"}


class Agent:
    def __init__(self, goal: str, workspace: Path, portal_url: str, planner: RulePlanner,
                 approver: Approver, backoff: float = 0.2, verbose: bool = True):
        self.goal = goal
        self.workspace = workspace.resolve()
        self.planner = planner
        self.approver = approver
        self.tools = ToolBox(self.workspace, portal_url, approver)
        self.guard = Guard(self.workspace)
        self.backoff = backoff
        self.verbose = verbose
        self.trace: list = []
        self.step = 0

    def say(self, text: str) -> None:
        if self.verbose:
            print(text)

    @staticmethod
    def _describe(a: Action) -> str:
        if a.tool == "http":
            tag = f" [{a.meta['expense_id']}]" if "expense_id" in a.meta else ""
            return f"http {a.args.get('method')} {a.args.get('path')}{tag}"
        if a.tool in ("read_file", "list_dir", "write_file"):
            return f"{a.tool} {a.args.get('path')}"
        if a.tool == "ask_human":
            return f"ask_human ({a.args.get('kind')}" + (f", {a.meta['expense_id']})" if "expense_id" in a.meta else ")")
        return a.tool

    def execute(self, action: Action, state: State) -> tuple:
        reason = self.guard.check(action, state)
        if reason:
            state.events.append(f"BLOCKED by guard: {self._describe(action)} - {reason}")
            return Result(False, error=f"blocked by guard: {reason}", blocked=True), 0
        retries = 0
        while True:
            res = self.tools.run(action, state)
            if res.transient and retries < MAX_TRANSIENT_RETRIES:
                retries += 1
                delay = self.backoff * (2 ** (retries - 1))
                msg = (f"transient failure on {self._describe(action)}: {res.error}; "
                       f"retry {retries}/{MAX_TRANSIENT_RETRIES} after {delay:.2f}s")
                state.events.append(msg)
                self.say(f"      ~ {msg}")
                time.sleep(delay)
                continue
            return res, retries

    def _log(self, action: Action, res: Result, retries: int, phase: str) -> None:
        self.step += 1
        args = {k: (v[:80] + "..." if isinstance(v, str) and len(v) > 80 else v) for k, v in action.args.items()}
        self.trace.append({"step": self.step, "phase": phase, "tool": action.tool, "args": args,
                           "rationale": action.rationale, "ok": res.ok, "blocked": res.blocked,
                           "status": res.status, "retries": retries, "result": res.brief(200)})
        self.say(f"[{self.step:02d}] {phase:<9} {self._describe(action)}")
        self.say(f"      why: {action.rationale}")
        self.say(f"      ->   {res.brief()}")

    def _understand(self, s: State) -> Optional[tuple]:
        goal = self.goal
        for _ in range(3):
            try:
                s.intent = self.planner.understand(goal)
                self.say(f"[00] UNDERSTAND  intent={json.dumps(s.intent)}")
                return None
            except Unsupported as e:
                return "unsupported", str(e)
            except NeedInfo as e:
                act = Action("ask_human", {"question": e.question, "kind": "clarify"},
                             "The request is ambiguous; I need the missing detail before acting", {"kind": "clarify"})
                res, retries = self.execute(act, s)
                self._log(act, res, retries, "ASK HUMAN")
                ans = ((res.data or {}).get("answer", "") if res.ok else "").strip()
                if not ans or ans.lower() in ("no", "n"):
                    return "needs_input", f"I could not proceed without this information: {e.question}"
                goal = f"{goal} {ans}"
        return "needs_input", "Still ambiguous after clarification attempts."

    def run(self) -> dict:
        s = State(goal=self.goal)
        self.say(f"GOAL: {self.goal}\n")
        early = self._understand(s)
        if early:
            return self._finish(s, early[0], early[1])
        seen: dict = {}
        for _ in range(MAX_STEPS):
            cands = self.planner.candidates(s)
            action = self.planner.choose(s, cands)
            if action.tool == "finish":
                return self._finish(s, action.args["status"], action.args["summary"])
            sig = json.dumps([action.tool, action.args], sort_keys=True, default=str)
            seen[sig] = seen.get(sig, 0) + 1
            if seen[sig] > 6:
                s.fatal = f"Stuck: repeated the same action {seen[sig]} times ({self._describe(action)})"
                continue
            res, retries = self.execute(action, s)
            self._log(action, res, retries, PHASES.get(action.meta.get("kind", ""), "EXECUTE"))
            self.planner.observe(s, action, res)
        return self._finish(s, "aborted", f"Step budget of {MAX_STEPS} exhausted")

    def _finish(self, s: State, status: str, summary: str) -> dict:
        result = {"status": status, "summary": summary, "steps": self.step, "events": s.events,
                  "records": {i: {k: r.get(k) for k in ("status", "verdict", "cost_center", "attempts",
                                                          "verified", "portal_ref", "note", "reasons")}
                              for i, r in s.expenses.items()},
                  "report_path": s.facts.get("report_written"),
                  "human_interactions": len(self.approver.log)}
        try:
            (self.workspace / "reports").mkdir(exist_ok=True)
            (self.workspace / "reports" / "run_trace.json").write_text(
                json.dumps({"goal": self.goal, "intent": s.intent, "result": result, "trace": self.trace},
                           indent=2, default=str), encoding="utf-8")
        except OSError:
            pass
        self.say(f"\nFINAL STATUS: {status.upper()}\n{summary}")
        return result


# ======================================================================================
# 5. RUNNERS: demo CLI and self-test
# ======================================================================================

def run_scenario(goal: str, approver: Approver, inject_faults: bool = True, planner: Optional[RulePlanner] = None,
                 verbose: bool = True, backoff: float = 0.2, workspace: Optional[Path] = None) -> tuple:
    ws = workspace or Path(tempfile.mkdtemp(prefix="operator_ws_"))
    seed_workspace(ws)
    portal = FinancePortal(inject_faults=inject_faults)
    portal.start()
    try:
        agent = Agent(goal, ws, portal.base_url, planner or RulePlanner(), approver, backoff, verbose)
        if verbose:
            print(f"Workspace: {ws}\nFinance portal: {portal.base_url} (fault injection {'ON' if inject_faults else 'OFF'})\n")
        result = agent.run()
    finally:
        portal.stop()
    return result, portal, ws


def selftest() -> int:
    failures = []

    def check(cond: bool, msg: str) -> None:
        print(("  PASS  " if cond else "  FAIL  ") + msg)
        if not cond:
            failures.append(msg)

    print("Scenario A: approver says yes, faults injected (503, stale cost center, silent write loss)")
    res, portal, ws = run_scenario(DEFAULT_GOAL, Approver("yes", verbose=False), verbose=False, backoff=0.0)
    rec = res["records"]
    check(res["status"] == "completed", f"status completed (got {res['status']})")
    check(set(portal.expenses) == {"E001", "E002", "E003", "E005", "E009"},
          f"portal holds exactly the 5 expected claims (got {sorted(portal.expenses)})")
    check(all(rec[i]["status"] == "submitted" and rec[i]["verified"] for i in portal.expenses),
          "every filed claim is independently verified")
    check(rec["E004"]["status"] == "flagged" and rec["E007"]["status"] == "flagged", "E004 and E007 flagged as violations")
    check(rec["E006"]["verdict"] == "duplicate" and rec["E008"]["verdict"] == "out_of_period",
          "E006 duplicate and E008 out-of-period skipped")
    check(rec["E005"]["cost_center"] == "MKT-200", "E005 stale cost center recovered (MKT-100 -> MKT-200)")
    check(rec["E009"]["attempts"] >= 2, "E009 silent write loss caught by verification and re-filed")
    check(any("retry" in e for e in res["events"]), "transient 503 was retried automatically")
    check(abs(sum(e["amount"] for e in portal.expenses.values()) - 1100.5) < 1e-6, "portal total is $1,100.50")
    report = (ws / res["report_path"]).read_text(encoding="utf-8")
    check("Recovery and safety events" in report and "E003" in report, "summary deliverable written with evidence")
    check((ws / "reports" / "run_trace.json").exists(), "JSON trace written")

    print("\nScenario B: approver says no")
    res, portal, ws = run_scenario(DEFAULT_GOAL, Approver("no", verbose=False), verbose=False, backoff=0.0)
    check(res["records"]["E003"]["status"] == "held" and "E003" not in portal.expenses,
          "denied approval -> E003 held and NOT filed")
    check(res["status"] == "completed" and set(portal.expenses) == {"E001", "E002", "E005", "E009"},
          "everything else still completed")

    print("\nScenario C: no faults injected")
    res, portal, ws = run_scenario(DEFAULT_GOAL, Approver("yes", verbose=False), inject_faults=False,
                                   verbose=False, backoff=0.0)
    check(res["status"] == "completed" and len(portal.expenses) == 5, "clean run completes")

    print("\nScenario D: ambiguous goal triggers a clarifying question")
    res, portal, ws = run_scenario("Please process the expense reports and file them",
                                   Approver("yes", script=["October 2026"], verbose=False),
                                   verbose=False, backoff=0.0)
    check(res["status"] == "completed" and len(portal.expenses) == 5 and res["human_interactions"] >= 1,
          "asked human for the period, then completed")
    res, portal, ws = run_scenario("Please process the expense reports", Approver("yes", verbose=False),
                                   verbose=False, backoff=0.0)
    check(res["status"] == "needs_input" and not portal.expenses, "unanswerable ambiguity -> stops safely, files nothing")

    print("\nScenario E: review-only request and unsupported request")
    res, portal, ws = run_scenario("Review the October 2026 expense reports, do not submit anything",
                                   Approver("yes", verbose=False), verbose=False, backoff=0.0)
    check(res["status"] == "completed" and not portal.expenses, "review-only run files nothing")
    res, portal, ws = run_scenario("Book me a flight to Paris", Approver("yes", verbose=False),
                                   verbose=False, backoff=0.0)
    check(res["status"] == "unsupported", "out-of-scope request is declined honestly")

    print("\nScenario F: guard blocks unsafe actions regardless of planner")
    ws = Path(tempfile.mkdtemp(prefix="operator_guard_"))
    seed_workspace(ws)
    g = Guard(ws)
    st = State(goal="x", intent={"submit": True}, facts={"policy": {"manager_approval_over": 500.0}})
    big = Action("http", {"method": "POST", "path": "/api/expenses", "body": {"id": "X1", "amount": 640}})
    check(g.check(big, st) is not None, "filing > threshold without approval is blocked")
    st.approvals["X1"] = "approved"
    check(g.check(big, st) is None, "same filing allowed once approval is recorded")
    check(g.check(Action("read_file", {"path": "../../etc/passwd"}), st) is not None, "path traversal blocked")
    check(g.check(Action("write_file", {"path": "policy.md", "content": "x"}), st) is not None,
          "writing outside outbox/reports blocked")
    check(g.check(Action("http", {"method": "DELETE", "path": "/api/expenses"}), st) is not None, "DELETE blocked")
    check(g.check(Action("shell", {"cmd": "rm -rf /"}), st) is not None, "unknown tools blocked")

    print("\nScenario G: LLM planner path (model call stubbed) still obeys the loop and guard")

    class FakeLLM(LLMPlanner):
        def __init__(self) -> None:
            self.key, self.model, self.enabled, self.failures = "fake", "fake", True, 0
            self.calls = 0

        def _call(self, purpose: str, system: str, user: str) -> str:
            self.calls += 1
            if purpose == "understand":
                return '{"intent":"expense_processing","month":10,"year":2026,"submit":true}'
            return 'Sure: {"choice": 1, "reason": "stub picks second option"}'

    fake = FakeLLM()
    res, portal, ws = run_scenario(DEFAULT_GOAL, Approver("yes", verbose=False), planner=fake,
                                   verbose=False, backoff=0.0)
    check(res["status"] == "completed" and len(portal.expenses) == 5 and fake.calls > 2,
          f"LLM-guided run completes with identical verified outcome ({fake.calls} model calls)")

    print()
    if failures:
        print(f"SELF-TEST FAILED ({len(failures)} failing checks)")
        return 1
    print("ALL SELF-TESTS PASSED")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Autonomous company operator prototype (expense processing)")
    ap.add_argument("--goal", default=DEFAULT_GOAL, help="the company request in plain language")
    ap.add_argument("--approver", choices=["ask", "yes", "no"], default=None,
                    help="how human approvals are answered (default: ask if interactive, else no)")
    ap.add_argument("--planner", choices=["rule", "llm"], default="rule")
    ap.add_argument("--no-faults", action="store_true", help="disable injected portal failures")
    ap.add_argument("--workspace", default=None, help="directory for the demo company files")
    ap.add_argument("--selftest", action="store_true", help="run built-in scenario tests and exit")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    mode = args.approver or ("ask" if sys.stdin.isatty() else "no")
    planner: RulePlanner = LLMPlanner() if args.planner == "llm" else RulePlanner()
    ws = Path(args.workspace) if args.workspace else None
    result, portal, workspace = run_scenario(args.goal, Approver(mode), not args.no_faults, planner,
                                             True, 0.2, ws)
    print("\n" + "=" * 78)
    print("EVIDENCE")
    print(f"  Portal records ({len(portal.expenses)}):")
    for e in sorted(portal.expenses.values(), key=lambda x: x["id"]):
        print(f"    {e['id']}  {e['employee']:<6} {e['vendor']:<14} {money(e['amount']):>10}  "
              f"{e['cost_center']}  {e['ref']}")
    if result.get("report_path"):
        print(f"  Summary deliverable : {workspace / result['report_path']}")
    print(f"  Full JSON trace     : {workspace / 'reports' / 'run_trace.json'}")
    print(f"  Steps: {result['steps']}   Human interactions: {result['human_interactions']}")
    print("=" * 78)
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
