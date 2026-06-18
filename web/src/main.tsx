import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Activity, ArchiveRestore, CheckCircle2, ChevronDown, Clipboard, Download, GitCompare, Menu, Play, RefreshCw, RotateCcw, ShieldCheck, UploadCloud, X } from 'lucide-react';
import './styles.css';

type Tab = 'status' | 'run' | 'review' | 'adopt' | 'rollback' | 'upstream';
type Lang = 'en' | 'zh';
type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };
type JsonObject = { [key: string]: JsonValue };

type RecentRun = {
  run_id?: string;
  status?: string;
  skill_name?: string;
  adoptable?: boolean;
  created_at?: string;
  run_dir?: string;
};
type StatusResponse = JsonObject & {
  hermes_home?: string;
  skills_count?: number;
  staging?: string | number;
  backups?: string | number;
  recent_runs?: RecentRun[];
};
type ReviewResponse = JsonObject & {
  run_id?: string;
  summary?: string;
  diff?: string;
  report?: string;
  gate?: string;
  candidate?: string;
  rejected?: string;
};
type RunResponse = JsonObject & { run_id?: string; status?: string; success?: boolean };

type RunForm = {
  skill: string;
  query: string;
  eval_file: string;
  lookback_days: number;
  limit: number;
  iterations: number;
  edit_budget: number;
  candidate_count: number;
  backend: string;
  optimizer_backend: string;
  target_backend: string;
  gate_mode: string;
  resume_run_id: string;
  allow_mock: boolean;
};

const dict = {
  en: {
    status: 'Status', run: 'Run', review: 'Review', adopt: 'Adopt', rollback: 'Rollback', upstream: 'Upstream',
    staged: 'staged-only WebUI', refresh: 'Refresh', home: 'HERMES_HOME override (read/run/review)', safety: 'Safety contracts',
    safetyText: 'Runs are staged-only. Adopt/Rollback require exact server-side confirmation and ignore HERMES_HOME overrides.',
    recent: 'Recent staged runs', rawStatus: 'Raw status', rawHint: 'Raw JSON is collapsed by default. Use it only for diagnostics.', show: 'Show raw', hide: 'Hide raw', copy: 'Copy', download: 'Download',
    runTitle: 'Run staged optimization', basic: 'Basic', advanced: 'Advanced', skill: 'Skill', query: 'Query/session search', evalFile: 'Eval file', backend: 'Backend', optimizer: 'Optimizer', target: 'Target',
    gateMode: 'Gate mode', lookback: 'Lookback days', gateHelp: 'soft/mixed are review-only/non-production adoption semantics; adoption is still a separate guarded action.', allowMock: 'Allow mock fallback', resume: 'Resume run ID', runButton: 'Run staged optimization', runNote: 'No skill is adopted by this action.',
    reviewControls: 'Review controls', runIdLatest: 'Run ID (blank = latest)', reviewSelected: 'Review selected run', summary: 'Summary',
    adoptTitle: 'Adopt staged proposal', rollbackTitle: 'Rollback adopted run', writebackWarn: 'Writes only to the active Hermes profile; HERMES_HOME override is ignored.', confirmation: 'Confirmation', force: 'Force sha guard override', forceHelp: 'Only use force when you have manually reviewed the integrity mismatch.',
    upstreamCopy: 'Read-only status/parity use home override. Update ignores home and uses the active canonical profile.', fetchOnly: 'Fetch only', upstreamStatus: 'Upstream status', parity: 'Parity status', update: 'Update/fetch pinned upstream', result: 'Result',
    noRuns: 'No staged runs found.', openMenu: 'Open menu', close: 'Close', language: 'Language', statusCol: 'Status', adoptable: 'Adoptable', created: 'Created', runId: 'Run ID', adoptExact: 'Type the exact confirmation to enable.',
  },
  zh: {
    status: '状态', run: '运行', review: '审查', adopt: '采用', rollback: '回滚', upstream: '上游',
    staged: '仅暂存 WebUI', refresh: '刷新', home: 'HERMES_HOME 覆盖（读取/运行/审查）', safety: '安全约束',
    safetyText: '运行只写入暂存区。采用/回滚需要服务端精确确认，并忽略 HERMES_HOME 覆盖。',
    recent: '最近暂存运行', rawStatus: '原始状态', rawHint: '原始 JSON 默认折叠，仅用于诊断。', show: '显示原始数据', hide: '隐藏原始数据', copy: '复制', download: '下载',
    runTitle: '运行暂存优化', basic: '基础', advanced: '高级', skill: 'Skill', query: '查询/session 搜索', evalFile: '评测文件', backend: '后端', optimizer: '优化器', target: '目标',
    gateMode: '门禁模式', lookback: '回溯天数', gateHelp: 'soft/mixed 表示仅审查/非生产采用语义；采用仍需单独受保护操作。', allowMock: '允许 mock fallback', resume: '恢复运行 ID', runButton: '运行暂存优化', runNote: '此操作不会采用 skill。',
    reviewControls: '审查控制', runIdLatest: '运行 ID（留空 = 最新）', reviewSelected: '审查所选运行', summary: '摘要',
    adoptTitle: '采用暂存方案', rollbackTitle: '回滚已采用运行', writebackWarn: '只写入当前激活 Hermes profile；HERMES_HOME 覆盖会被忽略。', confirmation: '确认文本', force: '强制覆盖 sha 保护', forceHelp: '仅在已人工审查完整性不匹配时使用 force。',
    upstreamCopy: '只读状态/对齐检查使用 home 覆盖。更新忽略 home 并使用当前 canonical profile。', fetchOnly: '仅 fetch', upstreamStatus: '上游状态', parity: '对齐状态', update: '更新/fetch 固定上游', result: '结果',
    noRuns: '未找到暂存运行。', openMenu: '打开菜单', close: '关闭', language: '语言', statusCol: '状态', adoptable: '可采用', created: '创建时间', runId: '运行 ID', adoptExact: '输入精确确认文本后才可启用。',
  },
} satisfies Record<Lang, Record<string, string>>;

const tabIcons: Record<Tab, React.ElementType> = { status: Activity, run: Play, review: GitCompare, adopt: CheckCircle2, rollback: ArchiveRestore, upstream: UploadCloud };
const tabs: Tab[] = ['status', 'run', 'review', 'adopt', 'rollback', 'upstream'];

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : typeof err === 'string' ? err : 'Unknown error';
}
async function api<T>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(url, { ...options, headers: { 'content-type': 'application/json', ...(options?.headers || {}) }, cache: 'no-store' });
  const data: unknown = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data && typeof data === 'object' && 'detail' in data ? String((data as { detail?: unknown }).detail) : `${res.status} ${res.statusText}`;
    throw new Error(detail);
  }
  return data as T;
}
function safeJson(value: unknown): string { return JSON.stringify(value ?? {}, null, 2); }
function shortRunId(id?: string): string { return id && id.length > 28 ? `${id.slice(0, 12)}…${id.slice(-10)}` : id || '—'; }
function summaryResult(kind: string, out: JsonObject): string {
  const parts = [`${kind} complete`];
  if (typeof out.run_id === 'string') parts.push(`run ${shortRunId(out.run_id)}`);
  if (typeof out.success === 'boolean') parts.push(`success=${out.success}`);
  if (typeof out.status === 'string') parts.push(`status=${out.status}`);
  return parts.join(' · ');
}

const Button = React.forwardRef<HTMLButtonElement, React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'default' | 'destructive' | 'secondary' | 'ghost' }>(function Button({ className = '', variant = 'default', ...props }, ref) { return <button ref={ref} className={`btn btn-${variant} ${className}`} {...props} />; });
function Card({ children, className = '' }: React.PropsWithChildren<{ className?: string }>) { return <section className={`card ${className}`}>{children}</section>; }
function Input(props: React.InputHTMLAttributes<HTMLInputElement>) { return <input className="input" {...props} />; }
function Select(props: React.SelectHTMLAttributes<HTMLSelectElement>) { return <select className="input select" {...props} />; }
function Badge({ children, tone = 'default' }: React.PropsWithChildren<{ tone?: 'default' | 'ok' | 'warn' }>) { return <span className={`badge badge-${tone}`}>{children}</span>; }
function Alert({ children, tone = 'default' }: React.PropsWithChildren<{ tone?: 'default' | 'warn' }>) { return <div className={`alert alert-${tone}`}>{children}</div>; }
function Field({ label, children }: React.PropsWithChildren<{ label: string }>) { return <label className="field"><span>{label}</span>{children}</label>; }
function CodeBlock({ value, placeholder = 'No data yet.' }: { value?: string; placeholder?: string }) { return <div className="scroll-area"><pre>{value || placeholder}</pre></div>; }
function RawBlock({ title, value, lang }: { title: string; value: unknown; lang: Lang }) {
  const [open, setOpen] = useState(false);
  const text = safeJson(value);
  const download = () => {
    const url = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
    const a = document.createElement('a'); a.href = url; a.download = `${title.toLowerCase().replace(/\s+/g, '-')}.json`; a.click(); URL.revokeObjectURL(url);
  };
  return <Card><div className="raw-head"><div><h2>{title}</h2><p>{dict[lang].rawHint}</p></div><Button variant="secondary" onClick={() => setOpen(!open)}><ChevronDown size={16}/>{open ? dict[lang].hide : dict[lang].show}</Button></div>{open && <><div className="button-row"><Button variant="ghost" onClick={() => navigator.clipboard?.writeText(text)}><Clipboard size={16}/>{dict[lang].copy}</Button><Button variant="ghost" onClick={download}><Download size={16}/>{dict[lang].download}</Button></div><CodeBlock value={text}/></>}</Card>;
}

function App() {
  const [tab, setTab] = useState<Tab>('status');
  const [sheet, setSheet] = useState(false);
  const [home, setHome] = useState('');
  const [lang, setLang] = useState<Lang>('en');
  const t = dict[lang];
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [review, setReview] = useState<ReviewResponse | null>(null);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  const [runForm, setRunForm] = useState<RunForm>({ skill: '', query: '', eval_file: '', lookback_days: 14, limit: 50, iterations: 1, edit_budget: 3, candidate_count: 1, backend: 'auto', optimizer_backend: '', target_backend: '', gate_mode: 'soft', resume_run_id: '', allow_mock: false });
  const [reviewRunId, setReviewRunId] = useState('');
  const [adoptRunId, setAdoptRunId] = useState('');
  const [adoptConfirm, setAdoptConfirm] = useState('');
  const [adoptForce, setAdoptForce] = useState(false);
  const [rollbackRunId, setRollbackRunId] = useState('');
  const [rollbackConfirm, setRollbackConfirm] = useState('');
  const [rollbackForce, setRollbackForce] = useState(false);
  const [upstream, setUpstream] = useState<JsonObject | null>(null);
  const [fetchOnly, setFetchOnly] = useState(true);
  const menuButtonRef = useRef<HTMLButtonElement>(null);

  const homeQS = useMemo(() => home ? `?home=${encodeURIComponent(home)}` : '', [home]);
  const refreshStatus = async () => { setBusy(true); setMessage(''); try { setStatus(await api<StatusResponse>(`/api/status${homeQS}`)); } catch (e: unknown) { setMessage(errorMessage(e)); } finally { setBusy(false); } };
  const loadReview = async (rid = reviewRunId) => { setBusy(true); setMessage(''); try { const qs = new URLSearchParams(); if (rid) qs.set('run_id', rid); if (home) qs.set('home', home); const data = await api<ReviewResponse>(`/api/review?${qs.toString()}`); setReview(data); if (data.run_id) { setAdoptRunId(data.run_id); setRollbackRunId(data.run_id); } } catch (e: unknown) { setMessage(errorMessage(e)); } finally { setBusy(false); } };
  useEffect(() => { refreshStatus(); if ('serviceWorker' in navigator && window.isSecureContext) navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => undefined); }, []);
  useEffect(() => { if (!sheet) return; const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') { setSheet(false); menuButtonRef.current?.focus(); } }; document.addEventListener('keydown', onKey); return () => document.removeEventListener('keydown', onKey); }, [sheet]);

  const selectTab = (next: Tab) => { setTab(next); setSheet(false); menuButtonRef.current?.focus(); };
  const nav = <nav className="sidebar-nav" aria-label="Primary">{tabs.map(id => { const Icon = tabIcons[id]; return <button key={id} className={tab === id ? 'active' : ''} onClick={() => selectTab(id)}><Icon size={16}/>{t[id]}</button>; })}</nav>;
  const topControls = <div className="top-controls"><Select value={lang} onChange={e=>setLang(e.target.value as Lang)} aria-label={t.language}><option value="en">English</option><option value="zh">中文</option></Select><Input value={home} onChange={e=>setHome(e.target.value)} placeholder={t.home} aria-label={t.home}/><Button onClick={refreshStatus} disabled={busy}><RefreshCw size={16}/>{t.refresh}</Button></div>;

  const run = async () => { setBusy(true); setMessage(''); try { const out = await api<RunResponse>('/api/run', { method: 'POST', body: JSON.stringify({ ...runForm, home }) }); setMessage(`Staged run complete · ${shortRunId(out.run_id)} · no skill was adopted.`); await refreshStatus(); if (out.run_id) { setReviewRunId(out.run_id); await loadReview(out.run_id); setTab('review'); } } catch (e: unknown) { setMessage(errorMessage(e)); } finally { setBusy(false); } };
  const doConfirm = async (kind: 'adopt' | 'rollback') => { setBusy(true); setMessage(''); try { const body = kind === 'adopt' ? { run_id: adoptRunId, confirmation: adoptConfirm, force: adoptForce, home } : { run_id: rollbackRunId, confirmation: rollbackConfirm, force: rollbackForce, home }; const out = await api<JsonObject>(`/api/${kind}`, { method: 'POST', body: JSON.stringify(body) }); setMessage(summaryResult(kind, out)); await refreshStatus(); } catch (e: unknown) { setMessage(errorMessage(e)); } finally { setBusy(false); } };
  const loadUpstream = async (kind: 'status' | 'parity' | 'update') => { setBusy(true); setMessage(''); try { const out = kind === 'update' ? await api<JsonObject>('/api/upstream/update', { method: 'POST', body: JSON.stringify({ fetch_only: fetchOnly, home }) }) : await api<JsonObject>(`/api/upstream/${kind}${homeQS}`); setUpstream(out); } catch (e: unknown) { setMessage(errorMessage(e)); } finally { setBusy(false); } };

  const adoptEnabled = !!adoptRunId && adoptConfirm.trim() === `ADOPT ${adoptRunId}`;
  const rollbackEnabled = !!rollbackRunId && rollbackConfirm.trim() === `ROLLBACK ${rollbackRunId}`;

  return <div className="app-shell">
    <header className="topbar"><div className="brand"><Button ref={menuButtonRef} variant="ghost" className="mobile-menu" onClick={() => setSheet(true)} aria-label={t.openMenu}><Menu size={18}/></Button><h1>Hermes SkillOpt</h1><Badge tone="warn">{t.staged}</Badge></div>{topControls}</header>
    <div className="body-grid"><aside className="sidebar"><div className="sidebar-title"><ShieldCheck size={16}/> {t.safety}</div>{nav}<Alert>{t.safetyText}</Alert></aside>
      <main className="main-panel">{message && <Alert tone={message.includes('complete') ? 'default' : 'warn'}>{message}</Alert>}
        {tab === 'status' && <div className="dashboard-grid"><Card className="wide"><h2>{t.status}</h2><div className="metric-grid"><div><b>HERMES_HOME</b><span>{status?.hermes_home || 'unknown'}</span></div><div><b>Skills</b><span>{status?.skills_count ?? '—'}</span></div><div><b>Staging</b><span>{status?.staging ?? '—'}</span></div><div><b>Backups</b><span>{status?.backups ?? '—'}</span></div></div></Card><Card className="wide"><h2>{t.recent}</h2><div className="run-cards">{(status?.recent_runs || []).length ? (status?.recent_runs || []).map((r) => <button className="run-card" key={r.run_id} onClick={()=>{setReviewRunId(r.run_id || ''); setTab('review'); loadReview(r.run_id || '');}}><div><strong title={r.run_id}>{shortRunId(r.run_id)}</strong><span>{r.skill_name || 'unknown-skill'} · {r.created_at || '—'}</span></div><div className="run-badges"><Badge>{r.status || 'unknown'}</Badge><Badge tone={r.adoptable ? 'ok' : 'warn'}>{t.adoptable}: {r.adoptable ? 'yes' : 'no'}</Badge></div></button>) : <p className="muted">{t.noRuns}</p>}</div><div className="table-wrap"><table><thead><tr><th>{t.runId}</th><th>{t.statusCol}</th><th>{t.skill}</th><th>{t.adoptable}</th><th>{t.created}</th></tr></thead><tbody>{(status?.recent_runs || []).map((r) => <tr key={r.run_id} onClick={()=>{setReviewRunId(r.run_id || ''); setTab('review'); loadReview(r.run_id || '');}}><td className="breakable">{r.run_id}</td><td>{r.status}</td><td className="breakable">{r.skill_name}</td><td>{String(r.adoptable)}</td><td className="breakable">{r.created_at}</td></tr>)}</tbody></table></div></Card><RawBlock title={t.rawStatus} value={status} lang={lang}/></div>}
        {tab === 'run' && <div className="cockpit-grid"><Card className="wide run-form"><div className="card-head"><div><h2>{t.runTitle}</h2><p>{t.runNote}</p></div><Badge tone="warn">review-only</Badge></div><h3>{t.basic}</h3><div className="form-grid"><Field label={t.skill}><Input value={runForm.skill} onChange={e=>setRunForm({...runForm, skill:e.target.value})}/></Field><Field label={t.query}><Input value={runForm.query} onChange={e=>setRunForm({...runForm, query:e.target.value})}/></Field><Field label={t.evalFile}><Input value={runForm.eval_file} onChange={e=>setRunForm({...runForm, eval_file:e.target.value})}/></Field><Field label={t.backend}><Select value={runForm.backend} onChange={e=>setRunForm({...runForm, backend:e.target.value})}><option>auto</option><option>hermes</option><option>mock</option></Select></Field></div><details open={advanced} onToggle={e=>setAdvanced(e.currentTarget.open)} className="advanced"><summary>{t.advanced}</summary><div className="form-grid"><Field label={t.optimizer}><Select value={runForm.optimizer_backend} onChange={e=>setRunForm({...runForm, optimizer_backend:e.target.value})}><option value="">default</option><option>auto</option><option>hermes</option><option>mock</option></Select></Field><Field label={t.target}><Select value={runForm.target_backend} onChange={e=>setRunForm({...runForm, target_backend:e.target.value})}><option value="">default</option><option>auto</option><option>replay</option><option>sandbox</option><option>hermes</option><option>mock</option></Select></Field><Field label={t.gateMode}><Select value={runForm.gate_mode} onChange={e=>setRunForm({...runForm, gate_mode:e.target.value})}><option>soft</option><option>mixed</option><option>strict</option></Select></Field><Field label={t.lookback ?? 'Lookback'}><Input type="number" value={runForm.lookback_days} onChange={e=>setRunForm({...runForm, lookback_days:Number(e.target.value)})}/></Field><Field label="Limit"><Input type="number" value={runForm.limit} onChange={e=>setRunForm({...runForm, limit:Number(e.target.value)})}/></Field><Field label="Iterations"><Input type="number" value={runForm.iterations} onChange={e=>setRunForm({...runForm, iterations:Number(e.target.value)})}/></Field><Field label="Edit budget"><Input type="number" value={runForm.edit_budget} onChange={e=>setRunForm({...runForm, edit_budget:Number(e.target.value)})}/></Field><Field label="Candidates"><Input type="number" value={runForm.candidate_count} onChange={e=>setRunForm({...runForm, candidate_count:Number(e.target.value)})}/></Field><Field label={t.resume}><Input value={runForm.resume_run_id} onChange={e=>setRunForm({...runForm, resume_run_id:e.target.value})}/></Field></div><Alert>{t.gateHelp}</Alert><label className="check"><input type="checkbox" checked={runForm.allow_mock} onChange={e=>setRunForm({...runForm, allow_mock:e.target.checked})}/> {t.allowMock}</label></details><div className="button-row sticky-actions"><Button onClick={run} disabled={busy}><Play size={16}/>{t.runButton}</Button></div></Card></div>}
        {tab === 'review' && <div className="review-grid"><Card><h2>{t.reviewControls}</h2><Field label={t.runIdLatest}><Input value={reviewRunId} onChange={e=>setReviewRunId(e.target.value)}/></Field><Button onClick={()=>loadReview()}>{t.reviewSelected}</Button></Card><Card className="wide"><h2>{t.summary}</h2><CodeBlock value={review?.summary}/></Card><Card className="wide"><h2>diff.patch</h2><CodeBlock value={review?.diff}/></Card><Card><h2>report.md</h2><CodeBlock value={review?.report}/></Card><Card><h2>gate/candidate summary</h2><CodeBlock value={review?.gate}/></Card><Card><h2>proposed_SKILL.md</h2><CodeBlock value={review?.candidate}/></Card><Card><h2>rejected_edits.jsonl</h2><CodeBlock value={review?.rejected}/></Card></div>}
        {tab === 'adopt' && <div className="cockpit-grid"><Card><h2>{t.adoptTitle}</h2><Alert tone="warn">{t.writebackWarn}</Alert><Field label={t.runId}><Input value={adoptRunId} onChange={e=>setAdoptRunId(e.target.value)}/></Field><Field label={t.confirmation}><Input value={adoptConfirm} onChange={e=>setAdoptConfirm(e.target.value)} placeholder={`ADOPT ${adoptRunId || '<run_id>'}`}/></Field><p className="muted">{t.adoptExact}</p><label className="check"><input type="checkbox" checked={adoptForce} onChange={e=>setAdoptForce(e.target.checked)}/> {t.force}</label><p className="muted">{t.forceHelp}</p><Button variant="destructive" disabled={!adoptEnabled || busy} onClick={()=>doConfirm('adopt')}>{t.adopt}</Button></Card></div>}
        {tab === 'rollback' && <div className="cockpit-grid"><Card><h2>{t.rollbackTitle}</h2><Alert tone="warn">{t.writebackWarn}</Alert><Field label={t.runId}><Input value={rollbackRunId} onChange={e=>setRollbackRunId(e.target.value)}/></Field><Field label={t.confirmation}><Input value={rollbackConfirm} onChange={e=>setRollbackConfirm(e.target.value)} placeholder={`ROLLBACK ${rollbackRunId || '<run_id>'}`}/></Field><p className="muted">{t.adoptExact}</p><label className="check"><input type="checkbox" checked={rollbackForce} onChange={e=>setRollbackForce(e.target.checked)}/> {t.force}</label><p className="muted">{t.forceHelp}</p><Button variant="destructive" disabled={!rollbackEnabled || busy} onClick={()=>doConfirm('rollback')}>{t.rollback}</Button></Card></div>}
        {tab === 'upstream' && <div className="cockpit-grid"><Card><h2>{t.upstream}</h2><Alert>{t.upstreamCopy}</Alert><label className="check"><input type="checkbox" checked={fetchOnly} onChange={e=>setFetchOnly(e.target.checked)}/> {t.fetchOnly}</label><div className="button-row"><Button onClick={()=>loadUpstream('status')}>{t.upstreamStatus}</Button><Button onClick={()=>loadUpstream('parity')}>{t.parity}</Button><Button variant="secondary" onClick={()=>loadUpstream('update')}>{t.update}</Button></div></Card><RawBlock title={t.result} value={upstream} lang={lang}/></div>}
      </main></div>
    {sheet && <div className="sheet" onMouseDown={(e)=>{ if (e.target === e.currentTarget) { setSheet(false); menuButtonRef.current?.focus(); } }}><div className="sheet-panel" role="dialog" aria-modal="true" aria-label="Navigation"><div className="sheet-head"><strong>Hermes SkillOpt</strong><Button variant="ghost" onClick={()=>{setSheet(false); menuButtonRef.current?.focus();}} aria-label={t.close}><X size={18}/>{t.close}</Button></div>{nav}<div className="sheet-controls">{topControls}</div></div></div>}
  </div>;
}

createRoot(document.getElementById('root')!).render(<App />);
