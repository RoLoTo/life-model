# Copyright 2026 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

import unittest
from datetime import date

from ..checks import assert_conservation
from ..spec import build_household, spec_from_model, validate_spec
from .spec_helpers import fixture_config, spec_of
from .test_mortgage_maturity import maturity_inputs


def bridge_inputs(*, receipt_month=1, receipt_day=31, limit=60000):
    inputs = maturity_inputs(receipt_day)
    inputs["meta"]["end_year"] = 2026
    inputs["people"][0]["debts"] = [
        {"kind": "bridge_line", "id": "line", "home": "home", "credit_limit": limit, "rate_pct": 5.2},
    ]
    receipt, payoff = inputs["events"]
    receipt["id"] = "03-receipt"
    receipt["at"]["month"] = receipt_month
    inputs["events"] = [
        receipt,
        payoff,
        {
            "action": "draw_bridge",
            "id": "01-draw",
            "person": "alex",
            "bridge": "line",
            "at": {"year": 2026, "month": 1, "day": 28},
            "amount": 53500,
        },
        {
            "action": "repay_bridge",
            "id": "04-repay",
            "person": "alex",
            "bridge": "line",
            "at": {"year": 2026, "month": receipt_month, "day": receipt_day},
            "cash_to_keep": 10000,
        },
    ]
    return inputs


class TestDatedBridge(unittest.TestCase):
    def build(self, inputs):
        return build_household(spec_of(inputs), config=fixture_config(), collect="month")

    def test_explicit_draw_and_repayment_in_one_month(self):
        built = self.build(bridge_inputs())
        built.model.step()
        line = built.items["line"]
        self.assertEqual(line.balance, 0)
        # 53,500 * 5.2% * 3 / 365, rounded at payoff.
        self.assertAlmostEqual(line.interest_paid_this_year, 22.87, places=2)
        self.assertAlmostEqual(built.items["cash"].balance, 16477.13, places=2)
        self.assertEqual(built.items["reserve"].balance, 80000)
        self.assertEqual(built.items["home"].mortgage.principal, 0)
        ledger = built.model.ledger.to_records()
        self.assertEqual([entry["amount"] for entry in ledger if entry["kind"] == "borrow"], [53500])
        self.assertEqual([entry["amount"] for entry in ledger if entry["cause"].startswith("Repay bridge")], [53522.87])
        assert_conservation(built.model, monthly=True)

    def test_accrual_carries_across_months_and_a_snapshot(self):
        built = self.build(bridge_inputs(receipt_month=2, receipt_day=5))
        built.model.step()
        line = built.items["line"]
        self.assertAlmostEqual(line.balance, 53530.49, places=2)
        self.assertEqual(line.get_available_credit(), 6500)
        snapshot = spec_from_model(built.model)
        self.assertEqual(snapshot.people[0].debts[0].accrued_through, date(2026, 2, 1))
        resumed = build_household(snapshot, config=fixture_config(), collect="month")
        for current in (built, resumed):
            current.model.step()
            self.assertEqual(current.items["line"].balance, 0)
            self.assertAlmostEqual(current.items["line"].interest_paid_this_year, 60.98, places=2)
            self.assertAlmostEqual(current.items["cash"].balance, 16439.02, places=2)
            assert_conservation(current.model, monthly=True)

    def test_accrued_interest_does_not_consume_credit_headroom(self):
        inputs = bridge_inputs()
        inputs["events"].append(
            {
                "action": "draw_bridge",
                "person": "alex",
                "bridge": "line",
                "at": {"year": 2026, "month": 1, "day": 30},
                "amount": 6500,
            }
        )
        built = self.build(inputs)
        built.model.step()
        line = built.items["line"]
        self.assertEqual(line.stat_principal_payment_history, [60000])
        # The original draw accrues three days and the final $6,500 accrues one day.
        self.assertEqual(line.stat_interest_payment_history, [23.79])
        self.assertAlmostEqual(built.items["cash"].balance, 16476.21, places=2)
        assert_conservation(built.model, monthly=True)

    def test_draw_above_limit_and_early_repayment_fail_without_posting(self):
        built = self.build(bridge_inputs(limit=53499.99))
        with self.assertRaisesRegex(ValueError, "approved credit"):
            built.model.step()
        self.assertEqual(built.items["line"].balance, 0)
        self.assertEqual(built.items["cash"].balance, 32000)
        self.assertFalse(built.model.ledger.to_records())

        inputs = bridge_inputs()
        inputs["events"][-1]["at"]["day"] = 30
        built = self.build(inputs)
        from ..debt.bridge import BridgeRepaymentShortfall

        with self.assertRaises(BridgeRepaymentShortfall) as failure:
            built.model.step()
        self.assertAlmostEqual(float(failure.exception.payoff), 53515.24, places=2)
        self.assertEqual(built.items["line"].balance, 53500)
        self.assertEqual(built.items["cash"].balance, 10000)
        self.assertEqual(built.items["reserve"].balance, 80000)

    def test_collateral_sale_retires_the_line_at_the_sale_date(self):
        inputs = bridge_inputs()
        inputs["events"][0]["home"] = "home"
        inputs["events"][-1]["at"] = {"year": 2026, "month": 2, "day": 1}
        built = self.build(inputs)
        built.model.step()
        line = built.items["line"]
        self.assertEqual((line.balance, line.credit_limit), (0, 0))
        self.assertAlmostEqual(line.interest_paid_this_year, 22.87, places=2)
        self.assertAlmostEqual(built.items["cash"].balance, 356477.13, places=2)
        snapshot = spec_from_model(built.model)
        self.assertEqual(snapshot.people[0].debts, [])
        self.assertFalse(snapshot.events)
        assert_conservation(built.model, monthly=True)

    def test_bridge_must_be_owned_and_opening_accrual_cannot_be_future(self):
        inputs = bridge_inputs()
        inputs["people"][0]["debts"][0].update(drawn_principal=1000, accrued_through="2026-02-01")
        inputs["events"][-1]["bridge"] = "missing"
        findings = validate_spec(spec_of(inputs), config=fixture_config())
        self.assertTrue(any(f.level == "error" and f.path.endswith(".accrued_through") for f in findings))
        self.assertTrue(any(f.level == "error" and f.path == "events[3].bridge" for f in findings))
