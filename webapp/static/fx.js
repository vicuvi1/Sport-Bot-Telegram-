/* Effects for the Workout Crew app: sounds, floating numbers, confetti, hits,
 * animated exercise demos and the report-card image. No dependencies.
 * Everything respects "reduce motion" and the in-app Sounds switch.
 */
(function () {
  'use strict';

  var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ---------------------------------------------------------------- sound

  var ctx = null;
  function soundOn() {
    try { return localStorage.getItem('sound') !== '0'; } catch (e) { return true; }
  }
  function setSound(on) {
    try { localStorage.setItem('sound', on ? '1' : '0'); } catch (e) { /* private mode */ }
  }
  function tone(freq, dur, type, gain, delay) {
    if (!soundOn()) return;
    try {
      ctx = ctx || new (window.AudioContext || window.webkitAudioContext)();
      if (ctx.state === 'suspended') ctx.resume();
      var t = ctx.currentTime + (delay || 0);
      var osc = ctx.createOscillator(), g = ctx.createGain();
      osc.type = type || 'sine';
      osc.frequency.setValueAtTime(freq, t);
      g.gain.setValueAtTime(gain || 0.12, t);
      g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
      osc.connect(g);
      g.connect(ctx.destination);
      osc.start(t);
      osc.stop(t + dur + 0.02);
    } catch (e) { /* no audio */ }
  }
  var sounds = {
    tap: function (combo) { tone(420 + Math.min(combo || 1, 10) * 55, 0.08, 'triangle', 0.11); },
    done: function () { tone(660, 0.09, 'square', 0.06); tone(990, 0.15, 'square', 0.06, 0.08); },
    win: function () { [523, 659, 784, 1046].forEach(function (f, i) { tone(f, 0.24, 'triangle', 0.13, i * 0.11); }); },
    hit: function () { tone(150, 0.22, 'sawtooth', 0.14); tone(80, 0.3, 'square', 0.08, 0.04); },
    hype: function () { tone(520, 0.12, 'sine', 0.1); tone(780, 0.2, 'sine', 0.1, 0.1); },
    beep: function (last) { tone(last ? 1175 : 784, last ? 0.4 : 0.12, 'sine', 0.18); },
    msg: function () { tone(880, 0.06, 'sine', 0.08); tone(1320, 0.08, 'sine', 0.08, 0.06); },
    alert: function () { tone(330, 0.12, 'square', 0.08); tone(247, 0.2, 'square', 0.08, 0.12); }
  };

  // ---------------------------------------------------------------- visual bits

  function center(target) {
    var r = target && target.getBoundingClientRect ? target.getBoundingClientRect() : null;
    return r ? { x: r.left + r.width / 2, y: r.top + r.height / 2 } : { x: window.innerWidth / 2, y: window.innerHeight / 2 };
  }

  // Big number that pops out of a button and floats away.
  function floatText(target, text, color, size) {
    var c = center(target);
    var el = document.createElement('div');
    el.className = 'float-text';
    el.textContent = text;
    el.style.color = color || 'var(--me)';
    el.style.fontSize = (size || 30) + 'px';
    el.style.left = c.x + 'px';
    el.style.top = (c.y - 14) + 'px';
    document.body.appendChild(el);
    setTimeout(function () { el.remove(); }, 1000);
  }

  function restart(el, cls, ms) {
    if (!el || reduced) return;
    el.classList.remove(cls);
    void el.getBoundingClientRect();
    el.classList.add(cls);
    setTimeout(function () { el.classList.remove(cls); }, ms || 600);
  }
  function shake(el) { restart(el || document.getElementById('app'), 'shake', 500); }
  function pop(el) { restart(el, 'pop', 450); }

  // Plays a reaction on every mascot marked as yours: jump | flex | power
  function mascotReact(kind) {
    document.querySelectorAll('.mascot.mine').forEach(function (m) { restart(m, 'm-' + kind, kind === 'power' ? 1600 : 650); });
  }

  // Something flies from one element to another (roast fireball, hype spark).
  function projectile(from, to, emoji, onHit) {
    if (!from || !to) { if (onHit) onHit(); return; }
    var a = center(from), b = center(to);
    var el = document.createElement('div');
    el.className = 'projectile';
    el.textContent = emoji;
    el.style.left = a.x + 'px';
    el.style.top = a.y + 'px';
    document.body.appendChild(el);
    if (reduced || !el.animate) { el.remove(); if (onHit) onHit(); return; }
    var dx = b.x - a.x, dy = b.y - a.y;
    var anim = el.animate([
      { transform: 'translate(-50%,-50%) scale(.6) rotate(0deg)' },
      { transform: 'translate(calc(-50% + ' + dx / 2 + 'px), calc(-50% + ' + (dy / 2 - 50) + 'px)) scale(1.4) rotate(200deg)', offset: 0.5 },
      { transform: 'translate(calc(-50% + ' + dx + 'px), calc(-50% + ' + dy + 'px)) scale(1) rotate(400deg)' }
    ], { duration: 520, easing: 'cubic-bezier(.4,0,.6,1)' });
    anim.onfinish = function () {
      el.remove();
      var boom = document.createElement('div');
      boom.className = 'boom';
      boom.style.left = b.x + 'px';
      boom.style.top = b.y + 'px';
      document.body.appendChild(boom);
      setTimeout(function () { boom.remove(); }, 500);
      if (onHit) onHit();
    };
  }

  function hit(el) { restart(el, 'hit', 500); }

  function confetti(ms) {
    if (reduced) return;
    var canvas = document.createElement('canvas');
    canvas.className = 'confetti';
    var dpr = window.devicePixelRatio || 1;
    canvas.width = window.innerWidth * dpr;
    canvas.height = window.innerHeight * dpr;
    document.body.appendChild(canvas);
    var g = canvas.getContext('2d');
    g.scale(dpr, dpr);
    var colors = ['#C8F135', '#FF8A3D', '#7FB2FF', '#F06BC6', '#FFD23F', '#F2F2EE'];
    var parts = [];
    for (var i = 0; i < 140; i++) {
      parts.push({
        x: window.innerWidth / 2 + (Math.random() - 0.5) * 80, y: window.innerHeight * 0.35,
        vx: (Math.random() - 0.5) * 13, vy: -Math.random() * 13 - 4,
        w: 6 + Math.random() * 6, h: 8 + Math.random() * 8, r: Math.random() * 6, vr: (Math.random() - 0.5) * 0.4,
        c: colors[i % colors.length]
      });
    }
    var start = performance.now(), total = ms || 2400;
    (function frame(now) {
      var t = now - start;
      g.clearRect(0, 0, window.innerWidth, window.innerHeight);
      parts.forEach(function (p) {
        p.vy += 0.38; p.vx *= 0.99; p.x += p.vx; p.y += p.vy; p.r += p.vr;
        g.save();
        g.globalAlpha = Math.max(0, 1 - t / total);
        g.translate(p.x, p.y);
        g.rotate(p.r);
        g.fillStyle = p.c;
        g.fillRect(-p.w / 2, -p.h / 2, p.w, p.h);
        g.restore();
      });
      if (t < total) requestAnimationFrame(frame); else canvas.remove();
    })(start);
  }

  // ---------------------------------------------------------------- exercise demos
  // A simple stick figure doing the move, animated between two poses (SMIL works
  // in Telegram's in-app browsers on Android, iOS and desktop).

  var POSES = {
    push: { dur: 1.6, ground: true,
      a: { head: [168, 82], lines: [[[38, 120], [74, 112], [106, 104], [150, 92]], [[150, 92], [154, 107], [156, 121]]] },
      b: { head: [168, 102], lines: [[[38, 120], [74, 117], [106, 113], [150, 109]], [[150, 109], [136, 112], [156, 121]]] } },
    squat: { dur: 1.8, ground: true,
      a: { head: [100, 22], lines: [[[100, 124], [102, 97], [100, 70], [100, 36]], [[100, 40], [116, 52], [132, 52]]] },
      b: { head: [116, 46], lines: [[[100, 124], [126, 101], [94, 92], [110, 58]], [[110, 60], [130, 66], [148, 62]]] } },
    situp: { dur: 1.8, ground: true,
      a: { head: [46, 116], lines: [[[160, 121], [138, 98], [112, 121], [62, 121]], [[62, 121], [56, 106], [48, 110]]] },
      b: { head: [122, 62], lines: [[[160, 121], [138, 98], [112, 121], [118, 76]], [[118, 78], [128, 88], [124, 72]]] } },
    plank: { dur: 2.6, ground: true,
      a: { head: [166, 89], lines: [[[38, 120], [74, 112], [110, 105], [150, 97]], [[150, 97], [150, 120], [174, 120]]] },
      b: { head: [166, 91], lines: [[[38, 120], [74, 113], [110, 108], [150, 99]], [[150, 99], [150, 120], [174, 120]]] } },
    pull: { dur: 2, bar: true,
      a: { head: [100, 48], lines: [[[86, 14], [88, 40], [100, 58]], [[114, 14], [112, 40], [100, 58]], [[100, 58], [100, 96], [98, 128]]] },
      b: { head: [100, 22], lines: [[[86, 14], [80, 30], [100, 34]], [[114, 14], [120, 30], [100, 34]], [[100, 34], [100, 72], [98, 104]]] } },
    jack: { dur: 1, ground: true,
      a: { head: [100, 26], lines: [[[92, 124], [100, 82], [108, 124]], [[100, 82], [100, 40]], [[84, 82], [100, 44], [116, 82]]] },
      b: { head: [100, 26], lines: [[[72, 124], [100, 82], [128, 124]], [[100, 82], [100, 40]], [[70, 10], [100, 44], [130, 10]]] } }
  };

  function demoKind(name) {
    var n = String(name || '').toLowerCase();
    if (/push|dip/.test(n)) return 'push';
    if (/squat|lunge/.test(n)) return 'squat';
    if (/sit|crunch|core|leg raise/.test(n)) return 'situp';
    if (/plank|hold/.test(n)) return 'plank';
    if (/pull|chin/.test(n)) return 'pull';
    return 'jack';
  }

  function demo(name, size, hair) {
    var pose = POSES[demoKind(name)];
    var pts = function (line) { return line.map(function (p) { return p.join(','); }).join(' '); };
    var anim = function (attr, a, b) {
      return reduced ? '' : '<animate attributeName="' + attr + '" values="' + a + ';' + b + ';' + a + '" dur="' + pose.dur +
        's" repeatCount="indefinite" calcMode="spline" keyTimes="0;0.5;1" keySplines=".45 0 .55 1;.45 0 .55 1"/>';
    };
    var lines = pose.a.lines.map(function (line, i) {
      var a = pts(line), b = pts(pose.b.lines[i]);
      return '<polyline points="' + a + '" fill="none" stroke="#F2F2EE" stroke-width="8" stroke-linecap="round" stroke-linejoin="round">' +
        anim('points', a, b) + '</polyline>';
    }).join('');
    var head = '<circle cx="' + pose.a.head[0] + '" cy="' + pose.a.head[1] + '" r="12" fill="#FFD9B8" stroke="' + (hair || '#C8F135') + '" stroke-width="5">' +
      anim('cx', pose.a.head[0], pose.b.head[0]) + anim('cy', pose.a.head[1], pose.b.head[1]) + '</circle>';
    var extra = (pose.ground ? '<line x1="10" y1="129" x2="190" y2="129" stroke="#2A2E37" stroke-width="4" stroke-linecap="round"/>' : '') +
      (pose.bar ? '<line x1="60" y1="12" x2="140" y2="12" stroke="#A3A8B3" stroke-width="5" stroke-linecap="round"/>' : '');
    return '<svg class="demo-fig" width="' + size + '" height="' + Math.round(size * 0.7) + '" viewBox="0 0 200 140" role="img" aria-label="How to do ' +
      String(name).replace(/[<>"&]/g, '') + '">' + extra + lines + head + '</svg>';
  }

  // ---------------------------------------------------------------- report card image

  function svgImage(svg) {
    return new Promise(function (resolve) {
      var img = new Image();
      img.onload = function () { resolve(img); };
      img.onerror = function () { resolve(null); };
      img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
    });
  }

  // Draws the report card as a 1080x1350 PNG (Instagram/Telegram story friendly).
  function reportImage(card, mascotSvg, monthTitle) {
    var W = 1080, H = 1350;
    var canvas = document.createElement('canvas');
    canvas.width = W;
    canvas.height = H;
    var g = canvas.getContext('2d');
    var display = '"Barlow Condensed", "Arial Narrow", sans-serif', body = '"DM Sans", system-ui, sans-serif';
    return (document.fonts ? document.fonts.ready : Promise.resolve()).then(function () { return svgImage(mascotSvg); }).then(function (img) {
      g.fillStyle = '#0F1115';
      g.fillRect(0, 0, W, H);
      g.save();
      g.translate(W / 2, 380);
      g.strokeStyle = 'rgba(255,255,255,0.05)';
      g.lineWidth = 6;
      for (var i = 0; i < 36; i++) { g.rotate(Math.PI / 18); g.beginPath(); g.moveTo(160, 0); g.lineTo(1100, 0); g.stroke(); }
      g.restore();

      g.fillStyle = '#C8F135';
      g.font = '700 46px ' + display;
      g.textAlign = 'center';
      g.fillText(monthTitle.toUpperCase() + ' REPORT CARD', W / 2, 110);
      if (img) g.drawImage(img, W / 2 - 170, 150, 340, 340);
      g.fillStyle = '#F2F2EE';
      g.font = '700 92px ' + display;
      g.fillText(card.name, W / 2, 580);
      g.font = '500 36px ' + body;
      g.fillStyle = '#A3A8B3';
      g.fillText('Rank ' + card.rank + ' · ' + card.xp + ' XP' + (card.is_champion ? ' · 👑 Champion' : ''), W / 2, 636);

      var stats = [
        [card.workouts + '/' + card.planned, 'workouts'], [String(card.reps), 'reps'],
        [card.best_streak + ' d', 'best streak'], [String(card.points), 'points'],
        [card.weight_change === null ? '–' : (card.weight_change > 0 ? '+' : '') + card.weight_change + ' kg', 'weight'],
        [card.best_day ? String(card.best_day.reps) : '–', 'best day reps']
      ];
      stats.forEach(function (s, i) {
        var col = i % 2, row = Math.floor(i / 2);
        var x = 90 + col * 470, y = 700 + row * 190;
        g.fillStyle = '#191C22';
        roundRect(g, x, y, 430, 160, 34);
        g.fill();
        g.textAlign = 'left';
        g.fillStyle = i === 0 ? '#C8F135' : '#F2F2EE';
        g.font = '700 78px ' + display;
        g.fillText(s[0], x + 34, y + 92);
        g.fillStyle = '#A3A8B3';
        g.font = '500 30px ' + body;
        g.fillText(s[1], x + 34, y + 136);
      });
      g.textAlign = 'center';
      g.fillStyle = '#A3A8B3';
      g.font = '500 30px ' + body;
      g.fillText(card.champion ? '👑 ' + card.champion + ' won the month' : 'Workout Crew', W / 2, 1300);
      return canvas.toDataURL('image/png');
    });
  }

  function roundRect(g, x, y, w, h, r) {
    g.beginPath();
    g.moveTo(x + r, y);
    g.arcTo(x + w, y, x + w, y + h, r);
    g.arcTo(x + w, y + h, x, y + h, r);
    g.arcTo(x, y + h, x, y, r);
    g.arcTo(x, y, x + w, y, r);
    g.closePath();
  }

  window.FX = {
    reduced: reduced, soundOn: soundOn, setSound: setSound, sounds: sounds,
    floatText: floatText, shake: shake, pop: pop, hit: hit, mascotReact: mascotReact,
    projectile: projectile, confetti: confetti, demo: demo, demoKind: demoKind, reportImage: reportImage
  };
})();
