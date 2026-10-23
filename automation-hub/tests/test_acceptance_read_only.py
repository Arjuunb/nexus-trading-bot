"""Focused acceptance-mode regression tests with synthetic saved intent.

Lifecycle tests compile the actual named hook bodies without importing the
financial application. Initialization tests execute only the actual module
blocks containing the three known financial initialization calls. These are
narrow regression tests, not a claim that a complete application boot is safe.
No provider, real session, runtime thread or external database is used.
"""

import ast
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).parents[1]
MODE = "NEXUS_ACCEPTANCE_READ_ONLY"
DISABLED = {
    "HUB_AUTO_ENGINE": "0",
    "HUB_ENABLE_EXTERNAL_LIVE": "0",
    "HUB_USE_LIVE_DATA": "0",
    "HUB_RESEARCH_AUTOSTART": "0",
    "NEXUS_SHADOW_SUPERVISED": "0",
}


def policy():
    path = ROOT / "services/acceptance_policy.py"
    if not path.is_file():
        # Let the approved source reproduce its execution-on behavior rather
        # than masking the original defect with an import failure.
        return SimpleNamespace(acceptance_read_only=lambda: False)
    spec = importlib.util.spec_from_file_location("acceptance_policy_regression", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SyntheticRuntime:
    def __init__(self, state, name):
        self.state = state
        self.name = name
        self.running = False
        self.interval_s = 20
        self.autostart_enabled = True

    def start(self):
        self.running = True
        return True

    def stop(self, *_args):
        self.running = False
        self.state["saved_intent"][self.name] = False

    def flush_runtime_state(self):
        self.state["financial_rows"].append("checkpoint-write")


class SyntheticService:
    """A production-line background service (monitor, exporter, journal, lab).

    Holds no saved trading intent; the test only needs to see whether it ran."""
    interval_s = 60
    countries = ("US",)
    destination = "synthetic"

    def __init__(self, *, starts=True):
        self.starts = starts
        self.running = False

    def start(self):
        self.running = self.starts
        return self.starts

    restore = ensure_started = start

    def stop(self, *_args):
        self.running = False

    shutdown = stop

    def reconcile(self):
        return {}


def lifecycle_namespace():
    state = {
        "saved_intent": {
            "instance": True,
            "engine": True,
            "pa": True,
            "smc": True,
            "shadow": True,
            "legacy": True,
        },
        "financial_rows": [("original-funding", "1.25")],
        "entry_gate": True,
    }
    runtimes = {
        name: SyntheticRuntime(state, name)
        for name in (
            "instance", "engine", "pa", "smc", "shadow", "legacy", "supervisor", "research"
        )
    }

    # The production line's background services and Adaptive MTF Lab. Guardian
    # reports "not configured" here so the harness never installs its bus.
    for name in ("status_monitor", "outbound_webhooks", "econ_feed", "journal_recorder",
                 "weekly_review_scheduler", "audit_exporter", "adaptive_lab",
                 "adaptive_lab_supervisor"):
        runtimes[name] = SyntheticService()
    runtimes["guardian"] = SyntheticService(starts=False)

    def restore():
        runtimes["instance"].start()
        return ["saved-instance-id"]

    def close_entry_gate():
        state["entry_gate"] = False

    api = SimpleNamespace(
        ledger=SimpleNamespace(),
        controls=SimpleNamespace(stop_all=close_entry_gate),
        instance_manager=SimpleNamespace(
            store=SimpleNamespace(available=True),
            restore_desired_instances=restore,
            shutdown=runtimes["instance"].stop,
        ),
        instance_supervisor=runtimes["supervisor"],
        engine=runtimes["engine"],
        price_action_runtime=runtimes["pa"],
        smc_runtime=runtimes["smc"],
        shadow_router=SimpleNamespace(
            store=SimpleNamespace(registrations=lambda **_kw: ["saved-shadow"])
        ),
        shadow_observation_worker=runtimes["shadow"],
        research_observer=runtimes["research"],
        strategy_intelligence=SimpleNamespace(context=SimpleNamespace(_research_worker=None)),
        **{name: runtimes[name] for name in (
            "status_monitor", "outbound_webhooks", "econ_feed", "journal_recorder",
            "weekly_review_scheduler", "audit_exporter", "adaptive_lab", "adaptive_lab_supervisor",
            "guardian")},
    )
    namespace = {
        "__name__": "isolated_lifecycle_regression",
        "_sec_os": os,
        "webhook_api": api,
        "manager": SimpleNamespace(emergency_stop_all=runtimes["legacy"].stop),
        "settings": SimpleNamespace(auto_engine=False),
        "acceptance_read_only": policy().acceptance_read_only,
    }
    parsed = ast.parse((ROOT / "app.py").read_text())
    names = {"_start_auto_engine", "_restore_shadow_observations", "_shutdown_all_runtimes"}
    functions = [
        deepcopy(node)
        for node in parsed.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    if len(functions) != len(names):
        raise AssertionError("Lifecycle entry points moved; update the focused harness")
    for node in functions:
        node.decorator_list = []
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(ROOT / "app.py"), "exec"), namespace)
    return namespace, state, runtimes


class LifecycleRegression(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, DISABLED | {MODE: "1"})
        self.environment.start()
        self.pytest_current_test = os.environ.pop("PYTEST_CURRENT_TEST", None)
        fake_data = ModuleType("data")
        fake_data.__path__ = []
        fake_ledger = ModuleType("data.ledger")
        fake_ledger.SUPABASE_STATUS = {"configured": False, "connected": False}
        self.modules = patch.dict(sys.modules, {"data": fake_data, "data.ledger": fake_ledger})
        self.modules.start()

    def tearDown(self):
        self.modules.stop()
        if self.pytest_current_test is not None:
            os.environ["PYTEST_CURRENT_TEST"] = self.pytest_current_test
        self.environment.stop()

    def test_acceptance_startup_preserves_saved_sessions_without_restoring_execution(self):
        namespace, state, runtimes = lifecycle_namespace()
        original = deepcopy(state)
        namespace["_start_auto_engine"]()
        self.assertFalse(any(runtime.running for runtime in runtimes.values()))
        self.assertEqual(state, original)

    def test_acceptance_shadow_restore_preserves_intent_without_starting_worker(self):
        namespace, state, runtimes = lifecycle_namespace()
        original = deepcopy(state)
        namespace["_restore_shadow_observations"]()
        self.assertFalse(runtimes["shadow"].running)
        self.assertEqual(state, original)

    def test_acceptance_shutdown_preserves_financial_records_and_desired_session_intent(self):
        namespace, state, _runtimes = lifecycle_namespace()
        original = deepcopy(state)
        namespace["_shutdown_all_runtimes"]()
        self.assertEqual(state, original)

    def test_acceptance_shutdown_can_stop_only_an_owned_research_metadata_worker(self):
        namespace, state, _runtimes = lifecycle_namespace()
        original = deepcopy(state)
        metadata_worker = SimpleNamespace(stopped=False)
        metadata_worker.stop = lambda: setattr(metadata_worker, "stopped", True)
        namespace["webhook_api"].strategy_intelligence.context._research_worker = metadata_worker
        namespace["_shutdown_all_runtimes"]()
        self.assertTrue(metadata_worker.stopped)
        self.assertEqual(state, original)

    def test_normal_mode_still_restores_saved_instances(self):
        os.environ[MODE] = "0"
        namespace, _state, runtimes = lifecycle_namespace()
        namespace["_start_auto_engine"]()
        self.assertTrue(runtimes["instance"].running)
        self.assertTrue(runtimes["supervisor"].running)


def initialization_blocks():
    parsed = ast.parse((ROOT / "webhook_api.py").read_text())
    targets = {"seed_if_empty", "_persist_account_snapshot", "backfill"}
    blocks = [
        deepcopy(node)
        for node in parsed.body
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr in targets
            for child in ast.walk(node)
        )
    ]
    if not blocks:
        raise AssertionError("Financial initialization blocks moved; update the focused harness")
    return compile(ast.Module(body=blocks, type_ignores=[]), str(ROOT / "webhook_api.py"), "exec")


class ImportInitializationRegression(unittest.TestCase):
    def execute_initialization(self, enabled):
        state = {"financial_rows": [("existing-fill", "7.50")], "memory_rows": []}

        def seed(_cash):
            state["financial_rows"].append(("new-account-seed", "10000.00"))

        def reconcile():
            state["financial_rows"].append(("boot-reconciliation", "10000.00"))

        def backfill():
            state["memory_rows"].append("boot-backfill")

        namespace = {
            "__name__": "isolated_initialization_regression",
            "_os": os,
            "settings": SimpleNamespace(starting_cash=10000),
            "account_store": SimpleNamespace(seed_if_empty=seed),
            "paper": SimpleNamespace(_persist_account_snapshot=reconcile),
            "trade_memory": SimpleNamespace(backfill=backfill),
        }
        with patch.dict(os.environ, DISABLED | {MODE: "1" if enabled else "0"}):
            namespace["acceptance_read_only"] = policy().acceptance_read_only
            exec(initialization_blocks(), namespace)
        return state

    def test_acceptance_import_skips_financial_seed_reconciliation_and_backfill(self):
        self.assertEqual(
            self.execute_initialization(True),
            {"financial_rows": [("existing-fill", "7.50")], "memory_rows": []},
        )

    def test_normal_import_retains_seed_reconciliation_and_backfill(self):
        state = self.execute_initialization(False)
        self.assertIn(("new-account-seed", "10000.00"), state["financial_rows"])
        self.assertIn(("boot-reconciliation", "10000.00"), state["financial_rows"])
        self.assertEqual(state["memory_rows"], ["boot-backfill"])


class RuntimeOverrideImportRegression(unittest.TestCase):
    def execute_override_loading(self, enabled):
        state = {"remote_probe_writes": 0, "local_cache_writes": 0, "applied": []}

        def loader(_path):
            state["remote_probe_writes"] += 1
            state["local_cache_writes"] += 1
            return {"engine_desired_running": 1}

        parsed = ast.parse((ROOT / "webhook_api.py").read_text())
        blocks = [
            deepcopy(node)
            for node in parsed.body
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and any(
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "load_overrides"
                for child in ast.walk(node)
            )
        ]
        self.assertEqual(len(blocks), 1, "Override initialization moved; update the focused harness")
        namespace = {
            "__name__": "isolated_override_regression",
            "_os": os,
            "settings": SimpleNamespace(settings_path="missing-synthetic-overrides.json"),
            "load_overrides": loader,
            "_apply_setting": lambda key, value: state["applied"].append((key, value)),
        }
        with patch.dict(os.environ, {MODE: "1" if enabled else "0"}):
            namespace["acceptance_read_only"] = policy().acceptance_read_only
            exec(
                compile(ast.Module(body=blocks, type_ignores=[]), str(ROOT / "webhook_api.py"), "exec"),
                namespace,
            )
        return state

    def test_acceptance_import_never_calls_remote_override_loader_or_warms_cache(self):
        self.assertEqual(
            self.execute_override_loading(True),
            {"remote_probe_writes": 0, "local_cache_writes": 0, "applied": []},
        )

    def test_normal_import_retains_runtime_override_loading(self):
        self.assertEqual(
            self.execute_override_loading(False),
            {
                "remote_probe_writes": 1,
                "local_cache_writes": 1,
                "applied": [("engine_desired_running", 1)],
            },
        )


class StrictOperatorPolicy(unittest.TestCase):
    def test_mode_is_explicit_and_disabled_by_default(self):
        for value, enabled in (("", False), ("0", False), ("1", True)):
            with self.subTest(value=value), patch.dict(os.environ, {MODE: value}):
                self.assertEqual(policy().acceptance_read_only(), enabled)
        with patch.dict(os.environ):
            os.environ.pop(MODE, None)
            self.assertFalse(policy().acceptance_read_only())

    def test_malformed_operator_values_fail_closed(self):
        for value in ("true", "false", "yes", "off", "2", " 1 ", "production"):
            with self.subTest(value=value), patch.dict(os.environ, {MODE: value}):
                with self.assertRaises(RuntimeError):
                    policy().acceptance_read_only()


if __name__ == "__main__":
    unittest.main()
