import { useCallback, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import { enc, errMessage, readChunks, request, saveFile, uploadFile } from './api';
import { t } from './i18n';
import { Icon } from './icons';
import { Button, Empty, Field, Warning, formatBytes, useDialogs, useInterval } from './ui';
import { ENGINES, useLoaders, useVersions } from './dialogs';
import type { Entry, Run, Server } from './types';

type Toast = (s: string, error?: boolean) => void;
const isStopped = (s: Server) => !s.state || s.state === 'offline' || s.state === 'error';

/* ---------------------------------------------------------------- Console */

export function ConsoleTab({ server, onToast }: { server: Server; onToast: Toast }) {
  const [lines, setLines] = useState<{ seq: number; text: string }[]>([]);
  const [command, setCommand] = useState('');
  const [history, setHistory] = useState<string[]>([]);
  const [histPos, setHistPos] = useState(-1);
  const [busy, setBusy] = useState(false);
  const [follow, setFollow] = useState(true);
  const cursor = useRef(0);
  const box = useRef<HTMLPreElement>(null);
  const failed = useRef(false);

  const load = useCallback(async () => {
    try {
      const r = await request<{ lines: { seq: number; text: string }[]; cursor: number }>('GET', `/servers/${enc(server.id)}/logs`, undefined, { after: String(cursor.current) });
      failed.current = false;
      // The runtime restarted (its cursor went back): start over.
      if (r.cursor < cursor.current) { cursor.current = 0; setLines([]); return; }
      if (r.lines?.length) {
        cursor.current = r.cursor;
        setLines((old) => [...old, ...r.lines].slice(-3000));
      }
    } catch (e) {
      if (!failed.current) onToast(errMessage(e), true);
      failed.current = true;
    }
  }, [server.id, onToast]);
  useEffect(() => { void load(); }, [load]);
  useInterval(() => { void load(); }, 2000);
  useEffect(() => { if (follow && box.current) box.current.scrollTop = box.current.scrollHeight; }, [lines, follow]);

  async function send(e: FormEvent) {
    e.preventDefault();
    const value = command.trim();
    if (!value) return;
    setBusy(true);
    try {
      await request('POST', `/servers/${enc(server.id)}/command`, { command: value });
      setHistory((h) => [value, ...h.filter((x) => x !== value)].slice(0, 50));
      setHistPos(-1);
      setCommand('');
      window.setTimeout(() => void load(), 400);
    } catch (x) {
      onToast(errMessage(x), true);
    } finally {
      setBusy(false);
    }
  }

  function onKey(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
    e.preventDefault();
    const next = Math.max(-1, Math.min(history.length - 1, histPos + (e.key === 'ArrowUp' ? 1 : -1)));
    setHistPos(next);
    setCommand(next < 0 ? '' : history[next]);
  }

  const online = server.state === 'online' || server.state === 'starting';
  return <div className="mc-tab-body">
    <div className="mc-console-toolbar">
      <h3>{t('detail.live')}</h3>
      <label className="mc-check"><input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} /><span>{t('console.follow')}</span></label>
      <Button onClick={() => saveFile(new TextEncoder().encode(lines.map((l) => l.text).join('\n') + '\n'), `${server.name}-console.log`, 'text/plain')} disabled={!lines.length}>
        <Icon name="download" /> {t('console.save')}</Button>
    </div>
    <pre className="mc-console" ref={box} aria-live="off" tabIndex={0}>
      {lines.length ? lines.map((l) => <div key={l.seq} className={/\b(ERROR|SEVERE|Exception)\b/.test(l.text) ? 'err' : /\bWARN/.test(l.text) ? 'warn' : l.text.startsWith('[Ervisio]') ? 'sys' : undefined}>{l.text}</div>) : t('detail.noLogs')}
    </pre>
    <form className="mc-command" onSubmit={(e) => void send(e)}>
      <span className="mc-prompt" aria-hidden="true">&gt;</span>
      <input className="mc-input mc-mono" value={command} onChange={(e) => setCommand(e.target.value)} onKeyDown={onKey} disabled={!online}
        placeholder={online ? t('detail.commandPlaceholder') : t('console.offline')} aria-label={t('detail.command')} maxLength={2048} spellCheck={false} />
      <Button type="submit" kind="primary" disabled={busy || !online || !command.trim()}>{t('detail.command')}</Button>
    </form>
    <p className="mc-help">{t('detail.commandHint')}</p>
  </div>;
}

/* ---------------------------------------------------------------- Files */

const BINARY = /\.(jar|zip|gz|tgz|tar|png|jpe?g|gif|webp|dat|dat_old|mca|mcr|nbt|sqlite|db|class|so|exe)$/i;
const ARCHIVE = /\.(zip|tar|tar\.gz|tgz)$/i;
const RESERVED = new Set(['server.properties', 'eula.txt']);
const MAX_TEXT = 192 * 1024;

export function FilesTab({ server, run, onToast }: { server: Server; run: Run; onToast: Toast }) {
  const [path, setPath] = useState('');
  const [entries, setEntries] = useState<Entry[]>([]);
  const [loading, setLoading] = useState(false);
  const [editor, setEditor] = useState<{ path: string; text: string; original: string; readOnly: boolean } | null>(null);
  const [busy, setBusy] = useState('');
  const { ask, confirmTyped, dialog } = useDialogs();
  const id = enc(server.id);

  const load = useCallback(async (p: string) => {
    setLoading(true);
    try {
      const r = await request<{ entries: Entry[] }>('GET', `/servers/${id}/files`, undefined, { path: p });
      setEntries(r.entries || []);
      setPath(p);
    } catch (e) {
      onToast(errMessage(e), true);
    } finally {
      setLoading(false);
    }
  }, [id, onToast]);
  useEffect(() => { void load(''); }, [load]);

  const child = (name: string) => (path ? `${path}/${name}` : name);
  const parent = path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '';

  async function open(entry: Entry) {
    const p = child(entry.name);
    if (entry.type === 'directory') { await load(p); return; }
    if (BINARY.test(entry.name)) { onToast(t('files.unsupported')); return; }
    if ((entry.size ?? 0) > MAX_TEXT) { onToast(t('files.tooLarge')); return; }
    try {
      const bytes = await readChunks(`/servers/${id}/file`, { path: p }, MAX_TEXT);
      if (bytes.includes(0)) { onToast(t('files.unsupported')); return; }
      const text = new TextDecoder().decode(bytes);
      setEditor({ path: p, text, original: text, readOnly: RESERVED.has(p) });
    } catch (e) {
      onToast(errMessage(e), true);
    }
  }

  async function saveEditor() {
    if (!editor) return;
    setBusy('save');
    if (await run(() => request('PATCH', `/servers/${id}/file`, { path: editor.path, text: editor.text }), t('files.saved', { name: editor.path }))) setEditor(null);
    setBusy('');
  }

  async function upload(files: FileList | null) {
    if (!files?.length) return;
    for (const file of Array.from(files)) {
      setBusy(t('files.uploading', { name: file.name, pct: 0 }));
      const ok = await run(() => uploadFile(server.id, child(file.name), file, (done, size) =>
        setBusy(t('files.uploading', { name: file.name, pct: size ? Math.round(done * 100 / size) : 100 }))), t('files.uploaded', { name: file.name }));
      if (!ok) break;
    }
    setBusy('');
    await load(path);
  }

  async function download(entry: Entry) {
    setBusy(t('files.downloading', { name: entry.name, pct: 0 }));
    try {
      const data = await readChunks(`/servers/${id}/file`, { path: child(entry.name) }, Infinity, (done, size) =>
        setBusy(t('files.downloading', { name: entry.name, pct: size ? Math.round(done * 100 / size) : 100 })));
      saveFile(data, entry.name);
    } catch (e) {
      onToast(errMessage(e), true);
    } finally {
      setBusy('');
    }
  }

  async function mkdir() {
    const name = await ask(t('files.newFolder'), t('files.folderHelp'), t('files.folderName'));
    if (name && await run(() => request('POST', `/servers/${id}/files`, { action: 'mkdir', path: child(name) }))) await load(path);
  }

  async function rename(entry: Entry) {
    const name = await ask(t('files.rename'), t('files.renameHelp', { name: entry.name }), t('files.renameTo'), entry.name);
    if (!name || name === entry.name) return;
    const target = name.includes('/') ? name : child(name);
    if (await run(() => request('POST', `/servers/${id}/files`, { action: 'move', path: child(entry.name), target }))) await load(path);
  }

  async function remove(entry: Entry) {
    const p = child(entry.name);
    if (!await confirmTyped(t('files.deleteTitle'), t(entry.type === 'directory' ? 'files.deleteFolderConfirm' : 'files.deleteConfirm'), entry.name)) return;
    if (await run(() => request('POST', `/servers/${id}/files`, { action: 'delete', path: p, confirm: p }), t('files.deleted', { name: entry.name }))) await load(path);
  }

  async function extract(entry: Entry) {
    const target = await ask(t('files.extract'), t('files.extractHelp'), t('files.extractTo'), child(entry.name.replace(ARCHIVE, '')));
    if (target && await run(() => request('POST', `/servers/${id}/files`, { action: 'extract', path: child(entry.name), target }), t('files.extracted', { name: entry.name }))) await load(path);
  }

  async function importServer(entry: Entry) {
    if (!await confirmTyped(t('files.importServer'), t('files.importConfirm'), server.name, t('files.importAction'))) return;
    if (await run(() => request('POST', `/servers/${id}/files`, { action: 'import', path: child(entry.name), confirm: server.name }), t('files.importDone'))) await load('');
  }

  if (editor) {
    const dirty = editor.text !== editor.original;
    return <div className="mc-tab-body">
      <div className="mc-title-row">
        <div><h3 className="mc-mono">{editor.path}</h3><small>{editor.readOnly ? t('files.readOnly') : dirty ? t('files.unsaved') : t('files.noChanges')}</small></div>
        <div className="mc-actions">
          <Button onClick={() => setEditor(null)}>{editor.readOnly ? t('common.back') : t('common.cancel')}</Button>
          {!editor.readOnly && <Button kind="primary" onClick={() => void saveEditor()} disabled={!!busy || !dirty}>{t('files.save')}</Button>}
        </div>
      </div>
      <textarea className="mc-editor" value={editor.text} readOnly={editor.readOnly} spellCheck={false} aria-label={editor.path}
        onChange={(e) => setEditor({ ...editor, text: e.target.value })} />
    </div>;
  }

  const crumbs = path ? path.split('/') : [];
  return <div className="mc-tab-body">
    <div className="mc-title-row">
      <nav className="mc-crumbs" aria-label={t('files.location')}>
        <button className="mc-link" onClick={() => void load('')}>{server.name}</button>
        {crumbs.map((c, i) => <span key={i}> / <button className="mc-link" onClick={() => void load(crumbs.slice(0, i + 1).join('/'))}>{c}</button></span>)}
      </nav>
      <div className="mc-actions">
        {path && <Button onClick={() => void load(parent)}><Icon name="up" /> {t('files.up')}</Button>}
        <Button onClick={() => void mkdir()}><Icon name="plus" /> {t('files.newFolder')}</Button>
        <label className={`mc-button primary ${busy ? 'disabled' : ''}`}>
          <input className="mc-hidden-file" type="file" multiple disabled={!!busy} onChange={(e) => { void upload(e.target.files); e.currentTarget.value = ''; }} />
          <Icon name="upload" /> {t('files.upload')}</label>
      </div>
    </div>
    {busy && busy !== 'save' && <p className="mc-help" role="status">{busy}</p>}
    <div className="mc-table-wrap">
      <table className="mc-table">
        <thead><tr><th>{t('common.name')}</th><th>{t('files.size')}</th><th>{t('files.modified')}</th><th><span className="mc-sr">{t('common.actions')}</span></th></tr></thead>
        <tbody>{entries.map((e) => <tr key={e.name}>
          <td><button className="mc-link mc-file-name" onClick={() => void open(e)}><Icon name={e.type === 'directory' ? 'folder' : 'file'} /> {e.name}</button></td>
          <td>{e.type === 'directory' ? '—' : formatBytes(e.size)}</td>
          <td>{e.mtime ? new Date(e.mtime * 1000).toLocaleString() : '—'}</td>
          <td className="mc-row-actions">
            {e.type === 'file' && <Button onClick={() => void download(e)} disabled={!!busy} ariaLabel={`${t('files.download')} ${e.name}`} title={t('files.download')}><Icon name="download" /></Button>}
            {e.type === 'file' && ARCHIVE.test(e.name) && <Button onClick={() => void extract(e)}>{t('files.extract')}</Button>}
            {e.type === 'file' && ARCHIVE.test(e.name) && !path && <Button onClick={() => void importServer(e)} disabled={!isStopped(server)} title={isStopped(server) ? undefined : t('backups.stopFirst')}>{t('files.importServer')}</Button>}
            {!RESERVED.has(child(e.name)) && child(e.name) !== 'server.jar' && <>
              <Button onClick={() => void rename(e)} ariaLabel={`${t('files.rename')} ${e.name}`} title={t('files.rename')}><Icon name="edit" /></Button>
              <Button kind="danger" onClick={() => void remove(e)} ariaLabel={`${t('common.delete')} ${e.name}`} title={t('common.delete')}><Icon name="trash" /></Button>
            </>}
          </td>
        </tr>)}</tbody>
      </table>
      {!entries.length && !loading && <Empty>{t('files.empty')}</Empty>}
    </div>
    <p className="mc-help">{t('files.uploadLimit')}</p>
    {dialog}
  </div>;
}

/* ---------------------------------------------------------------- Configuration (server.properties) */

/** Sets key=value in properties text, keeping comments and order (like properties_set in the runtime). */
export function setProperty(text: string, key: string, value: string): string {
  const lines = text.split(/\r?\n/);
  let found = false;
  const out = lines.map((line) => {
    const trimmed = line.trimStart();
    if (found || trimmed.startsWith('#') || trimmed.startsWith('!') || !line.includes('=')) return line;
    if (line.slice(0, line.indexOf('=')).trim() !== key) return line;
    found = true;
    return `${key}=${value}`;
  });
  if (!found) {
    while (out.length && out[out.length - 1] === '') out.pop();
    out.push(`${key}=${value}`, '');
  }
  return out.join('\n');
}

export function parseProperties(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of text.split(/\r?\n/)) {
    const s = line.trim();
    if (!s || s[0] === '#' || s[0] === '!' || !s.includes('=')) continue;
    const i = s.indexOf('=');
    out[s.slice(0, i).trim()] = s.slice(i + 1).trim();
  }
  return out;
}

type PropField = { key: string; kind: 'text' | 'number' | 'bool' | 'choice'; choices?: string[]; def: string };
const PROP_FIELDS: PropField[] = [
  { key: 'motd', kind: 'text', def: 'A Minecraft Server' },
  { key: 'max-players', kind: 'number', def: '20' },
  { key: 'gamemode', kind: 'choice', choices: ['survival', 'creative', 'adventure', 'spectator'], def: 'survival' },
  { key: 'difficulty', kind: 'choice', choices: ['peaceful', 'easy', 'normal', 'hard'], def: 'easy' },
  { key: 'online-mode', kind: 'bool', def: 'true' },
  { key: 'white-list', kind: 'bool', def: 'false' },
  { key: 'enforce-whitelist', kind: 'bool', def: 'false' },
  { key: 'pvp', kind: 'bool', def: 'true' },
  { key: 'hardcore', kind: 'bool', def: 'false' },
  { key: 'allow-flight', kind: 'bool', def: 'false' },
  { key: 'spawn-protection', kind: 'number', def: '16' },
  { key: 'view-distance', kind: 'number', def: '10' },
  { key: 'simulation-distance', kind: 'number', def: '10' },
  { key: 'level-seed', kind: 'text', def: '' },
];

export function ConfigTab({ server, run }: { server: Server; run: Run }) {
  const [text, setText] = useState('');
  const [original, setOriginal] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const id = enc(server.id);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await request<{ text: string }>('GET', `/servers/${id}/properties`);
      setText(r.text || ''); setOriginal(r.text || ''); setError('');
    } catch (e) {
      setError(errMessage(e));
    } finally {
      setLoading(false);
    }
  }, [id]);
  useEffect(() => { void load(); }, [load]);

  const values = parseProperties(text);
  const set = (key: string, value: string) => setText((old) => setProperty(old, key, value));
  async function save() {
    setBusy(true);
    if (await run(() => request('PUT', `/servers/${id}/properties`, { text }), server.state === 'online' ? t('config.savedRestart') : t('common.saved'))) await load();
    setBusy(false);
  }
  const dirty = text !== original;

  if (loading) return <div className="mc-tab-body"><p>{t('common.loading')}</p></div>;
  if (error) return <div className="mc-tab-body"><Warning>{error}</Warning></div>;
  return <div className="mc-tab-body">
    <div className="mc-title-row">
      <div><h3>{t('detail.config')}</h3><p>{t('detail.propertiesHint')}</p></div>
      <div className="mc-actions">
        <Button onClick={() => setText(original)} disabled={!dirty}>{t('config.discard')}</Button>
        <Button kind="primary" onClick={() => void save()} disabled={busy || !dirty}>{t('detail.saveConfig')}</Button>
      </div>
    </div>
    <div className="mc-config-grid">
      {PROP_FIELDS.map((f) => {
        const value = values[f.key] ?? f.def;
        return <Field key={f.key} label={t(`prop.${f.key}`)} hint={f.key}>
          {f.kind === 'bool' ? <select className="mc-input" value={value === 'true' ? 'true' : 'false'} onChange={(e) => set(f.key, e.target.value)}>
            <option value="true">{t('common.yes')}</option><option value="false">{t('common.no')}</option></select>
            : f.kind === 'choice' ? <select className="mc-input" value={value} onChange={(e) => set(f.key, e.target.value)}>
              {!f.choices!.includes(value) && <option value={value}>{value}</option>}
              {f.choices!.map((c) => <option key={c} value={c}>{t(`prop.${f.key}.${c}`)}</option>)}</select>
              : <input className="mc-input" type={f.kind === 'number' ? 'number' : 'text'} value={value} onChange={(e) => set(f.key, e.target.value.replace(/[\r\n]/g, ''))} />}
        </Field>;
      })}
    </div>
    <p className="mc-help">{t('config.portNote', { port: server.port })}</p>
    <Field label={t('detail.rawConfig')}>
      <textarea className="mc-editor mc-editor-short" value={text} spellCheck={false} onChange={(e) => setText(e.target.value)} />
    </Field>
    {server.state === 'online' && <p className="mc-help">{t('detail.pendingRestart')}</p>}
  </div>;
}

/* ---------------------------------------------------------------- Software */

export function SoftwareTab({ server, run }: { server: Server; run: Run }) {
  const [engine, setEngine] = useState(server.engine);
  const [version, setVersion] = useState(server.version);
  const [loader, setLoader] = useState(server.loaderVersion || '');
  const [jar, setJar] = useState<File | null>(null);
  const [busy, setBusy] = useState('');
  const { versions, error, loading } = useVersions(engine);
  const loaders = useLoaders(engine, version);
  const stopped = isStopped(server);
  useEffect(() => { if (versions.length && !versions.includes(version)) setVersion(engine === server.engine && versions.includes(server.version) ? server.version : versions[0]); }, [versions]);
  const sameAsNow = engine === server.engine && version === server.version && (loader || '') === (server.loaderVersion || '') && engine !== 'custom';

  async function apply() {
    if (engine === 'custom' && jar) {
      setBusy(t('files.uploading', { name: jar.name, pct: 0 }));
      const uploaded = await run(() => uploadFile(server.id, 'server.jar', jar, (d, s) => setBusy(t('files.uploading', { name: jar.name, pct: s ? Math.round(d * 100 / s) : 100 }))));
      if (!uploaded) { setBusy(''); return; }
    }
    setBusy(t('software.installing'));
    await run(() => request('POST', `/servers/${enc(server.id)}/software`, { engine, version, ...(loader ? { loaderVersion: loader } : {}) }),
      t('software.done', { engine: t(`engine.${engine}`), version }));
    setBusy('');
    setJar(null);
  }

  return <div className="mc-tab-body">
    <div className="mc-title-row"><div><h3>{t('detail.software')}</h3><p>{t('software.current', { engine: t(`engine.${server.engine}`), version: server.version })}{server.reportedVersion ? ` · ${t('software.reported', { v: server.reportedVersion })}` : ''}</p></div></div>
    {!stopped && <Warning>{t('software.stopFirst')}</Warning>}
    <div className="mc-form-grid">
      <Field label={t('detail.engine')} hint={t(`engine.hint.${engine}`)}>
        <select className="mc-input" value={engine} onChange={(e) => { setEngine(e.target.value); setLoader(''); }}>
          {ENGINES.map((x) => <option key={x} value={x}>{t(`engine.${x}`)}</option>)}</select>
      </Field>
      <Field label={t('servers.version')} hint={error || undefined}>
        <select className="mc-input" value={version} onChange={(e) => { setVersion(e.target.value); setLoader(''); }} disabled={loading}>
          {!versions.includes(version) && <option value={version}>{version}</option>}
          {versions.map((v) => <option key={v}>{v}</option>)}</select>
      </Field>
      {(engine === 'forge' || engine === 'neoforge') && <Field label={t('servers.loader')} hint={t('servers.loaderHint')}>
        <select className="mc-input" value={loader} onChange={(e) => setLoader(e.target.value)}>
          <option value="">{t('servers.loaderLatest')}</option>
          {loader && !loaders.includes(loader) && <option value={loader}>{loader}</option>}
          {loaders.map((v) => <option key={v}>{v}</option>)}</select>
      </Field>}
    </div>
    {engine === 'custom' && <div className="mc-upload-card">
      <div><b>{t('detail.customJar')}</b><p>{jar ? `${jar.name} · ${formatBytes(jar.size)}` : t('detail.jarHint')}</p></div>
      <label className="mc-button"><input className="mc-hidden-file" type="file" accept=".jar,application/java-archive" onChange={(e) => { setJar(e.target.files?.[0] || null); e.currentTarget.value = ''; }} />
        <Icon name="upload" /> {t('common.chooseFile')}</label>
    </div>}
    <p className="mc-help">{t('software.note')}</p>
    <div className="mc-dialog-actions start">
      <Button kind="primary" onClick={() => void apply()} disabled={!stopped || !!busy || sameAsNow || !version}>{busy || t('detail.applySoftware')}</Button>
    </div>
  </div>;
}
