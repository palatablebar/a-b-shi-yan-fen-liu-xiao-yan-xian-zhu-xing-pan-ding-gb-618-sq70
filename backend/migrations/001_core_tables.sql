-- 参考建表语句（运行时由 SQLAlchemy Base.metadata.create_all 建表）。
-- 口径：MySQL 8.0 / utf8mb4；assignments 的自然键保证 seed 幂等。

CREATE TABLE IF NOT EXISTS experiments (
  id INT AUTO_INCREMENT PRIMARY KEY,
  code VARCHAR(40) NOT NULL UNIQUE,
  name VARCHAR(120) NOT NULL,
  hypothesis TEXT,
  owner VARCHAR(80) NOT NULL DEFAULT '橙野 App · 增长实验组',
  primary_metric VARCHAR(40) NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'running',
  baseline_rate FLOAT NOT NULL DEFAULT 0.1,
  planned_mde FLOAT NOT NULL DEFAULT 0.01,
  alpha FLOAT NOT NULL DEFAULT 0.05,
  power_target FLOAT NOT NULL DEFAULT 0.8,
  traffic_note TEXT NULL,
  started_on DATE NULL,
  ended_on DATE NULL,
  srm_chi2 FLOAT NULL,
  srm_df INT NULL,
  srm_p_value FLOAT NULL,
  srm_status VARCHAR(12) NOT NULL DEFAULT 'unknown',
  srm_checked_at DATETIME NULL,
  required_per_variant INT NULL,
  power_at_current FLOAT NULL,
  mde_at_current FLOAT NULL,
  srm_payload JSON NULL,
  has_srm_alarm TINYINT(1) NOT NULL DEFAULT 0,
  seed_key VARCHAR(120) NULL UNIQUE,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  KEY ix_experiments_status (status),
  KEY ix_experiments_primary_metric (primary_metric),
  KEY ix_experiments_has_srm_alarm (has_srm_alarm)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS variants (
  id INT AUTO_INCREMENT PRIMARY KEY,
  experiment_id INT NOT NULL,
  code VARCHAR(40) NOT NULL,
  name VARCHAR(120) NOT NULL,
  ratio FLOAT NOT NULL DEFAULT 0.5,
  is_control TINYINT(1) NOT NULL DEFAULT 0,
  note TEXT NULL,
  sort_order INT NOT NULL DEFAULT 0,
  UNIQUE KEY uq_variants_experiment_code (experiment_id, code),
  CONSTRAINT fk_variants_experiment FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS assignments (
  id INT AUTO_INCREMENT PRIMARY KEY,
  experiment_id INT NOT NULL,
  variant_id INT NOT NULL,
  channel VARCHAR(20) NOT NULL,
  metric_key VARCHAR(40) NOT NULL,
  stat_date DATE NOT NULL,
  samples INT NOT NULL DEFAULT 0,
  conversions INT NOT NULL DEFAULT 0,
  operator_email VARCHAR(160) NOT NULL DEFAULT 'system',
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  UNIQUE KEY uq_assignment_cell (experiment_id, variant_id, channel, metric_key, stat_date),
  CONSTRAINT fk_assignment_experiment FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE CASCADE,
  CONSTRAINT fk_assignment_variant FOREIGN KEY (variant_id) REFERENCES variants(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS metric_results (
  id INT AUTO_INCREMENT PRIMARY KEY,
  experiment_id INT NOT NULL,
  variant_id INT NULL,
  variant_code VARCHAR(40) NOT NULL,
  variant_name VARCHAR(120) NOT NULL DEFAULT '',
  metric_key VARCHAR(40) NOT NULL,
  metric_name VARCHAR(80) NOT NULL DEFAULT '',
  is_primary TINYINT(1) NOT NULL DEFAULT 0,
  method VARCHAR(16) NOT NULL DEFAULT 'bh',
  control_n INT, control_conversions INT, variant_n INT, variant_conversions INT,
  control_rate FLOAT, variant_rate FLOAT, diff FLOAT, z FLOAT,
  p_value FLOAT, q_value FLOAT, q_bh FLOAT, q_bonferroni FLOAT,
  ci_low FLOAT, ci_high FLOAT, lift FLOAT, lift_ci_low FLOAT, lift_ci_high FLOAT,
  significant TINYINT(1) NOT NULL DEFAULT 0,
  verdict VARCHAR(16) NOT NULL DEFAULT 'continue',
  required_n INT, power_at_current FLOAT, alpha FLOAT NOT NULL DEFAULT 0.05,
  payload JSON NULL,
  computed_at DATETIME NOT NULL,
  computed_by VARCHAR(160) NOT NULL DEFAULT 'system',
  UNIQUE KEY uq_metric_experiment_variant_metric (experiment_id, variant_code, metric_key),
  CONSTRAINT fk_metric_experiment FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS decisions (
  id INT AUTO_INCREMENT PRIMARY KEY,
  experiment_id INT NOT NULL,
  verdict VARCHAR(16) NOT NULL,
  rationale TEXT,
  decided_by VARCHAR(160) NOT NULL DEFAULT '',
  decided_at DATETIME NOT NULL,
  analysis_run_id INT NULL,
  analysis_run_no INT NULL,
  revision INT NOT NULL DEFAULT 1,
  status VARCHAR(16) NOT NULL DEFAULT 'active',
  stale_reason TEXT NULL,
  seed_key VARCHAR(120) NULL UNIQUE,
  updated_at DATETIME NOT NULL,
  UNIQUE KEY uq_decision_experiment (experiment_id),
  CONSTRAINT fk_decision_experiment FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS users (
  id INT AUTO_INCREMENT PRIMARY KEY,
  email VARCHAR(160) NOT NULL UNIQUE,
  password_hash VARCHAR(255) NOT NULL,
  display_name VARCHAR(80) NOT NULL,
  role VARCHAR(20) NOT NULL,
  org VARCHAR(120) NOT NULL,
  created_at DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS audit_logs (
  id INT AUTO_INCREMENT PRIMARY KEY,
  actor_email VARCHAR(160) NOT NULL,
  actor_role VARCHAR(20) NOT NULL,
  action VARCHAR(64) NOT NULL,
  entity VARCHAR(40) NOT NULL,
  entity_id VARCHAR(64) NULL,
  detail JSON NULL,
  layer VARCHAR(16) NOT NULL DEFAULT 'domain',
  ip VARCHAR(64) NULL,
  seed_key VARCHAR(120) NULL UNIQUE,
  created_at DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---- 每日回传与对账（实验平台分流数 / 指标库转化数，每天各一次）----

CREATE TABLE IF NOT EXISTS return_batches (
  id INT AUTO_INCREMENT PRIMARY KEY,
  experiment_id INT NOT NULL,
  source VARCHAR(16) NOT NULL,              -- assignment | metric
  stat_date DATE NOT NULL,
  batch_no VARCHAR(64) NOT NULL,
  revision INT NOT NULL DEFAULT 1,
  fingerprint CHAR(64) NOT NULL,            -- 规范化内容的 SHA-256，识别重试 / 改数
  rows_count INT NOT NULL DEFAULT 0,
  samples_total INT NOT NULL DEFAULT 0,
  conversions_total INT NOT NULL DEFAULT 0,
  status VARCHAR(12) NOT NULL DEFAULT 'active',  -- active | superseded
  note TEXT NULL,
  received_by VARCHAR(160) NOT NULL DEFAULT 'system',
  received_at DATETIME NOT NULL,
  superseded_at DATETIME NULL,
  UNIQUE KEY uq_return_batch_revision (experiment_id, source, stat_date, batch_no, revision),
  KEY ix_return_batch_active (experiment_id, source, stat_date, status),
  CONSTRAINT fk_return_batch_experiment FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS return_batch_rows (
  id INT AUTO_INCREMENT PRIMARY KEY,
  batch_id INT NOT NULL,
  experiment_id INT NOT NULL,
  source VARCHAR(16) NOT NULL,
  stat_date DATE NOT NULL,
  variant_code VARCHAR(40) NOT NULL,
  channel VARCHAR(20) NOT NULL,
  metric_key VARCHAR(40) NOT NULL,
  samples INT NOT NULL DEFAULT 0,
  conversions INT NOT NULL DEFAULT 0,
  UNIQUE KEY uq_return_batch_row (batch_id, variant_code, channel, metric_key),
  CONSTRAINT fk_return_row_batch FOREIGN KEY (batch_id) REFERENCES return_batches(id) ON DELETE CASCADE,
  CONSTRAINT fk_return_row_experiment FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS reconciliation_cells (
  id INT AUTO_INCREMENT PRIMARY KEY,
  experiment_id INT NOT NULL,
  stat_date DATE NOT NULL,
  variant_code VARCHAR(40) NOT NULL,
  channel VARCHAR(20) NOT NULL,
  metric_key VARCHAR(40) NOT NULL,
  assignment_batch_id INT NULL,
  metric_batch_id INT NULL,
  assignment_samples INT NULL, assignment_conversions INT NULL,
  metric_samples INT NULL, metric_conversions INT NULL,
  sample_delta INT NULL, conversion_delta INT NULL,
  status VARCHAR(12) NOT NULL,              -- complete | delayed | conflict | reconciled
  reason VARCHAR(40) NULL,                  -- missing_* / samples_differ / conversions_differ / both / invalid
  resolution VARCHAR(16) NULL,              -- assignment | metric
  explanation TEXT NULL,
  explained_by VARCHAR(160) NULL,
  explained_at DATETIME NULL,
  canonical_source VARCHAR(16) NULL,
  canonical_samples INT NULL, canonical_conversions INT NULL,
  usable TINYINT(1) NOT NULL DEFAULT 0,
  auto_explainable TINYINT(1) NOT NULL DEFAULT 0,
  updated_at DATETIME NOT NULL,
  UNIQUE KEY uq_reconciliation_cell (experiment_id, stat_date, variant_code, channel, metric_key),
  CONSTRAINT fk_recon_experiment FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS analysis_runs (
  id INT AUTO_INCREMENT PRIMARY KEY,
  experiment_id INT NOT NULL,
  run_no INT NOT NULL,
  status VARCHAR(14) NOT NULL DEFAULT 'ready',  -- ready | invalidated | recomputing
  snapshot_fingerprint CHAR(64) NOT NULL,
  date_fingerprints JSON NULL,
  date_from VARCHAR(10) NULL, date_to VARCHAR(10) NULL,
  dates_used JSON NULL,
  method VARCHAR(16) NOT NULL DEFAULT 'bh',
  alpha FLOAT NOT NULL DEFAULT 0.05,
  srm_payload JSON NULL,
  metric_payload JSON NULL,
  decision_basis JSON NULL,
  sources JSON NULL,                        -- 双方原始批次数 / revision / 指纹 / 日期
  invalidated_reason TEXT NULL,
  invalidated_at DATETIME NULL,
  superseded_by INT NULL,
  previous_run_id INT NULL,
  recomputed_dates JSON NULL,
  reused_dates JSON NULL,
  computed_by VARCHAR(160) NOT NULL DEFAULT 'system',
  computed_at DATETIME NOT NULL,
  KEY ix_analysis_experiment_status (experiment_id, status),
  CONSTRAINT fk_analysis_experiment FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS decision_revisions (
  id INT AUTO_INCREMENT PRIMARY KEY,
  decision_id INT NOT NULL,
  experiment_id INT NOT NULL,
  revision INT NOT NULL DEFAULT 1,
  verdict VARCHAR(16) NOT NULL,
  rationale TEXT,
  decided_by VARCHAR(160) NOT NULL DEFAULT '',
  decided_at DATETIME NOT NULL,
  analysis_run_id INT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'active',  -- active | superseded | invalidated
  invalidated_reason TEXT NULL,
  created_at DATETIME NOT NULL,
  CONSTRAINT fk_decision_revision FOREIGN KEY (decision_id) REFERENCES decisions(id) ON DELETE CASCADE,
  CONSTRAINT fk_decision_revision_experiment FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
