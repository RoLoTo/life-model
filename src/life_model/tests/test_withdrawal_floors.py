# Copyright 2026 Spencer Williams
#
# Use of this source code is governed by an MIT license:
# https://github.com/sw23/life-model/blob/main/LICENSE

"""Per-source balance floors in the household withdrawal waterfall."""

import math
import unittest
from pathlib import Path

from ..account.bank import BankAccount
from ..account.brokerage import BrokerageAccount
from ..account.fixed_income import FixedIncomeHolding
from ..account.roth_IRA import RothIRA
from ..account.traditional_IRA import TraditionalIRA
from ..api import load_spec, validate
from ..checks import account_residuals, assert_conservation
from ..config.financial_config import FinancialConfig
from ..lifeevents import LifeEvent, LifeEvents
from ..model import EventKind, LifeModel
from ..people.family import Family
from ..people.person import MortalityMode, Person, Spending
from ..people.tax_unit import TaxUnit, validate_withdrawal_floors
from ..spec import build_model, canonical, spec_from_model
from ..spec.io import dumps, loads
from ..work.job import Job, Salary


def _config() -> FinancialConfig:
    return FinancialConfig(config_file=str(Path(__file__).parent / "fixtures" / "test_config.yaml"))


class TestWithdrawalFloorValidation(unittest.TestCase):
    def test_validates_keys_and_values(self):
        self.assertEqual(validate_withdrawal_floors(None), {})
        self.assertEqual(validate_withdrawal_floors({}), {})
        self.assertEqual(validate_withdrawal_floors({"brokerage": 0, "pretax": 25_000}), {"pretax": 25_000.0})
        self.assertEqual(
            list(validate_withdrawal_floors({"roth_earnings": 5_000, "brokerage": 10_000}).keys()),
            ["brokerage", "roth_earnings"],
        )

        for bad in (
            {"bank": 1_000},
            {"checking": 1_000},
            {"brokerage": -1},
            {"brokerage": math.nan},
            {"brokerage": math.inf},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_withdrawal_floors(bad)  # type: ignore[arg-type]

        for wrong_type in ({"brokerage": True}, ["brokerage"]):
            with self.subTest(wrong_type=wrong_type), self.assertRaises(TypeError):
                validate_withdrawal_floors(wrong_type)  # type: ignore[arg-type]

    def test_family_and_tax_unit_validate_floors(self):
        model = LifeModel(start_year=2026, end_year=2026, config=_config())
        with self.assertRaises(ValueError):
            Family(model, withdrawal_floors={"bank": 500})
        family = Family(model, withdrawal_floors={"brokerage": 10_000})
        person = Person(family, "Pat", 62, 60, Spending(model, base_annual=0))
        self.assertEqual(family.withdrawal_floors, {"brokerage": 10_000.0})
        self.assertEqual(TaxUnit.for_member(person).withdrawal_floors, {"brokerage": 10_000.0})


class TestWithdrawalFloorsInSettlement(unittest.TestCase):
    def test_monthly_bills_stop_at_floors_and_spill_to_next_source(self):
        model = LifeModel(start_year=2026, end_year=2026, config=_config(), collect="month")
        family = Family(
            model,
            withdrawal_floors={"fixed_income": 4_000, "brokerage": 10_000, "pretax": 8_000, "reserve": 3_000},
        )
        person = Person(family, "Pat", 62, 60, Spending(model, base_annual=120_000))
        BankAccount(owner=person, company="Bank", balance=0, interest_pct=0)
        fixed = FixedIncomeHolding(
            owner=person, name="Treasury", kind="treasury", face=5_000, rate_pct=0, reserve=False
        )
        broker = BrokerageAccount(owner=person, company="Broker", balance=12_000, growth_pct=0, dividend_yield_pct=0)
        ira = TraditionalIRA(owner=person, balance=10_000, growth_pct=0)
        roth = RothIRA(owner=person, balance=10_000, growth_pct=0)
        reserve = BankAccount(owner=person, company="Reserve", balance=4_000, interest_pct=0, reserve=True)

        # Month 1 needs $10,000: $1k fixed_income + $2k brokerage + $2k pretax + $5k roth_earnings.
        model.step()

        self.assertAlmostEqual(fixed.balance, 4_000, places=2)
        self.assertAlmostEqual(broker.balance, 10_000, places=2)
        self.assertAlmostEqual(ira.balance, 8_000, places=2)
        self.assertAlmostEqual(roth.balance, 5_000, places=2)
        self.assertAlmostEqual(reserve.balance, 4_000, places=2)
        self.assertEqual(person.debt, 0)
        self.assertEqual(account_residuals(model, 2026, month=1), {})
        assert_conservation(model, monthly=True)

    def test_all_sources_at_floors_carry_shortfall_as_debt(self):
        model = LifeModel(start_year=2026, end_year=2026, config=_config(), collect="month")
        family = Family(model, withdrawal_floors={"brokerage": 10_000, "roth_earnings": 5_000, "reserve": 4_000})
        person = Person(family, "Pat", 62, 60, Spending(model, base_annual=12_000))
        BankAccount(owner=person, company="Bank", balance=200, interest_pct=0)
        broker = BrokerageAccount(owner=person, company="Broker", balance=10_000, growth_pct=0, dividend_yield_pct=0)
        roth = RothIRA(owner=person, balance=5_000, growth_pct=0)
        reserve = BankAccount(owner=person, company="Reserve", balance=4_000, interest_pct=0, reserve=True)

        model.step()

        self.assertAlmostEqual(broker.balance, 10_000, places=2)
        self.assertAlmostEqual(roth.balance, 5_000, places=2)
        self.assertAlmostEqual(reserve.balance, 4_000, places=2)
        self.assertAlmostEqual(person.debt, 800, places=2)
        self.assertTrue(model.event_log.of_kind(EventKind.SHORTFALL_DEBT))
        assert_conservation(model, monthly=True)

    def test_december_tax_settlement_respects_brokerage_and_pretax_floors(self):
        model = LifeModel(start_year=2026, end_year=2026, config=_config(), collect="month")
        family = Family(model, withdrawal_floors={"brokerage": 50_000, "pretax": 40_000})
        person = Person(family, "Pat", 62, 65, Spending(model, base_annual=120_000))
        BankAccount(owner=person, company="Bank", balance=0, interest_pct=0)
        Job(owner=person, company="Acme", role="Engineer", salary=Salary(model, base_annual=120_000))
        broker = BrokerageAccount(owner=person, company="Broker", balance=55_000, growth_pct=0, dividend_yield_pct=0)
        ira = TraditionalIRA(owner=person, balance=45_000, growth_pct=0)
        roth = RothIRA(owner=person, balance=100_000, growth_pct=0)

        model.run()

        # December tax exceeds $10k: brokerage gives $5k, pre-tax gives $5k, Roth covers the rest.
        self.assertAlmostEqual(broker.balance, 50_000, places=2)
        self.assertAlmostEqual(ira.balance, 40_000, places=2)
        self.assertLess(roth.balance, 100_000)
        self.assertEqual(person.debt, 0)
        assert_conservation(model, monthly=True)

    def test_december_tax_settlement_with_all_floors_exhausted_becomes_debt(self):
        model = LifeModel(start_year=2026, end_year=2026, config=_config(), collect="month")
        family = Family(model, withdrawal_floors={"brokerage": 50_000, "pretax": 40_000, "roth_earnings": 30_000})
        person = Person(family, "Pat", 62, 65, Spending(model, base_annual=120_000))
        BankAccount(owner=person, company="Bank", balance=0, interest_pct=0)
        Job(owner=person, company="Acme", role="Engineer", salary=Salary(model, base_annual=120_000))
        broker = BrokerageAccount(owner=person, company="Broker", balance=50_000, growth_pct=0, dividend_yield_pct=0)
        ira = TraditionalIRA(owner=person, balance=40_000, growth_pct=0)
        roth = RothIRA(owner=person, balance=30_000, growth_pct=0)

        model.run()

        self.assertAlmostEqual(broker.balance, 50_000, places=2)
        self.assertAlmostEqual(ira.balance, 40_000, places=2)
        self.assertAlmostEqual(roth.balance, 30_000, places=2)
        self.assertGreater(person.debt, 0)
        assert_conservation(model, monthly=True)

    def test_roth_basis_floor_spills_to_pretax_before_roth_earnings(self):
        model = LifeModel(start_year=2026, end_year=2026, config=_config(), collect="month")
        family = Family(model, withdrawal_floors={"roth_basis": 8_000})
        person = Person(family, "Pat", 50, 65, Spending(model, base_annual=60_000))
        BankAccount(owner=person, company="Bank", balance=0, interest_pct=0)
        roth = RothIRA(owner=person, balance=10_000, contribution_basis=10_000, growth_pct=0)
        ira = TraditionalIRA(owner=person, balance=20_000, growth_pct=0)

        # Month 1 needs $5,000: roth_basis draws $2,000 to hit its $8,000 floor, then pretax pays $3,000.
        model.step()

        self.assertAlmostEqual(roth.balance, 8_000, places=2)
        self.assertAlmostEqual(ira.balance, 17_000, places=2)
        assert_conservation(model, monthly=True)

    def test_statutory_rmd_still_runs_when_pretax_is_at_its_floor(self):
        model = LifeModel(start_year=2026, end_year=2026, config=_config(), collect="month")
        family = Family(model, withdrawal_floors={"pretax": 200_000})
        person = Person(family, "Pat", 75, 65, Spending(model, base_annual=0))
        BankAccount(owner=person, company="Bank", balance=0, interest_pct=0)
        ira = TraditionalIRA(owner=person, balance=100_000, growth_pct=0)

        model.run()

        self.assertLess(ira.balance, 100_000)
        self.assertGreater(person.bank_account_balance, 0)
        assert_conservation(model, monthly=True)

    def test_joint_couple_shares_floor_and_survives_spouse_death(self):
        model = LifeModel(start_year=2026, end_year=2027, config=_config(), collect="month")
        family = Family(model, withdrawal_floors={"brokerage": 20_000})
        alex = Person(
            family,
            "Alex",
            65,
            60,
            Spending(model, base_annual=60_000),
            mortality_mode=MortalityMode.FIXED_AGE,
            death_age=66,
        )
        sam = Person(family, "Sam", 64, 60, Spending(model, base_annual=0))
        alex.get_married(sam)
        BankAccount(owner=alex, company="Bank", balance=0, interest_pct=0)
        BankAccount(owner=sam, company="Bank", balance=0, interest_pct=0)
        b_alex = BrokerageAccount(owner=alex, company="B1", balance=15_000, growth_pct=0, dividend_yield_pct=0)
        b_sam = BrokerageAccount(owner=sam, company="B2", balance=15_000, growth_pct=0, dividend_yield_pct=0)
        roth = RothIRA(owner=sam, balance=200_000, growth_pct=0)

        # Month 1 needs $5k (draws from combined $30k brokerage -> $25k).
        model.step()
        self.assertAlmostEqual(b_alex.balance + b_sam.balance, 25_000, places=2)

        # Run through Alex's death in 2027: combined brokerage stays at its $20k floor.
        model.run()
        self.assertTrue(alex.is_deceased)
        self.assertAlmostEqual(sum(a.balance for a in sam.brokerage_accounts), 20_000, places=2)
        self.assertLess(roth.balance, 200_000)
        assert_conservation(model, monthly=True)

    def test_mid_run_floor_update_is_honored_by_tax_unit(self):
        model = LifeModel(start_year=2026, end_year=2026, config=_config(), collect="month")
        family = Family(model, withdrawal_floors={"brokerage": 10_000})
        person = Person(family, "Pat", 62, 60, Spending(model, base_annual=24_000))
        BankAccount(owner=person, company="Bank", balance=0, interest_pct=0)
        broker = BrokerageAccount(owner=person, company="Broker", balance=12_000, growth_pct=0, dividend_yield_pct=0)
        roth = RothIRA(owner=person, balance=50_000, growth_pct=0)

        def lower_floor():
            family.withdrawal_floors = {"brokerage": 6_000}

        LifeEvents(model, [LifeEvent(2026, "Lower floor", lower_floor, month=3)])
        model.step()  # Jan: $2k from brokerage -> $10k
        self.assertAlmostEqual(broker.balance, 10_000, places=2)
        model.step()  # Feb: brokerage at floor -> $2k from Roth
        self.assertAlmostEqual(broker.balance, 10_000, places=2)
        self.assertAlmostEqual(roth.balance, 48_000, places=2)
        model.step()  # Mar: floor lowered to $6k -> $2k from brokerage -> $8k
        self.assertAlmostEqual(broker.balance, 8_000, places=2)
        assert_conservation(model, monthly=True)


class TestWithdrawalFloorsSpecRoundTrip(unittest.TestCase):
    def _base_spec(self) -> dict:
        return {
            "meta": {
                "name": "Floors test",
                "start_year": 2026,
                "end_year": 2027,
                "seed": 1,
                "accepted_warnings": ["fixed_growth"],
            },
            "people": [
                {
                    "id": "pat",
                    "name": "Pat",
                    "age": 62,
                    "birth": "1964-01",
                    "retirement_age": 60,
                    "spending": {"amount_annual": 24_000, "growth_pct": 0},
                    "accounts": [
                        {"kind": "bank", "id": "bank", "balance": 0, "interest_pct": 0},
                        {
                            "kind": "brokerage",
                            "id": "broker",
                            "balance": 25_000,
                            "growth_pct": 0,
                            "dividend_yield_pct": 0,
                        },
                        {"kind": "roth_ira", "id": "roth", "balance": 50_000, "growth_pct": 0},
                    ],
                }
            ],
            "withdrawal_floors": {"brokerage": 20_000, "roth_earnings": 10_000},
        }

    def test_yaml_and_json_round_trip_and_snapshot_resume(self):
        spec = load_spec(self._base_spec())
        self.assertEqual([f for f in validate(spec) if f.level == "error"], [])
        for fmt in ("yaml", "json"):
            reloaded = loads(dumps(spec, fmt))
            self.assertEqual(dumps(canonical(reloaded), fmt), dumps(canonical(spec), fmt))

        continuous = build_model(spec, seed=1, check=False)
        continuous.run()

        first_year = build_model(
            spec.model_copy(update={"meta": spec.meta.model_copy(update={"end_year": 2026})}),
            seed=1,
            check=False,
        )
        first_year.run()
        snap = spec_from_model(first_year)
        self.assertEqual(snap.withdrawal_floors, {"brokerage": 20_000.0, "roth_earnings": 10_000.0})
        resumed_spec = loads(dumps(snap, "json"))
        resumed = build_model(
            resumed_spec.model_copy(update={"meta": resumed_spec.meta.model_copy(update={"end_year": 2027})}),
            seed=1,
            check=False,
        )
        resumed.run()

        self.assertAlmostEqual(
            continuous.result().model_frame.iloc[-1]["Net Worth"],
            resumed.result().model_frame.iloc[-1]["Net Worth"],
            places=2,
        )
        assert_conservation(continuous)
        assert_conservation(resumed)

    def test_spec_rejects_bank_or_negative_floor(self):
        for bad in ({"bank": 1_000}, {"brokerage": -5}, {"unknown": 100}):
            data = self._base_spec()
            data["withdrawal_floors"] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                load_spec(data)


if __name__ == "__main__":
    unittest.main()
