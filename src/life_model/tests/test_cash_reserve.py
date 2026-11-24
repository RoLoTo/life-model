# Copyright 2026 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

import unittest
from datetime import date

from ..account.bank import BankAccount
from ..checks import account_residuals, assert_conservation
from ..lifeevents import LifeEvent, LifeEvents
from ..model import LifeModel
from ..people.family import Family
from ..people.person import Person, Spending
from ..policies import move_from_bank
from ..spec import build_household, spec_from_model
from ..spec.io import Format, dumps, loads
from .spec_helpers import fixture_config, minimal, spec_of


class TestCashReserve(unittest.TestCase):
    def _household(self, spending=0):
        model = LifeModel(start_year=2026, end_year=2026, config=fixture_config(), collect="month", checks="strict")
        owner = Person(Family(model), "Pat", 40, 65, Spending(model, base_annual=spending))
        bank = BankAccount(owner, balance=1000, interest_pct=0)
        return model, owner, bank, LifeEvents(model)

    def test_bills_leave_earmarked_cash_alone_and_release_runs_first_on_its_day(self):
        from ..account.cash_reserve import CashReserve

        model, owner, bank, schedule = self._household(spending=2400)
        snapshots = []
        schedule.add(LifeEvent(2026, "Before release", lambda: snapshots.append(bank.balance), month=2, day=14))
        schedule.add(LifeEvent(2026, "On release day", lambda: snapshots.append(bank.balance), month=2, day=15))
        reserve = CashReserve(
            owner, name="Earmark", amount=900, schedule=schedule, release_on=date(2026, 2, 15), bank=bank
        )
        self.assertEqual((bank.balance, reserve.balance), (100, 900))
        model.step()
        self.assertEqual((bank.balance, reserve.balance, owner.debt), (0, 900, 100))
        model.step()
        self.assertEqual(snapshots, [0, 900])
        self.assertEqual((reserve.balance, owner.debt), (0, 0))
        assert_conservation(model, monthly=True)
        self.assertEqual(account_residuals(model, 2026, month=2), {})

    def test_failed_funding_leaves_the_bank_ledger_and_schedule_unchanged(self):
        from ..account.cash_reserve import CashReserve

        model, owner, bank, schedule = self._household()
        before = len(model.agents), len(model.ledger)
        with self.assertRaisesRegex(ValueError, "insufficient cash"):
            CashReserve(owner, name="Earmark", amount=1001, schedule=schedule, release_on=date(2026, 2, 15), bank=bank)
        self.assertEqual((len(model.agents), len(model.ledger)), before)
        self.assertEqual((bank.balance, schedule.life_events), (1000, []))

    def test_regular_contribution_rejects_cash_reserve_before_debiting_bank(self):
        from ..account.cash_reserve import CashReserve

        model, owner, bank, schedule = self._household()
        reserve = CashReserve(owner, name="Earmark", amount=100, schedule=schedule, release_on=date(2026, 2, 15))
        ledger_size = len(model.ledger)
        with self.assertRaisesRegex(TypeError, "funded at creation"):
            move_from_bank(owner, reserve, 50, cause="Ordinary contribution")
        self.assertEqual((bank.balance, reserve.balance, len(model.ledger)), (1000, 100, ledger_size))

    def test_spec_round_trip_keeps_cash_earmarked_until_its_dated_release(self):
        data = minimal(
            jobs=[],
            spending={"amount_annual": 0},
            accounts=[
                {"id": "cash", "kind": "bank", "balance": 1000, "interest_pct": 0},
                {"id": "investments", "kind": "brokerage", "growth_pct": 0},
            ],
        )
        data["events"] = [
            {
                "id": "earmark",
                "action": "reserve_cash",
                "at": {"year": 2026, "month": 12, "day": 10},
                "person": "alex",
                "bank": "cash",
                "amount": 900,
                "release_on": "2027-02-15",
            },
            {
                "id": "contribution",
                "action": "contribute",
                "at": {"year": 2027, "month": 2, "day": 15},
                "person": "alex",
                "account": "investments",
                "amount": 1000,
            },
        ]
        spec = spec_of(data)
        formats: tuple[Format, ...] = ("json", "yaml")
        for fmt in formats:
            self.assertEqual(loads(dumps(spec, fmt), fmt), spec)
        built = build_household(spec, config=fixture_config(), check=False, collect="month")
        built.model.step_year()
        self.assertEqual((built.items["cash"].balance, built.items["earmark"].balance), (100, 900))
        self.assertEqual(account_residuals(built.model, 2026, month=12), {})
        snapshot = spec_from_model(built.model)
        for fmt in formats:
            with self.subTest(format=fmt):
                rebuilt = build_household(loads(dumps(snapshot, fmt), fmt), config=fixture_config(), collect="month")
                rebuilt.model.step()
                self.assertEqual(rebuilt.items["investments"].balance, 0)
                rebuilt.model.step()
                self.assertEqual((rebuilt.items["earmark"].balance, rebuilt.items["investments"].balance), (0, 1000))
                assert_conservation(rebuilt.model, monthly=True)
                self.assertEqual(account_residuals(rebuilt.model, 2027, month=2), {})
