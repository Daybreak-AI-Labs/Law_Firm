# Dashboard UI primitives

Shared, page-agnostic helpers defined once in
`packages/maverick-dashboard/maverick_dashboard/templates/base.html` and
available on every page that `{% extends "base.html" %}`. **Prefer these over
native `confirm()` / `alert()` or hand-rolled toasts** — they are accessible
(assertive vs. polite live regions; a focus-trapping, focus-returning modal),
themed (no hard-coded colors), and consistent across the dashboard.

## `window.mvToast(message, { error })`

Transient notification.

- `error: true` → an assertive (`role="alert"`) live region, prefixed `⚠ `.
- otherwise → a polite (`role="status"`) live region.
- Auto-dismisses (errors linger longer). The two live regions are baked into the
  DOM at load, so announcements are reliable (a `role` injected into an existing
  polite region is not).

```js
mvToast('Matter membership updated');
mvToast('Could not update membership', { error: true });
```

## `window.mvConfirm(message, { okText }) -> Promise<boolean>`

Accessible confirmation on the native `<dialog>`: focus trap, `Esc` cancels,
focus returns to the opener on close. Falls back to `window.confirm` where
`<dialog>.showModal` is unavailable. **Not re-entrant** — a call while already
open resolves `false`.

```js
if (!(await mvConfirm('Revoke this matter member?', { okText: 'Revoke' }))) return;
```

Callers must be `async`. If the confirmed action removes the element that held
focus (e.g. a deleted list row), move focus to a stable landmark afterward — a
`tabindex="-1"` section heading — since the opener no longer exists.

## `window.mvForm(title, fields, { okText }) -> Promise<object|null>`

Collects a small set of typed fields in the native `<dialog>` (focus trap, `Esc`
cancels, focus returns to the opener) — the replacement for a chain of
`window.prompt` calls. Resolves to a `{ key: value }` object, or `null` if
cancelled. Required fields with no value block submit with an inline error.
Falls back to sequential `prompt`s where `<dialog>.showModal` is unavailable.
**Not re-entrant** (shared dialog). Each field is
`{ key, label, type, required, default, options, placeholder, help }` where
`type` is `text` | `number` | `date` | `bool` | `select` (`options` for `select`).

```js
const vals = await mvForm('Add matter member', [
  { key: 'principal', label: 'Principal', type: 'text', required: true },
  { key: 'role', label: 'Role', type: 'select', required: true,
    options: ['attorney', 'staff', 'viewer'] },
], { okText: 'Add' });
if (vals === null) return;                 // cancelled
```

## `window.mvCopy(text)`

Copies `text` to the clipboard (Clipboard API, with a hidden-textarea fallback)
and toasts the outcome.

```js
copyBtn.addEventListener('click', function () { mvCopy(urlEl.textContent); });
```

## `--scrim`

The single overlay-backdrop token (`:root` in base.html). Use it for any modal /
drawer / picker backdrop instead of a bespoke `rgba(...)`:

```css
.my-overlay::backdrop { background: var(--scrim); }
```

## `window.mvEl(tag, cls, text)` / `window.mvWhen(ts)`

`mvEl` is a one-line element builder (`createElement` + optional class + optional
`textContent`); `mvWhen(epoch_seconds)` formats a timestamp with the browser
locale. Both were reimplemented per page before; use the shared ones.

```js
var row = mvEl('li', 'mv-row');
row.appendChild(mvEl('div', 'mv-row__title', name));
row.appendChild(mvEl('div', 'mv-row__sub', 'last run ' + mvWhen(ts)));
```

## `.card` / `.card-grid`

The shared card surface (border + token padding + hover) and a responsive
`auto-fill` grid of them. Cards and `.mv-row`s get a subtle `mv-rise` entrance,
disabled under `prefers-reduced-motion`.

```html
<div class="card-grid">
  <div class="card">…</div>
</div>
```

## `.mv-row` / `.btn--icon`

The shared list-row component — an icon/title + a muted subline + trailing
actions. `.btn--icon` is the compact square button for a row action.

```html
<li class="mv-row">
  <div class="mv-row__main">
    <div class="mv-row__title">Title</div>
    <div class="mv-row__sub">subtitle</div>
  </div>
  <div class="mv-row__actions"><button class="btn btn--icon" aria-label="Delete">✕</button></div>
</li>
```

## Adoption

In the retained firm UI, these back the halt confirmation, goal actions,
clipboard feedback, and deliverable cards. New destructive actions and
notifications should use these rather than native dialogs.
