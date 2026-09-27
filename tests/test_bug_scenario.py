from datetime import date, datetime, timezone
from decimal import Decimal

from terminalweave.billing.demurrage import (
    BusinessCalendar,
    DemurrageCalculator,
    DemurrageTariff,
    TariffBand,
)
from terminalweave.billing.discounts import DiscountBook, DiscountRule
from terminalweave.billing.reconciliation import (
    PaymentReconciler,
    PaymentRecord,
    Receivable,
)
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


def test_free_period_covering_all_days_bills_nothing():
    calendar = BusinessCalendar(weekends_free=False)
    tariff = DemurrageTariff(3, (TariffBand(1, None, Money(Decimal("10"), "USD")),))
    calculator = DemurrageCalculator(calendar, tariff)
    lines = calculator.calculate(
        datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 3, tzinfo=UTC)
    )
    assert lines == []
    total = calculator.total(
        datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 3, tzinfo=UTC)
    )
    assert total.amount == Decimal("0.00") and total.currency == "USD"


def test_holiday_does_not_consume_free_chargeable_day():
    calendar = BusinessCalendar({date(2026, 1, 1)}, weekends_free=False)
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
        (date(2026, 1, 3), 1, Decimal("10.00")),
    ]


def test_tariff_bands_are_snapshotted_against_later_rate_edits():
    bands = [TariffBand(1, None, Money(Decimal("10"), "USD"))]
    tariff = DemurrageTariff(0, bands)
    calculator = DemurrageCalculator(BusinessCalendar(weekends_free=False), tariff)
    discharge = datetime(2026, 1, 1, tzinfo=UTC)
    gate_out = datetime(2026, 1, 2, tzinfo=UTC)
    before = calculator.total(discharge, gate_out)
    bands[0] = TariffBand(1, None, Money(Decimal("999"), "USD"))
    bands.append(TariffBand(2, None, Money(Decimal("50"), "USD")))
    after = calculator.total(discharge, gate_out)
    assert before.amount == Decimal("20.00")
    assert after.amount == before.amount


def test_duplicate_payment_record_cannot_write_off_twice():
    reconciler = PaymentReconciler()
    payments = [
        PaymentRecord("P1", "REF", Money(Decimal("100"), "USD")),
        PaymentRecord("P1", "REF", Money(Decimal("100"), "USD")),
    ]
    receivables = [
        Receivable("INV-1", "REF", Money(Decimal("100"), "USD")),
        Receivable("INV-2", "REF", Money(Decimal("100"), "USD")),
    ]
    matched = reconciler.match(payments, receivables)
    assert matched == {"P1": "INV-1"}
    unmatched = reconciler.unmatched_value(receivables, matched)
    assert unmatched.amount == Decimal("100.00")


def test_distinct_payments_still_match_distinct_receivables():
    reconciler = PaymentReconciler()
    payments = [
        PaymentRecord("P1", "REF", Money(Decimal("100"), "USD")),
        PaymentRecord("P2", "REF", Money(Decimal("100"), "USD")),
    ]
    receivables = [
        Receivable("INV-1", "REF", Money(Decimal("100"), "USD")),
        Receivable("INV-2", "REF", Money(Decimal("100"), "USD")),
    ]
    assert reconciler.match(payments, receivables) == {"P1": "INV-1", "P2": "INV-2"}


def test_discounted_receivable_matches_payment_within_one_cent():
    rule = DiscountRule("CUT", Money(Decimal("50"), "USD"), Decimal("0.15"))
    subtotal = Money(Decimal("99.99"), "USD")
    discount = rule.apply(subtotal)
    net = subtotal - discount
    reconciler = PaymentReconciler()
    payments = [PaymentRecord("P1", "INV-9", Money(net.amount + Decimal("0.01"), "USD"))]
    receivables = [Receivable("INV-9", "INV-9", net)]
    assert reconciler.match(payments, receivables) == {"P1": "INV-9"}


def test_discount_book_skips_rules_in_other_currencies():
    book = DiscountBook(
        [
            DiscountRule("EUR10", Money(Decimal("50"), "EUR"), Decimal("0.10")),
            DiscountRule("USD5", Money(Decimal("50"), "USD"), Decimal("0.05")),
        ]
    )
    code, amount = book.best(Money(Decimal("100"), "USD"), {"EUR10", "USD5"})
    assert code == "USD5"
    assert amount.amount == Decimal("5.00") and amount.currency == "USD"
    code, amount = book.best(Money(Decimal("100"), "USD"), {"EUR10"})
    assert code is None
    assert amount.amount == Decimal("0.00") and amount.currency == "USD"
