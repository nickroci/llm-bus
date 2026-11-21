-- Read-only before or after migrations 0008/0009. One statement captures one UTC instant.
WITH clock AS (
  SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now') AS as_of,
         strftime('%Y-%m-%dT%H:%M:%fZ','now','-2 days') AS delivery_cutoff,
         strftime('%Y-%m-%dT%H:%M:%fZ','now','-14 days') AS retention_cutoff
), ages AS (
  SELECT received_at, strftime('%Y-%m-%dT%H:%M:%fZ',sent_at) AS age,
    CASE WHEN strftime('%Y-%m-%dT%H:%M:%fZ',sent_at) IS NOT NULL
      AND substr(sent_at,11,1)='T'
      AND CAST(substr(sent_at,1,4) AS INTEGER) BETWEEN 1 AND 9999
      AND date(substr(sent_at,1,10),'+0 days')=substr(sent_at,1,10)
      AND (substr(sent_at,-1)='Z' OR
        (substr(sent_at,-6,1) IN ('+','-') AND substr(sent_at,-3,1)=':'))
    THEN 1 ELSE 0 END AS valid
  FROM messages
)
SELECT as_of, delivery_cutoff, retention_cutoff,
  (SELECT COUNT(*) FROM ages WHERE valid=0) AS invalid_timestamps,
  (SELECT COUNT(*) FROM ages WHERE valid=1 AND received_at IS NULL AND age<=delivery_cutoff)
    AS expired_delivery_candidates,
  (SELECT MIN(age) FROM ages WHERE valid=1 AND received_at IS NULL AND age<=delivery_cutoff)
    AS oldest_expired_delivery_at,
  (SELECT COUNT(*) FROM ages WHERE valid=1 AND age<=retention_cutoff) AS deletion_candidates,
  (SELECT MIN(age) FROM ages WHERE valid=1 AND age<=retention_cutoff) AS oldest_eligible_at
FROM clock;
