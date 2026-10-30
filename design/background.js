(() => {
  const canvas = document.getElementById("bg");
  const ctx = canvas?.getContext("2d");
  if (!ctx) return;
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
  const SPACING = 48, LINK = 80, RADIUS = 240;
  let points = [], cols = 0, width = 0, height = 0, raf = 0, lastFrame = 0;
  let busy = false, focused = document.hasFocus(), lastPointer = -Infinity;
  const mouse = { x: 0, y: 0, glow: 0 };

  function build() {
    width = window.innerWidth;
    height = window.innerHeight;
    const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    canvas.style.width = width + "px";
    canvas.style.height = height + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    points = [];
    cols = Math.ceil((width + SPACING / 2) / SPACING);
    for (let y = SPACING / 2; y < height + SPACING; y += SPACING) {
      for (let col = 0; col < cols; col++) {
        points.push({ x: SPACING / 2 + col * SPACING + (Math.random() - 0.5) * 16,
                      y: y + (Math.random() - 0.5) * 16, near: 0 });
      }
    }
    start();
  }

  function draw(time) {
    raf = 0;
    if (document.hidden) return;
    const interactive = !busy && focused && !reduced.matches;
    const active = interactive && time - lastPointer < 500;
    if (interactive && time - lastFrame < 1000 / 30) {
      raf = requestAnimationFrame(draw);
      return;
    }
    lastFrame = time;
    mouse.glow = interactive ? mouse.glow + ((active ? 1 : 0) - mouse.glow) * 0.2 : 0;
    if (mouse.glow < 0.01) mouse.glow = 0;
    ctx.clearRect(0, 0, width, height);
    ctx.lineWidth = 1;
    ctx.strokeStyle = "rgba(255,255,255,0.02)";
    ctx.beginPath();
    const cells = new Map();
    for (let i = 0; i < points.length; i++) {
      const point = points[i];
      point.near = mouse.glow ? Math.max(0, 1 - Math.hypot(point.x - mouse.x, point.y - mouse.y) / RADIUS) * mouse.glow : 0;
      for (const j of [(i + 1) % cols ? i + 1 : -1, i + cols]) {
        const other = points[j];
        if (other && Math.hypot(point.x - other.x, point.y - other.y) < 56) {
          ctx.moveTo(point.x, point.y);
          ctx.lineTo(other.x, other.y);
        }
      }
      if (point.near > 0.02) {
        const key = `${Math.floor(point.x / LINK)},${Math.floor(point.y / LINK)}`;
        if (!cells.has(key)) cells.set(key, []);
        cells.get(key).push(i);
      }
    }
    ctx.stroke();
    // Проверяем только соседние ячейки, а не все пары точек на экране.
    for (const indices of cells.values()) for (const i of indices) {
      const a = points[i], cx = Math.floor(a.x / LINK), cy = Math.floor(a.y / LINK);
      for (let x = cx - 1; x <= cx + 1; x++) for (let y = cy - 1; y <= cy + 1; y++) {
        for (const j of cells.get(`${x},${y}`) || []) {
          if (j <= i) continue;
          const b = points[j], distance = Math.hypot(a.x - b.x, a.y - b.y);
          if (distance >= LINK) continue;
          ctx.strokeStyle = `rgba(96,165,250,${Math.min(a.near, b.near) * (1 - distance / LINK) * 0.7})`;
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.stroke();
        }
      }
    }
    for (const point of points) {
      ctx.fillStyle = point.near > 0.02 ? `rgba(147,197,253,${0.16 + point.near * 0.8})` : "rgba(255,255,255,0.16)";
      ctx.beginPath();
      ctx.arc(point.x, point.y, 1.2 + point.near * 1.4, 0, Math.PI * 2);
      ctx.fill();
    }
    if (interactive && (active || mouse.glow)) raf = requestAnimationFrame(draw);
  }

  function start() {
    if (!raf && !document.hidden) raf = requestAnimationFrame(draw);
  }
  window.addEventListener("pointermove", (event) => {
    if (busy || reduced.matches || !focused) return;
    mouse.x = event.clientX;
    mouse.y = event.clientY;
    lastPointer = performance.now();
    start();
  }, { passive: true });
  window.addEventListener("blur", () => { focused = false; start(); });
  window.addEventListener("focus", () => { focused = true; start(); });
  window.addEventListener("processingstate", (event) => { busy = event.detail; start(); });
  reduced.addEventListener("change", start);
  document.addEventListener("visibilitychange", () => {
    cancelAnimationFrame(raf);
    raf = 0;
    start();
  });
  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(build, 150);
  });
  build();
})();
