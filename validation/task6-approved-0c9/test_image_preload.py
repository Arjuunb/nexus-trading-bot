"""Tests for the exact-build policy; existing loader tests cover Docker boundaries."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location('preload_0c9', HERE / 'vps-image-preload.py')
P = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(P)
ENGINE = HERE.parent / 'task6-approved-4ece' / 'vps-image-preload.py'


class ExactBuildTests(unittest.TestCase):
    def test_original_verified_loader_is_reused(self):
        engine = P.load_engine(ENGINE)
        self.assertEqual(engine.STAGE, 'nexus-remote-stage-rc3-28acdbc')
        self.assertEqual(engine.API_MEMORY, 1610612736)
        self.assertEqual(engine.RESERVE, 8 * 1024**3)

    def test_tampered_loader_is_rejected_before_import(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'preload-engine.py'
            marker = Path(directory) / 'executed'
            path.write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
            with self.assertRaisesRegex(RuntimeError, 'PINNED_PRELOAD_ENGINE_REQUIRED'):
                P.load_engine(path)
            self.assertFalse(marker.exists())

    def test_missing_and_symlinked_loader_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'preload-engine.py'
            with self.assertRaisesRegex(RuntimeError, 'PINNED_PRELOAD_ENGINE_REQUIRED'):
                P.load_engine(path)
            path.symlink_to(ENGINE.resolve())
            with self.assertRaisesRegex(RuntimeError, 'PINNED_PRELOAD_ENGINE_REQUIRED'):
                P.load_engine(path)

    def test_default_verification_passes_exact_policy_and_never_loads(self):
        engine = Mock()
        engine.verify_packet.return_value = {'sealed_files_verified': 16, 'exports': {}, 'scans': {}}
        stream = io.StringIO()
        with patch.object(P, 'load_engine', return_value=engine), redirect_stdout(stream):
            P.main(['/synthetic/packet.zip'])
        engine.verify_packet.assert_called_once_with(Path('/synthetic/packet.zip'), policy=P.POLICY)
        engine.preload.assert_not_called()
        report = json.loads(stream.getvalue())
        for name in ('services_updated', 'application_started', 'deployment_performed',
                     'new_paper_execution', 'task6_complete'):
            self.assertIs(report[name], False)

    def test_loading_requires_explicit_option_and_exact_policy(self):
        engine = Mock()
        engine.preload.return_value = {'status': 'SYNTHETIC_CACHE_CHECK_ONLY'}
        with patch.object(P, 'load_engine', return_value=engine), redirect_stdout(io.StringIO()):
            P.main(['/synthetic/packet.zip', '--load'])
        engine.preload.assert_called_once_with(Path('/synthetic/packet.zip'), policy=P.POLICY)
        engine.verify_packet.assert_not_called()

    def test_no_deploy_or_policy_override_is_accepted(self):
        for option in ('--deploy', '--commit', '--skip-verification', '--source-sha256'):
            with self.subTest(option=option), redirect_stdout(io.StringIO()):
                with patch('sys.stderr', new=io.StringIO()), self.assertRaises(SystemExit) as error:
                    P.main(['/synthetic/packet.zip', option])
                self.assertEqual(error.exception.code, 2)

    def test_prior_approved_packet_cannot_substitute_for_this_build(self):
        engine = P.load_engine(ENGINE)
        with tempfile.TemporaryDirectory() as directory:
            packet = Path(directory) / 'old.zip'
            packet.write_bytes(b'old-build')
            with self.assertRaisesRegex(engine.Blocked, 'APPROVED_ZIP_IDENTITY_MISMATCH'):
                engine.verify_packet(packet, policy=P.POLICY)


if __name__ == '__main__':
    unittest.main()
