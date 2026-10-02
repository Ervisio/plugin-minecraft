import { getSdk } from './sdk';
import { t, hasString } from './i18n';
import type { Job } from './types';

/** An error from the Minecraft runtime ({"error":{"code","message"}}) or from the Ervisio broker. */
export class ApiError extends Error {
  code: string;
  status: number;
  constructor(message: string, code = 'unavailable', status = 0) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
  }
}

export async function request<T>(method: string, path: string, body?: unknown, query?: Record<string, string>): Promise<T> {
  let response;
  try {
    response = await getSdk().api.http('minecraft', { method, path: `/v1${path}`, body: body as object | undefined, query });
  } catch (error) {
    const err = error as Error & { code?: string };
    throw new ApiError(err.message || 'Request failed', err.code || 'unavailable');
  }
  let data: any;
  try { data = response.json(); } catch { data = response.body ? { message: response.body } : {}; }
  if (response.status < 200 || response.status >= 300) {
    throw new ApiError(data?.error?.message || data?.message || `HTTP ${response.status}`, data?.error?.code || 'request_failed', response.status);
  }
  return data as T;
}

export const enc = (s: string) => encodeURIComponent(s);

/** A message for people: known error codes are translated, the runtime's own (English) message is the fallback. */
export function errMessage(error: unknown): string {
  if (error instanceof ApiError && hasString(`err.${error.code}`)) return t(`err.${error.code}`, { detail: error.message });
  return error instanceof Error ? error.message : String(error);
}

const DONE = new Set(['completed', 'failed']);
export const jobDone = (j: Job) => DONE.has(j.status || '');

/**
 * Waits for a background job of the runtime. Resolves when it completed, throws an ApiError with the job's error when
 * it failed. Long jobs (Forge installers, big backups) can take minutes, so there is no overall timeout: the runtime
 * itself bounds them.
 */
export async function waitJob(jobId: string, onProgress?: (job: Job) => void): Promise<Job> {
  for (;;) {
    const { jobs } = await request<{ jobs: Job[] }>('GET', '/jobs');
    const job = jobs.find((j) => j.id === jobId);
    if (!job) throw new ApiError(t('jobs.lost'), 'job_lost');
    onProgress?.(job);
    if (job.status === 'completed') return job;
    if (job.status === 'failed') throw new ApiError(job.error || t('status.failed'), 'job_failed');
    await new Promise((r) => window.setTimeout(r, 1200));
  }
}

export function encodeBytes(bytes: Uint8Array): string {
  let s = '';
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

export function decodeBytes(value: string): Uint8Array {
  const s = atob(value);
  const out = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
  return out;
}

const CHUNK = 128 * 1024;

/** Reads a whole file (or backup) through a chunked endpoint answering {data, next, eof, size}. */
export async function readChunks(path: string, query: Record<string, string>, limit = Infinity, onProgress?: (done: number, size: number) => void): Promise<Uint8Array> {
  const parts: Uint8Array[] = [];
  let offset = 0;
  let total = 0;
  for (;;) {
    const r = await request<{ data: string; next: number; eof: boolean; size: number }>('GET', path, undefined, { ...query, offset: String(offset), length: String(CHUNK) });
    if (r.size > limit) throw new ApiError(t('files.tooLarge'), 'too_large');
    const bytes = decodeBytes(r.data || '');
    parts.push(bytes);
    total += bytes.length;
    offset = r.next ?? offset + bytes.length;
    onProgress?.(total, r.size);
    if (r.eof || !bytes.length) break;
  }
  const joined = new Uint8Array(total);
  let pos = 0;
  for (const p of parts) { joined.set(p, pos); pos += p.length; }
  return joined;
}

/** Uploads a file to a path relative to the server folder, in ordered 128 KiB chunks. */
export async function uploadFile(serverId: string, target: string, file: File, onProgress?: (done: number, size: number) => void): Promise<void> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let uploadId: string | undefined;
  let offset = 0;
  do {
    const chunk = bytes.subarray(offset, Math.min(bytes.length, offset + CHUNK));
    const r = await request<{ uploadId: string; next: number }>('PUT', `/servers/${enc(serverId)}/file`, {
      path: target, data: encodeBytes(chunk), offset, uploadId, final: offset + chunk.length >= bytes.length, total: bytes.length,
    });
    uploadId = r.uploadId;
    offset = r.next;
    onProgress?.(offset, bytes.length);
  } while (offset < bytes.length);
}

/** Saves bytes as a file in the browser (the frame is sandboxed with allow-downloads). */
export function saveFile(data: Uint8Array, name: string, type = 'application/octet-stream') {
  const url = URL.createObjectURL(new Blob([data as BlobPart], { type }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 2000);
}
