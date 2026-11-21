export interface Env {
  DB: D1Database;
  DELIVERY: Queue<string>;
}
export interface Session {
  label: string | null;
  revision: number;
  address: string;
  machine_id: string;
  repo_id: string;
  native_address: string;
  last_seen: string;
  ended: number;
}
export interface MessageRow {
  sequence: number;
  id: string;
  sender: string;
  recipient: string;
  repo_id: string;
  body: string;
  sent_at: string;
  retention_at: string;
  report_expires_at: string | null;
  received_at: string | null;
  acknowledged_at: string | null;
  reply_to: string | null;
  accepted_reply_to: string | null;
  coordination: string | null;
  availability: string | null;
  approval: string | null;
}
