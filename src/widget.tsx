import { useEffect, useState } from 'react';
import { errMessage, request } from './api';
import { t } from './i18n';
import { getSdk } from './sdk';
import type { Server } from './types';

const order = (s: Server) => (s.state === 'error' ? 0 : s.state === 'online' ? 1 : s.state === 'starting' || s.state === 'stopping' ? 2 : 3);

/** Overview widget: running count, then servers with problems first. */
export function ServersWidget() {
  const [servers, setServers] = useState<Server[] | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let live = true;
    const load = () => request<{ servers: Server[] }>('GET', '/servers')
      .then((r) => { if (live) { setServers(r.servers || []); setError(''); } })
      .catch((e) => { if (live) setError(errMessage(e)); });
    void load();
    const id = window.setInterval(load, 15000);
    return () => { live = false; window.clearInterval(id); };
  }, []);
  const list = [...(servers || [])].sort((a, b) => order(a) - order(b));
  const online = list.filter((s) => s.state === 'online').length;
  return <section className="mc-root mc-widget">
    <header><b>{t('app.title')}</b><span>{servers ? t('widget.summary', { online, total: list.length }) : ''}</span></header>
    {error ? <small role="status">{t('error.title')}</small>
      : !servers ? <small>{t('common.loading')}</small>
        : !list.length ? <small>{t('servers.empty')}</small>
          : list.slice(0, 5).map((s) => <div className={`mc-widget-row ${s.state === 'error' ? 'bad' : ''}`} key={s.id}>
            <i className={`mc-health-dot ${s.state === 'online' ? 'online' : s.state === 'error' ? 'error' : ''}`} />
            <b>{s.name}</b>
            <small>{s.state === 'online' && s.playersOnline != null ? t('servers.playersOf', { online: s.playersOnline, max: s.maxPlayers ?? '—' }) : t(`servers.state.${s.state || 'offline'}`)}</small>
          </div>)}
    {list.length > 5 && <button className="mc-link" onClick={() => getSdk().open('minecraft')}>{t('widget.more', { n: list.length - 5 })}</button>}
  </section>;
}
