/* Orthographic network illustration. The bundled surface uses public-domain
   Natural Earth land geometry (https://www.naturalearthdata.com/about/terms-of-use/).
   The camera parameters here match the baked surface. Routes are illustrative. */
(() => {
  'use strict';
  const canvas = document.getElementById('signal-atlas');
  if (!canvas) return;
  const surfaceUrl = new URL('images/signal-earth.webp', document.currentScript.src);
  const ctx = canvas.getContext('2d');
  const button = document.getElementById('atlas-motion');
  if (!ctx) {
    canvas.style.background = `url("${surfaceUrl.href}") center / contain no-repeat`;
    if (button) button.hidden = true;
    return;
  }

  const radians = angle => angle * Math.PI / 180;
  const pitch = radians(20);
  const roll = radians(-12);
  const tau = 2 * Math.PI;
  const stops = {
    mumbai: [19.076, 72.878], london: [51.507, -.128], doha: [25.285, 51.531],
    nairobi: [-1.286, 36.817], singapore: [1.352, 103.82], tokyo: [35.676, 139.65],
    bengaluru: [12.972, 77.595], delhi: [28.614, 77.209],
  };
  const routes = [
    ['mumbai', 'london', .23, true], ['mumbai', 'nairobi', .16, false],
    ['mumbai', 'singapore', .19, true], ['delhi', 'tokyo', .21, false],
    ['doha', 'bengaluru', .14, false], ['singapore', 'tokyo', .18, false],
  ];
  const surface = new Image();
  const still = document.createElement('canvas');
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const stage = canvas.closest('.atlas-stage') || canvas;
  let width = 0, height = 0, radius = 0, pixelRatio = 1;
  let visible = true, frameId = 0, timer = 0, previousFrame = null, elapsed = 1.7;
  let motionPreference = null, pointerX = 0, pointerY = 0, driftX = 0, driftY = 0;
  try { motionPreference = localStorage.getItem('onejourney-atlas-motion'); } catch {}

  function position([latitude, longitude]) {
    const lat = radians(latitude), lon = radians(longitude - 60);
    const east = Math.cos(lat) * Math.sin(lon);
    const forward = Math.cos(lat) * Math.cos(lon);
    const north = Math.sin(lat) * Math.cos(pitch) - forward * Math.sin(pitch);
    return [
      east * Math.cos(roll) - north * Math.sin(roll),
      -(east * Math.sin(roll) + north * Math.cos(roll)),
      Math.sin(lat) * Math.sin(pitch) + forward * Math.cos(pitch),
    ];
  }

  function aboveHorizon([x, y, z]) {
    return z >= 0 || x*x + y*y >= 1;
  }

  const nodes = Object.fromEntries(Object.entries(stops).map(([name, coords]) => [name, position(coords)]));
  const arcs = routes.map(([from, to, elevation, primary]) => {
    const a = nodes[from], b = nodes[to];
    const angle = Math.acos(Math.max(-1, Math.min(1, a.reduce((dot, axis, i) => dot + axis*b[i], 0))));
    const points = Array.from({length: 89}, (_, i) => {
      const t = i / 88;
      const scale = (1 + Math.sin(t * Math.PI) * elevation) / Math.sin(angle);
      return a.map((axis, j) => (axis * Math.sin((1-t)*angle) + b[j] * Math.sin(t*angle)) * scale);
    });
    return {points, primary};
  });

  function orbit(slope, inclination, extent) {
    const tilt = radians(slope), incline = radians(inclination);
    return Array.from({length: 241}, (_, i) => {
      const t = i * tau / 240;
      const x = Math.cos(t) * extent, y = Math.sin(t) * extent * Math.cos(incline);
      return [x * Math.cos(tilt) - y * Math.sin(tilt), x * Math.sin(tilt) + y * Math.cos(tilt), Math.sin(t)*extent*Math.sin(incline)];
    });
  }
  const rings = [orbit(-25, 69, 1.20), orbit(42, 75, 1.11)];

  function trace(context, points, lineWidth, colour) {
    context.beginPath();
    let penDown = false;
    for (const point of points) {
      if (aboveHorizon(point)) {
        const x = point[0] * radius, y = point[1] * radius;
        if (penDown) context.lineTo(x, y);
        else context.moveTo(x, y);
        penDown = true;
      } else penDown = false;
    }
    context.lineWidth = lineWidth;
    context.strokeStyle = colour;
    context.stroke();
  }

  function light(context, x, y, reach, colour, strength) {
    const glow = context.createRadialGradient(x, y, 0, x, y, reach);
    glow.addColorStop(0, `rgba(${colour},${strength})`);
    glow.addColorStop(.22, `rgba(${colour},${strength * .48})`);
    glow.addColorStop(.58, `rgba(${colour},${strength * .12})`);
    glow.addColorStop(1, `rgba(${colour},0)`);
    context.fillStyle = glow;
    context.fillRect(x-reach, y-reach, reach*2, reach*2);
  }

  function dot(context, x, y, size, colour) {
    context.fillStyle = colour;
    context.beginPath();
    context.arc(x, y, size, 0, tau);
    context.fill();
  }

  function bakeStill() {
    if (!radius) return;
    still.width = canvas.width;
    still.height = canvas.height;
    const base = still.getContext('2d');
    base.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    base.translate(width / 2, height / 2);
    light(base, -radius*.56, radius*.13, radius*.85, '235,66,36', .19);
    light(base, radius*.35, -radius*.55, radius*.80, '115,142,153', .12);
    if (surface.naturalWidth) {
      const imageRadius = radius * 480 / 478;
      base.drawImage(surface, -imageRadius, -imageRadius, imageRadius*2, imageRadius*2);
    } else {
      const shade = base.createRadialGradient(-radius*.38, -radius*.43, 0, 0, 0, radius);
      shade.addColorStop(0, '#45525a');
      shade.addColorStop(.67, '#1c252b');
      shade.addColorStop(1, '#0d151b');
      dot(base, 0, 0, radius, shade);
    }
    trace(base, rings[0], .85, 'rgba(248,113,76,.6)');
    trace(base, rings[1], .6, 'rgba(201,206,206,.31)');
    for (const {points, primary} of arcs) {
      trace(base, points, primary ? 5 : 2.5, primary ? 'rgba(255,79,42,.15)' : 'rgba(255,86,48,.08)');
      trace(base, points, primary ? 1.5 : .8, primary ? 'rgba(255,111,78,.96)' : 'rgba(255,124,88,.66)');
    }
    for (const [city, point] of Object.entries(nodes)) {
      if (point[2] <= 0) continue;
      const x = radius * point[0], y = radius * point[1], hub = city === 'mumbai';
      light(base, x, y, hub ? 18 : 9, '255,99,58', hub ? .92 : .58);
      dot(base, x, y, hub ? 3 : 1.8, '#ffe4cd');
    }
  }

  function drawPacket(points, phase, primary) {
    const index = phase * (points.length-1), lo = Math.floor(index);
    const a = points[lo], b = points[Math.min(lo+1, points.length-1)];
    const point = a.map((axis, j) => axis + (b[j]-axis)*(index-lo));
    if (!aboveHorizon(point)) return;
    const x = radius*point[0], y = radius*point[1];
    const fade = Math.min(1, phase*16, (1-phase)*16);
    ctx.globalAlpha = fade;
    light(ctx, x, y, primary ? 8 : 5, '255,121,74', primary ? .78 : .5);
    dot(ctx, x, y, primary ? 1.7 : 1.1, '#fff2e5');
    ctx.globalAlpha = 1;
  }

  function paused() {
    return motionPreference ? motionPreference === 'paused' : reducedMotion.matches;
  }
  function draw() {
    if (!radius) return;
    const stillFrame = paused();
    ctx.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.save();
    driftX += ((stillFrame ? 0 : pointerX) - driftX) * .1;
    driftY += ((stillFrame ? 0 : pointerY) - driftY) * .1;
    if (stillFrame) driftX = driftY = 0;
    ctx.translate(width/2 + driftX * 4, height/2 + driftY * 3);
    ctx.rotate(driftX * .011);
    ctx.drawImage(still, -width/2, -height/2, width, height);
    arcs.forEach(({points, primary}, i) => {
      drawPacket(points, (elapsed / (4.6+i*.61) + i*.217) % 1, primary);
    });
    drawPacket(rings[0], (elapsed/14+.34)%1, true);
    const hub = nodes.mumbai;
    const pulse = (elapsed/2.9)%1;
    ctx.beginPath();
    ctx.arc(radius*hub[0], radius*hub[1], 4+13*pulse, 0, tau);
    ctx.strokeStyle = `rgba(255,161,116,${.59 * (1-pulse)})`;
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.restore();
  }

  function stop() {
    cancelAnimationFrame(frameId);
    clearTimeout(timer);
    frameId = timer = 0;
    previousFrame = null;
  }
  function nextFrame(now) {
    frameId = 0;
    if (!visible || document.hidden || paused() || !radius) return;
    if (previousFrame !== null) elapsed += Math.min(100, now-previousFrame) / 1000;
    previousFrame = now;
    draw();
    timer = setTimeout(() => { timer = 0; frameId = requestAnimationFrame(nextFrame); }, 32);
  }
  function refresh() {
    stop();
    if (!visible || document.hidden || !radius) return;
    if (paused()) draw();
    else frameId = requestAnimationFrame(nextFrame);
  }
  function resize() {
    const rect = canvas.getBoundingClientRect();
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    if (rect.width === width && rect.height === height && ratio === pixelRatio) return;
    width = rect.width;
    height = rect.height;
    radius = Math.min(width*.39, height*.424);
    pixelRatio = ratio;
    canvas.width = Math.max(1, Math.round(width * ratio));
    canvas.height = Math.max(1, Math.round(height * ratio));
    bakeStill();
    refresh();
  }
  function syncMotionControl() {
    const label = button?.querySelector('[data-motion-label]');
    if (label) label.textContent = paused() ? 'Play motion' : 'Pause motion';
    button?.setAttribute('aria-pressed', String(paused()));
    refresh();
  }
  button?.addEventListener('click', () => {
    motionPreference = paused() ? 'playing' : 'paused';
    try { localStorage.setItem('onejourney-atlas-motion', motionPreference); } catch {}
    syncMotionControl();
  });
  reducedMotion.addEventListener('change', syncMotionControl);
  document.addEventListener('visibilitychange', refresh);
  stage.addEventListener('pointermove', event => {
    if (event.pointerType === 'touch' || paused()) return;
    const rect = stage.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    pointerX = Math.max(-1, Math.min(1, (event.clientX-rect.left)/rect.width*2-1));
    pointerY = Math.max(-1, Math.min(1, (event.clientY-rect.top)/rect.height*2-1));
  });
  stage.addEventListener('pointerleave', () => { pointerX = pointerY = 0; });
  new ResizeObserver(resize).observe(canvas);
  new IntersectionObserver(([entry]) => { visible = entry.isIntersecting; refresh(); }).observe(canvas);
  surface.onload = () => { bakeStill(); refresh(); };
  resize();
  syncMotionControl();
  surface.src = surfaceUrl.href;
})();
