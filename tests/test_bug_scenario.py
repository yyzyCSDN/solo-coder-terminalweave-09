from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from terminalweave.billing.demurrage import (
    BusinessCalendar,
    DemurrageCalculator,
    DemurrageLedger,
    DemurrageTariff,
    TariffBand,
)
from terminalweave.billing.discounts import DiscountBook, DiscountRule
from terminalweave.billing.reconciliation import (
    PaymentRecord,
    PaymentReconciler,
    Receivable,
)
from terminalweave.core.errors import ConflictError, ValidationError
from terminalweave.core.money import Money


UTC = timezone.utc


def test_free_period_is_consumed_once_before_progressive_bands():
    calendar = BusinessCalendar(weekends_free=False)
    tariff = DemurrageTariff(
        1,
        (
            TariffBand(1, 1, Money(Decimal("10"), "USD")),
            TariffBand(2, None, Money(Decimal("20"), "USD")),
        ),
    )
    calculator = DemurrageCalculator(calendar, tariff)
    lines = calculator.calculate(
        datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 3, tzinfo=UTC)
    )
    assert [(line.day, line.charge_day_number, line.amount.amount) for line in lines] == [
        (date(2026, 1, 2), 1, Decimal("10.00")),
        (date(2026, 1, 3), 2, Decimal("20.00")),
    ]


def _progressive_tariff(free_days):
    return DemurrageTariff(
        free_days,
        (
            TariffBand(1, 2, Money(Decimal("10"), "USD")),
            TariffBand(3, 4, Money(Decimal("20"), "USD")),
            TariffBand(5, None, Money(Decimal("30"), "USD")),
        ),
    )


def test_every_day_falls_into_exactly_one_band_across_holidays():
    # 2026-01-01 is a Thursday; 2026-01-02 is a holiday and is not billed.
    calendar = BusinessCalendar(holidays={date(2026, 1, 2)}, weekends_free=False)
    calculator = DemurrageCalculator(calendar, _progressive_tariff(0))
    lines = calculator.calculate(
        datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 6, tzinfo=UTC)
    )
    billed_days = [line.day for line in lines]
    assert billed_days == [
        date(2026, 1, 1),
        date(2026, 1, 3),
        date(2026, 1, 4),
        date(2026, 1, 5),
        date(2026, 1, 6),
    ]
    assert len(set(billed_days)) == len(billed_days)
    assert [line.charge_day_number for line in lines] == [1, 2, 3, 4, 5]
    assert [line.amount.amount for line in lines] == [
        Decimal("10.00"),
        Decimal("10.00"),
        Decimal("20.00"),
        Decimal("20.00"),
        Decimal("30.00"),
    ]


def test_free_period_spanning_holiday_is_not_double_billed():
    # One free chargeable day plus a holiday in between: the day crossing the
    # free period must not appear in any band.
    calendar = BusinessCalendar(holidays={date(2026, 1, 2)}, weekends_free=False)
    calculator = DemurrageCalculator(calendar, _progressive_tariff(1))
    lines = calculator.calculate(
        datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 4, tzinfo=UTC)
    )
    assert [(line.day, line.charge_day_number) for line in lines] == [
        (date(2026, 1, 3), 1),
        (date(2026, 1, 4), 2),
    ]


def test_weekend_is_skipped_not_billed_when_band_boundary_crosses_it():
    # 2026-01-03 and 2026-01-04 are a weekend.
    calendar = BusinessCalendar(weekends_free=True)
    calculator = DemurrageCalculator(calendar, _progressive_tariff(2))
    lines = calculator.calculate(
        datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 5, tzinfo=UTC)
    )
    assert [line.day for line in lines] == [date(2026, 1, 5)]
    assert lines[0].charge_day_number == 1
    assert lines[0].amount.amount == Decimal("10.00")


def test_issued_bill_keeps_original_rate_when_tariff_changes():
    calendar = BusinessCalendar(weekends_free=False)
    old_calculator = DemurrageCalculator(
        calendar,
        DemurrageTariff(0, (TariffBand(1, None, Money(Decimal("10"), "USD")),)),
    )
    ledger = DemurrageLedger()
    bill = ledger.issue(
        "B-1",
        "CONT-1",
        old_calculator,
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 3, tzinfo=UTC),
        tariff_label="2026-Q1",
    )
    assert bill.total == Money(Decimal("30.00"), "USD")
    assert [line.amount.amount for line in bill.lines] == [
        Decimal("10.00"),
        Decimal("10.00"),
        Decimal("10.00"),
    ]

    # A later tariff change must not touch the already issued bill.
    new_calculator = DemurrageCalculator(
        calendar,
        DemurrageTariff(0, (TariffBand(1, None, Money(Decimal("25"), "USD")),)),
    )
    assert new_calculator.total(
        datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 3, tzinfo=UTC)
    ) == Money(Decimal("75.00"), "USD")
    assert ledger.get("B-1").total == Money(Decimal("30.00"), "USD")

    # The same bill id cannot be issued (repriced) twice.
    with pytest.raises(ConflictError):
        ledger.issue(
            "B-1",
            "CONT-1",
            new_calculator,
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 3, tzinfo=UTC),
        )


def test_discount_book_ignores_foreign_currency_rules():
    book = DiscountBook(
        [
            DiscountRule("EUR_DEAL", Money(Decimal("50"), "EUR"), Decimal("0.10")),
            DiscountRule("USD_DEAL", Money(Decimal("100"), "USD"), Decimal("0.05")),
        ]
    )
    code, amount = book.best(Money(Decimal("200"), "USD"), {"EUR_DEAL", "USD_DEAL"})
    assert code == "USD_DEAL"
    assert amount == Money(Decimal("10.00"), "USD")

    # No matching-currency rule means no discount, never an exception.
    code, amount = book.best(Money(Decimal("200"), "JPY"), {"EUR_DEAL", "USD_DEAL"})
    assert code is None
    assert amount == Money.zero("JPY")


def test_reconciliation_accepts_rounding_tail_but_rejects_cross_currency():
    reconciler = PaymentReconciler()
    payments = [PaymentRecord("P-1", "INV-1", Money(Decimal("99.99"), "USD"))]
    receivables = [
        Receivable("INV-1", "INV-1", Money(Decimal("100.00"), "USD")),
        Receivable("INV-2", "INV-2", Money(Decimal("99.99"), "EUR")),
    ]
    assert reconciler.match(payments, receivables) == {"P-1": "INV-1"}


def test_duplicate_payment_cannot_be_applied_twice():
    receivables = [
        Receivable("INV-1", "INV-1", Money(Decimal("100.00"), "USD")),
        Receivable("INV-2", "INV-2", Money(Decimal("100.00"), "USD")),
    ]
    reconciler = PaymentReconciler()
    first = reconciler.apply(
        [PaymentRecord("P-1", "INV-1", Money(Decimal("100.00"), "USD"))], receivables
    )
    assert first == {"P-1": "INV-1"}
    assert reconciler.is_applied("P-1")
    assert reconciler.is_settled("INV-1")

    # Re-presenting the same payment must not write anything off again.
    with pytest.raises(ConflictError):
        reconciler.apply(
            [PaymentRecord("P-1", "INV-2", Money(Decimal("100.00"), "USD"))],
            receivables,
        )
    assert not reconciler.is_settled("INV-2")

    # The same payment id twice in one batch is also rejected.
    fresh = PaymentReconciler()
    duplicate_batch = [
        PaymentRecord("P-9", "INV-1", Money(Decimal("100.00"), "USD")),
        PaymentRecord("P-9", "INV-2", Money(Decimal("100.00"), "USD")),
    ]
    with pytest.raises(ConflictError):
        fresh.match(duplicate_batch, receivables)


def test_mixed_currency_tariff_bands_are_rejected():
    with pytest.raises(ValidationError):
        DemurrageTariff(
            0,
            (
                TariffBand(1, 1, Money(Decimal("10"), "USD")),
                TariffBand(2, None, Money(Decimal("20"), "EUR")),
            ),
        )
