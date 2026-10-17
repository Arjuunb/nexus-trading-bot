import test from 'node:test';
import assert from 'node:assert/strict';
import { createWorkspaceSession } from '../src/settings/workspace-session.ts';

// Fictitious values only. This suite never opens a socket or reads real accounts.
const USER_A = Object.freeze({
  id: 'fixture-user-a', email: 'a@example.invalid', full_name: 'Verified A',
  avatar_url: '', role: 'user', email_confirmed: true,
});
const USER_B = Object.freeze({
  id: 'fixture-user-b', email: 'b@example.invalid', full_name: 'Verified B',
  avatar_url: '', role: 'user', email_confirmed: true,
});
const TOKEN_A = 'fixture-access-a';
const TOKEN_B = 'fixture-access-b';
const TOKEN_A_RENEWED = 'fixture-access-a-renewed';
const DEFAULTS = Object.freeze({
  profile: Object.freeze({ fullName: '', email: '', timezone: 'UTC' }),
  appearance: Object.freeze({ theme: 'system', animations: true }),
  trading: Object.freeze({ preferredExchange: 'none', defaultTimeframe: '1h' }),
});
const SETTINGS_A = {
  ...DEFAULTS,
  profile: { fullName: 'Saved preference A', email: 'old-a@example.invalid', timezone: 'Europe/London' },
  appearance: { theme: 'dark', animations: false },
};
const SETTINGS_B = {
  ...DEFAULTS,
  profile: { fullName: 'Saved preference B', email: 'old-b@example.invalid', timezone: 'Asia/Tokyo' },
  appearance: { theme: 'light', animations: true },
};

const copy = value => structuredClone(value);
const deferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const drain = async () => {
  for (let i = 0; i < 12; i += 1) await Promise.resolve();
};

function fakeClock() {
  let next = 1;
  const tasks = new Map();
  return {
    schedule(callback) {
      const handle = next++;
      tasks.set(handle, { callback, cancelled: false });
      return handle;
    },
    cancel(handle) {
      const task = tasks.get(handle);
      if (task) task.cancelled = true;
    },
    async run({ includeCancelled = false } = {}) {
      const due = [...tasks.values()];
      tasks.clear();
      for (const task of due) {
        if (includeCancelled || !task.cancelled) task.callback();
      }
      await drain();
    },
  };
}

function harness({ intercept, initialA = SETTINGS_A, initialB = SETTINGS_B } = {}) {
  const clock = fakeClock();
  const requests = [];
  const saved = new Map([[TOKEN_A, copy(initialA)], [TOKEN_B, copy(initialB)]]);
  const identities = new Map([[TOKEN_A, USER_A], [TOKEN_A_RENEWED, USER_A], [TOKEN_B, USER_B]]);
  const request = async input => {
    requests.push(copy(input));
    const override = intercept?.(input);
    if (override !== undefined) return await override;
    const identity = identities.get(input.accessToken);
    if (!identity) return { status: 401, body: { detail: 'Not authenticated' } };
    if (input.method === 'GET' && input.path === '/auth/me') {
      return { status: 200, body: copy(identity) };
    }
    if (input.method === 'GET' && input.path === '/user/settings?ns=settings-center') {
      const ownerToken = input.accessToken === TOKEN_A_RENEWED ? TOKEN_A : input.accessToken;
      return { status: 200, body: { ns: 'settings-center', data: copy(saved.get(ownerToken)) } };
    }
    if (input.method === 'POST' && input.path === '/user/settings'
        && input.body?.ns === 'settings-center') {
      const ownerToken = input.accessToken === TOKEN_A_RENEWED ? TOKEN_A : input.accessToken;
      saved.set(ownerToken, copy(input.body.data));
      return { status: 200, body: { saved: true, ns: 'settings-center' } };
    }
    return { status: 404, body: { detail: 'Unknown test endpoint' } };
  };
  const normalizeSettings = incoming => Object.fromEntries(
    Object.entries(DEFAULTS).map(([section, values]) => [section, { ...values, ...incoming?.[section] }]),
  );
  const controller = createWorkspaceSession({
    defaults: copy(DEFAULTS), normalizeSettings, request,
    schedule: clock.schedule, cancel: clock.cancel, debounceMs: 400,
  });
  return { controller, requests, saved, clock };
}

test('signed-out users never send workspace requests or claim server persistence', async () => {
  const h = harness();
  await h.controller.setSession(null);
  h.controller.update('appearance', { theme: 'dark' });
  await h.clock.run();
  assert.equal(h.requests.length, 0);
  assert.equal(h.controller.getSnapshot().identity, null);
  assert.equal(h.controller.getSnapshot().backendConnected, false);
  assert.notEqual(h.controller.getSnapshot().saveState, 'saved');
  assert.equal(h.controller.getSnapshot().loadState, 'signed-out');
});

test('a verified session uses server workspace data and never a shared local cache', async () => {
  const previousWindow = globalThis.window;
  const previousStorage = globalThis.localStorage;
  const storageCalls = [];
  const storage = {
    getItem(key) { storageCalls.push(['read', key]); return JSON.stringify(SETTINGS_A); },
    setItem(key, value) { storageCalls.push(['write', key, value]); },
    removeItem(key) { storageCalls.push(['remove', key]); },
  };
  globalThis.localStorage = storage;
  globalThis.window = { localStorage: storage, __HUB_CONFIG__: { authMode: 'supabase' } };
  try {
    const h = harness();
    assert.equal(h.controller.getSnapshot().backendConnected, false);
    await h.controller.setSession(TOKEN_B);
    assert.deepEqual(h.controller.getSnapshot().settings, SETTINGS_B);
    assert.deepEqual(h.controller.getSnapshot().identity, USER_B);
    assert.deepEqual(storageCalls, []);
    const serialized = JSON.stringify(h.controller.getSnapshot());
    assert.equal(serialized.includes(TOKEN_B), false);
    h.controller.dispose();
  } finally {
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
    if (previousStorage === undefined) delete globalThis.localStorage;
    else globalThis.localStorage = previousStorage;
  }
});

test('switching from A to B clears A settings and verified identity before B responds', async () => {
  const bIdentity = deferred();
  const h = harness({
    intercept: input => input.accessToken === TOKEN_B && input.path === '/auth/me'
      ? bIdentity.promise : undefined,
  });
  await h.controller.setSession(TOKEN_A);
  assert.deepEqual(h.controller.getSnapshot().settings, SETTINGS_A);
  const loadingB = h.controller.setSession(TOKEN_B);
  assert.deepEqual(h.controller.getSnapshot().settings, DEFAULTS);
  assert.equal(h.controller.getSnapshot().identity, null);
  assert.equal(h.controller.getSnapshot().backendConnected, false);
  assert.equal(h.controller.getSnapshot().loadState, 'loading');
  bIdentity.resolve({ status: 200, body: copy(USER_B) });
  await loadingB;
  assert.deepEqual(h.controller.getSnapshot().settings, SETTINGS_B);
  assert.deepEqual(h.controller.getSnapshot().identity, USER_B);
});

test('A delayed workspace response cannot overwrite B after account switching', async () => {
  const aRead = deferred();
  const h = harness({
    intercept: input => input.accessToken === TOKEN_A && input.path.startsWith('/user/settings?')
      ? aRead.promise : undefined,
  });
  const loadingA = h.controller.setSession(TOKEN_A);
  await drain();
  await h.controller.setSession(TOKEN_B);
  aRead.resolve({ status: 200, body: { ns: 'settings-center', data: copy(SETTINGS_A) } });
  await loadingA;
  assert.deepEqual(h.controller.getSnapshot().settings, SETTINGS_B);
  assert.deepEqual(h.controller.getSnapshot().identity, USER_B);
  assert.equal(h.controller.getSnapshot().backendConnected, true);
});

test('a delayed A identity cannot initiate a workspace read after switching to B', async () => {
  const aIdentity = deferred();
  const h = harness({
    intercept: input => input.accessToken === TOKEN_A && input.path === '/auth/me'
      ? aIdentity.promise : undefined,
  });
  const loadingA = h.controller.setSession(TOKEN_A);
  await h.controller.setSession(TOKEN_B);
  aIdentity.resolve({ status: 200, body: copy(USER_A) });
  await loadingA;
  assert.deepEqual(h.controller.getSnapshot().identity, USER_B);
  assert.equal(h.requests.some(r => r.accessToken === TOKEN_A && r.path.startsWith('/user/settings')), false);
});

test('an A autosave queued before account switching never writes into B workspace', async () => {
  const h = harness();
  await h.controller.setSession(TOKEN_A);
  h.controller.update('appearance', { theme: 'private-a-theme' });
  await h.controller.setSession(TOKEN_B);
  // Also model a timer already queued by the event loop when cancellation occurs.
  await h.clock.run({ includeCancelled: true });
  assert.equal(h.requests.filter(r => r.method === 'POST').length, 0);
  assert.deepEqual(h.saved.get(TOKEN_B), SETTINGS_B);
  assert.deepEqual(h.controller.getSnapshot().settings, SETTINGS_B);
});

test('an A save already in flight retains A credentials and cannot mark B saved', async () => {
  const aWrite = deferred();
  const h = harness({
    intercept: input => input.method === 'POST' && input.accessToken === TOKEN_A
      ? aWrite.promise : undefined,
  });
  await h.controller.setSession(TOKEN_A);
  h.controller.update('appearance', { theme: 'dark-a-change' });
  await h.clock.run();
  assert.equal(h.controller.getSnapshot().saveState, 'saving');
  await h.controller.setSession(TOKEN_B);
  aWrite.resolve({ status: 200, body: { saved: true, ns: 'settings-center' } });
  await drain();
  const writes = h.requests.filter(r => r.method === 'POST');
  assert.equal(writes.length, 1);
  assert.equal(writes[0].accessToken, TOKEN_A);
  assert.equal(writes[0].body.data.appearance.theme, 'dark-a-change');
  assert.deepEqual(h.saved.get(TOKEN_B), SETTINGS_B);
  assert.equal(h.controller.getSnapshot().saveState, 'idle');
});

test('identity remains authoritative from auth/me even when saved preferences claim another identity', async () => {
  const spoofed = {
    ...SETTINGS_B,
    profile: { fullName: 'Pretend administrator', email: USER_A.email, timezone: 'UTC', id: USER_A.id, role: 'admin' },
  };
  const h = harness({ initialB: spoofed });
  await h.controller.setSession(TOKEN_B);
  assert.deepEqual(h.controller.getSnapshot().identity, USER_B);
  h.controller.update('profile', { fullName: 'Another preference', email: USER_A.email });
  await h.clock.run();
  assert.deepEqual(h.controller.getSnapshot().identity, USER_B);
});

test('server connection is unproven until the workspace GET succeeds', async () => {
  const read = deferred();
  const h = harness({ intercept: input => input.path.startsWith('/user/settings?') ? read.promise : undefined });
  const loading = h.controller.setSession(TOKEN_A);
  await drain();
  assert.deepEqual(h.controller.getSnapshot().identity, USER_A);
  assert.equal(h.controller.getSnapshot().backendConnected, false);
  assert.notEqual(h.controller.getSnapshot().saveState, 'saved');
  read.resolve({ status: 200, body: { ns: 'settings-center', data: copy(SETTINGS_A) } });
  await loading;
  assert.equal(h.controller.getSnapshot().backendConnected, true);
  assert.equal(h.controller.getSnapshot().loadState, 'ready');
  assert.equal(h.controller.getSnapshot().saveState, 'idle');
});

for (const status of [403, 503]) {
  test(`a ${status} workspace read is an honest disconnected failure`, async () => {
    const h = harness({
      intercept: input => input.path.startsWith('/user/settings?')
        ? { status, body: { detail: 'Fixture unavailable' } } : undefined,
    });
    await h.controller.setSession(TOKEN_A);
    const snapshot = h.controller.getSnapshot();
    assert.equal(snapshot.backendConnected, false);
    assert.equal(snapshot.loadState, 'error');
    assert.notEqual(snapshot.saveState, 'saved');
    assert.ok(snapshot.error);
    assert.deepEqual(snapshot.settings, DEFAULTS);
  });

  test(`a ${status} workspace write never claims Saved or backend connected`, async () => {
    const h = harness({
      intercept: input => input.method === 'POST'
        ? { status, body: { detail: 'Fixture unavailable' } } : undefined,
    });
    await h.controller.setSession(TOKEN_A);
    h.controller.update('appearance', { theme: 'light' });
    assert.notEqual(h.controller.getSnapshot().saveState, 'saved');
    await h.clock.run();
    const snapshot = h.controller.getSnapshot();
    assert.equal(snapshot.saveState, 'error');
    assert.equal(snapshot.backendConnected, false);
    assert.ok(snapshot.error);
    assert.deepEqual(h.saved.get(TOKEN_A), SETTINGS_A);
  });
}

test('only an acknowledged settings-center write produces Saved', async () => {
  const h = harness();
  await h.controller.setSession(TOKEN_A);
  h.controller.update('appearance', { theme: 'light', animations: true });
  assert.notEqual(h.controller.getSnapshot().saveState, 'saved');
  await h.clock.run();
  const snapshot = h.controller.getSnapshot();
  assert.equal(snapshot.saveState, 'saved');
  assert.equal(snapshot.backendConnected, true);
  assert.equal(h.saved.get(TOKEN_A).appearance.theme, 'light');
  const workspaceRequests = h.requests.filter(r => r.path.startsWith('/user/settings'));
  assert.equal(workspaceRequests.length, 2);
  assert.deepEqual(workspaceRequests.map(r => [r.method, r.path]), [
    ['GET', '/user/settings?ns=settings-center'], ['POST', '/user/settings'],
  ]);
  assert.equal(workspaceRequests[1].body.ns, 'settings-center');
  assert.equal(workspaceRequests[1].accessToken, TOKEN_A);
});

test('unverified auth/me response never permits a workspace request', async () => {
  const h = harness({
    intercept: input => input.path === '/auth/me'
      ? { status: 401, body: { detail: 'Expired session' } } : undefined,
  });
  await h.controller.setSession(TOKEN_A);
  h.controller.update('appearance', { theme: 'dark' });
  await h.clock.run();
  assert.equal(h.controller.getSnapshot().identity, null);
  assert.equal(h.controller.getSnapshot().backendConnected, false);
  assert.equal(h.requests.some(r => r.path.startsWith('/user/settings')), false);
});

test('signing out invalidates both pending reads and queued saves', async () => {
  const h = harness();
  await h.controller.setSession(TOKEN_A);
  h.controller.update('appearance', { theme: 'private-a-theme' });
  await h.controller.setSession(null);
  await h.clock.run({ includeCancelled: true });
  assert.deepEqual(h.controller.getSnapshot().settings, DEFAULTS);
  assert.equal(h.controller.getSnapshot().identity, null);
  assert.equal(h.controller.getSnapshot().backendConnected, false);
  assert.equal(h.requests.filter(r => r.method === 'POST').length, 0);
});

test('disposing invalidates an outstanding workspace result and emits no later state', async () => {
  const read = deferred();
  const h = harness({ intercept: input => input.path.startsWith('/user/settings?') ? read.promise : undefined });
  const notifications = [];
  const unsubscribe = h.controller.subscribe(() => notifications.push(copy(h.controller.getSnapshot())));
  const loading = h.controller.setSession(TOKEN_A);
  await drain();
  h.controller.dispose();
  const countAtDisposal = notifications.length;
  read.resolve({ status: 200, body: { ns: 'settings-center', data: copy(SETTINGS_A) } });
  await loading;
  assert.equal(notifications.length, countAtDisposal);
  assert.equal(h.controller.getSnapshot().backendConnected, false);
  unsubscribe();
});

for (const body of [{}, { saved: false, ns: 'settings-center' }, { saved: true, ns: 'another-workspace' }]) {
  test(`a 200 write without a settings-center persistence acknowledgement is an error: ${JSON.stringify(body)}`, async () => {
    const h = harness({ intercept: input => input.method === 'POST' ? { status: 200, body } : undefined });
    await h.controller.setSession(TOKEN_A);
    h.controller.update('appearance', { theme: 'light' });
    await h.clock.run();
    assert.equal(h.controller.getSnapshot().saveState, 'error');
    assert.equal(h.controller.getSnapshot().backendConnected, false);
    assert.ok(h.controller.getSnapshot().error);
    assert.deepEqual(h.saved.get(TOKEN_A), SETTINGS_A);
  });
}

test('same-user token renewal preserves edits and saves them with the verified new token', async () => {
  const h = harness();
  await h.controller.setSession(TOKEN_A);
  h.controller.update('appearance', { theme: 'renewed-a-preference' });
  const edited = copy(h.controller.getSnapshot().settings);
  await h.controller.renewSession(TOKEN_A_RENEWED);
  assert.deepEqual(h.controller.getSnapshot().settings, edited);
  assert.deepEqual(h.controller.getSnapshot().identity, USER_A);
  assert.equal(h.controller.getSnapshot().backendConnected, true);
  await h.clock.run();
  assert.equal(h.controller.getSnapshot().saveState, 'saved');
  assert.equal(h.saved.get(TOKEN_A).appearance.theme, 'renewed-a-preference');
  const writes = h.requests.filter(r => r.method === 'POST');
  assert.equal(writes.length, 1);
  assert.equal(writes[0].accessToken, TOKEN_A_RENEWED);
  assert.deepEqual(h.requests.filter(r => r.path.startsWith('/user/settings?')).map(r => r.accessToken), [TOKEN_A]);
  const serialized = JSON.stringify(h.controller.getSnapshot());
  assert.equal(serialized.includes(TOKEN_A_RENEWED), false);
});

test('renewal for a different identity clears old settings and cannot apply old autosave to that account', async () => {
  const h = harness();
  await h.controller.setSession(TOKEN_A);
  h.controller.update('appearance', { theme: 'private-a-theme' });
  await h.controller.renewSession(TOKEN_B);
  await h.clock.run({ includeCancelled: true });
  const snapshot = h.controller.getSnapshot();
  assert.deepEqual(snapshot.settings, DEFAULTS);
  assert.equal(snapshot.identity, null);
  assert.equal(snapshot.backendConnected, false);
  assert.equal(snapshot.loadState, 'error');
  assert.ok(snapshot.error);
  assert.equal(h.requests.filter(r => r.method === 'POST').length, 0);
  assert.deepEqual(h.saved.get(TOKEN_B), SETTINGS_B);
});

test('failed token renewal clears authenticated state and stops queued autosave', async () => {
  const h = harness({
    intercept: input => input.accessToken === TOKEN_A_RENEWED && input.path === '/auth/me'
      ? { status: 403, body: { detail: 'Credential revoked' } } : undefined,
  });
  await h.controller.setSession(TOKEN_A);
  h.controller.update('appearance', { theme: 'private-a-theme' });
  await h.controller.renewSession(TOKEN_A_RENEWED);
  await h.clock.run({ includeCancelled: true });
  assert.deepEqual(h.controller.getSnapshot().settings, DEFAULTS);
  assert.equal(h.controller.getSnapshot().identity, null);
  assert.equal(h.controller.getSnapshot().backendConnected, false);
  assert.equal(h.requests.filter(r => r.method === 'POST').length, 0);
});

test('a delayed A token renewal cannot overwrite B identity or credentials', async () => {
  const aRenewal = deferred();
  const h = harness({
    intercept: input => input.accessToken === TOKEN_A_RENEWED && input.path === '/auth/me'
      ? aRenewal.promise : undefined,
  });
  await h.controller.setSession(TOKEN_A);
  const renewal = h.controller.renewSession(TOKEN_A_RENEWED);
  await h.controller.setSession(TOKEN_B);
  aRenewal.resolve({ status: 200, body: copy(USER_A) });
  await renewal;
  h.controller.update('appearance', { theme: 'b-preference' });
  await h.clock.run();
  assert.deepEqual(h.controller.getSnapshot().identity, USER_B);
  const writes = h.requests.filter(r => r.method === 'POST');
  assert.equal(writes.length, 1);
  assert.equal(writes[0].accessToken, TOKEN_B);
  assert.equal(h.saved.get(TOKEN_B).appearance.theme, 'b-preference');
});

test('autosave waits for an in-progress same-user renewal before choosing credentials', async () => {
  const verification = deferred();
  const h = harness({
    intercept: input => input.path === '/auth/me' && input.accessToken === TOKEN_A_RENEWED
      ? verification.promise : undefined,
  });
  await h.controller.setSession(TOKEN_A);
  h.controller.update('appearance', { theme: 'preference-during-renewal' });
  const renewal = h.controller.renewSession(TOKEN_A_RENEWED);
  await h.clock.run();
  assert.equal(h.requests.filter(r => r.method === 'POST').length, 0);
  verification.resolve({ status: 200, body: copy(USER_A) });
  await renewal;
  await drain();
  const writes = h.requests.filter(r => r.method === 'POST');
  assert.equal(writes.length, 1);
  assert.equal(writes[0].accessToken, TOKEN_A_RENEWED);
  assert.equal(h.saved.get(TOKEN_A).appearance.theme, 'preference-during-renewal');
});

test('a workspace read for another namespace is never accepted as connected', async () => {
  const h = harness({
    intercept: input => input.path.startsWith('/user/settings?')
      ? { status: 200, body: { ns: 'other-namespace', data: copy(SETTINGS_A) } } : undefined,
  });
  await h.controller.setSession(TOKEN_A);
  assert.equal(h.controller.getSnapshot().loadState, 'error');
  assert.equal(h.controller.getSnapshot().backendConnected, false);
  assert.deepEqual(h.controller.getSnapshot().settings, DEFAULTS);
});

test('a repeated session after a failed write rechecks the server instead of retaining an unproven connection', async () => {
  let denyWrites = true;
  const h = harness({
    intercept: input => denyWrites && input.method === 'POST'
      ? { status: 503, body: { detail: 'Fixture unavailable' } } : undefined,
  });
  await h.controller.setSession(TOKEN_A);
  h.controller.update('appearance', { theme: 'unsaved-preference' });
  await h.clock.run();
  assert.equal(h.controller.getSnapshot().backendConnected, false);
  denyWrites = false;
  await h.controller.setSession(TOKEN_A);
  assert.deepEqual(h.controller.getSnapshot().settings, SETTINGS_A);
  assert.equal(h.controller.getSnapshot().backendConnected, true);
  assert.equal(h.controller.getSnapshot().saveState, 'idle');
});

test('an async form callback captured under A cannot write after B becomes ready', async () => {
  const h = harness();
  await h.controller.setSession(TOKEN_A);
  const aSubmitFinished = h.controller.captureUpdater();
  await h.controller.setSession(TOKEN_B);
  aSubmitFinished('profile', { fullName: 'A delayed form result' });
  await h.clock.run();
  assert.equal(h.requests.filter(r => r.method === 'POST').length, 0);
  assert.deepEqual(h.controller.getSnapshot().settings, SETTINGS_B);
  assert.deepEqual(h.saved.get(TOKEN_B), SETTINGS_B);
});

test('an old form callback stays invalid after sign-out and a fresh login to the same account', async () => {
  const h = harness();
  await h.controller.setSession(TOKEN_A);
  const previousLoginCallback = h.controller.captureUpdater();
  await h.controller.setSession(null);
  await h.controller.setSession(TOKEN_A);
  previousLoginCallback('appearance', { theme: 'stale-previous-login' });
  await h.clock.run();
  assert.equal(h.requests.filter(r => r.method === 'POST').length, 0);
  assert.deepEqual(h.controller.getSnapshot().settings, SETTINGS_A);
});

test('a current form callback survives same-owner token renewal and uses the verified new token', async () => {
  const h = harness();
  await h.controller.setSession(TOKEN_A);
  const currentLoginCallback = h.controller.captureUpdater();
  await h.controller.renewSession(TOKEN_A_RENEWED);
  currentLoginCallback('appearance', { theme: 'current-form-with-renewed-token' });
  await h.clock.run();
  const writes = h.requests.filter(r => r.method === 'POST');
  assert.equal(writes.length, 1);
  assert.equal(writes[0].accessToken, TOKEN_A_RENEWED);
  assert.equal(h.saved.get(TOKEN_A).appearance.theme, 'current-form-with-renewed-token');
});
