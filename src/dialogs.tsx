import { useEffect, useState, type FormEvent } from 'react';
import { enc, errMessage, request, waitJob } from './api';
import { t } from './i18n';
import { getSdk } from './sdk';
import { Button, Field, Modal, Warning, useDialogs } from './ui';
import type { Run, Server } from './types';

export const ENGINES = ['paper', 'vanilla', 'purpur', 'fabric', 'forge', 'neoforge', 'custom'];
const LOADER_ENGINES = new Set(['forge', 'neoforge']);

/** Versions of an engine from the official catalogues (custom servers use the vanilla list for their label). */
export function useVersions(engine: string) {
  const [versions, setVersions] = useState<string[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let live = true;
    setLoading(true); setError('');
    request<{ versions: string[] }>('GET', '/catalog/versions', undefined, { engine: engine === 'custom' ? 'vanilla' : engine })
      .then((r) => { if (live) setVersions(r.versions || []); })
      .catch((e) => { if (live) { setVersions([]); setError(errMessage(e)); } })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [engine]);
  return { versions, error, loading };
}

/** Forge/NeoForge builds for a Minecraft version (newest first); empty for other engines. */
export function useLoaders(engine: string, version: string) {
  const [loaders, setLoaders] = useState<string[]>([]);
  useEffect(() => {
    let live = true;
    setLoaders([]);
    if (!LOADER_ENGINES.has(engine) || !version) return;
    request<{ versions: string[] }>('GET', '/catalog/loaders', undefined, { engine, version })
      .then((r) => { if (live) setLoaders(r.versions || []); }).catch(() => { if (live) setLoaders([]); });
    return () => { live = false; };
  }, [engine, version]);
  return loaders;
}

export const splitArgs = (s: string) => s.split(/\s+/).filter(Boolean);

function freePort(servers: Server[]) {
  const used = new Set(servers.map((s) => s.port));
  let port = 25565;
  while (used.has(port) && port < 65535) port++;
  return port;
}

export function CreateDialog({ servers, onClose, onCreated, run }: { servers: Server[]; onClose: () => void; onCreated: (s: Server) => void; run: Run }) {
  const [name, setName] = useState('');
  const [engine, setEngine] = useState('paper');
  const [version, setVersion] = useState('');
  const [loader, setLoader] = useState('');
  const [port, setPort] = useState(freePort(servers));
  const [memory, setMemory] = useState(4096);
  const [java, setJava] = useState('');
  const [jvm, setJvm] = useState('');
  const [seed, setSeed] = useState('');
  const [motd, setMotd] = useState('');
  const [autostart, setAutostart] = useState(false);
  const [eula, setEula] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const { versions, error: versionsError, loading } = useVersions(engine);
  const loaders = useLoaders(engine, version);
  useEffect(() => { if (!versions.includes(version)) setVersion(versions[0] || ''); }, [versions]);
  useEffect(() => { setLoader(''); }, [engine, version]);
  const portTaken = servers.some((s) => s.port === port);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!eula) { setError(t('servers.eulaRequired')); return; }
    setBusy(true); setError('');
    try {
      const created = await request<Server>('POST', '/servers', {
        name: name.trim(), engine, version, port, memoryMB: memory, autostart, eula: true,
        ...(loader ? { loaderVersion: loader } : {}), ...(java.trim() ? { java: java.trim() } : {}),
        jvmArgs: splitArgs(jvm), ...(seed ? { seed } : {}), ...(motd ? { motd } : {}),
      });
      onCreated(created);
      // The download/installer runs as a job; the drawer shows it while it runs and run() reports how it ended.
      if (created.jobId) void run(() => waitJob(created.jobId!), t('servers.installed', { name: created.name }));
      else void run(async () => undefined);
    } catch (x) {
      setError(errMessage(x));
    } finally {
      setBusy(false);
    }
  }

  return <Modal title={t('servers.createTitle')} onClose={onClose} wide>
    <form onSubmit={(e) => void submit(e)}>
      <Field label={t('servers.serverName')} hint={t('servers.nameHint')}>
        <input className="mc-input" value={name} onChange={(e) => setName(e.target.value)} required maxLength={64} autoFocus />
      </Field>
      <div className="mc-form-grid">
        <Field label={t('servers.engine')} hint={t(`engine.hint.${engine}`)}>
          <select className="mc-input" value={engine} onChange={(e) => setEngine(e.target.value)}>
            {ENGINES.map((x) => <option key={x} value={x}>{t(`engine.${x}`)}</option>)}</select>
        </Field>
        <Field label={t('servers.version')} hint={versionsError || undefined}>
          <select className="mc-input" value={version} onChange={(e) => setVersion(e.target.value)} required disabled={loading}>
            {loading && <option value="">{t('common.loading')}</option>}
            {versions.map((v) => <option key={v}>{v}</option>)}</select>
        </Field>
        {LOADER_ENGINES.has(engine) && <Field label={t('servers.loader')} hint={t('servers.loaderHint')}>
          <select className="mc-input" value={loader} onChange={(e) => setLoader(e.target.value)}>
            <option value="">{t('servers.loaderLatest')}</option>{loaders.map((v) => <option key={v}>{v}</option>)}</select>
        </Field>}
        <Field label={t('servers.port')} hint={portTaken ? t('servers.portTaken') : t('servers.portHint')}>
          <input className="mc-input" type="number" min={1024} max={65535} value={port} onChange={(e) => setPort(Number(e.target.value))} required />
        </Field>
        <Field label={t('servers.memory')} hint={t('servers.memoryHint')}>
          <input className="mc-input" type="number" min={512} max={131072} step={256} value={memory} onChange={(e) => setMemory(Number(e.target.value))} required />
        </Field>
        <Field label={t('servers.motd')}><input className="mc-input" value={motd} onChange={(e) => setMotd(e.target.value)} maxLength={256} /></Field>
        <Field label={t('servers.seed')}><input className="mc-input" value={seed} onChange={(e) => setSeed(e.target.value)} maxLength={128} /></Field>
        <Field label={t('servers.java')} hint={t('servers.javaHint')}><input className="mc-input mc-mono" value={java} onChange={(e) => setJava(e.target.value)} placeholder="java" /></Field>
        <Field label={t('servers.jvm')} hint={t('servers.jvmHint')}><input className="mc-input mc-mono" value={jvm} onChange={(e) => setJvm(e.target.value)} placeholder="-XX:+UseG1GC" /></Field>
      </div>
      {engine === 'custom' && <Warning>{t('servers.customNote')}</Warning>}
      <label className="mc-check"><input type="checkbox" checked={autostart} onChange={(e) => setAutostart(e.target.checked)} /><span>{t('servers.autostart')}</span></label>
      <label className="mc-check mc-eula"><input type="checkbox" checked={eula} onChange={(e) => setEula(e.target.checked)} />
        <span>{t('servers.eula')} <button type="button" className="mc-link" onClick={() => getSdk().openExternal('https://www.minecraft.net/eula')}>minecraft.net/eula</button></span></label>
      {error && <Warning>{error}</Warning>}
      <div className="mc-dialog-actions">
        <Button onClick={onClose}>{t('common.cancel')}</Button>
        <Button type="submit" kind="primary" disabled={busy || !version || !eula || portTaken || !name.trim()}>{busy ? t('detail.working') : t('common.create')}</Button>
      </div>
    </form>
  </Modal>;
}

export function EditDialog({ server, run, onClose, onDeleted }: { server: Server; run: Run; onClose: () => void; onDeleted: () => void }) {
  const [name, setName] = useState(server.name);
  const [port, setPort] = useState(server.port);
  const [memory, setMemory] = useState(server.memoryMB);
  const [java, setJava] = useState(server.java || '');
  const [jvm, setJvm] = useState((server.jvmArgs || []).join(' '));
  const [autostart, setAutostart] = useState(!!server.autostart);
  const [busy, setBusy] = useState(false);
  const { confirmTyped, dialog } = useDialogs();
  const stopped = server.state === 'offline' || server.state === 'error' || !server.state;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    const ok = await run(() => request('PATCH', `/servers/${enc(server.id)}`, {
      name: name.trim(), port, memoryMB: memory, autostart, jvmArgs: splitArgs(jvm), ...(java.trim() ? { java: java.trim() } : {}),
    }), t('common.saved'));
    setBusy(false);
    if (ok) onClose();
  }

  async function remove() {
    if (!await confirmTyped(t('servers.deleteTitle'), t('servers.confirmDelete'), server.name, t('servers.delete'))) return;
    if (await run(() => request('DELETE', `/servers/${enc(server.id)}`, { confirm: server.name }), t('servers.deleted', { name: server.name }))) onDeleted();
  }

  return <Modal title={t('detail.editServer')} onClose={onClose}>
    <form onSubmit={(e) => void submit(e)}>
      <Field label={t('servers.serverName')}><input className="mc-input" value={name} onChange={(e) => setName(e.target.value)} required maxLength={64} /></Field>
      {!stopped && <Warning>{t('servers.stopToEdit')}</Warning>}
      <div className="mc-form-grid">
        <Field label={t('servers.port')}><input className="mc-input" type="number" min={1024} max={65535} value={port} disabled={!stopped} onChange={(e) => setPort(Number(e.target.value))} required /></Field>
        <Field label={t('servers.memory')}><input className="mc-input" type="number" min={512} max={131072} step={256} value={memory} disabled={!stopped} onChange={(e) => setMemory(Number(e.target.value))} required /></Field>
        <Field label={t('servers.java')} hint={t('servers.javaHint')}><input className="mc-input mc-mono" value={java} disabled={!stopped} onChange={(e) => setJava(e.target.value)} placeholder="java" /></Field>
        <Field label={t('servers.jvm')} hint={t('servers.jvmHint')}><input className="mc-input mc-mono" value={jvm} disabled={!stopped} onChange={(e) => setJvm(e.target.value)} /></Field>
      </div>
      <label className="mc-check"><input type="checkbox" checked={autostart} onChange={(e) => setAutostart(e.target.checked)} /><span>{t('servers.autostart')}</span></label>
      <div className="mc-dialog-actions split">
        <Button kind="danger" onClick={() => void remove()} disabled={!stopped} title={stopped ? undefined : t('servers.stopToDelete')}>{t('servers.delete')}</Button>
        <span className="mc-spacer" />
        <Button onClick={onClose}>{t('common.cancel')}</Button>
        <Button type="submit" kind="primary" disabled={busy}>{t('common.save')}</Button>
      </div>
    </form>
    {dialog}
  </Modal>;
}
