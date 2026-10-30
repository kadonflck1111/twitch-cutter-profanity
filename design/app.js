(() => {
  const $ = (id) => document.getElementById(id);

  const MODELS = [
    { id: "tiny", sub: "Черновая проверка", speed: 5, acc: 1, mem: "1.1 GB" },
    { id: "base", sub: "Быстрые тесты", speed: 4, acc: 2, mem: "1.3 GB" },
    { id: "small", sub: "Баланс скорости и точности", speed: 4, acc: 4, mem: "2.4 GB", rec: true },
    { id: "medium", sub: "Сложная речь", speed: 2, acc: 4, mem: "5.1 GB" },
    { id: "large-v3", sub: "Максимальная точность", speed: 1, acc: 5, mem: "9.7 GB" }
  ];

  const STAGES = [
    { id: "audio", label: "Извлечение звука", icon: "ph-speaker-high" },
    { id: "asr", label: "Распознавание речи", icon: "ph-microphone" },
    { id: "find", label: "Поиск фрагментов", icon: "ph-magnifying-glass" },
    { id: "render", label: "Сборка файла", icon: "ph-film-strip" }
  ];

  const state = {
    mode: "censor",
    model: "small",
    source: null,
    status: "ready",
    run: null,
    logOpen: false,
    log: [],
    words: [],
    error: "",
    opts: {
      whitelist: true, soft: true, rude: false,
      silence: true, donations: true, chat: true,
      xml: true, mp4: true,
      minPause: 1.5, pad: 0.25,
      saveNear: true, outputDir: "", keepQuality: true, openAfter: true
    }
  };

  let backend = null;
  let backendReady = false;
  let renderFrame = 0;
  let logId = 0;
  let renderedLogId = 0;
  let renderedBatchIndex = -1;
  const dirty = new Set();
  const dirtyRows = new Set();

  function scheduleRender(part) {
    dirty.add(part);
    if (renderFrame) return;
    renderFrame = requestAnimationFrame(() => {
      renderFrame = 0;
      if (dirty.has("runtime")) {
        renderRun();
        renderBatchProgress();
        renderSteps();
        renderActions();
      }
      if (dirty.has("log")) renderLog();
      dirty.clear();
    });
  }

  const COPY = {
    censor: {
      title: "Чистый звук без потери эмоций",
      desc: "Находим мат по словам и глушим только корень. Голос и интонация остаются.",
      flow: "Вырезать мат"
    },
    stream: {
      title: "Стрим без пауз и отвлечений",
      desc: "Убираем тишину, донаты и чтение чата. Готовый таймлайн открывается в DaVinci Resolve.",
      flow: "Нарезать стрим"
    }
  };

  /* Helpers */

  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const bars = (n) => Array.from({ length: 5 }, (_, i) => `<i class="${i < n ? "on" : ""}"></i>`).join("");
  const now = () => new Date().toLocaleTimeString("ru-RU", { hour12: false });
  const fmtTime = (s) => {
    s = Math.max(0, Math.round(s));
    const m = Math.floor(s / 60);
    return `${m}:${String(s % 60).padStart(2, "0")}`;
  };

  function toast(text, icon = "ph-info") {
    const el = $("toast");
    el.querySelector("i").className = `ph ${icon}`;
    $("toastText").textContent = text;
    el.classList.add("show");
    clearTimeout(toast.t);
    toast.t = setTimeout(() => el.classList.remove("show"), 2800);
  }

  function log(level, msg) {
    state.log.push({ id: ++logId, level: ["info", "ok", "warn", "err"].includes(level) ? level : "info", msg, time: now() });
    if (state.log.length > 500) state.log.splice(0, state.log.length - 500);
    scheduleRender("log");
  }

  function setStatus(status, text, desc) {
    state.status = status;
    document.body.dataset.status = status;
    $("statusText").textContent = text;
    $("statusDesc").textContent = desc;
    window.dispatchEvent(new CustomEvent("processingstate", { detail: ["loading", "processing", "stopping"].includes(status) }));
  }

  /* Source */

  function renderSource() {
    const el = $("sourceArea");
    const s = state.source;

    if (!s) {
      el.innerHTML = `
        <div class="drop" id="drop">
          <div class="drop-icon"><i class="ph ph-file-video"></i></div>
          <div>
            <h3>Выберите видео или папку</h3>
            <p>Поддерживаются MP4, MKV, AVI, MOV, WEBM и аудиофайлы</p>
            <small>Готовые файлы с суффиксами _clean и _нарезка в папке пропускаются</small>
          </div>
          <div class="drop-actions">
            <button class="btn" data-act="pick-file"><i class="ph ph-file-video"></i>Выбрать видео</button>
            <button class="btn" data-act="pick-folder"><i class="ph ph-folder"></i>Выбрать папку</button>
          </div>
        </div>`;
      bindDrop();
      return;
    }

    if (s.type === "file") {
      const f = s.file;
      el.innerHTML = `
        <div class="card file reveal">
          <div class="file-thumb"><i class="ph ph-film-slate"></i></div>
          <div class="file-body">
            <div class="file-name">${esc(f.name)}</div>
            <div class="file-path">${esc(f.path)}</div>
            <div class="tags">
              <span class="tag blue">${esc(f.ext)}</span><span class="tag">${esc(f.size)}</span>
              <span class="tag">${esc(f.duration)}</span><span class="tag">${esc(f.res)}</span>
            </div>
          </div>
          <div class="file-actions">
            <button class="btn btn-sm" data-act="pick-file"><i class="ph ph-arrows-clockwise"></i>Заменить</button>
            <button class="btn btn-sm btn-ghost" data-act="clear" aria-label="Очистить выбор" data-tip="Очистить выбор"><i class="ph ph-x"></i></button>
          </div>
        </div>`;
      return;
    }

    const rows = s.files.map((f, i) => {
      dirtyRows.add(i);
      return `<div class="batch-row" id="batch-${i}"><i class="ph ph-file-video"></i><span class="name">${esc(f.name)}</span><span class="num">${esc(f.size)}</span><span class="num">${esc(f.dur)}</span><span class="state"><span class="badge">В очереди</span></span></div>`;
    }).join("");

    el.innerHTML = `
      <div class="card batch reveal">
        <div class="batch-head">
          <div class="file-thumb" style="width:40px;height:40px;font-size:20px"><i class="ph ph-folders"></i></div>
          <div><strong>${esc(s.path)}</strong><br><span>${s.files.length} файлов</span></div>
          <div class="file-actions">
            <button class="btn btn-sm" data-act="pick-folder"><i class="ph ph-arrows-clockwise"></i>Заменить</button>
            <button class="btn btn-sm btn-ghost" data-act="clear" aria-label="Очистить выбор" data-tip="Очистить выбор"><i class="ph ph-x"></i></button>
          </div>
        </div>
        ${rows}
       </div>`;
    renderBatchProgress();
  }

  function renderBatchProgress() {
    const run = state.run;
    if (state.source?.type !== "batch") return;
    if (run) dirtyRows.add(run.batchIndex);
    if (renderedBatchIndex >= 0) dirtyRows.add(renderedBatchIndex);
    for (const i of dirtyRows) {
      const row = $("batch-" + i);
      if (!row) continue;
      const result = run?.results?.[i];
      const active = run?.active && run.batchIndex === i && !result;
      row.className = "batch-row" + (result ? result.success ? " is-done" : " is-error" : active ? " is-active" : "");
      row.firstElementChild.className = "ph " + (result ? result.success ? "ph-check-circle" : "ph-warning-circle" : active ? "ph-waveform" : "ph-file-video");
      const badge = row.querySelector(".badge");
      badge.className = "badge" + (result ? result.success ? " green" : " red" : active ? " blue" : "");
      badge.textContent = result ? result.success ? "Готово" : "Ошибка" : active ? `${Math.round((run.fileProgress || 0) * 100)}%` : "В очереди";
    }
    dirtyRows.clear();
    renderedBatchIndex = run?.batchIndex ?? -1;
  }

  function bindDrop() {
    const drop = $("drop");
    if (!drop) return;
    ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("is-over"); }));
    ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("is-over"); }));
    drop.addEventListener("drop", (e) => {
      if (state.run?.active) return;
      toast("Укажите файл в окне выбора", "ph-file-video");
      chooseFile();
    });
  }

  function adoptSource(source) {
    if (!source) return;
    state.source = source;
    resetRun();
    const label = source.type === "batch" ? `${source.files.length} файлов` : source.file.name;
    log("info", `Выбран источник ${label}`);
    renderAll();
  }

  async function chooseFile() {
    if (state.run?.active) return;
    if (!backendReady) return toast("Запустите приложение через Twitch Cutter Profanity.exe");
    try { adoptSource(await backend.choose_file()); } catch (error) { showError(error); }
  }

  async function chooseFolder() {
    if (state.run?.active) return;
    if (!backendReady) return toast("Запустите приложение через Twitch Cutter Profanity.exe");
    try { adoptSource(await backend.choose_folder()); } catch (error) { showError(error); }
  }

  /* Models */

  function renderModels() {
    $("models").innerHTML = MODELS.map((m) => `
      <button class="model" role="radio" aria-checked="${m.id === state.model}" data-model="${m.id}">
        <span class="model-name">${m.id}</span>
        ${m.rec ? `<span class="model-badge">Рекомендуем</span>` : ""}
        <span class="model-sub">${m.sub}</span>
        <span class="meter">
          <span>Скорость</span><span class="bars">${bars(m.speed)}</span>
          <span>Точность</span><span class="bars">${bars(m.acc)}</span>
          <span data-tip="Ориентир для GPU. Расход памяти зависит от размера пакета и записи">Память, ≈</span><b>${m.mem}</b>
        </span>
        <span class="model-radio" aria-hidden="true"></span>
      </button>`).join("");
  }

  /* Rules */

  function sw(key, label) {
    return `<label class="switch"><input type="checkbox" data-opt="${key}" ${state.opts[key] ? "checked" : ""} aria-label="${esc(label)}"><span class="track"></span><span class="knob"></span></label>`;
  }

  function setting(icon, title, desc, control, tip, extra = "") {
    return `
      <div class="setting ${extra ? "is-stacked" : ""}">
        <span class="setting-icon"><i class="ph ${icon}"></i></span>
        <div class="setting-text">
          <strong>${title}${tip ? `<button class="info" data-tip="${esc(tip)}" data-tip-wide aria-label="Подробнее"><i class="ph ph-info"></i></button>` : ""}</strong>
          <small>${desc}</small>
        </div>
        <div class="setting-control">${control}</div>
        ${extra ? `<div class="setting-full">${extra}</div>` : ""}
      </div>`;
  }

  function renderRules() {
    const o = state.opts;
    let html;
    if (state.mode === "censor") {
      const chips = state.words.map((w) => `<span class="tag">${esc(w)}</span>`).join("");
      html =
        setting("ph-shield-check", "Защита от ложных срабатываний", "Проверяет обычные слова по словарю исключений", sw("whitelist", "Защита от ложных срабатываний"), "Пользовательские стоп слова имеют приоритет над словарем исключений.") +
        setting("ph-speaker-simple-low", "Мягкое глушение", "Последние 0.10 сек слова остаются слышны, эмоция сохраняется", sw("soft", "Мягкое глушение"), "Глушится только корень слова. Окончание и интонация стримера остаются в записи.") +
        setting("ph-warning", "Дополнительная грубая лексика", "Добавляет к жесткому мату слова «сука», «говно» и похожие", sw("rude", "Дополнительная грубая лексика"), "По умолчанию выключено. Включите для расширенного словаря глушения.") +
        setting("ph-list-plus", "Пользовательские стоп слова", "Через запятую. Применяются вместе со встроенным словарем", "",
          "", `<input class="input" id="wordsInput" placeholder="например: блин, реклама, конкурент" value="${esc(state.words.join(", "))}" aria-label="Пользовательские стоп слова"><div id="wordsError"></div><div class="word-chips">${chips}</div>`);
    } else {
      html =
        setting("ph-waveform", "Удалять паузы и тишину", "Вырезает паузы длиннее порога. Короткие вдохи и «ну» остаются", sw("silence", "Удалять паузы и тишину"), "Порог задается ниже. Зазор оставляет немного воздуха вокруг склейки.") +
        setting("ph-hand-coins", "Удалять благодарности за донаты", "Реплики «спасибо за донат», «спасибо за сабку» и похожие", sw("donations", "Удалять благодарности за донаты")) +
        setting("ph-chat-centered-text", "Удалять чтение чата", "Фрагменты, где стример зачитывает сообщения зрителей", sw("chat", "Удалять чтение чата")) +
        setting("ph-timer", "Минимальная длительность паузы", "Паузы короче этого значения остаются в ролике",
          `<div class="range"><input type="range" min="0.5" max="5" step="0.1" value="${o.minPause}" data-range="minPause" aria-label="Минимальная пауза"><output id="out-minPause">${o.minPause.toFixed(1)} с</output></div>`) +
        setting("ph-arrows-out-line-horizontal", "Зазор вокруг склейки", "Сколько секунд оставить до и после вырезанного фрагмента",
          `<div class="range"><input type="range" min="0" max="1" step="0.05" value="${o.pad}" data-range="pad" aria-label="Зазор вокруг склейки"><output id="out-pad">${o.pad.toFixed(2)} с</output></div>`);
    }
    $("rulesArea").innerHTML = html;

    const input = $("wordsInput");
    if (input) {
      input.addEventListener("change", validateWords);
    }
  }

  function validateWords() {
    const input = $("wordsInput");
    if (!input) return true;
    const raw = input.value.split(",").map((word) => word.trim().toLowerCase()).filter(Boolean);
    const bad = raw.find((word) => word.length < 2 || /\s/.test(word) || !/\p{L}/u.test(word));
    input.classList.toggle("is-invalid", !!bad);
    $("wordsError").innerHTML = bad ? `<div class="field-error">Слово «${esc(bad)}» не подходит. Нужны хотя бы два символа, буквы и отсутствие пробелов.</div>` : "";
    if (bad) return false;
    state.words = [...new Set(raw)];
    $("rulesArea").querySelector(".word-chips").innerHTML = state.words.map((word) => `<span class="tag">${esc(word)}</span>`).join("");
    return true;
  }

  /* Export */

  function renderExport() {
    const o = state.opts;
    let formats = "";
    if (state.mode === "stream") {
      formats = `
        <div class="formats" style="margin-bottom:8px">
          <label class="card format lift"><input type="checkbox" data-opt="xml" ${o.xml ? "checked" : ""}><span class="format-icon"><i class="ph ph-brackets-angle"></i></span><span><strong>Таймлайн DaVinci Resolve</strong><small>XML со всеми склейками. Импорт через File, Import Timeline</small></span><span class="tick"><i class="ph ph-check"></i></span></label>
          <label class="card format lift"><input type="checkbox" data-opt="mp4" ${o.mp4 ? "checked" : ""}><span class="format-icon"><i class="ph ph-film-strip"></i></span><span><strong>Готовое видео MP4</strong><small>Точная склейка. Аппаратный рендер при поддержке NVENC</small></span><span class="tick"><i class="ph ph-check"></i></span></label>
        </div>
        <div id="formatError"></div>`;
    }
    $("exportArea").innerHTML = formats + `
      <div class="card settings">
        ${setting("ph-folder-simple", "Куда сохранить", o.saveNear ? "Рядом с исходным файлом, с суффиксом " + (state.mode === "censor" ? "_clean" : "_нарезка") : esc(o.outputDir),
          `<div class="choice" role="group" aria-label="Папка результата"><button aria-pressed="${o.saveNear}" data-save="near">Рядом с исходником</button><button aria-pressed="${!o.saveNear}" data-save="custom">Другая папка</button></div>`)}
        ${state.mode === "stream" ? setting("ph-sparkle", "Высокое качество видео", "При выключении размер файла уменьшается за счет более сильного сжатия", sw("keepQuality", "Высокое качество видео")) : setting("ph-sparkle", "Исходное качество видео", "Видеопоток копируется, перекодируется только звук", "")}
        ${setting("ph-folder-open", "Открыть папку после завершения", "Проводник откроется сразу после экспорта", sw("openAfter", "Открыть папку после завершения"))}
      </div>`;
  }

  /* Run area */

  function resetRun() {
    state.run = null;
    state.error = "";
    if (state.status !== "ready") setStatus("ready", "Готово", state.source ? "Можно запускать" : "Ожидает файл");
    document.body.classList.remove("is-locked");
  }

  function renderRun() {
    const el = $("runArea");
    const r = state.run;

    if (state.status === "error") {
      el.innerHTML = `
        <div class="alert" role="alert">
          <i class="ph-fill ph-warning-octagon"></i>
          <div>
            <strong>Обработка не завершена</strong>
            <p>${esc(state.error || "Подробности записаны в журнал обработки.")}</p>
          </div>
          <div class="alert-actions">
            <button class="btn btn-sm" data-act="go-model">Сменить модель</button>
            <button class="btn btn-sm" data-act="retry" style="background:var(--s4)"><i class="ph ph-arrow-clockwise"></i>Повторить</button>
          </div>
        </div>`;
      return;
    }

    if (["done", "partial"].includes(state.status) && r) {
      const formats = [...new Set((r.outputs || []).map((path) => path.split(".").pop().toUpperCase()))].join(", ") || "—";
      const stat2 = state.mode === "censor" ? ["Заглушено фрагментов", r.hits] : ["Создано файлов", r.outputs?.length || 0];
      el.innerHTML = `
        <div class="card result">
          <div class="result-head">
            <span class="result-icon"><i class="ph-fill ph-check-circle"></i></span>
            <div><h3>${r.failed ? "Завершено с ошибками" : "Обработка завершена"}</h3><p>${esc(r.failed ? `Ошибок: ${r.failed}. Подробности в журнале.` : `Результаты: ${r.output_dir || "рядом с исходником"}`)}</p></div>
          </div>
          <div class="stats">
            <div class="stat"><span>Обработано файлов</span><b>${r.succeeded} из ${r.total}</b></div>
            <div class="stat"><span>${stat2[0]}</span><b>${stat2[1]}</b></div>
            <div class="stat"><span>Форматы</span><b>${formats}</b></div>
            <div class="stat"><span>Время обработки</span><b>${fmtTime(r.elapsed || 0)}</b></div>
          </div>
          <div class="result-actions">
            <button class="btn" data-act="open" style="background:var(--green);border-color:var(--green);color:#000"><i class="ph ph-folder-open"></i>Открыть папку результата</button>
            <button class="btn" data-act="again"><i class="ph ph-plus"></i>Обработать еще один файл</button>
          </div>
        </div>`;
      return;
    }

    if (!r || state.status === "ready") { el.innerHTML = ""; return; }

    const stopping = state.status === "stopping";
    const loading = r.stage < 0;
    const isBatch = state.source.type === "batch";
    const file = (isBatch ? state.source.files[r.batchIndex] : state.source.file) || { name: "Подготовка" };
    const pct = Math.round(r.progress * 100);
    const title = loading ? "Загрузка модели " + state.model : file.name;
    const subtitle = stopping ? "Отменяем текущую операцию" : loading ? "При первом запуске модель скачивается" : `Файл ${r.batchIndex + 1} из ${isBatch ? state.source.files.length : 1} · ${STAGES[r.stage]?.label || "Завершение"}`;
    const existing = el.querySelector(".progress");
    if (existing && existing.dataset.loading === String(loading)) {
      existing.classList.toggle("is-stopping", stopping);
      existing.querySelector(".progress-title strong").textContent = title;
      existing.querySelector(".progress-title span").textContent = subtitle;
      existing.querySelector(".progress-pct").firstChild.textContent = pct;
      existing.querySelector(".bar-fill").style.transform = `scaleX(${r.progress})`;
      existing.querySelectorAll(".stage").forEach((stage, i) => {
        stage.classList.toggle("is-done", !loading && i < r.stage);
        stage.classList.toggle("is-active", !loading && i === r.stage);
      });
      if ($("runHits")) $("runHits").textContent = r.hits;
      return;
    }
    const stages = STAGES.map((s, i) => {
      const cls = loading ? "" : i < r.stage ? "is-done" : i === r.stage ? "is-active" : "";
      return `<div class="stage ${cls}"><i class="ph ${i < r.stage ? "ph-check-circle" : s.icon}"></i>${s.label}</div>`;
    }).join("");

    el.innerHTML = `
      <div class="card progress ${stopping ? "is-stopping" : ""}" data-loading="${loading}">
        <div class="progress-top">
          <div class="file-thumb"><i class="ph ${loading ? "ph-cpu" : "ph-film-slate"}"></i></div>
          <div class="progress-title">
            <strong>${esc(title)}</strong>
            <span>${esc(subtitle)}</span>
          </div>
          <div class="progress-pct">${pct}<small>%</small></div>
        </div>
        <div class="bar"><div class="bar-fill" style="transform:scaleX(${r.progress})"></div></div>
        <div class="stages">${stages}</div>
        <div class="stats">
          ${loading ? `
            <div class="stat"><span>Осталось</span><span class="skeleton w60"></span></div>
            <div class="stat"><span>Скорость</span><span class="skeleton w40"></span></div>
            <div class="stat"><span>Найдено</span><span class="skeleton w40"></span></div>
            <div class="stat"><span>Видеопамять</span><span class="skeleton w80"></span></div>` : `
            <div class="stat"><span>Осталось</span><b>—</b></div>
            <div class="stat"><span>Скорость</span><b>—</b></div>
            <div class="stat"><span>${state.mode === "censor" ? "Найдено мата" : "Обработано файлов"}</span><b id="runHits">${r.hits}</b></div>
            <div class="stat"><span>Модель</span><b>${esc(state.model)}</b></div>`}
        </div>
      </div>`;
  }

  /* Backend processing */

  async function startRun() {
    if (state.run?.active) return;
    if (!state.source) {
      $("runArea").innerHTML = `<div class="alert warn" role="alert"><i class="ph-fill ph-warning"></i><div><strong>Не выбран источник</strong><p>Выберите видео или папку, чтобы запустить обработку.</p></div><div class="alert-actions"><button class="btn btn-sm" data-act="pick-file">Выбрать видео</button></div></div>`;
      $("secSource").scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    if (state.mode === "stream" && !state.opts.xml && !state.opts.mp4) {
      $("formatError").innerHTML = `<div class="field-error" style="margin-bottom:8px"><i class="ph ph-warning-circle"></i>Выберите хотя бы один формат: DaVinci XML или MP4.</div>`;
      $("secExport").scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }

    if (!validateWords()) return;
    if (!backendReady || !state.source.path) return showError("Нет связи с Python. Запустите Twitch Cutter Profanity.exe.");
    if (!state.opts.saveNear && !state.opts.outputDir) return showError("Выберите папку для результатов.");
    if (state.source.type === "batch" && !state.source.files.length) return showError("В выбранной папке нет файлов для обработки.");
    state.error = "";
    state.run = { stage: -1, progress: 0, fileProgress: 0, batchIndex: 0, hits: 0, active: true, results: {}, startedAt: Date.now() };
    document.body.classList.add("is-locked");
    setStatus("loading", "Подготовка", "Запуск обработки");
    log("info", `Запуск обработки ${state.source.path}`);
    renderAll();
    $("runArea").scrollIntoView({ behavior: "smooth", block: "end" });
    try {
      const accepted = await backend.start_processing(state.mode, state.source.path, state.model, {
        ...state.opts, stopwords: state.words.join(", ")
      });
      if (!accepted) showError("Предыдущая задача еще завершается. Повторите запуск через несколько секунд.");
    } catch (error) { showError(error); }
  }

  async function stopRun() {
    if (!state.run?.active || state.status === "stopping") return;
    setStatus("stopping", "Остановка", "Отменяем текущую операцию");
    scheduleRender("runtime");
    try { await backend.stop_processing(); } catch (error) { showError(error); }
  }

  function showError(error) {
    state.error = error?.message || String(error);
    if (state.run) state.run.active = false;
    document.body.classList.remove("is-locked");
    setStatus("error", "Ошибка", "Подробности в журнале");
    log("err", state.error);
    scheduleRender("runtime");
  }

  function applyBackendMessage(message) {
    if (!message) return;
    if (message.type === "log") {
      log(message.level || "info", message.message || "");
      return;
    }
    if (message.type === "status") {
      if (state.status === "stopping" && message.status !== "stopping") return;
      setStatus(message.status || "processing", message.title || "Обработка", message.desc || "");
      if (state.run && message.status === "loading") state.run.stage = -1;
      scheduleRender("runtime");
      return;
    }
    if (message.type === "progress") {
      if (!state.run || !state.source) return;
      const total = message.total || 1;
      const current = message.file_index || 0;
      const progress = Number(message.progress || 0);
      state.run = {
        ...state.run,
        active: true,
        batchIndex: current,
        fileProgress: Math.max(0, Math.min(1, message.file_progress ?? (progress * total) - current)),
        progress: Math.max(0, Math.min(1, progress)),
        stage: Math.min(3, Number(message.stage || 0)),
        hits: Number(message.hits || 0),
      };
      scheduleRender("runtime");
      return;
    }
    if (message.type === "file_result" && state.run) {
      state.run.results[message.file_index] = message;
      dirtyRows.add(message.file_index);
      scheduleRender("runtime");
      return;
    }
    if (message.type === "done") {
      state.run = { ...(state.run || {}), ...message, stage: 3, progress: 1, active: false, hits: message.hits || 0 };
      document.body.classList.remove("is-locked");
      if (!message.succeeded && message.failed) return showError(message.failures.map((failure) => `${failure.file}: ${failure.message}`).join("\n"));
      setStatus(message.failed ? "partial" : "done", message.failed ? "Есть ошибки" : "Завершено", message.message);
      log(message.failed ? "warn" : "ok", message.message);
      if (message.succeeded) {
        $("lastFile").textContent = message.outputs?.[0]?.split(/[\\/]/).pop() || "Создан отчет";
        $("lastCount").textContent = message.succeeded;
        $("lastModel").textContent = state.model;
        $("btnOpenTop").disabled = $("btnOpenSide").disabled = false;
      }
      scheduleRender("runtime");
      return;
    }
    if (message.type === "stopped") {
      if (state.run) state.run.active = false;
      document.body.classList.remove("is-locked");
      setStatus("ready", "Остановлено", "Готовые файлы сохранены");
      log("warn", "Обработка остановлена");
      if (message.completed) $("btnOpenTop").disabled = $("btnOpenSide").disabled = false;
      $("runArea").innerHTML = "";
      scheduleRender("runtime");
      return;
    }
    if (message.type === "error") {
      showError(message.message || "Ошибка обработки");
    }
    if (message.type === "system") {
      if (message.version) $("systemVersion").textContent = message.version;
      $("systemDevice").textContent = $("chipDevice").textContent = message.device;
      $("systemCompute").textContent = message.compute;
      $("systemModels").textContent = message.modelCache;
    }
  }

  /* Chrome */

  function renderSteps() {
    const hasSource = !!state.source;
    const running = state.run && state.run.active;
    const done = ["done", "partial"].includes(state.status);
    const map = {
      source: hasSource ? "done" : "active",
      settings: hasSource ? (running || done ? "done" : "active") : "",
      export: running || done ? "done" : "",
      run: done ? "done" : running ? "active" : "",
      result: done ? "active" : ""
    };
    document.querySelectorAll(".step").forEach((b) => {
      const s = map[b.dataset.step];
      b.classList.toggle("is-active", s === "active");
      b.classList.toggle("is-done", s === "done");
      if (s === "active") b.setAttribute("aria-current", "step"); else b.removeAttribute("aria-current");
    });
  }

  function renderActions() {
    const running = state.run && state.run.active;
    const stopping = state.status === "stopping";
    const start = $("btnStart");
    start.disabled = !!running || stopping;
    start.classList.toggle("is-loading", !!running);
    if (start.dataset.running !== String(!!running)) {
      start.dataset.running = String(!!running);
      $("startLabel").innerHTML = running ? `<span class="loader" aria-hidden="true"><i></i><i></i><i></i></span> Обработка` : "Запустить обработку";
    }
    start.querySelector(".arrow").hidden = !!running;
    $("btnStop").disabled = !running || stopping;
    document.querySelectorAll(".lockable, #seg").forEach((element) => { element.inert = !!running; });

    const s = state.source;
    const src = !s ? "Источник не выбран" : s.type === "file" ? `<b>${esc(s.file.name)}</b>` : `<b>${s.files.length} файлов</b> из папки`;
    const summary = `${src} · модель <b>${state.model}</b><br><span class="kbd">Ctrl</span> <span class="kbd">Enter</span> запуск · <span class="kbd">Esc</span> остановка`;
    if ($("summary").innerHTML !== summary) $("summary").innerHTML = summary;
  }

  function renderLog() {
    const last = state.log[state.log.length - 1];
    $("logLast").textContent = last ? `${last.time}  ${last.msg}` : "Журнал пуст";
    const body = $("logBody");
    body.hidden = !state.logOpen;
    $("log").classList.toggle("is-open", state.logOpen);
    $("logToggle").setAttribute("aria-expanded", state.logOpen);
    if (!state.log.length) {
      body.innerHTML = `<div class="log-empty">Здесь появятся сообщения после запуска обработки.</div>`;
      renderedLogId = 0;
      return;
    }
    if (!state.logOpen) return;
    const LV = { info: "INFO", ok: "OK", warn: "WARN", err: "ERROR" };
    const follow = body.scrollHeight - body.scrollTop - body.clientHeight < 32;
    body.querySelector(".log-empty")?.remove();
    const additions = state.log.filter((entry) => entry.id > renderedLogId);
    if (additions.length) {
      body.insertAdjacentHTML("beforeend", additions.map((l) => `<div class="log-line lvl-${l.level}"><time>${l.time}</time><span class="lvl">${LV[l.level]}</span><span class="msg">${esc(l.msg)}</span></div>`).join(""));
      renderedLogId = additions[additions.length - 1].id;
      while (body.children.length > 500) body.firstElementChild.remove();
      if (follow) body.scrollTop = body.scrollHeight;
    }
  }

  function setMode(mode) {
    if (state.run && state.run.active) return;
    state.mode = mode;
    document.body.dataset.mode = mode;
    $("seg").dataset.mode = mode;
    document.querySelectorAll("#seg button").forEach((b) => b.setAttribute("aria-selected", b.dataset.mode === mode));
    $("modeTitle").textContent = COPY[mode].title;
    $("modeDesc").textContent = COPY[mode].desc;
    $("workflowName").innerHTML = `${COPY[mode].flow} <span>· новый проект</span>`;
    resetRun();
    renderAll();
  }

  function renderAll() {
    renderSource();
    renderModels();
    renderRules();
    renderExport();
    renderRun();
    renderSteps();
    renderActions();
    renderLog();
    $("chipModel").textContent = state.model;
  }

  /* Events */

  async function chooseOutput() {
    if (!backendReady) return toast("Выбор папки доступен в приложении");
    try {
      const folder = await backend.choose_output_folder();
      if (folder) {
        state.opts.outputDir = folder;
        state.opts.saveNear = false;
        renderExport();
      }
    } catch (error) { showError(error); }
  }

  async function openResult() {
    if (!backendReady) return toast("Запустите приложение через Twitch Cutter Profanity.exe");
    try {
      if (!await backend.open_result_folder()) toast("Сначала выполните обработку файла");
    } catch (error) { toast(error.message || String(error)); }
  }

  function closeSettings() {
    $("settingsPop").classList.remove("open");
    $("btnSettings").setAttribute("aria-expanded", "false");
  }

  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-act], [data-model], [data-save], #seg button, .step");
    if (!e.target.closest(".pop-anchor")) closeSettings();
    if (!t) return;

    if (state.run?.active && (t.closest(".lockable") || t.matches("#seg button"))) return;
    if (t.matches("#seg button")) return setMode(t.dataset.mode);
    if (t.classList.contains("step")) return $(t.dataset.target).scrollIntoView({ behavior: "smooth", block: "start" });

    if (t.dataset.model) {
      state.model = t.dataset.model;
      if (state.status === "error") resetRun();
      log("info", `Выбрана модель ${state.model}`);
      return renderAll();
    }
    if (t.dataset.save) {
      if (t.dataset.save === "custom") return chooseOutput();
      state.opts.saveNear = true;
      return renderExport();
    }

    switch (t.dataset.act) {
      case "minimize": return backendReady && backend ? backend.minimize() : null;
      case "maximize": return backendReady && backend ? backend.toggle_maximize() : null;
      case "close": return backendReady && backend ? backend.close() : null;
      case "pick-file": return chooseFile();
      case "pick-folder": return chooseFolder();
      case "clear": state.source = null; resetRun(); log("info", "Выбор источника очищен"); return renderAll();
      case "retry": return startRun();
      case "go-model": return $("secModel").scrollIntoView({ behavior: "smooth", block: "center" });
      case "open": return openResult();
      case "again": state.source = null; resetRun(); renderAll(); return $("scroll").scrollTo({ top: 0, behavior: "smooth" });
    }
  });

  document.addEventListener("change", (e) => {
    if (state.run?.active) return;
    const k = e.target.dataset.opt;
    if (k) {
      state.opts[k] = e.target.checked;
      if ((k === "xml" || k === "mp4") && $("formatError")) $("formatError").innerHTML = "";
    }
  });

  document.addEventListener("input", (e) => {
    if (state.run?.active) return;
    const k = e.target.dataset.range;
    if (!k) return;
    state.opts[k] = parseFloat(e.target.value);
    $("out-" + k).textContent = state.opts[k].toFixed(k === "pad" ? 2 : 1) + " с";
  });

  $("btnStart").addEventListener("click", startRun);
  $("btnStop").addEventListener("click", stopRun);
  $("btnSettings").addEventListener("click", () => {
    const open = !$("settingsPop").classList.contains("open");
    $("settingsPop").classList.toggle("open", open);
    $("btnSettings").setAttribute("aria-expanded", open);
  });
  ["btnOpenTop", "btnOpenSide"].forEach((id) => $(id).addEventListener("click", openResult));
  $("logToggle").addEventListener("click", () => { state.logOpen = !state.logOpen; renderLog(); });
  $("logClear").addEventListener("click", () => { state.log = []; renderLog(); });

  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey && e.key === "Enter") { e.preventDefault(); if (!(state.run && state.run.active)) startRun(); }
    else if (e.ctrlKey && e.key.toLowerCase() === "o") { e.preventDefault(); chooseFile(); }
    else if (e.key === "Escape") { if ($("settingsPop").classList.contains("open")) closeSettings(); else stopRun(); }
  });

  async function connectBackend() {
    if (backendReady) return;
    backend = window.pywebview.api;
    backendReady = true;
    log("ok", "Подключен движок обработки");
    try { applyBackendMessage({ type: "system", ...await backend.get_system_info() }); }
    catch (error) { log("warn", error.message || String(error)); }
  }
  window.addEventListener("pywebviewready", connectBackend);
  if (window.pywebview?.api) connectBackend();

  window.onBackendMessage = applyBackendMessage;
  window.onBackendMessages = (messages) => messages.forEach(applyBackendMessage);

  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => {
    e.preventDefault();
    if (!e.target.closest("#drop") && !(state.run && state.run.active)) {
      chooseFile();
    }
  });

  renderAll();
})();
