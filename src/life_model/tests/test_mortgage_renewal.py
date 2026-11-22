# Copyright 2026 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

import unittest
from decimal import Decimal

from pydantic import ValidationError

from ..checks import assert_conservation
from ..model import EventKind
from ..spec import build_household, spec_from_model, validate_spec
from .spec_helpers import fixture_config, minimal, spec_of


def renewal_inputs(**overrides):
    data = minimal(
        jobs=[],
        spending={"amount_annual": 0, "growth_pct": 2},
        accounts=[{"kind": "bank", "balance": 40000, "interest_pct": 0}],
        housing=[
            {
                "kind": "home",
                "id": "home",
                "purchase_price": 250000,
                "pmi": False,
                "expenses": {"property_tax_pct": 0, "insurance_pct": 0},
                "mortgage": {"original_amount": 120000, "term_months": 120, "rate_pct": 0},
            }
        ],
    )
    data["events"] = [
        {
            "action": "renew_mortgage",
            "person": "alex",
            "home": "home",
            "at": {"year": 2026, "month": 7, "day": 1},
            "rate_pct": 6,
            "rate_compounding": "semiannual",
            **overrides,
        }
    ]
    return data


class TestMortgageRenewal(unittest.TestCase):
    def test_renewal_reprices_the_july_payment_and_survives_resume(self):
        inputs = renewal_inputs()
        # A second renewal remains pending when the first one is captured in a snapshot.
        inputs["events"].extend(
            renewal_inputs(at="2026-09", rate_pct=0, rate_compounding="monthly", amortization_months=72)["events"]
        )
        built = build_household(spec_of(inputs), config=fixture_config(), collect="month")
        loan = built.items["home"].mortgage
        for _ in range(6):
            built.model.step()
        self.assertEqual(loan.principal, 114000)
        self.assertEqual(loan.person.usable_bank_balance, 34000)
        self.assertEqual(loan.remaining_months, 114)
        # A repricing must not reclassify any of the original principal.
        loan.acquisition_cap, loan.grandfathered_cap = 100000, 80000

        built.model.step()
        rate = (1 + Decimal("0.06") / 2) ** (Decimal(1) / 6) - 1
        payment = 114000 * rate / (1 - (1 + rate) ** -114)
        july_balance = 114000 * (1 + rate) - payment
        self.assertAlmostEqual(loan.monthly_payment, float(payment), places=8)
        self.assertAlmostEqual(loan.interest_paid_this_year, float(114000 * rate), places=8)
        self.assertAlmostEqual(loan.principal, float(july_balance), places=8)
        self.assertAlmostEqual(loan.person.usable_bank_balance, float(34000 - payment), places=8)
        self.assertEqual(loan.remaining_months, 113)
        self.assertEqual((loan.acquisition_cap, loan.grandfathered_cap), (100000, 80000))
        (renewal,) = built.model.event_log.of_kind(EventKind.MORTGAGE_RENEWED)
        self.assertEqual((renewal.year, renewal.month, renewal.amount), (2026, 7, 114000))
        self.assertEqual(renewal.details["date"], "2026-07-01")
        self.assertFalse([row for row in built.model.ledger.to_records() if row["kind"] == "borrow"])

        snapshot = spec_from_model(built.model)
        statement = snapshot.people[0].housing[0].mortgage
        self.assertEqual((statement.rate_pct, statement.rate_compounding), (6, "semiannual"))
        self.assertEqual(len(snapshot.events), 1)
        resumed = build_household(snapshot, config=fixture_config(), collect="month")
        august_balance = july_balance * (1 + rate) - payment
        for run in (built, resumed):
            loan = run.items["home"].mortgage
            run.model.step()
            self.assertAlmostEqual(loan.principal, float(august_balance), places=8)
            run.model.step()  # An explicit 72-month amortization starts with September's payment.
            self.assertEqual(loan.remaining_months, 71)
            self.assertAlmostEqual(loan.monthly_payment, float(august_balance / 72), places=8)
            self.assertAlmostEqual(loan.principal, float(august_balance * 71 / 72), places=8)
            self.assertAlmostEqual(
                loan.person.usable_bank_balance, float(34000 - 2 * payment - august_balance / 72), places=8
            )
            assert_conservation(run.model, monthly=True)

    def test_renewal_requires_a_calendar_month_and_cannot_be_midmonth(self):
        for when in (2026, {"age": 40, "month": 7}, {"year": 2026, "month": 7, "day": 2}):
            with self.subTest(when=when), self.assertRaises(ValidationError):
                spec_of(renewal_inputs(at=when))

    def test_renewal_requires_the_borrowers_home_with_a_mortgage(self):
        without_mortgage = renewal_inputs()
        del without_mortgage["people"][0]["housing"][0]["mortgage"]
        wrong_owner = renewal_inputs(person="jane")
        wrong_owner["people"].append({"id": "jane", "name": "Jane", "age": 45, "retirement_age": 65})
        for data in (renewal_inputs(home="missing"), without_mortgage, wrong_owner):
            with self.subTest(data=data):
                findings = validate_spec(spec_of(data), config=fixture_config())
                self.assertTrue(any(f.level == "error" and f.path == "events[0].home" for f in findings))

    def test_a_paid_off_mortgage_is_not_reopened(self):
        inputs = renewal_inputs()
        inputs["people"][0]["housing"][0]["mortgage"].update(original_amount=1000, term_months=1)
        built = build_household(spec_of(inputs), config=fixture_config(), collect="month")
        for _ in range(7):
            built.model.step()
        self.assertEqual(built.items["home"].mortgage.principal, 0)
        self.assertEqual(built.items["home"].person.usable_bank_balance, 39000)
        self.assertFalse(built.model.event_log.of_kind(EventKind.MORTGAGE_RENEWED))
        assert_conservation(built.model, monthly=True)
