"""L3A multi-agent workflow: coordinator, specialists, policy agent and verifier.

Every conclusion is derived from MCP evidence. Rows whose timestamps fall outside the
per-domain window anchored on the authoritative order row, and exact duplicate rows,
are treated as conflicting source data: excluded and reported in ``data_conflicts``.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

COORDINATOR = "coordinator"
ORDER_AGENT = "order-agent"
PAYMENT_AGENT = "payment-agent"
SHIPMENT_AGENT = "shipment-agent"
POLICY_AGENT = "policy-agent"
VERIFIER = "verifier"

# Least-privilege tool ownership: an agent may only call the tools listed here.
TOOL_OWNERSHIP: dict[str, frozenset[str]] = {
    ORDER_AGENT: frozenset({"get_order", "get_order_items", "get_sellers"}),
    PAYMENT_AGENT: frozenset({"get_payment_timeline", "get_refund_timeline"}),
    SHIPMENT_AGENT: frozenset({"get_shipment_summary"}),
    POLICY_AGENT: frozenset({"get_policy"}),
}

MAX_TOOL_ATTEMPTS = 2
MONEY_ISSUES = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
}


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _money(value: Any) -> Decimal:
    return Decimal(str(value))


def _brl(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


@dataclass
class Evidence:
    tool: str
    ref: str
    domain: str
    data: Any


@dataclass
class Finding:
    """A specialist's handoff back to the coordinator (the A2A reply payload)."""

    agent: str
    decision_code: str
    facts: dict[str, Any] = field(default_factory=dict)
    evidence: list[Evidence] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    def refs(self, *tools: str) -> list[str]:
        return [item.ref for item in self.evidence if not tools or item.tool in tools]


@dataclass
class CaseContext:
    case: dict[str, Any]
    gateway: EvidenceGateway
    trace: TraceWriter
    # Per-domain [start, end] windows anchored on the authoritative order row.
    windows: dict[str, tuple[datetime, datetime]] = field(default_factory=dict)

    @property
    def case_id(self) -> str:
        return self.case["case_id"]

    @property
    def order_id(self) -> str:
        return self.case["customer_request"]["claimed_order_id"]

    def set_windows(self, order: dict[str, Any]) -> None:
        purchased = _ts(order.get("order_purchase_timestamp"))
        approved = _ts(order.get("order_approved_at")) or purchased
        estimated = _ts(order.get("order_estimated_delivery_date"))
        delivered = _ts(order.get("order_delivered_customer_date"))
        opened = _ts(self.case["opened_at"])
        if purchased is None or opened is None:
            return
        case_end = max(filter(None, [opened, delivered]))
        self.windows = {
            # A seller's shipping limit must fall before the promised delivery date.
            "items": (purchased, estimated or case_end),
            # Captures and reconciliation checks happen right after approval.
            "payments": (approved, approved + timedelta(days=1)),
            "refunds": (purchased, case_end),
            "shipment": (purchased, case_end),
        }

    def in_window(self, domain: str, value: str | None) -> bool:
        moment = _ts(value)
        if moment is None or domain not in self.windows:
            return False
        start, end = self.windows[domain]
        return start <= moment <= end


class Agent:
    name = "agent"

    def __init__(self, ctx: CaseContext) -> None:
        self.ctx = ctx

    async def fetch(self, tool: str, **arguments: str) -> Evidence | None:
        """Call one owned MCP tool; ``None`` means the gateway had no record."""
        if tool not in TOOL_OWNERSHIP.get(self.name, frozenset()):
            raise PermissionError(f"{self.name} is not allowed to call {tool}")
        last_error: Exception | None = None
        for _ in range(MAX_TOOL_ATTEMPTS):
            try:
                raw = await self.ctx.gateway.call(tool, case_id=self.ctx.case_id, **arguments)
            except RuntimeError as exc:  # tool-level error: deterministic, do not retry
                self.ctx.trace.emit(
                    case_id=self.ctx.case_id,
                    event_type="tool_result_consumed",
                    actor=self.name,
                    tool_name=tool,
                    decision_code="TOOL_NO_RECORD",
                )
                del exc
                return None
            except (TimeoutError, OSError) as exc:
                last_error = exc
                continue
            evidence = Evidence(tool, raw["evidence_ref"], raw["domain"], raw["data"])
            self.ctx.trace.emit(
                case_id=self.ctx.case_id,
                event_type="tool_result_consumed",
                actor=self.name,
                tool_name=tool,
                evidence_refs=[evidence.ref],
                attributes={"domain": evidence.domain, "warnings": len(raw.get("warnings") or [])},
            )
            return evidence
        raise RuntimeError(f"{tool} failed after {MAX_TOOL_ATTEMPTS} attempts: {last_error}")


def _conflict(
    field_name: str, tool: str, kept: list[int], dropped: list[int], code: str
) -> dict[str, Any]:
    sources = [f"{tool}[{index}]" for index in (kept + dropped)][:5]
    selected = sources[0] if kept else "get_order"
    if not kept:
        # The authoritative order row defines the window the dropped rows contradict.
        sources = ["get_order", *sources][:5]
    return {
        "field": field_name,
        "sources": sources,
        "selected_source": selected,
        "resolution_code": code,
    }


def _filter_rows(
    ctx: CaseContext,
    domain: str,
    rows: list[dict[str, Any]],
    time_key: str,
    field_name: str,
    tool: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep in-window, de-duplicated rows; describe everything dropped as data conflicts."""
    kept: list[int] = []
    outside: list[int] = []
    duplicates: list[int] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not ctx.in_window(domain, row.get(time_key)):
            outside.append(index)
            continue
        key = json.dumps(row, sort_keys=True)
        if key in seen:
            duplicates.append(index)
            continue
        seen.add(key)
        kept.append(index)
    conflicts = []
    if outside:
        conflicts.append(_conflict(field_name, tool, kept, outside, "EXCLUDED_OUTSIDE_CASE_WINDOW"))
    if duplicates:
        conflicts.append(
            _conflict(f"{field_name}.duplicate", tool, kept, duplicates, "DEDUPLICATED_RECORD")
        )
    return [rows[index] for index in kept], conflicts


class OrderAgent(Agent):
    name = ORDER_AGENT

    async def load_order(self) -> Finding:
        order = await self.fetch("get_order", order_id=self.ctx.order_id)
        if order is None or not isinstance(order.data, dict):
            return Finding(self.name, "ORDER_NOT_FOUND")
        data = order.data
        if data.get("order_id") != self.ctx.order_id:
            return Finding(self.name, "ORDER_SCOPE_MISMATCH", evidence=[order])
        self.ctx.set_windows(data)
        return Finding(self.name, "ORDER_LOADED", facts={"order": data}, evidence=[order])

    async def investigate(self) -> Finding:
        finding = Finding(self.name, "ITEMS_RESOLVED")
        items = await self.fetch("get_order_items", order_id=self.ctx.order_id)
        rows = items.data if items and isinstance(items.data, list) else []
        valid_items, conflicts = _filter_rows(
            self.ctx,
            "items",
            rows,
            "shipping_limit_date",
            "order_items.shipping_limit_date",
            "get_order_items",
        )
        finding.conflicts.extend(conflicts)
        if items:
            finding.evidence.append(items)
        seller_ids = list(dict.fromkeys(row["seller_id"] for row in valid_items))
        sellers = await self.fetch("get_sellers", order_id=self.ctx.order_id)
        if sellers:
            finding.evidence.append(sellers)
            known = {row.get("seller_id") for row in sellers.data or []}
            finding.facts["unknown_sellers"] = [sid for sid in seller_ids if sid not in known]
        finding.facts.update(
            items=valid_items,
            item_ids=list(dict.fromkeys(row["order_item_id"] for row in valid_items)),
            seller_ids=seller_ids,
            items_total=sum(
                (_money(row["price"]) + _money(row["freight_value"]) for row in valid_items),
                Decimal(0),
            ),
        )
        if not valid_items:
            finding.decision_code = "ITEMS_MISSING"
        return finding


class PaymentAgent(Agent):
    name = PAYMENT_AGENT

    async def investigate(self) -> Finding:
        finding = Finding(self.name, "PAYMENTS_RECONCILED")
        timeline = await self.fetch("get_payment_timeline", order_id=self.ctx.order_id)
        payments = (timeline.data or {}).get("payments", []) if timeline else []
        events = (timeline.data or {}).get("events", []) if timeline else []
        if timeline:
            finding.evidence.append(timeline)
        valid_events, conflicts = _filter_rows(
            self.ctx,
            "payments",
            events,
            "event_at",
            "payment_events.event_at",
            "get_payment_timeline",
        )
        finding.conflicts.extend(conflicts)

        # Base payment rows carry no timestamp: they align 1:1 with "captured" events.
        captures_all = [event for event in events if event.get("event_type") == "captured"]
        payment_refs: list[str] = []
        if len(captures_all) == len(payments):
            for payment, capture in zip(payments, captures_all, strict=True):
                if self.ctx.in_window("payments", capture.get("event_at")):
                    payment_refs.append(f"{self.ctx.order_id}:{payment['payment_sequential']}")
        captures = [event for event in valid_events if event.get("event_type") == "captured"]
        mismatches = [
            event for event in valid_events if event.get("event_type") == "reconciliation_mismatch"
        ]

        refund_events: list[dict[str, Any]] = []
        refunds = await self.fetch("get_refund_timeline", order_id=self.ctx.order_id)
        if refunds:
            all_refunds = (refunds.data or {}).get("events", [])
            refund_events, conflicts = _filter_rows(
                self.ctx,
                "refunds",
                all_refunds,
                "event_at",
                "refund_events.event_at",
                "get_refund_timeline",
            )
            finding.conflicts.extend(conflicts)
            finding.evidence.append(refunds)

        finding.facts.update(
            captures=captures,
            captured_total=sum((_money(e["amount_brl"]) for e in captures), Decimal(0)),
            mismatches=mismatches,
            refund_events=refund_events,
            payment_refs=list(dict.fromkeys(payment_refs)),
        )
        if refund_events:
            finding.decision_code = "REFUND_ACTIVITY_FOUND"
        elif mismatches:
            finding.decision_code = "RECONCILIATION_MISMATCH"
        elif not captures:
            finding.decision_code = "NO_CAPTURE_FOUND"
        return finding


class ShipmentAgent(Agent):
    name = SHIPMENT_AGENT

    async def investigate(self, items: list[dict[str, Any]]) -> Finding:
        finding = Finding(self.name, "DELIVERY_ON_TIME")
        summary = await self.fetch("get_shipment_summary", order_id=self.ctx.order_id)
        if summary is None or not isinstance(summary.data, dict):
            finding.decision_code = "SHIPMENT_NOT_FOUND"
            return finding
        finding.evidence.append(summary)
        data = summary.data
        events = data.get("events") or []
        valid_events, conflicts = _filter_rows(
            self.ctx,
            "shipment",
            events,
            "event_at",
            "shipment_events.event_at",
            "get_shipment_summary",
        )
        finding.conflicts.extend(conflicts)
        delivered = _ts(data.get("delivered_customer_at"))
        estimated = _ts(data.get("estimated_delivery_at"))
        carrier = _ts(data.get("delivered_carrier_at"))
        limits = [_ts(item.get("shipping_limit_date")) for item in items]
        limits = [value for value in limits if value]
        is_late = bool(delivered and estimated and delivered > estimated)
        seller_late = bool(is_late and carrier and limits and carrier > min(limits))
        late_events = [e for e in valid_events if e.get("event_type") == "delivered_late"]
        event_actor = late_events[0].get("actor") if late_events else None
        finding.facts.update(
            is_late=is_late,
            seller_late=seller_late,
            late_event_actor=event_actor,
            unconfirmed_late_event=bool(late_events and not is_late),
        )
        if is_late:
            finding.decision_code = "LATE_SELLER_HANDOFF" if seller_late else "LATE_IN_TRANSIT"
        elif late_events:
            finding.decision_code = "LATE_EVENT_CONTRADICTED"
        return finding


class PolicyAgent(Agent):
    name = POLICY_AGENT

    async def load(self) -> Finding:
        policy = await self.fetch("get_policy", policy_version=self.ctx.case["policy_version"])
        if policy is None or not isinstance(policy.data, dict):
            return Finding(self.name, "POLICY_NOT_FOUND")
        return Finding(
            self.name,
            "POLICY_LOADED",
            facts={"rules": policy.data.get("rules", {})},
            evidence=[policy],
        )

    def decide(self, issue: str, policy: Finding) -> dict[str, Any] | None:
        rule = policy.facts.get("rules", {}).get(issue)
        self.ctx.trace.emit(
            case_id=self.ctx.case_id,
            event_type="policy_decided",
            actor=self.name,
            decision_code=(rule or {}).get("recommended_action", "NO_POLICY_RULE"),
            evidence_refs=policy.refs() or None,
            attributes={"primary_issue": issue},
        )
        return rule


def classify(
    order: dict[str, Any], items: Finding, payments: Finding, shipment: Finding
) -> tuple[str, list[str]]:
    """Return (primary_issue, supporting tool names) from verified facts only."""
    refunds = payments.facts["refund_events"]
    captures = payments.facts["captures"]
    status = order.get("order_status")
    if any(event.get("status") == "failed" for event in refunds):
        return "refund_failed", ["get_order", "get_refund_timeline", "get_payment_timeline"]
    if any(event.get("status") == "pending" for event in refunds):
        return "refund_pending", ["get_order", "get_refund_timeline", "get_payment_timeline"]
    if payments.facts["mismatches"]:
        return "payment_mismatch", ["get_order", "get_payment_timeline"]
    if status == "canceled" and captures:
        return "canceled_order_paid", ["get_order", "get_payment_timeline"]
    if status == "unavailable" and captures:
        return "unavailable_order_paid", ["get_order", "get_order_items", "get_payment_timeline"]
    if shipment.facts.get("is_late"):
        if shipment.facts["seller_late"]:
            return "late_delivery_seller", [
                "get_order",
                "get_shipment_summary",
                "get_order_items",
                "get_sellers",
            ]
        return "late_delivery_logistics", ["get_order", "get_shipment_summary"]
    if len(captures) >= 2:
        if payments.facts["captured_total"] == items.facts["items_total"]:
            return "valid_split_payment", ["get_order", "get_order_items", "get_payment_timeline"]
        return "duplicate_charge", ["get_order", "get_order_items", "get_payment_timeline"]
    return "unsupported_claim", ["get_order", "get_shipment_summary", "get_payment_timeline"]


def expected_refund(issue: str, items: Finding, payments: Finding) -> Decimal | None:
    """Independent evidence-side estimate used by the verifier to cross-check policy."""
    captures = payments.facts["captures"]
    refunds = payments.facts["refund_events"]
    if issue in {"canceled_order_paid", "unavailable_order_paid"}:
        return payments.facts["captured_total"]
    if issue == "late_delivery_seller":
        return sum((_money(row["freight_value"]) for row in items.facts["items"]), Decimal(0))
    if issue == "duplicate_charge":
        amounts = Counter(_money(event["amount_brl"]) for event in captures)
        return sum((amount * (count - 1) for amount, count in amounts.items()), Decimal(0))
    if issue == "payment_mismatch":
        return sum((_money(e["amount_brl"]) for e in payments.facts["mismatches"]), Decimal(0))
    if issue == "refund_failed":
        failed = [e for e in refunds if e.get("status") == "failed"]
        return sum((_money(e["amount_brl"]) for e in failed), Decimal(0))
    if issue in {"refund_pending", "valid_split_payment", "unsupported_claim"}:
        return Decimal(0)
    return None


class Verifier(Agent):
    name = VERIFIER

    def verify(
        self, output: dict[str, Any], owned_refs: set[str], cross_check_ok: bool
    ) -> list[str]:
        failures: list[str] = []
        refs = output["evidence_refs"]
        if not refs or not set(refs) <= owned_refs:
            failures.append("EVIDENCE_NOT_OWNED")
        for claim in output.get("claim_assessments", []):
            if not set(claim["evidence_refs"]) <= set(refs):
                failures.append("CLAIM_EVIDENCE_UNLINKED")
        finance = output["financial_resolution"]
        total = sum((_money(line["amount_brl"]) for line in finance["refund_lines"]), Decimal(0))
        if _money(finance["recommended_refund_brl"]) != total:
            failures.append("REFUND_TOTAL_MISMATCH")
        status = output["assessment"]["case_status"]
        if finance["recommended_refund_brl"] > 0 and status != "action_required":
            failures.append("STATUS_REFUND_INCONSISTENT")
        if status == "action_required" and not output["resolution_actions"]:
            failures.append("ACTION_MISSING")
        sellers = set(output["affected_entities"]["seller_ids"])
        for party in output["root_cause_analysis"]["responsible_parties"]:
            if party["party_type"] == "seller" and party["party_id"] not in sellers:
                failures.append("SELLER_OUT_OF_SCOPE")
        if not cross_check_ok:
            failures.append("POLICY_EVIDENCE_AMOUNT_DIFFERS")
        self.ctx.trace.emit(
            case_id=self.ctx.case_id,
            event_type="verification_completed",
            actor=self.name,
            decision_code="VERIFIED" if not failures else failures[0],
            evidence_refs=refs[:20] or None,
            attributes={"checks_failed": len(failures)},
        )
        return failures


def _assign(ctx: CaseContext, agent: str, task: str) -> None:
    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="task_assigned",
        actor=COORDINATOR,
        target=agent,
        decision_code=task,
    )


def _handoff(ctx: CaseContext, finding: Finding, target: str = COORDINATOR) -> None:
    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="handoff",
        actor=finding.agent,
        target=target,
        decision_code=finding.decision_code,
        evidence_refs=finding.refs() or None,
    )


def _insufficient(ctx: CaseContext, refs: list[str]) -> dict[str, Any]:
    claims = [
        {
            "claim_id": claim["claim_id"],
            "verdict": "insufficient_evidence",
            "confidence": 0.5,
            "evidence_refs": refs,
        }
        for claim in ctx.case["customer_request"]["claims"]
    ][:5]
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": ctx.case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "case_status": "needs_investigation",
            "confidence": 0.5,
        },
        "affected_entities": {
            "order_ids": [],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": claims,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "INSUFFICIENT_EVIDENCE", "rank": 1}],
            "responsible_parties": [{"party_type": "unknown", "party_id": None}],
        },
        "evidence_refs": refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0,
            "refund_lines": [],
        },
        "resolution_actions": ["request_more_information"],
    }


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    ctx = CaseContext(case, gateway, trace)
    order_agent, payment_agent = OrderAgent(ctx), PaymentAgent(ctx)
    shipment_agent, policy_agent, verifier = ShipmentAgent(ctx), PolicyAgent(ctx), Verifier(ctx)

    # 1. Coordinator scopes the case on the authoritative order row.
    _assign(ctx, ORDER_AGENT, "LOAD_ORDER")
    order_finding = await order_agent.load_order()
    _handoff(ctx, order_finding)
    if order_finding.decision_code != "ORDER_LOADED":
        return _insufficient(ctx, order_finding.refs())
    order = order_finding.facts["order"]

    # 2. Specialists investigate their own domains.
    _assign(ctx, ORDER_AGENT, "RESOLVE_ITEMS_AND_SELLERS")
    items = await order_agent.investigate()
    _handoff(ctx, items)
    _assign(ctx, PAYMENT_AGENT, "RECONCILE_PAYMENTS_AND_REFUNDS")
    payments = await payment_agent.investigate()
    _handoff(ctx, payments)
    _assign(ctx, SHIPMENT_AGENT, "CHECK_DELIVERY_TIMELINE")
    shipment = await shipment_agent.investigate(items.facts["items"])
    _handoff(ctx, shipment)
    _assign(ctx, POLICY_AGENT, "LOAD_POLICY")
    policy = await policy_agent.load()
    _handoff(ctx, policy)

    # 3. Coordinator classifies; policy agent maps the issue to a remedy.
    issue, support_tools = classify(order, items, payments, shipment)
    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="handoff",
        actor=COORDINATOR,
        target=POLICY_AGENT,
        decision_code=issue.upper(),
    )
    rule = policy_agent.decide(issue, policy)
    if rule is None:
        return _insufficient(ctx, order_finding.refs() + policy.refs())

    evidence_by_tool: dict[str, str] = {}
    for finding in (order_finding, items, payments, shipment, policy):
        for item in finding.evidence:
            evidence_by_tool[item.tool] = item.ref
    cited = [evidence_by_tool[t] for t in support_tools if t in evidence_by_tool]
    cited = list(dict.fromkeys([*cited, *policy.refs()]))

    refund = _money(rule.get("refund_brl", 0))
    estimate = expected_refund(issue, items, payments)
    cross_check_ok = estimate is None or estimate == refund

    seller_ids = items.facts["seller_ids"]
    parties = []
    for party in rule.get("responsible_parties", []):
        if party.get("party_type") == "seller":
            for seller_id in seller_ids or [None]:
                parties.append({"party_type": "seller", "party_id": seller_id})
        else:
            parties.append({"party_type": party["party_type"], "party_id": party.get("party_id")})

    action = rule.get("recommended_action")
    refund_lines = []
    if refund > 0:
        entity = (
            items.facts["item_ids"][0]
            if issue.startswith("late_delivery") and items.facts["item_ids"]
            else ctx.order_id
        )
        refund_lines.append(
            {"reason_code": issue.upper(), "amount_brl": _brl(refund), "entity_id": entity}
        )

    conflicts = [c for f in (items, payments, shipment) for c in f.conflicts][:5]
    confidence = 0.95 if cross_check_ok else 0.75
    if shipment.facts.get("unconfirmed_late_event") and issue == "unsupported_claim":
        confidence = 0.85

    claims = []
    for claim in case["customer_request"]["claims"][:5]:
        topic = claim.get("topic")
        if topic == "requested_full_refund":
            paid = payments.facts["captured_total"]
            if refund > 0 and paid > 0 and refund >= paid:
                verdict = "supported"
            elif refund > 0:
                verdict = "partially_supported"
            elif rule.get("case_status") == "needs_investigation":
                verdict = "insufficient_evidence"
            else:
                verdict = "unsupported"
        elif topic == issue and issue != "unsupported_claim":
            verdict = "supported"
        else:
            verdict = "unsupported"
        claims.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": cited,
            }
        )

    output = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": ctx.case_id,
        "assessment": {
            "primary_issue": issue,
            "case_status": rule.get("case_status", "needs_investigation"),
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": [ctx.order_id],
            "item_ids": items.facts["item_ids"],
            "seller_ids": seller_ids,
            "payment_references": payments.facts["payment_refs"] if issue in MONEY_ISSUES else [],
            "shipment_ids": [],
        },
        "claim_assessments": claims,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
            "responsible_parties": parties[:5],
        },
        "evidence_refs": cited,
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": _brl(refund),
            "refund_lines": refund_lines,
        },
        "resolution_actions": [action] if action else [],
    }

    # 4. Verifier checks invariants before the coordinator finalizes.
    _handoff(ctx, Finding(POLICY_AGENT, "REMEDY_PROPOSED", evidence=policy.evidence), VERIFIER)
    owned = {
        item.ref for f in (order_finding, items, payments, shipment, policy) for item in f.evidence
    }
    failures = verifier.verify(output, owned, cross_check_ok)
    if failures and "POLICY_EVIDENCE_AMOUNT_DIFFERS" not in failures:
        output["assessment"]["confidence"] = 0.6
        for claim in output["claim_assessments"]:
            claim["confidence"] = 0.6
    return output
