from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from terminalweave.core.money import Money
from terminalweave.core.errors import ValidationError, ConflictError

DEFAULT_TOLERANCE = Decimal('0.01')

@dataclass(frozen=True)
class PaymentRecord:
    payment_id: str
    reference: str
    amount: Money

    def __post_init__(self) -> None:
        if not self.payment_id:
            raise ValidationError('payment id required')
        if self.amount.amount < 0:
            raise ValidationError('payment amount cannot be negative')

@dataclass(frozen=True)
class Receivable:
    invoice_id: str
    reference: str
    amount: Money

    def __post_init__(self) -> None:
        if not self.invoice_id:
            raise ValidationError('invoice id required')
        if self.amount.amount < 0:
            raise ValidationError('receivable amount cannot be negative')

class PaymentReconciler:
    """Matches payments to receivables and applies the write-offs.

    Each payment is keyed by payment_id and matched at most once; each
    receivable is settled at most once. Re-presenting an already applied
    payment (or matching into an already settled invoice) is a conflict rather
    than a second write-off.
    """

    def __init__(self, tolerance: Decimal = DEFAULT_TOLERANCE) -> None:
        if tolerance < 0:
            raise ValidationError('tolerance cannot be negative')
        self.tolerance = tolerance
        self._applied_payments: set[str] = set()
        self._settled_invoices: set[str] = set()

    def _best_match(
        self,
        payment: PaymentRecord,
        receivables: list[Receivable],
        used_invoice_ids: set[str],
        tolerance: Decimal,
    ) -> Receivable | None:
        candidates = []
        for rec in receivables:
            if rec.invoice_id in used_invoice_ids:
                continue
            if rec.amount.currency != payment.amount.currency:
                continue
            delta = abs(rec.amount.amount - payment.amount.amount)
            if delta <= tolerance:
                # exact reference match wins, then smallest rounding tail
                candidates.append((0 if rec.reference == payment.reference else 1, delta, rec.invoice_id, rec))
        if not candidates:
            return None
        _, _, _, rec = min(candidates)
        return rec

    def match(
        self,
        payments: list[PaymentRecord],
        receivables: list[Receivable],
        tolerance: Decimal | None = None,
    ) -> dict[str, str]:
        """Pure matching within one batch: duplicate payment rows are rejected."""
        tolerance = self.tolerance if tolerance is None else tolerance
        if tolerance < 0:
            raise ValidationError('tolerance cannot be negative')
        result = {}
        seen_payment_ids: set[str] = set()
        used_invoice_ids: set[str] = set()
        for payment in payments:
            if payment.payment_id in seen_payment_ids:
                raise ConflictError(f'payment {payment.payment_id} appears more than once in batch')
            seen_payment_ids.add(payment.payment_id)
            rec = self._best_match(payment, receivables, used_invoice_ids, tolerance)
            if rec is not None:
                result[payment.payment_id] = rec.invoice_id
                used_invoice_ids.add(rec.invoice_id)
        return result

    def apply(
        self,
        payments: list[PaymentRecord],
        receivables: list[Receivable],
        tolerance: Decimal | None = None,
    ) -> dict[str, str]:
        """Stateful write-off: never applies the same payment twice."""
        tolerance = self.tolerance if tolerance is None else tolerance
        if tolerance < 0:
            raise ValidationError('tolerance cannot be negative')
        result = {}
        for payment in payments:
            if payment.payment_id in self._applied_payments:
                raise ConflictError(f'payment {payment.payment_id} has already been applied')
            rec = self._best_match(payment, receivables, self._settled_invoices, tolerance)
            if rec is None:
                continue
            self._applied_payments.add(payment.payment_id)
            self._settled_invoices.add(rec.invoice_id)
            result[payment.payment_id] = rec.invoice_id
        return result

    def is_applied(self, payment_id: str) -> bool:
        return payment_id in self._applied_payments

    def is_settled(self, invoice_id: str) -> bool:
        return invoice_id in self._settled_invoices

    @property
    def applied_payments(self) -> frozenset[str]:
        return frozenset(self._applied_payments)

    @property
    def settled_invoices(self) -> frozenset[str]:
        return frozenset(self._settled_invoices)

    def unmatched_value(self, receivables: list[Receivable], matched: dict[str, str]) -> Money:
        if not receivables:
            return Money.zero()
        currency = receivables[0].amount.currency
        matched_ids = set(matched.values())
        total = Money.zero(currency)
        for r in receivables:
            if r.amount.currency != currency:
                raise ValidationError('mixed currencies')
            if r.invoice_id not in matched_ids:
                total = total + r.amount
        return total
