import { useEffect, useState } from 'react';
import { t } from './i18n';
import { Empty, Field, formatBytes, formatDuration } from './ui';
import { AddonSearch, BackupsTab } from './tabs-b';
import type { ActivityEvent, Health, Run, Server } from './types';

/** A server picker shared by the catalogue and the backups sections; keeps a valid choice as servers come and go. */
function useServerChoice(servers: Server[], keep?: (s: Server) => boolean) {
  const usable = keep ? servers.filter(keep) : servers;
  const [id, setId] = useState(usable[0]?.id || '');
  useEffect(() => { if (!usable.some((s) => s.id === id)) setId(usable[0]?.id || ''); }, [usable.map((s) => s.id).join()]);
  const picker = <Field label={t('catalog.server')}>
    <select className="mc-input mc-compact-select" value={id} onChange={(e) => setId(e.target.value)}>
      {usable.map((s) => <option key={s.id} value={s.id}>{s.name} · {t(`engine.${s.engine}`)} {s.version}</option>)}</select>
  </Field>;
  return { server: usable.find((s) => s.id === id) || null, picker, usable };
}

export function Catalog({ servers, run }: { servers: Server[]; run: Run }) {
  const { server, picker, usable } = useServerChoice(servers, (s) => ['paper', 'purpur', 'fabric', 'forge', 'neoforge'].includes(s.engine));
  return <section className="mc-panel">
    <div className="mc-title-row"><div><h2>{t('catalog.title')}</h2><p>{t('catalog.disclaimer')}</p></div>{usable.length > 0 && picker}</div>
    {server ? <AddonSearch key={server.id} server={server} run={run} /> : <Empty>{t('catalog.noServer')}</Empty>}
  </section>;
}

export function GlobalBackups({ servers, run, onToast }: { servers: Server[]; run: Run; onToast: (s: string, error?: boolean) => void }) {
  const { server, picker } = useServerChoice(servers);
  return <section className="mc-panel">
    <div className="mc-title-row"><h2>{t('backups.title')}</h2>{servers.length > 0 && picker}</div>
    {server ? <BackupsTab key={server.id} server={server} run={run} onToast={onToast} /> : <Empty>{t('servers.empty')}</Empty>}
  </section>;
}

export function Activity({ events, servers }: { events: ActivityEvent[]; servers: Server[] }) {
  const nameOf = (id?: string | null) => (id ? servers.find((s) => s.id === id)?.name || t('activity.deleted') : t('activity.runtime'));
  return <section className="mc-panel">
    <div className="mc-title-row"><div><h2>{t('activity.title')}</h2><p>{t('activity.subtitle')}</p></div></div>
    {events.length ? events.map((e, i) => <div className="mc-list-row" key={`${e.time}-${i}`}>
      <span className={`mc-health-dot ${e.result === 'failed' ? 'error' : 'online'}`} />
      <div><b>{t(`jobs.${e.action}`)}</b><small>{nameOf(e.serverId)}</small></div>
      <span className="mc-spacer" />
      <small className="mc-nowrap">{t(`status.${e.result}`)} · {new Date(e.time).toLocaleString()}</small>
    </div>) : <Empty>{t('activity.empty')}</Empty>}
  </section>;
}

export function Resources({ health, servers }: { health: Health | null; servers: Server[] }) {
  const running = servers.filter((s) => s.state === 'online' || s.state === 'starting');
  const usedMB = running.reduce((n, s) => n + (s.rssMB || 0), 0);
  const heapMB = running.reduce((n, s) => n + s.memoryMB, 0);
  const disk = health?.disk;
  const items: [string, string][] = [
    [t('resources.servers'), t('resources.serversValue', { total: servers.length, running: running.length })],
    [t('resources.memory'), `${formatBytes(usedMB * 1048576)} / ${formatBytes(heapMB * 1048576)}`],
    [t('resources.disk'), disk ? t('resources.diskValue', { free: formatBytes(disk.free), total: formatBytes(disk.total) }) : t('resources.notReported')],
    [t('resources.java'), health?.java || t('resources.notReported')],
    [t('resources.version'), health?.version || t('resources.notReported')],
    [t('resources.uptime'), health?.uptime == null ? t('resources.notReported') : formatDuration(health.uptime)],
    [t('resources.data'), health?.data || t('resources.notReported')],
  ];
  return <section className="mc-panel">
    <div className="mc-title-row"><div><h2>{t('resources.title')}</h2><p>{t('resources.subtitle')}</p></div></div>
    <div className="mc-resource-grid">{items.map(([label, value]) => <div className="mc-resource" key={label}><small>{label}</small><b>{value}</b></div>)}</div>
    <p className="mc-help">{t('resources.note')}</p>
  </section>;
}
