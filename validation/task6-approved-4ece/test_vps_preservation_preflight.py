"""Offline operator-tool regressions using generic Compose and Docker fixtures."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parent
SOURCE = Path(os.environ.get("NEXUS_PREFLIGHT_UNDER_TEST", ROOT / "vps-preservation-preflight.py"))
SPEC = importlib.util.spec_from_file_location("preflight_under_test", SOURCE)
PREFLIGHT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREFLIGHT)
SECRET = "NEVER_PRINT_FIXTURE_PASSWORD_8291"


class PreservationChecks(unittest.TestCase):
    def setUp(self):
        self.original = {"containers": [], "services": {}, "networks": {"metadata": {"internal": True}}}
        fixture_services = [(PREFLIGHT.STAGE, name) for name in ("api", "research-worker", "postgres", "ingress", "metadata-init", "state-init")]
        fixture_services += [(PREFLIGHT.PROD, name) for name in ("app", "nginx", "certbot", "inactive-fixture")]
        for number, (project, name) in enumerate(fixture_services, 1):
            user = "100:100" if name == "research-worker" else ("tradexa" if name in ("api", "app") else "")
            main_memory = 1610612736 if name == "api" else 536870912
            mounts = [{"type": "volume", "source": project+"_fixture-state", "target": "/fixture-state", "read_only": False}] if name in ("api", "app") else []
            health = "healthy" if name in ("api", "research-worker", "postgres", "ingress", "app", "nginx") else "none"
            self.original["containers"].append({"id": "fixture-container-"+str(number), "name": "/"+project+"-"+name+"-1",
                "project": project, "service": name, "image": "sha256:"+format(number,"064x"), "status": "running" if health == "healthy" or name == "certbot" else "exited",
                "exit_code": 0, "health": health, "started": "2000-01-01T00:00:00Z", "restarts": 0, "oom": False,
                "user": user, "memory": main_memory, "memory_plus_swap": main_memory, "mounts": mounts})
            if project == PREFLIGHT.STAGE and name in ("api", "research-worker", "postgres", "ingress"):
                self.original["services"][name] = {"image_reference": "generic-fixture-"+name+":local", "memory": str(main_memory),
                    "memory_plus_swap": str(main_memory) if name == "api" else None, "configured_user": user if name == "research-worker" else "IMAGE_DEFAULT",
                    "mounts": mounts, "ports": [], "profiles": ["research"] if name == "research-worker" else [], "safe_controls": {"HUB_AUTO_ENGINE": "0"} if name == "api" else {}}
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        directory = Path(self.temporary.name)
        self.files = [directory / name for name in ("base.yaml", "customer.yaml", "memory.yaml")]
        self.envfile = directory / "compose.env"
        for path in [*self.files, self.envfile]:
            path.write_text("fixture")
        self.containers = {}
        self.images = {}
        self.config = {"name": PREFLIGHT.STAGE, "services": {}, "volumes": {}, "networks": self.original["networks"]}
        for row in self.original["containers"]:
            state = {"Status": row["status"], "ExitCode": row["exit_code"], "StartedAt": row["started"], "OOMKilled": row["oom"]}
            if row["health"] != "none":
                state["Health"] = {"Status": row["health"]}
            env = {"FIXTURE_CREDENTIAL": SECRET}
            if row["project"] == PREFLIGHT.STAGE and row["service"] in self.original["services"]:
                env.update(self.original["services"][row["service"]]["safe_controls"])
            c = {"Id": row["id"], "Name": row["name"], "Image": row["image"], "State": state,
                 "RestartCount": row["restarts"], "HostConfig": {"Memory": row["memory"], "MemorySwap": row["memory_plus_swap"]},
                 "Config": {"User": row["user"], "Env": [k+"="+v for k, v in env.items()],
                            "Labels": {"com.docker.compose.project": row["project"], "com.docker.compose.service": row["service"], "com.docker.compose.oneoff": "False"}},
                 "Mounts": [{"Type": m["type"], "Source": m["source"], "Name": m["source"] if m["type"] == "volume" else "", "Destination": m["target"], "RW": not m["read_only"]} for m in row["mounts"]]}
            self.containers[c["Id"]] = c
            if row["project"] != PREFLIGHT.STAGE or row["service"] not in self.original["services"]:
                continue
            source = self.original["services"][row["service"]]
            service = {"image": source["image_reference"], "mem_limit": source["memory"], "memswap_limit": source["memory_plus_swap"],
                       "environment": env, "ports": source["ports"], "profiles": source["profiles"], "volumes": copy.deepcopy(source["mounts"])}
            if source["configured_user"] != "IMAGE_DEFAULT":
                service["user"] = source["configured_user"]
            for m in service["volumes"]:
                if m["type"] == "volume":
                    self.config["volumes"][m["source"]] = {"name": m["source"]}
            self.config["services"][row["service"]] = service
            self.images[c["Image"]] = {"Config": {"User": row["user"], "Labels": {}}}
        self.calls = []

    def docker(self, args, **kwargs):
        self.calls.append(args)
        if args[0] == "ps":
            project = args[-1].rsplit("=", 1)[1]
            return " ".join(c["Id"] for c in self.containers.values() if c["Config"]["Labels"]["com.docker.compose.project"] == project)
        if args[0] == "inspect":
            return json.dumps([self.containers[cid] for cid in args[1:]])
        if args[:2] == ["image", "inspect"]:
            return json.dumps([self.images[args[2]]])
        if args[0] == "compose" and args[-3:] == ["config", "--format", "json"]:
            self.assertEqual([args[i+1] for i, a in enumerate(args) if a == "-f"], [str(p) for p in self.files])
            return json.dumps(self.config)
        if args[:2] == ["exec", "-i"]:
            api = next(c for c in self.containers.values() if c["Name"] == "/"+PREFLIGHT.STAGE+"-api-1")
            self.assertEqual(args, ["exec", "-i", api["Id"], "python", "-B", "-"])
            self.assertEqual(kwargs["input_text"], PREFLIGHT.DATABASE_READONLY_CODE)
            compile(kwargs["input_text"], "database_readonly", "exec")
            self.assertIn("default_transaction_read_only=on", kwargs["input_text"])
            return json.dumps({"status": "READ_ONLY_CHECKS_PASSED", "blockers": []})
        self.fail("Unexpected Docker operation: " + repr(args))

    def run_helper(self, *options):
        output = io.StringIO()
        with patch.object(PREFLIGHT, "FILES", self.files), patch.object(PREFLIGHT, "ENVFILE", self.envfile), patch.object(PREFLIGHT, "command", self.docker), patch.object(sys, "argv", [str(SOURCE), *options]), contextlib.redirect_stdout(output):
            code = PREFLIGHT.main()
        self.assertNotIn(SECRET, output.getvalue())
        return code, json.loads(output.getvalue())

    def test_compose_decimal_strings_and_missing_health(self):
        code, result = self.run_helper()
        self.assertEqual(code, 0)
        self.assertEqual(result["blockers"], [])
        self.assertEqual(len(result["containers"]), 10)
        self.assertTrue(any(c["health"] == "none" for c in result["containers"]))
        self.assertEqual(result["services"]["api"]["memory"], 1610612736)
        self.assertEqual(result["services"]["api"]["memory_plus_swap"], 1610612736)

    def test_wrong_memory_limit_still_blocks(self):
        self.config["services"]["api"]["mem_limit"] = "805306368"
        code, result = self.run_helper()
        self.assertEqual(code, 1)
        self.assertIn("API_1536_MIB_OVERRIDE_NOT_PRESERVED", result["blockers"])

    def test_unrecognized_memory_representation_blocks(self):
        self.config["services"]["api"]["memswap_limit"] = "unknown"
        code, result = self.run_helper()
        self.assertEqual(code, 1)
        self.assertIn("API_1536_MIB_OVERRIDE_NOT_PRESERVED", result["blockers"])

    def test_secret_difference_prints_key_only(self):
        self.config["services"]["api"]["environment"] = dict(self.config["services"]["api"]["environment"], FIXTURE_CREDENTIAL="CHANGED_SECRET_SENTINEL")
        code, result = self.run_helper()
        self.assertEqual(code, 1)
        self.assertEqual(result["services"]["api"]["environment_diff_keys"], ["FIXTURE_CREDENTIAL"])
        self.assertNotIn("CHANGED_SECRET_SENTINEL", json.dumps(result))

    def test_database_check_exec_targets_only_existing_stage_api(self):
        code, result = self.run_helper("--database-readonly")
        self.assertEqual(code, 0)
        self.assertEqual(result["database_read_only"]["status"], "READ_ONLY_CHECKS_PASSED")
        self.assertEqual(sum(args[0] == "exec" for args in self.calls), 1)
        self.assertTrue(all(args[0] in {"ps", "inspect", "image", "compose", "exec"} for args in self.calls))


if __name__ == "__main__":
    unittest.main()
