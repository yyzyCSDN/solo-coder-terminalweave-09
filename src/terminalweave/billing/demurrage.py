from __future__ import annotations
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from terminalweave.core.clock import ensure_utc, utc_now
from terminalweave.core.errors import ValidationError, NotFoundError, ConflictError
from terminalweave.core.money import Money

@dataclass(frozen=True)
class TariffBand:
    from_day: int
    to_day: int | None
    daily_rate: Money

    def __post_init__(self) -> None:
        if self.from_day < 1 or (self.to_day is not None and self.to_day < self.from_day):
            raise ValidationError('invalid tariff band')

class BusinessCalendar:

    def __init__(self, holidays: set[date] | None=None, weekends_free: bool=True):
        self.holidays = set(holidays or set())
        self.weekends_free = weekends_free

    def is_chargeable(self, day: date) -> bool:
        if day in self.holidays:
            return False
        if self.weekends_free and day.weekday() >= 5:
            return False
        return True

    def chargeable_days(self, start: date, end_inclusive: date) -> list[date]:
        if end_inclusive < start:
            return []
        result = []
        cursor = start
        while cursor <= end_inclusive:
            if self.is_chargeable(cursor):
                result.append(cursor)
            cursor += timedelta(days=1)
        return result

@dataclass(frozen=True)
class DemurrageTariff:
    free_chargeable_days: int
    bands: tuple[TariffBand, ...]

    def __post_init__(self) -> None:
        if self.free_chargeable_days < 0 or not self.bands:
            raise ValidationError('invalid demurrage tariff')
        currencies = {band.daily_rate.currency for band in self.bands}
        if len(currencies) != 1:
            raise ValidationError('tariff bands must share one currency')
        ordered = tuple(sorted(self.bands, key=lambda b: b.from_day))
        if ordered[0].from_day != 1:
            raise ValidationError('tariff bands must start at day 1')
        previous_end = 0
        for position, band in enumerate(ordered):
            # bands must be contiguous and non-overlapping so a charge day has exactly one rate
            if band.from_day != previous_end + 1:
                raise ValidationError('tariff bands must be contiguous')
            if band.to_day is None:
                if position != len(ordered) - 1:
                    raise ValidationError('open-ended band must be last')
                break
            previous_end = band.to_day
        object.__setattr__(self, 'bands', ordered)

    @property
    def currency(self) -> str:
        return self.bands[0].daily_rate.currency

    def rate_for_charge_day(self, charge_day: int) -> Money:
        for band in self.bands:
            if charge_day >= band.from_day and (band.to_day is None or charge_day <= band.to_day):
                return band.daily_rate
        raise ValidationError('no tariff band for charge day')

@dataclass(frozen=True)
class DemurrageLine:
    day: date
    charge_day_number: int
    amount: Money

@dataclass(frozen=True)
class DemurrageBill:
    """An issued bill is an immutable snapshot: concrete lines, never re-priced."""
    bill_id: str
    reference: str
    discharge_at: datetime
    gate_out_at: datetime
    lines: tuple[DemurrageLine, ...]
    currency: str
    issued_at: datetime
    tariff_label: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, 'discharge_at', ensure_utc(self.discharge_at))
        object.__setattr__(self, 'gate_out_at', ensure_utc(self.gate_out_at))
        object.__setattr__(self, 'issued_at', ensure_utc(self.issued_at))
        if not self.bill_id or not self.reference:
            raise ValidationError('bill id and reference required')
        days = [line.day for line in self.lines]
        if len(days) != len(set(days)):
            raise ValidationError('a day may only appear on one bill line')
        for line in self.lines:
            if line.amount.currency != self.currency:
                raise ValidationError('bill line currency mismatch')

    @property
    def total(self) -> Money:
        total = Money.zero(self.currency)
        for line in self.lines:
            total = total + line.amount
        return total

class DemurrageCalculator:

    def __init__(self, calendar: BusinessCalendar, tariff: DemurrageTariff):
        self.calendar = calendar
        self.tariff = tariff

    def calculate(self, discharge_at: datetime, gate_out_at: datetime) -> list[DemurrageLine]:
        discharge_at = ensure_utc(discharge_at)
        gate_out_at = ensure_utc(gate_out_at)
        if gate_out_at < discharge_at:
            raise ValidationError('gate-out cannot precede discharge')
        # Every calendar day is enumerated once; non-chargeable days (holidays,
        # free weekends) are skipped rather than charged.
        days = self.calendar.chargeable_days(discharge_at.date(), gate_out_at.date())
        # The free period consumes exactly its number of chargeable days, so the
        # first day after it is billable day 1 and no day lands in two segments.
        billable = days[self.tariff.free_chargeable_days:]
        lines = []
        for index, day in enumerate(billable, start=1):
            lines.append(DemurrageLine(day, index, self.tariff.rate_for_charge_day(index)))
        return lines

    def total(self, discharge_at: datetime, gate_out_at: datetime) -> Money:
        lines = self.calculate(discharge_at, gate_out_at)
        if not lines:
            return Money.zero(self.tariff.currency)
        total = Money.zero(lines[0].amount.currency)
        for line in lines:
            total = total + line.amount
        return total

class DemurrageLedger:
    """Stores issued bills as fixed snapshots.

    Replacing the tariff/calculator only affects future issues; bills already on
    the ledger keep their originally priced lines.
    """

    def __init__(self) -> None:
        self._bills: dict[str, DemurrageBill] = {}

    def issue(
        self,
        bill_id: str,
        reference: str,
        calculator: DemurrageCalculator,
        discharge_at: datetime,
        gate_out_at: datetime,
        issued_at: datetime | None = None,
        tariff_label: str | None = None,
    ) -> DemurrageBill:
        existing = self._bills.get(bill_id)
        if existing is not None:
            raise ConflictError(f'bill {bill_id} already issued and cannot be repriced')
        lines = tuple(calculator.calculate(discharge_at, gate_out_at))
        bill = DemurrageBill(
            bill_id,
            reference,
            discharge_at,
            gate_out_at,
            lines,
            calculator.tariff.currency,
            issued_at or utc_now(),
            tariff_label,
        )
        self._bills[bill_id] = bill
        return bill

    def get(self, bill_id: str) -> DemurrageBill:
        bill = self._bills.get(bill_id)
        if bill is None:
            raise NotFoundError(f'unknown bill {bill_id}')
        return bill

    def __contains__(self, bill_id: object) -> bool:
        return bill_id in self._bills

    def all(self) -> tuple[DemurrageBill, ...]:
        return tuple(self._bills[key] for key in sorted(self._bills))
