# Copyright 2026 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

import unittest

from ..account.bank import BankAccount
from ..checks import account_residuals, assert_conservation
from ..housing.home import Home, HomeExpenses
from ..spec import build_household
from ..spec.io import Format, dumps, loads
from .spec_helpers import fixture_config, minimal, spec_of


class TestCashFundedClosing(unittest.TestCase):
    def _spec(self, receipt_day):
        data = minimal(
            jobs=[],
            spending={"amount_annual": 0},
            accounts=[
                {"kind": "bank", "id": "bank", "balance": 120000, "interest_pct": 0},
                {"kind": "brokerage", "id": "investments", "balance": 100000, "growth_pct": 0},
            ],
        )
        data["events"] = [
            {
                "id": "a_receipt",
                "at": {"year": 2026, "month": 1, "day": receipt_day},
                "action": "add_account",
                "person": "alex",
                "account": {"kind": "bank", "id": "receipt_cash", "balance": 60000, "interest_pct": 0},
            },
            {
                "id": "b_closing",
                "at": {"year": 2026, "month": 1, "day": 28},
                "person": "alex",
                "action": "buy_home",
                "home": {
                    "kind": "home",
                    "id": "new_home",
                    "purchase_price": 500000,
                    "expenses": {"property_tax_pct": 0, "insurance_pct": 0},
                },
                "cash_funding": {
                    "closing_costs": 20000,
                    "cash_floor": 30000,
                    "minimum_down_payment": 100000,
                    "mortgage_limit": 400000,
                    "term_years": 25,
                    "rate_pct": 0,
                },
            },
        ]
        return spec_of(data)

    def _household(self, receipt_day):
        return build_household(self._spec(receipt_day), config=fixture_config(), collect="month", check=False)

    def test_closing_terms_round_trip_through_json_and_yaml(self):
        spec = self._spec(27)
        formats: tuple[Format, ...] = ("json", "yaml")
        for fmt in formats:
            with self.subTest(format=fmt):
                self.assertEqual(loads(dumps(spec, fmt), fmt), spec)

    def test_closing_uses_cash_that_arrived_before_the_closing_date(self):
        built = self._household(27)
        built.model.step()
        owner = built.people["alex"]
        home = built.items["new_home"]
        self.assertEqual(home.down_payment, 130000)
        self.assertEqual(home.mortgage.loan_amount, 370000)
        self.assertAlmostEqual(home.mortgage.principal, 370000 - 370000 / 300, places=6)
        self.assertAlmostEqual(owner.usable_bank_balance, 30000 - 370000 / 300, places=6)
        self.assertEqual((owner.debt, built.items["investments"].balance), (0, 100000))
        assert_conservation(built.model, monthly=True)
        self.assertEqual(account_residuals(built.model, 2026, month=1), {})

    def test_late_cash_reports_shortfall_before_creating_property_or_debt(self):
        from ..housing.closing import ClosingShortfall

        built = self._household(31)
        model = built.model
        owner = built.people["alex"]
        before = (len(model.agents), len(model.ledger))
        with self.assertRaises(ClosingShortfall) as failure:
            model.step()
        self.assertEqual(failure.exception.amount, 30000)
        self.assertEqual((len(model.agents), len(model.ledger)), before)
        self.assertEqual(
            (built.items["bank"].balance, built.items["investments"].balance, owner.debt), (120000, 100000, 0)
        )
        self.assertEqual(owner.homes, [])

    def test_mortgage_cap_sets_required_cash_even_when_minimum_down_is_lower(self):
        from ..housing.closing import ClosingShortfall, cash_funded_down_payment

        built = self._household(27)
        with self.assertRaises(ClosingShortfall) as failure:
            cash_funded_down_payment(
                built.people["alex"],
                price=500000,
                closing_costs=5000,
                cash_floor=0,
                minimum_down_payment=50000,
                mortgage_limit=375000,
            )
        self.assertEqual(failure.exception.amount, 10000)

    def test_cash_only_purchase_can_use_the_exact_total_of_multiple_bank_balances(self):
        data = minimal(jobs=[], spending={"amount_annual": 0}, accounts=[])
        built = build_household(spec_of(data), config=fixture_config(), check=False)
        owner = built.people["alex"]
        first = BankAccount(owner, balance=0.10, interest_pct=0)
        second = BankAccount(owner, balance=1002.26, interest_pct=0)
        home = Home(
            owner,
            name="Home",
            purchase_price=1000.13,
            value_growth_pct=0,
            down_payment=1000.13,
            mortgage=None,
            expenses=HomeExpenses(built.model),
            purchase=True,
            closing_cost_percent=100 * 2.23 / 1000.13,
            cash_only=True,
        )
        self.assertEqual((first.balance, second.balance, owner.debt, home.home_value), (0, 0, 0, 1000.13))
        self.assertAlmostEqual(sum(row.amount for row in built.model.ledger if row.sink == "closing costs"), 2.23)
