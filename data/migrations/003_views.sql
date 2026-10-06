-- 003_views.sql — the measurement sheet (the report's four columns, one query).
-- From PLAN.md §3.4. `dmctl sheet` = SELECT variant, sent_to, replied, booked FROM sheet.

CREATE VIEW sheet AS
SELECT
  m.variant_id                                   AS variant,
  c.address                                      AS sent_to,
  CASE WHEN EXISTS (SELECT 1 FROM replies r
                    WHERE r.prospect_id = m.prospect_id AND r.is_human = 1)
       THEN 'y' ELSE 'n' END                     AS replied,
  CASE WHEN o.outcome IN ('demo_booked','call_booked')
       THEN 'y' ELSE 'n' END                     AS booked,
  -- context columns (allowed extras; the four above are the contract)
  p.company_name, p.market, m.channel, m.sent_at,
  CASE WHEN m.state = 'BOUNCED' THEN 1 ELSE 0 END AS bounced
FROM messages m
JOIN prospects p ON p.id = m.prospect_id
JOIN contacts  c ON c.id = m.contact_id
LEFT JOIN outcomes o ON o.prospect_id = m.prospect_id
WHERE m.sequence_step = 1
  AND m.state IN ('SENT','REPLIED','BOUNCED','CLOSED');
