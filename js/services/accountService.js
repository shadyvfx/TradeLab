// TradeLab accounts: sign-up, sign-in, sign-out, the email checker, and saving
// each account's progress.
//
// Accounts always go to the TradeLab server, which saves them in the SQLite
// user database (tradelab.db), even while the rest of the app runs on mock
// data. The Flask server can also provide the tutor API from the same origin.
// The server checks the rules again, checks that the email's domain can
// receive mail, hashes the password and signs the browser in with an
// HttpOnly cookie.
//
// Progress belongs to whoever is using TradeLab. Signed in, the store holds
// that account's lessons, simulated trades, journal, tutor conversation and
// settings; changes are saved to the server (PUT /api/me/data) and loaded at
// the next sign-in. Signed out, the store holds a separate guest copy kept in
// this browser. An account with nothing saved yet takes over the progress in
// this browser, and the guest copy starts fresh.
//
//   POST   /api/users        { username, email, password } -> 201 { user }
//   POST   /api/session      { login, password }           -> { user }
//   GET    /api/session                                    -> { user } (null when signed out)
//   DELETE /api/session                                    -> 204
//   POST   /api/email-check  { email }                     -> { status, ok, message, suggestion }
//   GET    /api/me/data                                    -> { data, updatedAt }
//   PUT    /api/me/data      { data }                      -> { updatedAt }

import { isMock } from '../config.js';
import { api, ApiError } from './apiClient.js';
import { getState, setState, updateSlice, getOwner, switchOwner, personalSnapshot, readGuestData, resetGuestData } from '../state.js';
import { validateRegistration, validateSignIn } from '../core/accounts.js';
import { refreshQuotes } from './marketDataService.js';
import { cancelPending } from './tutorService.js';

// TradeLab's server (Flask on a production server). On Windows, the command is python serve.py.
export const SERVER_COMMAND = 'python3 serve.py';
export const SERVER_URL = 'http://127.0.0.1:5001';

export class AccountError extends Error {
  /**
   * @param {string} message shown in the status banner
   * @param {{fields?: Object<string,string>, suggestions?: Object<string,string>, code?: string, unavailable?: boolean}} [details]
   */
  constructor(message, { fields = {}, suggestions = {}, code = 'error', unavailable = false } = {}) {
    super(message);
    this.name = 'AccountError';
    this.fields = fields;
    this.suggestions = suggestions;
    this.code = code;
    this.unavailable = unavailable;
  }
}

function setUser(user, userStatus) {
  setState((state) => ({ ...state, runtime: { ...state.runtime, user, userStatus } }), 'account/status');
}

function setSync(patch) {
  setState((state) => ({ ...state, runtime: { ...state.runtime, sync: { ...state.runtime.sync, ...patch } } }), 'account/sync');
}

export function setGuestLimits(guestLimits) {
  setState((state) => ({ ...state, runtime: { ...state.runtime, guestLimits } }), 'guest/limits');
}

/** Tells the app that different data is now showing, so the current page rebuilds. */
function announceOwnerChange() {
  if (typeof window !== 'undefined' && typeof CustomEvent === 'function') window.dispatchEvent(new CustomEvent('tradelab:owner-changed'));
}

function isUnavailable(error) {
  // The Node dev server answers 503 accounts_unavailable; plain static hosts
  // answer 404 or 405; nothing answers at all when no server is running.
  return ['network', 'timeout', 'accounts_unavailable'].includes(error.code) || [404, 405, 501, 502, 503, 504].includes(error.status);
}

/** Turns a failed request into an AccountError the page can show. */
function toAccountError(error) {
  if (error instanceof AccountError) return error;
  if (error instanceof ApiError) {
    if (isUnavailable(error)) {
      setUser(null, 'unavailable');
      return new AccountError(`Can't reach the account server, so nothing was saved. Run ${SERVER_COMMAND} and open ${SERVER_URL}, then try again.`, {
        code: 'accounts_unavailable',
        unavailable: true,
      });
    }
    const listed = error.details?.fields;
    const fields = listed && typeof listed === 'object' ? listed : error.field ? { [error.field]: error.message } : {};
    const suggestions = error.details?.suggestions && typeof error.details.suggestions === 'object' ? error.details.suggestions : {};
    return new AccountError(error.message, { fields, suggestions, code: error.code });
  }
  return new AccountError(error?.message || 'Something went wrong. Try again.');
}

// ---------------------------------------------------------------------------
// Saving the signed-in account's progress

const SAVE_DELAY_MS = 800;
let saveTimer = null;
let pendingSnapshot = null;
let inFlight = null;
let guestResetPending = false;

/** Called by the store (persistNow) whenever the signed-in account's data changes. */
function queueSave(snapshot, { immediate = false } = {}) {
  pendingSnapshot = snapshot;
  clearTimeout(saveTimer);
  if (immediate) flushSave({ keepalive: true }).catch(() => {});
  else saveTimer = setTimeout(() => flushSave().catch(() => {}), SAVE_DELAY_MS);
}

/** Sends the latest changes now. Resolves when the server has them. */
async function flushSave({ keepalive = false } = {}) {
  clearTimeout(saveTimer);
  saveTimer = null;
  while (inFlight) await inFlight.catch(() => {}); // one save at a time, in order
  if (!pendingSnapshot) return;
  const snapshot = pendingSnapshot;
  pendingSnapshot = null;
  setSync({ status: 'saving' });
  inFlight = api.put('/me/data', { data: snapshot }, { keepalive });
  try {
    await inFlight;
    setSync({ status: 'saved', savedAt: Date.now() });
    if (guestResetPending) {
      // The account now has this browser's progress, so the guest copy starts over.
      resetGuestData();
      guestResetPending = false;
    }
  } catch (error) {
    if (!pendingSnapshot) pendingSnapshot = snapshot; // try again with the next change
    setSync({ status: 'error' });
    throw error;
  } finally {
    inFlight = null;
  }
}

/**
 * Shows the account's saved progress, or gives the account this browser's
 * progress if it has none yet. Returns whether it took over this browser's progress.
 */
async function adoptAccount(user) {
  if (!isMock()) {
    // In API mode the backend keeps everything and scopes it by the session.
    setUser(user, 'signed-in');
    return false;
  }
  const { data } = await api.get('/me/data');
  cancelPending(); // a tutor reply on its way belongs to the previous person
  if (data) {
    switchOwner({ kind: 'user', id: user.id }, { data, save: queueSave });
    setSync({ status: 'saved', savedAt: Date.now() });
  } else {
    switchOwner({ kind: 'user', id: user.id }, { save: queueSave });
    guestResetPending = true;
    pendingSnapshot = personalSnapshot();
    await flushSave().catch(() => {}); // if this fails, the next change tries again
  }
  setUser(user, 'signed-in');
  if (getState().runtime.instruments.length) await refreshQuotes(); // prices for this account's simulated day
  announceOwnerChange();
  return !data;
}

// ---------------------------------------------------------------------------
// Public API

/**
 * Asks the server who is signed in and loads that account's progress. Called
 * at startup before the market loads; never throws.
 * The Node dev server, which has no user database, answers { unavailable: true }.
 */
export async function loadAccountSession() {
  let session;
  try {
    session = await api.get('/session');
  } catch {
    setUser(null, 'unavailable');
    return;
  }
  if (session.unavailable) {
    setUser(null, 'unavailable');
    return;
  }
  setGuestLimits(session.guestLimits || null);
  if (!session.user) {
    setUser(null, 'signed-out');
    if (session.guestLimits?.simulationDay !== undefined) {
      updateSlice(
        'market',
        (market) => ({ ...market, day: session.guestLimits.simulationDay }),
        'guest/simulation-clock',
      );
    }
    return;
  }
  try {
    await adoptAccount(session.user);
  } catch {
    // Signed in, but the account's progress couldn't be loaded. Keep showing
    // this browser's guest data rather than mixing the two.
    setUser(null, 'unavailable');
  }
}

export async function reserveGuestSimulationDays(days) {
  try {
    const result = await api.post('/guest/simulation/advance', { days });
    if (result.limits) setGuestLimits(result.limits);
    return result;
  } catch (error) {
    if (error instanceof ApiError && error.code === 'guest_simulation_limit') {
      setGuestLimits({ ...(getState().runtime.guestLimits || {}), simulationDaysRemaining: 0 });
    }
    throw error;
  }
}

export async function reserveGuestJournalEntry() {
  try {
    const result = await api.post('/guest/journal/entries', {});
    if (result.limits) setGuestLimits(result.limits);
    return result.limits;
  } catch (error) {
    if (error instanceof ApiError && error.code === 'guest_journal_limit') {
      setGuestLimits({ ...(getState().runtime.guestLimits || {}), journalEntriesRemaining: 0 });
    }
    throw error;
  }
}

export async function syncGuestSimulationClock() {
  const result = await api.get('/guest/limits');
  if (result.authenticated === false && result.limits) {
    setGuestLimits(result.limits);
    if (getState().market.day !== result.limits.simulationDay) {
      updateSlice(
        'market',
        (market) => ({ ...market, day: result.limits.simulationDay }),
        'guest/simulation-clock',
      );
      await refreshQuotes();
    }
  }
  return result;
}

async function signedIn(user, verb) {
  let claimed;
  try {
    claimed = await adoptAccount(user);
  } catch (error) {
    throw new AccountError(`You're ${verb}, but your saved progress couldn't be loaded. Reload the page to try again.`, {
      code: 'progress_unavailable',
    });
  }
  return { user, claimed };
}

/**
 * Creates an account and signs in.
 * @param {{username:string, email:string, password:string}} input
 * @returns {Promise<{user: object, claimed: boolean}>} `claimed`: the account took over this browser's progress
 * @throws {AccountError} with `fields` (and `suggestions`) for problems with specific fields
 */
export async function register(input) {
  const check = validateRegistration(input);
  if (!check.ok) throw new AccountError('Some fields need attention.', { fields: check.errors, code: 'validation_failed' });
  let response;
  try {
    response = await api.post('/users', check.value);
  } catch (error) {
    throw toAccountError(error);
  }
  return signedIn(response.user, 'signed up');
}

/**
 * Signs in with a username or email address.
 * @param {{login:string, password:string}} input
 * @returns {Promise<{user: object, claimed: boolean}>}
 */
export async function signIn(input) {
  const check = validateSignIn(input);
  if (!check.ok) throw new AccountError('Some fields need attention.', { fields: check.errors, code: 'validation_failed' });
  let response;
  try {
    response = await api.post('/session', check.value);
  } catch (error) {
    throw toAccountError(error);
  }
  return signedIn(response.user, 'signed in');
}

/** Saves the account's last changes, signs out, and switches to this browser's guest data. */
export async function signOut() {
  try {
    if (getOwner().kind === 'user') await flushSave();
    await api.delete('/session');
  } catch (error) {
    throw toAccountError(error);
  }
  clearTimeout(saveTimer);
  pendingSnapshot = null;
  guestResetPending = false;
  cancelPending();
  if (getOwner().kind === 'user') {
    switchOwner({ kind: 'guest' }, { data: readGuestData(), flush: false });
    if (getState().runtime.instruments.length) await refreshQuotes();
  }
  setUser(null, 'signed-out');
  setSync({ status: 'idle', savedAt: null });
  announceOwnerChange();
}

/**
 * Asks the server whether an address can receive email (typos, made-up
 * domains, domains without mail servers).
 * @returns {Promise<{status:'ok'|'invalid'|'unverified', ok:boolean, message:string, suggestion:string|null}|null>}
 *   null when the server can't be asked
 */
export async function checkEmail(email, { signal } = {}) {
  try {
    return await api.post('/email-check', { email }, { signal, timeoutMs: 8000 });
  } catch (error) {
    if (error?.name === 'AbortError') throw error;
    return null;
  }
}

/** "@username", or the name and email of accounts made before usernames existed. */
export function accountLabel(user) {
  if (!user) return '';
  return user.username ? `@${user.username}` : user.displayName || user.email;
}

/** Two characters for the avatar: "AY" for @ay_0b, "TP" for an older account named "Test Person". */
export function accountInitials(user) {
  const alphanumeric = (text) => [...String(text || '')].filter((char) => /[\p{L}\p{N}]/u.test(char));
  const words = !user?.username && user?.displayName ? user.displayName.trim().split(/\s+/u) : [];
  const letters = words.length > 1 ? words.slice(0, 2).map((word) => alphanumeric(word)[0] || '') : alphanumeric(user?.username || user?.displayName || user?.email?.split('@')[0]).slice(0, 2);
  return letters.join('').toUpperCase() || '?';
}
