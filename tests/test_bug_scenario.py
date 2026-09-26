from datetime import date, datetime, timezone
from decimal import Decimal

from terminalweave.billing.demurrage import (
    BusinessCalendar,
    DemurrageCalculator,
    DemurrageTariff,
    TariffBand,
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
