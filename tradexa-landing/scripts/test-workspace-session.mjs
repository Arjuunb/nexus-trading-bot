// Compile the isolated controller so the outcome suite also runs on CI Node 20.
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const temporary = mkdtempSync(join(tmpdir(), 'tradexa-workspace-tests-'));
let result = 1;
try {
  const compile = spawnSync(process.execPath, [
    join(root, 'node_modules/typescript/bin/tsc'),
    join(root, 'src/settings/workspace-session.ts'),
    '--strict', '--skipLibCheck', '--target', 'ES2022', '--module', 'ESNext',
    '--moduleResolution', 'bundler', '--lib', 'ES2022,DOM', '--outDir', temporary,
  ], { stdio: 'inherit' });
  if (compile.status !== 0) throw new Error('Workspace controller compilation failed.');
  writeFileSync(join(temporary, 'workspace-session.mjs'),
    readFileSync(join(temporary, 'workspace-session.js')));
  const tests = readFileSync(join(root, 'test/workspace-session.test.mjs'), 'utf8');
  const original = '../src/settings/workspace-session.ts';
  if (tests.split(original).length !== 2) throw new Error('Unexpected test module imports.');
  writeFileSync(join(temporary, 'workspace-session.test.mjs'),
    tests.replace(original, './workspace-session.mjs'));
  result = spawnSync(process.execPath, ['--test', join(temporary, 'workspace-session.test.mjs')],
    { stdio: 'inherit' }).status ?? 1;
} finally {
  rmSync(temporary, { recursive: true, force: true });
}
process.exitCode = result;
