export type Action =
  | 'preview_names'
  | 'preview_full'
  | 'rename'
  | 'artists'
  | 'resume'
  | 'check'
  | 'login'
  | 'login_status'
  | 'authorization_probe'
  | 'discover'
  | 'local_plan'
  | 'reconcile_renames'
  | 'read_playlist'
  | 'save_credentials';
export type Category = 'liked' | 'artist' | 'normal';
export type UpdateStatus = 'none' | 'busy' | 'review_required' | 'stopping';
export interface WriteRecovery {
  status: 'clear' | 'review_required';
  operation: 'renames' | 'artists' | 'classification' | 'unknown';
  can_reconcile: boolean;
}
export interface Playlist {
  key: string;
  name: string;
  track_count: number;
  category: Category;
  source: 'local_record';
  updated_at?: string | null;
}
export interface LocalTrack {
  key: string;
  position: number;
  name: string;
  artists: string[];
  artist_count: number;
  metadata_available: boolean;
}
export type MetadataFilter = 'all' | 'incomplete';
export interface LocalTrackPage {
  status: 'available' | 'not_loaded' | 'unavailable' | 'missing_playlist';
  source: 'local_record';
  metadata_filter: MetadataFilter;
  playlist: Playlist | null;
  updated_at: string | null;
  counts: {
    expected: number | null;
    observed: number | null;
    missing: number | null;
    metadata_missing: number | null;
  };
  pagination: { offset: number; limit: number; total: number; next_offset: number | null };
  tracks: LocalTrack[];
}
export interface HistoryItem {
  name: string;
  count?: number;
  expected_count?: number;
  added_count?: number;
  phase?: 'pending' | 'created' | 'adding' | 'added' | 'completed';
  status: string;
}
export interface History {
  status: string;
  completed_count: number;
  items: HistoryItem[];
  resumable: boolean;
  operation?: 'artists' | 'renames' | 'classification';
  updated_at?: string;
}
export interface JobResult {
  playlist_key?: string;
  expected_count?: number;
  count?: number;
  missing_count?: number;
  missing_metadata_count?: number;
  status?: string;
  message?: string;
  completed_count?: number;
  job_count?: number;
  blocked_count?: number;
  authorized?: boolean;
  error_code?: ErrorCode;
  next_step?: NextStep;
  outcome_known?: boolean;
  write_attempted?: boolean;
  applied_to_account?: boolean | null;
  record_saved?: boolean;
  resumable?: boolean;
  url?: string;
  qr_png_base64?: string;
  path?: string;
  performance?: { call_count?: number; elapsed_seconds?: number };
}
export const ERROR_CODES = [
  'node_missing',
  'cli_missing',
  'credentials_required',
  'cli_timeout',
  'invalid_app_id',
  'invalid_private_key',
  'local_permission_denied',
  'local_snapshot_unavailable',
  'account_mismatch',
  'operation_failed',
] as const;
export type ErrorCode = (typeof ERROR_CODES)[number];
export const NEXT_STEPS = [
  'install_cli',
  'configure_credentials',
  'correct_credentials',
  'check_local_access',
  'load_desktop_playlists',
  'authorize_correct_account',
  'inspect_records',
] as const;
export type NextStep = (typeof NEXT_STEPS)[number];
export interface Job {
  id: string;
  action: Action;
  playlist_key?: string;
  label: string;
  status: string;
  progress?: {
    stage?: 'classification_preflight';
    label?: string;
    completed_count?: number;
    total_count?: number;
    elapsed_seconds?: number;
  };
  result?: JobResult | null;
  logs: { time: string; level: string; message: string }[];
}

export type ClassificationDimension = 'all' | 'scene' | 'style' | 'language';
export interface ClassificationQuery {
  offset: number;
  query: string;
  dimension: ClassificationDimension;
  tag: string;
  review: 'all' | 'pending';
}
export interface ClassificationRecord {
  position: number;
  name: string;
  artists: string;
  styles: string[];
  scenes: string[];
  language: string;
  pending_reasons: string[];
  evidence_note: string;
  language_evidence_note: string;
}
export interface ClassificationPage {
  status: 'available' | 'not_loaded' | 'unavailable';
  source: 'local_record';
  verification: 'verified' | 'local_only';
  updated_at: string | null;
  verified_at: string | null;
  summary: null | {
    source_count: number;
    covered_count: number;
    playlist_count: number;
    pending_count: number;
    unknown_style_count: number;
    unknown_language_count: number;
  };
  options: { scene: string[]; style: string[]; language: string[] };
  playlists: { name: string; dimension: 'scene' | 'style' | 'language' | 'review'; count: number; key: string | null }[];
  filters: Omit<ClassificationQuery, 'offset'>;
  pagination: { offset: number; limit: number; total: number; next_offset: number | null };
  records: ClassificationRecord[];
}
export interface PlaylistReadIntent {
  key: string;
  baseline_id: string | null;
  job_id: string | null;
  sequence: number;
  acceptance: 'sending' | 'accepted' | 'unknown';
}
export interface LocalPreview {
  source: 'local_record';
  scope: 'names' | 'full';
  updated_at: string;
  rename_count: number;
  renames: { key: string; old_name: string; name: string }[];
  artists: { name: string; count: number; tracks: { name: string; artists: string[] }[] }[];
  limitations: string[];
  liked: { expected: number; observed: number | null; missing: number | null };
}
export interface WorkbenchState {
  update?: { status: UpdateStatus };
  recovery?: WriteRecovery;
  connection: { installed: boolean; configured: boolean; authorized: boolean | null };
  data: {
    account: { nickname: string } | null;
    source: 'local_record' | 'empty';
    updated_at?: string | null;
    playlists: Playlist[];
    history: History | null;
    artists_completed: boolean;
    preview?: LocalPreview | null;
  };
  job: Job | null;
}
export interface AcceptedAction {
  accepted: boolean;
  job_id?: string;
  message?: string;
}
