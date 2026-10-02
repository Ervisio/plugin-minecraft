import { useCallback, useEffect, useState } from 'react';
import { enc, errMessage, readChunks, request, saveFile, uploadFile } from './api';
import { t } from './i18n';
import { Icon } from './icons';
import { Button, Empty, Field, Warning, formatBytes, useDialogs } from './ui';
import type { Addon, Backup, CatalogHit, PlayerData, PlayerItem, Run, Schedule, Server, World } from './types';

type Toast = (s: string, error?: boolean) => void;
const isStopped = (s: Server) => !s.state || s.state === 'offline' || s.state === 'error';
const ADDON_ENGINES = new Set(['paper', 'purpur', 'fabric', 'forge', 'neoforge']);
export const addonFolder = (engine: string) => (engine === 'paper' || engine === 'purpur' ? 'plugins' : 'mods');

/** Loads a list for a tab and reloads it after actions. */
function useList<T>(load: () => Promise<T>, initial: T, onToast: Toast) {
  const [data, setData] = useState<T>(initial);
  const [loading, setLoading] = useState(true);
  const reload = useCallback(async () => {
    try { setData(await load()); } catch (e) { onToast(errMessage(e), true); } finally { setLoading(false); }
  }, [load, onToast]);
  useEffect(() => { setLoading(true); void reload(); }, [reload]);
  return { data, loading, reload };
}

/* ---------------------------------------------------------------- Add-ons */

export function AddonSearch({ server, run, onInstalled }: { server: Server; run: Run; onInstalled?: () => void }) {
  const [q, setQ] = useState('');
  const [hits, setHits] = useState<CatalogHit[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [installing, setInstalling] = useState('');
  useEffect(() => {
    let live = true;
    setLoading(true);
    const timer = window.setTimeout(() => {
      request<{ hits: CatalogHit[] }>('GET', '/catalog/addons', undefined, { server: server.id, q })
        .then((r) => { if (live) { setHits(r.hits || []); setError(''); } })
        .catch((e) => { if (live) setError(errMessage(e)); })
        .finally(() => { if (live) setLoading(false); });
    }, 300);
    return () => { live = false; window.clearTimeout(timer); };
  }, [server.id, q]);

  async function install(hit: CatalogHit) {
    setInstalling(hit.projectId);
    if (await run(() => request('POST', `/servers/${enc(server.id)}/addons`, { action: 'install', projectId: hit.projectId }),
      t('addons.installed', { name: hit.title }) + (server.state === 'online' ? ' ' + t('detail.pendingRestart') : ''))) onInstalled?.();
    setInstalling('');
  }

  if (!ADDON_ENGINES.has(server.engine)) return <Empty>{t('addons.unsupported', { engine: t(`engine.${server.engine}`) })}</Empty>;
  return <>
    <input className="mc-input" type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder={t('addons.search')} aria-label={t('addons.search')} />
    <p className="mc-help">{t('addons.compatibility', { engine: t(`engine.${server.engine}`), version: server.version })}</p>
    {error ? <Warning>{error}</Warning> : loading && !hits.length ? <p className="mc-help">{t('common.loading')}</p> : !hits.length ? <Empty>{t('catalog.empty')}</Empty>
      : hits.map((h) => <div className="mc-list-row" key={h.projectId}>
        <div className="mc-addon-mark">{h.title.slice(0, 2).toUpperCase()}</div>
        <div><b>{h.title}</b><small>{h.description}</small></div>
        <span className="mc-spacer" />
        {h.downloads != null && <small className="mc-nowrap">{t('addons.downloads', { n: Number(h.downloads).toLocaleString() })}</small>}
        <Button kind="primary" disabled={!!installing} onClick={() => void install(h)}>{installing === h.projectId ? t('detail.working') : t('addons.install')}</Button>
      </div>)}
  </>;
}

export function AddonsTab({ server, run, onToast }: { server: Server; run: Run; onToast: Toast }) {
  const id = enc(server.id);
  const fetchList = useCallback(() => request<{ addons: Addon[] }>('GET', `/servers/${id}/addons`).then((r) => r.addons || []), [id]);
  const { data: addons, loading, reload } = useList<Addon[]>(fetchList, [], onToast);
  const { confirmTyped, dialog } = useDialogs();
  const note = server.state === 'online' ? ' ' + t('detail.pendingRestart') : '';

  const act = async (body: object, done: string) => { if (await run(() => request('POST', `/servers/${id}/addons`, body), done + note)) await reload(); };
  async function remove(a: Addon) {
    if (await confirmTyped(t('addons.removeTitle'), t('addons.removeConfirm'), a.name, t('addons.remove'))) await act({ action: 'delete', path: a.path, confirm: a.path }, t('addons.removed', { name: a.name }));
  }
  async function uploadJar(files: FileList | null) {
    const file = files?.[0];
    if (!file) return;
    if (await run(() => uploadFile(server.id, `${addonFolder(server.engine)}/${file.name}`, file), t('files.uploaded', { name: file.name }) + note)) await reload();
  }

  return <div className="mc-tab-body">
    <div className="mc-title-row">
      <div><h3>{t('addons.installedTitle')}</h3><p>{t('addons.folder', { folder: addonFolder(server.engine) })}</p></div>
      <label className="mc-button"><input className="mc-hidden-file" type="file" accept=".jar" onChange={(e) => { void uploadJar(e.target.files); e.currentTarget.value = ''; }} />
        <Icon name="upload" /> {t('addons.uploadJar')}</label>
    </div>
    {loading ? <p>{t('common.loading')}</p> : !addons.length ? <Empty>{t('addons.none')}</Empty> : addons.map((a) => <div className={`mc-list-row ${a.enabled ? '' : 'muted'}`} key={a.path}>
      <div className="mc-addon-mark">{a.name.slice(0, 2).toUpperCase()}</div>
      <div><b>{a.name}</b><small>{a.projectId ? t('addons.fromCatalog') : t('addons.manual')} · <span className="mc-mono">{a.path}</span></small></div>
      <span className="mc-spacer" />
      <label className="mc-switch"><input type="checkbox" role="switch" checked={a.enabled} onChange={(e) => void act({ action: 'toggle', path: a.path, enabled: e.target.checked }, t(e.target.checked ? 'addons.enabled' : 'addons.disabled', { name: a.name }))} />
        <span>{a.enabled ? t('common.enabled') : t('common.disabled')}</span></label>
      {a.projectId && <Button onClick={() => void act({ action: 'update', path: a.path }, t('addons.updated', { name: a.name }))}>{t('addons.update')}</Button>}
      <Button kind="danger" onClick={() => void remove(a)} ariaLabel={`${t('addons.remove')} ${a.name}`} title={t('addons.remove')}><Icon name="trash" /></Button>
    </div>)}
    <section className="mc-subsection">
      <h3>{t('addons.catalog')}</h3>
      <AddonSearch server={server} run={run} onInstalled={() => void reload()} />
    </section>
    {dialog}
  </div>;
}

/* ---------------------------------------------------------------- Players */

type PlayerList = 'online' | 'whitelist' | 'ops' | 'banned';
const ADD_ACTION: Record<PlayerList, string> = { online: '', whitelist: 'whitelist-add', ops: 'op', banned: 'ban' };
const nameOf = (p: PlayerItem | string) => (typeof p === 'string' ? p : p.name);

export function PlayersTab({ server, run, onToast }: { server: Server; run: Run; onToast: Toast }) {
  const id = enc(server.id);
  const fetchList = useCallback(() => request<PlayerData>('GET', `/servers/${id}/players`), [id]);
  const { data, loading, reload } = useList<PlayerData>(fetchList, {}, onToast);
  const [list, setList] = useState<PlayerList>('online');
  const { ask, dialog } = useDialogs();
  const online = server.state === 'online';

  async function act(action: string, name: string) {
    // The command goes through the console; the server rewrites its JSON lists a moment later.
    if (await run(() => request('POST', `/servers/${id}/players`, { action, name }), t(`players.done.${action}`, { name }))) window.setTimeout(() => void reload(), 800);
  }
  async function add() {
    const name = await ask(t(`players.add.${list}`), t('players.addHelp'), t('players.username'));
    if (name) await act(ADD_ACTION[list], name);
  }
  const rows = (data[list] || []) as PlayerItem[];

  return <div className="mc-tab-body">
    <div className="mc-segments" role="tablist">
      {(['online', 'whitelist', 'ops', 'banned'] as PlayerList[]).map((l) => <button key={l} role="tab" aria-selected={list === l} className={list === l ? 'on' : ''} onClick={() => setList(l)}>
        {t(`players.${l}`)} <b>{(data[l] || []).length}</b></button>)}
    </div>
    {!online && <Warning>{t('players.offline')}</Warning>}
    <div className="mc-title-row">
      <h3>{t(`players.${list}`)}</h3>
      <div className="mc-actions">
        <Button onClick={() => void reload()}>{t('common.refresh')}</Button>
        {list !== 'online' && <Button kind="primary" disabled={!online} onClick={() => void add()}><Icon name="plus" /> {t(`players.add.${list}`)}</Button>}
      </div>
    </div>
    {loading ? <p>{t('common.loading')}</p> : rows.length ? rows.map((p) => {
      const name = nameOf(p);
      return <div className="mc-list-row" key={name}>
        <div className="mc-player-avatar">{name.slice(0, 1).toUpperCase()}</div>
        <div><b>{name}</b>{typeof p !== 'string' && p.uuid && <small className="mc-mono">{p.uuid}</small>}</div>
        <span className="mc-spacer" />
        {list === 'online' && <>
          <Button disabled={!online} onClick={() => void act('op', name)}>{t('players.op')}</Button>
          <Button disabled={!online} onClick={() => void act('kick', name)}>{t('players.kick')}</Button>
          <Button kind="danger" disabled={!online} onClick={() => void act('ban', name)}>{t('players.ban')}</Button></>}
        {list === 'whitelist' && <Button disabled={!online} onClick={() => void act('whitelist-remove', name)}>{t('players.whitelistRemove')}</Button>}
        {list === 'ops' && <Button disabled={!online} onClick={() => void act('deop', name)}>{t('players.deop')}</Button>}
        {list === 'banned' && <Button disabled={!online} onClick={() => void act('pardon', name)}>{t('players.unban')}</Button>}
      </div>;
    }) : <Empty>{t(list === 'online' ? 'players.noneOnline' : 'players.empty')}</Empty>}
    {dialog}
  </div>;
}

/* ---------------------------------------------------------------- Worlds */

export function WorldsTab({ server, run, onToast }: { server: Server; run: Run; onToast: Toast }) {
  const id = enc(server.id);
  const fetchList = useCallback(() => request<{ worlds: World[] }>('GET', `/servers/${id}/worlds`).then((r) => r.worlds || []), [id]);
  const { data: worlds, loading, reload } = useList<World[]>(fetchList, [], onToast);
  const { confirmTyped, dialog } = useDialogs();
  const stopped = isStopped(server);

  async function activate(w: World) {
    if (await run(() => request('POST', `/servers/${id}/worlds`, { action: 'activate', name: w.name }), t('worlds.activated', { name: w.name }) + (stopped ? '' : ' ' + t('detail.pendingRestart')))) await reload();
  }
  async function remove(w: World) {
    if (!await confirmTyped(t('worlds.deleteTitle'), t('worlds.deleteConfirm'), w.name)) return;
    if (await run(() => request('POST', `/servers/${id}/worlds`, { action: 'delete', name: w.name, confirm: w.name }), t('worlds.deleted', { name: w.name }))) await reload();
  }

  return <div className="mc-tab-body">
    <div className="mc-title-row"><div><h3>{t('worlds.title')}</h3><p>{t('worlds.subtitle')}</p></div><Button onClick={() => void reload()}>{t('common.refresh')}</Button></div>
    {loading ? <p>{t('common.loading')}</p> : worlds.length ? worlds.map((w) => <div className="mc-list-row" key={w.name}>
      <div className="mc-addon-mark"><Icon name="cube" /></div>
      <div><b>{w.name}</b><small className="mc-mono">{w.path || w.name}</small></div>
      <span className="mc-spacer" />
      {w.active ? <span className="mc-badge">{t('worlds.active')}</span> : <>
        <Button onClick={() => void activate(w)}>{t('worlds.activate')}</Button>
        <Button kind="danger" disabled={!stopped} title={stopped ? undefined : t('backups.stopFirst')} onClick={() => void remove(w)} ariaLabel={`${t('worlds.delete')} ${w.name}`}><Icon name="trash" /></Button></>}
    </div>) : <Empty>{t('worlds.empty')}</Empty>}
    <p className="mc-help">{t('worlds.note')}</p>
    {dialog}
  </div>;
}

/* ---------------------------------------------------------------- Backups */

export function BackupsTab({ server, run, onToast }: { server: Server; run: Run; onToast: Toast }) {
  const id = enc(server.id);
  const fetchList = useCallback(() => request<{ backups: Backup[] }>('GET', `/servers/${id}/backups`).then((r) => r.backups || []), [id]);
  const { data: backups, loading, reload } = useList<Backup[]>(fetchList, [], onToast);
  const { confirmTyped, dialog } = useDialogs();
  const [progress, setProgress] = useState('');
  const stopped = isStopped(server);

  async function create() {
    if (await run(() => request('POST', `/servers/${id}/backups`, { action: 'create' }), t('backups.created', { name: server.name }))) await reload();
  }
  async function restore(b: Backup) {
    if (!await confirmTyped(t('backups.restoreTitle'), t('backups.restoreConfirm'), b.id, t('backups.restore'))) return;
    await run(() => request('POST', `/servers/${id}/backups`, { action: 'restore', backupId: b.id, confirm: b.id }), t('backups.restored', { name: server.name }));
  }
  async function remove(b: Backup) {
    if (!await confirmTyped(t('backups.delete'), t('backups.deleteConfirm'), b.id)) return;
    if (await run(() => request('POST', `/servers/${id}/backups`, { action: 'delete', backupId: b.id, confirm: b.id }), t('backups.deleted'))) await reload();
  }
  async function download(b: Backup) {
    try {
      const data = await readChunks(`/servers/${id}/backup-file`, { backupId: b.id }, Infinity, (d, s) => setProgress(t('files.downloading', { name: b.name, pct: s ? Math.round(d * 100 / s) : 100 })));
      saveFile(data, `${server.name}-${b.id}.tar.gz`, 'application/gzip');
    } catch (e) {
      onToast(errMessage(e), true);
    } finally {
      setProgress('');
    }
  }

  return <div className="mc-tab-body">
    <div className="mc-title-row">
      <div><h3>{t('backups.title')}</h3><p>{t('backups.subtitle')}</p></div>
      <Button kind="primary" disabled={!stopped} onClick={() => void create()}><Icon name="plus" /> {t('backups.create')}</Button>
    </div>
    {!stopped && <Warning>{t('backups.stopFirst')}</Warning>}
    {progress && <p className="mc-help" role="status">{progress}</p>}
    {loading ? <p>{t('common.loading')}</p> : backups.length ? backups.map((b) => <div className="mc-list-row" key={b.id}>
      <div className="mc-addon-mark"><Icon name="backup" /></div>
      <div><b>{b.created ? new Date(b.created).toLocaleString() : b.name}</b><small><span className="mc-mono">{b.id}</span> · {formatBytes(b.size)}</small></div>
      <span className="mc-spacer" />
      <Button onClick={() => void download(b)} disabled={!!progress} ariaLabel={`${t('backups.download')} ${b.id}`} title={t('backups.download')}><Icon name="download" /></Button>
      <Button disabled={!stopped} onClick={() => void restore(b)}>{t('backups.restore')}</Button>
      <Button kind="danger" onClick={() => void remove(b)} ariaLabel={`${t('backups.delete')} ${b.id}`} title={t('backups.delete')}><Icon name="trash" /></Button>
    </div>) : <Empty>{t('backups.empty')}</Empty>}
    {dialog}
  </div>;
}

/* ---------------------------------------------------------------- Schedules */

export function SchedulesTab({ server, run, onToast }: { server: Server; run: Run; onToast: Toast }) {
  const id = enc(server.id);
  const fetchList = useCallback(() => request<{ schedules: Schedule[] }>('GET', `/servers/${id}/schedules`).then((r) => r.schedules || []), [id]);
  const { data, loading, reload } = useList<Schedule[]>(fetchList, [], onToast);
  const [rows, setRows] = useState<Schedule[]>([]);
  const [busy, setBusy] = useState(false);
  useEffect(() => { setRows(data); }, [data]);
  const dirty = JSON.stringify(rows) !== JSON.stringify(data);
  const update = (sid: string, patch: Partial<Schedule>) => setRows((rs) => rs.map((r) => (r.id === sid ? { ...r, ...patch } : r)));

  async function save() {
    setBusy(true);
    if (await run(() => request('PUT', `/servers/${id}/schedules`, { schedules: rows }), t('schedules.saved'))) await reload();
    setBusy(false);
  }

  return <div className="mc-tab-body">
    <div className="mc-title-row">
      <div><h3>{t('schedules.title')}</h3><p>{t('schedules.subtitle')}</p></div>
      <div className="mc-actions">
        <Button onClick={() => setRows(data)} disabled={!dirty}>{t('config.discard')}</Button>
        <Button kind="primary" onClick={() => void save()} disabled={busy || !dirty}>{t('schedules.save')}</Button>
      </div>
    </div>
    {loading ? <p>{t('common.loading')}</p> : rows.length ? rows.map((r) => <div className="mc-schedule-row" key={r.id}>
      <Field label={t('schedules.action')}>
        <select className="mc-input" value={r.action} onChange={(e) => update(r.id, { action: e.target.value as Schedule['action'] })}>
          <option value="backup">{t('schedules.backup')}</option><option value="restart">{t('schedules.restart')}</option></select>
      </Field>
      <Field label={t('schedules.time')}>
        <input className="mc-input" type="time" value={`${String(r.hour).padStart(2, '0')}:${String(r.minute).padStart(2, '0')}`}
          onChange={(e) => { const [h, m] = e.target.value.split(':').map(Number); if (Number.isFinite(h) && Number.isFinite(m)) update(r.id, { hour: h, minute: m }); }} />
      </Field>
      <label className="mc-check"><input type="checkbox" checked={r.enabled} onChange={(e) => update(r.id, { enabled: e.target.checked })} /><span>{t('schedules.enabled')}</span></label>
      <small>{r.lastRun ? t('schedules.lastRun', { when: new Date(r.lastRun).toLocaleString() }) : t('schedules.never')}</small>
      <Button kind="danger" onClick={() => setRows((rs) => rs.filter((x) => x.id !== r.id))} ariaLabel={t('schedules.delete')} title={t('schedules.delete')}><Icon name="trash" /></Button>
    </div>) : <Empty>{t('schedules.empty')}</Empty>}
    <div className="mc-dialog-actions start">
      <Button disabled={rows.length >= 20} onClick={() => setRows((rs) => [...rs, { id: `s${Date.now().toString(36)}`, action: 'backup', hour: 4, minute: 0, enabled: true }])}><Icon name="plus" /> {t('schedules.create')}</Button>
    </div>
    <p className="mc-help">{t('schedules.note')}</p>
  </div>;
}
