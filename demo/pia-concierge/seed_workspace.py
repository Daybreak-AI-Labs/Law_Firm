"""Seed the Privacy + Finance workspaces with ~14 months of realistic history.

Run it against the demo home (same MAVERICK_HOME the demo servers use):

    # PowerShell
    $env:MAVERICK_HOME = "$PWD\\.demo-home"; python seed_workspace.py
    # bash
    MAVERICK_HOME=.demo-home python3 seed_workspace.py

Everything goes through the real engines (assessment scoring, DPA clause
review, AI Act classifier, RoPA drafting, DSAR machinery); only timestamps
are backdated afterwards so the workspaces look lived-in: approved records
with review cadences (some overdue), items awaiting review or answers,
stale work, a spread of risk levels, and the four privacy record types.

Idempotent-ish: a marker file stops accidental double-seeding; pass --force
to add another batch anyway.
"""
from __future__ import annotations

import os
import sys
import time

DAY = 86400
NOW = time.time()


def _backdate_assessment(aid: str, *, created_days_ago: float,
                         decided_days_ago: float | None = None,
                         cadence_days: int | None = None) -> None:
    from maverick.assessment import _rewrite_saved

    def _mut(rec: dict) -> None:
        rec["created_at"] = NOW - created_days_ago * DAY
        for f in rec.get("followups") or []:
            f["asked_at"] = rec["created_at"] + 2 * DAY
        if decided_days_ago is not None:
            rec["decided_at"] = NOW - decided_days_ago * DAY
            if cadence_days:
                rec["next_review_at"] = rec["decided_at"] + cadence_days * DAY

    _rewrite_saved(aid, _mut)


def _assessment(type_, subject, answers, *, days_ago, decide=None,
                cadence=365, decided_days_ago=None, followups=None,
                answer_followups=False):
    from maverick import assessment as A
    s = A.AssessmentSession(type=type_, subject=subject)
    qs = {q.id: q for q in A.get_template(type_).questions}
    for qid, (ans, note) in answers.items():
        if qid in qs:
            s.record(qid, ans, note)
    A.save_session(s)
    if followups:
        A.add_followups(s.id, followups, asked_by="privacy-lead")
        if answer_followups:
            rec = A.load_saved(s.id)
            for f in rec.get("followups", []):
                A.answer_followup(s.id, f["id"], "Confirmed — see ticket.",
                                  answered_by="requester")
    if decide:
        A.decide_assessment(s.id, decide, decided_by="privacy-lead",
                            cadence_days=cadence)
    _backdate_assessment(s.id, created_days_ago=days_ago,
                         decided_days_ago=decided_days_ago,
                         cadence_days=cadence if decide == "approved" else None)
    return s.id


GOOD_DPA = """Data Processing Agreement. The processor shall process personal
data only on documented instructions from the controller. Personnel are bound
by confidentiality. The processor implements the technical and organisational
measures of Article 32 including encryption at rest. Sub-processors require
prior written authorization. The processor shall assist the controller with
data subject requests and shall notify the controller of any personal data
breach without undue delay. Upon termination the processor shall delete or
return all personal data. The controller has audit and inspection rights.
Transfers rely on Standard Contractual Clauses. Data is retained for 12
months per the retention schedule."""

THIN_MSA = """Master services agreement. Vendor provides marketing analytics
over customer personal data, including transfer to processing centers outside
the EU. Payment terms are net 30. Either party may terminate with notice."""

ONETRUST_CSV = (
    '"Processing Activity Name","Purpose of Processing",'
    '"Personal Data Categories","Categories of Data Subjects",'
    '"Cross-Border Transfers","Retention Period"\n'
    '"CRM marketing","Campaign targeting and analytics",'
    '"Contact details, usage history","Customers, prospects",'
    '"SCCs to US vendors","24 months"\n'
    '"Payroll","Salary processing and statutory filings",'
    '"Bank details, salary, tax ids","Employees","None","7 years"\n'
    '"Recruiting pipeline","Candidate evaluation",'
    '"CVs, interview notes","Job applicants","None","12 months"\n')


# ---- Volume generation: an enterprise year, not a sketch -------------------
# Weighted, seeded-random history so the workspaces read like a program that
# runs hundreds of assessments a year (MVW-scale is 1500+; we seed a few
# hundred so pages stay snappy while the numbers feel real).

VENDORS = [
    "Acme Corp", "Globex GmbH", "Initech LLC", "Umbrella Analytics",
    "Stark Logistics", "Wayne Facilities", "Hooli Cloud", "Vandelay Imports",
    "Pied Piper CDN", "Aviato Travel", "Soylent Catering", "Wonka Rewards",
    "Cyberdyne Robotics", "Tyrell Staffing", "Oceanic Bookings",
    "Dunder Mifflin Print", "Prestige Worldwide Events", "Gringotts Payments",
    "Nakatomi Security", "Duff Beverages", "Sirius Cybernetics",
    "MomCorp Fulfillment", "Massive Dynamic Research", "Veridian Dynamics",
    "Bluth Construction", "Sterling Cooper Media", "Pearson Legal Ops",
    "Kruger Industrial", "Vehement Capital", "Octan Energy",
]
SYSTEMS = [
    "guest CRM", "loyalty points engine", "reservation platform",
    "call-center suite", "marketing automation", "owner portal",
    "housekeeping scheduler", "mobile check-in app", "payment gateway",
    "survey platform", "identity provider", "data lakehouse",
    "e-signature service", "travel-booking API", "chat widget",
    "HRIS", "expense tool", "revenue dashboard", "email relay",
    "document vault", "contact center analytics", "kiosk fleet",
]
PROJECTS = [
    "rollout", "renewal", "migration", "pilot", "vendor swap",
    "regional expansion", "upgrade", "consolidation", "integration",
    "annual review",
]
FOLLOWUP_POOL = [
    "Which sub-processors receive personal data under this contract?",
    "Is production data used in any lower environment?",
    "What is the documented retention period and who enforces it?",
    "Has the vendor provided a current SOC 2 / ISO 27001 report?",
    "Where is the data hosted and under what transfer mechanism?",
    "Who holds admin access and how is it reviewed?",
    "Is there a tested deletion path at contract end?",
    "Has a DPIA been filed with the works council where required?",
]

VOLUME_MIX = [  # (type, count, subject builder)
    ("pia", 120, lambda rnd: f"{rnd.choice(SYSTEMS)} {rnd.choice(PROJECTS)}"),
    ("vendor_risk", 100, lambda rnd: rnd.choice(VENDORS)),
    ("aira", 45, lambda rnd: f"{rnd.choice(SYSTEMS)} AI assist"),
    ("tia", 25, lambda rnd: f"EU→{rnd.choice(['US', 'IN', 'PH', 'UK', 'SG'])} {rnd.choice(SYSTEMS)} flow"),
    ("hipaa", 30, lambda rnd: f"benefits {rnd.choice(PROJECTS)}"),
    ("soc2", 25, lambda rnd: f"{rnd.choice(SYSTEMS)} controls"),
    ("pci_dss", 20, lambda rnd: f"{rnd.choice(SYSTEMS)} payments"),
    ("sox_control", 30, lambda rnd: f"{rnd.choice(['revenue', 'AP', 'AR', 'inventory', 'payroll'])} controls {rnd.choice(['Q1', 'Q2', 'Q3', 'Q4'])}"),
    ("itgc", 15, lambda rnd: f"{rnd.choice(SYSTEMS)} access review"),
    ("fraud_risk", 15, lambda rnd: f"{rnd.choice(['AP', 'procurement', 'T&E', 'refunds'])} process"),
    ("close_readiness", 12, lambda rnd: f"{rnd.choice(['Jan', 'Mar', 'Jun', 'Sep', 'Dec'])} close"),
    ("credit_risk", 8, lambda rnd: f"{rnd.choice(VENDORS)} credit line"),
]


def _volume_answers(rnd, template) -> dict:
    """Weighted answers: most programs are mostly clean, some messy."""
    profile = rnd.choices(("clean", "minor", "risky", "unverified"),
                          weights=(45, 30, 20, 5))[0]
    out = {}
    for q in template.questions:
        if rnd.random() > 0.85:  # not every question gets answered
            continue
        safe = "no" if q.risk_answer == "yes" else "yes"
        if profile == "clean":
            ans = safe if rnd.random() > 0.04 else q.risk_answer
        elif profile == "minor":
            risky_ok = q.severity != "high" and rnd.random() < 0.45
            ans = q.risk_answer if risky_ok else safe
        elif profile == "risky":
            ans = q.risk_answer if rnd.random() < 0.4 else safe
        else:
            ans = "unknown" if rnd.random() < 0.5 else safe
        out[q.id] = (ans, "")
    return out


def _generate_volume(rnd) -> int:
    from maverick import assessment as A
    n = 0
    for type_, count, subject_of in VOLUME_MIX:
        tpl = A.get_template(type_)
        for _ in range(count):
            # Denser recently: the program grew over the year.
            days_ago = round(420 * rnd.betavariate(1.1, 1.9), 1)
            decide = None
            cadence = rnd.choices((90, 180, 365), weights=(15, 25, 60))[0]
            decided_days_ago = None
            followups = None
            answer_followups = False
            roll = rnd.random()
            if days_ago > 30:
                if roll < 0.82:
                    decide = "approved"
                    decided_days_ago = max(0.5, days_ago - rnd.uniform(1, 10))
                elif roll < 0.88:
                    decide = "rejected"
                    decided_days_ago = max(0.5, days_ago - rnd.uniform(1, 10))
                elif roll < 0.94:
                    followups = rnd.sample(FOLLOWUP_POOL, rnd.randint(1, 2))
                    answer_followups = rnd.random() < 0.5
            else:
                if roll < 0.30:
                    decide = "approved"
                    decided_days_ago = max(0.2, days_ago - rnd.uniform(0.5, 5))
                elif roll < 0.55:
                    followups = rnd.sample(FOLLOWUP_POOL, rnd.randint(1, 2))
                    answer_followups = rnd.random() < 0.3
            _assessment(type_, subject_of(rnd), _volume_answers(rnd, tpl),
                        days_ago=days_ago, decide=decide, cadence=cadence,
                        decided_days_ago=decided_days_ago,
                        followups=followups,
                        answer_followups=answer_followups)
            n += 1
    return n


def _seed_operating_record(rnd) -> None:
    """Goals + run ledger behind the Overview / Spend / Workforce dashboards.

    ~50 goals over the last ~120 days across eight departments — most
    delivered with a costed episode (the same ledger Spend and Savings read),
    a few live, blocked, or queued. Denser recently so the weekly activity
    chart shows the program ramping. Rows are written through the real
    WorldModel APIs, then timestamps are backdated by direct UPDATE
    (the same trick the assessment seeding uses)."""
    from maverick.world_model import WorldModel

    w = WorldModel()
    if w.conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0]:
        return   # a lived-in world already exists; don't stack a second one
    vendors = ["Initech", "Globex", "Acme", "Vandelay", "Northwind",
               "Contoso", "Umbrella", "Aperture"]
    work = [
        ("legal_privacy", "Vendor DPA review — {v}"),
        ("legal_privacy", "Privacy addendum redline — {v}"),
        ("legal_gdpr_dpo", "DSAR fulfilment — {v} data subject"),
        ("itgrc_dpia", "DPIA refresh — {v} integration"),
        ("itgrc_access_review", "Quarterly access review — {v} tenant"),
        ("finance_anomaly", "Spend anomaly sweep — {v} invoices"),
        ("finance_anomaly", "Month-end accrual check — {v}"),
        ("sec_access_review", "Privileged access recert — {v} admins"),
        ("sec_access_review", "Threat-surface review — {v} SSO"),
        ("hr_accommodation_interactive", "Policy acknowledgement chase — {v} team"),
        ("ops_asset_mgmt", "Asset register reconcile — {v} fleet"),
        ("data_ab_experimentation", "Consent-rate analysis — {v} rollout"),
        ("cx_accessib_content", "Notice readability pass — {v} portal"),
        ("gtm_marketing_privacy", "Campaign consent audit — {v} list"),
    ]

    def _episode(gid, started, *, outcome, minutes, cost):
        eid = w.start_episode(gid)
        in_tok = int(cost * rnd.uniform(28_000, 45_000))
        w.end_episode(
            eid, "seeded demo run", outcome, cost_dollars=round(cost, 4),
            input_tokens=in_tok,
            output_tokens=int(in_tok * rnd.uniform(0.10, 0.22)),
            tool_calls=rnd.randint(4, 38))
        ended = started + minutes * 60
        w.conn.execute(
            "UPDATE episodes SET started_at = ?, ended_at = ? WHERE id = ?",
            (started, ended, eid))
        return ended

    # Terminal states only (done / blocked): the platform's orphan-reclaim
    # sweeps any stale 'active'/'pending' row to blocked on the next boot
    # (world_model.reclaim_orphan_goals), so seeded "live" work would read
    # as crashes. Live goals appear the moment real demo work runs.
    # Oldest first, so goal ids follow time and "Recent goals" reads sanely.
    specs = sorted((round(120 * rnd.betavariate(1.1, 2.1), 2)
                    for _ in range(52)), reverse=True)
    def _work_trail(gid, agent, title, started, minutes):
        """A believable run trail behind the goal, backdated to the run
        window — the chat view shows real steps, never "0 steps"."""
        steps = [
            ("status", f"Scoped the work: {title}."),
            ("tool", "Pulled the relevant records from the connected "
                     "systems and the vendor's prior history."),
            ("status", "Drafted the deliverable and ran the verification "
                       "pass against the department's playbook."),
        ]
        span = max(minutes * 60.0, 240.0)
        for i, (kind, content) in enumerate(steps):
            eid = w.append_event(gid, agent, kind, content)
            w.conn.execute("UPDATE goal_events SET ts = ? WHERE id = ?",
                           (started + span * (i + 1) / (len(steps) + 1), eid))

    counts = {"done": 0, "blocked": 0}
    signed = 0
    for days_ago in specs:
        domain, tpl = rnd.choice(work)
        title = tpl.format(v=rnd.choice(vendors))
        gid = w.create_goal(title, domain=domain)
        created = NOW - days_ago * DAY
        status = "blocked" if rnd.random() < 0.09 else "done"
        counts[status] += 1
        if status == "done":
            started = created + rnd.uniform(0.2, 5) * 3600
            # ~1 in 8 deliveries took a failed first run — feeds an honest
            # outcome mix instead of a suspicious 100% success rate.
            if rnd.random() < 0.12:
                started = _episode(gid, started, outcome="error",
                                   minutes=rnd.uniform(2, 9),
                                   cost=rnd.uniform(0.05, 0.5)) + 900
            minutes = rnd.uniform(4, 45)
            updated = _episode(gid, started, outcome="success",
                               minutes=minutes, cost=rnd.uniform(0.15, 2.6))
            _work_trail(gid, domain, title, started, minutes)
            result = (f"Completed: {title}. The deliverable is filed on the "
                      "record; sources and the verification pass are in the "
                      "step trail above.")
            # Result FIRST, then the sign-off: record_signoff requires the
            # goal to be done, and any later result write revokes the row.
            w.set_goal_status(gid, "done", result=result)
            # Most delivered work was reviewed and certified — the sign-off
            # queue shows a handful of items awaiting a human, not a backlog.
            if rnd.random() < 0.92:
                w.record_signoff(gid, "approved", decided_by="C. Day")
                w.conn.execute(
                    "UPDATE signoffs SET created_at = ? WHERE goal_id = ?",
                    (updated + rnd.uniform(1, 30) * 3600, gid))
                signed += 1
        else:
            w.set_goal_status(
                gid, "blocked",
                result=f"Blocked: {title} is waiting on an answer from the "
                       "business owner before the agent can continue.")
            updated = created + rnd.uniform(1, 20) * 3600
        w.conn.execute(
            "UPDATE goals SET created_at = ?, updated_at = ? WHERE id = ?",
            (created, updated, gid))
    w.conn.commit()
    spend = w.total_spend()
    print(f"Seeded the Operating Record: 52 goals over ~4 months "
          f"({counts['done']} delivered, {signed} signed off, "
          f"{counts['blocked']} blocked), "
          f"{spend['runs']} costed runs, ${spend['dollars']:.2f} ledger spend.")


def _seed_external_agent(rnd) -> None:
    """The bring-your-own-agent beat: a Salesforce Agentforce agent that
    Lightwork does not run, only governs.

    Enrolls ``sf-quotebot`` across the trust plane + enrollment sidecar,
    ingests two weeks of COMPLETED quoting runs through the real gateway
    (``record_run`` lands each one as a terminal done/blocked goal owned by
    ``agent:sf-quotebot`` — orphan-reclaim-proof, same rule as the Operating
    Record seed), backdates them with the same direct-UPDATE trick, then
    makes ONE real ``screen`` call for a high-risk action so a genuine
    approval row parks in the dashboard queue, labeled with external-agents
    provenance."""
    # The plane must be on for the gateway (and the dashboard roster). The
    # demo launchers set this too; the setdefault keeps a standalone
    # `python3 seed_workspace.py` working.
    os.environ.setdefault("MAVERICK_EXTERNAL_AGENTS", "1")
    from maverick import external_agents as xa

    if xa._load_sidecar().get("sf-quotebot"):
        return   # already enrolled; never restack runs or park a second approval

    xa.enroll(
        "sf-quotebot", "agentforce",
        description="Agentforce quoting agent (Sales Cloud)",
        owner="jordan@company.com", department="sales",
        max_risk="high",
        allow_tools=["crm_update:low", "send_contract:high", "research"],
        max_dollars=250.0, budget_period="monthly",
        enrolled_by="C. Day")

    accounts = ("Contoso", "Globex", "Initech", "Northwind", "Vandelay",
                "Aperture")
    # Oldest first (ids follow time), all safely in the past 2 weeks.
    ages = sorted((rnd.uniform(3.2, 13.5) for _ in accounts), reverse=True)
    spent = 0.0
    backdates: list[tuple[int, float, float, float]] = []
    for i, (account, days_ago) in enumerate(zip(accounts, ages, strict=True)):
        failed = i == 3   # one honest failure in the fortnight
        cost = round(rnd.uniform(1.0, 4.0), 2)
        in_tok = int(cost * rnd.uniform(28_000, 45_000))
        duration = rnd.uniform(240, 1500)
        res = xa.record_run("sf-quotebot", {
            "title": f"Quote for {account} renewal",
            "outcome": "failure" if failed else "success",
            "summary": (f"CPQ validation rejected the discount tier; "
                        f"escalated to the {account} account owner."
                        if failed else
                        f"Priced the {account} renewal in CPQ and filed the "
                        f"quote on the opportunity."),
            "steps": [
                f"Pulled the {account} opportunity and current contract "
                "from Sales Cloud.",
                "Priced the renewal against the CPQ rate card and the "
                "discount policy.",
                ("Discount tier failed CPQ validation; parked the quote "
                 "for the account owner."
                 if failed else
                 "Attached the quote PDF to the opportunity and notified "
                 "the account owner."),
            ],
            "cost_dollars": cost,
            "input_tokens": in_tok,
            "output_tokens": int(in_tok * rnd.uniform(0.10, 0.22)),
            "tool_calls": rnd.randint(4, 18),
            "duration_seconds": round(duration, 1),
            "idempotency_key": f"seed-quote-{account.lower()}",
        })
        spent += cost
        created = NOW - days_ago * DAY
        started = created + rnd.uniform(300, 3600)
        backdates.append((res["goal_id"], created, started,
                          started + duration))

    # Backdate in ONE batch after all the ingests (the same direct-UPDATE
    # trick _seed_operating_record uses). Batched deliberately: each
    # record_run opens its own world connection, so an uncommitted UPDATE
    # held across ingests would deadlock SQLite's write lock.
    from maverick.world_model import WorldModel
    w = WorldModel()
    for gid, created, started, ended in backdates:
        w.conn.execute(
            "UPDATE goals SET created_at = ?, updated_at = ? WHERE id = ?",
            (created, ended, gid))
        w.conn.execute(
            "UPDATE episodes SET started_at = ?, ended_at = ? "
            "WHERE goal_id = ?", (started, ended, gid))
        events = [r[0] for r in w.conn.execute(
            "SELECT id FROM goal_events WHERE goal_id = ? ORDER BY id",
            (gid,)).fetchall()]
        for j, eid in enumerate(events):
            w.conn.execute(
                "UPDATE goal_events SET ts = ? WHERE id = ?",
                (started + (ended - started) * (j + 1) / (len(events) + 1),
                 eid))
    w.conn.commit()

    # The governance seam, for real: the agent asks BEFORE sending a
    # contract; high risk is at the approval floor, so the action parks in
    # the queue for a human ("external agent · BYOA gateway" in /approvals).
    verdict = xa.screen(
        "sf-quotebot", "send_contract", risk="high",
        detail="Issue the Northwind renewal contract for e-signature")
    print(f"Seeded the BYOA gateway: sf-quotebot (Salesforce Agentforce), "
          f"{len(accounts)} governed runs over 2 weeks "
          f"(${spent:.2f} reported spend), 1 approval parked in the queue "
          f"(#{verdict.get('approval_id')}).")


def _generate_records_volume(rnd) -> None:
    from maverick import privacy_ops
    # DPA reviews for a slice of the vendor book (varied completeness).
    for vendor in rnd.sample(VENDORS, 24):
        text = GOOD_DPA
        r = rnd.random()
        if r < 0.25:
            text = THIN_MSA.replace("Vendor", vendor)
        elif r < 0.45:
            text = GOOD_DPA.replace("Standard Contractual Clauses",
                                    "a mechanism to be agreed")
        privacy_ops.review_dpa(vendor, text,
                               document_name=f"{vendor.split()[0].lower()}-dpa.pdf")
    # AI registry: one entry per AI-ish system.
    purposes = [
        ("drafts replies to guest messages; agent approves", "support"),
        ("summarizes call transcripts for QA", "contact-center"),
        ("forecasts occupancy for pricing analysts", "revenue"),
        ("ranks job candidates for hiring", "talent-ops"),
        ("scores credit applications for owners", "finance"),
        ("generates marketing copy variants", "marketing"),
        ("routes maintenance tickets by urgency", "facilities"),
        ("detects anomalous payments for review", "finance"),
    ]
    for i, (purpose, owner) in enumerate(rnd.sample(purposes, 8)):
        privacy_ops.register_ai_system(
            f"{rnd.choice(SYSTEMS)} model {i + 1}", purpose,
            provider=rnd.choice(("internal", "Anthropic", "vendor")),
            owner=owner)
    # RoPA: manual entries beyond the imports/drafts.
    for activity, purpose in [
        ("Guest stay processing", "Reservations, billing, and stay services"),
        ("Owner communications", "Statements, meeting notices, ballots"),
        ("CCTV on premises", "Safety and security of guests and staff"),
        ("Marketing preferences", "Consent-based offers and newsletters"),
        ("Workforce scheduling", "Shift planning and time tracking"),
        ("Loyalty program", "Points accrual and redemption"),
        ("Incident management", "Security and privacy incident handling"),
    ]:
        privacy_ops.upsert_ropa({
            "activity": activity, "purpose": purpose,
            "controller": "Company Ltd",
            "data_categories": "Personal data (see activity record)",
            "data_subjects": "Guests, owners, employees",
            "recipients": "Processors under Art. 28 DPAs",
            "transfers": rnd.choice(("None", "SCCs to US vendors",
                                     "Adequacy (EU/UK)")),
            "retention": rnd.choice(("24 months", "36 months", "7 years")),
            "security_measures": "Encryption in transit and at rest",
        })
    # DSARs: a realistic month's queue + history.
    first = ["jordan", "sam", "alex", "pat", "casey", "morgan", "riley",
             "drew", "jamie", "quinn", "avery", "reese", "kai", "rowan"]
    last = ["ellis", "rivera", "chen", "kumar", "ortiz", "novak", "silva",
            "haas", "okafor", "lindqvist"]
    for i in range(26):
        subject = f"{rnd.choice(first)}.{rnd.choice(last)}{i}@example.com"
        kind = rnd.choices(("access", "erasure", "portability"),
                           weights=(60, 25, 15))[0]
        req = privacy_ops.open_dsar(subject, kind,
                                    channel=rnd.choice(("email", "web",
                                                        "slack")))
        age = rnd.uniform(0, 120)
        privacy_ops._DSAR.update(req["id"], lambda rec, a=age: rec.update(
            created_at=NOW - a * DAY, due_at=NOW - a * DAY + 30 * DAY))
        if age > 25 and rnd.random() < 0.85:
            privacy_ops.fulfill_dsar(req["id"])
            if kind != "erasure" and rnd.random() < 0.9:
                privacy_ops.close_dsar(req["id"], closed_by="privacy-lead")
        # else: still open; anything older than 30d reads as overdue.


def seed() -> None:
    from maverick import privacy_ops

    # ---- Privacy assessments: a spread of ages, statuses, risk levels ----
    _assessment("pia", "Acme CRM rollout",
                {"pia_transfers": ("yes", "us-east-1; SCCs pending legal"),
                 "pia_retention": ("no", ""),
                 "pia_security": ("yes", "AES-256 at rest, TLS 1.3")},
                days_ago=2)
    _assessment("pia", "Marketing email platform",
                {"pia_necessity": ("yes", ""), "pia_lawful_basis": ("yes", ""),
                 "pia_transparency": ("no", "privacy notice not yet updated"),
                 "pia_retention": ("yes", ""), "pia_security": ("yes", "")},
                days_ago=12)  # stale: sat >7d unreviewed
    _assessment("pia", "HR onboarding portal",
                {"pia_special_category": ("yes", "health data for benefits"),
                 "pia_security": ("unknown", "")},
                days_ago=6,
                followups=["Which benefits provider receives health data?",
                           "Is the health data segregated from the HRIS?"])
    _assessment("pia", "Customer support ticketing",
                {"pia_necessity": ("yes", ""), "pia_lawful_basis": ("yes", ""),
                 "pia_transparency": ("yes", ""), "pia_retention": ("yes", ""),
                 "pia_security": ("yes", "")},
                days_ago=400, decide="approved", cadence=365,
                decided_days_ago=395)  # ~1 month overdue for re-review
    _assessment("pia", "Data warehouse consolidation",
                {"pia_transfers": ("yes", "EU-hosted, adequacy"),
                 "pia_retention": ("yes", ""), "pia_security": ("yes", "")},
                days_ago=200, decide="approved", cadence=365,
                decided_days_ago=190)  # due in ~5.5 months
    _assessment("aira", "Support triage copilot",
                {"aira_purpose": ("yes", "conversational assistant that "
                                         "drafts replies to tickets"),
                 "aira_prohibited": ("no", ""), "aira_high_risk": ("no", ""),
                 "aira_transparency": ("yes", ""),
                 "aira_oversight": ("yes", "agent approves every reply")},
                days_ago=90, decide="approved", cadence=180,
                decided_days_ago=85)
    _assessment("aira", "CV screening pilot",
                {"aira_purpose": ("yes", "ranks job candidates for hiring"),
                 "aira_high_risk": ("yes", "employment - Annex III"),
                 "aira_bias": ("unknown", ""),
                 "aira_oversight": ("no", "")},
                days_ago=30,
                followups=["Has the vendor shared bias-evaluation results "
                           "across protected groups?"])
    _assessment("vendor_risk", "Globex support tooling",
                {"vr_soc2": ("yes", "SOC 2 Type II current"),
                 "vr_dpa": ("yes", ""), "vr_encryption": ("yes", ""),
                 "vr_breach_history": ("no", "")},
                days_ago=320, decide="approved", cadence=365,
                decided_days_ago=310)  # due in ~2 months
    _assessment("vendor_risk", "Initech analytics",
                {"vr_dpa": ("no", "MSA only, no DPA countersigned"),
                 "vr_soc2": ("no", ""),
                 "vr_breach_history": ("yes", "2024 credential-stuffing "
                                              "incident disclosed")},
                days_ago=45, decide="rejected", cadence=0,
                decided_days_ago=40)
    _assessment("hipaa", "Benefits portal PHI flows",
                {"hipaa_risk_analysis": ("yes", ""),
                 "hipaa_access_control": ("yes", ""),
                 "hipaa_encryption": ("yes", ""),
                 "hipaa_baa": ("yes", "BAA on file with carrier"),
                 "hipaa_training": ("yes", "")},
                days_ago=150, decide="approved", cadence=365,
                decided_days_ago=140)
    _assessment("soc2", "Platform trust readiness",
                {"soc2_access": ("yes", ""),
                 "soc2_encryption": ("yes", ""),
                 "soc2_risk": ("unknown", "risk assessment refresh "
                                          "scheduled next quarter")},
                days_ago=4)
    _assessment("pci_dss", "Checkout tokenization review",
                {"pci_segmentation": ("yes", ""),
                 "pci_stored_pan": ("yes", "PANs tokenized, none stored"),
                 "pci_transit": ("yes", ""), "pci_malware": ("yes", "")},
                days_ago=75, decide="approved", cadence=90,
                decided_days_ago=70)  # due in ~20 days

    # ---- Finance assessments: same chassis, its own department ----
    _assessment("sox_control", "Q3 revenue recognition controls",
                {"sox_evidence": ("no", "walkthrough evidence incomplete")},
                days_ago=9)  # stale
    _assessment("itgc", "ERP access management",
                {"itgc_access_least_priv": ("yes", ""),
                 "itgc_access_review": ("yes", ""),
                 "itgc_change_mgmt": ("yes", "")},
                days_ago=270, decide="approved", cadence=365,
                decided_days_ago=260)
    _assessment("fraud_risk", "AP vendor master process",
                {"fraud_vendor_create_approve": ("no", "same analyst creates "
                                                       "and approves")},
                days_ago=20,
                followups=["When will creation/approval be segregated?"])
    _assessment("close_readiness", "June close",
                {"close_bs_recon": ("yes", ""),
                 "close_bank_rec": ("yes", ""),
                 "close_accruals": ("no", "estimate process manual"),
                 "close_flux": ("yes", "")},
                days_ago=16)

    # ---- Privacy records: DPA reviews, AI registry, RoPA, DSARs ----
    privacy_ops.review_dpa("Acme Corp", GOOD_DPA,
                           document_name="acme-dpa-2025.pdf")
    privacy_ops.review_dpa("Initech LLC", THIN_MSA,
                           document_name="initech-msa.docx")
    privacy_ops.review_dpa(
        "Globex GmbH",
        GOOD_DPA.replace("Standard Contractual Clauses", "a mechanism to be "
                                                          "agreed"),
        document_name="globex-dpa-draft.docx")

    privacy_ops.register_ai_system(
        "Support triage copilot", "conversational assistant that drafts "
        "replies to customer tickets", provider="Anthropic", owner="support")
    privacy_ops.register_ai_system(
        "CV screener", "ranks job candidates for hiring",
        provider="internal", owner="talent-ops")
    privacy_ops.register_ai_system(
        "Invoice-coding copilot", "suggests GL codes for incoming invoices; "
        "human approves every posting", provider="internal", owner="finance")
    privacy_ops.register_ai_system(
        "Churn model", "generates renewal-risk scores for account teams",
        provider="internal", owner="revops")

    privacy_ops.import_onetrust_ropa(ONETRUST_CSV)
    privacy_ops.upsert_ropa({
        "activity": "Support ticket processing",
        "purpose": "Customer support and quality assurance",
        "controller": "Company Ltd",
        "data_categories": "Contact details, ticket content",
        "data_subjects": "Customers",
        "recipients": "Ticketing processor under Art. 28 DPA",
        "transfers": "None",
        "retention": "36 months",
        "security_measures": "Encryption in transit and at rest",
    })

    r1 = privacy_ops.open_dsar("jordan.ellis@example.com", "access",
                               channel="email")
    privacy_ops.fulfill_dsar(r1["id"])
    privacy_ops.close_dsar(r1["id"], closed_by="privacy-lead")
    r2 = privacy_ops.open_dsar("sam.rivera@example.com", "erasure",
                               channel="slack")
    privacy_ops.fulfill_dsar(r2["id"])  # awaiting the operator erase step
    privacy_ops.open_dsar("alex.chen@example.com", "access", channel="email")
    r4 = privacy_ops.open_dsar("pat.kumar@example.com", "portability",
                               channel="email")
    # Backdate one open request past its statutory clock (overdue).
    privacy_ops._DSAR.update(r4["id"], lambda rec: rec.update(
        created_at=NOW - 40 * DAY, due_at=NOW - 10 * DAY))

    # Incident register: the Art. 33 clock in three states.
    inc = privacy_ops.open_incident(
        "Misdirected owner statement batch", severity="high",
        categories="contact details, account balances",
        affected_estimate="~120 owners", reported_by="ops")
    privacy_ops._INCIDENT.update(inc["id"], lambda r: r.update(
        created_at=NOW - 8 * 3600,
        notify_deadline_at=NOW - 8 * 3600 + 72 * 3600))
    doc = privacy_ops.open_incident(
        "Lost badge with cached emails", severity="low",
        categories="work emails", affected_estimate="1 employee")
    privacy_ops.decide_incident_notification(
        doc["id"], False, rationale="device encrypted and remotely wiped; "
        "no risk to rights and freedoms", decided_by="dpo")
    told = privacy_ops.open_incident(
        "Processor breach at Initech", severity="high",
        categories="names, emails", affected_estimate="~2,400 customers")
    privacy_ops.decide_incident_notification(
        told["id"], True, rationale="confirmed exfiltration of personal "
        "data; authority and subjects notified", decided_by="dpo")
    privacy_ops.close_incident(told["id"], closed_by="dpo")

    # A decided assessment feeding both registers (provenance on show).
    from maverick.assessment import list_saved
    aira_ids = [s["id"] for s in list_saved() if s["type"] == "aira"]
    if aira_ids:
        privacy_ops.register_ai_system_from_assessment(aira_ids[-1])
    pia_ids = [s["id"] for s in list_saved() if s["type"] == "pia"]
    if pia_ids:
        privacy_ops.draft_ropa_from_assessment(pia_ids[0])

    # ---- Volume: the rest of the year's program -------------------------
    import random
    rnd = random.Random(42)  # reproducible history
    _generate_volume(rnd)
    _generate_records_volume(rnd)
    _seed_operating_record(rnd)
    _seed_external_agent(rnd)

    from maverick.assessment import list_saved
    rows = list_saved()
    from collections import Counter
    by_status = Counter(r["status"] for r in rows)
    print(f"Seeded {len(rows)} assessments over ~14 months "
          f"({by_status['approved']} approved, "
          f"{by_status['pending_review']} awaiting review, "
          f"{by_status['needs_more']} awaiting answers, "
          f"{by_status['rejected']} rejected; "
          f"{sum(1 for r in rows if r['review_due'])} due for re-review), "
          f"plus DPA reviews, the AI registry, the Art. 30 register, and a "
          f"DSAR queue with history.")


def main() -> None:
    home = os.environ.get("MAVERICK_HOME", "")
    if not home:
        print("Set MAVERICK_HOME to the demo home first "
              "(e.g. demo/pia-concierge/.demo-home).")
        raise SystemExit(2)
    from pathlib import Path
    marker = Path(home) / ".workspace-seeded"
    if marker.exists() and "--force" not in sys.argv:
        print(f"Already seeded ({marker}). Pass --force to add another "
              "batch, or delete the demo home for a clean slate.")
        raise SystemExit(0)
    seed()
    marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")


if __name__ == "__main__":
    main()
