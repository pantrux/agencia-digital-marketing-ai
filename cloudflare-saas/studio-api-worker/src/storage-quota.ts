// Las copias del mismo archivo en R2 y NAS representan un solo archivo lógico.
export const storageFits = `(p.storage_limit_bytes IS NULL OR ? + COALESCE((
  SELECT SUM(s.bytes) FROM upload_sessions s JOIN orders previous ON previous.id = s.order_id
  JOIN plan_quotas previous_plan ON previous_plan.plan_id = previous.plan_id
  WHERE s.user_id = o.user_id AND COALESCE(previous.provider_mode, 'production') = COALESCE(o.provider_mode, 'production')
    AND (s.status = 'completing'
      OR (s.status IN ('creating', 'open', 'aborting') AND (s.expires_at > ? OR s.lease_until > ?))
      OR (s.status = 'completed' AND (previous.status IN ('queued', 'processing')
        OR previous_plan.retention_days IS NULL
        OR COALESCE((SELECT MAX(k.created_at) FROM delivery_kits k WHERE k.order_id = previous.id
          AND k.user_id = s.user_id AND k.provider_mode = COALESCE(previous.provider_mode, 'production')),
          previous.updated_at) + previous_plan.retention_days * 86400 > ?)))
), 0) + COALESCE((
  SELECT SUM(k.bytes + COALESCE(k.prores_bytes, 0)) FROM delivery_kits k JOIN orders previous ON previous.id = k.order_id
  JOIN plan_quotas previous_plan ON previous_plan.plan_id = previous.plan_id
  WHERE k.user_id = o.user_id AND k.provider_mode = COALESCE(o.provider_mode, 'production')
    AND (previous_plan.retention_days IS NULL OR k.created_at + previous_plan.retention_days * 86400 > ?)
), 0) <= p.storage_limit_bytes)`;

export function storageQuotaArguments(bytes: number, timestamp: number): [number, number, number, number, number] {
  return [bytes, timestamp, timestamp, timestamp, timestamp];
}
