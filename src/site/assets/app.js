/* Client-side search over the embedded index (12 §3). Linear scan over weighted fields —
   right size for a few hundred casefiles; the projection (src/search/index.py) is what a
   served FTS backend would index later. No dependencies. */
(function () {
  var idxEl = document.getElementById('idx');
  if (!idxEl) return;  // not the search page (the admin lookup page loads this file for initQueue below)
  var data = JSON.parse(idxEl.textContent);
  var DOCS = data.docs, W = data.weights;
  // Facets list canonical ids only; wording is never a facet value (12 §3). A child facet
  // (parent set) renders only when its parent has a selection and lists only the values whose
  // parent value is selected. `canonical` facets exist only in a v2 index built with the
  // resolver; without one (data.canonical false) the v1 wording Symptom facet is shown instead.
  // Under a selected Symptom family the UI lists ON / WHERE (the component · location the
  // symptom table carries, 13 §8), never the symptom standards themselves.
  var CANONICAL = !!data.canonical;
  var FACETS = [
    { key: 'equipment_type', label: 'Equipment' },
    { key: 'equipment_std', label: 'Equipment · standard', parent: 'equipment_type', canonical: true },
    { key: 'symptom_family', label: 'Symptom', canonical: true, family: true },
    { key: 'symptom_on', label: 'Symptom · on / where', parent: 'symptom_family', canonical: true },
    { key: 'condition', label: 'Symptom · condition', parent: 'symptom_family', canonical: true },
    { key: 'symptom', label: 'Symptom (wording)', fallback: true },
    { key: 'outcome', label: 'Outcome' },
    { key: 'kind', label: 'Action kind' },
    { key: 'role', label: 'Performed by' },
    { key: 'vessel', label: 'Vessel' },
    { key: 'year', label: 'Year' }
  ].filter(function (fc) { return CANONICAL ? !fc.fallback : !fc.canonical; });
  var LABELS = data.labels || {};
  var PARENT_VALS = data.facet_parent_vals || {};
  var PSEUDO = {};
  (data.pseudo_values || []).forEach(function (v) { PSEUDO[v] = 1; });
  function label(k, v) { return (LABELS[k] && LABELS[k][v]) || v; }
  function parentOf(k) { var fc = FACETS.filter(function (f) { return f.key === k; })[0]; return fc && fc.parent; }
  // a child value is listed when any of its parent values is selected
  function parentSelected(k, v) {
    var pk = parentOf(k); if (!pk) return true;
    var sel = state.f[pk] || []; if (!sel.length) return false;
    var pv = (PARENT_VALS[k] && PARENT_VALS[k][v]) || [];
    return pv.some(function (p) { return sel.indexOf(p) >= 0; });
  }
  // clearing a parent selection clears the child selections that depended on it
  function pruneChildren() {
    FACETS.forEach(function (fc) {
      if (!fc.parent || !state.f[fc.key]) return;
      var keep = state.f[fc.key].filter(function (v) { return parentSelected(fc.key, v); });
      if (keep.length) state.f[fc.key] = keep; else delete state.f[fc.key];
    });
  }
  var OUT_GROUP = { resolved: 'good', conclusive: 'good', partial: 'mid', inconclusive: 'mid',
    no_effect: 'bad', made_worse: 'bad', no_information: 'bad', unknown: 'none' };
  var SNIPPET_ORDER = ['symptoms', 'alarms', 'actions', 'findings', 'causes', 'problems', 'equipment', 'parts', 'quotes', 'title'];

  var $q = document.getElementById('q'), $f = document.getElementById('facets'),
      $r = document.getElementById('results'), $c = document.getElementById('count'),
      $a = document.getElementById('active'), $s = document.getElementById('sortnote');
  var state = { q: '', f: {}, open: {} };  // open: which facet lists are expanded (a facet with a selection opens itself)
  var ADMIN_PAGES = document.body.dataset.adminPages !== '0';

  // ---- url state
  function readUrl() {
    var p = new URLSearchParams(location.search);
    state.q = p.get('q') || '';
    state.f = {};
    FACETS.forEach(function (fc) {
      var v = p.getAll('f.' + fc.key);
      if (v.length) state.f[fc.key] = v;
    });
    pruneChildren();
    $q.value = state.q;
  }
  function writeUrl() {
    var p = new URLSearchParams();
    if (state.q) p.set('q', state.q);
    Object.keys(state.f).forEach(function (k) { state.f[k].forEach(function (v) { p.append('f.' + k, v); }); });
    var admin = new URLSearchParams(location.search).get('admin');
    if (admin) p.set('admin', admin);
    var qs = p.toString();
    history.replaceState(null, '', location.pathname + (qs ? '?' + qs : ''));
  }

  // ---- tokenising & scoring
  function tokens(s) {
    return (s || '').toLowerCase().replace(/[^\p{L}\p{N}\/\.\-]+/gu, ' ').split(' ')
      .map(function (t) { return t.replace(/^[\.\-\/]+|[\.\-\/]+$/g, ''); })
      .filter(function (t) { return t.length > 1; });
  }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function reEsc(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
  // A facet whose values are matched on another field. Symptom: a casefile carries
  // `symptom_broad` = its normalised symptoms plus every reviewed broader term above them
  // (config/symptom_links.json), so clicking the broader term reaches the narrower groups
  // while a narrower term still matches only itself. Mirrors index.facet_match.
  var FACET_MATCH = { symptom: 'symptom_broad' };
  function facetVals(d, k) { return d.facets[FACET_MATCH[k] || k] || d.facets[k] || []; }
  function matchesFacets(d) {
    return Object.keys(state.f).every(function (k) {
      var want = state.f[k], have = facetVals(d, k);
      return want.some(function (v) { return have.indexOf(v) >= 0; });
    });
  }
  function score(d, toks) {
    if (!toks.length) return { s: 1, hits: [] };
    var total = 0, hits = [], allFound = true;
    toks.forEach(function (t) {
      var found = false, best = 0;
      Object.keys(d.fields).forEach(function (fname) {
        var w = W[fname] || 1;
        d.fields[fname].forEach(function (e) {
          var lt = e.t.toLowerCase();
          var ix = lt.indexOf(t);
          if (ix < 0) return;
          found = true;
          // whole-word hit beats substring; title/symptoms weight dominates
          var whole = new RegExp('(^|[^\\p{L}\\p{N}])' + reEsc(t) + '([^\\p{L}\\p{N}]|$)', 'u').test(lt);
          var sc = w * (whole ? 1.0 : 0.6);
          if (sc > best) best = sc;
          hits.push({ field: fname, entry: e, sc: sc, tok: t });
        });
      });
      if (!found) allFound = false;
      total += best;
    });
    if (!allFound) return { s: 0, hits: [] };
    return { s: total, hits: hits };
  }
  function pickSnippet(d, hits, toks) {
    if (!hits.length) {
      var f0 = d.fields.symptoms[0] || d.fields.actions[0];
      return f0 ? { field: f0 === d.fields.symptoms[0] ? 'symptoms' : 'actions', entry: f0 } : null;
    }
    var byEntry = {};
    hits.forEach(function (h) {
      var key = h.field + '|' + h.entry.id + '|' + h.entry.t;
      byEntry[key] = byEntry[key] || { field: h.field, entry: h.entry, toks: {}, sc: 0 };
      byEntry[key].toks[h.tok] = 1; byEntry[key].sc += h.sc;
    });
    var cands = Object.keys(byEntry).map(function (k) { return byEntry[k]; });
    cands.sort(function (a, b) {
      var ta = Object.keys(a.toks).length, tb = Object.keys(b.toks).length;
      if (tb !== ta) return tb - ta;
      var oa = SNIPPET_ORDER.indexOf(a.field), ob = SNIPPET_ORDER.indexOf(b.field);
      if (oa !== ob) return oa - ob;
      return b.sc - a.sc;
    });
    return cands[0];
  }
  function highlight(text, toks) {
    if (!toks.length) return esc(text);
    var re = new RegExp('(' + toks.map(reEsc).join('|') + ')', 'gi');
    return esc(text).replace(re, '<mark>$1</mark>');
  }
  function trim(text, toks, max) {
    if (text.length <= max) return text;
    var lt = text.toLowerCase(), pos = -1;
    toks.forEach(function (t) { var i = lt.indexOf(t); if (i >= 0 && (pos < 0 || i < pos)) pos = i; });
    var start = Math.max(0, (pos < 0 ? 0 : pos) - Math.floor(max / 3));
    var out = text.slice(start, start + max);
    return (start > 0 ? '…' : '') + out + (start + max < text.length ? '…' : '');
  }
  function fmt(iso) {
    if (!iso) return '';
    var d = new Date(iso + 'T00:00:00Z');
    return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric', timeZone: 'UTC' });
  }

  // ---- render
  function run() {
    var toks = tokens(state.q);
    var pool = DOCS.filter(matchesFacets);
    var scored = pool.map(function (d) { var r = score(d, toks); return { d: d, s: r.s, hits: r.hits }; })
      .filter(function (x) { return x.s > 0; });
    scored.sort(function (a, b) { return b.s - a.s || (b.d.end || '').localeCompare(a.d.end || ''); });
    renderFacets(pool, toks);
    renderActive();
    $c.textContent = scored.length + ' of ' + DOCS.length + ' casefiles' + (toks.length ? ' match' : '');
    $s.textContent = toks.length ? 'ranked by weighted field hits' : 'most recent activity first';
    if (!scored.length) { $r.innerHTML = '<div class="empty">No casefile matches. Try fewer words, or clear a filter.</div>'; return; }
    $r.innerHTML = scored.map(function (x) { return resultCard(x.d, x.hits, toks); }).join('');
  }
  function resultCard(d, hits, toks) {
    var sn = pickSnippet(d, hits, toks);
    var href = 'casefile/' + d.id + '.html';
    var snip = '';
    if (sn) {
      var anchor = sn.entry.id ? href + '#' + sn.entry.id : href;
      var label = { symptoms: 'symptom', alarms: 'alarm', actions: 'action', findings: 'finding', causes: 'suspected cause',
        problems: 'problem', equipment: 'equipment', parts: 'part', quotes: 'quote', title: 'title' }[sn.field] || sn.field;
      var tail = '';
      if (sn.field === 'actions' && sn.entry.o) tail = ' <span class="arrow">→</span> <span class="chip ' + OUT_GROUP[sn.entry.o] + '">' + esc(sn.entry.o) + '</span>';
      snip = '<div class="snippet"><a href="' + anchor + '"><span class="src">' + label + '</span>' + highlight(trim(sn.entry.t, toks, 220), toks) + '</a>' + tail + '</div>';
    }
    var out = Object.keys(d.outcomes).map(function (k) { return '<span class="chip ' + OUT_GROUP[k] + '">' + d.outcomes[k] + ' ' + esc(k) + '</span>'; }).join('');
    var types = d.facets.equipment_type.map(function (t) { return '<span class="chip type">' + esc(t) + '</span>'; }).join('');
    var dates = d.start ? (fmt(d.start) + (d.end && d.end !== d.start ? ' → ' + fmt(d.end) : '')) : '';
    var adm = '<span class="adm">' + (d.needs_review ? '<span class="chip warn">needs review · ' + d.n_flags + ' flag' + (d.n_flags === 1 ? '' : 's') + '</span>' : '<span class="chip none">clean</span>') +
      (ADMIN_PAGES ? ' <a class="small" href="admin/journey/' + d.id + '.html">journey</a>' : '') + '</span>';
    return '<div class="result">' +
      '<div class="vessel">' + esc(d.vessel) + (d.imo ? ' · IMO ' + esc(d.imo) : '') + '</div>' +
      '<div class="title"><a href="' + href + '">' + highlight(d.title, toks) + '</a></div>' +
      '<div class="meta">' + (dates ? '<span class="nowrap">' + dates + '</span> · ' : '') + d.n_faults + ' fault' + (d.n_faults === 1 ? '' : 's') + ' · ' + d.n_actions + ' action' + (d.n_actions === 1 ? '' : 's') + (d.has_attachments ? ' · cites attachments' : '') + '</div>' +
      snip + '<div>' + types + out + adm + '</div></div>';
  }
  function renderFacets(pool, toks) {
    // counts within the current result pool (query + other facets), so numbers stay honest
    var scoredIds = {};
    pool.forEach(function (d) { if (score(d, toks).s > 0) scoredIds[d.id] = 1; });
    $f.innerHTML = FACETS.map(function (fc) {
      // a child facet appears only once its parent has a selection
      if (fc.parent && !(state.f[fc.parent] || []).length) return '';
      var counts = {};
      DOCS.forEach(function (d) {
        // for THIS facet, ignore its own selection so other values stay clickable
        var ok = Object.keys(state.f).every(function (k) {
          if (k === fc.key) return true;
          var have = facetVals(d, k);
          return state.f[k].some(function (v) { return have.indexOf(v) >= 0; });
        }) && score(d, toks).s > 0;
        if (!ok) return;
        // count on the matched field, so a broader term's count is what a click on it returns
        facetVals(d, fc.key).forEach(function (v) { if (parentSelected(fc.key, v)) counts[v] = (counts[v] || 0) + 1; });
      });
      var vals = Object.keys(counts).sort(function (a, b) {
        // pseudo values (unclassified / unresolved) always last, then by count, then by label
        var pa = PSEUDO[a] ? 1 : 0, pb = PSEUDO[b] ? 1 : 0;
        return pa - pb || counts[b] - counts[a] || label(fc.key, a).localeCompare(label(fc.key, b));
      });
      if (!vals.length) return '';
      var sel = state.f[fc.key] || [];
      var isOpen = !!state.open[fc.key] || sel.length > 0 || !!fc.parent;
      var items = vals.map(function (v) {
        var on = sel.indexOf(v) >= 0, pseudo = !!PSEUDO[v];
        var cls = (counts[v] ? '' : 'off ') + (pseudo ? 'pseudo' : '');
        var tip = pseudo ? 'no canonical term yet — see admin lookup' : label(fc.key, v);
        return '<label class="' + cls.trim() + '"><input type="checkbox" data-k="' + esc(fc.key) + '" data-v="' + esc(v) + '"' + (on ? ' checked' : '') + '> <span title="' + esc(tip) + '">' + esc(label(fc.key, v)) + '</span><span class="n">' + counts[v] + '</span></label>';
      }).join('');
      var head = '<h4 class="facet-head" data-toggle="' + esc(fc.key) + '" role="button" aria-expanded="' + isOpen + '">' +
        '<span class="caret">' + (isOpen ? '▾' : '▸') + '</span>' + esc(fc.label) +
        (sel.length ? '<span class="selcount">' + sel.length + '</span>' : '') +
        '<span class="hint">' + vals.length + '</span></h4>';
      var cls = 'facet' + (isOpen ? ' open' : '') + (fc.parent ? ' child' : '') + (fc.family ? ' family' : '');
      return '<div class="' + cls + '">' + head + '<div class="facet-list">' + items + '</div></div>';
    }).join('');
  }
  function renderActive() {
    var chips = [];
    Object.keys(state.f).forEach(function (k) { state.f[k].forEach(function (v) { chips.push('<span class="chip type" data-k="' + esc(k) + '" data-v="' + esc(v) + '">' + esc(k.replace(/_/g, ' ')) + ': ' + esc(label(k, v)) + '</span>'); }); });
    $a.innerHTML = chips.join('') + (chips.length ? ' <a href="#" id="clearAll" class="small">clear all</a>' : '');
  }

  // ---- events
  var timer;
  $q.addEventListener('input', function () { clearTimeout(timer); timer = setTimeout(function () { state.q = $q.value.trim(); writeUrl(); run(); }, 120); });
  $f.addEventListener('change', function (e) {
    var el = e.target; if (el.tagName !== 'INPUT') return;
    var k = el.dataset.k, v = el.dataset.v, arr = state.f[k] || [];
    if (el.checked) { if (arr.indexOf(v) < 0) arr.push(v); } else { arr = arr.filter(function (x) { return x !== v; }); }
    if (arr.length) state.f[k] = arr; else delete state.f[k];
    pruneChildren();
    writeUrl(); run();
  });
  $f.addEventListener('click', function (e) {
    var h = e.target.closest('[data-toggle]'); if (!h) return;
    var k = h.dataset.toggle, sel = state.f[k] || [];
    // a facet with selections is always open; toggling it closed clears nothing — it just stays open
    if (sel.length) return;
    state.open[k] = !state.open[k]; run();
  });
  $a.addEventListener('click', function (e) {
    if (e.target.id === 'clearAll') { e.preventDefault(); state.f = {}; writeUrl(); run(); return; }
    var ch = e.target.closest('.chip'); if (!ch) return;
    var k = ch.dataset.k, v = ch.dataset.v;
    state.f[k] = (state.f[k] || []).filter(function (x) { return x !== v; });
    if (!state.f[k].length) delete state.f[k];
    pruneChildren();
    writeUrl(); run();
  });
  window.addEventListener('popstate', function () { readUrl(); run(); });
  readUrl(); run();
})();

/* admin/lookup.html: the "needs attention" queue (12 §5.1) — show all, kind toggles, copy fix.
   Guarded on element presence; --no-admin builds have no queue. */
(function () {
  function initQueue() {
    var q = document.querySelector('details.queue'); if (!q) return;
    var rows = Array.prototype.slice.call(q.querySelectorAll('tbody tr[data-kind]'));
    var off = {}, showAll = false, more = q.querySelector('.queue-more'), shown = q.querySelector('[data-shown]');
    function apply() {
      var visible = 0;
      rows.forEach(function (tr) {
        var ok = !off[tr.dataset.kind] && (showAll || visible < 10);
        if (ok) visible += 1;
        tr.hidden = !ok;
      });
      if (shown) shown.textContent = String(visible);
      if (more && showAll) more.hidden = true;
    }
    q.querySelectorAll('.kindfilter [data-kind]').forEach(function (b) {
      b.addEventListener('click', function (e) {
        e.preventDefault(); e.stopPropagation();  // a click inside <summary> must not toggle the details
        off[b.dataset.kind] = !off[b.dataset.kind];
        b.classList.toggle('off', !!off[b.dataset.kind]);
        b.setAttribute('aria-pressed', off[b.dataset.kind] ? 'false' : 'true');
        apply();
      });
    });
    var all = q.querySelector('[data-action="show-all"]');
    if (all) all.addEventListener('click', function (e) { e.preventDefault(); showAll = true; apply(); });
    q.addEventListener('click', function (e) {
      var b = e.target.closest('.copyfix'); if (!b) return;
      var text = b.dataset.fix || '';
      function done() { b.classList.add('copied'); b.textContent = 'copied'; setTimeout(function () { b.classList.remove('copied'); b.textContent = 'copy fix'; }, 1200); }
      if (navigator.clipboard && navigator.clipboard.writeText) { navigator.clipboard.writeText(text).then(done, function () { fallback(text); done(); }); }
      else { fallback(text); done(); }
    });
    function fallback(text) {  // file:// contexts: select a temporary input and copy
      var i = document.createElement('input'); i.value = text; i.setAttribute('readonly', '');
      i.style.position = 'fixed'; i.style.opacity = '0'; document.body.appendChild(i); i.select();
      try { document.execCommand('copy'); } catch (e) { /* nothing more to do without a clipboard */ }
      document.body.removeChild(i);
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initQueue); else initQueue();
})();
