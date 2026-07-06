// ─── Parsy Parser Engine v3 ────────────────────────────────────────────────
// Browser-side parser with PDF.js + mammoth.js support
const ParsyEngine = (() => {

  // ── Utilities ──────────────────────────────────────────────────────────────
  const fmtSize = b => b < 1024 ? b+'B' : b < 1048576 ? (b/1024).toFixed(1)+'KB' : (b/1048576).toFixed(1)+'MB';
  const countWords = t => t.trim().split(/\s+/).filter(Boolean).length;
  const readingTime = w => Math.max(1, Math.ceil(w/200));

  function detectLanguage(text) {
    const sample = text.slice(0, 500).toLowerCase();
    if (/\b(le|la|les|de|du|et|un|une|est|avec|pour)\b/.test(sample)) return 'French';
    if (/\b(der|die|das|und|ist|ein|eine|nicht|sie)\b/.test(sample)) return 'German';
    if (/\b(el|la|los|las|de|que|en|un|una|con)\b/.test(sample)) return 'Spanish';
    if (/[\u4e00-\u9fff]/.test(sample)) return 'Chinese';
    if (/[\u0600-\u06ff]/.test(sample)) return 'Arabic';
    if (/[\u0400-\u04ff]/.test(sample)) return 'Russian/Cyrillic';
    if (/[\u3040-\u30ff]/.test(sample)) return 'Japanese';
    if (/\b(di|il|la|le|per|che|non|un|una)\b/.test(sample)) return 'Italian';
    if (/\b(de|het|een|van|in|op|is|niet)\b/.test(sample)) return 'Dutch';
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

  // ── Heading detection ──────────────────────────────────────────────────────
  function detectHeadingLevel(line) {
    const l = line.trim();
    if (!l) return 0;
    if (/^#{1,6}\s/.test(l)) return parseInt(l.match(/^(#+)/)[1].length);
    if (l.length < 60 && /^[A-Z]/.test(l) && !/[.!?,;]$/.test(l) && l === l.toUpperCase() && l.length > 3) return 1;
    if (l.length < 80 && /^[A-Z]/.test(l) && !/[.!?,;]$/.test(l) && /^[A-Z][a-z]/.test(l)) return 2;
    return 0;
  }

  // ── Table parser ───────────────────────────────────────────────────────────
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

  // ── Structure to Markdown ──────────────────────────────────────────────────
  function textToMarkdown(text, opts) {
    const lines = text.split('\n');
    const out = [];
    for (let i = 0; i < lines.length; i++) {
      const line = opts.clean ? lines[i].replace(/\s+$/,'') : lines[i];
      const trim = line.trim();
      if (!trim) { out.push(''); continue; }
      if (/^[\u2022\u2023\u25e6\-\*]\s+/.test(trim)) {
        out.push('- ' + trim.replace(/^[\u2022\u2023\u25e6\-\*]\s+/,'')); continue;
      }
      if (/^\d+[\.]\s+/.test(trim)) { out.push(trim); continue; }
      if (/^[-_=]{3,}$/.test(trim)) { out.push('\n---\n'); continue; }
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

  // ── JSON output ────────────────────────────────────────────────────────────
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

  // ── HTML output ────────────────────────────────────────────────────────────
  function textToHTML(text) {
    const lines = text.split('\n');
    const out = ['<!DOCTYPE html><html><head><meta charset="UTF-8"><style>body{font-family:system-ui,sans-serif;max-width:800px;margin:2rem auto;padding:0 1rem;line-height:1.7;color:#1a1a1a}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:8px 12px}th{background:#f5f5f5}code{background:#f4f4f4;padding:2px 6px;border-radius:3px}pre{background:#1e1e1e;color:#d4d4d4;padding:1rem;border-radius:8px;overflow-x:auto}</style></head><body>'];
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

  // ── Plain Text cleaner ─────────────────────────────────────────────────────
  function textToPlain(text, opts) {
    let t = text.replace(/^#{1,6}\s+/gm,'').replace(/[*_`~]+/g,'');
    if (opts.clean) t = cleanWhitespace(t);
    return t;
  }

  function buildCSV(tables) {
    if (!tables.length) return 'No tables detected in document.';
    return tables.map((t,i) => `# Table ${i+1}\n${tableToCSV(t.rows)}`).join('\n\n');
  }

  // ── Syntax highlighter (JSON) ──────────────────────────────────────────────
  function highlightJSON(str) {
    return str.replace(/("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g, match => {
      let cls = 'json-num';
      if (/^"/.test(match)) {
        cls = /:$/.test(match) ? 'json-key' : 'json-str';
      } else if (/true|false/.test(match)) {
        cls = 'json-bool';
      } else if (/null/.test(match)) {
        cls = 'json-null';
      }
      return `<span class="${cls}">${match}</span>`;
    });
  }

  // ── Markdown renderer ──────────────────────────────────────────────────────
  function renderMarkdown(md) {
    let html = md
      // Escape HTML first
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      // Headings
      .replace(/^######\s+(.+)$/gm,'<h6>$1</h6>')
      .replace(/^#####\s+(.+)$/gm,'<h5>$1</h5>')
      .replace(/^####\s+(.+)$/gm,'<h4>$1</h4>')
      .replace(/^###\s+(.+)$/gm,'<h3>$1</h3>')
      .replace(/^##\s+(.+)$/gm,'<h2>$1</h2>')
      .replace(/^#\s+(.+)$/gm,'<h1>$1</h1>')
      // HR
      .replace(/^---$/gm,'<hr>')
      // Bold + italic
      .replace(/\*\*\*(.+?)\*\*\*/g,'<strong><em>$1</em></strong>')
      .replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>')
      .replace(/\*(.+?)\*/g,'<em>$1</em>')
      .replace(/__(.+?)__/g,'<strong>$1</strong>')
      .replace(/_(.+?)_/g,'<em>$1</em>')
      // Inline code
      .replace(/`([^`]+)`/g,'<code>$1</code>')
      // Links
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g,'<a href="$2" target="_blank">$1</a>')
      // Tables (simple)
      .replace(/(\|.+\|\n\|[-| :]+\|\n(\|.+\|\n)*)/g, tblMatch => {
        const rows = tblMatch.trim().split('\n').filter(r => !r.match(/^\|[-| :]+\|$/));
        let tbl = '<table><thead>';
        rows.forEach((row, ri) => {
          const cells = row.split('|').filter((_,i,a) => i > 0 && i < a.length - 1).map(c => c.trim());
          const tag = ri === 0 ? 'th' : 'td';
          if (ri === 1) tbl += '</thead><tbody>';
          tbl += '<tr>' + cells.map(c => `<${tag}>${c}</${tag}>`).join('') + '</tr>';
        });
        tbl += '</tbody></table>';
        return tbl;
      })
      // Unordered list items
      .replace(/^[-*]\s+(.+)$/gm,'<li>$1</li>')
      // Numbered list items
      .replace(/^\d+\.\s+(.+)$/gm,'<li>$1</li>')
      // Blockquotes
      .replace(/^>\s+(.+)$/gm,'<blockquote>$1</blockquote>')
      // Paragraphs (wrap non-tagged lines)
      .split('\n\n')
      .map(block => {
        const t = block.trim();
        if (!t || /^<[h1-6hr]|^<ul|^<ol|^<li|^<table|^<blockquote/.test(t)) return t;
        if (/<li>/.test(t)) return '<ul>' + t + '</ul>';
        return '<p>' + t.replace(/\n/g,' ') + '</p>';
      })
      .join('\n');
    return html;
  }

  // ── PDF.js integration ────────────────────────────────────────────────────
  async function parsePDF(file, opts, onStep) {
    onStep('Loading PDF.js…');
    // Load PDF.js if not already loaded
    if (!window.pdfjsLib) {
      await loadScript('https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js');
      window.pdfjsLib.GlobalWorkerOptions.workerSrc =
        'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
    }
    onStep('Reading PDF…');
    const arrayBuf = await file.arrayBuffer();
    const pdf = await pdfjsLib.getDocument({ data: arrayBuf }).promise;
    const numPages = pdf.numPages;
    onStep(`PDF loaded (${numPages} pages)`);

    const allText = [];
    const tables = [];
    let wordCount = 0;

    for (let i = 1; i <= numPages; i++) {
      const page = await pdf.getPage(i);
      const content = await page.getTextContent();
      // Group items into lines by Y position
      const lineMap = new Map();
      content.items.forEach(item => {
        const y = Math.round(item.transform[5]);
        if (!lineMap.has(y)) lineMap.set(y, []);
        lineMap.get(y).push({ text: item.str, x: item.transform[4], size: item.height });
      });
      // Sort lines top to bottom (PDF y-axis is inverted)
      const sortedYs = [...lineMap.keys()].sort((a,b) => b - a);
      const pageLines = sortedYs.map(y =>
        lineMap.get(y).sort((a,b) => a.x - b.x).map(it => it.text).join(' ').trim()
      ).filter(Boolean);
      allText.push(...pageLines, '');
      onStep(`Parsed page ${i}/${numPages}`);
    }

    // Get metadata
    const meta = await pdf.getMetadata().catch(() => ({}));
    const docMeta = {
      fileName: file.name,
      fileSize: fmtSize(file.size),
      pageCount: numPages,
      title: meta.info?.Title || '',
      author: meta.info?.Author || '',
      createdAt: meta.info?.CreationDate || '',
      pipeline: 'PDF.js (browser)',
    };

    const rawText = allText.join('\n');
    const words = countWords(rawText);
    Object.assign(docMeta, {
      wordCount: words,
      charCount: rawText.length,
      readingTime: readingTime(words) + ' min',
      language: detectLanguage(rawText),
      parsedAt: new Date().toLocaleString(),
    });

    const parsedTables = opts.tables ? parseTables(rawText) : [];
    docMeta.tableCount = parsedTables.length;

    let output = buildOutput(rawText, docMeta, parsedTables, opts);
    return { output, meta: docMeta, tables: parsedTables, raw: rawText };
  }

  // ── DOCX via mammoth.js ────────────────────────────────────────────────────
  async function parseDOCX(file, opts, onStep) {
    onStep('Loading mammoth.js…');
    if (!window.mammoth) {
      await loadScript('https://cdnjs.cloudflare.com/ajax/libs/mammoth/1.6.0/mammoth.browser.min.js');
    }
    onStep('Converting DOCX…');
    const arrayBuf = await file.arrayBuffer();
    const result = await mammoth.convertToMarkdown({ arrayBuffer: arrayBuf });
    onStep('DOCX converted');
    const text = result.value;
    const words = countWords(text);
    const parsedTables = opts.tables ? parseTables(text) : [];
    const meta = {
      fileName: file.name,
      fileSize: fmtSize(file.size),
      wordCount: words,
      charCount: text.length,
      lineCount: text.split('\n').length,
      readingTime: readingTime(words) + ' min',
      tableCount: parsedTables.length,
      language: detectLanguage(text),
      pipeline: 'mammoth.js (browser)',
      parsedAt: new Date().toLocaleString(),
    };
    const output = buildOutput(text, meta, parsedTables, opts);
    return { output, meta, tables: parsedTables, raw: text };
  }

  // ── Output builder ─────────────────────────────────────────────────────────
  function buildOutput(text, meta, tables, opts) {
    let output;
    if (opts.format === 'markdown') output = textToMarkdown(text, opts);
    else if (opts.format === 'json')  output = textToJSON(text, meta, tables, opts);
    else if (opts.format === 'html')  output = textToHTML(textToMarkdown(text, opts));
    else if (opts.format === 'csv')   output = buildCSV(tables);
    else output = textToPlain(text, opts);

    if (opts.meta && opts.format === 'markdown') {
      const metaBlock = '---\n' + Object.entries(meta).map(([k,v])=>`${k}: ${v}`).join('\n') + '\n---\n\n';
      output = metaBlock + output;
    }
    return output;
  }

  // ── Script loader ──────────────────────────────────────────────────────────
  function loadScript(src) {
    return new Promise((resolve, reject) => {
      if (document.querySelector(`script[src="${src}"]`)) { resolve(); return; }
      const s = document.createElement('script');
      s.src = src; s.onload = resolve; s.onerror = reject;
      document.head.appendChild(s);
    });
  }

  // ── Main parse dispatcher ──────────────────────────────────────────────────
  async function parseFile(file, opts, onStep) {
    const ext = file.name.split('.').pop().toLowerCase();
    onStep('Routing document…');

    if (ext === 'pdf') return parsePDF(file, opts, onStep);
    if (ext === 'docx') return parseDOCX(file, opts, onStep);

    // All other types: read as text
    const raw = await readFileText(file);
    onStep('Read file');

    let text = raw;
    if (ext === 'html' || ext === 'htm') { text = stripHTML(raw); onStep('Stripped HTML'); }
    else if (ext === 'xml')  { text = stripXML(raw);      onStep('Parsed XML'); }
    else if (ext === 'csv')  { text = csvToReadable(raw);  onStep('Parsed CSV'); }
    else if (ext === 'json') { text = jsonToReadable(raw); onStep('Parsed JSON'); }
    else if (ext === 'md')   { text = raw;                 onStep('Read Markdown'); }
    else                     {                             onStep('Processed text'); }

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
      pipeline: 'Browser (local)',
      parsedAt: new Date().toLocaleString(),
    };
    onStep('Extracted metadata');

    const output = buildOutput(text, meta, tables, opts);
    onStep('Generated output');
    return { output, meta, tables, raw: text };
  }

  // ── File reader ────────────────────────────────────────────────────────────
  function readFileText(file) {
    return new Promise((res, rej) => {
      const r = new FileReader();
      r.onload = e => res(e.target.result);
      r.onerror = rej;
      r.readAsText(file, 'UTF-8');
    });
  }

  // ── HTML stripper ──────────────────────────────────────────────────────────
  function stripHTML(html) {
    const div = document.createElement('div');
    div.innerHTML = html;
    div.querySelectorAll('script,style,noscript,nav,footer').forEach(el => el.remove());
    div.querySelectorAll('p,div,br,h1,h2,h3,h4,h5,h6,li,tr,td,th').forEach(el => {
      const tag = el.tagName.toLowerCase();
      if (/^h[1-6]$/.test(tag)) {
        el.prepend('\n' + '#'.repeat(parseInt(tag[1])) + ' ');
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
    try { return JSON.stringify(JSON.parse(json), null, 2); }
    catch { return json; }
  }

  return {
    parseFile, fmtSize, tableToMarkdown, tableToCSV,
    renderMarkdown, highlightJSON,
  };
})();
