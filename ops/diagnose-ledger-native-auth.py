#!/usr/bin/env python3
"""Owner-run authentication comparison only; no backup, financial queries or deployment."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import tempfile

HELPER_SHA256 = "172d0deacc84432cda85436bc882c00718157e1b392eb087722cd4cb098f3fdb"
HELPER_PATH = Path(__file__).with_name("backup-supabase-ledger-to-mac.py")
if hashlib.sha256(HELPER_PATH.read_bytes()).hexdigest() != HELPER_SHA256:
    raise SystemExit("BLOCKED:PINNED_BACKUP_HELPER_REQUIRED")
spec = importlib.util.spec_from_file_location("pinned_ledger_backup", HELPER_PATH)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class NativePQ:
    """Use one explicitly selected native libpq for both credential paths.

    Passwords cross only in-process C arguments and the private temporary file.
    No SQL is sent by this class. All raw error text remains local in memory.
    """
    def __init__(self, library):
        self.lib = ctypes.CDLL(str(library))
        signatures = {
            "PQlibVersion": (ctypes.c_int, []),
            "PQconnectdbParams": (ctypes.c_void_p, [ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_char_p), ctypes.c_int]),
            "PQstatus": (ctypes.c_int, [ctypes.c_void_p]),
            "PQpass": (ctypes.c_char_p, [ctypes.c_void_p]),
            "PQerrorMessage": (ctypes.c_char_p, [ctypes.c_void_p]),
            "PQsslInUse": (ctypes.c_int, [ctypes.c_void_p]),
            "PQconnectionUsedPassword": (ctypes.c_int, [ctypes.c_void_p]),
            "PQfinish": (None, [ctypes.c_void_p]),
        }
        for name, (result, args) in signatures.items():
            function = getattr(self.lib, name)
            function.restype, function.argtypes = result, args
        self.version = self.lib.PQlibVersion()

    def probe(self, config, *, expected_password):
        helper.require(not any(k.startswith("PG") for k in os.environ), "CLEAN_POSTGRES_ENVIRONMENT_REQUIRED")
        parameters = dict(config, options=helper.SOURCE_OPTIONS)
        helper.require(parameters.get("sslmode") == "verify-full", "VERIFIED_TLS_REQUIRED")
        helper.require(parameters.get("hostaddr") and "," not in parameters["hostaddr"], "ONE_PINNED_ENDPOINT_REQUIRED")
        array = ctypes.c_char_p * (len(parameters) + 1)
        keys = array(*(str(k).encode("utf-8") for k in parameters), None)
        values = array(*(str(v).encode("utf-8") for v in parameters.values()), None)
        connection = self.lib.PQconnectdbParams(keys, values, 0)
        helper.require(bool(connection), "NATIVE_CONNECTION_ALLOCATION_FAILED")
        try:
            authenticated = self.lib.PQstatus(connection) == 0
            result = {"authenticated": authenticated,
                      "consumed_password_matches_input": self.lib.PQpass(connection) == expected_password.encode("utf-8"),
                      "password_authentication_used": bool(self.lib.PQconnectionUsedPassword(connection)),
                      "tls_in_use": bool(self.lib.PQsslInUse(connection))}
            if not authenticated:
                message = (self.lib.PQerrorMessage(connection) or b"").decode("utf-8", "replace")
                result["failure_category"], _ = helper.connection_diagnostic(RuntimeError(message))
            return result
        finally:
            self.lib.PQfinish(connection)


def pinned_endpoint(config):
    if config.get("hostaddr"):
        return str(ipaddress.ip_address(config["hostaddr"]))
    candidates = socket.getaddrinfo(config["host"], config["port"], type=socket.SOCK_STREAM)
    for family, _, _, _, address in candidates:
        if family in (socket.AF_INET, socket.AF_INET6):
            return str(ipaddress.ip_address(address[0]))
    raise helper.Blocked("ONE_PINNED_ENDPOINT_REQUIRED")


def compare_authentication(config, *, native, bin_dir, directory):
    helper.require(config.get("sslmode") == "verify-full", "VERIFIED_TLS_REQUIRED")
    config = dict(config, hostaddr=pinned_endpoint(config))
    evidence = {"native_libpq_version": native.version, "single_endpoint_pinned": True,
                "same_password_input_used": True, "native_memory_attempted": False,
                "native_passfile_attempted": False, "native_psql_attempted": False,
                "python_psycopg_attempted": False}
    password_file = directory / ".pgpass"
    created = False
    try:
        fd = os.open(password_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        created = True
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(helper.passfile_line(config))
        memory_config = dict(config, passfile=str(password_file))
        file_config = {key: value for key, value in memory_config.items() if key != "password"}
        for name, parameters in (("native_memory", memory_config), ("native_passfile", file_config)):
            print(json.dumps({"phase": name.upper() + "_AUTHENTICATION_ONLY"}), flush=True)
            evidence[name + "_attempted"] = True
            result = native.probe(parameters, expected_password=config["password"])
            evidence[name] = result
            if not result["consumed_password_matches_input"]:
                raise helper.SourceConnectionBlocked("NATIVE_CONSUMED_PASSWORD_MISMATCH", None,
                    stage=name.upper() + "_AUTHENTICATION_ONLY", evidence=evidence)
            if not result["authenticated"]:
                raise helper.SourceConnectionBlocked(result["failure_category"], None,
                    stage=name.upper() + "_AUTHENTICATION_ONLY", evidence=evidence)
            if not result["tls_in_use"]:
                raise helper.SourceConnectionBlocked("VERIFIED_TLS_REQUIRED", None,
                    stage=name.upper() + "_AUTHENTICATION_ONLY", evidence=evidence)
            if not result["password_authentication_used"]:
                raise helper.SourceConnectionBlocked("PASSWORD_AUTHENTICATION_NOT_EXERCISED", None,
                    stage=name.upper() + "_AUTHENTICATION_ONLY", evidence=evidence)
        evidence["native_psql_attempted"] = True
        evidence.update(helper.verify_native_source(file_config, bin_dir=bin_dir))
        return {"status": "AUTHENTICATION_COMPARISON_PASSED_NO_BACKUP", "client_authentication_evidence": evidence}
    except helper.SourceConnectionBlocked as error:
        error.client_evidence = dict(evidence, **error.client_evidence)
        raise
    finally:
        if created and password_file.exists():
            password_file.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diagnose-auth", action="store_true", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--sslrootcert", type=Path, required=True)
    parser.add_argument("--pg-bin", type=Path, default=Path("/Applications/Postgres.app/Contents/Versions/17/bin"))
    parser.add_argument("--native-libpq", type=Path, default=Path("/Applications/Postgres.app/Contents/Versions/17/lib/libpq.5.dylib"))
    parser.add_argument("--destination", type=Path, default=Path.home() / "Nexus-Backups")
    args = parser.parse_args()
    os.umask(0o077)
    directory = None
    try:
        for key in list(os.environ):
            if key.startswith("PG"):
                del os.environ[key]
        helper.require(re.fullmatch(r"[a-z0-9]{20}", args.project), "SUPABASE_PROJECT_REF_REQUIRED")
        helper.check_tools(args.pg_bin)
        native = NativePQ(args.native_libpq.resolve())
        helper.require(native.version // 10000 == 17, "NATIVE_POSTGRES_LIBPQ_17_REQUIRED")
        destination = args.destination.expanduser().resolve()
        helper.require(destination.is_dir(), "EXISTING_BACKUP_DESTINATION_REQUIRED")
        ca_data = helper.load_root_certificate(args.sslrootcert)
        password = helper.read_database_password("mac-dialog", project=args.project)
        directory = Path(tempfile.mkdtemp(prefix="external-ledger-auth-check-", dir=destination))
        ca_path = helper.preserve_root_certificate(directory, ca_data)
        config = {"host": "db." + args.project + ".supabase.co", "port": 5432, "dbname": "postgres", "user": "postgres",
                  "password": password, "sslmode": "verify-full", "sslrootcert": str(ca_path),
                  "connect_timeout": 10, "application_name": "nexus_read_only_ledger_backup"}
        report = compare_authentication(config, native=native, bin_dir=args.pg_bin.resolve(), directory=directory)
    except Exception as error:
        report = helper.failure_report(error)
    report.update(scope="READ_ONLY_AUTHENTICATION_COMPARISON_ONLY", financial_rows_read=False,
                  backup_performed=False, deployment_performed=False, original_data_deleted=False,
                  external_ledger_recovery_proven=False, automatic_retries=0)
    if directory:
        report["directory"] = str(directory)
        report["temporary_credential_removed"] = not (directory / ".pgpass").exists()
        helper.private_json(directory / "authentication-receipt.json", report)
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "AUTHENTICATION_COMPARISON_PASSED_NO_BACKUP" else 1


if __name__ == "__main__":
    raise SystemExit(main())
