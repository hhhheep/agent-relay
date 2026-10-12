import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';
import { loadProvider } from './provider.mjs';

export const sha = data => crypto.createHash('sha256').update(data).digest('hex');
const json = value => JSON.stringify(value, null, 2) + '\n';
const portable = value => value.split(path.sep).join('/');
const uri = value => encodeURI(value).replaceAll('#','%23').replaceAll('(','%28').replaceAll(')','%29');
const inside = (parent, child) => child === parent || child.startsWith(parent + '/');
export function atomic(file, data) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temp = file + '.' + crypto.randomUUID() + '.tmp';
  const fd = fs.openSync(temp, 'wx');
  try { fs.writeFileSync(fd, data); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
  fs.renameSync(temp, file);
}
export function readConfig(file) {
  const base = path.dirname(path.resolve(file));
  const c = JSON.parse(fs.readFileSync(file, 'utf8'));
  c.vault = fs.realpathSync(path.resolve(base, c.vault));
  c.state = path.resolve(base, c.state);
  c.provider_module = path.resolve(base, c.provider_module);
  c.credential_env_file = path.resolve(base, c.credential_env_file);
  for (const key of ['output']) {
    if (!c[key] || path.isAbsolute(c[key]) || c[key].split(/[\\/]/).includes('..'))
      throw new Error('INVALID_CONFIG_PATH');
    c[key] = c[key].replaceAll('\\', '/');
  }
  for (const roots of [c.watch_roots, c.exclude_roots]) {
    if (!Array.isArray(roots)) throw new Error('INVALID_CONFIG_ROOTS');
    for (const root of roots)
      if (!root || path.isAbsolute(root) || root.split(/[\\/]/).includes('..') || root.includes('\\'))
        throw new Error('INVALID_CONFIG_ROOTS');
  }
  if (!c.extensions.every(ext => ['.md', '.txt'].includes(ext))) throw new Error('INVALID_EXTENSIONS');
  for (const k of ['poll_seconds','stable_seconds','max_source_bytes','max_batch_bytes',
    'batch_files','max_attempts','retry_seconds'])
    if (!Number.isInteger(c[k]) || c[k] < 1) throw new Error('INVALID_CONFIG_NUMBER');
  if (c.batch_files > 5 || c.max_attempts > 5 || c.max_source_bytes > c.max_batch_bytes)
    throw new Error('INVALID_CONFIG_BOUND');
  return c;
}

// Separate SQLite write transaction is an OS-released singleton lock. It needs
// no stale-PID guessing and does not block status reads on the jobs database.
export function acquireLock(state) {
  fs.mkdirSync(state, { recursive: true });
  const db = new DatabaseSync(path.join(state, 'singleton.sqlite'));
  try { db.exec('PRAGMA busy_timeout=0; CREATE TABLE IF NOT EXISTS owner(id INTEGER); BEGIN IMMEDIATE;'); }
  catch { db.close(); throw new Error('ALREADY_RUNNING'); }
  return () => { db.exec('ROLLBACK;'); db.close(); };
}

export class Engine {
  constructor(config, { generate, now = () => Date.now() } = {}) {
    this.c = config; this.now = now; this.generate = generate;
    this.rules = fs.readFileSync(new URL('./rules.md', import.meta.url), 'utf8');
    this.rule = sha(this.rules); this.warnings = [];
    fs.mkdirSync(config.state, { recursive: true });
    this.db = new DatabaseSync(path.join(config.state, 'jobs.sqlite'));
    this.db.exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
      CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
      CREATE TABLE IF NOT EXISTS baseline(source TEXT,sha TEXT,PRIMARY KEY(source,sha));
      CREATE TABLE IF NOT EXISTS observed(source TEXT PRIMARY KEY,sha TEXT,size INTEGER,
        mtime REAL,stable_since REAL,observations INTEGER);
      CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,source TEXT,sha TEXT,rules TEXT,
        status TEXT CHECK(status IN ('pending','processing','success','failed')),
        attempts INTEGER DEFAULT 0,priority INTEGER DEFAULT 0,receipt TEXT,
        output TEXT,created REAL,updated REAL,retry_at REAL,error TEXT,attempt_dir TEXT);
      CREATE INDEX IF NOT EXISTS job_state ON jobs(status,retry_at,priority,created);`);
    this.warnings = JSON.parse(this.meta('scan_warnings') || '[]');
  }
  close() { this.db.close(); }
  meta(key) { return this.db.prepare('SELECT value FROM meta WHERE key=?').get(key)?.value; }
  setMeta(key, value) { this.db.prepare('INSERT OR REPLACE INTO meta VALUES (?,?)').run(key, String(value)); }
  safe(relative) {
    if (typeof relative !== 'string' || !relative || relative.includes('\\') ||
      path.isAbsolute(relative) || relative.split('/').some(x => !x || x === '..' || x === '.'))
      throw new Error('UNSAFE_SOURCE_PATH');
    const file = path.join(this.c.vault, relative);
    let current = this.c.vault;
    for (const segment of relative.split('/')) {
      current = path.join(current, segment);
      if (fs.existsSync(current) && fs.lstatSync(current).isSymbolicLink()) throw new Error('SYMLINK_EXCLUDED');
    }
    return file;
  }
  allowed(relative) {
    return this.c.watch_roots.some(root => inside(root, relative)) &&
      !this.c.exclude_roots.some(root => inside(root, relative)) &&
      !inside(this.c.output, relative) &&
      !relative.split('/').some(p => p.startsWith('.') || /^(?:node_modules|Discourse Canvas|Nodes)$/.test(p)) &&
      !/(?:\.tmp|\.part|\.partial|\.bak|~)(?:\.|$)/i.test(relative);
  }
  files() {
    const sources = [], manifests = [];
    const walk = relative => {
      if (!this.allowed(relative)) return;
      const file = path.join(this.c.vault,relative);
      if (!fs.existsSync(file)) return;
      const stat = fs.lstatSync(file);
      if (stat.isSymbolicLink()) return;
      if (stat.isDirectory()) for (const name of fs.readdirSync(file)) walk(relative + '/' + name);
      else if (stat.isFile()) {
        if (relative.endsWith('.delivery.json')) manifests.push(relative);
        else if (this.c.extensions.includes(path.extname(relative).toLowerCase())) sources.push(relative);
      }
    };
    for (const root of this.c.watch_roots) walk(root);
    return { sources: [...new Set(sources)].sort(), manifests: [...new Set(manifests)].sort() };
  }
  read(source) {
    const file = this.safe(source), before = fs.statSync(file);
    if (!before.isFile()) throw new Error('SOURCE_NOT_FILE');
    if (before.size > this.c.max_source_bytes) throw new Error('SOURCE_TOO_LARGE');
    const raw = fs.readFileSync(file), after = fs.statSync(file);
    if (before.size !== after.size || before.mtimeMs !== after.mtimeMs) throw new Error('SOURCE_WRITING');
    const text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(raw);
    if (text.includes('\0')) throw new Error('BINARY_TEXT_EXCLUDED');
    return { source, sha256: sha(raw), raw, text, size: after.size, mtime: after.mtimeMs,
      lines: text.replace(/^\uFEFF/, '').split(/\r?\n/) };
  }
  baseline() {
    if (this.meta('baseline_at')) throw new Error('BASELINE_ALREADY_EXISTS');
    const { sources, manifests } = this.files();
    let count = 0;
    this.db.exec('BEGIN');
    try {
      for (const source of [...sources, ...manifests]) {
        // Baseline hashes raw files without uploading or duplicating content.
        const digest = sha(fs.readFileSync(this.safe(source)));
        this.db.prepare('INSERT OR IGNORE INTO baseline VALUES (?,?)').run(source,digest); count++;
      }
      this.setMeta('baseline_at', this.now()); this.setMeta('baseline_files', count);
      this.db.exec('COMMIT');
    } catch (e) { this.db.exec('ROLLBACK'); throw e; }
    this.writeViews();
    return { baselined: count, api_calls: 0 };
  }
  enqueue(doc, { priority = 0, receipt = null, force = false } = {}) {
    if (!force && this.db.prepare('SELECT 1 FROM baseline WHERE source=? AND sha=?').get(doc.source,doc.sha256)) return;
    const id = sha(doc.source + '\0' + doc.sha256 + '\0' + this.rule), time = this.now();
    this.db.prepare(`INSERT OR IGNORE INTO jobs(id,source,sha,rules,status,priority,receipt,output,created,updated)
      VALUES (?,?,?,?,'pending',?,?,?,?,?)`).run(id,doc.source,doc.sha256,this.rule,priority,receipt,
        this.c.output + '/items/' + id, time,time);
    if (priority) this.db.prepare('UPDATE jobs SET priority=1,receipt=? WHERE id=?').run(receipt,id);
    return id;
  }
  scan() {
    if (!this.meta('baseline_at')) return this.baseline();
    this.warnings = [];
    const { sources, manifests } = this.files(), held = new Set(), ready = new Map();
    for (const source of manifests) {
      try {
        const raw = fs.readFileSync(this.safe(source));
        if (raw.length > 256000) throw new Error('MANIFEST_TOO_LARGE');
        const digest = sha(raw);
        if (this.db.prepare('SELECT 1 FROM baseline WHERE source=? AND sha=?').get(source,digest)) continue;
        const m = JSON.parse(raw.toString('utf8'));
        if (m.schema !== 'research-delivery/v1' || !Array.isArray(m.files) || !m.files.length || m.files.length > 100)
          throw new Error('INVALID_DELIVERY_SCHEMA');
        for (const row of m.files) if (typeof row.path === 'string') held.add(row.path);
        if (m.status !== 'complete') throw new Error('DELIVERY_NOT_COMPLETE');
        const verified = [];
        for (const row of m.files) {
          if (!this.allowed(row.path) || !this.c.extensions.includes(path.extname(row.path).toLowerCase()) ||
            !/^[a-f0-9]{64}$/.test(row.sha256)) throw new Error('INVALID_DELIVERY_FILE');
          const doc = this.read(row.path);
          if (doc.sha256 !== row.sha256) throw new Error('DELIVERY_SHA_MISMATCH');
          verified.push(doc);
        }
        for (const doc of verified) ready.set(doc.source,{doc,receipt:source});
      } catch (e) { this.warnings.push({ source, code: safeError(e) }); }
    }
    for (const {doc,receipt} of ready.values()) this.enqueue(doc,{priority:1,receipt});
    for (const source of sources) {
      if (held.has(source) || ready.has(source)) continue;
      try {
        const doc = this.read(source), old = this.db.prepare('SELECT * FROM observed WHERE source=?').get(source);
        const same = old && old.sha === doc.sha256 && old.size === doc.size && old.mtime === doc.mtime;
        const since = same ? old.stable_since : this.now(), observations = same ? old.observations + 1 : 1;
        this.db.prepare('INSERT OR REPLACE INTO observed VALUES (?,?,?,?,?,?)')
          .run(source,doc.sha256,doc.size,doc.mtime,since,observations);
        if (observations >= 2 && this.now() - since >= this.c.stable_seconds*1000 &&
          this.now() - doc.mtime >= this.c.stable_seconds*1000) this.enqueue(doc);
      } catch (e) {
        if (e.message === 'SOURCE_TOO_LARGE') {
          // An unchanged pre-existing oversized source is part of the no-upload
          // baseline, not a new actionable failure. Changed oversized versions
          // remain visible for explicit human selection instead of truncation.
          try {
            const digest = sha(fs.readFileSync(this.safe(source)));
            if (this.db.prepare('SELECT 1 FROM baseline WHERE source=? AND sha=?').get(source,digest)) continue;
          } catch {}
        }
        this.warnings.push({source,code:safeError(e)});
      }
    }
    this.setMeta('scan_at',this.now()); this.setMeta('scan_warnings',JSON.stringify(this.warnings)); this.writeViews();
    return this.status();
  }
  recover() {
    for (const job of this.db.prepare("SELECT * FROM jobs WHERE status IN ('processing','pending','failed')").all()) {
      if (this.complete(job)) this.success(job);
      else if (job.status === 'processing') {
        // Saved response may be reparsed without another API call after a crash.
        if (this.restoreResponse(job)) continue;
        this.db.prepare('UPDATE jobs SET status=?,updated=?,retry_at=?,error=? WHERE id=?')
          .run(job.attempts >= this.c.max_attempts ? 'failed' : 'pending', this.now(),
            job.attempts >= this.c.max_attempts ? null : this.now(), 'INTERRUPTED',job.id);
      }
    }
  }
  complete(job) {
    try {
      const dir = this.safe(job.output), marker = JSON.parse(fs.readFileSync(path.join(dir,'complete.json')));
      if (marker.source !== job.source || marker.sha256 !== job.sha || marker.rules !== job.rules) return false;
      for (const [file,digest] of Object.entries(marker.files)) {
        if (!['summary.md','summary.json','source.txt','rules.md','provider.json'].includes(file)) return false;
        if (sha(fs.readFileSync(path.join(dir,file))) !== digest) return false;
      }
      return ['summary.md','summary.json','source.txt','rules.md','provider.json'].every(f => marker.files[f]);
    } catch { return false; }
  }
  success(job) {
    this.db.prepare("UPDATE jobs SET status='success',updated=?,retry_at=NULL,error=NULL WHERE id=?").run(this.now(),job.id);
  }
  restoreResponse(job) {
    if (!job.attempt_dir) return false;
    try {
      const dir = path.join(this.c.state,job.attempt_dir);
      const stored = JSON.parse(fs.readFileSync(path.join(dir,'inputs.json')));
      if (stored.rules !== job.rules || job.rules !== this.rule) return false;
      const parsed = validate(JSON.parse(JSON.parse(fs.readFileSync(path.join(dir,'response.json'))).content),stored.sources);
      const index = stored.sources.findIndex(s => s.source === job.source && s.sha256 === job.sha);
      if (index < 0) return false;
      const receipt = path.join(dir,'provider.json');
      const meta = JSON.parse(fs.readFileSync(receipt));
      atomic(receipt,json({...meta,status:'validated',error:null,recovered_from_saved_response:true}));
      this.writeArtifact(job,stored.sources[index],parsed.documents[index],dir);
      return true;
    } catch { return false; }
  }
  writeArtifact(job, doc, generated, attemptDir) {
    const out = this.safe(job.output);
    const original = this.safe(doc.source);
    const link = uri(portable(path.relative(out,original)));
    const heading = {summary:'摘要',observations:'關鍵觀察（來源聲稱）',limitations:'限制／未確認',questions:'研究問題候選'};
    let markdown = `# PROPOSED／待審閱：${path.basename(doc.source)}\n\n來源：[${doc.source}](${link})\n\n來源 SHA256：\`${doc.sha256}\`\n\n規則 SHA256：\`${job.rules}\`\n\n整理只反映來源內容，未獨立驗證；不改科學完成判定。引用對照 [保留的原文版本](source.txt)（行號以此版本為準）。\n`;
    for (const key of Object.keys(heading)) {
      markdown += `\n## ${heading[key]}\n\n`;
      markdown += generated[key].map(i => `- ${escapeMD(i.text)}（L${i.line_start}–L${i.line_end}）\n  > ${escapeMD(i.quote)}\n`).join('\n') || '來源沒有明示／未提出。\n';
    }
    fs.mkdirSync(out,{recursive:true});
    atomic(path.join(out,'source.txt'),doc.text);
    atomic(path.join(out,'rules.md'),this.rules);
    atomic(path.join(out,'summary.json'),json({status:'PROPOSED',source:doc.source,sha256:doc.sha256,
      rules:job.rules,generated_at:new Date(this.now()).toISOString(),...generated}));
    atomic(path.join(out,'summary.md'),markdown);
    atomic(path.join(out,'provider.json'),fs.readFileSync(path.join(attemptDir,'provider.json')));
    const files = Object.fromEntries(['source.txt','rules.md','summary.json','summary.md','provider.json']
      .map(name => [name,sha(fs.readFileSync(path.join(out,name)))]));
    atomic(path.join(out,'complete.json'),json({source:doc.source,sha256:doc.sha256,rules:job.rules,files}));
    if (!this.complete(job)) throw new Error('ARTIFACT_INCOMPLETE');
    this.success(job);
  }
  async runBatch(signal) {
    if (signal?.aborted) return 0;
    const rows = this.db.prepare(`SELECT * FROM jobs WHERE rules=? AND attempts<? AND
      (status='pending' OR (status='failed' AND retry_at IS NOT NULL AND retry_at<=?))
      ORDER BY priority DESC,created,id LIMIT ?`).all(this.rule,this.c.max_attempts,this.now(),this.c.batch_files);
    const jobs = [], sources = []; let bytes = 0;
    for (const job of rows) {
      if (this.complete(job)) { this.success(job); continue; }
      if (this.restoreResponse(job)) continue;
      try {
        const doc = this.read(job.source);
        if (doc.sha256 !== job.sha) throw new Error('SOURCE_CHANGED');
        if (job.priority) {
          const m = JSON.parse(fs.readFileSync(this.safe(job.receipt),'utf8'));
          if (m.schema !== 'research-delivery/v1' || m.status !== 'complete' || !Array.isArray(m.files) ||
            !m.files.some(row=>row.path===job.source && row.sha256===job.sha))
            throw new Error('DELIVERY_NOT_COMPLETE');
          for (const row of m.files) {
            if (!this.allowed(row.path) || this.read(row.path).sha256 !== row.sha256)
              throw new Error('DELIVERY_SHA_MISMATCH');
          }
        }
        // Recheck stability at submission, including after a failed attempt.
        const observed = this.db.prepare('SELECT * FROM observed WHERE source=?').get(job.source);
        if (!job.priority && (!observed || observed.sha !== doc.sha256 || observed.mtime !== doc.mtime ||
          this.now()-observed.stable_since < this.c.stable_seconds*1000 ||
          this.now()-doc.mtime < this.c.stable_seconds*1000)) continue;
        if (bytes + doc.size > this.c.max_batch_bytes) break;
        bytes += doc.size; jobs.push(job);
        sources.push({source:doc.source,sha256:doc.sha256,text:doc.text,lines:doc.lines});
      } catch (e) {
        this.db.prepare("UPDATE jobs SET status='failed',error=?,retry_at=NULL,updated=? WHERE id=?")
          .run(safeError(e),this.now(),job.id);
      }
    }
    if (!jobs.length) { this.writeViews(); return 0; }
    const relativeDir = 'attempts/' + new Date(this.now()).toISOString().replaceAll(':','-') + '-' + crypto.randomUUID();
    const dir = path.join(this.c.state,relativeDir); fs.mkdirSync(dir,{recursive:true});
    atomic(path.join(dir,'inputs.json'),json({rules:this.rule,sources}));
    // Persist attempts before dispatch. Crash boundaries may repeat the API call.
    this.db.exec('BEGIN');
    try {
      for (const job of jobs) this.db.prepare("UPDATE jobs SET status='processing',attempts=attempts+1,attempt_dir=?,updated=? WHERE id=?")
        .run(relativeDir,this.now(),job.id);
      this.db.exec('COMMIT');
    } catch(e) { this.db.exec('ROLLBACK'); throw e; }
    this.writeViews();
    try {
      let generate = this.generate;
      if (!generate) {
        this.provider ??= await loadProvider(this.c);
        generate = this.provider.generate;
      }
      const content = await generate({ sources:sources.map(({text,...doc}) => doc), rules:this.rules,
        receipt:path.join(dir,'provider.json'),response:path.join(dir,'response.json'),atomic,signal });
      const receipt = path.join(dir,'provider.json');
      let parsed;
      try {
        parsed = validate(JSON.parse(content),sources);
        const meta = JSON.parse(fs.readFileSync(receipt));
        atomic(receipt,json({...meta,status:'validated',error:null}));
      } catch (e) {
        const meta = JSON.parse(fs.readFileSync(receipt));
        atomic(receipt,json({...meta,status:'parse_error',error:{code:safeError(e)}}));
        throw new Error('GENERATED_CONTENT_INVALID');
      }
      for (let i=0;i<jobs.length;i++) this.writeArtifact(jobs[i],sources[i],parsed.documents[i],dir);
    } catch (e) {
      if (!signal?.aborted) for (const job of jobs) {
        const current = this.db.prepare('SELECT * FROM jobs WHERE id=?').get(job.id);
        if (current.status === 'success') continue;
        const retry = current.attempts < this.c.max_attempts;
        this.db.prepare("UPDATE jobs SET status='failed',error=?,retry_at=?,updated=? WHERE id=?")
          .run(safeError(e),retry ? this.now()+this.c.retry_seconds*1000*2**(current.attempts-1) : null,this.now(),job.id);
      }
      if (!fs.existsSync(path.join(dir,'provider.json'))) atomic(path.join(dir,'provider.json'),json({
        status:'setup_error',usage:null,finish_reason:null,error:{code:safeError(e)}}));
    }
    this.writeViews();
    return jobs.length;
  }
  backfill(sources, limit) {
    if (!Number.isInteger(limit) || limit < 1 || limit > 20 || !sources.length || sources.length > limit)
      throw new Error('BACKFILL_LIMIT_REQUIRED_1_TO_20');
    for (const source of sources) {
      if (!this.allowed(source) || !this.c.extensions.includes(path.extname(source))) throw new Error('SOURCE_OUTSIDE_SCOPE');
      const doc = this.read(source);
      if (this.now()-doc.mtime < this.c.stable_seconds*1000) throw new Error('SOURCE_WRITING');
      this.db.prepare('INSERT OR REPLACE INTO observed VALUES (?,?,?,?,?,?)')
        .run(source,doc.sha256,doc.size,doc.mtime,this.now()-this.c.stable_seconds*1000,2);
      this.enqueue(doc,{force:true});
    }
    this.writeViews(); return {submitted:sources.length};
  }
  status() {
    return {baseline_at:this.meta('baseline_at'),baseline_files:Number(this.meta('baseline_files') || 0),
      scan_at:this.meta('scan_at'),rules:this.rule,
      counts:Object.fromEntries(this.db.prepare('SELECT status,count(*) n FROM jobs GROUP BY status').all().map(r=>[r.status,r.n])),
      warnings:this.warnings,scope:'local document organization; remote transfer and events are outside scope'};
  }
  writeViews() {
    const out = this.safe(this.c.output), status = this.status();
    const entries = this.db.prepare('SELECT * FROM jobs ORDER BY updated DESC LIMIT 300').all();
    atomic(path.join(out,'status.json'),json(status));
    const sourceLink = source => uri(portable(path.relative(out,this.safe(source))));
    let md = '# 背景研究整理：PROPOSED／待審閱\n\n所有摘要與問題均待審閱，不是科學接受或實驗授權。原文仍是內容權威。\n\n';
    md += `基線文件：${status.baseline_files}（未上傳）。狀態：${Object.entries(status.counts).map(([s,n])=>`${s} ${n}`).join('、') || '尚無新件'}。\n\n`;
    if (status.scan_at) md += `最後補查：${new Date(Number(status.scan_at)).toLocaleString('zh-TW',{timeZone:'Asia/Taipei'})}（Asia/Taipei）。\n\n`;
    md += '本服務自動補查已存入本地 Sources 的文字文件，不需要遠端發送事件。來源傳輸沿用既有方式。\n\n';
    md += '按來源或問題：使用 Obsidian 全文搜尋此目錄，或 CLI `find 查詢詞`（不需 Obsidian）。最近 300 個版本列於下方；所有版本仍保留。\n\n';
    for (const job of entries) {
      const summary = uri(portable(path.relative(out,this.safe(job.output)))+'/summary.md');
      md += `- ${job.status === 'success' ? `[待審阅摘要](${summary})` : job.status} — [${escapeMD(job.source)}](${sourceLink(job.source)}) · SHA ${job.sha.slice(0,12)} · 嘗試 ${job.attempts}${job.error?' · '+job.error:''}\n`;
    }
    if (this.warnings.length) md += '\n## 需判斷事項\n\n' + this.warnings.map(w=>`- ${escapeMD(w.source)}：${w.code}`).join('\n')+'\n';
    atomic(path.join(out,'Review Queue.md'),md);
  }
}
const escapeMD = text => text.replaceAll('\n',' ').replace(/[<>\[\]`]/g,'');
export function safeError(error) {
  const code = String(error?.message || 'INTERNAL_ERROR');
  return /^[A-Z_0-9]{1,80}$/.test(code) ? code : 'INTERNAL_ERROR';
}
export function validate(value,sources) {
  if (!value || !Array.isArray(value.documents) || value.documents.length !== sources.length)
    throw new Error('INVALID_DOCUMENT_COUNT');
  const documents = sources.map(source => {
    const matches = value.documents.filter(d=>d.source===source.source && d.sha256===source.sha256);
    if (matches.length !== 1) throw new Error('INVALID_SOURCE_IDENTITY');
    const doc = matches[0], clean = {source:doc.source,sha256:doc.sha256};
    for (const key of ['summary','observations','limitations','questions']) {
      if (!Array.isArray(doc[key]) || doc[key].length > 5 || (key==='summary' && !doc[key].length))
        throw new Error('INVALID_CONTENT_ARRAY');
      clean[key] = doc[key].map(item=> {
        if (!item || typeof item.text !== 'string' || !item.text.trim() || item.text.length > 2000 ||
          typeof item.quote !== 'string' || !item.quote.trim() || item.quote.length > 500 ||
          !Number.isInteger(item.line_start) || !Number.isInteger(item.line_end) ||
          item.line_start < 1 || item.line_end < item.line_start || item.line_end > source.lines.length ||
          !source.lines.slice(item.line_start-1,item.line_end).join('\n').includes(item.quote))
          throw new Error('INVALID_SOURCE_LOCATOR');
        return {text:item.text.trim(),line_start:item.line_start,line_end:item.line_end,quote:item.quote};
      });
    }
    return clean;
  });
  return {documents};
}
