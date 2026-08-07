'use strict';
/* The board's client side. Two halves:
   1. Presentation: filters/sort/search over data-* attributes, relative timestamps,
      status banner, photo fallbacks, the lightbox.
   2. The write side: direct GitHub API calls with a token pasted once. No server.
   Every failure path ends in a visible sentence — this is an iPad, tooltips don't exist. */

const CFG = {{CONFIG}};
const $ = s => document.querySelector(s);
const board = document.getElementById('board');
// Cards live in one grid per tier. Filtering and sorting act on all of them, but a card
// is only ever re-appended to its own band — sorting must never move an estate piece
// into the quick-flips shelf.
const cards = [...board.querySelectorAll('article.card')];
const tierSections = [...board.querySelectorAll('section.tier')];

/* ---- status banner + relative time ---------------------------------------------------- */
(function () {
  const upd = $('#updated');
  if (upd && upd.dataset.ts) {
    const then = Date.parse(upd.dataset.ts);
    if (!isNaN(then)) {
      const mins = Math.max(0, Math.round((Date.now() - then) / 60000));
      const label = mins < 2 ? 'updated just now'
        : mins < 90 ? 'updated ' + mins + ' min ago'
        : mins < 36 * 60 ? 'updated ' + Math.round(mins / 60) + ' h ago'
        : 'updated ' + Math.round(mins / 1440) + ' days ago';
      upd.textContent = label;
      upd.title = new Date(then).toLocaleString();
    }
  }
  if (cards.length === 0) $('#empty').hidden = false;

  // status.json is written by every run, including the ones that fail — so the page can
  // say WHY it looks the way it does instead of silently going stale.
  fetch('status.json', { cache: 'no-store' }).then(r => r.ok ? r.json() : null).then(s => {
    if (!s || s.state === 'ok') return;
    const n = $('#notice');
    const msgs = {
      scan_blocked: 'The last scrape couldn’t reach Marketplace (usually the Apify '
        + 'monthly limit). The board below is re-ranked from the catalogue — fresh '
        + 'listings will appear once scraping resumes.',
      appraisals_failed: 'The last run scraped fine but every AI valuation failed. '
        + 'New finds are waiting unvalued.',
      catalog_corrupt: 'The catalogue file is damaged and the run stopped to protect your '
        + 'stored appraisals. See the Actions log.'
    };
    // The run's own error text, when it recorded one. This is the difference between
    // "go regenerate your token" (a guess, and on 2026-08-06 the wrong one) and the
    // actual "You've hit your session limit · resets 4:10pm (UTC)". textContent, never
    // innerHTML — the string comes from a third-party CLI, not from us.
    n.textContent = (msgs[s.state] || ('Last run reported: ' + s.state))
      + (s.reason ? ' Reason: ' + s.reason : '');
    n.className = 'notice' + (s.state === 'catalog_corrupt' ? ' bad' : '');
    n.hidden = false;
    if (cards.length === 0 && s.state === 'scan_blocked')
      $('#empty-sub').textContent = 'Scraping is paused until the Apify month resets.';
  }).catch(() => {});
})();

/* ---- photo fallback -------------------------------------------------------------------- */
// A missing/expired image swaps to the text placeholder instead of an empty box.
document.addEventListener('error', ev => {
  const img = ev.target;
  if (!(img instanceof HTMLImageElement) || !img.classList.contains('thumb')) return;
  const ph = document.createElement('div');
  ph.className = 'thumb ph';
  const span = document.createElement('span');
  span.textContent = img.dataset.ph || img.alt || 'no photo';
  ph.appendChild(span);
  img.replaceWith(ph);
}, true);

/* ---- filters (combinable), sort, search ------------------------------------------------ */
const state = { radius: false, clean: false, star: false, q: '', sort: 'priority' };

function applyView() {
  let visible = 0;
  cards.forEach(c => {
    const d = c.dataset;
    let show = true;
    if (state.radius && d.oor === '1') show = false;
    if (state.clean && d.flag === '1') show = false;
    if (state.star && d.killer !== '1') show = false;
    if (state.q && !(d.title || '').toLowerCase().includes(state.q)) show = false;
    c.classList.toggle('hide', !show);
    if (show) visible++;
  });
  const key = { priority: 'priority', margin: 'margin', ask: 'ask', fresh: 'fresh' }[state.sort];
  const dir = state.sort === 'ask' || state.sort === 'fresh' ? 1 : -1;
  tierSections.forEach(section => {
    const grid = section.querySelector('.grid');
    const own = cards.filter(c => c.dataset.tier === section.dataset.tier);
    [...own]
      .sort((a, b) => dir * (parseFloat(a.dataset[key] || 0) - parseFloat(b.dataset[key] || 0)))
      .forEach(c => grid.appendChild(c));
    // A band with nothing left after filtering hides its heading too, rather than
    // leaving a title over empty space.
    section.classList.toggle('empty', own.every(c => c.classList.contains('hide')));
  });
  $('#empty').hidden = visible > 0 || cards.length === 0;
  if (visible === 0 && cards.length > 0)
    $('#empty-sub').textContent = 'Nothing matches these filters.';
}
document.querySelectorAll('.filter').forEach(btn => btn.addEventListener('click', () => {
  const k = btn.dataset.filter;
  state[k] = !state[k];
  btn.setAttribute('aria-pressed', String(state[k]));
  applyView();
}));
$('#sort').addEventListener('change', ev => { state.sort = ev.target.value; applyView(); });
$('#search').addEventListener('input', ev => {
  state.q = ev.target.value.trim().toLowerCase(); applyView();
});

/* ---- lightbox --------------------------------------------------------------------------- */
const lb = { photos: [], i: 0 };
function lbShow() {
  $('#lb-img').src = lb.photos[lb.i];
  $('#lb-count').textContent = (lb.i + 1) + ' / ' + lb.photos.length;
  $('#lb-prev').disabled = lb.i === 0;
  $('#lb-next').disabled = lb.i === lb.photos.length - 1;
}
function openLightbox(photos, alt) {
  lb.photos = photos; lb.i = 0;
  $('#lb-img').alt = alt || '';
  lbShow();
  const dlg = $('#lightbox');
  if (typeof dlg.showModal === 'function') dlg.showModal();
  else dlg.setAttribute('open', '');            // Safari < 15.4: non-modal but usable
}
document.addEventListener('click', ev => {
  const hero = ev.target.closest('.hero');
  if (!hero || ev.target.closest('.swingtag')) return;
  const card = hero.closest('article');
  let photos = [];
  try { photos = JSON.parse(card.dataset.photos || '[]'); } catch (e) { photos = []; }
  if (photos.length) openLightbox(photos, card.dataset.title);
});
$('#lb-close').addEventListener('click', () => $('#lightbox').close
  ? $('#lightbox').close() : $('#lightbox').removeAttribute('open'));
$('#lb-prev').addEventListener('click', () => { if (lb.i > 0) { lb.i--; lbShow(); } });
$('#lb-next').addEventListener('click', () => { if (lb.i < lb.photos.length - 1) { lb.i++; lbShow(); } });

/* ---- the write side --------------------------------------------------------------------- */
const S = {
  get token() { return localStorage.getItem('bench_token') || ''; },
  set token(v) { v ? localStorage.setItem('bench_token', v) : localStorage.removeItem('bench_token'); }
};
const b64 = str => {
  const bytes = new TextEncoder().encode(str);
  let bin = '';
  for (let i = 0; i < bytes.length; i += 0x8000)
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(bin);
};
const unb64 = s => new TextDecoder().decode(
  Uint8Array.from(atob(s.replace(/\s/g, '')), c => c.charCodeAt(0)));

function explain(status, body) {
  const msg = (body && body.message) || '';
  if (status === 401) return 'GitHub rejected the token. Paste a fresh one under Connection.';
  if (status === 403) return 'Token lacks permission. It needs Actions: read & write and '
    + 'Contents: read & write on this repo.';
  if (status === 404) return 'Not found — usually the token isn’t scoped to ' + CFG.repo
    + ', or the workflow file is missing on ' + CFG.branch + '.';
  if (status === 409 || status === 422) return 'GitHub refused the write (' + (msg || status)
    + '). Reload the page and try once more — someone else may have written first.';
  return 'GitHub returned ' + status + (msg ? ': ' + msg : '') + '.';
}

async function api(path, opts = {}) {
  if (!CFG.repo) throw new Error('This board was built without a repo, so the buttons '
    + 'have nothing to talk to. Re-run the pipeline from Actions.');
  if (!S.token) throw new Error('No token yet — tap Connection and paste one.');
  const res = await fetch('https://api.github.com' + path, Object.assign({}, opts, {
    headers: Object.assign({
      'Accept': 'application/vnd.github+json',
      'Authorization': 'Bearer ' + S.token,
      'X-GitHub-Api-Version': '2022-11-28'
    }, opts.headers || {})
  }));
  if (!res.ok) {
    let body = null;
    try { body = await res.json(); } catch (e) { /* no body */ }
    const err = new Error(explain(res.status, body));
    err.status = res.status;
    throw err;
  }
  return res.status === 204 ? null : res.json();
}

const say = (el, msg, kind) => { if (el) { el.textContent = msg; el.className = 'status ' + (kind || ''); } };

async function checkConnection() {
  const el = $('#conn');
  if (!CFG.repo) { el.textContent = 'read-only board'; el.className = 'conn'; return; }
  if (!S.token) { el.textContent = 'not connected'; el.className = 'conn'; return; }
  try {
    await api('/repos/' + CFG.repo);
    el.textContent = 'connected'; el.className = 'conn ok';
  } catch (err) {
    // The full sentence, visible. A title= tooltip does not exist on a touch screen.
    el.textContent = err.message; el.className = 'conn bad';
  }
}

/* ---- scrape now ------------------------------------------------------------------------- */
$('#scrape-now').addEventListener('click', async ev => {
  const btn = ev.currentTarget;
  const el = $('#conn');
  if (btn.getAttribute('aria-busy') === 'true') return;
  // This spends real Apify credit; a double-tap must not double-bill.
  if (!window.confirm('Run a scrape now? This spends Apify credit.')) return;
  btn.setAttribute('aria-busy', 'true');
  el.textContent = 'starting…'; el.className = 'conn';
  try {
    await api('/repos/' + CFG.repo + '/actions/workflows/' + CFG.boardWorkflow + '/dispatches', {
      method: 'POST',
      body: JSON.stringify({ ref: CFG.branch, inputs: {} })
    });
    el.textContent = 'scrape started — this page updates itself when the run commits '
      + '(check back in ~10 minutes)';
    el.className = 'conn ok';
  } catch (err) { el.textContent = err.message; el.className = 'conn bad'; }
  finally { setTimeout(() => btn.removeAttribute('aria-busy'), 8000); }
});

/* ---- connection dialog ------------------------------------------------------------------ */
$('#repo-name').textContent = CFG.repo || '(not configured)';
$('#open-settings').addEventListener('click', () => {
  $('#token').value = S.token;
  const dlg = $('#settings');
  if (typeof dlg.showModal === 'function') dlg.showModal();
  else dlg.setAttribute('open', '');
});
$('#save-token').addEventListener('click', () => {
  S.token = $('#token').value.trim();
  say($('#settings-status'), S.token ? 'Saved.' : 'Token cleared.', 'ok');
  setTimeout(checkConnection, 0);
});
$('#forget-token').addEventListener('click', () => {
  S.token = '';
  say($('#settings-status'), 'Token forgotten.', 'ok');
  setTimeout(checkConnection, 0);
});

/* ---- log a piece ------------------------------------------------------------------------- */
// "1,200" must be $1,200 — parseFloat quietly read it as $1. "$120" must work too.
// Returns null for empty, NaN for unreadable (caller shows an error instead of dropping it).
const cents = v => {
  const s = String(v == null ? '' : v).trim().replace(/[$,\s]/g, '');
  if (!s) return null;
  if (!/^\d+(\.\d{1,2})?$/.test(s)) return NaN;
  return Math.round(parseFloat(s) * 100);
};

async function readJson(path) {
  try {
    const r = await api('/repos/' + CFG.repo + '/contents/' + path + '?ref=' + CFG.branch
      + '&nocache=' + Date.now());
    return { data: JSON.parse(unb64(r.content)), sha: r.sha };
  } catch (err) {
    if (err.status === 404) return { data: null, sha: null };
    throw err;
  }
}

document.querySelectorAll('.logform').forEach(form => {
  form.addEventListener('submit', async ev => {
    ev.preventDefault();
    const btn = form.querySelector('button[type=submit]');
    if (btn.getAttribute('aria-busy') === 'true') return;
    const st = form.querySelector('.status');
    const id = form.dataset.id;
    const card = form.closest('article');
    const fd = new FormData(form);
    const paid = cents(fd.get('paid')), materials = cents(fd.get('materials'));
    const hoursRaw = String(fd.get('hours') || '').trim();
    const hours = hoursRaw ? parseFloat(hoursRaw) : NaN;
    const sold = cents(fd.get('sold'));
    for (const [label, v] of [['Paid', paid], ['Materials', materials], ['Sold for', sold]]) {
      if (Number.isNaN(v)) {
        say(st, label + ' isn’t a number I can read — digits only, like 120 or 1200.50.', 'bad');
        return;
      }
    }
    if (paid === null && materials === null && isNaN(hours) && sold === null) {
      say(st, 'Nothing to save — fill in at least one field.', 'bad'); return;
    }
    btn.setAttribute('aria-busy', 'true');
    say(st, 'saving…');
    try {
      const cur = await readJson(CFG.piecesPath);
      const ledger = cur.data || { version: 1, pieces: {} };
      const prev = ledger.pieces[id] || { listing_id: id };
      const entry = Object.assign({}, prev, {
        listing_id: id,
        title: prev.title || (card ? card.dataset.title || '' : '')
      });
      if (paid !== null) entry.acquired_price_cents = paid;
      if (materials !== null) entry.materials_cents = materials;
      if (!isNaN(hours)) entry.labor_hours = hours;
      if (sold !== null) {
        entry.sold_price_cents = sold;
        entry.sold_at = entry.sold_at || new Date().toISOString();
      }
      ledger.pieces[id] = entry;
      const body = { message: 'Log piece ' + id, branch: CFG.branch,
                     content: b64(JSON.stringify(ledger, null, 1)) };
      if (cur.sha) body.sha = cur.sha;
      await api('/repos/' + CFG.repo + '/contents/' + CFG.piecesPath,
                { method: 'PUT', body: JSON.stringify(body) });
      say(st, 'Saved. Your numbers update on the next run.', 'ok');
    } catch (err) { say(st, err.message, 'bad'); }
    finally { btn.removeAttribute('aria-busy'); }
  });
});

/* ---- negotiation -------------------------------------------------------------------------- */
const POSTURES = [[25, 'aggressive'], [50, 'measured'], [75, 'keen'], [100, 'eager']];
document.querySelectorAll('.negoform').forEach(form => {
  const slider = form.querySelector('input[name=posture]');
  const label = form.querySelector('.postval');
  const show = () => { label.textContent = (POSTURES.find(p => slider.value <= p[0]) || POSTURES[3])[1]; };
  slider.addEventListener('input', show); show();

  form.addEventListener('submit', async ev => {
    ev.preventDefault();
    const btn = form.querySelector('button[type=submit]');
    if (btn.getAttribute('aria-busy') === 'true') return;   // double-tap = double dispatch
    const st = form.querySelector('.status');
    const out = form.querySelector('.drafts');
    const id = form.dataset.id;
    const fd = new FormData(form);
    out.innerHTML = '';
    btn.setAttribute('aria-busy', 'true');
    say(st, 'asking… this runs on GitHub and takes a minute or two.');
    try {
      const before = await readJson(CFG.draftsDir + '/' + id + '.json');
      const stamp = (before.data && before.data.generated_at) || null;
      await api('/repos/' + CFG.repo + '/actions/workflows/' + CFG.negotiateWorkflow + '/dispatches', {
        method: 'POST',
        body: JSON.stringify({ ref: CFG.branch, inputs: {
          listing_id: id, posture: String(fd.get('posture')),
          conversation: String(fd.get('conversation') || ''),
          notes: String(fd.get('notes') || '')
        } })
      });
      // Poll the committed file rather than the run, so a failure that still writes a
      // reason surfaces as that reason instead of a red X you have to go hunting for.
      // Compare stamps null-safely: every payload (errors included) now carries
      // generated_at, and `undefined !== undefined` was false — a repeated error
      // previously hung this loop for its full five minutes.
      for (let i = 0; i < 60; i++) {
        await new Promise(r => setTimeout(r, 5000));
        const now = await readJson(CFG.draftsDir + '/' + id + '.json');
        const nowStamp = (now.data && now.data.generated_at) || null;
        if (now.data && nowStamp !== stamp) { renderDrafts(out, st, now.data); return; }
        say(st, 'still working… ' + ((i + 1) * 5) + 's');
      }
      say(st, 'Gave up waiting after five minutes. Check the Actions tab — the run may '
        + 'still be going, and the drafts will appear here when it finishes.', 'bad');
    } catch (err) { say(st, err.message, 'bad'); }
    finally { btn.removeAttribute('aria-busy'); }
  });
});

function renderDrafts(out, st, data) {
  if (data.status !== 'ok') {
    say(st, data.error || 'Drafting failed and gave no reason.', 'bad'); return;
  }
  if (!Array.isArray(data.drafts)) {
    say(st, 'The draft file came back in a shape this page doesn’t recognise.', 'bad');
    return;
  }
  say(st, data.posture_label + (data.walkaway_price_cents
    ? ' · walk away above $' + Math.round(data.walkaway_price_cents / 100)
    : ' · no walk-away — the numbers say this piece isn’t worth buying'), 'ok');
  out.innerHTML = data.drafts.map(d => {
    const over = (d.over_walkaway_cents || []).length
      ? '<p class="reason">⚠ mentions $'
        + d.over_walkaway_cents.map(c => Math.round(c / 100)).join(', $')
        + ' — above your walk-away. Check before sending.</p>' : '';
    const esc = s => String(s).replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
    return '<div class="draft"><p class="dtext">' + esc(d.text) + '</p>'
      + '<p class="basis">' + esc(d.rationale) + '</p>' + over
      + '<button type="button" class="copy">Copy</button></div>';
  }).join('');
  out.querySelectorAll('.copy').forEach(btn => btn.addEventListener('click', () => {
    const text = btn.parentElement.querySelector('.dtext').textContent;
    navigator.clipboard.writeText(text)
      .then(() => { btn.textContent = 'Copied'; })
      .catch(() => { btn.textContent = 'Copy failed — select the text instead'; });
  }));
}

applyView();      // establish sort/empty state on load, not only after a click
checkConnection();
