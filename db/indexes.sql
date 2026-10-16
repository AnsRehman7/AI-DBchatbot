-- Keys and indexes from the MariaDB dump, created after the bulk load.

ALTER TABLE app_auth_log ADD PRIMARY KEY (id);
CREATE INDEX idx_auth_log_at ON app_auth_log (at);
CREATE INDEX idx_auth_log_email ON app_auth_log (email);

ALTER TABLE app_otp_codes ADD PRIMARY KEY (email);
ALTER TABLE app_rate_limits ADD PRIMARY KEY (rl_key);

ALTER TABLE app_sessions ADD PRIMARY KEY (id);
CREATE INDEX idx_sessions_email ON app_sessions (email);
CREATE INDEX idx_sessions_expires ON app_sessions (expires_at);

ALTER TABLE app_settings ADD PRIMARY KEY (k);
ALTER TABLE app_users ADD PRIMARY KEY (email);

ALTER TABLE vendors_list ADD PRIMARY KEY (zoho_id);
ALTER TABLE vendors_list_new ADD CONSTRAINT vendors_list_new_zoho_id_key UNIQUE (zoho_id);
ALTER TABLE vendors_list_sandbox ADD CONSTRAINT vendors_list_sandbox_zoho_id_key UNIQUE (zoho_id);

ALTER TABLE vendor_zoning ADD PRIMARY KEY (id);
ALTER TABLE vendor_zoning ADD CONSTRAINT idx_zoho_zoning_id UNIQUE (zoho_zoning_id);
CREATE INDEX idx_vendor_id ON vendor_zoning (vendor_id);
CREATE INDEX idx_task_type ON vendor_zoning (task_type);
CREATE INDEX idx_active ON vendor_zoning (zone_data_active);
CREATE INDEX idx_vendor_active ON vendor_zoning (vendor_id, zone_data_active);
CREATE INDEX idx_is_deleted ON vendor_zoning (is_deleted);

-- MySQL also had idx_lookup (vendor_id, zip_code) and idx_vendor (vendor_id);
-- both are covered by uq_vendor_zip, so they are not recreated.
ALTER TABLE vendor_zip_driving_distance ADD PRIMARY KEY (id);
ALTER TABLE vendor_zip_driving_distance ADD CONSTRAINT uq_vendor_zip UNIQUE (vendor_id, zip_code);
CREATE INDEX idx_zip ON vendor_zip_driving_distance (zip_code);

ALTER TABLE vendor_cache_log ADD PRIMARY KEY (vendor_id);
CREATE INDEX idx_status ON vendor_cache_log (status);

ALTER TABLE vendor_cache_queue ADD PRIMARY KEY (vendor_id);
CREATE INDEX idx_processed ON vendor_cache_queue (processed);

ALTER TABLE zip_centroids ADD PRIMARY KEY (zip_code);
CREATE INDEX idx_state ON zip_centroids (state);
CREATE INDEX idx_lat ON zip_centroids (lat);
CREATE INDEX idx_lng ON zip_centroids (lng);

ALTER TABLE missing_zip_centroids ADD PRIMARY KEY (zip_code);
CREATE INDEX idx_resolved ON missing_zip_centroids (resolved);
CREATE INDEX idx_last_seen ON missing_zip_centroids (last_seen);

-- Rows were inserted with explicit ids, so move the identity sequences past them.
SELECT setval(pg_get_serial_sequence('app_auth_log', 'id'), max(id)) FROM app_auth_log;
SELECT setval(pg_get_serial_sequence('vendor_zoning', 'id'), max(id)) FROM vendor_zoning;
SELECT setval(pg_get_serial_sequence('vendor_zip_driving_distance', 'id'), max(id)) FROM vendor_zip_driving_distance;

ANALYZE;
