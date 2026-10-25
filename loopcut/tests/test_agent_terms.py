"""The account's agent terms (config.agent_terms, contract agreed with the gateway 2026-10-24): the manifest's optional
account.agent sets how many workers a turn may start, a worker's and the orchestrator's step limits, the job's worker
dollars, the prices those are counted at (and whether a reply's usage.cost is charged), and the showcase's typical
build. Every field is optional; without one, from
an older server, signed out, on BYOK, on the ChatGPT plan, through a harness stand-in for the gateway or with
LOOPCUT_AGENT_TERMS=off, every limit is 0.2.15's. No Blender.

    python3 -m unittest discover -s loopcut/tests
"""

import dataclasses
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import (agent, chat_credits, cloud, cloud_http, config, orchestrator, pricing, router,  # noqa: E402
                     state, tools, waves)
import platform_contract  # noqa: E402
from prompt_index_fixture import built_index  # noqa: E402
from test_cloud import manifest  # noqa: E402
from test_orchestrator import LIVE, USER, reference_wave  # noqa: E402

# The live manifest's terms for a free GLM account (2026-10-24), and a typical build.
LIVE_GLM = {"max_workers": 2, "worker_max_steps": 24, "orchestrator_max_steps": 36, "job_max_usd": 0.2,
            "prices": {"fast": [0.15, 0.03, 0.5], "pro": [0.435, 0.0036, 0.87]}}
TERMS = {**LIVE_GLM, "typical_build": 30}
PRICES = {"fast": (0.15, 0.03, 0.5), "pro": (0.435, 0.0036, 0.87)}
MIMO = {"fast": (0.435, 0.0036, 0.87)}  # An account whose "fast" a model dearer than GLM serves.
SERVICE = {"LOOPCUT_API_KEY": "k", "LOOPCUT_BASE_URL": config.SERVICE_BASE_URL, "LOOPCUT_MODEL": "pro"}
# Roles' limits as 0.2.15 had them: workers at once, the turn's total, worker dollars, seconds per worker run, whether
# terms apply, the prices.
TODAY = (3, 0, 0.40, 1800, False, ())


def limits(roles: config.Roles) -> tuple:
    return (roles.max_workers, roles.turn_workers, roles.job_max_usd, roles.worker_max_seconds, roles.terms,
            roles.prices)


class TermsTest(unittest.TestCase):
    """agent_terms: each field checked by type and range; a bad one is ignored with a console note, never an error."""

    def setUp(self):
        config._agent_notes.clear()

    def test_every_field_is_read(self):
        self.assertEqual(config.agent_terms({"agent": TERMS}), {**TERMS, "prices": PRICES})

    def test_the_gateways_own_fixture_parses_and_its_terms_are_read(self):
        """The platform contract's manifest fixture (contract/fixtures/manifest/signed_in_billing_trial.json, since
        loopcut-web 22b42da), read where the contract is (platform_contract), never copied here."""
        path = platform_contract.require("fixtures", "manifest", "signed_in_billing_trial.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        with mock.patch("builtins.print") as log:
            account = cloud._manifest(data)["account"]  # The add-on's own manifest check accepts it.
            self.assertEqual(config.agent_terms(account), {**LIVE_GLM, "prices": PRICES})
        log.assert_not_called()
        self.assertEqual(chat_credits.typical_build(account), chat_credits.TYPICAL_BUILD, "it sends none")

    def test_no_agent_object_is_no_terms(self):
        for account in ({}, {"plan": "pro"}, {"agent": None}, {"agent": {}}, [], "pro"):
            with self.subTest(account=account), mock.patch("builtins.print") as log:
                self.assertEqual(config.agent_terms(account), {})
                log.assert_not_called()

    def test_the_range_edges_are_accepted(self):
        for key, (low, high) in config.AGENT_TERMS.items():
            for value in (low, high):
                with self.subTest(key=key, value=value):
                    self.assertEqual(config.agent_terms({"agent": {key: value}}), {key: value})
        self.assertEqual(config.agent_terms({"agent": {"job_max_usd": 0}}), {"job_max_usd": 0}, "0 = no limit")

    def test_bad_values_are_ignored_each_with_one_note(self):
        bad = [("max_workers", 0), ("max_workers", 7), ("max_workers", "2"), ("max_workers", 2.0), ("max_workers", True),
               ("worker_max_steps", 4), ("worker_max_steps", 201), ("worker_max_steps", None),
               ("orchestrator_max_steps", 4), ("orchestrator_max_steps", 201), ("orchestrator_max_steps", [36]),
               ("job_max_usd", -0.01), ("job_max_usd", 100.5), ("job_max_usd", float("nan")), ("job_max_usd", "0.2"),
               ("job_max_usd", float("inf")), ("job_max_usd", False),
               ("typical_build", 0), ("typical_build", 1001), ("typical_build", 12.5), ("typical_build", {})]
        for key, value in bad:
            with self.subTest(key=key, value=value), mock.patch("builtins.print") as log:
                self.assertEqual(config.agent_terms({"agent": {key: value, "max_workers": 2} if key != "max_workers"
                                                     else {key: value}}),
                                 {} if key == "max_workers" else {"max_workers": 2}, "the good fields still count")
                config.agent_terms({"agent": {key: value}})  # load_roles reads them every turn: said once.
                log.assert_called_once()
                self.assertIn(f"account.agent.{key}", log.call_args.args[0])

    def test_bad_price_rows_are_dropped_and_good_ones_kept(self):
        prices = {"fast": [0.4, 0.004, 0.9], "Pro ": [1, 0, 2], "two": [1, 2], "four": [1, 2, 3, 4],
                  "negative": [-1, 0, 0], "text": ["1", 0, 0], "infinite": [1, float("inf"), 0], "bool": [True, 0, 0],
                  "tuple": "1,2,3", "": [1, 1, 1]}
        with mock.patch("builtins.print") as log:
            terms = config.agent_terms({"agent": {"prices": prices}})
        self.assertEqual(terms, {"prices": {"fast": (0.4, 0.004, 0.9), "pro": (1.0, 0.0, 2.0)}})
        self.assertEqual(log.call_count, 8)
        with mock.patch("builtins.print") as log:
            self.assertEqual(config.agent_terms({"agent": {"prices": [0.4, 0.004, 0.9]}}), {})
            self.assertEqual(config.agent_terms({"agent": {"prices": {"pro": [1, 2]}}}), {}, "no valid row, no prices")
        self.assertEqual(log.call_count, 2)

    def test_a_non_object_agent_is_ignored(self):
        with mock.patch("builtins.print") as log:
            self.assertEqual(config.agent_terms({"agent": [2, 24]}), {})
        log.assert_called_once()

    def test_fields_a_later_contract_adds_are_left_alone(self):
        with mock.patch("builtins.print") as log:
            self.assertEqual(config.agent_terms({"agent": {"router_max_steps": 3, "max_workers": 2}}), {"max_workers": 2})
        log.assert_not_called()

    def test_only_a_signed_in_runtime_is_read(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = cloud.Runtime(cloud_http.Client(api_key="lc_test"), folder, signed_in=True)
            runtime.manifest = manifest(account={"plan": "pro", "agent": {"max_workers": 2}})
            with mock.patch.object(cloud, "_runtime", runtime):
                self.assertEqual(config.agent_terms(), {"max_workers": 2})
                runtime.signed_in = False
                self.assertEqual(config.agent_terms(), {})
            with mock.patch.object(cloud, "_runtime", None):
                self.assertEqual(config.agent_terms(), {})


class RolesTest(unittest.TestCase):
    """load_roles: the terms replace the defaults of the limits they name, on Loopcut's own gateway only; an explicit
    setting still wins."""

    def setUp(self):
        config._agent_notes.clear()
        self.empty = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        self.empty.close()
        self.addCleanup(os.unlink, self.empty.name)

    def roles(self, agent=None, account=None, **env) -> config.Roles:
        account = account if account is not None else {"plan": "pro", **({} if agent is None else {"agent": agent})}
        clean = {k: v for k, v in os.environ.items() if not k.startswith("LOOPCUT_")}
        with mock.patch.dict(os.environ, {**clean, "LOOPCUT_ENV_FILE": self.empty.name, **SERVICE, **env}, clear=True), \
                mock.patch.object(config, "_preference_values", return_value={}), \
                mock.patch.object(config, "_manifest_account", return_value=account):
            return config.load_roles()

    def test_without_terms_every_limit_is_todays(self):
        for agent_ in (None, {}):
            with self.subTest(agent=agent_):
                roles = self.roles(agent_)
                self.assertTrue(roles.orchestrate)
                self.assertEqual(limits(roles), TODAY)
                self.assertEqual(roles.orchestrator.max_steps, 0)

    def test_an_older_servers_manifest_changes_nothing(self):
        """No account.agent at all (servers before the contract) builds exactly the Roles no runtime does."""
        older = self.roles(account=manifest()["account"])
        none = self.roles(account={})
        self.assertEqual(older, none)
        self.assertEqual(limits(older), TODAY)

    def test_every_term_is_applied(self):
        roles = self.roles(TERMS)
        self.assertEqual(limits(roles), (2, 2, 0.20, 1800, True, tuple(sorted(PRICES.items()))))
        self.assertEqual((roles.worker_max_steps, roles.orchestrator_max_steps), (24, 36))
        self.assertEqual(roles.orchestrator.max_steps, 0, "the orchestrated loop takes its steps (agent._configure)")
        self.assertEqual(roles.worker.max_steps, 0, "a worker's cap is the job budget's, never its loop's")

    def test_without_step_terms_nothing_is_capped(self):
        for agent_ in (None, {"max_workers": 2}):
            with self.subTest(agent=agent_):
                roles = self.roles(agent_)
                self.assertEqual((roles.worker_max_steps, roles.orchestrator_max_steps), (0, 0))

    def test_an_explicit_step_limit_still_wins(self):
        roles = self.roles(TERMS, LOOPCUT_MAX_STEPS="50")
        self.assertEqual((roles.orchestrator.max_steps, roles.orchestrator_max_steps, roles.worker_max_steps),
                         (50, 0, 24), "no setting names a worker's")
        self.assertEqual(self.roles(TERMS, LOOPCUT_MAX_STEPS="0").orchestrator_max_steps, 0, "0 is explicit too")
        self.assertEqual(limits(self.roles(TERMS, LOOPCUT_AGENT_TERMS="off"))[:2], (3, 0))
        self.assertEqual(self.roles(TERMS, LOOPCUT_AGENT_TERMS="off").worker_max_steps, 0)

    def test_each_term_alone(self):
        for key, value, expected in (("max_workers", 2, {"max_workers": 2, "turn_workers": 2}),
                                     ("max_workers", 6, {"max_workers": 3, "turn_workers": 6}),
                                     ("job_max_usd", 0.2, {"job_max_usd": 0.2}),
                                     ("job_max_usd", 0, {"job_max_usd": 0.0}),
                                     ("prices", {"fast": [0.435, 0.0036, 0.87]},
                                      {"prices": (("fast", (0.435, 0.0036, 0.87)),)}),
                                     ("worker_max_steps", 24, {"worker_max_steps": 24}),
                                     ("orchestrator_max_steps", 36, {"orchestrator_max_steps": 36})):
            with self.subTest(key=key, value=value):
                roles = self.roles({key: value})
                self.assertEqual({k: getattr(roles, k) for k in expected}, expected)
                self.assertTrue(roles.terms, "an account.agent with a valid field: usage.cost counts")
                rest = dict(zip(("max_workers", "turn_workers", "job_max_usd", "worker_max_seconds"), TODAY))
                for name in set(rest) - set(expected):
                    self.assertEqual(getattr(roles, name), rest[name], name)

    def test_at_most_three_run_at_once_whatever_the_turns_total(self):
        for total, at_once in ((1, 1), (2, 2), (3, 3), (4, 3), (6, 3)):
            with self.subTest(total=total):
                roles = self.roles({"max_workers": total})
                self.assertEqual((roles.max_workers, roles.turn_workers), (at_once, total))
        roles = self.roles({"max_workers": 6}, LOOPCUT_MAX_WORKERS="5")
        self.assertEqual((roles.max_workers, roles.turn_workers), (5, 0), "an explicit setting: at once only, as today")

    def test_bad_terms_keep_the_defaults_and_never_fail_the_turn(self):
        bad = {"max_workers": 9, "worker_max_steps": "24", "orchestrator_max_steps": 2, "job_max_usd": -1,
               "prices": {"fast": [1, 2]}, "typical_build": 0}
        with mock.patch("builtins.print"):
            self.assertEqual(limits(self.roles(bad)), TODAY)

    def test_an_explicit_setting_still_wins(self):
        roles = self.roles(TERMS, LOOPCUT_MAX_WORKERS="3", LOOPCUT_JOB_MAX_USD="0.40")
        self.assertEqual((roles.max_workers, roles.turn_workers, roles.job_max_usd), (3, 0, 0.40), "as in 0.2.15")
        self.assertEqual(roles.prices, tuple(sorted(PRICES.items())), "no setting names these")

    def test_off_loopcuts_own_gateway_the_terms_never_apply(self):
        stand_in = "http://127.0.0.1:8765/v1"
        for env in ({"LOOPCUT_BASE_URL": "https://api.example.com/v1"},
                    {"LOOPCUT_BASE_URL": config.CHATGPT_PLAN_BASE_URL},
                    {"LOOPCUT_WORKER_BASE_URL": "https://api.example.com/v1"},
                    {"LOOPCUT_ORCH_BASE_URL": "https://api.example.com/v1"},
                    {"LOOPCUT_BASE_URL": stand_in, "LOOPCUT_SERVICE_URLS": stand_in},
                    {"LOOPCUT_WORKER_BASE_URL": stand_in, "LOOPCUT_SERVICE_URLS": stand_in}):
            with self.subTest(env=env):
                roles = self.roles(TERMS, **env)
                self.assertEqual(limits(roles), TODAY)
        roles = self.roles(TERMS, LOOPCUT_BASE_URL=stand_in, LOOPCUT_SERVICE_URLS=stand_in)
        self.assertTrue(roles.orchestrate, "a harness bridge still runs workers, with today's limits")

    def test_loopcut_agent_terms_off_is_todays_limits(self):
        for value in ("off", "false", "0"):
            with self.subTest(value=value):
                self.assertEqual(limits(self.roles(TERMS, LOOPCUT_AGENT_TERMS=value)), TODAY)
        self.assertEqual(self.roles(TERMS, LOOPCUT_AGENT_TERMS="on").turn_workers, 2)
        with self.assertRaisesRegex(config.ConfigError, "LOOPCUT_AGENT_TERMS must be a boolean"):
            self.roles(TERMS, LOOPCUT_AGENT_TERMS="sometimes")


class TurnTest(unittest.TestCase):
    """agent._configure reads the terms as a turn starts; a manifest refreshed during the turn changes the next."""

    def setUp(self):
        config._agent_notes.clear()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.data = folder.name
        self.runtime = cloud.Runtime(cloud_http.Client(api_key="lc_test"), self.data, signed_in=True)
        self.runtime.manifest = manifest(account={"plan": "pro", "agent": dict(TERMS)}, **built_index())
        for patcher in (mock.patch.object(cloud, "_runtime", self.runtime),
                        mock.patch.object(self.runtime, "refresh", side_effect=AssertionError("network on main")),
                        mock.patch.object(config, "_getter", return_value=(None, lambda k, d="": SERVICE.get(k, d))),
                        mock.patch.object(config, "codex_app", return_value=None),
                        mock.patch.object(agent, "_keep_mode", side_effect=lambda value: value),
                        mock.patch.dict(os.environ, {"LOOPCUT_DATA_DIR": self.data, "LOOPCUT_SERVICE_URLS": ""})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def configure(self) -> agent.Turn:
        turn = agent.Turn()
        agent._configure(turn, state.new_session())
        return turn

    def test_the_orchestrated_turn_takes_the_terms(self):
        turn = self.configure()
        self.assertIs(turn.spec, agent.ORCH_SPEC)
        self.assertEqual(limits(turn.roles), (2, 2, 0.20, 1800, True, tuple(sorted(PRICES.items()))))
        self.assertEqual((turn.config.max_steps, turn.carry_steps), (36, True))
        self.assertEqual(turn.roles.orchestrator.max_steps, 0, "the main config's, for a divert")
        self.assertEqual((turn.roles.worker_max_steps, turn.roles.worker.max_steps), (24, 0))

    def test_the_orchestrators_steps_are_for_the_orchestrated_loop_only(self):
        with self.runtime.lock:
            del self.runtime.manifest["prompts"]  # No prompt index yet: the open single loop.
        turn = self.configure()
        self.assertIs(turn.spec, agent.SINGLE_SPEC)
        self.assertEqual((turn.config.max_steps, turn.carry_steps), (0, False))

    def test_a_refresh_mid_turn_applies_to_the_next_turn(self):
        session = {"id": "c", "turn_number": 1, "jobs": []}
        turn = self.configure()
        with self.runtime.lock:
            self.runtime.manifest["account"]["agent"] = {"max_workers": 4}  # A refresh while the turn runs.
        job = waves.Job(session, turn.roles)  # Its first delegate, after the refresh.
        self.assertEqual((job.max_workers, job.turn_workers, job.budget.max_usd, job.budget.reported),
                         (2, 2, 0.20, True), "this turn keeps what it started with")
        self.assertEqual(job.budget.prices, PRICES)
        self.assertEqual(waves.workers_left(job, turn.roles), 2)
        self.assertEqual(job.budget.max_steps, 24)
        later = self.configure()
        self.assertEqual(limits(later.roles), (3, 4, 0.40, 1800, True, ()))
        self.assertEqual((later.config.max_steps, later.roles.worker_max_steps), (0, 0))

    def test_a_divert_to_the_workspace_takes_the_main_configs_step_limit(self):
        """router.to_workspace mid-turn: the workspace loop runs with the base max_steps, never the orchestrator's."""
        turn = self.configure()
        self.assertEqual(turn.config.max_steps, 36)  # The orchestrator's own, from the terms.
        turn.may_divert = True
        session = state.new_session()

        def divert(session, turn, cfg, is_cancelled, stream=None, divert=None):
            turn.diverted = True
        with mock.patch.object(router, "start_turn", side_effect=divert), \
                mock.patch.object(agent, "_workspace_turn"), \
                mock.patch.object(agent, "_on_main", side_effect=lambda fn, is_cancelled=None, name="": ()):
            agent._route(session, turn)
        self.assertIs(turn.spec, agent.WORKSPACE_SPEC)
        self.assertEqual(turn.config.max_steps, turn.roles.orchestrator.max_steps)
        self.assertEqual((turn.config.max_steps, turn.carry_steps), (0, False))
        self.assertTrue(turn.config.keep_context)


class PricesTest(unittest.TestCase):
    """pricing: the account's prices before the table; a reported cost before either."""

    USAGE = {"input": 1_000_000, "cached": 0, "output": 1_000_000}

    def test_the_accounts_prices_come_before_the_table(self):
        self.assertAlmostEqual(pricing.usd("fast", self.USAGE), 0.65, msg="GLM-5.3-Flash's, today's table")
        self.assertAlmostEqual(pricing.usd("fast", self.USAGE, MIMO), 1.305)
        self.assertAlmostEqual(pricing.usd("FAST", self.USAGE, MIMO), 1.305)
        self.assertAlmostEqual(pricing.usd("fast", {"input": 1_000_000, "cached": 500_000, "output": 0}, MIMO),
                               0.5 * 0.435 + 0.5 * 0.0036)
        self.assertAlmostEqual(pricing.usd("pro", self.USAGE, PRICES), 1.305, msg="the live GLM account's pro row")
        self.assertAlmostEqual(pricing.usd("opus", self.USAGE, MIMO), pricing.usd("opus", self.USAGE),
                               msg="an alias the account does not price keeps the table's")
        self.assertAlmostEqual(pricing.usd("mimo-v3", self.USAGE, {"mimo-v3": (1.0, 0.1, 2.0)}), 3.0)
        self.assertIsNone(pricing.usd("mimo-v3", self.USAGE), "no price anywhere")

    def test_the_tables_long_context_rate_is_its_own(self):
        long = {"input": 200_000, "cached": 0, "output": 0}
        own = {"claude-haiku-5-5": (0.10, 0.01, 0.50)}
        self.assertAlmostEqual(pricing.usd("claude-haiku-5-5", long), 0.10)
        self.assertAlmostEqual(pricing.usd("claude-haiku-5-5", long, own), 0.02)

    def test_a_reported_cost_comes_first(self):
        self.assertEqual(pricing.charged("fast", {**self.USAGE, "cost": 0.0123}, MIMO), 0.0123)
        self.assertEqual(pricing.charged("unpriced", {"input": 5, "cost": 0}), 0.0)
        for bad in (-1, float("nan"), float("inf"), "0.01", True, None):
            with self.subTest(cost=bad):
                self.assertAlmostEqual(pricing.charged("fast", {**self.USAGE, "cost": bad}, MIMO), 1.305)
        self.assertIsNone(pricing.charged("unpriced", self.USAGE))


class JobBudgetTest(unittest.TestCase):
    USAGE = {"input": 100_000, "cached": 0, "output": 100_000}  # GLM's table: $0.065; the dearer row: $0.1305.

    def test_job_max_usd_counts_at_the_accounts_prices(self):
        table = waves.JobBudget(0.20, 1800)
        own = waves.JobBudget(0.20, 1800, prices=MIMO, reported=True)
        for _ in range(2):
            table.add("fast", self.USAGE)
            own.add("fast", self.USAGE)
        self.assertAlmostEqual(table.usd, 0.13)
        self.assertEqual(table.spent(), "", "priced as GLM, the job would go on")
        self.assertAlmostEqual(own.usd, 0.261)
        self.assertEqual(own.spent(), "the job's $0.20 is used")

    def test_with_terms_a_reported_cost_is_charged_instead_of_the_tokens(self):
        budget = waves.JobBudget(0.20, 1800, prices=MIMO, reported=True)
        self.assertEqual(budget.add("fast", {**self.USAGE, "cost": 0.001}), 0.001)
        self.assertAlmostEqual(budget.usd, 0.001)
        self.assertAlmostEqual(budget.add("fast", self.USAGE), 0.1305, msg="no cost: the tokens at its prices")
        unpriced = waves.JobBudget(0.20, 1800, reported=True)
        self.assertFalse(unpriced.priced("mimo-v3"))
        self.assertIsNone(unpriced.add("mimo-v3", self.USAGE))
        self.assertEqual(unpriced.add("mimo-v3", {**self.USAGE, "cost": 0.05}), 0.05)
        self.assertTrue(unpriced.priced("mimo-v3"), "its dollars are known now, so they count in the estimate")
        self.assertTrue(waves.JobBudget(0.2, 1800, prices={"mimo-v3": (1, 0, 1)}).priced("mimo-v3"))

    def test_without_terms_a_reported_cost_changes_nothing(self):
        """A server without account.agent keeps 0.2.15's dollars even if its usage carries a cost."""
        budget = waves.JobBudget(0.40, 1800)
        self.assertAlmostEqual(budget.add("fast", {**self.USAGE, "cost": 0.3}), 0.065)
        self.assertIsNone(budget.add("mimo-v3", {**self.USAGE, "cost": 0.3}))
        self.assertFalse(budget.priced("mimo-v3"))
        self.assertAlmostEqual(budget.usd, 0.065)

    def test_the_worker_step_cap_stops_each_run_at_its_own_count(self):
        budget = waves.JobBudget(0, 1800, clock=lambda: 1, steps=5)
        for n in range(5):
            budget.take_step(0, worker_steps=n)
        with self.assertRaises(agent.BudgetStop) as stop:
            budget.take_step(0, worker_steps=5)
        self.assertEqual(str(stop.exception), "its 5 steps are used")
        self.assertEqual((budget.spent(0, 4), budget.spent(0, 5)), ("", "its 5 steps are used"))
        self.assertEqual(budget.spent(), "", "a queued worker has all its steps still")
        self.assertEqual(budget.spent(0, 5, cap=8), "", "a retry's raised count")
        self.assertEqual(budget.spent(0, 8, cap=8), "its 8 steps are used")
        budget.take_step(0, worker_steps=5, cap=8)
        budget.take_step(0, worker_steps=0)  # Another worker of the job starts at its own zero.

    def test_the_steps_left_estimate_counts_the_cap(self):
        sample = dict(elapsed=21, seconds=1800, job_steps=21, usd=0, max_usd=0, running=1, priced=True)
        self.assertEqual(waves.budget_steps_left(steps=21, max_steps=24, **sample), 3)
        self.assertGreater(waves.budget_steps_left(steps=21, **sample), 3, "no cap: time alone")
        budget = waves.JobBudget(0, 1800, clock=lambda: 21, steps=24)
        self.assertEqual(budget.take_step(0, worker_steps=21), 3)
        self.assertEqual(budget.take_step(0, worker_steps=21, cap=36), 15)

    def test_the_job_follows_the_roles(self):
        session = {"id": "c", "turn_number": 1, "jobs": []}
        with tempfile.TemporaryDirectory() as data, mock.patch.dict(os.environ, {"LOOPCUT_DATA_DIR": data}):
            plain = waves.Job(session, config.Roles(None, None, True))
            hosted = waves.Job(session, config.Roles(None, None, True, terms=True, prices=tuple(MIMO.items())))
        self.assertEqual((plain.budget.reported, plain.budget.prices, plain.budget.max_steps), (False, {}, 0))
        self.assertEqual((hosted.budget.reported, hosted.budget.prices), (True, MIMO))
        with tempfile.TemporaryDirectory() as data, mock.patch.dict(os.environ, {"LOOPCUT_DATA_DIR": data}):
            capped = waves.Job(session, config.Roles(None, None, True, worker_max_steps=24))
        self.assertEqual(capped.budget.max_steps, 24)


class TurnWorkersTest(unittest.TestCase):
    """The account's max_workers is the turn's total across its waves: delegate refuses a wave past what is left,
    neutrally (its own card, ui.layout), and says it before planning (router.note)."""

    def job(self, cap, *states, before=0):
        crew = [types.SimpleNamespace(thread=object() if state in ("running", "finished") else None,
                                      finished=1.0 if state in ("finished", "never started") else None,
                                      reported=state in ("finished", "never started"), brief={"deliverable": f"P{n}"})
                for n, state in enumerate(states)]
        return types.SimpleNamespace(turn_workers=cap, started_before=before, lock=threading.Lock(), crew=crew,
                                     stopped=False)

    def test_no_limit_without_the_term(self):
        self.assertIsNone(waves.workers_left(None, config.Roles))
        self.assertIsNone(waves.workers_left(None, types.SimpleNamespace(max_workers=3)))
        self.assertIsNone(waves.workers_left(self.job(0, "running"), None))

    def test_queued_and_run_workers_count_but_one_stopped_before_starting_does_not(self):
        self.assertEqual(waves.workers_left(None, types.SimpleNamespace(turn_workers=3)), 3, "no wave yet")
        self.assertEqual(waves.workers_left(self.job(3, "finished", "queued", "never started"), None), 1)
        self.assertEqual(waves.workers_left(self.job(2, "finished", "running", "queued"), None), 0)
        self.assertEqual(waves.workers_left(self.job(3, "finished", before=2), None), 0, "before a resume too")

    def delegate(self, roles, job=None, dry_run=False, wave=None, wait=False):
        session = state.reset()
        self.addCleanup(state.reset)
        turn = agent.Turn()
        turn.roles, turn.job = roles, job
        session["turn"] = turn
        session["items"].append(state.item_user(USER))
        with mock.patch.object(orchestrator, "live_facts", lambda plan: json.loads(json.dumps(LIVE))), \
                mock.patch.object(orchestrator, "layout", lambda plan, live, project: ([], [], [], [])), \
                mock.patch.object(waves, "prepare", side_effect=AssertionError("nothing may start")):
            return orchestrator.delegate(wave=wave or reference_wave(), dry_run=dry_run, wait=wait)

    def test_a_wave_past_what_is_left_is_refused_with_how_many_may_start(self):
        roles = types.SimpleNamespace(orchestrate=True, job_max_usd=0.4, worker_max_seconds=1800, max_workers=3,
                                      turn_workers=1)
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                result = self.delegate(roles, dry_run=dry_run)
                self.assertFalse(result.ok)
                self.assertIn("workers: this turn can start 1 more worker, and this wave briefs 2; brief at most 1, "
                              "or do the rest yourself", result.text)
                self.assertEqual((result.plan_capped, result.plan_refused), (True, False), "its own neutral card")
        broken = reference_wave()
        broken["workers"][0]["requirements"] = [{"quote": "not what the user said", "do": "x"}]
        mixed = self.delegate(roles, wave=broken)
        self.assertIn("this turn can start 1 more worker", mixed.text)
        self.assertEqual((mixed.plan_capped, mixed.plan_refused), (False, True), "a plan to fix as well: as today")

    def test_with_no_workers_left_delegate_says_so_and_nothing_to_call_again(self):
        roles = types.SimpleNamespace(orchestrate=True, job_max_usd=0.4, worker_max_seconds=1800, max_workers=2,
                                      turn_workers=2)
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                result = self.delegate(roles, self.job(2, "finished", "finished"), dry_run=dry_run)
                self.assertEqual(result.text, "delegate refused; nothing started: this turn can start 0 more workers "
                                              "(its 2 are started: P0, P1). Do the rest yourself, or say plainly what "
                                              "is not done (for your planning; to the user, workers are helpers).")
                self.assertNotIn("call again", result.text)
                self.assertNotIn("don't mention", result.text)
                self.assertEqual((result.ok, result.plan_capped, result.plan_refused), (False, True, False))
        dry = self.delegate(roles, self.job(2, "finished", "running"), dry_run=True, wait=True)
        self.assertIn("(its 2 are started: P0, P1). Still running: P1; wait for them", dry.text, "a dry run never waits")
        self.assertEqual((dry.plan_capped, dry.then), (True, None))

    def test_the_finish_gates_bounce_sends_nothing_back_to_a_worker_with_none_left(self):
        session = state.new_session()
        turn = agent.Turn()
        turn.roles = types.SimpleNamespace(turn_workers=2)
        failing = {"id": "c1", "kind": "exists", "name": "Lamp", "quote": "a lamp"}
        for job, bounce in ((self.job(2, "finished"), orchestrator.BOUNCE),
                            (self.job(2, "finished", "finished"), orchestrator.BOUNCE_NO_WORKERS),
                            (self.job(0, "finished", "finished"), orchestrator.BOUNCE)):
            with self.subTest(cap=job.turn_workers, crew=len(job.crew)):
                job.bounces, job.retired = 0, {}
                job.final_checks = lambda: [("lamp c1", failing)]
                turn.job = job
                with mock.patch.object(orchestrator, "_late_workers", return_value=None):
                    text = orchestrator.finish_gate(session, turn, None, lambda checks: [
                        {"status": "fail", "detail": "Lamp is not in the scene"}])
                self.assertEqual(text, bounce.format(lines='- id "lamp c1": ' + orchestrator.result_line(
                    "", failing, {"status": "fail", "detail": "Lamp is not in the scene"}).strip()))
        self.assertNotIn("delegate, continues", orchestrator.BOUNCE_NO_WORKERS)
        self.assertIn("Fix them yourself (this turn can start no more workers, for your planning; to the user, "
                      "workers are helpers), or say plainly", orchestrator.BOUNCE_NO_WORKERS)
        self.assertNotIn("don't mention", orchestrator.BOUNCE_NO_WORKERS)

    def test_the_budget_line_tells_the_model_how_many_more_after_these(self):
        """A free-plan persona (2026-10-24) told a Japanese user "since I can't add workers..." from this line: what
        is left reads as the model's to plan with, none left as what to do, and the user's word is helpers."""
        plan = orchestrator.normalise_wave(reference_wave())
        head = "Budget: $0.400 of $0.40 left for this turn's workers; 1800 s per worker run"
        for turn_workers, after in ((3, "; after these, you can start 1 more worker this turn"),
                                    (4, "; after these, you can start 2 more workers this turn"),
                                    (2, "; after these, start no more workers this turn and do any further work "
                                        "yourself (for your planning; to the user, workers are helpers)")):
            with self.subTest(turn_workers=turn_workers):
                line = orchestrator.budget_line(plan, config.Roles(None, None, True, turn_workers=turn_workers))
                self.assertEqual(line, head + after + ".")
                self.assertNotIn("this turn can start", line, "not a fact about the turn to pass on")
                self.assertNotIn("don't mention", line, "the limit is real and shown; nothing to hide")
        self.assertEqual(orchestrator.budget_line(plan, config.Roles(None, None, True)), head + ".", "as in 0.2.15")
        self.assertEqual(orchestrator.budget_line(plan, config.Roles(None, None, True, worker_max_steps=24)),
                         "Budget: $0.400 of $0.40 left for this turn's workers; 1800 s and 24 steps per worker run.")
        self.assertEqual(orchestrator.budget_line(plan, config.Roles(None, None, True, turn_workers=2,
                                                                     worker_max_steps=24)),
                         "Budget: $0.400 of $0.40 left for this turn's workers; 1800 s and 24 steps per worker run"
                         + after + ".")

    def test_the_routing_note_names_the_limit_before_planning(self):
        decision = router.Decision(3, [("Chair", "a chair"), ("Table", "a table")])
        self.assertIn("This turn can start at most 2 workers.", router.note(decision, workers=2))
        self.assertNotIn("at most", router.note(decision))
        self.assertNotIn("at most", router.note(router.Decision(1), workers=2), "a small edit is the agent's own")
        for decision, expected in ((decision, "This turn can start at most 2 workers."), (None, "<routing>This turn "
                                   "can start at most 2 workers.</routing>")):
            with self.subTest(decision=decision):
                session = state.new_session()
                session["messages"] = [{"role": "user", "content": "a chair and a table"}]
                turn = agent.Turn()
                turn.roles = types.SimpleNamespace(turn_workers=2)
                with mock.patch.object(router, "route", return_value=decision):
                    router.start_turn(session, turn, object(), lambda: False)
                self.assertIn(expected, session["messages"][0]["content"])
                router.start_turn(session, turn, object(), lambda: False)  # A resumed turn: said once.
                self.assertEqual(session["messages"][0]["content"].count("at most 2 workers"), 1)

    def test_the_delegate_schema_stays_static_and_defers_to_the_turns_limit(self):
        delegate = next(s for s in orchestrator.SCHEMAS if s["function"]["name"] == "delegate")["function"]
        self.assertNotIn("any number (the rest queue)", delegate["description"])
        self.assertIn("up to the turn's limit (the rest queue)", delegate["description"])

    def test_a_resumed_turn_goes_on_counting_the_workers_it_started(self):
        """end_turn records the workers started on the message that began the turn; the resume's new Job reads it."""
        roles = config.Roles(None, None, True, max_workers=3, turn_workers=3)
        message = {"role": "user", "content": "a chair, a table and a lamp"}
        session = {"id": "c", "turn_number": 1, "jobs": [], "messages": [message], "items": []}
        with tempfile.TemporaryDirectory() as data, mock.patch.dict(os.environ, {"LOOPCUT_DATA_DIR": data}):
            job = waves.Job(session, roles)
            job.crew += self.job(3, "finished", "finished", "never started").crew
            waves.end_turn(session, types.SimpleNamespace(job=job, cancel=threading.Event()))
            self.assertEqual(message[waves.WORKERS_STARTED], 2)
            resumed = waves.Job(session, roles)
            self.assertEqual((resumed.started_before, waves.workers_left(resumed, roles)), (2, 1))
            resumed.crew += self.job(3, "finished").crew
            cancelled = threading.Event()
            cancelled.set()
            with mock.patch.object(waves, "stop"):
                waves.end_turn(session, types.SimpleNamespace(job=resumed, cancel=cancelled))
            self.assertEqual(message[waves.WORKERS_STARTED], 3, "a Stop records them too")
            plain = waves.Job({**session, "messages": [{"role": "user", "content": "x"}]}, config.Roles(None, None, True))
            plain.crew += self.job(0, "finished").crew
            waves.end_turn(session, types.SimpleNamespace(job=plain, cancel=threading.Event()))
            self.assertNotIn(waves.WORKERS_STARTED, plain.request, "without the term, nothing is written")


class ShowcaseTest(unittest.TestCase):
    def test_the_typical_build_is_the_accounts_else_the_constant(self):
        self.assertEqual(chat_credits.TYPICAL_BUILD, 12, "the server's figure at the 1x markup")
        self.assertEqual(chat_credits.typical_build({"agent": {"typical_build": 30}}), 30)
        with mock.patch("builtins.print"):
            for account in ({}, {"agent": {"typical_build": 0}}, {"agent": {"typical_build": "12"}}, None):
                with self.subTest(account=account):
                    self.assertEqual(chat_credits.typical_build(account), chat_credits.TYPICAL_BUILD)


if __name__ == "__main__":
    unittest.main()
