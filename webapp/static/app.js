/* Workout Crew Mini App. Plain JS, no build step, no dependencies.
 *
 * All data comes from GET /api/state and every change goes through
 * POST /api/action (see webapp/server.py); each call carries Telegram's
 * signed initData, which the server verifies. Opened outside Telegram, the
 * app runs a local demo with sample data instead (nothing is sent anywhere).
 */
(function () {
  'use strict';

  var tg = window.Telegram && window.Telegram.WebApp;
  var initData = tg && tg.initData;
  var DEMO = !initData;

  var app = document.getElementById('app');
  var toastEl = document.getElementById('toast');

  var state = null;
  var tab = 'home';
  var sheet = null;      // {kind: 'challenge', target?}
  var burst = false;     // "workout complete" panel
  var busy = false;
  var wakeDismissed = false; // "Not now" on the wake-up check (until reload)
  var restLeft = 0, restTimer = null;

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

  var toastTimer = null;
  function toast(message) {
    if (!message) return;
    toastEl.textContent = message;
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.hidden = true; }, 2600);
  }

  function ring(size, stroke, progress, color, inner) {
    var r = (size - stroke) / 2;
    var c = 2 * Math.PI * r;
    var dash = (Math.max(0, Math.min(1, progress)) * c).toFixed(1) + ' ' + c.toFixed(1);
    return '<div class="ring" style="width:' + size + 'px;height:' + size + 'px">' +
      '<svg width="' + size + '" height="' + size + '" viewBox="0 0 ' + size + ' ' + size + '" aria-hidden="true">' +
      '<circle cx="' + size / 2 + '" cy="' + size / 2 + '" r="' + r + '" fill="none" stroke="#2A2E37" stroke-width="' + stroke + '"></circle>' +
      '<circle class="fg" cx="' + size / 2 + '" cy="' + size / 2 + '" r="' + r + '" fill="none" stroke="' + h(color) + '" stroke-width="' + stroke +
      '" stroke-linecap="round" stroke-dasharray="' + dash + '" transform="rotate(-90 ' + size / 2 + ' ' + size / 2 + ')"></circle></svg>' +
      '<div class="val">' + inner + '</div></div>';
  }

  function pct(p) { return Math.round(p * 100) + '%'; }

  function bar(p, color) {
    return '<div class="bar"><i style="width:' + Math.round(Math.min(1, p) * 100) + '%;background:' + h(color || 'var(--me)') + '"></i></div>';
  }

  // Original chibi mascot. mood: determined | cheer | sleepy | smirk
  function mascot(hair, band, mood, size, label) {
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
    } else {
      eyes = '<path d="M31 53 L45 57" stroke="#141414" stroke-width="3.5" stroke-linecap="round"/><path d="M69 53 L55 57" stroke="#141414" stroke-width="3.5" stroke-linecap="round"/>' +
        '<ellipse cx="39" cy="64" rx="5.5" ry="7.5" fill="#141414"/><ellipse cx="61" cy="64" rx="5.5" ry="7.5" fill="#141414"/>' +
        '<circle cx="41" cy="61" r="2.2" fill="#fff"/><circle cx="63" cy="61" r="2.2" fill="#fff"/>';
      mouth = '<path d="M42 76 Q50 83 58 76" stroke="#141414" stroke-width="3" fill="none" stroke-linecap="round"/>';
    }
    return '<svg width="' + size + '" height="' + size + '" viewBox="0 0 100 100" role="img" aria-label="' + h(label || 'Mascot') + '">' +
      '<path d="M16 56 L6 32 L26 40 L22 14 L40 30 L50 4 L60 30 L78 14 L74 40 L94 32 L84 56 Z" fill="' + h(hair) + '"/>' +
      '<circle cx="50" cy="60" r="29" fill="#FFD9B8"/>' +
      '<rect x="21" y="43" width="58" height="9" rx="3" fill="' + h(band) + '"/>' +
      '<path d="M78 45 L96 38 L90 52 Z" fill="' + h(band) + '"/>' + eyes +
      '<ellipse cx="30" cy="72" rx="4.5" ry="2.2" fill="#FF9E9E"/><ellipse cx="70" cy="72" rx="4.5" ry="2.2" fill="#FF9E9E"/>' + mouth +
      (mood === 'sleepy' ? '<text x="78" y="26" fill="#A3A8B3" font-size="16" font-weight="700">z</text><text x="88" y="14" fill="#A3A8B3" font-size="12" font-weight="700">z</text>' : '') +
      '</svg>';
  }

  var ICONS = {
    home: '<path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"/>',
    today: '<path d="M6 7v10M18 7v10M3 10v4M21 10v4M6 12h12"/>',
    duel: '<path d="M4 20L14 10M10 4l10 10M20 20L10 10M14 4L4 14"/>',
    progress: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    flame: '<path d="M12 2c1 4 5 5 5 10a5 5 0 0 1-10 0c0-3 2-4 2-7 1 1 2 2 3 4"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    spark: '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/>'
  };
  function icon(name, size, color) {
    return '<svg width="' + (size || 24) + '" height="' + (size || 24) + '" viewBox="0 0 24 24" fill="none" stroke="' + (color || 'currentColor') +
      '" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + ICONS[name] + '</svg>';
  }

  function me() { return state.me; }
  function anime() { return state.me.anime; }
  function others() { return state.crew.filter(function (m) { return !m.me; }); }
  function meCrew() { return state.crew.filter(function (m) { return m.me; })[0]; }

  function rank(member) {
    if (member.streak >= 14) return 'S';
    if (member.streak >= 7) return 'A';
    if (member.streak >= 3) return 'B';
    return 'C';
  }

  function title(member) {
    if (/late|😴|⏰/.test(member.wake || '')) return 'The Sleepyhead';
    if (member.status === 'completed') return 'The Relentless';
    if (member.streak === 0) return 'The Comeback Kid';
    return 'The Grinder';
  }

  function greeting() {
    var hour = new Date().getHours();
    if (hour < 12) return 'Morning';
    if (hour < 18) return 'Afternoon';
    return 'Evening';
  }

  function niceDate(iso) {
    var d = new Date(iso + 'T12:00:00');
    return d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' });
  }

  // ---------------------------------------------------------------- screens

  function homeScreen() {
    var t = state.today;
    var parts = [];
    var eyebrow = anime()
      ? '<span class="jp me-c">修行編</span> · Training arc, day ' + (state.streak + (t.status === 'completed' ? 0 : 1))
      : h(niceDate(t.date));
    parts.push('<header class="row between"><div class="grow"><div class="small muted">' + eyebrow + '</div>' +
      '<div class="display h1">' + h(greeting()) + ', ' + h(me().name) + '</div></div>' +
      '<button class="btn icon" data-act="toggle-anime" aria-label="' + (anime() ? 'Turn anime style off' : 'Turn anime style on') + '">' +
      icon('spark', 22, anime() ? 'var(--me)' : 'var(--muted)') + '</button></header>');

    if (anime()) {
      var line;
      if (t.paused_until) line = 'Vacation arc. Recharge, then we come back stronger.';
      else if (t.status === 'rest') line = 'Rest day. Even heroes recover.';
      else if (t.status === 'completed') line = 'Today is ours. Rest up, legend.';
      else if (t.done_count > 0) line = (t.items.length - t.done_count) + ' more to go. Finish it!';
      else line = 'Day ' + (state.streak + 1) + ' of the arc. Let\'s go!';
      parts.push('<div class="row">' + mascot(me().color, '#FF8A3D', t.status === 'completed' ? 'cheer' : 'determined', 64, 'Your mascot') +
        '<div class="bubble grow">' + h(line) + '</div></div>');
    }

    var status = t.paused_until ? 'Paused until ' + h(niceDate(t.paused_until))
      : t.status === 'rest' ? 'Rest day' : t.done_count + ' of ' + t.items.length + ' exercises';
    parts.push('<section class="card row" style="gap:16px">' +
      ring(116, 12, t.progress, me().color, '<span class="display" style="font-size:34px">' + pct(t.progress) + '</span><span class="small muted">today</span>') +
      '<div class="grow" style="display:flex;flex-direction:column;gap:10px">' +
      '<div><div class="small muted">Done</div><div class="display h2">' + status + '</div></div>' +
      '<div class="row small">' + icon('flame', 18, 'var(--them)') + '<span><b>' + state.streak + '-day</b> streak · best ' + state.best_streak + '</span>' +
      (anime() ? '<span class="badge" style="background:var(--them);transform:rotate(-6deg)">LV ' + state.streak + '</span>' : '') + '</div>' +
      (t.status === 'pending' ? '<button class="btn primary" data-act="tab" data-tab="today">Continue workout</button>' : '') +
      '</div></section>');

    parts.push('<section class="card"><div class="small muted">This week</div><div class="days">' +
      ['M', 'T', 'W', 'T', 'F', 'S', 'S'].map(function (d) { return '<span class="small muted">' + d + '</span>'; }).join('') +
      state.week.map(function (d) { return '<span class="day ' + h(d.code) + '" title="' + h(d.date + ': ' + d.code) + '"></span>'; }).join('') +
      '</div></section>');

    parts.push(answerCards());

    if (others().length) {
      var rows = others().map(function (o) {
        return '<div class="row"><span class="display" style="width:38px;height:38px;border-radius:19px;background:' + h(o.color) +
          ';color:#0F1115;display:flex;align-items:center;justify-content:center;font-size:20px">' + h(o.name.charAt(0)) + '</span>' +
          '<div class="grow"><div class="row between"><b>' + h(o.name) + '</b><span class="small muted">' +
          (o.status === 'completed' ? 'done' : o.done + ' of ' + o.total) + ' · ' + o.streak + '-day streak</span></div>' + bar(o.progress, o.color) + '</div></div>';
      }).join('');
      var pts = state.points.map(function (p) { return '<b style="color:' + h(p.color) + '">' + p.points + '</b>'; }).join(' <span class="muted">vs</span> ');
      parts.push('<button class="card tap" data-act="tab" data-tab="duel"><div class="row between small muted"><span>Your crew</span><span>Duel ›</span></div>' +
        rows + '<div class="row between small"><span>Points this week</span><span>' + pts + '</span></div></button>');
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
    var test = state.fitness[0];
    if (test) {
      var first = test.points[0].value, last = test.points[test.points.length - 1].value;
      tiles.push('<button class="card tap" data-act="tab" data-tab="progress">' + icon('progress', 22, 'var(--me)') +
        '<span class="small muted">' + h(test.name) + '</span><span class="display h2">' + (test.points.length > 1 ? first + ' → ' + last : last) + ' ' + h(test.unit) + '</span></button>');
    }
    if (tiles.length) parts.push('<div class="grid2">' + tiles.join('') + '</div>');
    return parts.join('');
  }

  // Challenges waiting for my answer and forfeits I owe (shown on Home and Duel).
  function answerCards() {
    var out = '';
    state.challenges.forEach(function (c) {
      if (!c.mine_to_answer) return;
      out += '<section class="card"><div class="small muted">' + h(c.from) + ' challenges you</div><div class="display h2">' + h(c.label) + '</div>' +
        '<div class="grid2"><button class="btn done" data-act="challenge-answer" data-id="' + c.id + '" data-accept="1">Accept</button>' +
        '<button class="btn" data-act="challenge-answer" data-id="' + c.id + '" data-accept="0">Chicken out</button></div></section>';
    });
    state.forfeits.forEach(function (f) {
      if (!f.mine_to_do) return;
      out += '<section class="card"><div class="small muted">Forfeit from ' + h(f.winner) + '</div><div class="display h2">' + h(f.task) + '</div>' +
        '<button class="btn done" data-act="forfeit-done" data-id="' + f.id + '">Done it</button></section>';
    });
    return out;
  }

  function todayScreen() {
    var t = state.today;
    var parts = [];
    parts.push('<header class="row"><div class="grow"><div class="display h1">Today' +
      (anime() ? ' <span class="jp me-c" style="font-size:17px">修行</span>' : '') + '</div>' +
      '<div class="small muted">' + h(niceDate(t.date)) + ' · ' + t.done_count + ' of ' + t.items.length + ' done</div></div>' +
      ring(56, 7, t.progress, me().color, '<span class="display" style="font-size:17px">' + pct(t.progress) + '</span>') + '</header>');

    if (t.paused_until) {
      parts.push('<section class="card"><div class="display h2">Paused until ' + h(niceDate(t.paused_until)) + '</div>' +
        '<div class="small muted">No reminders, streak frozen. Resume from /pause in the bot.</div></section>');
      return parts.join('');
    }
    if (t.status === 'rest') {
      parts.push('<section class="card"><div class="display h2">Rest day</div><div class="small muted">Recovery is part of training.</div></section>');
      return parts.join('');
    }
    if (t.quick) parts.push('<div class="small muted">Quick workout: targets halved, still counts for your streak.</div>');
    if (t.comeback_pct) parts.push('<div class="small muted">Comeback day: targets at ' + t.comeback_pct + '% to ease back in.</div>');

    t.items.forEach(function (it) {
      var done = it.status === 'completed';
      parts.push('<section class="card ex' + (it.status === 'skipped' ? ' skipped' : '') + '">' +
        '<div class="row between"><b style="font-size:16px">' + h(it.name) + '</b>' +
        '<span class="num"><span style="color:' + (done ? 'var(--me)' : 'var(--text)') + '">' + it.done + '</span><small> / ' + it.target + ' ' + h(it.unit) + '</small></span></div>' +
        bar(it.target ? it.done / it.target : 1) +
        '<div class="grid4">' +
        '<button class="btn" data-act="delta" data-id="' + it.id + '" data-delta="-5" aria-label="Minus 5 ' + h(it.name) + '">−5</button>' +
        '<button class="btn" data-act="delta" data-id="' + it.id + '" data-delta="5" aria-label="Plus 5 ' + h(it.name) + '">+5</button>' +
        '<button class="btn" data-act="delta" data-id="' + it.id + '" data-delta="10" aria-label="Plus 10 ' + h(it.name) + '">+10</button>' +
        '<button class="btn' + (done ? ' done' : '') + '" data-act="item-done" data-id="' + it.id + '">' + (done ? 'Done ✓' : 'Done') + '</button>' +
        '</div></section>');
    });

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
    if (t.status === 'pending') parts.push('<button class="btn primary" data-act="complete-all">Complete all</button>');
    return parts.join('');
  }

  function faceoff(a, b) {
    var lines = '';
    for (var i = 0; i < 20; i++) lines += '<line x1="64" y1="0" x2="340" y2="0" transform="rotate(' + i * 18 + ')"/>';
    function fighter(m, mood) {
      return '<div class="fighter">' + mascot(m.color, m.me ? '#FF8A3D' : '#C8F135', mood, 92, m.name + '\'s fighter') +
        '<b>' + h(m.me ? 'You' : m.name) + ' <span class="badge" style="background:' + h(m.color) + '">' + rank(m) + '</span></b>' +
        '<span class="small muted" style="font-style:italic">' + h(title(m)) + '</span>' +
        '<div style="width:100%"><div class="row between small muted" style="font-size:11px"><span>HP</span><span>' + pct(m.progress) + '</span></div>' + bar(m.progress, m.color) + '</div></div>';
    }
    return '<section class="card faceoff" aria-label="Today\'s duel">' +
      '<svg class="lines" viewBox="0 0 350 250" preserveAspectRatio="xMidYMid slice" aria-hidden="true"><g transform="translate(175 104)" stroke="#2C313B" stroke-width="2">' + lines + '</g></svg>' +
      '<span class="kanji" aria-hidden="true">対決</span>' +
      '<div class="fighters">' + fighter(a, a.status === 'completed' ? 'cheer' : 'determined') + fighter(b, /late|😴|⏰/.test(b.wake || '') ? 'sleepy' : 'smirk') + '</div>' +
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
    if (anime()) {
      parts.push(faceoff(mine, rival));
    } else {
      parts.push('<section class="card"><div class="grid2">' + [mine, rival].map(function (m) {
        return '<div style="display:flex;flex-direction:column;align-items:center;gap:6px">' +
          ring(108, 11, m.progress, m.color, '<span class="display" style="font-size:30px">' + pct(m.progress) + '</span>') +
          '<b>' + h(m.me ? 'You' : m.name) + '</b><span class="small muted">' + m.done + '/' + m.total + ' · ' + m.reps + ' reps · ' + m.streak + ' days</span></div>';
      }).join('') + '</div></section>');
    }

    var total = state.points.reduce(function (s, p) { return s + p.points; }, 0) || 1;
    parts.push('<section class="card"><div class="row between small muted"><span>Points this week</span><span>Sunday 20:30</span></div>' +
      '<div class="row between display big">' + state.points.map(function (p) { return '<span style="color:' + h(p.color) + '">' + h(p.name) + ' ' + p.points + '</span>'; }).join('') + '</div>' +
      '<div style="display:flex;gap:3px;height:10px;border-radius:5px;overflow:hidden">' + state.points.map(function (p) {
        return '<i style="flex:' + Math.max(p.points, 1) / total + ';background:' + h(p.color) + '"></i>';
      }).join('') + '</div></section>');

    var wakes = state.crew.filter(function (m) { return m.wake; });
    if (wakes.length) {
      parts.push('<section class="card"><span class="small muted">Wake-up</span><div class="row between">' + wakes.map(function (m) {
        return '<span><b>' + h(m.me ? 'You' : m.name) + '</b> ' + h(m.wake) + '</span>';
      }).join('') + '</div></section>');
    }

    others().forEach(function (o) {
      parts.push('<div class="grid2"><button class="btn roast" data-act="roast" data-target="' + o.id + '">Roast ' + h(o.name) + '</button>' +
        '<button class="btn" style="height:50px" data-act="hype" data-target="' + o.id + '">Hype ' + h(o.name) + '</button></div>');
    });
    parts.push('<button class="btn outline" style="height:48px" data-act="open-challenge">Challenge ' + h(others().length === 1 ? rival.name : 'someone') + '</button>');

    parts.push(answerCards());
    state.challenges.forEach(function (c) {
      if (c.mine_to_answer) return;
      parts.push('<section class="card row"><div class="grow"><b>' + h(c.label) + '</b><div class="small muted">' + h(c.from) + ' → ' + h(c.to) + ' · ' + h(c.status) + '</div></div></section>');
    });
    state.forfeits.forEach(function (f) {
      if (f.mine_to_do || !f.task) return;
      parts.push('<section class="card dashed small muted">' + h(f.loser) + ' still owes: <b style="color:var(--text)">' + h(f.task) + '</b></section>');
    });
    return parts.join('');
  }

  function chart(test) {
    var pts = test.points, n = pts.length;
    var values = pts.map(function (p) { return p.value; });
    var lo = Math.min.apply(null, values), hi = Math.max.apply(null, values);
    var span = hi - lo || 1;
    var W = 326, top = 30, bottom = 140;
    var coords = pts.map(function (p, i) {
      var x = n === 1 ? W / 2 : 24 + i * ((W - 48) / (n - 1));
      var y = bottom - ((p.value - lo) / span) * (bottom - top);
      return { x: x, y: n === 1 ? (top + bottom) / 2 : y, p: p };
    });
    var month = function (m) { return new Date(m + '-15').toLocaleDateString(undefined, { month: 'short' }); };
    var svg = '<svg width="100%" viewBox="0 0 ' + W + ' 170" role="img" aria-label="' + h(test.name) + ': ' + h(values.join(', ')) + '">' +
      '<line x1="10" y1="140" x2="316" y2="140" stroke="#2A2E37"/>' +
      '<polyline points="' + coords.map(function (c) { return c.x.toFixed(0) + ',' + c.y.toFixed(0); }).join(' ') + '" fill="none" stroke="var(--me)" stroke-width="3" stroke-linejoin="round" stroke-linecap="round"/>' +
      coords.map(function (c, i) {
        var last = i === n - 1;
        return '<circle cx="' + c.x.toFixed(0) + '" cy="' + c.y.toFixed(0) + '" r="' + (last ? 7 : 5) + '" fill="' + (last ? 'var(--me)' : '#0F1115') + '" stroke="var(--me)" stroke-width="3"/>' +
          '<text x="' + c.x.toFixed(0) + '" y="' + (c.y - 12).toFixed(0) + '" fill="' + (last ? '#F2F2EE' : '#A3A8B3') + '" font-size="12" text-anchor="middle">' + c.p.value + '</text>' +
          '<text x="' + c.x.toFixed(0) + '" y="162" fill="#A3A8B3" font-size="12" text-anchor="middle">' + h(month(c.p.month)) + '</text>';
      }).join('') + '</svg>';
    var change = n > 1 && values[0] ? Math.round((values[n - 1] - values[0]) / values[0] * 100) : null;
    return '<section class="card"><div class="row between"><span class="small muted">' +
      (anime() ? '<span class="jp" style="color:var(--text)">戦闘力</span> · ' : '') + h(test.name) + '</span>' +
      (change !== null ? '<span class="display h2 me-c">' + (change >= 0 ? '+' : '') + change + '%</span>' : '') + '</div>' + svg + '</section>';
  }

  function progressScreen() {
    var parts = ['<header class="display h1">Progress</header>'];
    if (state.fitness.length) state.fitness.forEach(function (t) { parts.push(chart(t)); });
    else parts.push('<section class="card dashed small muted">Take your first monthly fitness test with <b>/test</b> in the bot. It becomes your baseline.</section>');

    var done = state.last4weeks.filter(function (d) { return d.code === 'done'; }).length;
    var planned = state.last4weeks.filter(function (d) { return ['done', 'partial', 'missed'].indexOf(d.code) >= 0; }).length;
    parts.push('<section class="card"><div class="row between small muted"><span>Last 4 weeks</span><span>' + done + ' of ' + planned + ' days</span></div>' +
      '<div class="days">' + ['M', 'T', 'W', 'T', 'F', 'S', 'S'].map(function (d) { return '<span class="small muted">' + d + '</span>'; }).join('') +
      state.last4weeks.map(function (d) { return '<span class="day ' + h(d.code) + '" title="' + h(d.date + ': ' + d.code) + '"></span>'; }).join('') + '</div>' +
      '<div class="row small muted" style="flex-wrap:wrap;gap:12px"><span>Bright: done</span><span>Dim: partial</span><span>Blue: paused</span><span>Outline: missed</span></div></section>');

    if (state.records.length) {
      parts.push('<div class="grid2">' + state.records.map(function (r) {
        return '<div class="card"><span class="small muted">Best day · ' + h(r.name) + '</span><span class="display big">' + r.best + ' ' + h(r.unit) + '</span></div>';
      }).join('') + '</div>');
    }

    var labels = { workout: 'Workouts', full: 'Full targets', wake: 'On-time wake-ups', challenge: 'Challenges', test_pr: 'Test records', forfeit: 'Forfeits done' };
    var mp = state.my_points || {};
    var rows = Object.keys(labels).filter(function (k) { return mp[k]; }).map(function (k) {
      return '<div class="row between"><span>' + labels[k] + '</span><b>+' + mp[k] + '</b></div>';
    }).join('');
    parts.push('<section class="card"><div class="row between"><span class="small muted">Your points this week</span><span class="display h2 me-c">' + (mp.total || 0) + '</span></div>' +
      (rows || '<span class="small muted">Nothing yet this week.</span>') + '</section>');

    parts.push('<section class="card row between"><span>Anime style</span><button class="btn" data-act="toggle-anime" aria-pressed="' + anime() + '">' + (anime() ? 'On' : 'Off') + '</button></section>');
    return parts.join('');
  }

  function wakeOverlay() {
    var w = state.wake.today;
    return '<div class="scrim" style="align-items:stretch"><section class="sheet" style="border-radius:0;min-height:100%;justify-content:center;align-items:center;gap:20px;position:relative;overflow:hidden" aria-label="Wake-up check">' +
      (anime() ? '<span class="wake-kanji" aria-hidden="true">起きろ!</span><div style="position:relative">' + mascot(me().color, '#FF8A3D', 'sleepy', 120, 'Your mascot, half asleep') + '</div>'
               : '<div style="width:110px;height:110px;border-radius:55px;background:var(--surface-2);display:flex;align-items:center;justify-content:center">' + icon('sun', 60, 'var(--me)') + '</div>') +
      '<div style="text-align:center;position:relative"><div class="small muted" style="letter-spacing:1.5px">WAKE-UP CHECK · ' + h(state.wake.time) + '</div>' +
      '<div class="display" style="font-size:52px">Tap the ' + w.challenge + '</div><div class="muted">Prove you\'re actually awake</div></div>' +
      '<div class="grid2" style="width:100%;position:relative">' + w.choices.map(function (n) {
        return '<button class="btn display" style="height:92px;font-size:46px;border-radius:24px;background:var(--bg)" data-act="wake" data-choice="' + n + '">' + n + '</button>';
      }).join('') + '</div><button class="btn outline" style="position:relative" data-act="wake-later">Not now</button></section></div>';
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

  function burstPanel() {
    return '<div class="burst" data-act="close-burst" role="dialog" aria-label="Workout complete"><div class="panel">' +
      (anime() ? mascot(me().color, '#FF8A3D', 'cheer', 92, 'Your mascot celebrating') : '') +
      '<div><div class="' + (anime() ? 'kanji' : 'display h1') + '">' + (anime() ? '完了!' : 'Done!') + '</div>' +
      '<div class="display h2">TRAINING COMPLETE</div><div class="small">' +
      (others().length ? 'Your crew has been told.' : 'Streak: ' + state.streak + ' days.') + '</div></div></div></div>';
  }

  function nav() {
    var tabs = [['home', 'Home'], ['today', 'Today'], ['duel', 'Duel'], ['progress', 'Progress']];
    return '<nav class="nav" aria-label="Sections">' + tabs.map(function (t) {
      return '<button data-act="tab" data-tab="' + t[0] + '"' + (tab === t[0] ? ' aria-current="page"' : '') + '>' + icon(t[0]) + t[1] + '</button>';
    }).join('') + '</nav>';
  }

  function render() {
    if (!state) return;
    var screen = tab === 'today' ? todayScreen() : tab === 'duel' ? duelScreen() : tab === 'progress' ? progressScreen() : homeScreen();
    var demo = DEMO ? '<div class="demo">Demo with sample data. Open it from the bot in Telegram to see your real crew.</div>' : '';
    var overlay = '';
    var w = state.wake;
    if (w.enabled && w.today && w.today.status === 'pending' && !wakeDismissed) overlay = wakeOverlay();
    else if (burst) overlay = burstPanel();
    else if (sheet) overlay = challengeSheet();
    app.innerHTML = demo + screen + nav() + overlay;
  }

  // ---------------------------------------------------------------- data

  function api(path, body) {
    if (DEMO) return Promise.resolve(demoApi(path, body));
    return fetch(path, {
      method: body ? 'POST' : 'GET',
      headers: { 'Authorization': 'tma ' + initData, 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined
    }).then(function (res) {
      return res.json().then(function (data) {
        if (!res.ok) throw new Error(data.message || 'Request failed');
        return data;
      });
    });
  }

  function setState(next) {
    var wasDone = state && state.today.status === 'completed';
    state = next;
    if (!wasDone && state.today.status === 'completed' && tab === 'today') {
      burst = true;
      haptic('success');
      setTimeout(function () { burst = false; render(); }, 3500);
    }
    render();
  }

  function act(body) {
    if (busy) return;
    busy = true;
    haptic('light');
    api('/api/action', body).then(function (res) {
      if (res.message) toast(res.message);
      setState(res.state);
    }).catch(function (e) {
      haptic('error');
      toast(e.message);
    }).then(function () { busy = false; });
  }

  function load() {
    api('/api/state').then(setState).catch(function (e) {
      app.innerHTML = '<div class="boot">' + h(e.message) + '</div>';
    });
  }

  function startRest(secs) {
    clearInterval(restTimer);
    restLeft = secs;
    render();
    restTimer = setInterval(function () {
      restLeft -= 1;
      if (restLeft <= 0) {
        clearInterval(restTimer);
        restLeft = 0;
        haptic('success');
        toast('Rest over. Next set!');
      }
      if (tab === 'today') render();
    }, 1000);
  }

  // ---------------------------------------------------------------- events

  app.addEventListener('click', function (e) {
    var el = e.target.closest('[data-act]');
    if (!el) return;
    if (el.dataset.act === 'close-sheet' && e.target.closest('[data-stop]') && !e.target.closest('button')) return;
    var d = el.dataset;
    switch (d.act) {
      case 'tab': tab = d.tab; haptic('light'); render(); window.scrollTo(0, 0); break;
      case 'delta': act({ type: 'item_delta', item_id: +d.id, delta: +d.delta }); break;
      case 'item-done': act({ type: 'item_done', item_id: +d.id }); break;
      case 'complete-all': act({ type: 'complete_all' }); break;
      case 'quick': act({ type: 'quick', on: d.on === '1' }); break;
      case 'feedback': act({ type: 'feedback', value: d.value }); break;
      case 'roast': act({ type: 'roast', target: +d.target }); break;
      case 'hype': act({ type: 'hype', target: +d.target }); break;
      case 'wake': act({ type: 'wake_answer', choice: +d.choice }); break;
      case 'open-challenge': sheet = { kind: 'challenge' }; render(); break;
      case 'challenge-target': sheet.target = +d.target; render(); break;
      case 'challenge': sheet = null; act({ type: 'challenge', target: +d.target, kind: d.kind }); break;
      case 'challenge-answer': act({ type: 'challenge_answer', id: +d.id, accept: d.accept === '1' }); break;
      case 'forfeit-done': act({ type: 'forfeit_done', id: +d.id }); break;
      case 'toggle-anime': act({ type: 'settings', anime: !state.me.anime }); break;
      case 'rest': startRest(+d.secs); break;
      case 'close-sheet': sheet = null; render(); break;
      case 'close-burst': burst = false; render(); break;
      case 'wake-later': wakeDismissed = true; render(); break;
    }
  });

  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible' && state) load();
  });
  setInterval(function () { if (document.visibilityState === 'visible' && !busy && !DEMO) load(); }, 30000);

  if (tg) {
    tg.ready();
    tg.expand();
    try {
      if (tg.isVersionAtLeast && tg.isVersionAtLeast('6.1')) { tg.setHeaderColor('#0F1115'); tg.setBackgroundColor('#0F1115'); }
    } catch (e) { /* older Telegram */ }
  }

  // ---------------------------------------------------------------- demo

  var demoState = null;
  function demoApi(path, body) {
    if (!demoState) demoState = makeDemo();
    var s = demoState, message = null;
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
    }
    if (body) {
      switch (body.type) {
        case 'item_delta': var it = item(body.item_id); it.done = Math.max(0, it.done + body.delta); break;
        case 'item_done': item(body.item_id).done = item(body.item_id).target; break;
        case 'complete_all': s.today.items.forEach(function (i) { i.done = i.target; }); break;
        case 'quick': s.today.quick = body.on; break;
        case 'feedback': s.today.feedback = body.value; message = 'Thanks! Noted.'; break;
        case 'roast': message = 'Demo: roast delivered to Andrei 😈'; break;
        case 'hype': message = 'Demo: hype sent to Andrei 💪'; break;
        case 'wake_answer':
          if (body.choice !== s.wake.today.challenge) { message = 'Wrong number. Are you even awake?'; break; }
          s.wake.today = { status: 'on_time', answered_at: '06:31', minutes_late: 1, choices: [] };
          s.crew[0].wake = '☀️ 06:31 ✅'; message = 'On time! Good morning.'; break;
        case 'challenge': s.challenges.unshift({ id: 9, label: s.challenge_kinds.filter(function (k) { return k.key === body.kind; })[0].label, status: 'offered', from: 'Victor', to: 'Andrei', mine_to_answer: false }); message = 'Challenge sent!'; break;
        case 'forfeit_done': s.forfeits = []; message = 'Forfeit done. +5 pts'; break;
        case 'settings': s.me.anime = !!body.anime; break;
      }
      recompute();
      return { ok: true, message: message, state: JSON.parse(JSON.stringify(s)) };
    }
    return JSON.parse(JSON.stringify(s));
  }

  function makeDemo() {
    var today = new Date().toISOString().slice(0, 10);
    var days = function (codes) { return codes.map(function (c, i) { return { date: today, code: c }; }); };
    return {
      me: { id: 1, name: 'Victor', is_owner: true, color: '#C8F135', anime: true, roast_level: 'savage', feed: true },
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
        { id: 1, name: 'Victor', me: true, color: '#C8F135', status: 'pending', done: 1, total: 4, reps: 100, streak: 12, progress: 0.62, wake: '' },
        { id: 2, name: 'Andrei', me: false, color: '#FF8A3D', status: 'pending', done: 1, total: 4, reps: 30, streak: 4, progress: 0.25, wake: '☀️ 07:42 ⏰' }],
      crew_streak: 5,
      points: [{ id: 1, name: 'Victor', points: 85, color: '#C8F135' }, { id: 2, name: 'Andrei', points: 60, color: '#FF8A3D' }],
      my_points: { workout: 50, full: 20, wake: 15, challenge: 0, test_pr: 0, forfeit: 0, total: 85 },
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

  load();
})();
