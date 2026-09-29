// The rewind window: plays back the underwater cam's kept video (../rewind.m3u8) by the clock.
// LiveCams (RewindForm) serves this folder, rewrites the playlist when asked, and keeps clips and pictures.
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const video = $('video'), bar = $('bar'), head = $('head'), hover = $('hover');
  const host = window.chrome && window.chrome.webview;
  const FRAME = 1 / 30;
  let hls = null;
  let frags = [];      // hls.js's pieces: .start and .duration (s, playback time), .programDateTime (ms, clock)
  let total = 0;       // seconds of video kept
  let dragging = false;
  let toastTimer = 0;

  const pad = n => String(n).padStart(2, '0');
  const hms = ms => { const d = new Date(ms); return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
  const hm = ms => { const d = new Date(ms); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
  const dayKey = ms => { const d = new Date(ms); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; };
  function dayName(ms) {
    const d = new Date(ms);
    if (d.toDateString() === new Date().toDateString()) return 'Today';
    if (d.toDateString() === new Date(Date.now() - 864e5).toDateString()) return 'Yesterday';
    return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
  }
  const send = message => host && host.postMessage(message);
  const clamp = t => Math.max(0, Math.min(t, Math.max(0, total - 0.2)));

  function toast(text) {
    const el = $('toast');
    el.textContent = text;
    el.style.display = 'block';
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.style.display = 'none'; }, 5000);
  }

  // --- playback time <-> clock ------------------------------------------------------------------
  function index(value, key) { // last piece whose key <= value
    let lo = 0, hi = frags.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (key(frags[mid]) <= value) lo = mid; else hi = mid - 1;
    }
    return lo;
  }
  function toClock(t) {
    if (!frags.length) return null;
    const f = frags[index(t, x => x.start)];
    return f.programDateTime + Math.max(0, Math.min(t - f.start, f.duration)) * 1000;
  }
  // The playback time of a clock time; one in a break (night, a game) goes to the video right after it.
  function toMedia(ms) {
    if (!frags.length) return 0;
    const i = index(ms, x => x.programDateTime), f = frags[i];
    if (ms < f.programDateTime) return f.start;
    if (ms <= f.programDateTime + f.duration * 1000) return f.start + (ms - f.programDateTime) / 1000;
    return i + 1 < frags.length ? frags[i + 1].start : f.start + f.duration - 0.5;
  }
  const inVideo = ms => {
    if (!frags.length) return false;
    const f = frags[index(ms, x => x.programDateTime)];
    return ms >= f.programDateTime && ms <= f.programDateTime + f.duration * 1000;
  };

  // --- loading ----------------------------------------------------------------------------------
  function load(atMs) {
    if (hls) hls.destroy();
    frags = [];
    total = 0;
    hls = new Hls({ autoStartLoad: false, maxBufferLength: 30, maxMaxBufferLength: 60, backBufferLength: 30 });
    hls.on(Hls.Events.LEVEL_LOADED, (_, data) => {
      frags = data.details.fragments.filter(f => f.programDateTime != null);
      total = data.details.totalduration;
      $('empty').style.display = frags.length ? 'none' : 'flex';
      if (!frags.length) return;
      const target = atMs != null ? toMedia(atMs) : Math.max(0, total - 30);
      hls.startLoad(target);
      video.currentTime = target;
      video.play().catch(() => {});
      describe();
      drawTimeline();
      if (atMs != null && !inVideo(atMs)) toast(`No video at ${hms(atMs)} (night, a game or a pause): showing what came next.`);
    });
    hls.on(Hls.Events.ERROR, (_, data) => {
      if (data.details === Hls.ErrorDetails.LEVEL_EMPTY_ERROR) { $('empty').style.display = 'flex'; return; }
      if (!data.fatal) return;
      if (data.type === Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError();
      else if (data.type === Hls.ErrorTypes.NETWORK_ERROR) hls.startLoad();
      else toast('Playback stopped: ' + data.details);
    });
    hls.loadSource('../rewind.m3u8?' + Date.now());
    hls.attachMedia(video);
  }

  function describe() {
    const first = frags[0].programDateTime, last = frags[frags.length - 1];
    const end = last.programDateTime + last.duration * 1000;
    const amount = total < 3600 ? `${Math.max(1, Math.round(total / 60))} min` : `${(total / 3600).toFixed(1)} h`;
    $('range').textContent = `Kept: ${dayName(first)} ${hm(first)} to ${dayName(end).toLowerCase()} ${hm(end)} · ` +
      `${amount} of video (night and games aren't kept)`;
    const days = [...new Set(frags.map(f => dayKey(f.programDateTime)))];
    const select = $('day');
    select.innerHTML = '';
    for (const d of days.reverse()) {
      const option = document.createElement('option');
      option.value = d;
      option.textContent = dayName(new Date(d + 'T12:00').getTime());
      select.appendChild(option);
    }
  }

  // --- the timeline -----------------------------------------------------------------------------
  function drawTimeline() {
    document.querySelectorAll('.gap, .tick').forEach(e => e.remove());
    if (!total) return;
    const width = bar.clientWidth;
    const addTick = (x, text, isDay) => {
      const tick = document.createElement('div');
      tick.className = 'tick' + (isDay ? ' day' : '');
      tick.style.left = x + 'px';
      tick.textContent = text;
      $('timeline').appendChild(tick);
    };
    for (let i = 1; i < frags.length; i++) { // breaks: night, games, reloads
      const prev = frags[i - 1];
      if (frags[i].programDateTime - (prev.programDateTime + prev.duration * 1000) > 5000) {
        const gap = document.createElement('div');
        gap.className = 'gap';
        gap.style.left = (frags[i].start / total * width - 1) + 'px';
        bar.appendChild(gap);
      }
    }
    const first = frags[0].programDateTime, last = frags[frags.length - 1];
    const end = last.programDateTime + last.duration * 1000;
    addTick(24, `${dayName(first)} ${hm(first)}`, true);
    let lastX = 24, lastDay = dayKey(first);
    const hour = new Date(first);
    hour.setMinutes(0, 0, 0);
    for (let h = hour.getTime() + 3600e3; h <= end; h += 3600e3) {
      if (!inVideo(h)) continue;
      const x = toMedia(h) / total * width, newDay = dayKey(h) !== lastDay;
      if (x - lastX < (newDay ? 90 : 46) || x > width - 20) continue;
      addTick(x, newDay ? `${dayName(h)} ${hm(h)}` : hm(h), newDay);
      lastX = x;
      lastDay = dayKey(h);
    }
  }
  window.addEventListener('resize', drawTimeline);

  const timeAt = event => {
    const r = bar.getBoundingClientRect();
    return clamp((event.clientX - r.left) / r.width * total);
  };
  bar.addEventListener('mousedown', e => { dragging = true; video.currentTime = timeAt(e); });
  window.addEventListener('mouseup', () => { dragging = false; });
  window.addEventListener('mousemove', e => {
    const r = bar.getBoundingClientRect();
    const over = e.clientY >= r.top - 12 && e.clientY <= r.bottom + 12 && e.clientX >= r.left && e.clientX <= r.right;
    if (dragging) video.currentTime = timeAt(e);
    if ((over || dragging) && total) {
      const t = timeAt(e);
      hover.textContent = `${dayName(toClock(t))} ${hms(toClock(t))}`;
      hover.style.left = (t / total * r.width) + 'px';
      hover.style.display = 'block';
    } else {
      hover.style.display = 'none';
    }
  });

  function frame() {
    if (total) {
      const width = bar.clientWidth, t = video.currentTime;
      head.style.left = (t / total * width) + 'px';
      const ms = toClock(t);
      $('clock').innerHTML = `${hms(ms)}<small>${dayName(ms)}</small>`;
      const b = video.buffered;
      for (let i = 0; i < b.length; i++) {
        if (b.start(i) <= t + 0.5 && b.end(i) >= t - 0.5) {
          $('buffered').style.left = (b.start(i) / total * width) + 'px';
          $('buffered').style.width = ((b.end(i) - b.start(i)) / total * width) + 'px';
        }
      }
    }
    $('play').textContent = video.paused ? 'Play' : 'Pause';
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  // --- controls ---------------------------------------------------------------------------------
  const skip = s => { video.currentTime = clamp(video.currentTime + s); };
  const togglePlay = () => (video.paused ? video.play().catch(() => {}) : video.pause());
  const step = direction => { video.pause(); skip(direction * FRAME); };
  const speeds = [...$('speed').options].map(o => Number(o.value));
  const setSpeed = rate => { video.playbackRate = rate; $('speed').value = String(rate); };

  $('back60').onclick = () => skip(-60);
  $('back10').onclick = () => skip(-10);
  $('fwd10').onclick = () => skip(10);
  $('fwd60').onclick = () => skip(60);
  $('play').onclick = togglePlay;
  $('frame').onclick = () => step(1);
  $('speed').onchange = e => setSpeed(Number(e.target.value));
  $('latest').onclick = () => send({ type: 'refresh' });
  const goTo = () => {
    const time = $('time').value;
    if (!time || !$('day').value) return;
    const target = new Date(`${$('day').value}T${time.length === 5 ? time + ':00' : time}`).getTime();
    video.currentTime = clamp(toMedia(target));
    if (!inVideo(target)) toast(`No video at ${hms(target)} (night, a game or a pause): showing what came next.`);
  };
  $('time').addEventListener('change', goTo);
  $('time').addEventListener('keydown', e => { if (e.key === 'Enter') goTo(); });
  $('day').onchange = goTo;
  $('keep').onclick = () => total && send({ type: 'keep', at: toClock(video.currentTime) });
  $('saved').onclick = () => send({ type: 'saved' });
  $('still').onclick = () => {
    if (!video.videoWidth) return;
    const canvas = document.createElement('canvas');
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext('2d').drawImage(video, 0, 0);
    send({ type: 'still', at: toClock(video.currentTime), data: canvas.toDataURL('image/jpeg', 0.95) });
  };
  // Playing past the end: fetch what was recorded since, and carry on.
  video.addEventListener('ended', () => send({ type: 'refresh', at: toClock(video.currentTime) }));

  document.addEventListener('keydown', e => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
    const i = speeds.indexOf(video.playbackRate);
    switch (e.key) {
      case ' ': togglePlay(); break;
      case 'ArrowLeft': skip(e.shiftKey ? -60 : -10); break;
      case 'ArrowRight': skip(e.shiftKey ? 60 : 10); break;
      case ',': step(-1); break;
      case '.': step(1); break;
      case '[': if (i > 0) setSpeed(speeds[i - 1]); break;
      case ']': if (i >= 0 && i < speeds.length - 1) setSpeed(speeds[i + 1]); break;
      default: return;
    }
    e.preventDefault();
  });

  if (host) {
    host.addEventListener('message', e => {
      const m = e.data;
      if (m.type === 'toast') toast(m.text);
      else if (m.type === 'reload') load(m.at == null ? null : m.at);
    });
  }

  const params = new URLSearchParams(location.search);
  load(params.has('at') ? Number(params.get('at')) : null);
})();
