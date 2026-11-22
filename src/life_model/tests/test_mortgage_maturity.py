# Copyright 2026 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

import unittest
from datetime import date

from pydantic import ValidationError

from ..checks import assert_conservation
from ..housing.home import MortgagePayoffShortfall
from ..model import EventKind
from ..spec import build_household, spec_from_model, validate_spec
from .spec_helpers import fixture_config, minimal, spec_of


def maturity_inputs(sale_day=20):
    inputs = minimal(
        jobs=[],
        spending={"amount_annual": 0, "growth_pct": 2},
        accounts=[
            {"kind": "bank", "id": "cash", "primary": True, "balance": 32000, "interest_pct": 0},
            {"kind": "bank", "id": "reserve", "reserve": True, "balance": 80000, "interest_pct": 0},
        ],
        housing=[
            {
                "kind": "home",
                "id": "home",
                "purchase_price": 400000,
                "value_growth_pct": 0,
                "expenses": {},
                "pmi": False,
                "mortgage": {"original_amount": 75000, "term_years": 25, "rate_pct": 4},
            },
            {
                "kind": "home",
                "id": "other-home",
                "purchase_price": 60000,
                "value_growth_pct": 0,
                "expenses": {},
                "pmi": False,
            },
        ],
    )
    inputs["events"] = [
        {
            "id": "01-sale",
            "action": "sell_home",
            "person": "alex",
            "home": "other-home",
            "selling_cost_pct": 0,
            "at": {"year": 2026, "month": 1, "day": sale_day},
        },
        {
            "id": "02-payoff",
            "action": "pay_mortgage_at_maturity",
            "person": "alex",
            "home": "home",
            "at": {"year": 2026, "month": 1, "day": 28},
            "interest_due": 500,
            "cash_to_keep": 10000,
        },
    ]
    return inputs


class TestMortgageMaturity(unittest.TestCase):
    def test_earlier_or_same_day_proceeds_pay_principal_and_interest_once(self):
        for sale_day in (20, 28):
            with self.subTest(sale_day=sale_day):
                built = build_household(spec_of(maturity_inputs(sale_day)), config=fixture_config(), collect="month")
                loan = built.items["home"].mortgage
                built.model.step()
                self.assertEqual(loan.principal, 0)
                self.assertEqual(loan.interest_paid_this_year, 500)
                self.assertEqual(loan.stat_principal_payment_history, [75000])
                self.assertEqual(loan.stat_interest_payment_history, [500])
                self.assertEqual(loan.person.stat_interest_paid, 500)
                self.assertEqual(loan.person.stat_housing_costs, 75500)
                self.assertEqual(built.items["cash"].balance, 16500)
                self.assertEqual(built.items["reserve"].balance, 80000)
                (payoff,) = built.model.event_log.of_kind(EventKind.MORTGAGE_PAID_AT_MATURITY)
                self.assertEqual((payoff.amount, payoff.details["date"]), (75500, "2026-01-28"))
                built.model.step()
                self.assertEqual(loan.stat_principal_payment_history, [75000])
                self.assertEqual(built.items["cash"].balance, 16500)
                assert_conservation(built.model, monthly=True)
                snapshot = spec_from_model(built.model)
                resumed = build_household(snapshot, config=fixture_config())
                self.assertEqual(resumed.items["home"].mortgage.principal, 0)
                self.assertFalse(snapshot.events)

    def test_proceeds_after_the_due_date_leave_a_shortfall_without_partial_payment(self):
        built = build_household(spec_of(maturity_inputs(31)), config=fixture_config(), collect="month")
        loan = built.items["home"].mortgage
        with self.assertRaises(MortgagePayoffShortfall) as raised:
            built.model.step()
        failure = raised.exception
        self.assertEqual((failure.when, failure.amount, failure.bank_cash), (date(2026, 1, 28), 53500, 32000))
        self.assertEqual((failure.payoff, failure.cash_to_keep), (75500, 10000))
        self.assertEqual(built.items["cash"].balance, 32000)
        self.assertEqual(built.items["reserve"].balance, 80000)
        self.assertEqual(loan.principal, 75000)
        self.assertEqual(loan.stat_principal_payment_history, [])
        self.assertFalse(built.model.ledger.to_records())

    def test_payoff_requires_a_full_date_and_an_owned_home_with_a_mortgage(self):
        inputs = maturity_inputs()
        del inputs["events"][1]["at"]["day"]
        with self.assertRaisesRegex(ValidationError, "full calendar date"):
            spec_of(inputs)
        inputs = maturity_inputs()
        inputs["events"][1]["home"] = "other-home"
        findings = validate_spec(spec_of(inputs), config=fixture_config())
        self.assertTrue(any(f.level == "error" and f.path == "events[1].home" for f in findings))
