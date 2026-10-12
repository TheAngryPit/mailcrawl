INSERT INTO senders(account_id, address_norm, domain) VALUES
  (1, 'bao1@example.com', 'example.com'),
  (1, 'bao2@example.com', 'example.com'),
  (1, 'bao3@example.com', 'example.com'),
  (1, 'bao4@example.com', 'example.com');

INSERT INTO threads(account_id, gmail_thread_id) VALUES
  (1, 'old-1'),
  (1, 'old-2'),
  (1, 'old-3'),
  (1, 'old-4');

INSERT INTO messages(
  account_id, gmail_message_id, thread_pk, internal_date, sender_id,
  body_text, is_from_me, raw_sha256, provenance_hash, imported_at
) VALUES
  (1, 'old-msg-1', 1, 1699913600, 1, 'hello there', 0, 'raw', 'prov', 1700000000),
  (1, 'old-msg-2', 2, 1699913600, 2, 'star note', 0, 'raw', 'prov', 1700000000),
  (1, 'old-msg-3', 3, 1699913600, 3, 'note', 1, 'raw', 'prov', 1700000000),
  (1, 'old-msg-4', 4, 1699913600, 4, 'promo note', 0, 'raw', 'prov', 1700000000);

INSERT INTO labels(
  account_id, gmail_label_id, name, kind, axis, in_catalogue, catalogue_key, seen_at
) VALUES
  (1, 'CATEGORY_PERSONAL', 'CATEGORY_PERSONAL', 'system', NULL, 0, NULL, 1700000000),
  (1, 'STARRED', 'STARRED', 'system', NULL, 0, NULL, 1700000000),
  (1, 'CATEGORY_PROMOTIONS', 'CATEGORY_PROMOTIONS', 'system', 'content type', 1, 'newsletter', 1700000000),
  (1, 'pseudo:archived', 'pseudo:archived', 'system', NULL, 0, NULL, 1700000000),
  (1, 'Example/Custom', 'Example/Custom', 'user', NULL, 0, NULL, 1700000000);

INSERT INTO message_labels(msg_pk, account_id, gmail_label_id, observed_at) VALUES
  (1, 1, 'CATEGORY_PERSONAL', 1700000000),
  (2, 1, 'STARRED', 1700000000),
  (4, 1, 'CATEGORY_PROMOTIONS', 1700000000),
  (1, 1, 'pseudo:archived', 1700000000);
