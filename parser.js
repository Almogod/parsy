// ─── Parsy Parser Engine ───────────────────────────────────────────────
const ParsyEngine = (() => {

  // ── Utilities ──────────────────────────────────────────────────────
  const fmtSize = b => b < 1024 ? b+'B' : b < 1048576 ? (b/1024).toFixed(1)+'KB' : (b/1048576).toFixed(1)+'MB';
  const countWords = t => t.trim().split(/\s+/).filter(Boolean).length;
  const readingTime = w => Math.max(1, Math.ceil(w/200));

  function detectLanguage(text) {
    const sample = text.slice(0, 500).toLowerCase();
    if (/\b(le|la|les|de|du|et|un|une|est)\b/.test(sample)) return 'French';
    if (/\b(der|die|das|und|ist|ein|eine)\b/.test(sample)) return 'German';
    if (/\b(el|la|los|las|de|que|en|un|una)\b/.test(sample)) return 'Spanish';
    if (/[\u4e00-\u9fff]/.test(sample)) return 'Chinese';
    if (/[\u0600-\u06ff]/.test(sample)) return 'Arabic';
    if (/[\u0400-\u04ff]/.test(sample)) return 'Russian/Cyrillic';
    if (/[\u3040-\u30ff]/.test(sample)) return 'Japanese';
    return 'English';
  }

  function cleanWhitespace(text) {
    return text
      .replace(/\r\n/g, '\n')
      .replace(/\r/g, '\n')
      .replace(/\t/g, '  ')
      .replace(/[ \t]+$/gm, '')
      .replace(/\n{3,}/g, '\n\n')
      .trim();
  }

  // ── Heading detection ──────────────────────────────────────────────
  function detectHeadingLevel(line) {
    const l = line.trim();
    if (!l) return 0;
    if (/^#{1,6}\s/.test(l)) return parseInt(l.match(/^(#+)/)[1].length);
    if (l.length < 60 && /^[A-Z]/.test(l) && !/[.!?,;]$/.test(l) && l === l.toUpperCase() && l.length > 3) return 1;
    if (l.length < 80 && /^[A-Z]/.test(l) && !/[.!?,;]$/.test(l) && /^[A-Z][a-z]/.test(l)) return 2;
    return 0;
  }

  // ── Table parser ───────────────────────────────────────────────────
  function parseTables(text) {
    const tables = [];
    const lines = text.split('\n');
    let i = 0;
    while (i < lines.length) {
      if (lines[i] && lines[i].includes('|')) {
        const start = i;
        const rows = [];
        while (i < lines.length && lines[i].includes('|')) {
          const cells = lines[i].split('|').map(c => c.trim()).filter((c, idx, arr) => idx > 0 && idx < arr.length - 1 || arr.length === 1);
          if (cells.length > 0 && !cells.every(c => /^[-:]+$/.test(c))) rows.push(cells);
          i++;
        }
        if (rows.length >= 2) tables.push({ startLine: start, rows });
      } else i++;
    }
    return tables;
  }

  function tableToMarkdown(rows) {
    if (!rows.length) return '';
    const cols = Math.max(...rows.map(r => r.length));
    const header = rows[0].concat(Array(cols - rows[0].length).fill(''));
    const sep = header.map(() => '---');
    const body = rows.slice(1).map(r => r.concat(Array(cols - r.length).fill('')));
    const fmt = row => '| ' + row.join(' | ') + ' |';
    return [fmt(header), fmt(sep), ...body.map(fmt)].join('\n');
  }

  function tableToCSV(rows) {
    return rows.map(r => r.map(c => /[,"\n]/.test(c) ? `"${c.replace(/"/g,'""')}"` : c).join(',')).join('\n');
  }

  // ── Structure to Markdown ──────────────────────────────────────────
  function textToMarkdown(text, opts) {
    const lines = text.split('\n');
    const out = [];
    for (let i = 0; i < lines.length; i++) {
      const line = opts.clean ? lines[i].replace(/\s+$/,'') : lines[i];
      const trim = line.trim();
      if (!trim) { out.push(''); continue; }

      // Bullet lists
      if (/^[\u2022\u2023\u25e6\-\*]\s+/.test(trim)) {
        out.push('- ' + trim.replace(/^[\u2022\u2023\u25e6\-\*]\s+/,'')); continue;
      }
      // Numbered lists
      if (/^\d+[\.\)]\s+/.test(trim)) {
        out.push(trim); continue;
      }
      // Horizontal rule
      if (/^[-_=]{3,}$/.test(trim)) { out.push('\n---\n'); continue; }

      // Headings
      const hLevel = detectHeadingLevel(line);
      if (hLevel > 0) {
        const clean = trim.replace(/^#+\s*/,'');
        out.push('\n' + '#'.repeat(hLevel) + ' ' + clean + '\n');
        continue;
      }
      out.push(line);
    }
    return cleanWhitespace(out.join('\n'));
  }

  // ── JSON output ────────────────────────────────────────────────────
  function textToJSON(text, meta, tables, opts) {
    const lines = text.split('\n').filter(l => l.trim());
    const sections = [];
    let cur = { heading: null, content: [] };
    for (const line of lines) {
      const hLevel = detectHeadingLevel(line);
      if (hLevel > 0) {
        if (cur.content.length || cur.heading) sections.push(cur);
        cur = { heading: line.trim().replace(/^#+\s*/,''), level: hLevel, content: [] };
      } else if (line.trim()) {
        cur.content.push(line.trim());
      }
    }
    if (cur.content.length || cur.heading) sections.push(cur);
    return JSON.stringify({ metadata: meta, sections, tables: opts.tables ? tables : [] }, null, 2);
  }

  // ── HTML output ────────────────────────────────────────────────────
  function textToHTML(text) {
    const lines = text.split('\n');
    const out = ['<!DOCTYPE html><html><head><meta charset="UTF-8"><style>body{font-family:system-ui,sans-serif;max-width:800px;margin:2rem auto;padding:0 1rem;line-height:1.7;color:#1a1a1a}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:8px 12px}th{background:#f5f5f5}</style></head><body>'];
    let inList = false;
    for (const line of lines) {
      const t = line.trim();
      if (!t) { if (inList) { out.push('</ul>'); inList=false; } continue; }
      const hm = t.match(/^(#{1,6})\s+(.*)/);
      if (hm) { out.push(`<h${hm[1].length}>${hm[2]}</h${hm[1].length}>`); continue; }
      if (/^- /.test(t)) { if (!inList) { out.push('<ul>'); inList=true; } out.push(`<li>${t.slice(2)}</li>`); continue; }
      if (inList) { out.push('</ul>'); inList=false; }
      if (/^\|/.test(t)) { out.push(`<p><code>${t}</code></p>`); continue; }
      if (t==='---') { out.push('<hr>'); continue; }
      out.push(`<p>${t}</p>`);
    }
    if (inList) out.push('</ul>');
    out.push('</body></html>');
    return out.join('\n');
  }

  // ── Plain Text cleaner ─────────────────────────────────────────────
  function textToPlain(text, opts) {
    let t = text.replace(/^#{1,6}\s+/gm,'').replace(/[*_`~]+/g,'');
    if (opts.clean) t = cleanWhitespace(t);
    return t;
  }

  // ── Per-format CSV from tables ─────────────────────────────────────
  function buildCSV(tables) {
    if (!tables.length) return 'No tables detected in document.';
    return tables.map((t,i) => `# Table ${i+1}\n${tableToCSV(t.rows)}`).join('\n\n');
  }

  // ── Main parse dispatcher ──────────────────────────────────────────
  async function parseFile(file, opts, onStep) {
    const ext = file.name.split('.').pop().toLowerCase();
    const raw = await readFileText(file);
    onStep('Read file');

    // Route by type
    let text = raw;
    if (ext === 'html' || ext === 'htm') { text = stripHTML(raw); onStep('Stripped HTML'); }
    else if (ext === 'xml') { text = stripXML(raw); onStep('Parsed XML'); }
    else if (ext === 'csv') { text = csvToReadable(raw); onStep('Parsed CSV'); }
    else if (ext === 'json') { text = jsonToReadable(raw); onStep('Parsed JSON'); }
    else if (ext === 'md') { text = raw; onStep('Read Markdown'); }
    else { onStep('Processed text'); }

    if (opts.clean) { text = cleanWhitespace(text); onStep('Cleaned whitespace'); }

    const tables = opts.tables ? parseTables(text) : [];
    onStep('Extracted tables');

    const words = countWords(text);
    const meta = {
      fileName: file.name,
      fileSize: fmtSize(file.size),
      fileType: file.type || ext.toUpperCase(),
      wordCount: words,
      charCount: text.length,
      lineCount: text.split('\n').length,
      readingTime: readingTime(words) + ' min',
      tableCount: tables.length,
      language: detectLanguage(text),
      parsedAt: new Date().toLocaleString(),
    };
    onStep('Extracted metadata');

    let output;
    if (opts.format === 'markdown') output = textToMarkdown(text, opts);
    else if (opts.format === 'json') output = textToJSON(text, meta, tables, opts);
    else if (opts.format === 'html') output = textToHTML(textToMarkdown(text, opts));
    else if (opts.format === 'csv') output = buildCSV(tables);
    else output = textToPlain(text, opts);

    if (opts.meta && opts.format === 'markdown') {
      const metaBlock = '---\n' + Object.entries(meta).map(([k,v])=>`${k}: ${v}`).join('\n') + '\n---\n\n';
      output = metaBlock + output;
    }
    onStep('Generated output');

    return { output, meta, tables, raw: text };
  }

  // ── File reader ────────────────────────────────────────────────────
  function readFileText(file) {
    return new Promise((res, rej) => {
      const r = new FileReader();
      r.onload = e => res(e.target.result);
      r.onerror = rej;
      r.readAsText(file, 'UTF-8');
    });
  }

  // ── Helpers ────────────────────────────────────────────────────────
  function stripHTML(html) {
    const div = document.createElement('div');
    div.innerHTML = html;
    // Remove scripts/styles
    div.querySelectorAll('script,style,noscript').forEach(el => el.remove());
    // Block elements → newlines
    div.querySelectorAll('p,div,br,h1,h2,h3,h4,h5,h6,li,tr,td,th').forEach(el => {
      const tag = el.tagName.toLowerCase();
      if (/^h[1-6]$/.test(tag)) {
        const level = parseInt(tag[1]);
        el.prepend('\n' + '#'.repeat(level) + ' ');
        el.append('\n');
      } else if (tag === 'li') {
        el.prepend('\n- ');
      } else {
        el.append('\n');
      }
    });
    return cleanWhitespace(div.textContent || div.innerText || '');
  }

  function stripXML(xml) {
    const lines = [];
    const parser = new DOMParser();
    const doc = parser.parseFromString(xml, 'text/xml');
    function walk(node, depth) {
      if (node.nodeType === 3) {
        const t = node.textContent.trim();
        if (t) lines.push('  '.repeat(depth) + t);
      } else if (node.nodeType === 1) {
        lines.push('  '.repeat(depth) + `<${node.tagName}>`);
        node.childNodes.forEach(c => walk(c, depth + 1));
      }
    }
    walk(doc.documentElement, 0);
    return lines.join('\n');
  }

  function csvToReadable(csv) {
    const rows = csv.trim().split('\n').map(r => r.split(',').map(c => c.replace(/^"|"$/g,'').trim()));
    if (!rows.length) return csv;
    return tableToMarkdown(rows);
  }

  function jsonToReadable(json) {
    try {
      const obj = JSON.parse(json);
      return JSON.stringify(obj, null, 2);
    } catch { return json; }
  }

  return { parseFile, fmtSize, tableToMarkdown, tableToCSV };
})();
