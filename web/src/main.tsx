import React, { useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Activity, ArchiveRestore, CheckCircle2, GitCompare, Menu, Play, RefreshCw, RotateCcw, ShieldCheck, UploadCloud, X } from 'lucide-react';
import './styles.css';

type Tab = 'status' | 'run' | 'review' | 'adopt' | 'rollback' | 'upstream';
type Json = Record<string, any>;

const tabs: { id: Tab; label: string; icon: React.ElementType }[] = [
  { id: 'status', label: 'Status', icon: Activity },
  { id: 'run', label: 'Run', icon: Play },
  { id: 'review', label: 'Review', icon: GitCompare },
  { id: 'adopt', label: 'Adopt', icon: CheckCircle2 },
  { id: 'rollback', label: 'Rollback', icon: ArchiveRestore },
  { id: 'upstream', label: 'Upstream', icon: UploadCloud },
];

async function api<T>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(url, { ...options, headers: { 'content-type': 'application/json', ...(options?.headers || {}) }, cache: 'no-store' });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `${res.status} ${res.statusText}`);
  return data as T;
}

function Button({ className = '', variant = 'default', ...props }: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'default' | 'destructive' | 'secondary' | 'ghost' }) {
  return <button className={`btn btn-${variant} ${className}`} {...props} />;
}
function Card({ children, className = '' }: React.PropsWithChildren<{ className?: string }>) { return <section className={`card ${className}`}>{children}</section>; }
function Input(props: React.InputHTMLAttributes<HTMLInputElement>) { return <input className="input" {...props} />; }
function Select(props: React.SelectHTMLAttributes<HTMLSelectElement>) { return <select className="input select" {...props} />; }
function Badge({ children, tone = 'default' }: React.PropsWithChildren<{ tone?: 'default' | 'ok' | 'warn' }>) { return <span className={`badge badge-${tone}`}>{children}</span>; }
function Alert({ children, tone = 'default' }: React.PropsWithChildren<{ tone?: 'default' | 'warn' }>) { return <div className={`alert alert-${tone}`}>{children}</div>; }
function ScrollArea({ children, className = '' }: React.PropsWithChildren<{ className?: string }>) { return <div className={`scroll-area ${className}`}>{children}</div>; }
function Field({ label, children }: React.PropsWithChildren<{ label: string }>) { return <label className="field"><span>{label}</span>{children}</label>; }
function CodeBlock({ value, placeholder = 'No data yet.' }: { value?: string; placeholder?: string }) { return <ScrollArea><pre>{value || placeholder}</pre></ScrollArea>; }
function JsonBlock({ value }: { value?: any }) { return <CodeBlock value={typeof value === 'string' ? value : JSON.stringify(value ?? {}, null, 2)} />; }

function App() {
  const [tab, setTab] = useState<Tab>('status');
  const [sheet, setSheet] = useState(false);
  const [home, setHome] = useState('');
  const [lang, setLang] = useState('en');
  const [status, setStatus] = useState<Json | null>(null);
  const [review, setReview] = useState<Json | null>(null);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [runForm, setRunForm] = useState({ skill: '', query: '', eval_file: '', lookback_days: 14, limit: 50, iterations: 1, edit_budget: 3, candidate_count: 1, backend: 'auto', optimizer_backend: '', target_backend: '', gate_mode: 'soft', resume_run_id: '', allow_mock: false });
  const [reviewRunId, setReviewRunId] = useState('');
  const [adoptRunId, setAdoptRunId] = useState('');
  const [adoptConfirm, setAdoptConfirm] = useState('');
  const [adoptForce, setAdoptForce] = useState(false);
  const [rollbackRunId, setRollbackRunId] = useState('');
  const [rollbackConfirm, setRollbackConfirm] = useState('');
  const [rollbackForce, setRollbackForce] = useState(false);
  const [upstream, setUpstream] = useState<Json | null>(null);
  const [fetchOnly, setFetchOnly] = useState(true);

  const homeQS = useMemo(() => home ? `?home=${encodeURIComponent(home)}` : '', [home]);
  const refreshStatus = async () => { setBusy(true); setMessage(''); try { setStatus(await api<Json>(`/api/status${homeQS}`)); } catch (e: any) { setMessage(e.message); } finally { setBusy(false); } };
  const loadReview = async (rid = reviewRunId) => { setBusy(true); setMessage(''); try { const qs = new URLSearchParams(); if (rid) qs.set('run_id', rid); if (home) qs.set('home', home); const data = await api<Json>(`/api/review?${qs.toString()}`); setReview(data); if (data.run_id) { setAdoptRunId(data.run_id); setRollbackRunId(data.run_id); } } catch (e: any) { setMessage(e.message); } finally { setBusy(false); } };
  useEffect(() => { refreshStatus(); if ('serviceWorker' in navigator && window.isSecureContext) navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => {}); }, []);
  const nav = <nav className="sidebar-nav">{tabs.map(t => { const Icon = t.icon; return <button key={t.id} className={tab === t.id ? 'active' : ''} onClick={() => { setTab(t.id); setSheet(false); }}><Icon size={16}/>{t.label}</button>; })}</nav>;
  const topControls = <div className="top-controls"><Select value={lang} onChange={e=>setLang(e.target.value)} aria-label="Language"><option value="en">English</option><option value="zh">中文</option></Select><Input value={home} onChange={e=>setHome(e.target.value)} placeholder="HERMES_HOME override (read/run/review)" aria-label="HERMES_HOME override"/><Button onClick={refreshStatus} disabled={busy}><RefreshCw size={16}/>Refresh</Button></div>;

  const run = async () => { setBusy(true); setMessage(''); try { const out = await api<Json>('/api/run', { method: 'POST', body: JSON.stringify({ ...runForm, home }) }); setMessage(`Staged run complete: ${out.run_id || 'unknown run'}. No skill was adopted.`); await refreshStatus(); if (out.run_id) { setReviewRunId(out.run_id); await loadReview(out.run_id); setTab('review'); } } catch (e: any) { setMessage(e.message); } finally { setBusy(false); } };
  const doConfirm = async (kind: 'adopt' | 'rollback') => { setBusy(true); setMessage(''); try { const body = kind === 'adopt' ? { run_id: adoptRunId, confirmation: adoptConfirm, force: adoptForce, home } : { run_id: rollbackRunId, confirmation: rollbackConfirm, force: rollbackForce, home }; const out = await api<Json>(`/api/${kind}`, { method: 'POST', body: JSON.stringify(body) }); setMessage(`${kind} complete: ${JSON.stringify(out)}`); await refreshStatus(); } catch (e: any) { setMessage(e.message); } finally { setBusy(false); } };
  const loadUpstream = async (kind: 'status' | 'parity' | 'update') => { setBusy(true); setMessage(''); try { const out = kind === 'update' ? await api<Json>('/api/upstream/update', { method: 'POST', body: JSON.stringify({ fetch_only: fetchOnly, home }) }) : await api<Json>(`/api/upstream/${kind}${homeQS}`); setUpstream(out); } catch (e: any) { setMessage(e.message); } finally { setBusy(false); } };

  return <div className="app-shell">
    <header className="topbar"><div className="brand"><Button variant="ghost" className="mobile-menu" onClick={() => setSheet(true)} aria-label="Open menu"><Menu size={18}/></Button><h1>Hermes SkillOpt</h1><Badge tone="warn">staged-only WebUI</Badge></div>{topControls}</header>
    <div className="body-grid"><aside className="sidebar"><div className="sidebar-title"><ShieldCheck size={16}/> Safety contracts</div>{nav}<Alert>Runs are staged-only. Adopt/Rollback require exact server-side confirmation and ignore HERMES_HOME overrides.</Alert></aside>
      <main className="main-panel">{message && <Alert tone={message.includes('complete') ? 'default' : 'warn'}>{message}</Alert>}
        {tab === 'status' && <div className="dashboard-grid"><Card className="wide"><h2>Status</h2><div className="metric-grid"><div><b>HERMES_HOME</b><span>{status?.hermes_home || 'unknown'}</span></div><div><b>Skills</b><span>{status?.skills_count ?? '—'}</span></div><div><b>Staging</b><span>{status?.staging ?? '—'}</span></div><div><b>Backups</b><span>{status?.backups ?? '—'}</span></div></div></Card><Card className="wide"><h2>Recent staged runs</h2><div className="table-wrap"><table><thead><tr><th>Run ID</th><th>Status</th><th>Skill</th><th>Adoptable</th><th>Created</th></tr></thead><tbody>{(status?.recent_runs || []).map((r: Json) => <tr key={r.run_id} onClick={()=>{setReviewRunId(r.run_id); setTab('review'); loadReview(r.run_id);}}><td>{r.run_id}</td><td>{r.status}</td><td>{r.skill_name}</td><td>{String(r.adoptable)}</td><td>{r.created_at}</td></tr>)}</tbody></table></div></Card><Card><h2>Raw status</h2><JsonBlock value={status}/></Card></div>}
        {tab === 'run' && <div className="cockpit-grid"><Card className="wide"><h2>Run staged optimization</h2><div className="form-grid"><Field label="Skill"><Input value={runForm.skill} onChange={e=>setRunForm({...runForm, skill:e.target.value})}/></Field><Field label="Query/session search"><Input value={runForm.query} onChange={e=>setRunForm({...runForm, query:e.target.value})}/></Field><Field label="Eval file"><Input value={runForm.eval_file} onChange={e=>setRunForm({...runForm, eval_file:e.target.value})}/></Field><Field label="Backend"><Select value={runForm.backend} onChange={e=>setRunForm({...runForm, backend:e.target.value})}><option>auto</option><option>hermes</option><option>mock</option></Select></Field><Field label="Optimizer"><Select value={runForm.optimizer_backend} onChange={e=>setRunForm({...runForm, optimizer_backend:e.target.value})}><option value="">default</option><option>auto</option><option>hermes</option><option>mock</option></Select></Field><Field label="Target"><Select value={runForm.target_backend} onChange={e=>setRunForm({...runForm, target_backend:e.target.value})}><option value="">default</option><option>auto</option><option>replay</option><option>sandbox</option><option>scorecard</option><option>live-readonly</option></Select></Field><Field label="Gate"><Select value={runForm.gate_mode} onChange={e=>setRunForm({...runForm, gate_mode:e.target.value})}><option>soft</option><option>hard</option><option>mixed</option><option>strict</option></Select></Field>{(['lookback_days','limit','iterations','edit_budget','candidate_count'] as const).map(k=><Field key={k} label={k.replace(/_/g,' ')}><Input type="number" value={runForm[k]} onChange={e=>setRunForm({...runForm, [k]: Number(e.target.value)})}/></Field>)}<Field label="Resume run ID"><Input value={runForm.resume_run_id} onChange={e=>setRunForm({...runForm, resume_run_id:e.target.value})}/></Field><label className="check"><input type="checkbox" checked={runForm.allow_mock} onChange={e=>setRunForm({...runForm, allow_mock:e.target.checked})}/> Allow mock fallback</label></div><Button onClick={run} disabled={busy}><Play size={16}/>Run full cycle (staged only)</Button></Card><Card><h2>Run guard</h2><Alert>No automatic adoption. Server calls full_run with auto_adopt=False and force=False.</Alert></Card></div>}
        {tab === 'review' && <div className="review-grid"><Card><h2>Review controls</h2><Field label="Run ID (blank = latest)"><Input value={reviewRunId} onChange={e=>setReviewRunId(e.target.value)}/></Field><Button onClick={()=>loadReview()}>Review selected run</Button></Card><Card className="wide"><h2>Summary</h2><CodeBlock value={review?.summary}/></Card><Card className="wide"><h2>diff.patch</h2><CodeBlock value={review?.diff}/></Card><Card><h2>report.md</h2><CodeBlock value={review?.report}/></Card><Card><h2>gate/candidate summary</h2><CodeBlock value={review?.gate}/></Card><Card><h2>proposed_SKILL.md</h2><CodeBlock value={review?.candidate}/></Card><Card><h2>rejected_edits.jsonl</h2><CodeBlock value={review?.rejected}/></Card></div>}
        {tab === 'adopt' && <div className="cockpit-grid"><Card><h2>Adopt staged proposal</h2><Alert tone="warn">Writes only to the active Hermes profile; HERMES_HOME override is ignored.</Alert><Field label="Run ID"><Input value={adoptRunId} onChange={e=>setAdoptRunId(e.target.value)}/></Field><Field label="Confirmation"><Input value={adoptConfirm} onChange={e=>setAdoptConfirm(e.target.value)} placeholder={`ADOPT ${adoptRunId || '<run_id>'}`}/></Field><label className="check"><input type="checkbox" checked={adoptForce} onChange={e=>setAdoptForce(e.target.checked)}/> Force sha guard override</label><Button variant="destructive" onClick={()=>doConfirm('adopt')}>Adopt</Button></Card></div>}
        {tab === 'rollback' && <div className="cockpit-grid"><Card><h2>Rollback adopted run</h2><Alert tone="warn">Writes only to the active Hermes profile; HERMES_HOME override is ignored.</Alert><Field label="Run ID"><Input value={rollbackRunId} onChange={e=>setRollbackRunId(e.target.value)}/></Field><Field label="Confirmation"><Input value={rollbackConfirm} onChange={e=>setRollbackConfirm(e.target.value)} placeholder={`ROLLBACK ${rollbackRunId || '<run_id>'}`}/></Field><label className="check"><input type="checkbox" checked={rollbackForce} onChange={e=>setRollbackForce(e.target.checked)}/> Force sha guard override</label><Button variant="destructive" onClick={()=>doConfirm('rollback')}>Rollback</Button></Card></div>}
        {tab === 'upstream' && <div className="cockpit-grid"><Card><h2>Upstream</h2><Alert>Read-only status/parity use home override. Update ignores home and uses the active canonical profile.</Alert><label className="check"><input type="checkbox" checked={fetchOnly} onChange={e=>setFetchOnly(e.target.checked)}/> Fetch only</label><div className="button-row"><Button onClick={()=>loadUpstream('status')}>Upstream status</Button><Button onClick={()=>loadUpstream('parity')}>Parity status</Button><Button variant="secondary" onClick={()=>loadUpstream('update')}>Update/fetch pinned upstream</Button></div></Card><Card className="wide"><h2>Result</h2><JsonBlock value={upstream}/></Card></div>}
      </main></div>
    {sheet && <div className="sheet"><div className="sheet-panel"><Button variant="ghost" onClick={()=>setSheet(false)}><X size={18}/>Close</Button>{nav}<div className="sheet-controls">{topControls}</div></div></div>}
  </div>;
}

createRoot(document.getElementById('root')!).render(<App />);
