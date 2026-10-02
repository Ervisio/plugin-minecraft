export type ServerState = 'online' | 'offline' | 'starting' | 'stopping' | 'error';

/** GET /v1/servers item (see MinecraftRuntime.server_view). */
export interface Server {
  id: string;
  name: string;
  engine: string;
  version: string;
  loaderVersion?: string | null;
  port: number;
  memoryMB: number;
  autostart?: boolean;
  java?: string;
  jvmArgs?: string[];
  state?: ServerState;
  error?: string;
  jobId?: string;
  cpu?: number | null;
  rssMB?: number | null;
  playersOnline?: number | null;
  maxPlayers?: number | null;
  reportedVersion?: string;
  tps?: number | null;
}

export interface Job {
  id: string;
  serverId?: string | null;
  action?: string;
  status?: 'running' | 'completed' | 'failed';
  progress?: number | null;
  error?: string;
}

export interface Health {
  version: string;
  java: string;
  data: string;
  uptime: number;
  disk?: { total: number; free: number };
}

export interface ActivityEvent {
  time: string;
  serverId?: string | null;
  action: string;
  result: string;
}

export interface Entry { name: string; type: 'file' | 'directory'; size?: number; mtime?: number }
export interface Addon { name: string; path: string; enabled: boolean; projectId?: string | null; versionId?: string | null }
export interface CatalogHit { projectId: string; title: string; description?: string; icon?: string; downloads?: number }
export type PlayerItem = { name: string; uuid?: string | null };
export interface PlayerData { online?: PlayerItem[]; whitelist?: PlayerItem[]; ops?: PlayerItem[]; banned?: PlayerItem[] }
export interface World { name: string; path?: string; active?: boolean }
export interface Backup { id: string; name: string; size?: number; created?: string }
export interface Schedule { id: string; action: 'backup' | 'restart'; hour: number; minute: number; enabled: boolean; lastRun?: string | null }

/** run() of the page: performs an action, waits for its job when it returns one, reports the outcome. */
export type Run = (fn: () => Promise<unknown>, done?: string) => Promise<boolean>;
