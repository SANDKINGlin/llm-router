-- Database Migration 001 Rollback: Rollback Enhanced Admin Provider Management
-- This script rolls back all changes made by 001_initial_schema.sql

-- Drop indexes first
DROP INDEX IF EXISTS idx_audit_logs_resource;
DROP INDEX IF EXISTS idx_audit_logs_user_time;
DROP INDEX IF EXISTS idx_cost_metrics_provider_time;
DROP INDEX IF EXISTS idx_config_history_version;
DROP INDEX IF EXISTS idx_config_history_provider;
DROP INDEX IF EXISTS idx_providers_active;
DROP INDEX IF EXISTS idx_providers_tier;
DROP INDEX IF EXISTS idx_providers_name;

-- Drop tables in reverse order (due to foreign key dependencies)
DROP TABLE IF EXISTS audit_logs;
DROP TABLE IF EXISTS cost_metrics;
DROP TABLE IF EXISTS config_history;
DROP TABLE IF EXISTS user_roles;
DROP TABLE IF EXISTS providers;

-- Verify rollback
SELECT 'Rollback complete. All enhanced admin provider management tables have been removed.' AS status;
