// Account: create a TradeLab account or sign in.
//
// The form checks the rules as you type (core/accounts.js): a username with
// no spaces, a valid email, and a password of at least 8 characters with a
// capital letter and a special character. While you type an email, the
// server's email checker says whether that address can receive mail (it
// catches made-up domains and typos like gnail.com). On submit the server
// checks everything again, saves the account in the user database and signs
// you in. Every result, success or error, appears in the status banner at the
// top of the form.

import { html, raw, render, on, createDisposer, $ } from '../utils/dom.js';
import { getState, watch } from '../state.js';
import { formatDate, relativeTime } from '../utils/format.js';
import { icon } from '../components/icons.js';
import { fieldError } from '../components/ui.js';
import { announce } from '../components/notifications.js';
import { passwordChecks, usernameError, emailError, passwordError, normalizeEmail } from '../core/accounts.js';
import {
  register,
  signIn,
  signOut,
  checkEmail,
  loadAccountSession,
  accountLabel,
  accountInitials,
  AccountError,
  SERVER_COMMAND,
  SERVER_URL,
} from '../services/accountService.js';

const FIELD_LABELS = {
  username: 'Username',
  email: 'Email',
  password: 'Password',
  login: 'Username or email',
  signinPassword: 'Password',
};

// Which input each server field name belongs to, per form.
const FIELD_IDS = {
  signup: { username: 'signup-username', email: 'signup-email', password: 'signup-password' },
  signin: { login: 'signin-login', password: 'signin-password' },
};

const LIVE_RULES = { username: usernameError, email: emailError, password: passwordError };

const ui = freshUi('signup');

function freshUi(mode) {
  return {
    mode,
    values: { username: '', email: '', password: '', login: '', signinPassword: '' },
    serverErrors: {},
    suggestions: {}, // field -> a corrected value the server suggested ("ay@gmail.com")
    attempted: false,
    busy: false,
    showPassword: false,
    status: null, // { tone: 'success' | 'error' | 'warn' | 'info', title, text, items: [{ field, message }] }
    emailCheck: { state: 'idle', email: '', result: null }, // the live email checker
  };
}

// Email checker results already fetched, by address.
const emailResults = new Map();
const EMAIL_CHECK_DELAY_MS = 600;
const domainOf = (email) => email.slice(email.lastIndexOf('@') + 1);

// ---------------------------------------------------------------------------
// Pieces

function statusBanner(status) {
  if (!status) return '';
  const iconName = { success: 'check-circle', error: 'alert', warn: 'alert', info: 'info' }[status.tone] || 'info';
  return html`<div class="callout callout--${status.tone} account-status__box" role="${status.tone === 'error' ? 'alert' : 'status'}" tabindex="-1" id="account-status-box">
    ${icon(iconName, { className: 'callout__icon' })}
    <div>
      <p class="callout__title">${status.title}</p>
      ${status.text ? html`<p>${status.text}</p>` : ''}
      ${status.items?.length
        ? html`<ul class="account-status__list">${status.items.map(
            (item) => html`<li><a class="link" href="#${item.id}" data-focus-field="${item.id}">${item.label}</a>: ${item.message}</li>`,
          )}</ul>`
        : ''}
      ${status.retry ? html`<div class="callout__actions"><button type="button" class="btn btn--secondary btn--sm" data-action="recheck">${icon('reset')}Check again</button></div>` : ''}
    </div>
  </div>`;
}

function checklist(password) {
  return passwordChecks(password).map(
    (check) => html`<li class="checklist__item${check.met ? ' is-met' : ''}">
      ${icon(check.met ? 'check-circle' : 'circle', { className: 'checklist__icon' })}<span>${check.label}</span><span class="sr-only">${check.met ? ', done' : ', not yet'}</span>
    </li>`,
  );
}

function textField({ id, name, label, type = 'text', hint = '', autocomplete, value, extra = '' }) {
  return html`<div class="field">
    <label class="field__label" for="${id}">${label}</label>
    <input class="input" id="${id}" name="${name}" type="${type}" autocomplete="${autocomplete}" value="${value}" ${raw(extra)}
      aria-describedby="${hint ? `${id}-hint ` : ''}${id}-error" aria-invalid="false" />
    ${hint ? html`<p class="field__hint" id="${id}-hint">${hint}</p>` : ''}
    <div id="${id}-error"></div>
  </div>`;
}

function revealLabel() {
  return ui.showPassword ? 'Hide password' : 'Show password';
}

function passwordField({ id, name, autocomplete, value, withRules }) {
  return html`<div class="field">
    <label class="field__label" for="${id}">Password</label>
    <div class="input-reveal">
      <input class="input" id="${id}" name="${name}" type="${ui.showPassword ? 'text' : 'password'}" autocomplete="${autocomplete}" value="${value}"
        autocapitalize="none" autocorrect="off" spellcheck="false" aria-describedby="${withRules ? `${id}-rules ` : ''}${id}-error" aria-invalid="false" />
      <button type="button" class="btn btn--ghost btn--sm input-reveal__btn" data-toggle-password aria-controls="${id}" aria-label="${revealLabel()}">${ui.showPassword ? 'Hide' : 'Show'}</button>
    </div>
    ${withRules ? html`<ul class="checklist" id="${id}-rules" aria-label="Password requirements">${checklist(value)}</ul>` : ''}
    <div id="${id}-error"></div>
  </div>`;
}

function signupForm() {
  return html`<form class="account-form" id="signup-form" novalidate>
    ${textField({
      id: 'signup-username',
      name: 'username',
      label: 'Username',
      hint: 'No spaces or @, up to 30 characters. You sign in with it.',
      autocomplete: 'username',
      value: ui.values.username,
      extra: 'autocapitalize="none" autocorrect="off" spellcheck="false"',
    })}
    <div class="field">
      <label class="field__label" for="signup-email">Email</label>
      <input class="input" id="signup-email" name="email" type="email" autocomplete="email" value="${ui.values.email}"
        inputmode="email" autocapitalize="none" autocorrect="off" spellcheck="false" aria-describedby="signup-email-error" aria-invalid="false" />
      <div class="email-check" id="signup-email-error" aria-live="polite"></div>
    </div>
    ${passwordField({ id: 'signup-password', name: 'password', autocomplete: 'new-password', value: ui.values.password, withRules: true })}
    <button type="submit" class="btn btn--primary btn--block" id="signup-submit" ${ui.busy ? 'disabled' : ''}>${ui.busy ? 'Creating account…' : 'Create account'}</button>
    <p class="account-form__alt small">Already have an account? <a class="link" href="#/account?mode=signin">Sign in</a></p>
  </form>`;
}

function signinForm() {
  return html`<form class="account-form" id="signin-form" novalidate>
    ${textField({
      id: 'signin-login',
      name: 'login',
      label: 'Username or email',
      autocomplete: 'username',
      value: ui.values.login,
      extra: 'autocapitalize="none" autocorrect="off" spellcheck="false"',
    })}
    ${passwordField({ id: 'signin-password', name: 'signinPassword', autocomplete: 'current-password', value: ui.values.signinPassword, withRules: false })}
    <button type="submit" class="btn btn--primary btn--block" id="signin-submit" ${ui.busy ? 'disabled' : ''}>${ui.busy ? 'Signing in…' : 'Sign in'}</button>
    <p class="account-form__alt small">New to TradeLab? <a class="link" href="#/account">Create an account</a></p>
  </form>`;
}

function signedOutCard() {
  const signup = ui.mode === 'signup';
  return html`<section class="panel account-card" aria-labelledby="account-title">
    <div class="panel__head account-card__head">
      <div>
        <h2 class="panel__title" id="account-title">${signup ? 'Create your TradeLab account' : 'Sign in to TradeLab'}</h2>
        <p class="panel__subtitle">${signup ? 'Choose a username and password to sign in with.' : 'Use the username or email you signed up with.'}</p>
      </div>
    </div>
    <div class="panel__body stack">
      <nav class="seg seg--block account-tabs" aria-label="Account forms">
        <a class="seg__item seg__link" href="#/account" aria-current="${signup ? 'page' : 'false'}">Create account</a>
        <a class="seg__item seg__link" href="#/account?mode=signin" aria-current="${signup ? 'false' : 'page'}">Sign in</a>
      </nav>
      <div class="account-status" id="account-status">${statusBanner(ui.status)}</div>
      ${signup ? signupForm() : signinForm()}
    </div>
  </section>`;
}

function syncText(sync) {
  if (sync.status === 'saving') return html`<span class="dots" aria-hidden="true"><i></i><i></i><i></i></span>Saving your progress…`;
  if (sync.status === 'saved') return html`${icon('check-circle')}Progress saved to your account ${relativeTime(sync.savedAt)}.`;
  if (sync.status === 'error') return html`${icon('alert')}Couldn't save your latest progress. TradeLab tries again with your next change.`;
  return html`${icon('check-circle')}Your progress saves to this account automatically.`;
}

function signedInCard(user) {
  return html`<section class="panel account-card" aria-labelledby="account-title">
    <div class="panel__head account-card__head">
      <div>
        <h2 class="panel__title" id="account-title">Your TradeLab account</h2>
        <p class="panel__subtitle">You're signed in on this browser.</p>
      </div>
    </div>
    <div class="panel__body stack">
      <div class="account-status" id="account-status">${statusBanner(ui.status)}</div>
      <div class="account-profile">
        <span class="avatar avatar--lg" aria-hidden="true">${accountInitials(user)}</span>
        <div class="account-profile__text">
          <p class="account-profile__name">${accountLabel(user)}</p>
          <p class="small muted">${user.email}</p>
        </div>
      </div>
      <p class="account-sync small account-sync--${getState().runtime.sync.status}" id="account-sync" role="status">${syncText(getState().runtime.sync)}</p>
      <dl class="kv">
        <div class="kv__row"><dt class="kv__key">Username</dt><dd class="kv__val">${user.username || html`<span class="muted">None yet (account made before usernames)</span>`}</dd></div>
        <div class="kv__row"><dt class="kv__key">Email</dt><dd class="kv__val account-profile__email">${user.email}</dd></div>
        <div class="kv__row"><dt class="kv__key">Member since</dt><dd class="kv__val">${formatDate(String(user.createdAt || '').slice(0, 10), 'medium')}</dd></div>
      </dl>
      <div class="cluster">
        <a class="btn btn--primary" href="#/">Go to Overview</a>
        <button type="button" class="btn btn--secondary" data-action="sign-out" ${ui.busy ? 'disabled' : ''}>${icon('logout')}${ui.busy ? 'Signing out…' : 'Sign out'}</button>
      </div>
    </div>
  </section>`;
}

function aboutPanel() {
  return html`<aside class="panel account-about" aria-labelledby="account-about-title">
    <div class="panel__head panel__head--plain"><h2 class="panel__title" id="account-about-title">How accounts work</h2></div>
    <div class="panel__body">
      <ul class="account-about__list small">
        <li>${icon('shield')}<p><strong>Your password is never stored.</strong> The TradeLab server keeps a salted PBKDF2-SHA256 hash of it in the user database, next to your username and email.</p></li>
        <li>${icon('user')}<p><strong>Sign in with your username or email.</strong> Usernames are unique, and capital letters don't make a new one: Ayoub and ayoub are the same.</p></li>
        <li>${icon('check-circle')}<p><strong>Your email gets checked.</strong> The server makes sure its domain exists and can receive mail, and catches typos like gnail.com.</p></li>
        <li>${icon('paper', { className: 'account-about__hatch' })}<p><strong>Your progress belongs to your account.</strong> Lessons, simulated trades, the journal, tutor chats and settings save to your account and come back when you sign in. Signed out, TradeLab shows a separate guest copy.</p></li>
      </ul>
    </div>
  </aside>`;
}

function page(state) {
  const { user, userStatus } = state.runtime;
  return html`<div class="page account">
    <div class="account-layout">
      ${userStatus === 'signed-in' && user ? signedInCard(user) : signedOutCard()}
      ${aboutPanel()}
    </div>
  </div>`;
}

// ---------------------------------------------------------------------------
// Behavior

const unavailableStatus = () => ({
  tone: 'warn',
  title: "The account server isn't running",
  text: html`Sign-up and sign-in need the TradeLab server, which also connects the AI tutor to this website. Run <code>${SERVER_COMMAND}</code> in the TradeLab folder (on Windows, <code>python serve.py</code>) and open <code>${SERVER_URL}</code>, then check again.`,
  retry: true,
});

function fieldsFor(mode) {
  return mode === 'signup' ? ['username', 'email', 'password'] : ['login', 'signinPassword'];
}

function emailLine(tone, message, suggestion = null) {
  const iconName = { good: 'check-circle', bad: 'alert' }[tone];
  return html`<p class="email-check__line email-check__line--${tone}" id="signup-email-error-text">
    ${tone === 'busy' ? html`<span class="dots" aria-hidden="true"><i></i><i></i><i></i></span>` : iconName ? icon(iconName) : ''}<span>${message}</span>
    ${suggestion ? html`<button type="button" class="btn btn--secondary btn--sm email-check__use" data-use-email="${suggestion}">Use ${suggestion}</button>` : ''}
  </p>`;
}

/** What the email checker line says right now, and whether the address is a problem. */
function emailFeedback() {
  const value = ui.values.email;
  if (ui.serverErrors.email) return { content: emailLine('bad', ui.serverErrors.email, ui.suggestions.email), invalid: true };
  const formatProblem = emailError(value);
  if (formatProblem) {
    return ui.attempted ? { content: emailLine('bad', formatProblem), invalid: true } : { content: emailLine('hint', "We'll check that this address can receive email."), invalid: false };
  }
  const check = ui.emailCheck;
  if (check.email === normalizeEmail(value)) {
    if (check.state === 'checking') return { content: emailLine('busy', `Checking ${domainOf(check.email)}…`), invalid: false };
    const result = check.result;
    if (result?.status === 'invalid') return { content: emailLine('bad', result.message, result.suggestion), invalid: true };
    if (result?.status === 'ok') return { content: emailLine('good', result.message), invalid: false };
    if (result) return { content: emailLine('hint', result.message), invalid: false };
  }
  return { content: emailLine('hint', "We'll check that this address can receive email."), invalid: false };
}

/**
 * The message to show under a field right now, or ''. Messages appear after
 * the first submit and then update as you type. (Showing them when a field
 * loses focus would move the button while it's being clicked.)
 */
function visibleError(field) {
  if (ui.serverErrors[field]) return ui.serverErrors[field];
  if (!ui.attempted) return '';
  const value = ui.values[field];
  if (ui.mode === 'signup') return LIVE_RULES[field](value);
  if (field === 'login') return value.trim() ? '' : 'Enter your username or email.';
  return value ? '' : 'Enter your password.';
}

function inputId(field) {
  return ui.mode === 'signup' ? FIELD_IDS.signup[field] : field === 'login' ? 'signin-login' : 'signin-password';
}

function paintField(root, field) {
  const id = inputId(field);
  const input = $(`#${id}`, root);
  if (!input) return;
  if (field === 'email' && ui.mode === 'signup') {
    const { content, invalid } = emailFeedback();
    input.setAttribute('aria-invalid', invalid ? 'true' : 'false');
    render($('#signup-email-error', root), content);
    return;
  }
  const message = visibleError(field);
  input.setAttribute('aria-invalid', message ? 'true' : 'false');
  render($(`#${id}-error`, root), fieldError(`${id}-error-text`, message));
  if (field === 'password' && ui.mode === 'signup') render($('#signup-password-rules', root), html`${checklist(ui.values.password)}`);
}

function paintStatus(root) {
  const container = $('#account-status', root);
  if (container) render(container, statusBanner(ui.status));
}

function focusStatus(root) {
  requestAnimationFrame(() => $('#account-status-box', root)?.focus());
}

/** Maps server field names onto this form's fields ("password" is "signinPassword" when signing in). */
function localField(field) {
  return ui.mode === 'signin' && field === 'password' ? 'signinPassword' : field;
}

function errorStatus(title, error) {
  const fields = Object.entries(error.fields || {}).map(([field, message]) => [localField(field), message]);
  return {
    tone: 'error',
    title,
    text: fields.length ? (fields.length === 1 ? 'Fix this and try again:' : `Fix these ${fields.length} things and try again:`) : error.message,
    items: fields.map(([field, message]) => ({ id: inputId(field), label: FIELD_LABELS[field] || field, message })),
  };
}

export default {
  id: 'account',
  // Sign-in and sign-out happen here, and this page updates itself.
  keepOnOwnerChange: true,
  mount(root, ctx) {
    const disposer = createDisposer();
    Object.assign(ui, freshUi(ctx.query.mode === 'signin' ? 'signin' : 'signup'));
    if (getState().runtime.userStatus === 'unavailable') ui.status = unavailableStatus();

    const paint = ({ keepFocus = true } = {}) => {
      const active = document.activeElement;
      const focusedId = keepFocus && active && root.contains(active) ? active.id : null;
      const caret = focusedId && 'selectionStart' in active ? [active.selectionStart, active.selectionEnd] : null;
      render(root, page(getState()));
      for (const field of fieldsFor(ui.mode)) paintField(root, field);
      if (focusedId) {
        const element = $(`#${focusedId}`, root);
        element?.focus({ preventScroll: true });
        if (caret && element?.setSelectionRange) {
          try {
            element.setSelectionRange(caret[0], caret[1]);
          } catch {
            // Email inputs don't support selection ranges.
          }
        }
      }
    };

    const setMode = (mode) => {
      if (ui.mode === mode) return;
      const keepStatus = ui.status?.tone === 'warn' ? ui.status : null;
      Object.assign(ui, { ...freshUi(mode), values: ui.values, showPassword: ui.showPassword, status: keepStatus });
      paint({ keepFocus: false });
    };

    paint({ keepFocus: false });

    // The email checker: asks the server about the address once you pause typing.
    let emailTimer = null;
    let emailRequest = null;
    const stopEmailCheck = () => {
      clearTimeout(emailTimer);
      emailRequest?.abort();
      emailRequest = null;
    };
    disposer.add(stopEmailCheck);

    const runEmailCheck = async () => {
      const email = normalizeEmail(ui.values.email);
      if (emailError(email)) return;
      if (emailResults.has(email)) {
        ui.emailCheck = { state: 'done', email, result: emailResults.get(email) };
        paintField(root, 'email');
        return;
      }
      ui.emailCheck = { state: 'checking', email, result: null };
      paintField(root, 'email');
      emailRequest = new AbortController();
      try {
        const result = await checkEmail(email, { signal: emailRequest.signal });
        if (result && result.status !== 'unverified') emailResults.set(email, result);
        if (normalizeEmail(ui.values.email) !== email) return;
        ui.emailCheck = { state: 'done', email, result };
      } catch {
        return; // replaced by a newer check
      }
      paintField(root, 'email');
    };

    const scheduleEmailCheck = () => {
      stopEmailCheck();
      ui.emailCheck = { state: 'idle', email: '', result: null };
      if (!emailError(ui.values.email)) emailTimer = setTimeout(runEmailCheck, EMAIL_CHECK_DELAY_MS);
    };

    // Typing: update the value, then the live checks for that field only, so
    // the caret and focus never jump.
    disposer.add(
      on(root, 'input', '.account-form input', (_event, input) => {
        const field = input.name;
        ui.values[field] = input.value;
        delete ui.serverErrors[localField(field)];
        delete ui.serverErrors[field];
        delete ui.suggestions[field];
        if (field === 'email' && ui.mode === 'signup') scheduleEmailCheck();
        paintField(root, field);
      }),
    );

    // "Use ay@gmail.com": take the suggested address and check it.
    disposer.add(
      on(root, 'click', '[data-use-email]', (_event, button) => {
        const input = $('#signup-email', root);
        ui.values.email = button.dataset.useEmail;
        input.value = ui.values.email;
        delete ui.serverErrors.email;
        delete ui.suggestions.email;
        stopEmailCheck();
        runEmailCheck();
        input.focus();
      }),
    );

    disposer.add(
      on(root, 'click', '[data-toggle-password]', (_event, button) => {
        ui.showPassword = !ui.showPassword;
        const input = $(`#${button.getAttribute('aria-controls')}`, root);
        input.type = ui.showPassword ? 'text' : 'password';
        button.textContent = ui.showPassword ? 'Hide' : 'Show';
        button.setAttribute('aria-label', revealLabel());
        announce(ui.showPassword ? 'Password shown.' : 'Password hidden.');
      }),
    );

    disposer.add(
      on(root, 'click', '[data-focus-field]', (event, link) => {
        event.preventDefault();
        $(`#${link.dataset.focusField}`, root)?.focus();
      }),
    );

    disposer.add(
      on(root, 'click', '[data-action="recheck"]', async (_event, button) => {
        button.disabled = true;
        await loadAccountSession();
        const status = getState().runtime.userStatus;
        ui.status = status === 'unavailable' ? unavailableStatus() : { tone: 'success', title: 'Connected to the account server', text: 'You can create an account or sign in now.' };
        paint({ keepFocus: false });
        focusStatus(root);
      }),
    );

    const submit = async (mode) => {
      if (ui.busy) return;
      stopEmailCheck(); // the server checks the email again on submit
      if (ui.emailCheck.state === 'checking') ui.emailCheck = { state: 'idle', email: '', result: null };
      ui.attempted = true;
      ui.serverErrors = {};
      ui.suggestions = {};
      const values = ui.values;
      ui.busy = true;
      ui.status = null;
      paint();
      try {
        const { user, claimed } =
          mode === 'signup'
            ? await register({ username: values.username, email: values.email, password: values.password })
            : await signIn({ login: values.login, password: values.signinPassword });
        Object.assign(ui, freshUi(mode));
        const label = accountLabel(user);
        if (mode === 'signup') {
          ui.status = { tone: 'success', title: 'Account created', text: `Welcome to TradeLab, ${label}. You're signed in, and the progress you made in this browser now belongs to your account.` };
        } else if (claimed) {
          ui.status = { tone: 'success', title: 'Signed in', text: `Welcome back, ${label}. This account had no saved progress yet, so it now has the progress from this browser.` };
        } else {
          ui.status = { tone: 'success', title: 'Signed in', text: `Welcome back, ${label}. Your saved progress is loaded.` };
        }
        paint({ keepFocus: false });
        const returnTo = ctx.query.returnTo;
        if (typeof returnTo === 'string' && returnTo.startsWith('/') && !returnTo.startsWith('//')) {
          ctx.navigate(returnTo);
          return;
        }
        focusStatus(root);
      } catch (error) {
        ui.busy = false;
        const accountError = error instanceof AccountError ? error : new AccountError(error?.message || 'Something went wrong. Try again.');
        for (const [field, message] of Object.entries(accountError.fields || {})) ui.serverErrors[localField(field)] = message;
        for (const [field, value] of Object.entries(accountError.suggestions || {})) ui.suggestions[localField(field)] = value;
        if (mode === 'signin' && accountError.code === 'invalid_credentials') ui.values.signinPassword = '';
        const title = mode === 'signup' ? 'Account not created' : "Couldn't sign in";
        ui.status = accountError.unavailable
          ? {
              ...unavailableStatus(),
              tone: 'error',
              title,
              text: html`Can't reach the account server, so nothing was saved. Start TradeLab with <code>${SERVER_COMMAND}</code>, then try again.`,
            }
          : errorStatus(title, accountError);
        paint({ keepFocus: false });
        focusStatus(root);
      } finally {
        ui.busy = false;
      }
    };

    disposer.add(
      on(root, 'submit', '#signup-form', (event) => {
        event.preventDefault();
        submit('signup');
      }),
    );
    disposer.add(
      on(root, 'submit', '#signin-form', (event) => {
        event.preventDefault();
        submit('signin');
      }),
    );

    disposer.add(
      on(root, 'click', '[data-action="sign-out"]', async () => {
        if (ui.busy) return;
        ui.busy = true;
        paint();
        let signedOut = false;
        try {
          await signOut();
          signedOut = true;
          Object.assign(ui, freshUi('signin'));
          ui.status = { tone: 'info', title: 'Signed out', text: "Your progress is saved to your account for next time. Until you sign in again, TradeLab shows this browser's guest progress." };
        } catch (error) {
          ui.status = { tone: 'error', title: "Couldn't sign out", text: error.message };
        } finally {
          ui.busy = false;
          paint({ keepFocus: false });
          focusStatus(root);
        }
        if (signedOut) ctx.navigate('#/account?mode=signin', { replace: true });
      }),
    );

    // The "Progress saved" line follows each save without redrawing the page.
    disposer.add(
      watch(
        (state) => state.runtime.sync,
        (sync) => {
          const line = $('#account-sync', root);
          if (!line) return;
          line.className = `account-sync small account-sync--${sync.status}`;
          render(line, syncText(sync));
        },
      ),
    );

    // Signing out from the header menu, or the server going away, updates this page too.
    disposer.add(
      watch(
        (state) => `${state.runtime.userStatus}:${state.runtime.user?.id ?? ''}`,
        () => {
          if (ui.busy) return;
          ui.status = getState().runtime.userStatus === 'unavailable' ? unavailableStatus() : null;
          paint({ keepFocus: false });
        },
      ),
    );

    return {
      cleanup: () => disposer.dispose(),
      update(next) {
        setMode(next.query.mode === 'signin' ? 'signin' : 'signup');
      },
    };
  },
};
