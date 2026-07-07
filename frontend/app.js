// ─── Parsy App Controller v3 ────────────────────────────────────────────────
(() => {
  const BACKEND_URL = 'http://localhost:8000';
  let backendAvailable = false;

  // ── State ─────────────────────────────────────────────────────────────────
  let files = [], results = [], activeResult = 0;
  let selectedFormat = 'markdown', currentOutput = '';
  let previewMode = false, wordWrap = true, showLineNums = false;
  let searchOpen = false, searchQuery = '';
  let history = JSON.parse(sessionStorage.getItem('parsyHistory') || '[]');

  // ── Elements ──────────────────────────────────────────────────────────────
  const $ = id => document.getElementById(id);
  const uploadZone   = $('uploadZone'),  fileInput    = $('fileInput');
  const browseBtn    = $('browseBtn'),   addMoreBtn   = $('addMoreBtn');
  const fileQueue    = $('fileQueue'),   queueList    = $('queueList');
  const optionsBar   = $('optionsBar'),  parseBtn     = $('parseBtn');
  const progressArea = $('progressArea'),progressFill = $('progressFill');
  const progressLabel= $('progressLabel'),progressPct = $('progressPct');
  const progressSteps= $('progressSteps'),resultsArea = $('resultsArea');
  const resultsTabs  = $('resultsTabs'), outputCode   = $('outputCode');
  const metaPane     = $('metaPane'),    resultStats  = $('resultStats');
  const copyBtn      = $('copyBtn'),     downloadBtn  = $('downloadBtn');
  const newParseBtn  = $('newParseBtn'), themeToggle  = $('themeToggle');
  const toast        = $('toast'),       backendBadge = $('backendBadge');

  // ── Backend check ─────────────────────────────────────────────────────────
  async function checkBackend() {
    try {
      const r = await fetch(`${BACKEND_URL}/health`, { signal: AbortSignal.timeout(2000) });
      if (r.ok) {
        backendAvailable = true;
        backendBadge.textContent = '● Backend connected';
        backendBadge.className = 'backend-badge online';
      }
    } catch {
      backendAvailable = false;
      backendBadge.textContent = '◎ Local mode (browser)';
      backendBadge.className = 'backend-badge offline';
    }
  }
  checkBackend();

  // ── Theme ─────────────────────────────────────────────────────────────────
  let dark = localStorage.getItem('parsyTheme') !== 'light';
  function applyTheme() {
    document.documentElement.setAttribute('data-theme', dark ? '' : 'light');
    $('iconSun').style.display  = dark ? '' : 'none';
    $('iconMoon').style.display = dark ? 'none' : '';
    localStorage.setItem('parsyTheme', dark ? 'dark' : 'light');
  }
  applyTheme();
  themeToggle?.addEventListener('click', () => { dark = !dark; applyTheme(); });

  // ── Upload ────────────────────────────────────────────────────────────────
  browseBtn?.addEventListener('click', () => fileInput.click());
  uploadZone?.addEventListener('click', e => { if (e.target === browseBtn) return; fileInput.click(); });
  fileInput?.addEventListener('change', () => addFiles(Array.from(fileInput.files)));
  addMoreBtn?.addEventListener('click', () => fileInput.click());
  uploadZone?.addEventListener('dragover', e => { e.preventDefault(); uploadZone.classList.add('drag-over'); });
  uploadZone?.addEventListener('dragleave', () => uploadZone.classList.remove('drag-over'));
  uploadZone?.addEventListener('drop', e => {
    e.preventDefault(); uploadZone.classList.remove('drag-over');
    addFiles(Array.from(e.dataTransfer.files));
  });

  function addFiles(newFiles) {
    const allowed = ['pdf','txt','md','csv','json','html','htm','xml','docx','xlsx'];
    newFiles.forEach(f => {
      const ext = f.name.split('.').pop().toLowerCase();
      if (!allowed.includes(ext)) { showToast(`⚠ Unsupported: ${f.name}`); return; }
      if (files.find(x => x.name === f.name && x.size === f.size)) return;
      files.push(f);
    });
    renderQueue();
  }

  function renderQueue() {
    if (!files.length) {
      uploadZone.style.display = '';
      fileQueue.style.display = optionsBar.style.display = 'none';
      return;
    }
    uploadZone.style.display = 'none';
    fileQueue.style.display = optionsBar.style.display = '';
    queueList.innerHTML = files.map((f, i) => `
      <div class="queue-item" id="qi-${i}">
        <span class="qi-icon">${fileIcon(f.name)}</span>
        <div class="qi-info">
          <div class="qi-name">${f.name}</div>
          <div class="qi-size">${ParsyEngine.fmtSize(f.size)}</div>
        </div>
        <span class="qi-status ready" id="qi-status-${i}">Ready</span>
        <button class="qi-remove" data-i="${i}">✕</button>
      </div>`).join('');
    queueList.querySelectorAll('.qi-remove').forEach(btn =>
      btn.addEventListener('click', () => { files.splice(+btn.dataset.i, 1); renderQueue(); })
    );
  }

  function fileIcon(name) {
    const ext = name.split('.').pop().toLowerCase();
    return {pdf:'📄',docx:'📝',xlsx:'📊',txt:'📃',md:'📋',csv:'📈',json:'🔧',html:'🌐',htm:'🌐',xml:'📦'}[ext] || '📁';
  }

  // ── Format toggle ─────────────────────────────────────────────────────────
  $('formatToggle')?.addEventListener('click', e => {
    const btn = e.target.closest('.toggle-btn');
    if (!btn) return;
    document.querySelectorAll('.toggle-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    selectedFormat = btn.dataset.format;
    // If we have results, re-render with new format
    if (results.length) renderResult(activeResult);
  });

  // ── Parse ─────────────────────────────────────────────────────────────────
  parseBtn?.addEventListener('click', runParse);

  async function runParse() {
    if (!files.length) return;
    const opts = {
      format: selectedFormat,
      tables: $('optTables').checked,
      meta:   $('optMeta').checked,
      structure: $('optStructure').checked,
      clean:  $('optClean').checked,
      use_ml: true,
    };
    optionsBar.style.display = fileQueue.style.display = 'none';
    progressArea.style.display = '';
    resultsArea.style.display = 'none';
    results = [];
    setOrchStep('routing');

    for (let i = 0; i < files.length; i++) {
      const f = files[i];
      currentOutput = '';
      if (backendAvailable) await parseViaBackend(f, opts, i);
      else                   await parseLocally(f, opts, i);
    }

    await delay(300);
    progressArea.style.display = 'none';
    setOrchStep('done');
    saveToHistory();
    showResults();
  }

  // ── Backend SSE parse ─────────────────────────────────────────────────────
  async function parseViaBackend(file, opts, idx) {
    const form = new FormData();
    form.append('file', file);
    form.append('format', opts.format);
    form.append('tables', opts.tables);
    form.append('meta', opts.meta);
    form.append('clean', opts.clean);

    return new Promise(resolve => {
      let outputBuf = '', metrics = {};
      fetch(`${BACKEND_URL}/parse`, { method: 'POST', body: form })
        .then(resp => {
          if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
          const reader = resp.body.getReader();
          const decoder = new TextDecoder();
          let buf = '';
          function pump() {
            reader.read().then(({ done, value }) => {
              if (done) { results.push({ file, output: outputBuf, meta: metrics, tables: [] }); resolve(); return; }
              buf += decoder.decode(value, { stream: true });
              const parts = buf.split('\n\n'); buf = parts.pop();
              parts.forEach(part => handleSSEPart(part, metrics, out => { outputBuf += out; }));
              pump();
            }).catch(err => { results.push({ file, output: outputBuf || `Error: ${err}`, meta: {}, tables: [] }); resolve(); });
          }
          pump();
        })
        .catch(err => { results.push({ file, output: `Backend error: ${err}`, meta: {}, tables: [] }); resolve(); });
    });
  }

  function handleSSEPart(part, metrics, onChunk) {
    const lines = part.split('\n');
    let eventName = '', dataStr = '';
    for (const l of lines) {
      if (l.startsWith('event:')) eventName = l.slice(6).trim();
      if (l.startsWith('data:'))  dataStr   = l.slice(5).trim();
    }
    if (!dataStr) return;
    let d; try { d = JSON.parse(dataStr); } catch { return; }
    if (eventName === 'status' || eventName === 'route') {
      updateProgress(d.pct || 0, d.message || d.step);
      if (eventName === 'route') { setOrchStep(d.route === 'vision_ocr' ? 'ocr' : 'fast'); updateOrchDetails(d); }
    } else if (eventName === 'ml_result') {
      // Phase 2: ML annotation received
      addProgressStep(`🤖 ML: ${d.regions} regions · ${d.clusters} heading clusters · ${d.inferenceMs}ms`);
      updateProgress(d.pct || 78, `ML annotation done (${d.inferenceMs}ms)`);
    } else if (eventName === 'chunk') {
      onChunk(d.chunk);
      updateProgress(d.pct, `Streaming… (${(d.offset/1024).toFixed(0)}KB)`);
      setOrchStep('normalizing');
    } else if (eventName === 'done') {
      Object.assign(metrics, d.metrics || {});
      updateProgress(100, 'Complete!');
    } else if (eventName === 'error') {
      updateProgress(0, `Error: ${d.message}`);
    }
  }

  // ── Local parse ───────────────────────────────────────────────────────────
  async function parseLocally(file, opts, idx) {
    updateProgress(10, `Routing ${file.name}…`);
    setOrchStep('routing');
    await delay(100);
    updateProgress(30, `Parsing ${file.name}…`);
    setOrchStep('fast');
    const steps = [];
    const onStep = s => { steps.push(s); renderSteps(steps); };
    try {
      const r = await ParsyEngine.parseFile(file, opts, onStep);
      setOrchStep('normalizing');
      updateProgress(90, 'Normalizing…');
      await delay(100);
      results.push({ file, ...r });
      updateProgress(100, 'Done');
    } catch (err) {
      results.push({ file, output: `Error: ${err.message}`, meta: {}, tables: [] });
    }
  }

  function updateProgress(pct, label) {
    progressFill.style.width = pct + '%';
    progressLabel.textContent = label;
    progressPct.textContent = Math.round(pct) + '%';
  }

  function renderSteps(steps) {
    progressSteps.innerHTML = steps.map((s, i) =>
      `<div class="progress-step ${i === steps.length-1 ? 'active':'done'}">
        ${i === steps.length-1 ? '⟳':'✓'} ${s}
      </div>`).join('');
  }

  function addProgressStep(msg) {
    const div = document.createElement('div');
    div.className = 'progress-step active';
    div.textContent = '⟳ ' + msg;
    progressSteps.appendChild(div);
    progressSteps.scrollTop = progressSteps.scrollHeight;
  }

  // ── Orchestration diagram ─────────────────────────────────────────────────
  const orchSteps = ['routing','fast','ocr','normalizing','done'];
  function setOrchStep(step) {
    document.querySelectorAll('.orch-node').forEach(n => {
      n.classList.remove('active','done');
      const ns = n.dataset.step;
      const si = orchSteps.indexOf(ns), ci = orchSteps.indexOf(step);
      if (si < ci) n.classList.add('done');
      else if (ns === step) n.classList.add('active');
    });
  }

  function updateOrchDetails(d) {
    const detEl = $('orchDetails'); if (!detEl) return;
    detEl.innerHTML = `
      <span class="orch-pill">${d.route}</span>
      <span class="orch-pill">${(d.confidence*100).toFixed(0)}% confidence</span>
      <span class="orch-pill">${d.pageCount} pages</span>
      <span class="orch-pill">${d.workers} workers</span>
      <span class="orch-pill complexity-${d.complexity}">${d.complexity} complexity</span>
    `;
    if (d.reasons) {
      const rl = $('orchReasons');
      if (rl) rl.innerHTML = d.reasons.map(r => `<li>${r}</li>`).join('');
    }
  }

  // ── Session history ───────────────────────────────────────────────────────
  function saveToHistory() {
    results.forEach(r => {
      const entry = {
        id: Date.now(),
        name: r.file.name,
        size: ParsyEngine.fmtSize(r.file.size),
        words: r.meta?.wordCount || 0,
        tables: r.meta?.tableCount || 0,
        lang: r.meta?.language || '–',
        format: selectedFormat,
        output: r.output,
        meta: r.meta,
        ts: new Date().toLocaleTimeString(),
      };
      history.unshift(entry);
    });
    if (history.length > 20) history.length = 20;
    sessionStorage.setItem('parsyHistory', JSON.stringify(history));
    renderHistory();
  }

  function renderHistory() {
    const panel = $('historyList'); if (!panel) return;
    if (!history.length) {
      panel.innerHTML = '<div class="history-empty">No sessions yet</div>';
      return;
    }
    panel.innerHTML = history.map(h => `
      <div class="history-item" data-id="${h.id}">
        <div class="hi-icon">${fileIcon(h.name)}</div>
        <div class="hi-info">
          <div class="hi-name">${h.name}</div>
          <div class="hi-meta">${h.words.toLocaleString()} words · ${h.tables} tables · ${h.lang}</div>
        </div>
        <div class="hi-time">${h.ts}</div>
      </div>`).join('');
    panel.querySelectorAll('.history-item').forEach(el =>
      el.addEventListener('click', () => loadHistoryItem(+el.dataset.id))
    );
  }

  function loadHistoryItem(id) {
    const h = history.find(x => x.id === id); if (!h) return;
    results = [{ file: { name: h.name }, output: h.output, meta: h.meta, tables: [] }];
    progressArea.style.display = 'none';
    optionsBar.style.display = fileQueue.style.display = 'none';
    uploadZone.style.display = 'none';
    showResults();
    showToast(`↩ Restored: ${h.name}`);
  }

  // ── Results view ──────────────────────────────────────────────────────────
  function showResults() {
    resultsArea.style.display = '';
    activeResult = 0;
    resultsTabs.innerHTML = results.map((r, i) =>
      `<button class="result-tab ${i===0?'active':''}" data-i="${i}">${fileIcon(r.file.name)} ${r.file.name}</button>`
    ).join('');
    resultsTabs.querySelectorAll('.result-tab').forEach(btn =>
      btn.addEventListener('click', () => {
        activeResult = +btn.dataset.i;
        resultsTabs.querySelectorAll('.result-tab').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        renderResult(activeResult);
      })
    );
    renderResult(0);
    renderHistory();
  }

  function renderResult(idx) {
    const r = results[idx]; if (!r) return;
    const container = $('outputPane');
    container.classList.add('switching');

    setTimeout(() => {
      currentOutput = r.output || '';
      applyOutputMode();
      const m = r.meta || {};
      resultStats.innerHTML = [
        m.wordCount   && `<span class="stat-pill"><b>${(+m.wordCount).toLocaleString()}</b> words</span>`,
        m.pageCount   && `<span class="stat-pill"><b>${m.pageCount}</b> pages</span>`,
        m.tableCount  && `<span class="stat-pill"><b>${m.tableCount}</b> tables</span>`,
        m.language    && `<span class="stat-pill"><b>${m.language}</b></span>`,
        m.readingTime && `<span class="stat-pill"><b>${m.readingTime}</b> read</span>`,
        m.pipeline    && `<span class="stat-pill route-pill">${m.pipeline}</span>`,
      ].filter(Boolean).join('');

      if (Object.keys(m).length) {
        metaPane.innerHTML = `<div class="meta-title">Metadata</div>` +
          Object.entries(m).map(([k,v]) =>
            `<div class="meta-row"><span class="meta-key">${k}</span><span class="meta-val">${v}</span></div>`
          ).join('');
        metaPane.style.display = '';
      } else metaPane.style.display = 'none';

      container.classList.remove('switching');
    }, 80);
  }

  function applyOutputMode() {
    const container = $('outputPane');
    if (previewMode && selectedFormat === 'markdown') {
      container.innerHTML = `<div class="md-preview">${ParsyEngine.renderMarkdown(currentOutput)}</div>`;
    } else if (selectedFormat === 'json') {
      outputCode.innerHTML = ParsyEngine.highlightJSON(currentOutput.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'));
      if (!container.contains(outputCode)) container.appendChild(outputCode);
    } else {
      outputCode.textContent = currentOutput;
      if (!container.contains(outputCode)) container.appendChild(outputCode);
    }
    container.classList.toggle('wrap', wordWrap);
    container.classList.toggle('line-nums', showLineNums);
  }

  // ── Toolbar controls ──────────────────────────────────────────────────────
  copyBtn?.addEventListener('click', async () => {
    await navigator.clipboard.writeText(currentOutput);
    showToast('✓ Copied to clipboard');
  });

  downloadBtn?.addEventListener('click', () => {
    const r = results[activeResult]; if (!r) return;
    const exts = { markdown:'md', plaintext:'txt', json:'json', html:'html', csv:'csv' };
    const ext  = exts[selectedFormat] || 'txt';
    const base = r.file.name.replace(/\.[^.]+$/, '');
    const blob = new Blob([currentOutput], { type: 'text/plain' });
    const a = Object.assign(document.createElement('a'), {
      href: URL.createObjectURL(blob), download: `${base}_parsed.${ext}`
    });
    a.click(); URL.revokeObjectURL(a.href);
    showToast('✓ Downloaded');
  });

  newParseBtn?.addEventListener('click', () => {
    files = []; results = []; activeResult = 0; currentOutput = '';
    fileInput.value = '';
    resultsArea.style.display = progressArea.style.display = 'none';
    optionsBar.style.display = fileQueue.style.display = 'none';
    uploadZone.style.display = '';
    setOrchStep(''); checkBackend();
  });

  // ── Preview toggle ────────────────────────────────────────────────────────
  document.addEventListener('click', e => {
    const btn = e.target.closest('[data-action]');
    if (!btn) return;
    const action = btn.dataset.action;
    if (action === 'preview') {
      previewMode = !previewMode;
      btn.classList.toggle('active', previewMode);
      btn.title = previewMode ? 'Show raw' : 'Preview rendered';
      applyOutputMode();
    } else if (action === 'wrap') {
      wordWrap = !wordWrap;
      btn.classList.toggle('active', wordWrap);
      applyOutputMode();
    } else if (action === 'search') {
      toggleSearch();
    } else if (action === 'history') {
      $('historyPanel')?.classList.toggle('open');
    }
  });

  // ── Inline search ─────────────────────────────────────────────────────────
  function toggleSearch() {
    const panel = $('searchPanel'); if (!panel) return;
    searchOpen = !searchOpen;
    panel.style.display = searchOpen ? '' : 'none';
    if (searchOpen) $('searchInput')?.focus();
  }

  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'f' && resultsArea?.style.display !== 'none') {
      e.preventDefault(); toggleSearch();
    }
    if (e.key === 'Escape' && searchOpen) toggleSearch();
  });

  $('searchInput')?.addEventListener('input', e => {
    searchQuery = e.target.value;
    applySearch();
  });

  function applySearch() {
    const container = $('outputPane'); if (!container || !searchQuery) return;
    if (previewMode) return; // search only in raw mode
    const escaped = searchQuery.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const regex = new RegExp(`(${escaped})`, 'gi');
    const text = currentOutput.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    outputCode.innerHTML = text.replace(regex, '<mark>$1</mark>');
    const matches = (outputCode.innerHTML.match(/<mark>/g) || []).length;
    const counter = $('searchCount');
    if (counter) counter.textContent = matches ? `${matches} match${matches !== 1 ? 'es' : ''}` : 'No matches';
  }

  // ── Toast ─────────────────────────────────────────────────────────────────
  let toastTimer;
  function showToast(msg) {
    toast.textContent = msg;
    toast.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove('show'), 2200);
  }

  function delay(ms) { return new Promise(r => setTimeout(r, ms)); }

  // ── Terminal demo ─────────────────────────────────────────────────────────
  const demo = $('terminalDemo');
  if (demo) {
    const demoLines = [
      { t:200,  txt:'> parsy report_q4_2024.pdf', cls:'cmd' },
      { t:700,  txt:'[Router] Inspecting document…', cls:'dim' },
      { t:1100, txt:'[Router] Route: fast_text (confidence: 97%)', cls:'ok' },
      { t:1400, txt:'[Router] 3 tables detected, 18 sections', cls:'ok' },
      { t:1800, txt:'[PDF.js] Parsed 50 pages in parallel', cls:'dim' },
      { t:2200, txt:'[Normalizer] Heading tree validated', cls:'ok' },
      { t:2600, txt:'[Normalizer] Dates normalized (ISO 8601)', cls:'ok' },
      { t:2900, txt:'[Stream] Chunking output (8192B chunks)…', cls:'dim' },
      { t:3200, txt:'', cls:'' },
      { t:3300, txt:'# Q4 2024 Financial Report', cls:'h1' },
      { t:3500, txt:'**Author**: Jane Smith  **Date**: 2024-12-01', cls:'' },
      { t:3700, txt:'', cls:'' },
      { t:3800, txt:'## Executive Summary', cls:'h2' },
      { t:4000, txt:'Revenue grew 24% YoY reaching $142M…', cls:'' },
      { t:4200, txt:'', cls:'' },
      { t:4300, txt:'| Metric  | Q3   | Q4   | Δ    |', cls:'tbl' },
      { t:4400, txt:'|---------|------|------|------|', cls:'tbl' },
      { t:4500, txt:'| Revenue | 114M | 142M | +24% |', cls:'tbl' },
      { t:4600, txt:'| Margin  | 31%  | 38%  | +7pp |', cls:'tbl' },
    ];
    demoLines.forEach(({ t, txt, cls }) => setTimeout(() => {
      const span = document.createElement('div');
      span.textContent = txt;
      const styles = {
        cmd: 'color:#a5b4fc;font-weight:700',
        ok:  'color:#10b981',
        dim: 'color:#52525b',
        h1:  'color:#fafafa;font-weight:800',
        h2:  'color:#e4e4e7;font-weight:700',
        tbl: 'color:#06b6d4',
      };
      if (styles[cls]) span.style.cssText = styles[cls];
      demo.appendChild(span);
      demo.scrollTop = demo.scrollHeight;
    }, t));
  }

  // ── Scroll header ─────────────────────────────────────────────────────────
  window.addEventListener('scroll', () => {
    const h = $('header');
    if (h) h.style.borderBottomColor = window.scrollY > 40 ? 'var(--border-active)' : 'var(--border)';
  });

  // ── Init history ──────────────────────────────────────────────────────────
  renderHistory();

})();
