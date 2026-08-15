"""Invoice preparation, repository-backed and tenant-scoped end to end.

Every write in here happens under a `Scope`. There is no path by which a caller
reaches another organization's project, vendor, purchase order or approval —
not because this module is careful, but because the repositories have no
unscoped method to call.

The stage separation is unchanged:

    Observation → Evidence → Resolved State → Decision → Verification → Policy → Human
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any
from uuid import UUID, uuid4

from construction_ai.domain.models import Evidence, Invoice
from construction_ai.persistence.db import Scope
from construction_ai.resolution.project import classification_band, resolve_projects
from construction_ai.verification.invoice import verify_invoice


class InvoicePipeline:
    def __init__(self, *, repositories, erp_resolver=None):
        self.repos = repositories
        self.erp = erp_resolver

    # -- helpers ------------------------------------------------------------

    def _unique_reference(self, scope: Scope, invoice_number: str) -> str:
        """`INV-8831`, suffixed only if that display id is already taken."""
        base = f"INV-{invoice_number}"
        if self.repos.invoices.get_by_reference(scope=scope, reference=base) is None:
            return base
        for suffix in range(2, 100):
            candidate = f"{base}-{suffix}"
            if self.repos.invoices.get_by_reference(scope=scope, reference=candidate) is None:
                return candidate
        raise RuntimeError(f"could not allocate a reference for invoice {invoice_number}")

    def _project_id_for_reference(self, scope: Scope, reference: str | None) -> UUID | None:
        """An ERP project reference ('PRJ-0042') resolved inside this tenant.

        A reference the tenant does not have resolves to nothing rather than to
        someone else's project of the same name — references are unique per
        organization, never globally.
        """
        if not reference:
            return None
        project = self.repos.projects.get_by_reference(scope=scope.organization_only, reference=reference)
        return UUID(project.project_id) if project else None

    def _resolve_vendor(self, scope: Scope, vendor_name: str) -> UUID | None:
        """Invoice vendor name → a company in *this* tenant, or nothing.

        This resolves the *invoice's* vendor independently. The purchase order's
        supplier is resolved separately by `_resolve_erp_supplier` so that
        vendor_match compares two independent resolutions, never a value copied
        from one side to the other (item 6).
        """
        if not self.erp or not vendor_name:
            return None
        supplier = self.erp.resolve_supplier(vendor_name)
        if not supplier:
            return None
        company = self.repos.companies.find_by_erp_supplier(scope=scope, erp_supplier_id=supplier.get("name"))
        return UUID(company.company_id) if company else None

    def _resolve_erp_supplier(self, scope: Scope, erp_supplier_id: str | None) -> UUID | None:
        """ERP PO/quote supplier → a local company, independent of the invoice.

        The ERP record carries the supplier ERP actually returned; that is the
        authoritative input. This path must never borrow the invoice's resolved
        vendor — doing so made vendor_match tautological in v0.3.
        """
        if not erp_supplier_id:
            return None
        from construction_ai.erp import resolve_supplier_to_company

        resolution = resolve_supplier_to_company(
            self.repos, scope, erp_supplier_id=erp_supplier_id, supplier_name=erp_supplier_id
        )
        return resolution.company_id

    def _persist_evidence(
        self, scope: Scope, candidates: list[Evidence], *, source_version_id: UUID | None,
        subject_type: str, subject_id: UUID,
    ) -> list[UUID]:
        stored: list[UUID] = []
        for candidate in candidates:
            record = self.repos.evidence.record(
                scope=scope,
                field=candidate.field,
                value=candidate.value,
                confidence=candidate.confidence,
                authority=candidate.authority,
                source_type=candidate.source_type,
                source_id=candidate.source_id,
                source_version_id=source_version_id,
                extractor=candidate.extractor,
                observed_at=candidate.observed_at,
                # Evidence without a subject is under-specified: "total = 4760"
                # is a fact about this invoice, not about the project.
                subject_type=subject_type,
                subject_id=subject_id,
            )
            stored.append(UUID(record.evidence_id))
        return stored

    # -- the pipeline -------------------------------------------------------

    def process(
        self,
        *,
        scope: Scope,
        extracted: Invoice,
        signals: dict[str, Any],
        evidence: list[Evidence] | None = None,
        work_confirmed: bool | None = None,
        source_version_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Process an extracted invoice end to end.

        `work_confirmed` is intentionally `None` on the API path (item 12): a
        caller cannot declare physical work complete. When None, the pipeline
        derives it from `work_confirmations` records. A non-None value is a test
        override only and is never set by the HTTP API.
        """
        with self.repos.db.transaction():
            organization_scope = scope.organization_only

            # 1. Resolved state — project, then vendor identity.
            projects = self.repos.projects.list(scope=organization_scope)
            candidates = resolve_projects(projects, signals)
            project_id: UUID | None = None
            if candidates and classification_band(candidates[0].confidence) == "automatic":
                project_id = UUID(candidates[0].project_id)
            working = organization_scope.for_project(project_id)

            vendor_company_id = self._resolve_vendor(working, extracted.vendor_name)

            # 2. Duplicate detection before insert: a second row for the same vendor
            #    invoice number is not something to create and then complain about.
            duplicate = self.repos.invoices.find_duplicate(
                scope=organization_scope, vendor_company_id=vendor_company_id, invoice_number=extracted.invoice_number
            )
            if duplicate is not None:
                invoice = duplicate
                invoice_id = UUID(invoice.invoice_id)
            else:
                invoice = self.repos.invoices.create(
                    scope=working,
                    reference=self._unique_reference(organization_scope, extracted.invoice_number),
                    invoice_number=extracted.invoice_number,
                    vendor_name=extracted.vendor_name,
                    total=extracted.total,
                    subtotal=extracted.subtotal,
                    tax=extracted.tax,
                    currency=extracted.currency,
                    po_reference=extracted.po_number,
                    quote_reference=extracted.quote_number,
                    vendor_company_id=vendor_company_id,
                    source_version_id=source_version_id,
                    created_by="ai",
                )
                invoice_id = UUID(invoice.invoice_id)

            # Idempotent reprocessing: if this invoice already has an approval,
            # do not create a second one. Evidence, packet, graph, and audit are
            # likewise skipped — they were written on the first pass.
            existing_approval = None
            if duplicate is not None:
                existing_approval = self.repos.approvals.for_subject(
                    scope=working, subject_type="invoice", subject_id=invoice_id
                )

            if existing_approval is None:
                evidence_ids = self._persist_evidence(
                    working, list(evidence or []), source_version_id=source_version_id,
                    subject_type="invoice", subject_id=invoice_id,
                )
            else:
                evidence_ids = [UUID(eid) for eid in existing_approval.evidence_ids]

            # 3. Related commercial records.
            #
            # The PO is stored under the project ERPNext says it belongs to, not
            # under the project we resolved. Writing our own answer into the record
            # we are about to check it against would make project_match tautological.
            #
            # The PO/quote vendor is resolved INDEPENDENTLY from the supplier ERP
            # actually returned (item 6). It is never copied from the invoice's
            # resolved vendor — that destroyed vendor_match independence in v0.3.
            #
            # ERP observations are first-class evidence (item 8): every ERP query
            # affecting the decision is persisted with its raw hash, normalized
            # fields, adapter version and retrieval time — never an ephemeral value.
            purchase_order = quote = None
            erp_evidence_ids: list[UUID] = []
            if self.erp and invoice.po_number:
                erp_po, po_evidence = self.erp.resolve_purchase_order(invoice.po_number)
                if erp_po:
                    erp_project_id = self._project_id_for_reference(organization_scope, erp_po.project_id)
                    po_vendor_company_id = self._resolve_erp_supplier(working, erp_po.vendor_company_id)
                    purchase_order = self.repos.purchase_orders.upsert(
                        scope=organization_scope.for_project(erp_project_id),
                        reference=erp_po.po_number,
                        vendor_company_id=po_vendor_company_id,
                        amount=erp_po.amount,
                        quote_reference=erp_po.quote_number,
                        erp_docname=erp_po.po_number,
                    )
                    if existing_approval is None and po_evidence:
                        erp_evidence_ids.extend(self._persist_evidence(
                            working, po_evidence, source_version_id=None,
                            subject_type="invoice", subject_id=invoice_id,
                        ))
            if self.erp and invoice.quote_number:
                erp_quote, quote_evidence = self.erp.resolve_quote(invoice.quote_number)
                if erp_quote:
                    erp_project_id = self._project_id_for_reference(organization_scope, erp_quote.project_id)
                    quote_vendor_company_id = self._resolve_erp_supplier(working, erp_quote.vendor_company_id)
                    quote = self.repos.quotes.upsert(
                        scope=organization_scope.for_project(erp_project_id),
                        reference=erp_quote.quote_number,
                        vendor_company_id=quote_vendor_company_id,
                        amount=erp_quote.amount,
                        approved=erp_quote.approved,
                        erp_docname=erp_quote.quote_number,
                    )
                    if existing_approval is None and quote_evidence:
                        erp_evidence_ids.extend(self._persist_evidence(
                            working, quote_evidence, source_version_id=None,
                            subject_type="invoice", subject_id=invoice_id,
                        ))
            evidence_ids = [*evidence_ids, *erp_evidence_ids]

            # 4. Verification, then policy. Both unchanged and both deterministic.
            invoice.project_id = str(project_id) if project_id else None
            invoice.vendor_company_id = str(vendor_company_id) if vendor_company_id else None
            # Item 12: work completion is derived from records, not a request field.
            # rc6: The production payable pipeline MUST NOT fall back to generic
            # project-wide work confirmation. An invoice with scoped SOV
            # allocations requires invoice/SOV-specific confirmation; a
            # scope-free project confirmation cannot silently satisfy payable
            # work checks. allow_project_fallback=False enforces this.
            if work_confirmed is None:
                from construction_ai.work.confirmation import WorkConfirmationService

                work_confirmed = WorkConfirmationService(self.repos.work_confirmations).is_work_confirmed(
                    scope=working, project_id=project_id, invoice_id=invoice_id,
                    allow_project_fallback=False,
                )
            # Phase 15/16: scope-specific work confirmation + quantitative progress
            # billing, derived from persisted SOV allocations. Both are None
            # (UNAVAILABLE) when the invoice has no SOV allocations.
            work_scope_confirmed: bool | None = None
            progress_billing_overbilled: bool | None = None
            allocations = self.repos.invoice_allocations.for_invoice(scope=working, invoice_id=invoice_id)
            if allocations:
                from construction_ai.work.confirmation import WorkConfirmationService
                from construction_ai.verification.progress_billing import evaluate_progress_billing

                sov_item_ids = [UUID(a.sov_item_id) for a in allocations]
                work_scope_confirmed = WorkConfirmationService(
                    self.repos.work_confirmations
                ).is_work_confirmed_for_sov_items(scope=working, sov_item_ids=sov_item_ids)
                progress = evaluate_progress_billing(self.repos, scope=working, invoice_id=invoice_id)
                progress_billing_overbilled = progress.overbilled if progress.evaluated else None
            # Phase 18: tri-state duplicate detection. A pre-insert find_duplicate
            # match (vendor+invoice number) is itself a CONFIRMED_DUPLICATE — the
            # submission duplicates an existing invoice. For a fresh invoice, run
            # the broader signal set (ERP supplier, content hash, weak signals).
            from construction_ai.verification.duplicate import CONFIRMED_DUPLICATE, detect_duplicates

            if duplicate is not None:
                duplicate_status = CONFIRMED_DUPLICATE
            else:
                dup_result = detect_duplicates(self.repos, scope=organization_scope, invoice_id=invoice_id)
                duplicate_status = dup_result.status
            verification = verify_invoice(
                invoice,
                purchase_order,
                quote,
                duplicate=duplicate is not None,
                work_confirmed=work_confirmed,
                require_vendor_identity=True,
                work_scope_confirmed=work_scope_confirmed,
                progress_billing_overbilled=progress_billing_overbilled,
                duplicate_status=duplicate_status,
            )
            recommended = "APPROVE" if verification.passed else "HOLD"

            if existing_approval is not None:
                approval = existing_approval
                packet_id = None
                graph_counts = None
            else:
                approval = self.repos.approvals.create(
                    scope=working,
                    reference=f"APR-{uuid4().hex[:12]}",
                    approval_type="PURCHASE_INVOICE",
                    subject_type="invoice",
                    subject_id=invoice_id,
                    recommended_action=recommended,
                    amount=invoice.total,
                    exceptions=verification.exceptions,
                    evidence_ids=evidence_ids,
                    requested_by="ai",
                )
                self.repos.invoices.set_status(scope=working, invoice_id=invoice_id, status="prepared" if recommended == "APPROVE" else "held")

                packet_payload = {
                    "approval": {
                        "approval_id": approval.approval_id,
                        "reference": approval.reference,
                        "status": approval.status.value,
                        "recommended_action": approval.recommended_action,
                        "amount": approval.amount,
                    },
                    "subject": asdict(invoice),
                    "verification": {
                        "subject_id": str(invoice_id),
                        "checks": verification.checks,
                        "check_details": verification.check_details,
                        "exceptions": verification.exceptions,
                        "passed": verification.passed,
                    },
                    "evidence": [asdict(e) for e in self.repos.evidence.get_many(scope=working, evidence_ids=evidence_ids)],
                    "project_candidates": [asdict(c) for c in candidates[:5]],
                    "related_records": {
                        "purchase_order": asdict(purchase_order) if purchase_order else None,
                        "quote": asdict(quote) if quote else None,
                    },
                    "warnings": [],
                }
                packet_id = self.repos.approval_packets.create(
                    scope=working,
                    approval_id=UUID(approval.approval_id),
                    reference=f"APKT-{uuid4().hex[:12]}",
                    payload=packet_payload,
                    verifier_version=verification.check_details.get("vendor_match", {}).get("verifier_version")
                    if verification.check_details else None,
                    policy_inputs={
                        "checks": list(verification.checks.keys()),
                        "duplicate_status": duplicate_status,
                        "work_scope_confirmed": work_scope_confirmed,
                        "progress_billing_overbilled": progress_billing_overbilled,
                    },
                )

                # Project the invoice and everything it now relates to into typed nodes
                # and observed edges. Structural only — nothing here is inferred, so
                # nothing here needs promoting.
                graph_counts = None
                if project_id is not None:
                    from construction_ai.graph.projection import project_graph

                    graph_counts = project_graph(self.repos, working)

                self.repos.audit.append(
                    scope=working,
                    event_type="INVOICE_APPROVAL_PREPARED",
                    actor="ai",
                    object_type="invoice",
                    object_id=invoice_id,
                    payload={
                        "approval_id": approval.approval_id,
                        "packet_id": str(packet_id),
                        "recommended_action": recommended,
                        "exceptions": verification.exceptions,
                        "project_resolved": bool(project_id),
                        "graph": graph_counts,
                    },
                )

            return {
                "status": "prepared",
                "requires_human": True,
                "invoice_id": invoice.invoice_id,
                "invoice_reference": invoice.reference,
                "project_id": str(project_id) if project_id else None,
                "approval_id": approval.approval_id,
                "packet_id": str(packet_id) if packet_id else None,
                "recommended_action": recommended,
                "exceptions": verification.exceptions,
                "signals": dict(signals),
                "duplicate_of": duplicate.invoice_id if duplicate else None,
                "graph": graph_counts,
            }
