/* Maverick dashboard shell behaviors. Extracted from templates/base.html;
   served by GET /static/maverick-ui.js. Loaded at the end of <body>, so
   the DOM is ready without defer. First section: reusable feedback
   primitives (mvToast/mvConfirm/mvForm/mvCopy). Second: halt pill, goal
   form, sidebar + preferences behaviors. */
    // Reusable feedback primitives, on every page that extends base.html.
    // Prefer these over the browser's native dialogs. Contract: docs/ui-primitives.md.
    //   window.mvToast(message, {error}) -> transient status/alert toast
    //   window.mvConfirm(message, {okText}) -> Promise<bool> (native <dialog>;
    //     focus-returning, not re-entrant; falls back to window.confirm)
    //   window.mvCopy(text) -> copy to clipboard with a toast (+ fallback)
    (function () {
      var region = document.getElementById('mv-toasts');
      var alertRegion = document.getElementById('mv-toasts-alert');
      window.mvToast = function (message, opts) {
        opts = opts || {};
        // Route to a region whose live politeness is fixed at load (an injected
        // role can't reliably upgrade a polite region to assertive); prefix
        // errors so the state survives grayscale / color-blindness.
        var target = opts.error ? alertRegion : region;
        if (!target) return;
        var t = document.createElement('div');
        t.className = 'mv-toast' + (opts.error ? ' mv-toast--error' : '');
        t.textContent = (opts.error ? '⚠ ' : '') + String(message);
        target.appendChild(t);
        setTimeout(function () {
          t.style.transition = 'opacity 200ms'; t.style.opacity = '0';
          setTimeout(function () { t.remove(); }, 220);
        }, opts.error ? 5000 : 3200);
      };
      var dlg = document.getElementById('mv-confirm');
      var msgEl = document.getElementById('mv-confirm-msg');
      var okBtn = document.getElementById('mv-confirm-ok');
      var cancelBtn = document.getElementById('mv-confirm-cancel');
      window.mvConfirm = function (message, opts) {
        opts = opts || {};
        if (!dlg || typeof dlg.showModal !== 'function') {
          return Promise.resolve(window.confirm(String(message)));
        }
        if (dlg.open) return Promise.resolve(false);   // not re-entrant (shared dialog)
        var opener = document.activeElement;           // restore focus on close
        msgEl.textContent = String(message);
        okBtn.textContent = opts.okText || 'Confirm';
        return new Promise(function (resolve) {
          function done(val) {
            okBtn.removeEventListener('click', onOk);
            cancelBtn.removeEventListener('click', onCancel);
            dlg.removeEventListener('cancel', onCancel);
            if (dlg.open) dlg.close();
            // Return focus to the opener when it survives; a delete that removed
            // its own row leaves the caller to re-home focus to a stable spot.
            if (opener && opener.isConnected && typeof opener.focus === 'function') opener.focus();
            resolve(val);
          }
          function onOk() { done(true); }
          function onCancel(e) { if (e) e.preventDefault(); done(false); }
          okBtn.addEventListener('click', onOk);
          cancelBtn.addEventListener('click', onCancel);
          dlg.addEventListener('cancel', onCancel);   // Esc / backdrop
          dlg.showModal();
          cancelBtn.focus();
        });
      };
      // Collect a small set of typed fields in a modal form (replaces per-value
      // window.prompt chains). fields: [{key,label,type,required,default,options,
      // placeholder,help}] where type is text|number|date|bool|select. Resolves to
      // a {key:value} object, or null if cancelled. Falls back to sequential
      // prompts where <dialog> is unsupported.
      var fdlg = document.getElementById('mv-form');
      window.mvForm = function (title, fields, opts) {
        opts = opts || {};
        fields = fields || [];
        if (!fdlg || typeof fdlg.showModal !== 'function') {
          var out0 = {};
          for (var i = 0; i < fields.length; i++) {
            var s = fields[i];
            var v0 = window.prompt((s.label || s.key) + (s.required ? ' *' : ''),
              s.default != null ? String(s.default) : '');
            if (v0 === null) return Promise.resolve(null);
            if (v0 !== '') out0[s.key] = v0;
          }
          return Promise.resolve(out0);
        }
        if (fdlg.open) return Promise.resolve(null);   // shared dialog, not re-entrant
        var opener = document.activeElement;
        var form = document.getElementById('mv-form-form');
        var body = document.getElementById('mv-form-fields');
        var errEl = document.getElementById('mv-form-err');
        var cancelBtn = document.getElementById('mv-form-cancel');
        document.getElementById('mv-form-title').textContent = String(title || 'Details');
        document.getElementById('mv-form-ok').textContent = opts.okText || 'OK';
        body.textContent = ''; errEl.textContent = '';
        var controls = [];
        fields.forEach(function (f) {
          var id = 'mvf-' + f.key;
          var wrap = document.createElement('div'); wrap.className = 'mv-form__field';
          var ctl;
          if (f.type === 'bool' || f.type === 'checkbox') {
            wrap.className += ' mv-form__check';
            ctl = document.createElement('input'); ctl.type = 'checkbox'; ctl.checked = !!f.default; ctl.id = id;
            var l1 = document.createElement('label'); l1.setAttribute('for', id); l1.textContent = f.label || f.key;
            wrap.appendChild(ctl); wrap.appendChild(l1);
          } else {
            var lab = document.createElement('label'); lab.setAttribute('for', id);
            lab.textContent = (f.label || f.key) + (f.required ? ' *' : '');
            wrap.appendChild(lab);
            if (f.type === 'select') {
              ctl = document.createElement('select');
              (f.options || []).forEach(function (o) {
                var op = document.createElement('option');
                op.value = (o && o.value != null) ? o.value : o;
                op.textContent = (o && o.label != null) ? o.label : o;
                ctl.appendChild(op);
              });
              if (f.default != null) ctl.value = String(f.default);
            } else {
              ctl = document.createElement('input');
              ctl.type = f.type === 'number' ? 'number' : f.type === 'date' ? 'date' : 'text';
              ctl.className = 'input';
              if (f.default != null) ctl.value = String(f.default);
              if (f.placeholder) ctl.placeholder = f.placeholder;
            }
            ctl.id = id;
            wrap.appendChild(ctl);
          }
          if (f.help) { var h = document.createElement('div'); h.className = 'mv-form__help'; h.textContent = f.help; wrap.appendChild(h); }
          controls.push({ f: f, ctl: ctl });
          body.appendChild(wrap);
        });
        return new Promise(function (resolve) {
          function collect() {
            var out = {}, missing = [];
            controls.forEach(function (c) {
              if (c.f.type === 'bool' || c.f.type === 'checkbox') { out[c.f.key] = c.ctl.checked; return; }
              var val = (c.ctl.value || '').trim();
              if (!val && c.f.required && c.f.default == null) { missing.push(c.f.label || c.f.key); return; }
              if (val) out[c.f.key] = val;
            });
            if (missing.length) { errEl.textContent = 'Required: ' + missing.join(', '); return null; }
            return out;
          }
          function done(val) {
            form.removeEventListener('submit', onSubmit);
            cancelBtn.removeEventListener('click', onCancel);
            fdlg.removeEventListener('cancel', onCancel);
            if (fdlg.open) fdlg.close();
            if (opener && opener.isConnected && typeof opener.focus === 'function') opener.focus();
            resolve(val);
          }
          function onSubmit(e) { e.preventDefault(); var v = collect(); if (v !== null) done(v); }
          function onCancel(e) { if (e) e.preventDefault(); done(null); }
          form.addEventListener('submit', onSubmit);
          cancelBtn.addEventListener('click', onCancel);
          fdlg.addEventListener('cancel', onCancel);   // Esc / backdrop
          fdlg.showModal();
          var first = body.querySelector('input, select'); if (first) first.focus();
        });
      };
      // Copy text to the clipboard with a toast; falls back for old browsers.
      window.mvCopy = function (text) {
        function ok() { mvToast('Copied to clipboard'); }
        function fail() { mvToast('Copy failed — select and copy manually', { error: true }); }
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(String(text)).then(ok, fail);
          return;
        }
        try {
          var ta = document.createElement('textarea');
          ta.value = String(text); ta.style.position = 'fixed'; ta.style.opacity = '0';
          document.body.appendChild(ta); ta.select();
          document.execCommand('copy'); ta.remove(); ok();
        } catch (e) { fail(); }
      };
      // Small DOM builder + a uniform timestamp formatter, shared by the
      // list-rendering pages (was reimplemented per page).
      window.mvEl = function (tag, cls, text) {
        var e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text != null) e.textContent = text;
        return e;
      };
      window.mvWhen = function (ts) {
        // Return '' for missing/invalid timestamps rather than formatting the
        // Unix epoch: (ts || 0) would turn 0/null/undefined/NaN into 1/1/1970,
        // which then leaks into visible text and aria-labels.
        if (!ts || !isFinite(ts)) return '';
        try { return new Date(ts * 1000).toLocaleString(); } catch (e) { return ''; }
      };
    })();

    // Halt indicator + arm/clear via /api/v1/halt.
    (function() {
      const pill = document.getElementById('halt-pill');
      const label = document.getElementById('halt-label');
      if (!pill || !label) return;
      let armed = false;
      async function refresh() {
        try {
          const r = await fetch('/api/v1/halt');
          if (!r.ok) throw new Error('halt status ' + r.status);
          const j = await r.json();
          armed = !!j.active;
          pill.classList.toggle('armed', armed);
          label.textContent = armed ? 'STOPPED — click to resume' : 'Stop all';
          pill.setAttribute('aria-pressed', armed ? 'true' : 'false');
        } catch (e) {
          // Keep the last known state on a transient poll failure — resetting the
          // label to "Stop all" while leaving aria-pressed="true" would desync the
          // visible and announced state. Seed a neutral state only if we never
          // had one.
          if (!pill.hasAttribute('aria-pressed')) {
            label.textContent = 'Stop all';
            pill.setAttribute('aria-pressed', 'false');
          }
        }
      }
      pill.addEventListener('click', async function(ev) {
        ev.preventDefault();
        if (armed) {
          await fetch('/api/v1/halt', { method: 'DELETE' });
        } else {
          if (!(await mvConfirm('Stop all work? Every running goal pauses safely at its next step and waits for you to resume.', { okText: 'Stop all work' }))) return;
          await fetch('/api/v1/halt', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({reason: 'manual via dashboard'}),
          });
        }
        await refresh();
      });
      pill.addEventListener('keydown', function(ev) {
        // role="button" must activate on Space; the native <a href>
        // already handles Enter. Stop Space from scrolling the page.
        if (ev.key === ' ' || ev.key === 'Spacebar') {
          ev.preventDefault();
          pill.click();
        }
      });
      refresh();
      setInterval(refresh, 15000);
    })();

    // Shared goal-create form handler. The form appears on both the
    // overview (first run) and chat pages; submit via fetch so a
    // 400/403/429 shows the server message inline instead of a raw JSON
    // page. No-ops on pages without the form + its error region.
    (function() {
      const form = document.querySelector('form[action="/chat/send"]');
      const errBox = document.getElementById('goal-error');
      if (!form || !errBox) return;
      const btn = form.querySelector('button[type="submit"]');
      form.addEventListener('submit', async function(ev) {
        ev.preventDefault();
        errBox.style.display = 'none';
        const kindEl = form.querySelector('[name="authoring_kind"]');
        const kind = kindEl ? kindEl.value : 'goal';
        const files = form.querySelector('input[type="file"]');
        // An explicit create-artifact choice stages an UNSAVED first pass in the
        // dedicated editor.  Attachments continue through the ordinary goal path
        // until authoring endpoints support their full evidence/security model.
        if ((kind === 'flow' || kind === 'agent') && !(files && files.files.length)) {
          const title = (form.querySelector('[name="title"]') || {}).value || '';
          const details = (form.querySelector('[name="description"]') || {}).value || '';
          const brief = (title.trim() + (details.trim() ? '\n\n' + details.trim() : '')).slice(0, 8000);
          if (!brief) {
            errBox.textContent = 'Describe what you want Maverick to draft.';
            errBox.style.display = 'block';
            return;
          }
          try {
            sessionStorage.setItem('maverick.authoring-handoff', JSON.stringify({
              kind: kind, brief: brief, created_at: Date.now()
            }));
            window.location.href = kind === 'flow' ? '/flows/designer' : '/workflow-builder';
          } catch (e) {
            errBox.textContent = 'Could not stage the draft in this browser session.';
            errBox.style.display = 'block';
          }
          return;
        }
        if (btn) { btn.disabled = true; btn.setAttribute('aria-busy', 'true'); }
        try {
          const resp = await fetch(form.action, { method: 'POST', body: new FormData(form) });
          if (resp.redirected) { window.location.href = resp.url; return; }
          if (resp.ok) { window.location.reload(); return; }
          let detail = 'Could not start the goal (' + resp.status + ').';
          try { const j = await resp.json(); if (j && j.detail) detail = j.detail; } catch (e) {}
          if (resp.status === 429) {
            const ra = resp.headers.get('Retry-After');
            if (ra) detail += ' (retry in ' + ra + 's)';
          }
          errBox.textContent = detail;
          errBox.style.display = 'block';
        } catch (e) {
          errBox.textContent = 'Network error — is the dashboard still running?';
          errBox.style.display = 'block';
        } finally {
          if (btn) { btn.disabled = false; btn.removeAttribute('aria-busy'); }
        }
      });
    })();

    // Collapsible sidebar groups. Chat-first: groups start COLLAPSED so the
    // nav isn't a wall of every page — the group holding the active page is
    // always forced open, and any group the user pins open persists per-group
    // in localStorage. Progressive enhancement: with no JS the markup leaves
    // every group open (aria-expanded="true"), so nothing is hidden without JS.
    (function() {
      const KEY = 'mvk_nav_expanded';
      let expanded = {};
      try { expanded = JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch (e) {}
      document.querySelectorAll('.navgrp__toggle').forEach(function(btn) {
        const grp = btn.getAttribute('data-grp');
        const hasActive = btn.parentElement.querySelector('a.active');
        const open = !!expanded[grp] || !!hasActive;
        btn.setAttribute('aria-expanded', open ? 'true' : 'false');
        btn.addEventListener('click', function() {
          const nowOpen = btn.getAttribute('aria-expanded') === 'true';
          btn.setAttribute('aria-expanded', nowOpen ? 'false' : 'true');
          if (nowOpen) delete expanded[grp]; else expanded[grp] = true;
          try { localStorage.setItem(KEY, JSON.stringify(expanded)); } catch (e) {}
        });
      });
    })();

    // Sidebar toggle. Wide screens: the hamburger collapses/expands the
    // persistent sidebar (preference persisted). Narrow screens: it opens the
    // off-canvas drawer, closed by the backdrop, Escape, or following a link.
    (function() {
      const body = document.body;
      const btn = document.getElementById('nav-toggle');
      const backdrop = document.getElementById('nav-backdrop');
      const sidebar = document.getElementById('sidebar');
      if (!btn) return;
      const KEY = 'mvk_sidebar_collapsed';
      const mobile = window.matchMedia('(max-width: 880px)');
      try { if (localStorage.getItem(KEY) === '1') body.classList.add('nav-collapsed'); } catch (e) {}
      function syncAria() {
        const shown = mobile.matches ? body.classList.contains('nav-open')
                                     : !body.classList.contains('nav-collapsed');
        btn.setAttribute('aria-expanded', shown ? 'true' : 'false');
        // On mobile the closed drawer is only translated off-screen (still in
        // the DOM + tab order). Mark it inert so keyboard focus can't land on
        // the hidden nav links. Desktop-collapsed already uses display:none.
        if (sidebar) sidebar.inert = mobile.matches && !body.classList.contains('nav-open');
      }
      function closeDrawer() { body.classList.remove('nav-open'); syncAria(); }
      btn.addEventListener('click', function() {
        if (mobile.matches) {
          body.classList.toggle('nav-open');
        } else {
          const collapsed = body.classList.toggle('nav-collapsed');
          try { localStorage.setItem(KEY, collapsed ? '1' : ''); } catch (e) {}
        }
        syncAria();
      });
      if (backdrop) backdrop.addEventListener('click', closeDrawer);
      document.addEventListener('keydown', function(ev) { if (ev.key === 'Escape') closeDrawer(); });
      document.querySelectorAll('.sidebar a').forEach(function(a) { a.addEventListener('click', closeDrawer); });
      if (mobile.addEventListener) mobile.addEventListener('change', syncAria);
      syncAria();
    })();

    // Preferences popover: close on outside-click and Escape (it is a native
    // <details>, so choosing an option already reloads the page and closes it).
    (function () {
      var prefs = document.getElementById('prefs-menu');
      if (!prefs) return;
      document.addEventListener('click', function (ev) {
        if (prefs.open && !prefs.contains(ev.target)) prefs.open = false;
      });
      document.addEventListener('keydown', function (ev) {
        if (ev.key === 'Escape' && prefs.open) {
          prefs.open = false;
          var s = prefs.querySelector('summary'); if (s) s.focus();
        }
      });
    })();
