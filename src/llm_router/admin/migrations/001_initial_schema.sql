-- Database Migration 001: Initial Schema for Enhanced Admin Provider Management
-- This migration creates all required tables for dynamic provider management

-- Providers table for dynamic provider configuration
CREATE TABLE IF NOT EXISTS providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    tier TEXT NOT NULL CHECK(tier IN ('strong', 'medium', 'fast')),
    base_url TEXT,
    quota INTEGER DEFAULT 1000000,
    cooldown_s INTEGER DEFAULT 30,
    cost_multiplier REAL DEFAULT 1.0,
    default_model TEXT,
    config_json TEXT,  -- Additional flexible configuration
    is_active BOOLEAN DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- User roles table for authentication and authorization
CREATE TABLE IF NOT EXISTS user_roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('view', 'operate', 'admin')),
    permissions TEXT,  -- JSON array of specific permissions
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Configuration history for rollback capability
CREATE TABLE IF NOT EXISTS config_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id INTEGER NOT NULL,
    config_version INTEGER NOT NULL,
    config_data TEXT NOT NULL,  -- JSON string of configuration
    changed_by TEXT,
    changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    rollback_data TEXT,  -- Previous configuration for rollback
    FOREIGN KEY (provider_id) REFERENCES providers(id) ON DELETE CASCADE
);

-- Cost metrics tracking
CREATE TABLE IF NOT EXISTS cost_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id INTEGER NOT NULL,
    model_name TEXT NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost REAL DEFAULT 0.0,
    FOREIGN KEY (provider_id) REFERENCES providers(id) ON DELETE CASCADE
);

-- Audit logs for security events
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    action_type TEXT NOT NULL,  -- LOGIN, LOGOUT, KEY_REVEAL, CONFIG_CHANGE, etc.
    resource_type TEXT,  -- provider, key, config, user
    resource_id TEXT,
    details TEXT,  -- JSON string of additional details
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES user_roles(id) ON DELETE SET NULL
);

-- Create indexes for performance optimization
CREATE INDEX IF NOT EXISTS idx_providers_name ON providers(name);
CREATE INDEX IF NOT EXISTS idx_providers_tier ON providers(tier);
CREATE INDEX IF NOT EXISTS idx_providers_active ON providers(is_active);
CREATE INDEX IF NOT EXISTS idx_config_history_provider ON config_history(provider_id);
CREATE INDEX IF NOT EXISTS idx_config_history_version ON config_history(config_version);
CREATE INDEX IF NOT EXISTS idx_cost_metrics_provider_time ON cost_metrics(provider_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_logs_user_time ON audit_logs(user_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_logs_resource ON audit_logs(resource_type, resource_id);

-- Insert default admin user (password: admin123)
INSERT INTO user_roles (username, password_hash, role, permissions) VALUES
('admin', 'pbkdf2:sha256:500000$TMAFxv7qQxq0j5J8Y6hXw$ZGl5qR5XL8YJ8Y6hXw5J8Y6hXw5J8Y6hXw5J8Y6hXw', 'admin', '["all"]');
