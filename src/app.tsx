import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { enc, errMessage, jobDone, request, waitJob } from './api';
import { t } from './i18n';
import { Icon } from './icons';
import { Button, Empty, useEscape, useInterval } from './ui';
import { CreateDialog, EditDialog } from './dialogs';
import { ConsoleTab, FilesTab, ConfigTab, SoftwareTab } from './tabs-a';
import { AddonsTab, PlayersTab, WorldsTab, BackupsTab, SchedulesTab } from './tabs-b';
import { Activity, Catalog, GlobalBackups, Resources } from './sections';
import type { ActivityEvent, Health, Job, Run, Server } from './types';

type Section = 'servers' | 'catalog' | 'backups' | 'activity' | 'resources';
type Tab = 'console' | 'files' | 'config' | 'software' | 'addons' | 'players' | 'worlds' | 'backups' | 'schedules';
type Filter = 'all' | 'online' | 'offline' | 'error';
const TABS: Tab[] = ['console', 'files', 'config', 'software', 'addons', 'players', 'worlds', 'backups', 'schedules'];
const SECTIONS: { id: Section; icon: string }[] = [
  { id: 'servers', icon: 'servers' }, { id: 'catalog', icon: 'catalog' }, { id: 'backups', icon: 'backup' },
  { id: 'activity', icon: 'activity' }, { id: 'resources', icon: 'resources' },
];

export const displayState = (state?: string) => t(`servers.state.${state || 'offline'}`);
const busyState = (s?: Server) => s?.state === 'starting' || s?.state === 'stopping';
const matchesFilter = (s: Server, f: Filter) => f === 'all' || (f === 'offline' ? (s.state || 'offline') === 'offline' : s.state === f);

function useToast() {
  const [toast, setToast] = useState<{ text: string; error: boolean } | null>(null);
  const timer = useRef(0);
  const show = useCallback((text: string, error = false) => {
    window.clearTimeout(timer.current);
    setToast({ text, error });
    timer.current = window.setTimeout(() => setToast(null), error ? 8000 : 4500);
  }, []);
  return { toast, show };
}

export function App() {
  const [servers, setServers] = useState<Server[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [activity, setActivity] = useState<ActivityEvent[]>([]);
  const [cpuHistory, setCpuHistory] = useState<Record<string, number[]>>({});
  const [section, setSection] = useState<Section>('servers');
  const [selected, setSelected] = useState('');
  const [tab, setTab] = useState<Tab>('console');
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<Filter>('all');
  const [loading, setLoading] = useState(true);
  const [fatal, setFatal] = useState('');
  const [createOpen, setCreateOpen] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const { toast, show } = useToast();

  const loadRoot = useCallback(async () => {
    try {
      const [ss, h, j, a] = await Promise.all([
        request<{ servers: Server[] }>('GET', '/servers'), request<Health>('GET', '/health'),
        request<{ jobs: Job[] }>('GET', '/jobs'), request<{ events: ActivityEvent[] }>('GET', '/activity'),
      ]);
      const list = ss.servers || [];
      setServers(list);
      setCpuHistory((old) => {
        const next: Record<string, number[]> = {};
        for (const s of list) next[s.id] = s.cpu == null ? (old[s.id] || []) : [...(old[s.id] || []), s.cpu].slice(-30);
        return next;
      });
      setHealth(h);
      setJobs(j.jobs || []);
      setActivity(a.events || []);
      setFatal('');
      setSelected((old) => (list.some((s) => s.id === old) ? old : ''));
    } catch (e) {
      setFatal(errMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void loadRoot(); }, [loadRoot]);
  const pending = jobs.filter((j) => !jobDone(j));
  const fast = pending.length > 0 || servers.some(busyState);
  useInterval(() => { void loadRoot(); }, fast ? 2500 : 8000);

  /** Runs an action; when it answers with a jobId, waits for the job. Errors become a toast; returns success. */
  const run: Run = useCallback(async (fn, done) => {
    try {
      const result = await fn() as { jobId?: string } | undefined;
      if (result && typeof result === 'object' && result.jobId) {
        void loadRoot();
        await waitJob(result.jobId);
      }
      await loadRoot();
      if (done) show(done);
      return true;
    } catch (e) {
      show(errMessage(e), true);
      void loadRoot();
      return false;
    }
  }, [loadRoot, show]);

  const server = servers.find((s) => s.id === selected) || null;
  const filtered = useMemo(() => servers.filter((s) =>
    matchesFilter(s, filter) && `${s.name} ${s.engine} ${s.version} ${s.port}`.toLowerCase().includes(query.trim().toLowerCase())), [servers, query, filter]);
  const online = servers.filter((s) => s.state === 'online');
  const playersOnline = online.reduce((n, s) => n + (s.playersOnline || 0), 0);
  const counts: Record<Filter, number> = {
    all: servers.length, online: online.length,
    offline: servers.filter((s) => matchesFilter(s, 'offline')).length, error: servers.filter((s) => s.state === 'error').length,
  };
  const power = (s: Server, action: 'start' | 'stop' | 'restart' | 'kill') =>
    run(() => request('POST', `/servers/${enc(s.id)}/power`, { action }), t(`power.done.${action}`, { name: s.name }));
  const open = (s: Server) => { setSelected(s.id); setTab('console'); };

  return <div className="mc-root">
    <header className="mc-header">
      <div className="mc-brand"><span className="mc-brand-mark"><Icon name="cube" size={24} /></span><div><h1>{t('app.title')}</h1><p>{t('app.subtitle')}</p></div></div>
      <div className="mc-header-actions">
        <Button kind="primary" onClick={() => setCreateOpen(true)} disabled={!!fatal}><Icon name="plus" /> {t('common.create')}</Button>
      </div>
    </header>
    <div className="mc-layout">
      <aside className="mc-sidebar" aria-label={t('app.title')}>
        <div className="mc-sidebar-caption">{t('nav.manage')}</div>
        {SECTIONS.map((s) => <button key={s.id} className={`mc-nav ${section === s.id ? 'active' : ''}`} aria-current={section === s.id ? 'page' : undefined}
          onClick={() => { setSection(s.id); setSelected(''); }}>
          <Icon name={s.icon} size={17} />{t(`nav.${s.id}`)}{s.id === 'servers' && <b>{servers.length}</b>}
        </button>)}
        <div className="mc-sidebar-footer">
          <div><span className={`mc-health-dot ${fatal ? 'error' : 'online'}`} />{fatal ? t('error.title') : t('status.runtime')}</div>
          {!fatal && <small>{t('status.runningCount', { count: online.length })}</small>}
        </div>
      </aside>
      <main className="mc-main">
        {fatal ? <section className="mc-panel mc-error">
          <h2>{t('error.title')}</h2><p>{t('error.text')}</p><code>{fatal}</code><small>{t('error.install')}</small>
          <div><Button kind="primary" onClick={() => { setLoading(true); void loadRoot(); }}>{t('error.retry')}</Button></div>
        </section> : loading ? <section className="mc-panel"><p>{t('common.loading')}</p></section>
          : section === 'servers' ? <section className="mc-panel mc-server-overview">
            <div className="mc-toolbar">
              <input className="mc-input mc-filter" value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t('servers.search')} aria-label={t('servers.search')} />
              <div className="mc-chips" role="group" aria-label={t('servers.filter')}>
                {(['all', 'online', 'offline', 'error'] as Filter[]).map((f) => <button key={f} className={`mc-chip ${filter === f ? 'on' : ''}`} aria-pressed={filter === f} onClick={() => setFilter(f)}>
                  {t(`filter.${f}`)} <b>{counts[f]}</b></button>)}
              </div>
            </div>
            <div className="mc-title-row"><h2>{t('servers.title')}</h2><small>{t('servers.summary', { online: online.length, players: playersOnline })}</small></div>
            {servers.length === 0 ? <div className="mc-first">
              <h3>{t('servers.empty')}</h3><p>{t('servers.emptyText')}</p>
              <Button kind="primary" onClick={() => setCreateOpen(true)}><Icon name="plus" /> {t('common.create')}</Button>
            </div> : filtered.length ? <div className="mc-card-grid">
              {filtered.map((s) => <ServerCard key={s.id} server={s} history={cpuHistory[s.id] || []} active={s.id === selected}
                job={pending.find((j) => j.serverId === s.id)} onOpen={() => open(s)} onPower={(a) => void power(s, a)} />)}
            </div> : <Empty>{t('servers.noMatch')}</Empty>}
          </section>
            : section === 'catalog' ? <Catalog servers={servers} run={run} />
              : section === 'backups' ? <GlobalBackups servers={servers} run={run} onToast={show} />
                : section === 'activity' ? <Activity events={activity} servers={servers} />
                  : <Resources health={health} servers={servers} />}
      </main>
    </div>
    {createOpen && <CreateDialog servers={servers} onClose={() => setCreateOpen(false)} run={run}
      onCreated={(s) => { setCreateOpen(false); setSection('servers'); open(s); }} />}
    {server && <Drawer onClose={() => setSelected('')} label={server.name}>
      <DetailHeader server={server} job={pending.find((j) => j.serverId === server.id)} onPower={(a) => void power(server, a)} onEdit={() => setEditOpen(true)} onClose={() => setSelected('')} />
      <nav className="mc-tabs" aria-label={server.name}>
        {TABS.map((x) => <button key={x} className={tab === x ? 'active' : ''} aria-current={tab === x ? 'page' : undefined} onClick={() => setTab(x)}>{t(`detail.${x}`)}</button>)}
      </nav>
      <ServerTab key={`${server.id}:${tab}`} server={server} tab={tab} run={run} onToast={show} />
    </Drawer>}
    {editOpen && server && <EditDialog server={server} run={run} onClose={() => setEditOpen(false)} onDeleted={() => { setEditOpen(false); setSelected(''); }} />}
    {toast && <div className={`mc-toast ${toast.error ? 'error' : ''}`} role={toast.error ? 'alert' : 'status'}>{toast.text}</div>}
  </div>;
}

function Drawer({ children, onClose, label }: { children: ReactNode; onClose: () => void; label: string }) {
  useEscape(onClose);
  return <div className="mc-detail-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
    <aside className="mc-detail-panel" role="dialog" aria-modal="true" aria-label={label}>{children}</aside>
  </div>;
}

function ServerCard({ server: s, history, active, job, onOpen, onPower }: {
  server: Server; history: number[]; active: boolean; job?: Job; onOpen: () => void; onPower: (action: 'start' | 'stop') => void;
}) {
  const running = s.state === 'online' || s.state === 'starting';
  const memPct = s.rssMB && s.memoryMB ? (s.rssMB / s.memoryMB) * 100 : null;
  return <article className={`mc-server-card ${active ? 'selected' : ''} ${s.state === 'error' ? 'is-error' : ''}`}>
    <button className="mc-card-select" onClick={onOpen} aria-label={`${t('servers.open')}: ${s.name}`}>
      <span className="mc-server-icon"><Icon name="cube" size={20} /></span>
      <span className="mc-card-title"><b>{s.name}</b><small>{engineLabel(s)}</small></span>
      <span className={`mc-state ${s.state || 'offline'}`}><i />{displayState(s.state)}</span>
    </button>
    <CpuSparkline values={history} />
    <div className="mc-metrics">
      <Metric label={t('servers.cpu')} value={s.cpu == null ? '—' : `${Math.round(s.cpu)}%`} percent={s.cpu == null ? null : Math.min(100, s.cpu)} />
      <Metric label={t('servers.memory')} value={`${s.rssMB ? (s.rssMB / 1024).toFixed(1) : 0} / ${(s.memoryMB / 1024).toFixed(s.memoryMB % 1024 ? 1 : 0)} GB`} percent={memPct ?? 0} />
      <div className="mc-metric-line"><span><Icon name="players" size={14} /> {t('servers.playersOf', { online: s.playersOnline ?? 0, max: s.maxPlayers ?? '—' })}</span><b>{s.tps == null ? '— TPS' : `${s.tps.toFixed(1)} TPS`}</b></div>
    </div>
    {s.state === 'error' && s.error && <p className="mc-card-error">{s.error}</p>}
    {job && <div className="mc-card-job"><span>{t(`jobs.${job.action}`)}</span>{job.progress ? <progress value={job.progress} max={100} /> : null}</div>}
    <footer>
      <code>:{s.port}</code>
      <span className="mc-card-actions">
        <Button kind="icon" disabled={busyState(s)} onClick={() => onPower(running ? 'stop' : 'start')} ariaLabel={t(running ? 'servers.stop' : 'servers.start')} title={t(running ? 'servers.stop' : 'servers.start')}>
          <Icon name={running ? 'stop' : 'play'} /></Button>
        <Button kind="icon" onClick={onOpen} ariaLabel={t('servers.open')} title={t('servers.open')}><Icon name="chevron" /></Button>
      </span>
    </footer>
  </article>;
}

export const engineLabel = (s: Server) => `${t(`engine.${s.engine}`)} · ${s.version}${s.loaderVersion ? ` (${s.loaderVersion})` : ''}`;

function CpuSparkline({ values }: { values: number[] }) {
  if (values.length < 2) return <div className="mc-cpu-spark empty" aria-hidden="true" />;
  const max = Math.max(100, ...values);
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * 100},${38 - (v / max) * 34}`);
  return <svg className="mc-cpu-spark" viewBox="0 0 100 40" preserveAspectRatio="none" role="img" aria-label={t('servers.cpuHistory')}>
    <polygon points={`0,40 ${pts.join(' ')} 100,40`} fill="var(--mc-accent-soft)" />
    <polyline points={pts.join(' ')} fill="none" stroke="var(--mc-accent)" strokeWidth="1.6" vectorEffect="non-scaling-stroke" />
  </svg>;
}

function Metric({ label, value, percent }: { label: string; value: string; percent?: number | null }) {
  return <div className="mc-metric"><div><span>{label}</span><b>{value}</b></div>
    <span className="mc-bar" aria-hidden="true"><i style={{ width: `${Math.max(0, Math.min(100, percent ?? 0))}%` }} /></span></div>;
}

function DetailHeader({ server: s, job, onPower, onEdit, onClose }: {
  server: Server; job?: Job; onPower: (a: 'start' | 'stop' | 'restart' | 'kill') => void; onEdit: () => void; onClose: () => void;
}) {
  const running = s.state === 'online' || s.state === 'starting';
  return <div className="mc-detail-head">
    <div className="mc-server-icon big"><Icon name="cube" size={22} /></div>
    <div className="mc-detail-name"><h2>{s.name}</h2><p>{engineLabel(s)} · <code>:{s.port}</code></p></div>
    <span className={`mc-state ${s.state || 'offline'}`}><i />{displayState(s.state)}</span>
    <Button kind="icon" onClick={onClose} ariaLabel={t('common.close')} title={t('common.close')}><Icon name="close" /></Button>
    <div className="mc-actions">
      {s.state === 'stopping'
        ? <Button kind="danger" onClick={() => onPower('kill')}>{t('servers.kill')}</Button>
        : <Button kind={running ? 'danger' : 'primary'} disabled={!!job} onClick={() => onPower(running ? 'stop' : 'start')}>
          <Icon name={running ? 'stop' : 'play'} /> {t(running ? 'servers.stop' : 'servers.start')}</Button>}
      <Button onClick={() => onPower('restart')} disabled={!!job || s.state !== 'online'}><Icon name="restart" /> {t('servers.restart')}</Button>
      <Button onClick={onEdit}><Icon name="edit" /> {t('servers.edit')}</Button>
    </div>
    {s.state === 'error' && s.error && <p className="mc-detail-error" role="status">{s.error}</p>}
    {job && <div className="mc-card-job wide"><span>{t(`jobs.${job.action}`)}…</span>{job.progress ? <progress value={job.progress} max={100} /> : null}</div>}
  </div>;
}

function ServerTab({ server, tab, run, onToast }: { server: Server; tab: Tab; run: Run; onToast: (s: string, error?: boolean) => void }) {
  switch (tab) {
    case 'console': return <ConsoleTab server={server} onToast={onToast} />;
    case 'files': return <FilesTab server={server} run={run} onToast={onToast} />;
    case 'config': return <ConfigTab server={server} run={run} />;
    case 'software': return <SoftwareTab server={server} run={run} />;
    case 'addons': return <AddonsTab server={server} run={run} onToast={onToast} />;
    case 'players': return <PlayersTab server={server} run={run} onToast={onToast} />;
    case 'worlds': return <WorldsTab server={server} run={run} onToast={onToast} />;
    case 'backups': return <BackupsTab server={server} run={run} onToast={onToast} />;
    default: return <SchedulesTab server={server} run={run} onToast={onToast} />;
  }
}
