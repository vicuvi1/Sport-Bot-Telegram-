/* Workout Crew Mini App. Plain JS, no build step, no dependencies (effects in fx.js).
 *
 * All data comes from GET /api/state; every change goes through POST /api/action
 * (see webapp/server.py). GET /api/pulse is polled every few seconds so the duel,
 * live training and chat update without reloading. Each call carries Telegram's
 * signed initData, which the server verifies. Outside Telegram the app uses the
 * local dev server (python -m webapp.dev) when it answers, otherwise a demo.
 */
(function () {
  'use strict';

  var tg = window.Telegram && window.Telegram.WebApp;
  var initData = tg && tg.initData;
  var FX = window.FX;
  var params = new URLSearchParams(location.search);
  var DEMO = false;           // set at start: no Telegram and no dev server (sample data)
  var DEV = false;            // set at start: local dev server (python -m webapp.dev)
  var devAs = params.get('as');
  var startRoute = params.get('screen');

  var app = document.getElementById('app');
  var toastEl = document.getElementById('toast');
  var ptr = document.getElementById('ptr');

  var TABS = ['home', 'today', 'duel', 'chat', 'progress'];
  var state = null;
  var pulseData = null;
  var tab = 'home';
  var screen = null;          // full-screen page: settings | exercises | test | gear | body | live | player | report
  var sheet = null;           // {kind: 'challenge' | 'roast' | 'day' | 'bet' | 'image', ...}
  var celebration = null;     // {kind: 'done' | 'record' | 'rank', ...}
  var busy = false;
  var wakeDismissed = false;  // "Not now" on the wake-up check (until reload)
  var editingItem = null;     // Today: item id being typed in
  var animateNext = true;     // ring fill / count-up / slide-in when a screen opens
  var slideDir = '';
  var afterRender = [];       // effects that need the new DOM (hits, floating numbers)
  var prevVals = {};          // last bar/ring values, so changes animate from there
  var restLeft = 0, restTimer = null;
  var testTimer = { key: null, left: 0, handle: null };
  var photoUrls = {};         // progress photo id -> blob: URL (loaded with auth)
  var proofUrls = {};         // proof clip id -> blob: URL
  var chatDraft = '';
  var showStickers = false;
  var chatSeenSent = 0;
  var chatNewFrom = 0;         // messages after this id slide in
  var pendingReload = false;
  var reportWhich = 'last';
  var player = null;
  var wakeLock = null;

  // ---------------------------------------------------------------- helpers

  function h(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function haptic(kind) {
    if (!tg || !tg.HapticFeedback) return;
    try {
      if (kind === 'success' || kind === 'error' || kind === 'warning') tg.HapticFeedback.notificationOccurred(kind);
      else tg.HapticFeedback.impactOccurred(kind || 'light');
    } catch (e) { /* older Telegram */ }
  }

  // toast(text) | toast(text, {undo: values}) | toast(text, {button: {label, act, ...data}})
  var toastTimer = null, undoValues = null;
  function toast(message, opts) {
    opts = opts || {};
    if (!message && !opts.undo) return;
    undoValues = opts.undo || null;
    var extra = '';
    if (opts.undo) extra = ' <button class="toast-undo" data-act="undo">Undo</button>';
    else if (opts.button) {
      var b = opts.button, attrs = '';
      Object.keys(b).forEach(function (k) { if (k !== 'label') attrs += ' data-' + k + '="' + h(b[k]) + '"'; });
      extra = ' <button class="toast-btn"' + attrs + '>' + h(b.label) + '</button>';
    }
    toastEl.innerHTML = h(message || 'Saved.') + extra;
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.hidden = true; undoValues = null; }, extra ? 5000 : 2600);
  }

  function clamp(p) { return Math.max(0, Math.min(1, p || 0)); }

  // Rings and bars with a key animate from their previous value to the new one.
  function ring(size, stroke, progress, color, inner, key) {
    var r = (size - stroke) / 2;
    var c = 2 * Math.PI * r;
    var dash = (clamp(progress) * c).toFixed(1) + ' ' + c.toFixed(1);
    var from = null;
    if (animateNext) from = '0 ' + c.toFixed(1);
    else if (key && prevVals['r' + key] && prevVals['r' + key] !== dash) from = prevVals['r' + key];
    if (key) prevVals['r' + key] = dash;
    return '<div class="ring" style="width:' + size + 'px;height:' + size + 'px">' +
      '<svg width="' + size + '" height="' + size + '" viewBox="0 0 ' + size + ' ' + size + '" aria-hidden="true">' +
      '<circle cx="' + size / 2 + '" cy="' + size / 2 + '" r="' + r + '" fill="none" stroke="#2A2E37" stroke-width="' + stroke + '"></circle>' +
      '<circle class="fg" cx="' + size / 2 + '" cy="' + size / 2 + '" r="' + r + '" fill="none" stroke="' + h(color) + '" stroke-width="' + stroke +
      '" stroke-linecap="round" stroke-dasharray="' + (from || dash) + '"' + (from ? ' data-dash="' + dash + '"' : '') +
      ' transform="rotate(-90 ' + size / 2 + ' ' + size / 2 + ')"></circle></svg>' +
      '<div class="val">' + inner + '</div></div>';
  }

  function bar(p, color, key) {
    var w = Math.round(clamp(p) * 100), from = null;
    if (key) {
      if (animateNext) from = 0;
      else if (prevVals['b' + key] != null && prevVals['b' + key] !== w) from = prevVals['b' + key];
      prevVals['b' + key] = w;
    }
    return '<div class="bar"><i style="width:' + (from === null ? w : from) + '%;background:' + h(color || 'var(--me)') + '"' +
      (from === null ? '' : ' data-to="' + w + '"') + '></i></div>';
  }

  function count(value, suffix) {
    return '<span data-count="' + value + '" data-suffix="' + h(suffix || '') + '">' + (animateNext ? 0 : value) + h(suffix || '') + '</span>';
  }

  function pct(p) { return Math.round(p * 100) + '%'; }

  // Original chibi mascot. mood: determined | cheer | sleepy | smirk | sulk.
  // opts: {mine: plays your reactions, sweat: working hard}
  function mascot(gear, mood, size, label, opts) {
    opts = opts || {};
    var hair = gear.hair || '#C8F135', band = gear.band || '#FF8A3D', aura = gear.aura || '';
    var eyes, mouth;
    if (mood === 'sleepy') {
      eyes = '<path d="M33 63 Q39 67 45 63" stroke="#141414" stroke-width="3.5" fill="none" stroke-linecap="round"/><path d="M55 63 Q61 67 67 63" stroke="#141414" stroke-width="3.5" fill="none" stroke-linecap="round"/>';
      mouth = '<ellipse cx="50" cy="77" rx="4" ry="5" fill="#141414"/>';
    } else if (mood === 'cheer') {
      eyes = '<path d="M33 64 Q39 58 45 64" stroke="#141414" stroke-width="3.5" fill="none" stroke-linecap="round"/><path d="M55 64 Q61 58 67 64" stroke="#141414" stroke-width="3.5" fill="none" stroke-linecap="round"/>';
      mouth = '<path d="M40 73 Q50 86 60 73 Z" fill="#141414"/>';
    } else if (mood === 'smirk') {
      eyes = '<path d="M33 62 Q39 58 45 62" stroke="#141414" stroke-width="3.5" fill="none" stroke-linecap="round"/><path d="M55 62 Q61 58 67 62" stroke="#141414" stroke-width="3.5" fill="none" stroke-linecap="round"/>';
      mouth = '<path d="M44 77 Q53 80 60 73" stroke="#141414" stroke-width="3" fill="none" stroke-linecap="round"/>';
    } else if (mood === 'sulk') {
      eyes = '<path d="M32 56 L44 53" stroke="#141414" stroke-width="3" stroke-linecap="round"/><path d="M68 56 L56 53" stroke="#141414" stroke-width="3" stroke-linecap="round"/>' +
        '<ellipse cx="39" cy="64" rx="4" ry="5" fill="#141414"/><ellipse cx="61" cy="64" rx="4" ry="5" fill="#141414"/>';
      mouth = '<path d="M42 80 Q50 73 58 80" stroke="#141414" stroke-width="3" fill="none" stroke-linecap="round"/>';
    } else {
      eyes = '<path d="M31 53 L45 57" stroke="#141414" stroke-width="3.5" stroke-linecap="round"/><path d="M69 53 L55 57" stroke="#141414" stroke-width="3.5" stroke-linecap="round"/>' +
        '<ellipse cx="39" cy="64" rx="5.5" ry="7.5" fill="#141414"/><ellipse cx="61" cy="64" rx="5.5" ry="7.5" fill="#141414"/>' +
        '<circle cx="41" cy="61" r="2.2" fill="#fff"/><circle cx="63" cy="61" r="2.2" fill="#fff"/>';
      mouth = '<path d="M42 76 Q50 83 58 76" stroke="#141414" stroke-width="3" fill="none" stroke-linecap="round"/>';
    }
    var glow = aura ? '<g class="m-aura"><circle cx="50" cy="52" r="47" fill="none" stroke="' + h(aura) + '" stroke-width="3" stroke-dasharray="6 7" opacity=".9"/></g>' +
      '<circle cx="50" cy="52" r="42" fill="' + h(aura) + '" opacity=".14"/>' : '';
    return '<svg class="mascot' + (opts.mine ? ' mine' : '') + '" xmlns="http://www.w3.org/2000/svg" width="' + size + '" height="' + size +
      '" viewBox="0 0 100 100" role="img" aria-label="' + h(label || 'Mascot') + '">' + glow + '<g class="m-body">' +
      '<path d="M16 56 L6 32 L26 40 L22 14 L40 30 L50 4 L60 30 L78 14 L74 40 L94 32 L84 56 Z" fill="' + h(hair) + '"/>' +
      '<circle cx="50" cy="60" r="29" fill="#FFD9B8"/>' +
      '<rect x="21" y="43" width="58" height="9" rx="3" fill="' + h(band) + '"/>' +
      '<path d="M78 45 L96 38 L90 52 Z" fill="' + h(band) + '"/><g class="m-eyes">' + eyes + '</g>' +
      '<ellipse cx="30" cy="72" rx="4.5" ry="2.2" fill="#FF9E9E"/><ellipse cx="70" cy="72" rx="4.5" ry="2.2" fill="#FF9E9E"/>' + mouth +
      (opts.sweat ? '<path class="m-sweat" d="M80 52 Q84 59 80 62 Q76 59 80 52 Z" fill="#7FB2FF"/>' : '') +
      (mood === 'sleepy' ? '<text x="78" y="26" fill="#A3A8B3" font-size="16" font-weight="700">z</text><text x="88" y="14" fill="#A3A8B3" font-size="12" font-weight="700">z</text>' : '') +
      '</g></svg>';
  }

  var ICONS = {
    home: '<path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"/>',
    today: '<path d="M6 7v10M18 7v10M3 10v4M21 10v4M6 12h12"/>',
    duel: '<path d="M4 20L14 10M10 4l10 10M20 20L10 10M14 4L4 14"/>',
    chat: '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 21l1.9-5.4A8 8 0 1 1 21 12z"/>',
    progress: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    flame: '<path d="M12 2c1 4 5 5 5 10a5 5 0 0 1-10 0c0-3 2-4 2-7 1 1 2 2 3 4"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
    back: '<path d="M15 18l-6-6 6-6"/>',
    lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
    play: '<path d="M7 4l13 8-13 8z"/>'
  };
  function icon(name, size, color) {
    return '<svg width="' + (size || 24) + '" height="' + (size || 24) + '" viewBox="0 0 24 24" fill="none" stroke="' + (color || 'currentColor') +
      '" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + ICONS[name] + '</svg>';
  }

  function me() { return state.me; }
  function anime() { return state.me.anime; }
  function others() { return state.crew.filter(function (m) { return !m.me; }); }
  function meCrew() { return state.crew.filter(function (m) { return m.me; })[0]; }
  function member(id) { return state.crew.filter(function (m) { return m.id === id; })[0]; }
  function findItem(id) { return state.today.items.filter(function (i) { return i.id === id; })[0]; }
  function livePulse(id) {
    var p = pulseData && pulseData.crew.filter(function (c) { return c.id === id; })[0];
    return p || member(id);
  }
  var RANK_LETTERS = ['E', 'D', 'C', 'B', 'A', 'S'];

  function title(m) {
    if (m.title) return m.title;
    if (/late|😴|⏰/.test(m.wake || '')) return 'The Sleepyhead';
    if (m.status === 'completed') return 'The Relentless';
    if (m.streak === 0) return 'The Comeback Kid';
    return 'The Grinder';
  }

  function myMood(progress, status) {
    if (status === 'completed') return 'cheer';
    if (!progress && new Date().getHours() >= 18) return 'sulk';
    return 'determined';
  }

  function greeting() {
    var hour = new Date().getHours();
    return hour < 12 ? 'Morning' : hour < 18 ? 'Afternoon' : 'Evening';
  }

  function niceDate(iso) {
    var d = new Date(iso + 'T12:00:00');
    return d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' });
  }

  function monthName(month, long) {
    return new Date(month + '-15T12:00:00').toLocaleDateString(undefined, { month: long === false ? 'short' : 'long' });
  }

  function ago(isoUtc) {
    var s = Math.max(0, (Date.now() - new Date(isoUtc).getTime()) / 1000);
    if (s < 60) return 'just now';
    if (s < 3600) return Math.floor(s / 60) + ' min ago';
    if (s < 86400) return Math.floor(s / 3600) + ' h ago';
    return Math.floor(s / 86400) + ' d ago';
  }

  function backHeader(text, extra) {
    return '<header class="row"><button class="btn icon" data-act="close-screen" aria-label="Back">' + icon('back', 20) + '</button>' +
      '<div class="display h1 grow">' + text + '</div>' + (extra || '') + '</header>';
  }

  function toggle(on, act, label, extra) {
    return '<button class="switch' + (on ? ' on' : '') + '" role="switch" aria-checked="' + on + '" aria-label="' + h(label) +
      '" data-act="' + act + '"' + (extra || '') + '><i></i></button>';
  }

  // ---------------------------------------------------------------- tabs

  function homeScreen() {
    var t = state.today, r = me().rank, mine = meCrew();
    var parts = [];
    var eyebrow = anime()
      ? '<span class="jp me-c">修行編</span> · Training arc, day ' + (state.streak + (t.status === 'completed' ? 0 : 1))
      : h(niceDate(t.date));
    parts.push('<header class="row between"><div class="grow"><div class="small muted">' + eyebrow + '</div>' +
      '<div class="display h1">' + h(greeting()) + ', ' + h(me().name) + '</div>' +
      '<div class="row small" style="gap:6px 8px;margin-top:4px;flex-wrap:wrap"><span class="badge" style="background:var(--me)">RANK ' + h(r.letter) + '</span>' +
      '<span class="muted">' + r.xp + ' XP' + (r.next_at ? ' · ' + (r.next_at - r.xp) + ' to ' + h(r.next_letter) : ' · max rank') + '</span>' +
      (mine && mine.title ? '<span class="badge" style="background:#C8A2FF">' + h(mine.title) + '</span>' : '') + '</div></div>' +
      '<button class="btn icon" data-act="open-screen" data-screen="settings" aria-label="Settings">' + icon('gear', 22) + '</button></header>');

    others().forEach(function (o) {
      if (livePulse(o.id).live) {
        parts.push('<button class="card tap row live-banner" data-act="open-screen" data-screen="live"><span class="live-dot"></span>' +
          '<span class="grow"><b>' + h(o.name) + ' is training live</b><br><span class="small muted">Join in and race them</span></span><b class="me-c">Join ›</b></button>');
      }
    });

    if (anime()) {
      var line;
      if (t.paused_until) line = 'Vacation arc. Recharge, then we come back stronger.';
      else if (t.status === 'rest') line = 'Rest day. Even heroes recover.';
      else if (t.status === 'completed') line = 'Today is ours. Rest up, legend.';
      else if (t.progress >= 0.7) line = 'Almost there! Don\'t stop now!';
      else if (t.done_count > 0) line = (t.items.length - t.done_count) + ' more to go. Finish it!';
      else if (new Date().getHours() >= 18) line = 'It\'s getting late… I believed in you 😢';
      else line = 'Day ' + (state.streak + 1) + ' of the arc. Let\'s go!';
      parts.push('<div class="row"><button class="mascot-btn" data-act="open-screen" data-screen="gear" aria-label="Customize your fighter">' +
        mascot(me().gear, myMood(t.progress, t.status), 64, 'Your mascot', { mine: true, sweat: t.progress >= 0.7 && t.status !== 'completed' }) + '</button>' +
        '<div class="bubble grow">' + h(line) + '</div></div>');
    }

    var status = t.paused_until ? 'Paused until ' + h(niceDate(t.paused_until))
      : t.status === 'rest' ? 'Rest day' : t.done_count + ' of ' + t.items.length + ' exercises';
    parts.push('<section class="card row" style="gap:16px">' +
      ring(116, 12, t.progress, me().gear.hair, '<span class="display" style="font-size:34px">' + count(Math.round(t.progress * 100), '%') + '</span><span class="small muted">today</span>', 'home') +
      '<div class="grow" style="display:flex;flex-direction:column;gap:10px">' +
      '<div><div class="small muted">Done</div><div class="display h2">' + status + '</div></div>' +
      '<div class="row small" style="flex-wrap:wrap">' + icon('flame', 18, 'var(--them)') + '<span><b>' + state.streak + '-day</b> streak · best ' + state.best_streak + '</span></div>' +
      (t.status === 'pending' ? '<button class="btn primary" data-act="start-player">' + icon('play', 16) + ' Start workout</button>' : '') +
      '</div></section>');

    parts.push('<section class="card"><div class="small muted">This week</div>' + dayGrid(state.week) + '</section>');
    parts.push(answerCards(true));

    if (others().length) {
      var rows = others().map(function (o) {
        var p = livePulse(o.id);
        return '<div class="row"><span class="display avatar" style="background:' + h(o.color) + '">' + h(o.name.charAt(0)) + '</span>' +
          '<div class="grow"><div class="row between"><b>' + h(o.name) + ' <span class="muted small">rank ' + h(o.rank) + '</span>' +
          (p.live ? ' <span class="live-dot"></span>' : '') + '</b><span class="small muted">' +
          (o.status === 'completed' ? 'done' : o.done + ' of ' + o.total) + ' · ' + o.streak + '-day streak</span></div>' + bar(p.progress, o.color, 'home' + o.id) + '</div></div>';
      }).join('');
      var pts = state.points.map(function (p) { return '<b style="color:' + h(p.color) + '">' + p.points + '</b>'; }).join(' <span class="muted">vs</span> ');
      var latest = state.activity.filter(function (a) { return !a.me; })[0];
      parts.push('<button class="card tap" data-act="tab" data-tab="duel"><div class="row between small muted"><span>Your crew</span><span>Duel ›</span></div>' +
        rows + '<div class="row between small"><span>Points this week</span><span>' + pts + '</span></div>' +
        (latest ? '<div class="small muted">Latest: <b style="color:var(--text)">' + h(latest.who) + '</b> ' + h(latest.text) + ' · ' + h(ago(latest.at)) + '</div>' : '') +
        '</button>');
    } else {
      parts.push('<section class="card dashed small muted">Train with your brother: in the bot, open <b>/crew</b> and tap <b>Invite</b>.</section>');
    }

    var tiles = [];
    var w = state.wake;
    if (w.enabled) {
      var wt = w.today;
      var wakeText = !wt ? 'Check at ' + h(w.time) : wt.status === 'on_time' ? h(wt.answered_at) + ' · on time'
        : wt.status === 'late' ? h(wt.answered_at) + ' · ' + wt.minutes_late + ' min late' : wt.status === 'missed' ? 'Overslept' : 'Waiting…';
      tiles.push('<div class="card">' + icon('sun', 22, 'var(--me)') + '<span class="small muted">Wake-up · streak ' + w.streak + '</span>' +
        '<span class="display h2">' + wakeText + '</span></div>');
    }
    var ft = state.fitness_test;
    tiles.push('<button class="card tap" data-act="open-screen" data-screen="test">' + icon('progress', 22, 'var(--me)') +
      '<span class="small muted">Fitness test</span><span class="display h2">' + (ft.complete ? 'Done this month ✓' : 'Take it now') + '</span></button>');
    parts.push('<div class="grid2">' + tiles.join('') + '</div>');
    return parts.join('');
  }

  function dayGrid(days) {
    return '<div class="days">' + ['M', 'T', 'W', 'T', 'F', 'S', 'S'].map(function (d) { return '<span class="small muted">' + d + '</span>'; }).join('') +
      days.map(function (d) {
        var past = d.code !== 'future';
        return '<button class="day ' + h(d.code) + '"' + (past ? ' data-act="day" data-date="' + h(d.date) + '"' : ' disabled') +
          ' aria-label="' + h(d.date + ': ' + d.code) + '"></button>';
      }).join('') + '</div>';
  }

  // Challenges and bets waiting for my answer, forfeits I owe (Home and Duel).
  function answerCards(withBets) {
    var out = '';
    state.challenges.forEach(function (c) {
      if (!c.mine_to_answer) return;
      out += '<section class="card"><div class="small muted">' + h(c.from) + ' challenges you</div><div class="display h2">' + h(c.label) + '</div>' +
        '<div class="grid2"><button class="btn done" data-act="challenge-answer" data-id="' + c.id + '" data-accept="1">Accept</button>' +
        '<button class="btn" data-act="challenge-answer" data-id="' + c.id + '" data-accept="0">Chicken out</button></div></section>';
    });
    if (withBets) {
      state.bets.forEach(function (b) {
        if (!b.mine_to_answer) return;
        out += '<section class="card"><div class="small muted">🎲 ' + h(b.rival) + ' bets ' + b.stake + ' pts</div><div class="display h2">' + h(b.label) + '</div>' +
          betAnswerButtons(b) + '</section>';
      });
    }
    state.forfeits.forEach(function (f) {
      if (!f.mine_to_do) return;
      out += '<section class="card"><div class="small muted">Forfeit from ' + h(f.winner) + '</div><div class="display h2">' + h(f.task) + '</div>' +
        '<button class="btn done" data-act="forfeit-done" data-id="' + f.id + '">Done it</button></section>';
    });
    return out;
  }

  function betAnswerButtons(b) {
    return '<div class="grid2"><button class="btn done" data-act="bet-answer" data-id="' + b.id + '" data-accept="1">Bet\'s on</button>' +
      '<button class="btn" data-act="bet-answer" data-id="' + b.id + '" data-accept="0">Back out</button></div>';
  }

  function exerciseCard(it) {
    var done = it.status === 'completed';
    var number = editingItem === it.id
      ? '<form class="row" data-form="set-amount" data-id="' + it.id + '" style="gap:6px"><input class="input" type="number" inputmode="numeric" min="0" max="5000" name="value" value="' + it.done +
        '" aria-label="Amount of ' + h(it.name) + '" style="width:90px"><button class="btn done" type="submit">Save</button></form>'
      : '<button class="num-btn" data-act="edit-amount" data-id="' + it.id + '" aria-label="Type the amount for ' + h(it.name) + '">' +
        '<span class="num"><span class="num-val" style="color:' + (done ? 'var(--me)' : 'var(--text)') + '">' + it.done + '</span><small> / ' + it.target + ' ' + h(it.unit) + '</small></span></button>';
    return '<section class="card ex' + (it.status === 'skipped' ? ' skipped' : '') + (done ? ' done-glow' : '') + '" data-item="' + it.id + '">' +
      '<div class="row between"><b style="font-size:16px">' + h(it.name) + '</b>' + number + '</div>' +
      bar(it.target ? it.done / it.target : 1, null, 'ex' + it.id) +
      '<div class="grid4">' +
      '<button class="btn" data-act="delta" data-id="' + it.id + '" data-delta="-5" aria-label="Minus 5 ' + h(it.name) + '">−5</button>' +
      '<button class="btn" data-act="delta" data-id="' + it.id + '" data-delta="5" aria-label="Plus 5 ' + h(it.name) + '">+5</button>' +
      '<button class="btn" data-act="delta" data-id="' + it.id + '" data-delta="10" aria-label="Plus 10 ' + h(it.name) + '">+10</button>' +
      '<button class="btn' + (done ? ' done' : '') + '" data-act="item-done" data-id="' + it.id + '">' + (done ? 'Done ✓' : 'Done') + '</button>' +
      '</div></section>';
  }

  function todayScreen() {
    var t = state.today;
    var parts = [];
    parts.push('<header class="row"><div class="grow"><div class="display h1">Today' +
      (anime() ? ' <span class="jp me-c" style="font-size:17px">修行</span>' : '') + '</div>' +
      '<div class="small muted">' + h(niceDate(t.date)) + ' · ' + t.done_count + ' of ' + t.items.length + ' done</div></div>' +
      ring(56, 7, t.progress, me().gear.hair, '<span class="display" style="font-size:17px">' + pct(t.progress) + '</span>', 'today') + '</header>');

    if (t.paused_until) {
      parts.push('<section class="card"><div class="display h2">Paused until ' + h(niceDate(t.paused_until)) + '</div>' +
        '<div class="small muted">No reminders, streak frozen.</div><button class="btn done" data-act="resume">Resume now</button></section>');
      return parts.join('');
    }
    if (t.status === 'rest') {
      parts.push('<section class="card"><div class="display h2">Rest day</div><div class="small muted">Recovery is part of training.</div></section>');
      return parts.join('');
    }
    if (t.status === 'pending') {
      parts.push('<div class="' + (others().length ? 'grid2' : '') + '" style="display:grid;gap:10px">' +
        '<button class="btn primary" data-act="start-player">' + icon('play', 16) + ' Guided workout</button>' +
        (others().length ? '<button class="btn outline" style="height:52px" data-act="open-screen" data-screen="live"><span class="live-dot"></span> Train live</button>' : '') + '</div>');
    }
    if (t.quick) parts.push('<div class="small muted">Quick workout: targets halved, still counts for your streak.</div>');
    if (t.comeback_pct) parts.push('<div class="small muted">Comeback day: targets at ' + t.comeback_pct + '% to ease back in.</div>');

    t.items.forEach(function (it) { parts.push(exerciseCard(it)); });

    if (t.can_give_feedback) {
      parts.push('<section class="card"><b>How did it feel?</b><span class="small muted">Your answer tunes your next targets.</span><div class="grid3">' +
        state.feedback_options.map(function (o) {
          return '<button class="btn" style="font-size:13px;padding:0 4px" data-act="feedback" data-value="' + h(o.key) + '">' + h(o.label) + '</button>';
        }).join('') + '</div></section>');
    } else if (t.feedback) {
      parts.push('<div class="small muted">You said: ' + h((state.feedback_options.filter(function (o) { return o.key === t.feedback; })[0] || {}).label) + '</div>');
    }

    parts.push('<div class="grid3">' +
      '<button class="btn outline" data-act="rest" data-secs="60">' + (restLeft ? 'Rest ' + restLeft + 's' : 'Rest 60s') + '</button>' +
      '<button class="btn outline" data-act="rest" data-secs="90">Rest 90s</button>' +
      (t.status === 'pending' ? '<button class="btn outline" data-act="quick" data-on="' + (t.quick ? '0' : '1') + '">' + (t.quick ? 'Full workout' : 'Short on time') + '</button>' : '<span></span>') +
      '</div>');
    if (t.status === 'pending') parts.push('<button class="btn outline" data-act="complete-all">Complete all</button>');
    parts.push('<button class="btn outline" data-act="open-screen" data-screen="exercises">Edit exercises</button>');
    return parts.join('');
  }

  // The anime arena: your training drains their HP, theirs drains yours.
  function arena(a, b) {
    var lines = '';
    for (var i = 0; i < 20; i++) lines += '<line x1="64" y1="0" x2="340" y2="0" transform="rotate(' + i * 18 + ')"/>';
    var pa = livePulse(a.id), pb = livePulse(b.id);
    function fighter(m, p, hp, side) {
      var mood = side === 'mine' ? myMood(p.progress, p.status)
        : p.status === 'completed' ? 'cheer' : /late|😴|⏰/.test(m.wake || '') ? 'sleepy' : hp < 0.3 ? 'sulk' : 'smirk';
      return '<div class="fighter ' + side + '" data-fighter="' + m.id + '"><div class="mascot-wrap">' +
        mascot(m.gear, mood, 92, m.name + '\'s fighter', { mine: side === 'mine', sweat: p.progress >= 0.7 && p.status !== 'completed' }) + '</div>' +
        '<b>' + h(m.me ? 'You' : m.name) + ' <span class="badge" style="background:' + h(m.gear.hair || m.color) + '">' + h(m.rank) + '</span>' +
        (p.live ? ' <span class="live-dot"></span>' : '') + '</b>' +
        '<span class="small muted" style="font-style:italic">' + h(title(m)) + '</span>' +
        '<div class="hp"><div class="row between small muted" style="font-size:11px"><span>HP</span><span>' + Math.round(hp * 100) + '</span></div>' +
        bar(hp, hp < 0.3 ? '#FF4D5E' : (m.gear.hair || m.color), 'hp' + m.id) + '</div>' +
        (hp <= 0 ? '<span class="ko">K.O.</span>' : '') + '</div>';
    }
    return '<section class="card arena faceoff" aria-label="Today\'s duel">' +
      '<svg class="lines" viewBox="0 0 350 250" preserveAspectRatio="xMidYMid slice" aria-hidden="true"><g transform="translate(175 104)" stroke="#2C313B" stroke-width="2">' + lines + '</g></svg>' +
      '<span class="kanji" aria-hidden="true">対決</span>' +
      '<div class="fighters">' + fighter(a, pa, 1 - pb.progress, 'mine') + fighter(b, pb, 1 - pa.progress, 'rival') + '</div>' +
      '<svg class="vs-burst" width="64" height="64" viewBox="0 0 64 64" aria-hidden="true"><path d="M32 2 L38 20 L58 12 L46 28 L62 36 L42 40 L48 60 L32 46 L16 60 L22 40 L2 36 L18 28 L6 12 L26 20 Z" fill="#F2F2EE"/>' +
      '<text x="32" y="39" text-anchor="middle" fill="#0F1115" font-size="17" font-family="Dela Gothic One, sans-serif">VS</text></svg></section>';
  }

  function duelScreen() {
    var parts = ['<header class="row between"><div class="display h1">Duel</div><div class="small muted">' + h(niceDate(state.today.date)) +
      ' · crew streak ' + state.crew_streak + '</div></header>'];
    if (!others().length) {
      parts.push('<section class="card"><div class="display h2">A duel needs two</div>' +
        '<div class="small muted">In the bot, open /crew and tap Invite. Send the link to your brother.</div></section>');
      return parts.join('');
    }
    var mine = meCrew(), rival = others()[0];
    if (livePulse(rival.id).live) {
      parts.push('<button class="card tap row live-banner" data-act="open-screen" data-screen="live"><span class="live-dot"></span>' +
        '<b class="grow">' + h(rival.name) + ' is training live right now</b><b class="me-c">Join ›</b></button>');
    }
    if (anime()) {
      parts.push(arena(mine, rival));
    } else {
      parts.push('<section class="card"><div class="grid2">' + [mine, rival].map(function (m) {
        var p = livePulse(m.id);
        return '<div style="display:flex;flex-direction:column;align-items:center;gap:6px" data-fighter="' + m.id + '">' +
          ring(108, 11, p.progress, m.gear.hair || m.color, '<span class="display" style="font-size:30px">' + pct(p.progress) + '</span>', 'duel' + m.id) +
          '<b>' + h(m.me ? 'You' : m.name) + ' · ' + h(m.rank) + '</b><span class="small muted">' + m.done + '/' + m.total + ' · ' + m.reps + ' reps · ' + m.streak + ' days</span></div>';
      }).join('') + '</div></section>');
    }

    if (state.ghost) {
      var g = state.ghost, ahead = g.me_today >= g.rival_yesterday;
      parts.push('<section class="card ghost"><span class="small muted">Ghost pace · ' + h(g.time) + '</span>' +
        '<div>Yesterday at this time <b>' + h(rival.name) + '</b> had <b>' + g.rival_yesterday + ' reps</b>. You have <b class="' + (ahead ? 'me-c' : 'them-c') + '">' + g.me_today + '</b>.</div>' +
        bar(g.rival_yesterday ? g.me_today / g.rival_yesterday : 1, ahead ? 'var(--me)' : 'var(--them)', 'ghost') + '</section>');
    }

    var total = state.points.reduce(function (s, p) { return s + Math.max(0, p.points); }, 0) || 1;
    parts.push('<section class="card"><div class="row between small muted"><span>Points this week</span><span>Sunday 20:30</span></div>' +
      '<div class="row between display big">' + state.points.map(function (p) { return '<span style="color:' + h(p.color) + '">' + h(p.name) + ' ' + p.points + '</span>'; }).join('') + '</div>' +
      '<div style="display:flex;gap:3px;height:10px;border-radius:5px;overflow:hidden">' + state.points.map(function (p) {
        return '<i style="flex:' + Math.max(p.points, 1) / total + ';background:' + h(p.color) + '"></i>';
      }).join('') + '</div></section>');

    others().forEach(function (o) {
      parts.push('<div class="grid3"><button class="btn roast" data-act="roast" data-target="' + o.id + '">Roast 🔥</button>' +
        '<button class="btn roast" data-act="open-roast" data-target="' + o.id + '">Write one</button>' +
        '<button class="btn" style="height:50px" data-act="hype" data-target="' + o.id + '">Hype 💪</button></div>');
    });
    parts.push('<div class="grid3"><button class="btn outline" style="height:48px" data-act="open-challenge">Challenge</button>' +
      '<button class="btn outline" style="height:48px" data-act="open-bet">Bet 🎲</button>' +
      '<button class="btn outline" style="height:48px" data-act="open-screen" data-screen="live"><span class="live-dot"></span> Live</button></div>');
    parts.push(betsCard());
    parts.push(answerCards(false));
    state.forfeits.forEach(function (f) {
      if (f.mine_to_do || !f.task) return;
      parts.push('<section class="card dashed small muted">' + h(f.loser) + ' still owes: <b style="color:var(--text)">' + h(f.task) + '</b></section>');
    });

    parts.push('<section class="card"><div class="row between"><b>Fight log</b><span class="small muted">' + (anime() ? '<span class="jp">戦記</span>' : 'Live') + '</span></div>' +
      (state.activity.length ? state.activity.slice(0, 20).map(logEntry).join('') : '<span class="small muted">Nothing yet today. Make the first move.</span>') + '</section>');
    return parts.join('');
  }

  function betsCard() {
    if (!state.bets.length) return '';
    return '<section class="card"><div class="row between"><b>Bets today 🎲</b><span class="small muted">settled at 23:00</span></div>' +
      state.bets.map(function (b) {
        var status = b.status === 'offered' ? (b.mine_to_answer ? 'Your answer?' : 'Waiting for ' + h(b.rival))
          : b.status === 'accepted' ? (b.score ? 'You ' + b.score.me + ' · ' + h(b.rival) + ' ' + b.score.rival : 'First to finish wins')
          : b.status === 'won' ? (b.result === 'won' ? '💰 You won +' + b.stake : '💸 You lost −' + b.stake)
          : b.status === 'draw' ? 'Draw 🤝' : b.status === 'declined' ? h(b.rival) + ' backed out' : 'Expired';
        var leading = b.score && b.status === 'accepted' ? (b.score.me > b.score.rival ? 'me-c' : b.score.me < b.score.rival ? 'them-c' : '') : '';
        return '<div class="ladder"><div class="row between"><span><b>' + h(b.label) + '</b></span><span class="badge" style="background:#FFD23F">' + b.stake + ' PTS</span></div>' +
          '<span class="small ' + leading + '">' + status + '</span>' + (b.mine_to_answer ? betAnswerButtons(b) : '') + '</div>';
      }).join('') + '</section>';
  }

  function logEntry(a) {
    var counts = a.reactions.map(function (r) { return '<span class="chip">' + h(r.emoji) + ' ' + r.count + '</span>'; }).join('');
    var buttons = a.me ? '' : '<div class="react-row">' + state.reactions.map(function (e) {
      return '<button class="react' + (a.my_reaction === e ? ' on' : '') + '" data-act="react" data-id="' + a.id + '" data-emoji="' + h(e) + '" aria-label="React ' + h(e) + '">' + h(e) + '</button>';
    }).join('') + '</div>';
    return '<div class="log' + (a.me ? ' mine' : '') + '"><div><b>' + h(a.me ? 'You' : a.who) + '</b> ' + h(a.text) +
      ' <span class="small muted">· ' + h(ago(a.at)) + '</span></div>' + (counts ? '<div class="react-row">' + counts + '</div>' : '') + buttons + '</div>';
  }

  // ---------------------------------------------------------------- chat

  var STICKER_MOODS = { flex: 'cheer', fire: 'determined', sleepy: 'sleepy', smirk: 'smirk', cry: 'sulk', rage: 'determined' };

  function chatScreen() {
    var rival = others()[0];
    if (!rival) {
      return '<header class="display h1">Crew chat</header><section class="card dashed small muted">Invite your brother first: in the bot, open <b>/crew</b> and tap <b>Invite</b>.</section>';
    }
    var here = pulseData && pulseData.crew.filter(function (c) { return c.id === rival.id && c.in_app; })[0];
    var parts = ['<header class="row between"><div class="display h1">Crew chat</div><span class="small muted">' +
      (here ? '<span class="live-dot" style="background:var(--me)"></span> ' + h(rival.name) + ' is here' : h(rival.name) + ' gets a ping in Telegram') + '</span></header>'];

    var items = state.chat.map(function (m) { return { at: m.at, msg: m }; });
    state.activity.forEach(function (a) {
      if (a.kind !== 'progress' && a.kind !== 'proof') items.push({ at: a.at, sys: a });
    });
    items.sort(function (x, y) { return new Date(x.at) - new Date(y.at); });
    items = items.slice(-80);

    var list = '', lastSeen = chatNewFrom;
    items.forEach(function (x, idx) {
      if (x.sys) {
        list += '<div class="sys">' + h(x.sys.me ? 'You' : x.sys.who) + ' ' + h(x.sys.text) + ' · ' + h(ago(x.sys.at)) + '</div>';
        return;
      }
      var m = x.msg, side = m.me ? 'mine' : 'theirs', fresh = m.id > lastSeen && !m.me ? ' new' : '';
      if (m.kind === 'sticker') {
        var gear = (member(m.user_id) || meCrew()).gear;
        list += '<div class="msg sticker ' + side + fresh + '">' + mascot(gear, STICKER_MOODS[m.text] || 'determined', 78, 'Sticker') +
          '<span class="emoji">' + h(state.stickers[m.text] || '') + '</span></div>';
      } else if (m.kind === 'proof') {
        list += '<div class="msg proof-msg ' + side + fresh + '">' + proofBlock(m.proof, m.me) + '</div>';
      } else {
        list += '<div class="msg ' + side + fresh + '">' + h(m.text) + '</div>';
      }
      var nextItem = items[idx + 1];
      if (!nextItem || nextItem.sys || nextItem.msg.user_id !== m.user_id) {
        list += '<div class="msg-meta ' + side + '">' + h(m.me ? 'You' : m.who) + ' · ' + h(ago(m.at)) + '</div>';
      }
    });
    parts.push('<div class="chat-list">' + (list || '<div class="sys">Say something. Or send a sticker. Or a proof clip of your last rep 🎥</div>') + '</div>');

    parts.push('<div class="composer"><div class="composer-inner">' +
      (showStickers ? '<div class="stickers">' + Object.keys(state.stickers).map(function (k) {
        return '<button data-act="chat-sticker" data-sticker="' + h(k) + '" aria-label="Sticker ' + h(k) + '">' + mascot(meCrew().gear, STICKER_MOODS[k], 40, 'Sticker') +
          '<span class="emoji">' + h(state.stickers[k]) + '</span></button>';
      }).join('') + '</div>' : '') +
      '<div class="quick no-swipe">' + state.quick_replies.map(function (q) {
        return '<button class="chip-btn" data-act="chat-quick" data-text="' + h(q) + '">' + h(q) + '</button>';
      }).join('') + '</div>' +
      '<form data-form="chat"><label class="round-btn" aria-label="Send a proof clip">🎥<input type="file" accept="video/*,image/*" data-input="proof" hidden></label>' +
      '<button type="button" class="round-btn' + (showStickers ? ' send' : '') + '" data-act="toggle-stickers" aria-label="Stickers">😀</button>' +
      '<input class="input grow" name="text" maxlength="300" autocomplete="off" placeholder="Message ' + h(rival.name) + '…" value="' + h(chatDraft) + '" aria-label="Message">' +
      '<button class="round-btn send" type="submit" aria-label="Send">➤</button></form></div></div>');
    return parts.join('');
  }

  function proofBlock(p, mine) {
    if (!p) return '<span class="small muted">Proof removed.</span>';
    var media;
    var url = proofUrls[p.id] && proofUrls[p.id] !== 'pending' ? proofUrls[p.id] : null;
    if (!p.available) media = '<div class="small muted">Clip expired (kept for 30 days).</div>';
    else if (DEMO) media = '<div class="proof-img" style="display:flex;align-items:center;justify-content:center;font-size:40px">🎥</div>';
    else if (p.media === 'video') media = '<video data-proof="' + p.id + '" playsinline muted loop controls preload="metadata"' + (url ? ' src="' + h(url) + '"' : '') + '></video>';
    else media = '<div class="proof-img" data-proof-img="' + p.id + '"' + (url ? ' style="background-image:url(' + h(url) + ')"' : '') + '></div>';
    var verdict = p.verdict ? '<span class="verdict ' + p.verdict + '">' + (p.verdict === 'legit' ? 'LEGIT 🔥' : 'CAP 🧢') + '</span>' : '';
    var votes = mine
      ? '<span class="small muted">' + (p.verdict ? '' : 'Waiting for the verdict… ') + '🔥 ' + p.counts.legit + ' · 🧢 ' + p.counts.cap + '</span>'
      : '<div class="grid2"><button class="btn' + (p.mine === 'legit' ? ' done' : '') + '" style="height:36px" data-act="proof-vote" data-id="' + p.id + '" data-vote="legit">Legit 🔥 ' + p.counts.legit + '</button>' +
        '<button class="btn' + (p.mine === 'cap' ? ' done' : '') + '" style="height:36px" data-act="proof-vote" data-id="' + p.id + '" data-vote="cap">Cap 🧢 ' + p.counts.cap + '</button></div>';
    return '<div class="proof">' + media + verdict + votes + '</div>';
  }

  function loadProofMedia() {
    if (DEMO) return;
    app.querySelectorAll('[data-proof], [data-proof-img]').forEach(function (el) {
      var id = el.getAttribute('data-proof') || el.getAttribute('data-proof-img');
      if (proofUrls[id]) return;
      proofUrls[id] = 'pending';
      fetch('/api/proof/' + id, { headers: authHeaders() }).then(function (r) {
        if (!r.ok) throw new Error('gone');
        return r.blob();
      }).then(function (blob) {
        proofUrls[id] = URL.createObjectURL(blob);
        app.querySelectorAll('[data-proof="' + id + '"]').forEach(function (v) { v.src = proofUrls[id]; });
        app.querySelectorAll('[data-proof-img="' + id + '"]').forEach(function (d) { d.style.backgroundImage = 'url(' + proofUrls[id] + ')'; });
      }).catch(function () { proofUrls[id] = null; });
    });
  }

  function markChatSeen() {
    if (!state.chat.length || DEMO) { state.unread = 0; return; }
    var last = state.chat[state.chat.length - 1].id;
    if (last > chatSeenSent) {
      chatSeenSent = last;
      api('/api/action', { type: 'chat_seen', id: last }).catch(function () { /* next time */ });
    }
    state.unread = 0;
  }

  // ---------------------------------------------------------------- progress

  function chart(test) {
    var pts = test.points, n = pts.length;
    var values = pts.map(function (p) { return p.value; });
    var change = n > 1 && values[0] ? Math.round((values[n - 1] - values[0]) / values[0] * 100) : null;
    return '<section class="card"><div class="row between"><span class="small muted">' +
      (anime() ? '<span class="jp" style="color:var(--text)">戦闘力</span> · ' : '') + h(test.name) + '</span>' +
      (change !== null ? '<span class="display h2 me-c">' + (change >= 0 ? '+' : '') + change + '%</span>' : '') + '</div>' +
      lineChart(pts.map(function (p) { return { label: monthName(p.month, false), value: p.value }; }), test.name) + '</section>';
  }

  function lineChart(points, label) {
    var n = points.length;
    if (!n) return '';
    var values = points.map(function (p) { return p.value; });
    var lo = Math.min.apply(null, values), hi = Math.max.apply(null, values), span = hi - lo || 1;
    var W = 326, top = 30, bottom = 140;
    var coords = points.map(function (p, i) {
      var x = n === 1 ? W / 2 : 24 + i * ((W - 48) / (n - 1));
      return { x: x, y: n === 1 ? (top + bottom) / 2 : bottom - ((p.value - lo) / span) * (bottom - top), p: p };
    });
    return '<svg width="100%" viewBox="0 0 ' + W + ' 170" role="img" aria-label="' + h(label) + ': ' + h(values.join(', ')) + '">' +
      '<line x1="10" y1="140" x2="316" y2="140" stroke="#2A2E37"/>' +
      '<polyline points="' + coords.map(function (c) { return c.x.toFixed(0) + ',' + c.y.toFixed(0); }).join(' ') + '" fill="none" stroke="var(--me)" stroke-width="3" stroke-linejoin="round" stroke-linecap="round"/>' +
      coords.map(function (c, i) {
        var last = i === n - 1;
        return '<circle cx="' + c.x.toFixed(0) + '" cy="' + c.y.toFixed(0) + '" r="' + (last ? 7 : 5) + '" fill="' + (last ? 'var(--me)' : '#0F1115') + '" stroke="var(--me)" stroke-width="3"/>' +
          '<text x="' + c.x.toFixed(0) + '" y="' + (c.y - 12).toFixed(0) + '" fill="' + (last ? '#F2F2EE' : '#A3A8B3') + '" font-size="12" text-anchor="middle">' + h(c.p.value) + '</text>' +
          (n <= 8 || i === 0 || last ? '<text x="' + c.x.toFixed(0) + '" y="162" fill="#A3A8B3" font-size="11" text-anchor="middle">' + h(c.p.label) + '</text>' : '');
      }).join('') + '</svg>';
  }

  function progressScreen() {
    var r = me().rank;
    var parts = ['<header class="display h1">Progress</header>'];

    var span = r.next_at ? (r.xp - r.floor) / (r.next_at - r.floor) : 1;
    parts.push('<button class="card tap row" style="gap:14px" data-act="open-screen" data-screen="gear">' +
      mascot(me().gear, 'determined', 64, 'Your fighter', { mine: true }) + '<div class="grow" style="display:flex;flex-direction:column;gap:6px">' +
      '<div class="row between"><span class="display h2">Rank ' + h(r.letter) + '</span><span class="small muted">' + count(r.xp, ' XP') + '</span></div>' +
      bar(span, null, 'xp') + '<span class="small muted">' + (r.next_at ? (r.next_at - r.xp) + ' XP to rank ' + h(r.next_letter) : 'Maximum rank. Legend.') + ' · Gear ›</span></div></button>');

    var rep = state.report.last.planned ? state.report.last : state.report.current;
    parts.push('<button class="card tap row" data-act="open-screen" data-screen="report"><span style="font-size:30px">📊</span><div class="grow">' +
      '<b>' + h(monthName(rep.month)) + ' report card</b><div class="small muted">' + rep.workouts + ' workouts · ' + rep.reps + ' reps · ' + rep.points + ' pts</div></div><span class="muted">›</span></button>');

    var ft = state.fitness_test;
    if (state.fitness.length) state.fitness.forEach(function (t) { parts.push(chart(t)); });
    parts.push('<button class="btn ' + (ft.complete ? 'outline' : 'primary') + '" data-act="open-screen" data-screen="test">' +
      (ft.complete ? 'Fitness test done this month ✓' : 'Take this month\'s fitness test') + '</button>');

    if (state.ladders.length) {
      parts.push('<section class="card"><b>Skill ladders</b><span class="small muted">Harder versions instead of endless reps.</span>' +
        state.ladders.map(function (l) {
          var ready = l.can_level_up;
          return '<div class="ladder"><div class="row between"><span><b>' + h(l.name) + '</b> <span class="small muted">level ' + l.level + '/' + l.levels + '</span></span>' +
            (ready ? '<button class="btn done" style="height:36px" data-act="level-up" data-id="' + l.exercise_id + '">Level up</button>' : '') + '</div>' +
            (l.next ? '<div class="small muted">Next: <b style="color:var(--text)">' + h(l.next) + '</b> · target ' + l.unlock_target + ' ' + h(l.unit) +
              ', hit it ' + Math.min(l.solid_days, l.days_needed) + '/' + l.days_needed + ' days this week</div>' : '<div class="small muted">Top of the ladder 🏆</div>') + '</div>';
        }).join('') + '</section>');
    }

    var b = state.body;
    parts.push('<button class="card tap" data-act="open-screen" data-screen="body"><div class="row between"><b>Body</b><span class="small muted">private · ›</span></div>' +
      '<div class="row between"><span>' + (b.latest_weight ? '<span class="display big">' + b.latest_weight + ' kg</span>' : '<span class="muted">Log your weight</span>') + '</span>' +
      '<span class="small muted">' + (b.weight_change !== null ? (b.weight_change > 0 ? '+' : '') + b.weight_change + ' kg since start' : '') + '</span></div>' +
      (b.photos.length ? '<span class="small muted">' + b.photos.length + ' progress photo' + (b.photos.length > 1 ? 's' : '') + '</span>' : '') + '</button>');

    var done = state.last4weeks.filter(function (d) { return d.code === 'done'; }).length;
    var planned = state.last4weeks.filter(function (d) { return ['done', 'partial', 'missed'].indexOf(d.code) >= 0; }).length;
    parts.push('<section class="card"><div class="row between small muted"><span>Last 4 weeks · tap a day</span><span>' + done + ' of ' + planned + ' days</span></div>' +
      dayGrid(state.last4weeks) +
      '<div class="row small muted" style="flex-wrap:wrap;gap:12px"><span>Bright: done</span><span>Dim: partial</span><span>Blue: paused</span><span>Outline: missed</span></div></section>');

    if (state.records.length) {
      parts.push('<div class="grid2">' + state.records.map(function (rec) {
        return '<div class="card"><span class="small muted">Best day · ' + h(rec.name) + '</span><span class="display big">' + count(rec.best, ' ' + rec.unit) + '</span></div>';
      }).join('') + '</div>');
    }

    var s = state.seasons;
    if (s.standings.length > 1) {
      parts.push('<section class="card"><div class="row between"><b>Season ' + h(monthName(s.month)) + '</b>' +
        '<span class="small muted">ends on the 1st</span></div>' +
        s.standings.map(function (row, i) {
          return '<div class="row between"><span>' + (['👑', '🥈', '🥉'][i] || '▫️') + ' ' + h(row.name) + '</span><b style="color:' + h(row.color) + '">' + row.points + '</b></div>';
        }).join('') +
        (s.champions.length ? '<div class="small muted">Hall of fame: ' + s.champions.map(function (c) { return h(c.name) + ' (' + h(c.title) + ')'; }).join(', ') + '</div>' : '') +
        '</section>');
    }

    var labels = { workout: 'Workouts', full: 'Full targets', wake: 'On-time wake-ups', challenge: 'Challenges', test_pr: 'Test records',
      forfeit: 'Forfeits done', proof: 'Proof clips', bets: 'Bets' };
    var mp = state.my_points || {};
    var rows = Object.keys(labels).filter(function (k) { return mp[k]; }).map(function (k) {
      return '<div class="row between"><span>' + labels[k] + '</span><b>' + (mp[k] > 0 ? '+' : '') + mp[k] + '</b></div>';
    }).join('');
    parts.push('<section class="card"><div class="row between"><span class="small muted">Your points this week</span><span class="display h2 me-c">' + (mp.total || 0) + '</span></div>' +
      (rows || '<span class="small muted">Nothing yet this week.</span>') + '</section>');
    return parts.join('');
  }

  // ---------------------------------------------------------------- full screens

  function settingsScreen() {
    var s = state.settings;
    var levels = [['savage', 'Savage'], ['friendly', 'Friendly'], ['off', 'Off']];
    var parts = [backHeader('Settings')];
    parts.push('<section class="card"><b>Workout reminder</b>' +
      '<form class="row" data-form="settings-time" data-key="workout_time"><input class="input grow" type="time" name="value" value="' + h(s.workout_time) + '" aria-label="Reminder time">' +
      '<button class="btn done" type="submit">Save</button></form>' +
      '<div class="row between"><span>Reminders on</span>' + toggle(s.notifications, 'toggle-setting', 'Reminders', ' data-key="notifications"') + '</div></section>');
    parts.push('<section class="card"><b>Wake-up challenge</b>' +
      '<div class="row between"><span>On</span>' + toggle(s.wake_enabled, 'toggle-setting', 'Wake-up challenge', ' data-key="wake_enabled"') + '</div>' +
      '<form class="row" data-form="settings-time" data-key="wake_time"><input class="input grow" type="time" name="value" value="' + h(s.wake_time) + '" aria-label="Wake-up time">' +
      '<button class="btn done" type="submit">Save</button></form></section>');
    parts.push('<section class="card"><b>Crew</b><span class="small muted">Roasts you receive</span><div class="grid3">' +
      levels.map(function (l) {
        return '<button class="btn' + (s.roast_level === l[0] ? ' done' : '') + '" data-act="roast-level" data-level="' + l[0] + '">' + l[1] + '</button>';
      }).join('') + '</div>' +
      '<div class="row between"><span>Telegram messages for crew news and chat</span>' + toggle(s.feed, 'toggle-setting', 'Crew messages', ' data-key="feed"') + '</div></section>');
    parts.push('<section class="card"><b>Vacation / sick pause</b>' +
      (s.paused_until ? '<span>Paused until <b>' + h(niceDate(s.paused_until)) + '</b>. Your streak is safe.</span><button class="btn done" data-act="resume">Resume now</button>'
        : '<span class="small muted">No reminders, streak frozen.</span><div class="grid3">' +
          [[3, '3 days'], [7, '1 week'], [14, '2 weeks']].map(function (p) {
            return '<button class="btn" data-act="pause" data-days="' + p[0] + '">' + p[1] + '</button>';
          }).join('') + '</div>') + '</section>');
    parts.push('<section class="card"><div class="row between"><span>Anime style</span>' + toggle(anime(), 'toggle-anime', 'Anime style') + '</div>' +
      '<div class="row between"><span>Sounds</span>' + toggle(FX.soundOn(), 'toggle-sound', 'Sounds') + '</div>' +
      '<button class="btn outline" data-act="open-screen" data-screen="exercises">Edit exercises</button>' +
      '<button class="btn outline" data-act="open-screen" data-screen="gear">Customize your fighter</button></section>');
    parts.push('<p class="small muted" style="text-align:center">Timezone: ' + h(s.timezone) + ' (change it in the bot\'s /settings)</p>');
    return parts.join('');
  }

  var DAY_LETTERS = ['M', 'T', 'W', 'T', 'F', 'S', 'S'];
  function exercisesScreen() {
    var parts = [backHeader('Exercises'), '<p class="small muted">Changes apply from your next workout day.</p>'];
    state.exercises.forEach(function (e) {
      var days = e.days.split(',');
      parts.push('<section class="card' + (e.active ? '' : ' skipped') + '"><div class="row between"><b>' + h(e.name) + '</b>' +
        toggle(e.active, 'ex-toggle', 'Active: ' + e.name, ' data-id="' + e.id + '"') + '</div>' +
        '<div class="row between"><span class="small muted">Target</span><div class="row">' +
        '<button class="btn" data-act="ex-target" data-id="' + e.id + '" data-target="' + Math.max(1, e.target - 5) + '" aria-label="Lower target">−5</button>' +
        '<span class="display big" style="min-width:70px;text-align:center">' + e.target + ' <small class="muted" style="font-size:15px">' + h(e.unit) + '</small></span>' +
        '<button class="btn" data-act="ex-target" data-id="' + e.id + '" data-target="' + (e.target + 5) + '" aria-label="Raise target">+5</button></div></div>' +
        '<div class="days">' + DAY_LETTERS.map(function (d, i) {
          var on = days.indexOf(String(i)) >= 0;
          return '<button class="day-chip' + (on ? ' on' : '') + '" data-act="ex-day" data-id="' + e.id + '" data-day="' + i + '" aria-pressed="' + on + '">' + d + '</button>';
        }).join('') + '</div>' +
        '<button class="btn outline" data-act="ex-delete" data-id="' + e.id + '">Remove</button></section>');
    });
    parts.push('<form class="card" data-form="ex-add"><b>Add an exercise</b>' +
      '<input class="input" name="name" maxlength="40" placeholder="Name, e.g. Burpees" aria-label="Exercise name" required>' +
      '<div class="grid2"><input class="input" name="target" type="number" min="1" max="5000" placeholder="Target" aria-label="Target" required>' +
      '<select class="input" name="unit" aria-label="Unit"><option value="reps">reps</option><option value="sec">seconds</option></select></div>' +
      '<button class="btn done" type="submit">Add</button></form>');
    return parts.join('');
  }

  function testScreen() {
    var ft = state.fitness_test;
    var parts = [backHeader('Fitness test'),
      '<p class="small muted">Once a month, max effort. Warm up first. Results show on your Progress charts.</p>'];
    ft.tests.forEach(function (t) {
      var timer = t.timer ? (testTimer.key === t.key && testTimer.left > 0
        ? '<div class="display" style="font-size:44px;text-align:center">' + Math.floor(testTimer.left / 60) + ':' + ('0' + testTimer.left % 60).slice(-2) + '</div>'
        : '<button class="btn outline" data-act="test-timer" data-key="' + h(t.key) + '" data-secs="' + t.timer + '">Start ' + t.timer / 60 + '-min timer</button>') : '';
      parts.push('<section class="card"><div class="row between"><b>' + h(t.name) + '</b>' +
        (t.done ? '<span class="me-c">' + (t.result === null ? 'skipped' : h(t.result) + ' ' + h(t.unit)) + ' ✓</span>' : '') + '</div>' +
        '<div class="row" style="gap:12px"><div class="demo-box" style="flex-shrink:0">' + FX.demo(t.name, 110, me().gear.hair) + '</div>' +
        '<span class="small muted">' + h(t.how) + '</span></div>' + timer +
        '<form class="grid3" data-form="test-save" data-key="' + h(t.key) + '">' +
        '<input class="input" type="number" inputmode="numeric" min="0" max="3600" name="value" placeholder="' + h(t.unit) + '" aria-label="' + h(t.name) + ' result" required>' +
        '<button class="btn done" type="submit">Save</button><button class="btn" type="button" data-act="test-skip" data-key="' + h(t.key) + '">Skip</button></form></section>');
    });
    parts.push('<section class="card row between"><span>Pull-up test (needs a bar)</span>' + toggle(ft.pullups, 'test-pullups', 'Pull-up test') + '</section>');
    return parts.join('');
  }

  function gearScreen() {
    var cat = state.gear_catalog, ids = me().gear.ids || {};
    var parts = [backHeader('Your fighter'),
      '<div style="display:flex;justify-content:center">' + mascot(me().gear, 'determined', 150, 'Your fighter', { mine: true }) + '</div>',
      '<p class="small muted" style="text-align:center">Rank ' + h(me().rank.letter) + ' · unlock gear with streaks, records and wins.</p>'];
    [['hair', 'Hair'], ['band', 'Headband'], ['aura', 'Aura']].forEach(function (slot) {
      parts.push('<section class="card"><b>' + slot[1] + '</b><div class="gear-grid">' + cat[slot[0]].map(function (g) {
        var on = ids[slot[0]] === g.id;
        return '<button class="gear' + (on ? ' on' : '') + (g.unlocked ? '' : ' locked') + '" data-act="equip" data-slot="' + slot[0] + '" data-id="' + h(g.id) + '"' +
          (g.unlocked ? '' : ' aria-disabled="true" data-how="' + h(g.how) + '"') + ' aria-label="' + h(g.label + (g.unlocked ? '' : ' (locked: ' + g.how + ')')) + '">' +
          '<span class="swatch" style="background:' + (g.color ? h(g.color) : 'transparent') + '">' + (g.unlocked ? '' : icon('lock', 16, '#0F1115')) + '</span>' +
          '<span class="small">' + h(g.label) + '</span>' + (g.unlocked ? '' : '<span class="tiny muted">' + h(g.how) + '</span>') + '</button>';
      }).join('') + '</div></section>');
    });
    return parts.join('');
  }

  function bodyScreen() {
    var b = state.body;
    var parts = [backHeader('Body'), '<p class="small muted">Private: only you can see this, never your crew.</p>'];
    parts.push('<form class="card" data-form="body-log"><b>Today' + (b.logged_today ? ' ✓' : '') + '</b><div class="grid2">' +
      '<input class="input" name="weight" type="number" step="0.1" min="25" max="350" placeholder="Weight (kg)" aria-label="Weight in kg">' +
      '<input class="input" name="waist" type="number" step="0.5" min="40" max="250" placeholder="Waist (cm)" aria-label="Waist in cm"></div>' +
      '<button class="btn done" type="submit">Save</button><span class="tiny muted">Weigh yourself in the morning. The chart uses weekly averages, so daily swings don\'t matter.</span></form>');
    if (b.weekly.length) {
      parts.push('<section class="card"><div class="row between"><b>Weight · weekly average</b><span class="small muted">' +
        (b.weight_change !== null ? (b.weight_change > 0 ? '+' : '') + b.weight_change + ' kg' : '') + '</span></div>' +
        lineChart(b.weekly.map(function (w) { return { label: new Date(w.week + 'T12:00:00').toLocaleDateString(undefined, { day: 'numeric', month: 'short' }), value: w.avg }; }), 'Weekly average weight') + '</section>');
    }
    if (b.waist.length) {
      parts.push('<section class="card"><div class="row between"><b>Waist</b><span class="small muted">' +
        (b.waist_change !== null ? (b.waist_change > 0 ? '+' : '') + b.waist_change + ' cm' : '') + '</span></div>' +
        b.waist.slice().reverse().map(function (w) { return '<div class="row between small"><span>' + h(niceDate(w.date)) + '</span><b>' + w.value + ' cm</b></div>'; }).join('') + '</section>');
    }
    var photos = b.photos;
    parts.push('<section class="card"><div class="row between"><b>Progress photos</b><label class="btn done" style="display:inline-flex;align-items:center">Add photo' +
      '<input type="file" accept="image/*" data-input="photo" hidden></label></div>' +
      (photos.length > 1 ? '<span class="small muted">Then and now</span><div class="grid2">' + photoTile(photos[0]) + photoTile(photos[photos.length - 1]) + '</div>' : '') +
      (photos.length ? '<div class="photo-grid">' + photos.slice().reverse().map(photoTile).join('') + '</div>'
        : '<span class="small muted">Same spot, same light, once a month. In 3 months you\'ll be glad you did.</span>') + '</section>');
    return parts.join('');
  }

  function photoTile(p) {
    var url = photoUrls[p.id];
    return '<figure class="photo"><div class="photo-img"' + (url && url !== 'pending' ? ' style="background-image:url(' + h(url) + ')"' : '') + ' role="img" aria-label="Progress photo ' + h(p.date) + '"></div>' +
      '<figcaption class="row between tiny"><span>' + h(niceDate(p.date)) + '</span><button class="link" data-act="photo-delete" data-id="' + p.id + '">Delete</button></figcaption></figure>';
  }

  // Train live: both of you, side by side, updating every 2 seconds.
  function liveScreen() {
    var mine = meCrew(), crew = [mine].concat(others());
    var parts = [backHeader('<span class="live-dot"></span> Train live')];
    parts.push('<div class="live-grid">' + crew.slice(0, 2).map(function (m) {
      var p = livePulse(m.id);
      var status = p.rest_left > 0 ? 'Resting ' + p.rest_left + 's' : m.me ? 'You' : p.live ? 'Training now' : p.in_app ? 'In the app' : 'Not here yet';
      var exs = (p.items || []).map(function (it) {
        return '<div><div class="row between"><span>' + h(it.name) + '</span><span>' + it.done + '/' + it.target + '</span></div>' +
          bar(it.target ? it.done / it.target : 1, m.gear.hair || m.color, 'lx' + m.id + it.name) + '</div>';
      }).join('');
      return '<section class="card live-card" data-fighter="' + m.id + '">' +
        mascot(m.gear, m.me ? myMood(p.progress, p.status) : 'smirk', 56, m.name, { mine: m.me, sweat: p.progress >= 0.7 && p.status !== 'completed' }) +
        '<b>' + h(m.me ? 'You' : m.name) + (p.live && !m.me ? ' <span class="live-dot"></span>' : '') + '</b>' +
        ring(92, 9, p.progress, m.gear.hair || m.color, '<span class="display" style="font-size:24px">' + (p.reps || 0) + '</span><span class="tiny muted">reps</span>', 'live' + m.id) +
        '<span class="status">' + h(status) + '</span>' + (m.me ? '' : '<div class="mini-ex">' + exs + '</div>') + '</section>';
    }).join('') + '</div>');
    if (!others().length) parts.push('<section class="card dashed small muted">Invite your brother to race live.</section>');
    state.today.items.forEach(function (it) { parts.push(exerciseCard(it)); });
    parts.push('<div class="grid2"><button class="btn outline" data-act="live-rest" data-secs="60">' + (restLeft ? 'Rest ' + restLeft + 's' : 'Rest 60s') + '</button>' +
      '<button class="btn outline" data-act="live-rest" data-secs="90">Rest 90s</button></div>');
    parts.push('<button class="btn outline" data-act="close-screen">End session</button>');
    return parts.join('');
  }

  // Guided workout: one exercise at a time, demo, big tap counter, rest timer.
  function playerScreen() {
    var p = player;
    var steps = '<div class="steps">' + p.order.map(function (id, i) {
      var it = findItem(id);
      return '<i class="' + (i < p.i || (it && it.status === 'completed') ? 'done' : i === p.i ? 'now' : '') + '"></i>';
    }).join('') + '</div>';
    var head = backHeader(p.phase === 'done' ? 'Done!' : 'Exercise ' + Math.min(p.i + 1, p.order.length) + ' of ' + p.order.length);
    if (p.phase === 'done') {
      return head + '<section class="card player" style="align-items:center;text-align:center">' +
        mascot(me().gear, 'cheer', 140, 'Your mascot celebrating', { mine: true }) +
        '<div class="' + (anime() ? 'jp' : 'display') + '" style="font-size:52px">' + (anime() ? '完了!' : 'Workout complete') + '</div>' +
        '<div class="muted">' + state.today.items.reduce(function (s, i) { return s + (i.unit === 'reps' ? i.done : 0); }, 0) + ' reps today · ' + state.streak + '-day streak</div>' +
        '<button class="btn primary" style="width:100%" data-act="close-screen">Finish</button></section>';
    }
    var it = findItem(p.order[p.i]);
    if (!it) { p.phase = 'done'; return playerScreen(); }
    if (p.phase === 'rest') {
      return head + steps + '<section class="card player" style="align-items:center;text-align:center">' +
        '<span class="small muted">REST</span>' +
        ring(200, 14, p.restTotal ? p.left / p.restTotal : 0, 'var(--them)', '<span class="display" style="font-size:64px">' + p.left + '</span><span class="small muted">seconds</span>') +
        '<div class="small muted">Next up: <b style="color:var(--text)">' + h(it.name) + '</b> · ' + it.target + ' ' + h(it.unit) + '</div>' +
        '<div class="demo-box" style="width:100%">' + FX.demo(it.name, 150, me().gear.hair) + '</div>' +
        '<div class="grid2" style="width:100%"><button class="btn" data-act="p-rest-add">+15s</button><button class="btn done" data-act="p-skip-rest">Skip rest</button></div></section>';
    }
    var done = it.done + p.pending + p.inflight;
    var timed = it.unit !== 'reps';
    var body;
    if (timed) {
      var left = p.timing ? p.left : Math.max(0, it.target - done);
      body = '<div class="big-count">' + left + '<small>s</small></div>' +
        '<button class="tap-big" data-act="' + (p.timing ? 'p-timer-stop' : 'p-timer') + '">' + (p.timing ? 'Stop' : 'Go') + '</button>' +
        '<div class="small muted" style="text-align:center">' + (p.timing ? 'Hold it! Beeps in the last 3 seconds.' : 'Tap Go and hold for ' + Math.max(0, it.target - done) + ' seconds.') + '</div>';
    } else {
      body = '<div class="big-count"><span class="p-count">' + done + '</span><small> / ' + it.target + '</small></div>' +
        bar(it.target ? done / it.target : 1, null, 'p' + it.id) +
        '<button class="tap-big" data-act="p-add" data-n="1" aria-label="Add one rep">+1</button>' +
        '<div class="grid3"><button class="btn" data-act="p-add" data-n="5">+5</button><button class="btn" data-act="p-add" data-n="10">+10</button>' +
        '<button class="btn done" data-act="p-finish">Done ✓</button></div>';
    }
    var tip = !timed && it.target > 30 ? 'Tip: split it, e.g. ' + Math.ceil(it.target / 2) + ' + ' + Math.floor(it.target / 2) + ' with a short rest.' : '';
    return head + steps + '<section class="card player">' +
      '<div class="row between"><div class="display h1">' + h(it.name) + '</div><button class="link" data-act="p-next">Skip ›</button></div>' +
      '<div class="demo-box">' + FX.demo(it.name, 220, me().gear.hair) + '</div>' + body +
      (tip ? '<div class="tiny muted" style="text-align:center">' + h(tip) + '</div>' : '') + '</section>';
  }

  function reportScreen() {
    var r = state.report[reportWhich];
    var parts = [backHeader('Report card')];
    parts.push('<div class="seg"><button class="' + (reportWhich === 'last' ? 'on' : '') + '" data-act="report-which" data-which="last">' + h(monthName(state.report.last.month)) + '</button>' +
      '<button class="' + (reportWhich === 'current' ? 'on' : '') + '" data-act="report-which" data-which="current">' + h(monthName(state.report.current.month)) + ' so far</button></div>');
    if (!r.planned) {
      parts.push('<section class="card dashed small muted">No workouts in ' + h(monthName(r.month)) + '.</section>');
      return parts.join('');
    }
    var stats = [[r.workouts + '/' + r.planned, 'workouts'], [r.reps, 'reps'], [r.best_streak + ' days', 'best streak'], [r.points, 'points'],
      [r.weight_change === null ? '–' : (r.weight_change > 0 ? '+' : '') + r.weight_change + ' kg', 'weight'], [r.best_day ? r.best_day.reps : '–', 'best day reps']];
    parts.push('<section class="card report">' + mascot(r.gear, r.is_champion ? 'cheer' : 'determined', 110, 'Your fighter', { mine: true }) +
      '<div class="small me-c" style="letter-spacing:2px">' + h(monthName(r.month).toUpperCase()) + ' REPORT CARD</div>' +
      '<div class="display" style="font-size:40px">' + h(r.name) + '</div>' +
      '<div class="row" style="gap:8px"><span class="badge" style="background:var(--me)">RANK ' + h(r.rank) + '</span>' +
      (r.is_champion ? '<span class="badge" style="background:#FFD23F">👑 CHAMPION</span>' : '') + '</div>' +
      '<div class="stats">' + stats.map(function (s, i) {
        return '<div class="stat"><b' + (i === 0 ? ' class="me-c"' : '') + '>' + h(s[0]) + '</b><span class="small muted">' + s[1] + '</span></div>';
      }).join('') + '</div>' +
      (r.champion ? '<div class="small muted">👑 ' + h(r.champion) + ' won the month</div>' : '') + '</section>');
    parts.push('<button class="btn primary" data-act="report-image">Save as image</button>');
    return parts.join('');
  }

  // ---------------------------------------------------------------- overlays

  function wakeOverlay() {
    var w = state.wake.today;
    return '<div class="scrim" style="align-items:stretch"><section class="sheet full" aria-label="Wake-up check">' +
      (anime() ? '<span class="wake-kanji" aria-hidden="true">起きろ!</span><div style="position:relative">' + mascot(me().gear, 'sleepy', 120, 'Your mascot, half asleep') + '</div>'
               : '<div class="wake-sun">' + icon('sun', 60, 'var(--me)') + '</div>') +
      '<div style="text-align:center;position:relative"><div class="small muted" style="letter-spacing:1.5px">WAKE-UP CHECK · ' + h(state.wake.time) + '</div>' +
      '<div class="display" style="font-size:52px">Tap the ' + w.challenge + '</div><div class="muted">Prove you\'re actually awake</div></div>' +
      '<div class="grid2" style="width:100%;position:relative">' + w.choices.map(function (n) {
        return '<button class="btn display" style="height:92px;font-size:46px;border-radius:24px;background:var(--bg)" data-act="wake" data-choice="' + n + '">' + n + '</button>';
      }).join('') + '</div><button class="btn outline" style="position:relative" data-act="wake-later">Not now</button></section></div>';
  }

  function welcomeOverlay() {
    var starters = [['lime', '#C8F135'], ['orange', '#FF8A3D'], ['blue', '#7FB2FF'], ['pink', '#F06BC6']];
    return '<div class="scrim" style="align-items:stretch"><section class="sheet full" aria-label="Welcome">' +
      '<div class="display h1" style="text-align:center">Welcome to the crew, ' + h(me().name) + '!</div>' +
      '<p class="muted" style="text-align:center">Pick your fighter. You\'ll unlock more gear as you train.</p>' +
      '<div class="grid2" style="width:100%">' + starters.map(function (s) {
        return '<button class="card tap" style="align-items:center;background:var(--surface-2)" data-act="onboard" data-hair="' + s[0] + '">' +
          mascot({ hair: s[1], band: s[0] === 'orange' ? '#C8F135' : '#FF8A3D' }, 'determined', 96, 'Fighter with ' + s[0] + ' hair') + '</button>';
      }).join('') + '</div>' +
      '<p class="small muted" style="text-align:center">Log workouts, beat your brother on the Duel tab, chat and bet, climb from rank E to S.</p></section></div>';
  }

  function celebrationOverlay() {
    var c = celebration, big, sub;
    if (c.kind === 'rank') { big = anime() ? '昇格!' : 'RANK UP!'; sub = 'RANK ' + c.from + ' → ' + c.to; }
    else if (c.kind === 'record') { big = anime() ? '新記録!' : 'NEW RECORD!'; sub = c.text || 'Personal best'; }
    else { big = anime() ? '完了!' : 'Done!'; sub = 'TRAINING COMPLETE'; }
    var lines = '';
    for (var i = 0; i < 24; i++) lines += '<line x1="80" y1="0" x2="600" y2="0" transform="rotate(' + i * 15 + ')"/>';
    return '<div class="burst" data-act="close-celebration" role="dialog" aria-label="' + h(sub) + '">' +
      '<svg class="burst-lines" viewBox="-300 -300 600 600" aria-hidden="true"><g stroke="rgba(255,255,255,.18)" stroke-width="3">' + lines + '</g></svg>' +
      '<div class="panel">' + (anime() ? mascot(me().gear, 'cheer', 92, 'Your mascot celebrating', { mine: true }) : '') +
      '<div><div class="' + (anime() ? 'kanji' : 'display h1') + '">' + h(big) + '</div>' +
      '<div class="display h2">' + h(sub) + '</div><div class="small">' +
      (c.kind === 'rank' ? 'Your crew has been told.' : c.kind === 'record' ? 'Logged on your Progress chart.' : (others().length ? 'Your crew has been told.' : 'Streak: ' + state.streak + ' days.')) +
      '</div></div></div></div>';
  }

  function challengeSheet() {
    var targets = others();
    var target = sheet.target || (targets.length === 1 ? targets[0].id : null);
    var body;
    if (!target) {
      body = '<b>Who do you challenge?</b>' + targets.map(function (o) {
        return '<button class="btn" data-act="challenge-target" data-target="' + o.id + '">' + h(o.name) + '</button>';
      }).join('');
    } else {
      body = '<b>Pick today\'s challenge</b><span class="small muted">They do it: +15 for them. They don\'t (or chicken out): the points are yours.</span>' +
        state.challenge_kinds.map(function (k) {
          return '<button class="btn" style="text-align:left" data-act="challenge" data-target="' + target + '" data-kind="' + h(k.key) + '">' + h(k.label) + '</button>';
        }).join('');
    }
    return '<div class="scrim" data-act="close-sheet"><section class="sheet" data-stop="1">' + body +
      '<button class="btn outline" data-act="close-sheet">Cancel</button></section></div>';
  }

  function betSheet() {
    var rival = member(sheet.target) || others()[0];
    return '<div class="scrim" data-act="close-sheet"><section class="sheet" data-stop="1">' +
      '<b>🎲 Bet against ' + h(rival.name) + '</b><span class="small muted">The winner takes the stake from the loser\'s weekly points. Settled at 23:00.</span>' +
      state.bet_kinds.map(function (k) {
        return '<button class="btn' + (sheet.betKind === k.key ? ' done' : '') + '" style="text-align:left" data-act="bet-kind" data-kind="' + h(k.key) + '">' + h(k.label) + '</button>';
      }).join('') +
      '<div class="row" style="gap:8px"><span class="small muted grow">Stake</span>' + state.stakes.map(function (s) {
        return '<button class="chip-btn' + (sheet.stake === s ? ' on' : '') + '" data-act="bet-stake" data-stake="' + s + '">' + s + ' pts</button>';
      }).join('') + '</div>' +
      '<button class="btn primary" data-act="bet-send">Send the bet</button><button class="btn outline" data-act="close-sheet">Cancel</button></section></div>';
  }

  function roastSheet() {
    var target = member(sheet.target) || {};
    return '<div class="scrim" data-act="close-sheet"><form class="sheet" data-stop="1" data-form="roast" data-target="' + sheet.target + '">' +
      '<b>Your roast for ' + h(target.name) + '</b>' +
      '<textarea class="input" name="text" maxlength="140" rows="3" placeholder="Make it hurt (lovingly)…" aria-label="Your roast" required></textarea>' +
      '<button class="btn roast" type="submit">Send roast</button><button class="btn outline" type="button" data-act="close-sheet">Cancel</button></form></div>';
  }

  function daySheet() {
    var d = sheet.data;
    var body = !d ? '<span class="muted">Loading…</span>'
      : !d.status ? '<span class="muted">Nothing logged that day.</span>'
      : '<span class="small muted">' + h(d.status) + '</span>' + d.items.map(function (i) {
        return '<div class="row between"><span>' + h(i.name) + '</span><b>' + i.done + ' / ' + i.target + ' ' + h(i.unit) + '</b></div>';
      }).join('');
    return '<div class="scrim" data-act="close-sheet"><section class="sheet" data-stop="1"><b>' + h(niceDate(sheet.date)) + '</b>' + body +
      '<button class="btn outline" data-act="close-sheet">Close</button></section></div>';
  }

  function imageSheet() {
    return '<div class="scrim" data-act="close-sheet"><section class="sheet" data-stop="1"><b>Your report card</b>' +
      '<img class="share-img" src="' + h(sheet.url) + '" alt="Report card image">' +
      '<span class="small muted">On your phone: press and hold the image to save or share it.</span>' +
      '<a class="btn done" style="display:flex;align-items:center;justify-content:center;text-decoration:none" href="' + h(sheet.url) + '" download="report-card.png">Download</a>' +
      '<button class="btn outline" data-act="close-sheet">Close</button></section></div>';
  }

  function nav() {
    var tabs = [['home', 'Home'], ['today', 'Today'], ['duel', 'Duel'], ['chat', 'Chat'], ['progress', 'Progress']];
    return '<nav class="nav" aria-label="Sections">' + tabs.map(function (t) {
      var badge = t[0] === 'chat' && state.unread ? '<span class="nav-badge">' + state.unread + '</span>' : '';
      return '<button data-act="tab" data-tab="' + t[0] + '"' + (tab === t[0] && !screen ? ' aria-current="page"' : '') + '>' + icon(t[0]) + t[1] + badge + '</button>';
    }).join('') + '</nav>';
  }

  function overlayShowing() {
    return !!app.querySelector('.scrim, .burst');
  }

  function render() {
    if (!state) return;
    chatNewFrom = chatSeenSent;
    if (tab === 'chat' && !screen) markChatSeen();
    var page = screen === 'settings' ? settingsScreen() : screen === 'exercises' ? exercisesScreen()
      : screen === 'test' ? testScreen() : screen === 'gear' ? gearScreen() : screen === 'body' ? bodyScreen()
      : screen === 'live' ? liveScreen() : screen === 'player' && player ? playerScreen() : screen === 'report' ? reportScreen()
      : tab === 'today' ? todayScreen() : tab === 'duel' ? duelScreen() : tab === 'chat' ? chatScreen()
      : tab === 'progress' ? progressScreen() : homeScreen();
    var banner = DEMO ? '<div class="demo">Demo with sample data. Open it from the bot in Telegram to see your real crew.</div>'
      : DEV && params.get('banner') !== '0' ? '<div class="demo">Dev mode · you are <b>' + h(me().name) + '</b> · switch to ' + others().map(function (o) {
        return '<a href="?as=' + o.id + '" style="color:var(--me)">' + h(o.name) + '</a>';
      }).join(', ') + ' · bot messages print in the terminal</div>' : '';
    var overlay = '';
    var w = state.wake;
    var rankUp = state.me.onboarded && RANK_LETTERS.indexOf(state.me.rank.letter) > RANK_LETTERS.indexOf(state.me.seen_rank);
    if (!state.me.onboarded) overlay = welcomeOverlay();
    else if (w.enabled && w.today && w.today.status === 'pending' && !wakeDismissed) overlay = wakeOverlay();
    else if (celebration) overlay = celebrationOverlay();
    else if (rankUp) {
      celebration = { kind: 'rank', from: state.me.seen_rank, to: state.me.rank.letter };
      haptic('success');
      FX.sounds.win();
      afterRender.push(function () { FX.confetti(); });
      overlay = celebrationOverlay();
    } else if (sheet) {
      overlay = sheet.kind === 'roast' ? roastSheet() : sheet.kind === 'day' ? daySheet() : sheet.kind === 'bet' ? betSheet()
        : sheet.kind === 'image' ? imageSheet() : challengeSheet();
    }
    document.body.classList.toggle('chat-open', tab === 'chat' && !screen && others().length > 0);
    var focusedChat = document.activeElement && document.activeElement.name === 'text' && document.activeElement.closest('[data-form="chat"]');
    app.innerHTML = banner + page + nav() + overlay;
    if (focusedChat) {
      var input = app.querySelector('[data-form="chat"] input[name="text"]');
      if (input) { input.focus(); input.setSelectionRange(input.value.length, input.value.length); }
    }
    var entering = animateNext;
    if (entering) {
      var i = 0;
      Array.prototype.forEach.call(app.children, function (el) {
        if (el.tagName === 'NAV' || el.classList.contains('scrim') || el.classList.contains('burst') || el.classList.contains('composer')) return;
        el.classList.add('slide-in');
        if (slideDir) el.classList.add(slideDir);
        el.style.animationDelay = Math.min(i++, 8) * 35 + 'ms';
      });
      runCountUps();
      animateNext = false;
      slideDir = '';
    }
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        app.querySelectorAll('circle.fg[data-dash]').forEach(function (c) { c.setAttribute('stroke-dasharray', c.getAttribute('data-dash')); });
        app.querySelectorAll('.bar > i[data-to]').forEach(function (b) { b.style.width = b.getAttribute('data-to') + '%'; });
        var effects = afterRender;
        afterRender = [];
        effects.forEach(function (f) { try { f(); } catch (e) { /* cosmetic */ } });
      });
    });
    if (screen === 'body') loadPhotos();
    if (tab === 'chat' && !screen) {
      loadProofMedia();
      if (entering || nearBottom) window.scrollTo(0, document.body.scrollHeight);
    }
  }

  var nearBottom = true;
  window.addEventListener('scroll', function () {
    nearBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 160;
  }, { passive: true });

  function runCountUps() {
    app.querySelectorAll('[data-count]').forEach(function (el) {
      var target = +el.getAttribute('data-count'), suffix = el.getAttribute('data-suffix') || '', start = performance.now();
      if (FX.reduced) { el.textContent = target + suffix; return; }
      (function step(now) {
        var p = Math.min(1, (now - start) / 650);
        el.textContent = Math.round(target * (1 - Math.pow(1 - p, 3))) + suffix;
        if (p < 1) requestAnimationFrame(step);
      })(start);
    });
  }

  // ---------------------------------------------------------------- data

  function authHeaders(extra) {
    var headers = {};
    if (initData) headers['Authorization'] = 'tma ' + initData;
    else if (devAs) headers['X-Dev-User'] = devAs;
    for (var k in extra || {}) headers[k] = extra[k];
    return headers;
  }

  function api(path, body) {
    if (DEMO) return Promise.resolve(demoApi(path, body));
    return fetch(path, {
      method: body ? 'POST' : 'GET',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: body ? JSON.stringify(body) : undefined
    }).then(function (res) {
      return res.json().then(function (data) {
        if (!res.ok) throw new Error(data.message || 'Request failed');
        return data;
      });
    });
  }

  function byId(list, id) { return (list || []).filter(function (x) { return x.id === id; })[0]; }

  // What changed since the last state: overtakes, hits in the arena, new chat, someone going live.
  function diffEffects(prev, next) {
    var myId = next.me.id, meOld = byId(prev.crew, myId), meNew = byId(next.crew, myId);
    next.crew.forEach(function (o) {
      if (o.me || !meOld || !meNew) return;
      var oOld = byId(prev.crew, o.id);
      if (!oOld) return;
      if (oOld.reps <= meOld.reps && o.reps > meNew.reps && o.reps > 0) {
        toast('⚡ ' + o.name + ' just passed you: ' + o.reps + ' vs ' + meNew.reps + ' reps', { button: { label: 'Revenge', act: 'tab', tab: 'today' } });
        FX.sounds.alert();
        haptic('warning');
      } else if (meOld.reps <= oOld.reps && meNew.reps > o.reps && o.reps > 0) {
        afterRender.push(function () { FX.floatText(null, '⚡ You passed ' + o.name + '!', 'var(--me)', 28); });
      }
      if (tab === 'duel' && !screen) {
        if (o.progress > oOld.progress) {
          afterRender.push(function () {
            var el = app.querySelector('[data-fighter="' + myId + '"]');
            FX.hit(el);
            FX.floatText(el, '-' + Math.round((o.progress - oOld.progress) * 100) + ' HP', '#FF4D5E', 28);
            FX.sounds.hit();
          });
        }
        if (meNew.progress > meOld.progress) {
          afterRender.push(function () {
            var el = app.querySelector('[data-fighter="' + o.id + '"]');
            FX.hit(el);
            FX.floatText(el, '-' + Math.round((meNew.progress - meOld.progress) * 100) + ' HP', '#FF4D5E', 28);
          });
        }
      }
      if (!oOld.live && o.live && screen !== 'live') {
        toast('🔴 ' + o.name + ' started training live', { button: { label: 'Join', act: 'open-screen', screen: 'live' } });
      }
    });
    var lastOld = prev.chat.length ? prev.chat[prev.chat.length - 1].id : 0;
    var fresh = next.chat.filter(function (m) { return m.id > lastOld && !m.me; });
    if (fresh.length) {
      FX.sounds.msg();
      var m = fresh[fresh.length - 1];
      var preview = m.kind === 'sticker' ? (next.stickers[m.text] || 'sticker') : m.kind === 'proof' ? '🎥 proof clip, judge it!' : m.text;
      if (tab !== 'chat' || screen) toast('💬 ' + m.who + ': ' + preview, { button: { label: 'Open', act: 'tab', tab: 'chat' } });
    }
  }

  function setState(next) {
    var prev = state;
    // Taps not yet sent stay visible after a refresh.
    Object.keys(deltaQueue).forEach(function (id) {
      var it = byId(next.today.items, +id);
      if (it) it.done = Math.max(0, it.done + deltaQueue[id]);
    });
    state = next;
    if (prev) {
      diffEffects(prev, next);
      var celebrateHere = (tab === 'today' && !screen) || screen === 'player' || screen === 'live';
      if (prev.today.status !== 'completed' && next.today.status === 'completed' && celebrateHere) {
        celebration = { kind: 'done' };
        haptic('success');
        FX.sounds.win();
        afterRender.push(function () { FX.confetti(); FX.shake(); FX.mascotReact('power'); });
        setTimeout(function () { if (celebration && celebration.kind === 'done') { celebration = null; render(); } }, 3500);
      }
    } else if (startRoute) {
      openRoute(startRoute);
      startRoute = null;
    }
    pulseData = null;   // the full state is fresher
    render();
  }

  function openRoute(route) {
    if (TABS.indexOf(route) >= 0) tab = route;
    else if (route === 'live') openLive();
    else if (route === 'player') setTimeout(startPlayer, 0);
    else if (['report', 'settings', 'test', 'body', 'gear', 'exercises'].indexOf(route) >= 0) screen = route;
  }

  function snapshotValues(ids) {
    var values = {};
    state.today.items.forEach(function (i) { if (!ids || ids.indexOf(i.id) >= 0) values[i.id] = i.done; });
    return values;
  }

  function act(body, opts) {
    if (busy) return Promise.resolve();
    busy = true;
    haptic('light');
    opts = opts || {};
    return api('/api/action', body).then(function (res) {
      if (res.pr) { celebration = { kind: 'record', text: res.message }; haptic('success'); FX.sounds.win(); afterRender.push(function () { FX.confetti(); }); }
      else if (res.message || opts.undo) toast(res.message, opts.undo ? { undo: opts.undo } : null);
      setState(res.state);
      if (opts.after) opts.after(res);
    }).catch(function (e) {
      haptic('error');
      toast(e.message);
    }).then(function () { busy = false; });
  }

  // Fire-and-forget actions whose result doesn't need a re-render.
  function quietAct(body) {
    api('/api/action', body).catch(function () { /* best effort */ });
  }

  function load() {
    return api('/api/state').then(function (s) { pendingReload = false; setState(s); }).catch(function (e) {
      if (!state) app.innerHTML = '<div class="boot">' + h(e.message) + '</div>';
    });
  }

  function typing() {
    var a = document.activeElement;
    return a && /INPUT|TEXTAREA|SELECT/.test(a.tagName) && !(a.name === 'text' && a.closest('[data-form="chat"]') && !a.value);
  }

  // Small snapshot every few seconds; a full reload only when something changed.
  var pulseTimer = null;
  function pulseLoop() {
    clearTimeout(pulseTimer);
    pulseTimer = setTimeout(function () {
      var idle = document.visibilityState === 'visible' && !busy && !flushing && !Object.keys(deltaQueue).length;
      if (!idle || !state) return pulseLoop();
      api('/api/pulse').then(function (p) {
        if (!p || !p.crew) return;
        if (p.sig !== state.sig) {
          if (typing() || sheet) pendingReload = true;
          else return load();
        } else if (screen === 'live' && !typing()) {
          pulseData = p;
          render();
        } else {
          pulseData = p;
        }
      }).catch(function () { /* offline for a moment */ }).then(pulseLoop);
    }, screen === 'live' ? 2000 : 4000);
  }

  function loadPhotos() {
    if (DEMO) return;
    state.body.photos.forEach(function (p) {
      if (photoUrls[p.id]) return;
      photoUrls[p.id] = 'pending';
      fetch('/api/photo/' + p.id, { headers: authHeaders() }).then(function (r) { return r.blob(); }).then(function (blob) {
        photoUrls[p.id] = URL.createObjectURL(blob);
        if (screen === 'body') render();
      });
    });
  }

  // Shrinks a photo on the phone before upload (max 1280 px, JPEG): faster and smaller.
  function shrinkImage(file, cb) {
    var img = new Image();
    var url = URL.createObjectURL(file);
    img.onload = function () {
      var scale = Math.min(1, 1280 / Math.max(img.width, img.height));
      var canvas = document.createElement('canvas');
      canvas.width = Math.round(img.width * scale);
      canvas.height = Math.round(img.height * scale);
      canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
      URL.revokeObjectURL(url);
      canvas.toBlob(cb, 'image/jpeg', 0.85);
    };
    img.onerror = function () { toast('That file isn\'t a photo.'); };
    img.src = url;
  }

  function upload(path, blob, type, okMessage) {
    busy = true;
    toast('Uploading…');
    return fetch(path, { method: 'POST', headers: authHeaders({ 'Content-Type': type }), body: blob })
      .then(function (r) { return r.json(); })
      .then(function (res) { toast(res.message || okMessage); if (res.state) setState(res.state); })
      .catch(function () { toast('Upload failed. Try again.'); })
      .then(function () { busy = false; });
  }

  function uploadPhoto(file) {
    if (DEMO) { toast('Demo: photos are saved only in the real app.'); return; }
    shrinkImage(file, function (blob) { upload('/api/photo', blob, 'image/jpeg'); });
  }

  // Proof clips: videos up to ~15 s / 20 MB go as they are, photos get shrunk.
  function uploadProof(file) {
    if (DEMO) { toast('Demo: proof clips work in the real app.'); return; }
    if (/^image\//.test(file.type)) { shrinkImage(file, function (blob) { upload('/api/proof', blob, 'image/jpeg'); }); return; }
    if (file.size > 20 * 1024 * 1024) { toast('Too big: keep the clip to about 5 seconds.'); return; }
    var v = document.createElement('video');
    var url = URL.createObjectURL(file);
    v.preload = 'metadata';
    v.onloadedmetadata = function () {
      URL.revokeObjectURL(url);
      if (v.duration > 16) { toast('Keep it short: 15 seconds max.'); return; }
      upload('/api/proof', file, file.type || 'video/mp4');
    };
    v.onerror = function () { URL.revokeObjectURL(url); upload('/api/proof', file, file.type || 'video/mp4'); };
    v.src = url;
  }

  function startRest(secs, shared) {
    clearInterval(restTimer);
    restLeft = secs;
    if (shared) quietAct({ type: 'live_rest', seconds: secs });
    render();
    restTimer = setInterval(function () {
      restLeft -= 1;
      if (restLeft > 0 && restLeft <= 3) FX.sounds.beep(false);
      if (restLeft <= 0) {
        clearInterval(restTimer);
        restLeft = 0;
        haptic('success');
        FX.sounds.beep(true);
        toast('Rest over. Next set!');
      }
      var btn = app.querySelector('[data-act="rest"][data-secs="60"], [data-act="live-rest"][data-secs="60"]');
      if (btn) btn.textContent = restLeft ? 'Rest ' + restLeft + 's' : 'Rest 60s';
    }, 1000);
  }

  function startTestTimer(key, secs) {
    clearInterval(testTimer.handle);
    testTimer = { key: key, left: secs, handle: null };
    render();
    testTimer.handle = setInterval(function () {
      testTimer.left -= 1;
      if (testTimer.left > 0 && testTimer.left <= 3) FX.sounds.beep(false);
      if (testTimer.left <= 0) {
        clearInterval(testTimer.handle);
        haptic('success');
        FX.sounds.beep(true);
        toast('Time! Enter your result.');
      }
      if (screen === 'test') render();
    }, 1000);
  }

  // ---------------------------------------------------------------- juicy tapping

  var deltaQueue = {}, deltaTimer = null, flushing = false, undoBase = null, combo = { n: 0, t: 0 };

  function tapDelta(el, itemId, delta) {
    var it = findItem(itemId);
    if (!it) return;
    if (state.today.paused_until) { toast('Workouts are paused. Resume first.'); return; }
    if (!undoBase) undoBase = snapshotValues();
    var before = it.done, after = Math.max(0, before + delta);
    if (after === before) return;
    it.done = after;
    deltaQueue[itemId] = (deltaQueue[itemId] || 0) + (after - before);
    var now = Date.now();
    combo.n = delta > 0 && now - combo.t < 1400 ? combo.n + 1 : 1;
    combo.t = now;
    haptic(delta > 0 ? 'light' : 'soft');
    FX.sounds.tap(combo.n);
    FX.floatText(el, (after > before ? '+' : '') + (after - before), after > before ? 'var(--me)' : 'var(--muted)', 30);
    if (combo.n >= 3) showCombo(combo.n);
    if (after > before) FX.mascotReact('jump');
    var justDone = before < it.target && after >= it.target;
    if (justDone) { FX.sounds.done(); haptic('success'); FX.mascotReact('flex'); }
    updateItemDom(it, justDone);
    clearTimeout(deltaTimer);
    deltaTimer = setTimeout(flushDeltas, 650);
  }

  function showCombo(n) {
    var old = document.querySelector('.combo');
    if (old) old.remove();
    var el = document.createElement('div');
    el.className = 'combo';
    el.textContent = 'COMBO x' + n + (n >= 6 ? ' 🔥' : '');
    document.body.appendChild(el);
    setTimeout(function () { el.remove(); }, 800);
  }

  function updateItemDom(it, justDone) {
    app.querySelectorAll('[data-item="' + it.id + '"]').forEach(function (card) {
      var num = card.querySelector('.num-val');
      if (num) { num.textContent = it.done; num.style.color = it.done >= it.target ? 'var(--me)' : 'var(--text)'; }
      var fill = card.querySelector('.bar > i');
      var w = Math.round(clamp(it.target ? it.done / it.target : 1) * 100);
      if (fill) fill.style.width = w + '%';
      prevVals['bex' + it.id] = w;
      if (justDone) { card.classList.add('done-glow'); FX.pop(card); }
    });
  }

  function flushDeltas() {
    if (flushing) { deltaTimer = setTimeout(flushDeltas, 300); return; }
    var ids = Object.keys(deltaQueue);
    if (!ids.length) return;
    var queue = deltaQueue, undo = undoBase;
    deltaQueue = {};
    undoBase = null;
    flushing = true;
    var chain = Promise.resolve(), last = null, words = [];
    ids.forEach(function (id) {
      var d = queue[id], it = findItem(+id);
      if (it && d) words.push((d > 0 ? '+' : '') + d + ' ' + it.name);
      while (d !== 0) {
        var step = Math.max(-100, Math.min(100, d));
        d -= step;
        (function (s) {
          chain = chain.then(function () {
            return api('/api/action', { type: 'item_delta', item_id: +id, delta: s }).then(function (r) { last = r; });
          });
        })(step);
      }
    });
    chain.then(function () {
      flushing = false;
      if (last) {
        toast(last.message || 'Logged ' + words.join(', '), last.message ? null : { undo: undo });
        setState(last.state);
      }
    }).catch(function (e) {
      flushing = false;
      toast(e.message);
      load();
    });
  }

  // ---------------------------------------------------------------- guided player

  function startPlayer() {
    var items = state.today.items;
    var order = items.filter(function (i) { return i.status !== 'completed' && i.status !== 'skipped'; }).map(function (i) { return i.id; });
    if (!order.length) order = items.map(function (i) { return i.id; });
    if (!order.length) { toast('No exercises today.'); return; }
    player = { order: order, i: 0, phase: 'work', left: 0, restTotal: 0, pending: 0, inflight: 0, timing: false, timer: null, flushT: null };
    screen = 'player';
    animateNext = true;
    if (navigator.wakeLock) navigator.wakeLock.request('screen').then(function (l) { wakeLock = l; }).catch(function () { /* fine */ });
    render();
    window.scrollTo(0, 0);
  }

  function stopPlayer() {
    if (!player) return;
    clearInterval(player.timer);
    clearTimeout(player.flushT);
    playerFlush();
    player = null;
    if (wakeLock) { wakeLock.release().catch(function () { /* fine */ }); wakeLock = null; }
  }

  function playerFlush(then) {
    var p = player;
    if (!p || !p.pending) { if (then) then(); return; }
    var it = findItem(p.order[p.i]), send = Math.min(100, p.pending);
    p.pending -= send;
    p.inflight += send;
    api('/api/action', { type: 'item_delta', item_id: it.id, delta: send }).then(function (res) {
      if (player === p) p.inflight -= send;
      setState(res.state);
      if (then) then();
    }).catch(function (e) {
      if (player === p) p.inflight -= send;
      toast(e.message);
    });
  }

  function playerAdd(el, n) {
    var p = player, it = findItem(p.order[p.i]);
    var before = it.done + p.pending + p.inflight;
    p.pending += n;
    var now = Date.now();
    combo.n = now - combo.t < 1400 ? combo.n + 1 : 1;
    combo.t = now;
    FX.sounds.tap(combo.n);
    haptic('light');
    FX.floatText(el, '+' + n, 'var(--me)', 34);
    if (combo.n >= 5 && combo.n % 5 === 0) showCombo(combo.n);
    var after = before + n;
    var countEl = app.querySelector('.p-count');
    if (countEl) countEl.textContent = after;
    var fill = app.querySelector('.player .bar > i');
    if (fill) fill.style.width = Math.round(clamp(after / it.target) * 100) + '%';
    clearTimeout(p.flushT);
    if (before < it.target && after >= it.target) {
      FX.sounds.done();
      haptic('success');
      playerFlush(playerNext);
    } else {
      p.flushT = setTimeout(function () { playerFlush(); }, 900);
    }
  }

  function playerNext() {
    var p = player;
    if (!p) return;
    clearInterval(p.timer);
    p.timing = false;
    if (p.i >= p.order.length - 1) { p.phase = 'done'; render(); return; }
    p.i += 1;
    var secs = state.today.quick ? 30 : 60;
    p.phase = 'rest';
    p.left = secs;
    p.restTotal = secs;
    quietAct({ type: 'live_rest', seconds: secs });
    render();
    p.timer = setInterval(function () {
      p.left -= 1;
      if (p.left > 0 && p.left <= 3) FX.sounds.beep(false);
      if (p.left <= 0) { clearInterval(p.timer); FX.sounds.beep(true); haptic('success'); p.phase = 'work'; }
      if (screen === 'player' && player === p) render();
    }, 1000);
  }

  function playerTimer() {
    var p = player, it = findItem(p.order[p.i]);
    p.timing = true;
    p.left = Math.max(1, it.target - it.done);
    p.timedStart = p.left;
    render();
    p.timer = setInterval(function () {
      p.left -= 1;
      if (p.left > 0 && p.left <= 3) FX.sounds.beep(false);
      if (p.left <= 0) {
        clearInterval(p.timer);
        FX.sounds.beep(true);
        haptic('success');
        p.timing = false;
        api('/api/action', { type: 'item_set', item_id: it.id, value: it.target }).then(function (res) { setState(res.state); playerNext(); });
        return;
      }
      if (screen === 'player' && player === p) render();
    }, 1000);
  }

  function playerTimerStop() {
    var p = player, it = findItem(p.order[p.i]);
    clearInterval(p.timer);
    p.timing = false;
    var held = p.timedStart - p.left;
    api('/api/action', { type: 'item_set', item_id: it.id, value: it.done + held }).then(function (res) { setState(res.state); });
  }

  // ---------------------------------------------------------------- events

  function switchTab(next) {
    if (next === tab && !screen) return;
    var from = TABS.indexOf(tab), to = TABS.indexOf(next);
    slideDir = screen ? '' : to > from ? 'from-right' : 'from-left';
    if (screen === 'live') quietAct({ type: 'live_stop' });
    if (screen === 'player') stopPlayer();
    tab = next;
    screen = null;
    showStickers = false;
    animateNext = true;
    haptic('light');
    render();
    if (next !== 'chat') window.scrollTo(0, 0);
  }

  function openLive() {
    screen = 'live';
    quietAct({ type: 'live_start' });
  }

  // Roast/hype animation in the arena: something flies over and hits.
  function attack(targetId, emoji, word, friendly) {
    var from = app.querySelector('[data-fighter="' + state.me.id + '"]'), to = app.querySelector('[data-fighter="' + targetId + '"]');
    if (!from || !to) { FX.floatText(null, emoji + ' ' + word, friendly ? 'var(--me)' : '#FF4D5E', 30); return; }
    FX.mascotReact('jump');
    FX.projectile(from, to, emoji, function () {
      if (friendly) { FX.sounds.hype(); FX.pop(to); } else { FX.sounds.hit(); FX.hit(to); haptic('heavy'); }
      FX.floatText(to, word, friendly ? 'var(--me)' : '#FF4D5E', 28);
    });
  }

  function sendChat(body) {
    if (DEMO) { act(body); return; }
    api('/api/action', body).then(function (res) {
      if (res.message) toast(res.message);
      FX.sounds.tap(2);
      nearBottom = true;
      setState(res.state);
    }).catch(function (e) { toast(e.message); });
  }

  function onClick(e) {
    var el = e.target.closest('[data-act]');
    if (!el) return;
    if (el.dataset.act === 'close-sheet' && e.target.closest('[data-stop]') && !e.target.closest('button')) return;
    var d = el.dataset;
    switch (d.act) {
      case 'tab': toastEl.hidden = true; switchTab(d.tab); break;
      case 'open-screen':
        toastEl.hidden = true;
        if (d.screen === 'live') openLive(); else screen = d.screen;
        sheet = null; animateNext = true; render(); window.scrollTo(0, 0);
        break;
      case 'close-screen':
        if (screen === 'live') quietAct({ type: 'live_stop' });
        if (screen === 'player') stopPlayer();
        screen = null; animateNext = true; render(); window.scrollTo(0, 0);
        break;
      case 'delta': tapDelta(el, +d.id, +d.delta); break;
      case 'item-done':
        var item = findItem(+d.id);
        if (item && item.done < item.target) { FX.sounds.done(); FX.mascotReact('flex'); FX.floatText(el, '✓', 'var(--me)', 34); }
        act({ type: 'item_done', item_id: +d.id }, { undo: snapshotValues([+d.id]) });
        break;
      case 'complete-all': act({ type: 'complete_all' }, { undo: snapshotValues() }); break;
      case 'undo': if (undoValues) { var v = undoValues; undoValues = null; toastEl.hidden = true; act({ type: 'items_set', values: v }); } break;
      case 'edit-amount': editingItem = +d.id; render(); var input = app.querySelector('input[name="value"]'); if (input) { input.focus(); input.select(); } break;
      case 'quick': act({ type: 'quick', on: d.on === '1' }); break;
      case 'feedback': act({ type: 'feedback', value: d.value }); break;
      case 'roast':
        act({ type: 'roast', target: +d.target }, { after: function (res) { if (res.sent) attack(+d.target, '🔥', 'ROASTED'); } });
        break;
      case 'open-roast': sheet = { kind: 'roast', target: +d.target }; render(); break;
      case 'hype':
        act({ type: 'hype', target: +d.target }, { after: function (res) { if (res.sent) attack(+d.target, '💪', 'HYPED', true); } });
        break;
      case 'react': act({ type: 'react', activity_id: +d.id, emoji: d.emoji }); break;
      case 'wake': act({ type: 'wake_answer', choice: +d.choice }); break;
      case 'wake-later': wakeDismissed = true; render(); break;
      case 'open-challenge': sheet = { kind: 'challenge' }; render(); break;
      case 'challenge-target': sheet.target = +d.target; render(); break;
      case 'challenge': sheet = null; act({ type: 'challenge', target: +d.target, kind: d.kind }); break;
      case 'challenge-answer': act({ type: 'challenge_answer', id: +d.id, accept: d.accept === '1' }); break;
      case 'forfeit-done': act({ type: 'forfeit_done', id: +d.id }); break;
      case 'open-bet': sheet = { kind: 'bet', target: others()[0].id, betKind: 'reps', stake: 10 }; render(); break;
      case 'bet-kind': sheet.betKind = d.kind; render(); break;
      case 'bet-stake': sheet.stake = +d.stake; render(); break;
      case 'bet-send': var bs = sheet; sheet = null; act({ type: 'bet_offer', target: bs.target, kind: bs.betKind, stake: bs.stake }); break;
      case 'bet-answer':
        act({ type: 'bet_answer', id: +d.id, accept: d.accept === '1' }, { after: function () { if (d.accept === '1') FX.floatText(null, '🎲 BET\'S ON!', '#FFD23F', 34); } });
        break;
      case 'toggle-anime': act({ type: 'settings', anime: !state.me.anime }); break;
      case 'toggle-sound': FX.setSound(!FX.soundOn()); FX.sounds.tap(1); render(); break;
      case 'toggle-setting': var change = { type: 'settings' }; change[d.key] = !state.settings[d.key]; act(change); break;
      case 'roast-level': act({ type: 'settings', roast_level: d.level }); break;
      case 'pause': act({ type: 'pause', days: +d.days }); break;
      case 'resume': act({ type: 'resume' }); break;
      case 'ex-toggle': act({ type: 'ex_toggle', id: +d.id }); break;
      case 'ex-target': act({ type: 'ex_target', id: +d.id, target: +d.target }); break;
      case 'ex-day':
        var ex = state.exercises.filter(function (x) { return x.id === +d.id; })[0];
        var days = ex.days.split(','), i = days.indexOf(d.day);
        if (i >= 0) days.splice(i, 1); else days.push(d.day);
        act({ type: 'ex_days', id: +d.id, days: days.join(',') });
        break;
      case 'ex-delete':
        if (el.dataset.confirm) act({ type: 'ex_delete', id: +d.id });
        else { el.dataset.confirm = '1'; el.textContent = 'Tap again to remove'; }
        break;
      case 'test-timer': startTestTimer(d.key, +d.secs); break;
      case 'test-skip': act({ type: 'fitness_save', key: d.key, value: null }); break;
      case 'test-pullups': act({ type: 'fitness_pullups', on: !state.fitness_test.pullups }); break;
      case 'equip':
        if (el.getAttribute('aria-disabled')) toast('Locked: ' + el.dataset.how);
        else act({ type: 'equip', slot: d.slot, id: d.id }, { after: function () { FX.mascotReact('flex'); } });
        break;
      case 'onboard': act({ type: 'onboard', hair: d.hair }, { after: function () { FX.confetti(1600); } }); break;
      case 'level-up': act({ type: 'level_up', exercise_id: +d.id }, { after: function () { FX.confetti(); FX.sounds.win(); } }); break;
      case 'photo-delete':
        if (el.dataset.confirm) act({ type: 'photo_delete', id: +d.id });
        else { el.dataset.confirm = '1'; el.textContent = 'Sure?'; }
        break;
      case 'day':
        sheet = { kind: 'day', date: d.date, data: null }; render();
        (DEMO ? Promise.resolve({ date: d.date, status: 'completed', items: [{ name: 'Push-ups', done: 50, target: 50, unit: 'reps' }] })
              : fetch('/api/day?date=' + encodeURIComponent(d.date), { headers: authHeaders() }).then(function (r) { return r.json(); }))
          .then(function (data) { if (sheet && sheet.kind === 'day') { sheet.data = data; render(); } });
        break;
      case 'rest': startRest(+d.secs, false); break;
      case 'live-rest': startRest(+d.secs, true); break;
      case 'close-sheet': sheet = null; render(); if (pendingReload) load(); break;
      case 'close-celebration':
        if (celebration && celebration.kind === 'rank') { quietAct({ type: 'ack_rank' }); state.me.seen_rank = state.me.rank.letter; }
        celebration = null; render(); break;
      // chat
      case 'chat-quick': sendChat({ type: 'chat_send', text: d.text }); break;
      case 'chat-sticker': showStickers = false; sendChat({ type: 'chat_send', sticker: d.sticker }); break;
      case 'toggle-stickers': showStickers = !showStickers; render(); break;
      case 'proof-vote':
        act({ type: 'proof_vote', id: +d.id, vote: d.vote }, { after: function () { FX.floatText(null, d.vote === 'legit' ? '🔥 LEGIT' : '🧢 CAP', d.vote === 'legit' ? 'var(--me)' : '#FF4D5E', 30); } });
        break;
      // guided player
      case 'start-player': startPlayer(); break;
      case 'p-add': playerAdd(el, +d.n); break;
      case 'p-finish':
        var cur = findItem(player.order[player.i]);
        clearTimeout(player.flushT);
        player.pending = 0;
        FX.sounds.done();
        api('/api/action', { type: 'item_set', item_id: cur.id, value: Math.max(cur.target, cur.done) }).then(function (res) { setState(res.state); playerNext(); });
        break;
      case 'p-next': clearTimeout(player.flushT); playerFlush(); playerNext(); break;
      case 'p-skip-rest': clearInterval(player.timer); player.phase = 'work'; render(); break;
      case 'p-rest-add': player.left += 15; player.restTotal += 15; render(); break;
      case 'p-timer': playerTimer(); break;
      case 'p-timer-stop': playerTimerStop(); break;
      // report card
      case 'report-which': reportWhich = d.which; animateNext = true; render(); break;
      case 'report-image':
        var rc = state.report[reportWhich];
        toast('Drawing your card…');
        FX.reportImage(rc, mascot(rc.gear, rc.is_champion ? 'cheer' : 'determined', 300, 'Fighter'), monthName(rc.month))
          .then(function (url) { sheet = { kind: 'image', url: url }; render(); });
        break;
    }
  }

  function onSubmit(e) {
    var form = e.target.closest('[data-form]');
    if (!form) return;
    e.preventDefault();
    var f = form.dataset, data = new FormData(form);
    switch (f.form) {
      case 'set-amount':
        editingItem = null;
        act({ type: 'item_set', item_id: +f.id, value: +data.get('value') }, { undo: snapshotValues([+f.id]) });
        break;
      case 'settings-time': var s = { type: 'settings' }; s[f.key] = data.get('value'); act(s); break;
      case 'ex-add': act({ type: 'ex_add', name: data.get('name'), target: +data.get('target'), unit: data.get('unit') }); break;
      case 'test-save': act({ type: 'fitness_save', key: f.key, value: +data.get('value') }); break;
      case 'body-log': act({ type: 'body_log', weight: data.get('weight'), waist: data.get('waist') }); break;
      case 'roast':
        sheet = null;
        act({ type: 'roast', target: +f.target, text: data.get('text') }, { after: function (res) { if (res.sent) attack(+f.target, '🔥', 'ROASTED'); } });
        break;
      case 'chat':
        var text = String(data.get('text') || '').trim();
        if (!text) return;
        chatDraft = '';
        sendChat({ type: 'chat_send', text: text });
        break;
    }
  }

  app.addEventListener('click', onClick);
  toastEl.addEventListener('click', onClick);
  app.addEventListener('submit', onSubmit);
  app.addEventListener('change', function (e) {
    if (e.target.matches('[data-input="photo"]') && e.target.files[0]) uploadPhoto(e.target.files[0]);
    if (e.target.matches('[data-input="proof"]') && e.target.files[0]) uploadProof(e.target.files[0]);
  });
  app.addEventListener('input', function (e) {
    if (e.target.name === 'text' && e.target.closest('[data-form="chat"]')) chatDraft = e.target.value;
  });
  app.addEventListener('focusout', function () {
    setTimeout(function () { if (pendingReload && !typing() && !sheet) load(); }, 50);
  });

  // Swipe between tabs; pull down at the top to refresh.
  var touch = null;
  document.addEventListener('touchstart', function (e) {
    if (e.touches.length !== 1) { touch = null; return; }
    var t = e.touches[0];
    touch = { x: t.clientX, y: t.clientY, t: Date.now(), top: window.scrollY <= 0, pull: 0,
      noSwipe: !!e.target.closest('.no-swipe, input, textarea, select, .composer, .scrim, .burst, video, .player') };
  }, { passive: true });
  document.addEventListener('touchmove', function (e) {
    if (!touch || !touch.top || screen || sheet) return;
    var t = e.touches[0], dy = t.clientY - touch.y, dx = t.clientX - touch.x;
    if (dy > 10 && Math.abs(dx) < dy && window.scrollY <= 0) {
      touch.pull = dy;
      ptr.style.transform = 'translateY(' + Math.min(70, dy * 0.45) + 'px) rotate(' + dy * 2 + 'deg)';
    }
  }, { passive: true });
  document.addEventListener('touchend', function (e) {
    if (!touch) return;
    var t = e.changedTouches[0], dx = t.clientX - touch.x, dy = t.clientY - touch.y, dt = Date.now() - touch.t;
    if (touch.pull) {
      if (touch.pull > 120) {
        ptr.classList.add('spin');
        ptr.style.transform = 'translateY(24px)';
        haptic('light');
        load().then(function () { ptr.classList.remove('spin'); ptr.style.transform = ''; });
      } else {
        ptr.style.transform = '';
      }
    } else if (!touch.noSwipe && !screen && !sheet && state && !overlayShowing() && Math.abs(dx) > 70 && Math.abs(dy) < 45 && dt < 600) {
      var i = TABS.indexOf(tab) + (dx < 0 ? 1 : -1);
      if (i >= 0 && i < TABS.length) switchTab(TABS[i]);
    }
    touch = null;
  }, { passive: true });

  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible' && state && !DEMO) load();
  });

  if (tg) {
    tg.ready();
    tg.expand();
    try {
      if (tg.isVersionAtLeast && tg.isVersionAtLeast('6.1')) { tg.setHeaderColor('#0F1115'); tg.setBackgroundColor('#0F1115'); }
      if (tg.isVersionAtLeast && tg.isVersionAtLeast('7.7') && tg.disableVerticalSwipes) tg.disableVerticalSwipes();
    } catch (e) { /* older Telegram */ }
  }

  // ---------------------------------------------------------------- demo

  var demoState = null;
  function demoApi(path, body) {
    if (!demoState) demoState = makeDemo();
    var s = demoState, out = { ok: true, message: null };
    function item(id) { return s.today.items.filter(function (i) { return i.id === id; })[0]; }
    function recompute() {
      var t = s.today;
      t.items.forEach(function (i) { i.status = i.done >= i.target ? 'completed' : 'pending'; });
      t.done_count = t.items.filter(function (i) { return i.status === 'completed'; }).length;
      t.progress = t.items.reduce(function (a, i) { return a + Math.min(1, i.done / i.target); }, 0) / t.items.length;
      t.status = t.done_count === t.items.length ? 'completed' : 'pending';
      t.can_give_feedback = t.status === 'completed' && !t.feedback;
      var mine = s.crew[0];
      mine.progress = t.progress; mine.done = t.done_count; mine.status = t.status;
      mine.reps = t.items.reduce(function (a, i) { return a + (i.unit === 'reps' ? i.done : 0); }, 0);
      s.sig = 'demo-' + Date.now();
    }
    function chat(kind, text) {
      s.chat.push({ id: Date.now(), user_id: 1, who: 'Victor', me: true, kind: kind, text: text || '', at: new Date().toISOString() });
    }
    if (path === '/api/pulse') {
      // The demo brother trains now and then, so the arena and alerts come alive.
      var bro = s.crew[1];
      if (Math.random() < 0.18 && bro.progress < 1) {
        bro.reps += 5;
        bro.progress = Math.min(1, Math.round((bro.progress + 0.05) * 100) / 100);
        s.activity.unshift({ id: Date.now(), who: 'Andrei', me: false, kind: 'progress', text: '+5 Push-ups', at: new Date().toISOString(), reactions: [], my_reaction: null });
        s.sig = 'demo-' + Date.now();
      }
      return { sig: s.sig, unread: s.unread, chat_last: 0, crew: s.crew.map(function (c) {
        return { id: c.id, status: c.status, progress: c.progress, reps: c.reps, live: c.live, rest_left: 0, in_app: true, items: [] };
      }) };
    }
    if (body) {
      var x;
      switch (body.type) {
        case 'item_delta': x = item(body.item_id); x.done = Math.max(0, x.done + body.delta); break;
        case 'item_set': item(body.item_id).done = body.value; break;
        case 'items_set': Object.keys(body.values).forEach(function (k) { item(+k).done = body.values[k]; }); out.message = 'Undone.'; break;
        case 'item_done': item(body.item_id).done = item(body.item_id).target; break;
        case 'complete_all': s.today.items.forEach(function (i) { i.done = i.target; }); break;
        case 'quick': s.today.quick = body.on; break;
        case 'feedback': s.today.feedback = body.value; out.message = 'Thanks! Noted.'; break;
        case 'roast': out.message = 'Demo: roast delivered to Andrei 😈'; out.sent = true;
          s.activity.unshift({ id: Date.now(), who: 'Victor', me: true, kind: 'roast', text: 'roasted Andrei 😈: ' + (body.text || 'Loser alert!'), at: new Date().toISOString(), reactions: [], my_reaction: null }); break;
        case 'hype': out.message = 'Demo: hype sent to Andrei 💪'; out.sent = true; break;
        case 'react':
          s.activity.forEach(function (a) { if (a.id === body.activity_id) { a.my_reaction = a.my_reaction === body.emoji ? null : body.emoji; a.reactions = a.my_reaction ? [{ emoji: a.my_reaction, count: 1 }] : []; } }); break;
        case 'wake_answer':
          if (body.choice !== s.wake.today.challenge) { out.message = 'Wrong number. Are you even awake?'; break; }
          s.wake.today = { status: 'on_time', answered_at: '06:31', minutes_late: 1, choices: [] };
          s.crew[0].wake = '☀️ 06:31 ✅'; out.message = 'On time! Good morning.'; break;
        case 'challenge': out.message = 'Challenge sent!'; break;
        case 'challenge_answer': s.challenges = []; out.message = body.accept ? 'Game on!' : 'Bawk bawk 🐔'; break;
        case 'forfeit_done': s.forfeits = []; out.message = 'Forfeit done. +5 pts'; break;
        case 'settings':
          if ('anime' in body) s.me.anime = !!body.anime;
          ['workout_time', 'wake_time', 'wake_enabled', 'roast_level', 'feed', 'notifications'].forEach(function (k) { if (k in body) s.settings[k] = body[k]; });
          if (body.workout_time || body.wake_time) out.message = 'Saved.'; break;
        case 'pause': s.settings.paused_until = '2026-12-31'; out.message = 'Paused. Your streak is safe.'; break;
        case 'resume': s.settings.paused_until = null; out.message = 'Welcome back!'; break;
        case 'ex_toggle': s.exercises.forEach(function (e) { if (e.id === body.id) e.active = !e.active; }); break;
        case 'ex_target': s.exercises.forEach(function (e) { if (e.id === body.id) e.target = body.target; }); break;
        case 'ex_days': s.exercises.forEach(function (e) { if (e.id === body.id) e.days = body.days; }); break;
        case 'ex_delete': s.exercises = s.exercises.filter(function (e) { return e.id !== body.id; }); out.message = 'Exercise removed.'; break;
        case 'ex_add': s.exercises.push({ id: Date.now(), name: body.name, target: body.target, unit: body.unit === 'sec' ? 's' : 'reps', active: true, days: '0,1,2,3,4,5,6' }); out.message = 'Exercise added.'; break;
        case 'fitness_save':
          s.fitness_test.tests.forEach(function (t) { if (t.key === body.key) { t.done = true; t.result = body.value; } });
          if (body.key === 'pushups' && body.value > 31) { out.pr = true; out.message = '🏅 New record! ' + body.value + ' reps'; } else out.message = body.value === null ? 'Skipped.' : 'Saved.'; break;
        case 'fitness_pullups': s.fitness_test.pullups = body.on; break;
        case 'equip': s.me.gear.ids[body.slot] = body.id;
          s.gear_catalog[body.slot].forEach(function (g) { if (g.id === body.id) s.me.gear[body.slot] = g.color; }); s.crew[0].gear = s.me.gear; break;
        case 'onboard': s.me.onboarded = true; out.message = 'Welcome to the crew! Let\'s train.'; break;
        case 'ack_rank': s.me.seen_rank = s.me.rank.letter; break;
        case 'level_up': out.message = 'Level up! Push-ups → Diamond push-ups (target 21).'; break;
        case 'body_log': if (body.weight) { s.body.latest_weight = +body.weight; s.body.logged_today = true; } out.message = 'Saved. Only you can see this.'; break;
        case 'chat_send': chat(body.sticker ? 'sticker' : 'text', body.sticker || body.text); break;
        case 'chat_seen': s.unread = 0; break;
        case 'live_start': s.crew[0].live = true; break;
        case 'live_stop': s.crew[0].live = false; break;
        case 'live_rest': break;
        case 'bet_offer': s.bets.unshift({ id: Date.now(), kind: body.kind, label: s.bet_kinds.filter(function (k) { return k.key === body.kind; })[0].label,
          stake: body.stake, status: 'offered', rival: 'Andrei', mine_to_answer: false, i_offered: true, result: null, score: null }); out.message = 'Bet sent! They have until 23:00 to answer.'; break;
        case 'bet_answer': s.bets.forEach(function (b) { if (b.id === body.id) { b.status = body.accept ? 'accepted' : 'declined'; b.mine_to_answer = false; b.score = b.kind === 'first' ? null : { me: s.crew[0].reps, rival: s.crew[1].reps }; } });
          out.message = body.accept ? 'Bet\'s on! 🎲' : 'Backed out. No points lost.'; break;
        case 'proof_vote': s.chat.forEach(function (m) {
          if (m.proof && m.proof.id === body.id) {
            m.proof.mine = m.proof.mine === body.vote ? null : body.vote;
            m.proof.counts = { legit: m.proof.mine === 'legit' ? 1 : 0, cap: m.proof.mine === 'cap' ? 1 : 0 };
            m.proof.verdict = m.proof.mine;
          }
        }); break;
      }
      recompute();
      out.state = JSON.parse(JSON.stringify(s));
      return out;
    }
    return JSON.parse(JSON.stringify(s));
  }

  function makeDemo() {
    var today = new Date().toISOString().slice(0, 10);
    var month = today.slice(0, 7);
    var first = new Date(today + 'T12:00:00');
    first.setDate(0);
    var lastMonth = first.toISOString().slice(0, 7);
    var now = Date.now();
    function minsAgo(m) { return new Date(now - m * 60000).toISOString(); }
    var days = function (codes) { return codes.map(function (c) { return { date: today, code: c }; }); };
    var catalog = {
      hair: [['lime', 'Volt', '#C8F135', 1], ['orange', 'Blaze', '#FF8A3D', 1], ['blue', 'Azure', '#7FB2FF', 1], ['pink', 'Sakura', '#F06BC6', 1],
        ['silver', 'Silver', '#D9DEE7', 0, 'Reach rank A'], ['crimson', 'Crimson', '#FF4D5E', 0, 'Win 3 challenges'], ['gold', 'Golden', '#FFD23F', 0, '5,000 lifetime reps']],
      band: [['orange', 'Orange band', '#FF8A3D', 1], ['lime', 'Volt band', '#C8F135', 1], ['red', 'Crimson band', '#E5484D', 1],
        ['gold', 'Gold band', '#FFD23F', 0, 'Reach rank B'], ['ink', 'Ninja band', '#1B1E24', 0, '10 on-time wake-ups']],
      aura: [['none', 'No aura', '', 1], ['spark', 'Spark aura', '#FFD23F', 1], ['flame', 'Flame aura', '#FF8A3D', 0, '21-day streak'],
        ['lightning', 'Lightning aura', '#7FB2FF', 0, 'Win 5 challenges'], ['royal', 'Royal aura', '#C8A2FF', 0, 'Win a monthly season']]
    };
    var gearCatalog = {};
    Object.keys(catalog).forEach(function (slot) {
      gearCatalog[slot] = catalog[slot].map(function (g) { return { id: g[0], label: g[1], color: g[2], unlocked: !!g[3], how: g[4] || 'Starter' }; });
    });
    var myGear = { hair: '#C8F135', band: '#FF8A3D', aura: '#FFD23F', ids: { hair: 'lime', band: 'orange', aura: 'spark' } };
    var broGear = { hair: '#FF8A3D', band: '#C8F135', aura: '', ids: {} };
    var report = function (m, workouts) {
      return { month: m, name: 'Victor', workouts: workouts, planned: workouts + 2, reps: workouts * 180, best_streak: Math.min(workouts, 12), best_day: { date: today, reps: 240 },
        points: workouts * 14, rank: 'C', xp: 340, gear: myGear, weight_change: -1.4, champion: 'Victor', is_champion: true };
    };
    return {
      sig: 'demo',
      me: { id: 1, name: 'Victor', is_owner: true, color: '#C8F135', anime: true, roast_level: 'savage', feed: true, onboarded: false,
        rank: { letter: 'C', index: 2, xp: 340, floor: 300, next_letter: 'B', next_at: 700 }, seen_rank: 'C', gear: myGear },
      settings: { workout_time: '07:00', timezone: 'Europe/Chisinau', notifications: true, wake_enabled: true, wake_time: '06:30', roast_level: 'savage', feed: true, paused_until: null },
      exercises: [{ id: 11, name: 'Push-ups', target: 50, unit: 'reps', active: true, days: '0,1,2,3,4,5,6' },
        { id: 12, name: 'Squats', target: 50, unit: 'reps', active: true, days: '0,1,2,3,4,5,6' },
        { id: 13, name: 'Sit-ups', target: 30, unit: 'reps', active: true, days: '0,2,4' },
        { id: 14, name: 'Plank', target: 60, unit: 's', active: true, days: '0,1,2,3,4,5,6' }],
      fitness_test: { month: month, pullups: false, complete: false, tests: [
        { key: 'pushups', name: 'Max push-ups', unit: 'reps', how: 'One set, good form: chest close to the floor, body straight.', timer: null, done: false, result: null },
        { key: 'squats', name: 'Squats in 2 minutes', unit: 'reps', how: 'As many full squats as you can in 2 minutes.', timer: 120, done: false, result: null },
        { key: 'plank', name: 'Longest plank', unit: 'sec', how: 'Forearm plank until your hips drop.', timer: null, done: false, result: null }] },
      ladders: [{ exercise_id: 11, name: 'Push-ups', level: 2, levels: 6, next: 'Diamond push-ups', unlock_target: 60, unit: 'reps', solid_days: 2, days_needed: 3, can_level_up: false },
        { exercise_id: 14, name: 'Plank', level: 1, levels: 5, next: 'Long-lever plank', unlock_target: 120, unit: 's', solid_days: 3, days_needed: 3, can_level_up: true }],
      gear_catalog: gearCatalog,
      body: { weekly: [{ week: '2026-08-24', avg: 82.4 }, { week: '2026-08-31', avg: 82.0 }, { week: '2026-09-07', avg: 81.7 }, { week: '2026-09-14', avg: 81.5 },
        { week: '2026-09-21', avg: 81.0 }, { week: '2026-09-28', avg: 80.6 }, { week: '2026-10-05', avg: 80.2 }],
        latest_weight: 80.1, weight_change: -2.3, waist: [{ date: '2026-09-01', value: 90 }, { date: '2026-10-01', value: 88 }], waist_change: -2, logged_today: false, photos: [] },
      seasons: { month: month, standings: [{ name: 'Victor', points: 220, color: '#C8F135' }, { name: 'Andrei', points: 185, color: '#FF8A3D' }],
        champions: [{ month: lastMonth, title: 'Last month\'s champion', name: 'Andrei' }] },
      activity: [
        { id: 3, who: 'Andrei', me: false, kind: 'progress', text: '+20 Push-ups', at: minsAgo(4), reactions: [], my_reaction: null },
        { id: 2, who: 'Andrei', me: false, kind: 'wake', text: 'woke up at 07:42, 42 min late 🐢', at: minsAgo(95), reactions: [{ emoji: '😂', count: 1 }], my_reaction: '😂' },
        { id: 1, who: 'Victor', me: true, kind: 'wake', text: 'woke up at 06:31 ✅', at: minsAgo(160), reactions: [], my_reaction: null }],
      reactions: ['🔥', '💀', '😂', '🐔', '👑'],
      ghost: { time: '18:40', rival_yesterday: 120, me_today: 100 },
      chat: [
        { id: 1, user_id: 2, who: 'Andrei', me: false, kind: 'text', text: 'bet you can\'t beat 100 push-ups today', at: minsAgo(120) },
        { id: 2, user_id: 1, who: 'Victor', me: true, kind: 'text', text: 'watch me 😤', at: minsAgo(118) },
        { id: 3, user_id: 2, who: 'Andrei', me: false, kind: 'sticker', text: 'smirk', at: minsAgo(117) },
        { id: 4, user_id: 2, who: 'Andrei', me: false, kind: 'proof', text: '', at: minsAgo(30),
          proof: { id: 9, media: 'video', available: true, mine: null, counts: { legit: 0, cap: 0 }, verdict: null } }],
      unread: 2,
      stickers: { flex: '💪', fire: '🔥', sleepy: '😴', smirk: '😏', cry: '😭', rage: '😤' },
      quick_replies: ['Lazy? 😴', 'Watch this 😤', 'Your move 👀', 'GG 🤝', 'Catch me if you can 🏃', 'Not today 🛑'],
      bets: [{ id: 21, kind: 'push', label: 'More push-ups today', stake: 10, status: 'offered', rival: 'Andrei', mine_to_answer: true, i_offered: false, result: null, score: null }],
      bet_kinds: [{ key: 'reps', label: 'More total reps today' }, { key: 'push', label: 'More push-ups today' }, { key: 'first', label: 'Finish today\'s workout first' }],
      stakes: [5, 10, 20],
      report: { last: report(lastMonth, 24), current: report(month, 8) },
      today: { date: today, status: 'pending', quick: false, comeback_pct: null, feedback: null, can_give_feedback: false, paused_until: null,
        done_count: 1, progress: 0.62,
        items: [
          { id: 1, name: 'Push-ups', done: 30, target: 50, unit: 'reps', status: 'pending' },
          { id: 2, name: 'Squats', done: 50, target: 50, unit: 'reps', status: 'completed' },
          { id: 3, name: 'Sit-ups', done: 20, target: 30, unit: 'reps', status: 'pending' },
          { id: 4, name: 'Plank', done: 30, target: 60, unit: 's', status: 'pending' }] },
      streak: 12, best_streak: 21,
      week: days(['done', 'done', 'done', 'today', 'future', 'future', 'future']),
      last4weeks: days(['done', 'done', 'done', 'done', 'done', 'rest', 'done', 'done', 'partial', 'done', 'done', 'paused', 'paused', 'paused',
        'done', 'done', 'done', 'done', 'done', 'done', 'done', 'done', 'done', 'done', 'today', 'future', 'future', 'future']),
      crew: [
        { id: 1, name: 'Victor', me: true, color: '#C8F135', status: 'pending', done: 1, total: 4, reps: 100, streak: 12, progress: 0.62, wake: '', rank: 'C', gear: myGear, title: null, live: false, rest_left: 0 },
        { id: 2, name: 'Andrei', me: false, color: '#FF8A3D', status: 'pending', done: 1, total: 4, reps: 30, streak: 4, progress: 0.25, wake: '☀️ 07:42 ⏰', rank: 'D',
          gear: broGear, title: 'Last month\'s champion', live: true, rest_left: 0 }],
      crew_streak: 5,
      points: [{ id: 1, name: 'Victor', points: 85, color: '#C8F135' }, { id: 2, name: 'Andrei', points: 60, color: '#FF8A3D' }],
      my_points: { workout: 50, full: 20, wake: 15, challenge: 0, test_pr: 0, forfeit: 0, proof: 3, bets: -5, total: 83 },
      wake: { enabled: true, time: '06:30', streak: 9, today: { status: 'pending', challenge: 7, choices: [3, 9, 7, 1], answered_at: null, minutes_late: null } },
      fitness: [{ key: 'pushups', name: 'Max push-ups', unit: 'reps', points: [
        { month: '2026-07', value: 18 }, { month: '2026-08', value: 24 }, { month: '2026-09', value: 27 }, { month: '2026-10', value: 31 }] }],
      records: [{ name: 'Push-ups', unit: 'reps', best: 60 }, { name: 'Plank', unit: 's', best: 120 }],
      challenges: [{ id: 5, label: '💪 100 push-ups today', status: 'offered', from: 'Andrei', to: 'Victor', mine_to_answer: true }],
      challenge_kinds: [{ key: 'push100', label: '💪 100 push-ups today' }, { key: 'squat150', label: '🦵 150 squats today' },
        { key: 'plank180', label: '🧘 3 minutes of plank today' }, { key: 'full', label: '🔥 Full workout today' }, { key: 'early', label: '⚡ Full workout before 09:00' }],
      forfeits: [{ id: 3, task: '💪 50 extra push-ups', status: 'pending', loser: 'Andrei', winner: 'Victor', mine_to_do: false }],
      feedback_options: [{ key: 'easy', label: '😴 Too easy' }, { key: 'ok', label: '👌 Just right' }, { key: 'hard', label: '🥵 Too hard' }]
    };
  }

  // Outside Telegram: use the local dev server if it answers, otherwise the demo.
  function boot() {
    if (initData) return load();
    return fetch('/api/state', { headers: authHeaders() }).then(function (res) {
      if (!res.ok) throw new Error('no dev server');
      return res.json();
    }).then(function (data) { DEV = true; setState(data); }, function () { DEMO = true; return load(); });
  }
  boot().then(pulseLoop);
})();
