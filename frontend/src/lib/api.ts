/**
 * Typed client for the tao-sentinel JSON API.
 *
 * The shapes mirror what the FastAPI backend actually serves (see
 * tao_sentinel/web/app.py `_build_status` / `_subnet_detail`); they are the
 * single source of truth — if the backend payload changes, change these
 * types alongside it.
 */

export interface SubnetRow {
  netuid: number;
  name: string | null;
  score: number;
  grade: "A" | "B" | "C" | "D" | "F";
  metrics: {
    emission_pct: number | null;
    n_validators: number | null;
    n_validators_is_cap: boolean;
    n_active_validators: number | null;
    n_miners: number | null;
    price_tao: number | null;
    market_cap_tao: number | null;
    provisional?: boolean;
    [k: string]: unknown;
  };
  warnings: string[];
  pinned: boolean;
  spark: number[] | null;
}

export interface Position {
  coldkey: string;
  hotkey: string;
  netuid: number;
  alpha_staked: number;
  value_tao: number | null;
  share_pct: number | null;
  name: string | null;
}

export interface Portfolio {
  coldkey: string;
  positions: Position[];
  total_value_tao: number;
  total_value_usd: number | null;
  tao_price_usd: number | null;
}

export interface AlertItem {
  rule_type: string;
  severity: "info" | "warning" | "critical";
  title: string;
  message: string;
  netuid: number | null;
  timestamp: string;
}

export interface StatusMeta {
  mock: boolean;
  coldkey: string | null;
  watchlist: number[];
  refresh_seconds: number;
  n_subnets: number;
  n_alerts: number;
  generated_at: string;
  tao_price_usd: number | null;
  tao_price_spark: number[] | null;
  provisional: boolean;
}

export interface Status {
  subnets: SubnetRow[];
  portfolio: Portfolio | null;
  alerts: AlertItem[];
  meta: StatusMeta;
}

export interface ValidatorRow {
  hotkey: string;
  stake_tao: number;
  share_pct: number;
  vtrust: number | null;
}

export interface SubnetDetail {
  netuid: number;
  name: string | null;
  report: {
    netuid: number;
    name: string | null;
    score: number;
    grade: "A" | "B" | "C" | "D" | "F";
    metrics: Record<string, unknown>;
    warnings: string[];
  };
  pool: {
    netuid: number;
    name: string | null;
    price_tao: number | null;
    market_cap_tao: number | null;
    tao_in: number | null;
    alpha_in: number | null;
  } | null;
  spark: number[] | null;
  spark_change_pct: number | null;
  validators: ValidatorRow[];
}

async function getJson<T>(url: string): Promise<T> {
  const resp = await fetch(url, { headers: { Accept: "application/json" } });
  if (!resp.ok) {
    throw new Error(`${url} -> HTTP ${resp.status}`);
  }
  return (await resp.json()) as T;
}

export const fetchStatus = () => getJson<Status>("/api/status");
export const fetchSubnet = (netuid: number) =>
  getJson<SubnetDetail>(`/api/subnet/${netuid}`);
