# Copyright 2026 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

import unittest

from pydantic import ValidationError

from ..checks import assert_conservation
from ..model import EventKind
from ..spec import build_household, spec_from_model, validate_spec
from .spec_helpers import fixture_config, minimal, spec_of


def prepayment_inputs(**prepayment):
    inputs = minimal(
        jobs=[],
        spending={"amount_annual": 6000, "growth_pct": 2},
        accounts=[
            {"kind": "bank", "id": "bank", "primary": True, "balance": 5000, "interest_pct": 0},
            {"kind": "bank", "id": "reserve", "reserve": True, "balance": 20000, "interest_pct": 0},
        ],
        housing=[
            {
                "kind": "home",
                "id": "home",
                "purchase_price": 200000,
                "value_growth_pct": 0,
                "expenses": {},
                "pmi": False,
                "mortgage": {"original_amount": 12000, "term_months": 12, "rate_pct": 0},
            },
            {
                "kind": "home",
                "id": "other-home",
                "purchase_price": 6000,
                "value_growth_pct": 0,
                "expenses": {},
                "pmi": False,
            },
        ],
    )
    inputs["events"] = [
        {
            "action": "sell_home",
            "person": "alex",
            "home": "other-home",
            "selling_cost_pct": 0,
            "at": {"year": 2026, "month": 1, "day": 31},
        },
        {
            "action": "prepay_mortgage",
            "person": "alex",
            "home": "home",
            "at": {"year": 2026, "month": 1, "day": 31},
            "cash_fraction": 0.5,
            "cash_to_keep": 3000,
            **prepayment,
        },
    ]
    return inputs


class TestMortgagePrepayment(unittest.TestCase):
    def test_prepayment_uses_settled_cash_after_bills_and_reopens_with_same_terms(self):
        inputs = prepayment_inputs()
        later = prepayment_inputs(at={"year": 2026, "month": 3, "day": 31})["events"][1]
        inputs["events"].append(later)
        built = build_household(spec_of(inputs), config=fixture_config(), collect="month")
        mortgage = built.items["home"].mortgage
        built.model.step()
        # Cash after sale, regular mortgage payment and spending: 5000 + 6000 - 1000 - 500.
        # Prepay half the cash above the 3000 floor: (9500 - 3000) / 2 = 3250.
        self.assertEqual(mortgage.principal, 7750)
        self.assertEqual(mortgage.payments_made, 1)
        self.assertEqual(mortgage.stat_principal_payment_history, [4250])
        self.assertEqual(mortgage.monthly_payment, 1000)
        self.assertEqual(built.items["bank"].balance, 6250)
        self.assertEqual(built.items["reserve"].balance, 20000)
        self.assertEqual(mortgage.person.stat_housing_costs, 4250)
        (prepayment,) = built.model.event_log.of_kind(EventKind.MORTGAGE_PREPAYMENT)
        self.assertEqual((prepayment.amount, prepayment.details["cash_before"]), (3250, 9500))
        self.assertEqual(prepayment.details["date"], "2026-01-31")
        records = built.model.ledger.to_records()
        principal_entries = [record for record in records if record["to_name"] == prepayment.account_name]
        self.assertEqual([record["amount"] for record in principal_entries], [1000, 3250])
        self.assertEqual(principal_entries[-1]["stage"], "post_step")

        snapshot = spec_from_model(built.model)
        self.assertEqual([(event.at.month, event.at.day) for event in snapshot.events], [(3, 31)])
        resumed = build_household(snapshot, config=fixture_config(), collect="month")
        for run in (built, resumed):
            run.model.step()
            self.assertEqual(run.items["home"].mortgage.principal, 6750)
            self.assertEqual(run.items["bank"].balance, 4750)
            run.model.step()
            self.assertEqual(run.items["home"].mortgage.principal, 5625)
            self.assertEqual(run.items["bank"].balance, 3125)
            assert_conservation(run.model, monthly=True)

    def test_floor_and_outstanding_principal_bound_the_payment(self):
        for wanted_floor, opening_cash, expected in (
            (10000, 5000, 0),
            (3000, 30000, 11000),
            (3000, 5000.01, 3250),
        ):
            with self.subTest(wanted_floor=wanted_floor, opening_cash=opening_cash):
                inputs = prepayment_inputs(cash_to_keep=wanted_floor, cash_fraction=0.5 if opening_cash < 6000 else 1)
                inputs["people"][0]["accounts"][0]["balance"] = opening_cash
                built = build_household(spec_of(inputs), config=fixture_config(), collect="month")
                built.model.step()
                (payment,) = built.model.event_log.of_kind(EventKind.MORTGAGE_PREPAYMENT)
                self.assertEqual(payment.amount, expected)
                self.assertEqual(built.items["home"].mortgage.principal, 11000 - expected)
                self.assertEqual(built.items["reserve"].balance, 20000)
                assert_conservation(built.model, monthly=True)

    def test_scheduled_maturity_payoff_runs_before_surplus_prepayment(self):
        inputs = prepayment_inputs()
        inputs["people"][0]["accounts"][0]["balance"] = 25000
        inputs["events"].append(
            {
                "action": "pay_mortgage_at_maturity",
                "person": "alex",
                "home": "home",
                "at": {"year": 2026, "month": 1, "day": 20},
                "interest_due": 100,
            }
        )
        built = build_household(spec_of(inputs), config=fixture_config(), collect="month")
        built.model.step()
        self.assertEqual(built.items["home"].mortgage.principal, 0)
        self.assertEqual(built.items["bank"].balance, 18400)
        self.assertEqual(built.items["reserve"].balance, 20000)
        self.assertFalse(built.model.event_log.of_kind(EventKind.MORTGAGE_PREPAYMENT))
        assert_conservation(built.model, monthly=True)

    def test_prepayment_requires_month_end_and_an_owned_home_with_a_mortgage(self):
        with self.assertRaisesRegex(ValidationError, "last day"):
            spec_of(prepayment_inputs(at={"year": 2026, "month": 2, "day": 27}))
        inputs = prepayment_inputs(home="other-home")
        findings = validate_spec(spec_of(inputs), config=fixture_config())
        self.assertTrue(any(f.level == "error" and f.path == "events[1].home" for f in findings))
