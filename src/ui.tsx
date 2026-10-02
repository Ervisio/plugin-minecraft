import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { t } from './i18n';

export function Button({ children, onClick, kind = '', disabled = false, type = 'button', title, ariaLabel }: {
  children: ReactNode; onClick?: () => void; kind?: string; disabled?: boolean; type?: 'button' | 'submit'; title?: string; ariaLabel?: string;
}) {
  return <button type={type} disabled={disabled} title={title} aria-label={ariaLabel} className={`mc-button ${kind}`} onClick={onClick}>{children}</button>;
}

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return <label className="mc-field"><span>{label}</span>{children}{hint && <small>{hint}</small>}</label>;
}

export const Empty = ({ children }: { children: ReactNode }) => <div className="mc-empty">{children}</div>;
export const Warning = ({ children }: { children: ReactNode }) => <div className="mc-warning" role="status">{children}</div>;

export function formatBytes(bytes?: number | null) {
  if (bytes == null || !Number.isFinite(bytes)) return '—';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let n = bytes, u = 0;
  while (n >= 1024 && u < units.length - 1) { n /= 1024; u++; }
  return `${n.toFixed(u > 1 ? 1 : 0)} ${units[u]}`;
}

export function formatDuration(seconds: number) {
  const d = Math.floor(seconds / 86400), h = Math.floor(seconds % 86400 / 3600), m = Math.floor(seconds % 3600 / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}

export function useInterval(fn: () => void, ms: number | null) {
  const ref = useRef(fn);
  useEffect(() => { ref.current = fn; }, [fn]);
  useEffect(() => {
    if (ms == null) return;
    const id = window.setInterval(() => ref.current(), ms);
    return () => window.clearInterval(id);
  }, [ms]);
}

// Escape closes only the topmost layer (a dialog above the server drawer).
const escapeStack: { current: () => void }[] = [];
let escapeListening = false;

/** Closes something on Escape, while it is mounted and nothing opened above it. */
export function useEscape(onEscape: () => void) {
  const ref = useRef(onEscape);
  useEffect(() => { ref.current = onEscape; }, [onEscape]);
  useEffect(() => {
    if (!escapeListening) {
      escapeListening = true;
      window.addEventListener('keydown', (e) => { if (e.key === 'Escape') escapeStack[escapeStack.length - 1]?.current(); });
    }
    escapeStack.push(ref);
    return () => { escapeStack.splice(escapeStack.indexOf(ref), 1); };
  }, []);
}

export function Modal({ title, children, onClose, wide = false, alert = false }: { title: string; children: ReactNode; onClose: () => void; wide?: boolean; alert?: boolean }) {
  useEscape(onClose);
  return <div className="mc-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
    <section className={`mc-dialog ${wide ? 'mc-dialog-wide' : ''}`} role={alert ? 'alertdialog' : 'dialog'} aria-modal="true" aria-label={title}>
      <header><h2>{title}</h2><Button onClick={onClose} ariaLabel={t('common.close')}>×</Button></header>
      {children}
    </section>
  </div>;
}

type Ask =
  | { kind: 'text'; title: string; help: string; label: string; initial: string; done: (v: string | null) => void }
  | { kind: 'typed'; title: string; help: string; word: string; action: string; done: (v: boolean) => void };

/**
 * Promise-based dialogs (the frame's sandbox blocks window.prompt/confirm). Render `dialog` once in the component.
 * - ask(title, help, label, initial) → the text, or null when cancelled
 * - confirmTyped(title, help, word, action) → true when the person typed `word` and confirmed
 */
export function useDialogs() {
  const [state, setState] = useState<Ask | null>(null);
  const [value, setValue] = useState('');
  const ask = useCallback((title: string, help: string, label: string, initial = '') => new Promise<string | null>((done) => {
    setValue(initial);
    setState({ kind: 'text', title, help, label, initial, done });
  }), []);
  const confirmTyped = useCallback((title: string, help: string, word: string, action = t('common.delete')) => new Promise<boolean>((done) => {
    setValue('');
    setState({ kind: 'typed', title, help, word, action, done });
  }), []);
  const close = (answer: boolean) => {
    if (!state) return;
    setState(null);
    if (state.kind === 'text') state.done(answer ? value.trim() : null);
    else state.done(answer);
  };
  const dialog = state ? <Modal title={state.title} onClose={() => close(false)} alert={state.kind === 'typed'}>
    <form onSubmit={(e) => { e.preventDefault(); if (state.kind === 'text' ? value.trim() : value === state.word) close(true); }}>
      <p className="mc-dialog-text">{state.help}</p>
      <Field label={state.kind === 'text' ? state.label : t('misc.confirmText', { value: state.word })}>
        <input className="mc-input" autoFocus value={value} onChange={(e) => setValue(e.target.value)} spellCheck={false} />
      </Field>
      <div className="mc-dialog-actions">
        <Button onClick={() => close(false)}>{t('common.cancel')}</Button>
        {state.kind === 'text'
          ? <Button type="submit" kind="primary" disabled={!value.trim()}>{t('common.confirm')}</Button>
          : <Button type="submit" kind="danger" disabled={value !== state.word}>{state.action}</Button>}
      </div>
    </form>
  </Modal> : null;
  return { ask, confirmTyped, dialog };
}
