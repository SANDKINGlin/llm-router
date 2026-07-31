"""Provider management module for dynamic provider creation and configuration."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field
from fastapi import HTTPException
import sqlite3

logger = logging.getLogger(__name__)

# Pydantic models for provider operations
class ProviderCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    tier: str = Field(..., pattern="^(strong|medium|fast)$")
    base_url: Optional[str] = None
    quota: int = Field(default=1000000, ge=1)
    cooldown_s: int = Field(default=30, ge=0)
    cost_multiplier: float = Field(default=1.0, ge=0.0)
    default_model: Optional[str] = None
    config_json: Optional[str] = None

class ProviderUpdate(BaseModel):
    tier: Optional[str] = Field(None, pattern="^(strong|medium|fast)$")
    base_url: Optional[str] = None
    quota: Optional[int] = Field(None, ge=1)
    cooldown_s: Optional[int] = Field(None, ge=0)
    cost_multiplier: Optional[float] = Field(None, ge=0.0)
    default_model: Optional[str] = None
    config_json: Optional[str] = None

class ProviderResponse(BaseModel):
    id: int
    name: str
    tier: str
    base_url: Optional[str]
    quota: int
    cooldown_s: int
    cost_multiplier: float
    default_model: Optional[str]
    is_active: bool
    created_at: datetime
    updated_at: datetime

class ProviderListResponse(BaseModel):
    providers: list[ProviderResponse]
    total: int

# Database operations
class ProviderManager:
    def __init__(self, db_path: str = "/home/lin/projects/llm-router/data/keys.db"):
        self.db_path = db_path

    def get_connection(self) -> sqlite3.Connection:
        """Get database connection with row factory."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def create_provider(self, provider_data: ProviderCreate) -> int:
        """Create a new provider and return the provider ID."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    INSERT INTO providers (name, tier, base_url, quota, cooldown_s, cost_multiplier, default_model, config_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    provider_data.name,
                    provider_data.tier,
                    provider_data.base_url,
                    provider_data.quota,
                    provider_data.cooldown_s,
                    provider_data.cost_multiplier,
                    provider_data.default_model,
                    provider_data.config_json
                ))
                provider_id = cursor.lastrowid
                conn.commit()

                # Log creation to audit logs
                cursor.execute("""
                    INSERT INTO audit_logs (action_type, resource_type, resource_id, details)
                    VALUES (?, ?, ?, ?)
                """, ("CREATE_PROVIDER", "provider", str(provider_id), f"Created provider: {provider_data.name}"))
                conn.commit()

                logger.info(f"Created provider {provider_data.name} with ID {provider_id}")
                return provider_id

            except sqlite3.IntegrityError as e:
                if "UNIQUE constraint failed: providers.name" in str(e):
                    raise HTTPException(status_code=409, detail=f"Provider '{provider_data.name}' already exists")
                raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    def get_provider(self, provider_name: str) -> Optional[dict]:
        """Get provider by name."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM providers WHERE name = ? AND is_active = 1
            """, (provider_name,))
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None

    def list_providers(self) -> list[dict]:
        """List all active providers."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM providers WHERE is_active = 1 ORDER BY name
            """)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def update_provider(self, provider_name: str, update_data: ProviderUpdate) -> bool:
        """Update provider configuration with validation and history tracking."""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Get current provider data for history
            cursor.execute("SELECT * FROM providers WHERE name = ?", (provider_name,))
            current_provider = cursor.fetchone()
            if not current_provider:
                raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' not found")

            provider_id = current_provider["id"]
            current_config = dict(current_provider)

            # Validate configuration changes
            self._validate_config_update(update_data)

            # Get current config version
            cursor.execute("SELECT MAX(config_version) FROM config_history WHERE provider_id = ?", (provider_id,))
            max_version = cursor.fetchone()[0] or 0
            new_version = max_version + 1

            # Save current configuration to history before updating
            import json
            cursor.execute("""
                INSERT INTO config_history (provider_id, config_version, config_data, changed_by, rollback_data)
                VALUES (?, ?, ?, ?, ?)
            """, (
                provider_id,
                new_version,
                json.dumps(current_config, default=str),
                "system",
                json.dumps(current_config, default=str)  # Store current state as rollback data
            ))

            # Build update query dynamically
            update_fields = []
            params = []

            if update_data.tier is not None:
                update_fields.append("tier = ?")
                params.append(update_data.tier)
            if update_data.base_url is not None:
                update_fields.append("base_url = ?")
                params.append(update_data.base_url)
            if update_data.quota is not None:
                update_fields.append("quota = ?")
                params.append(update_data.quota)
            if update_data.cooldown_s is not None:
                update_fields.append("cooldown_s = ?")
                params.append(update_data.cooldown_s)
            if update_data.cost_multiplier is not None:
                update_fields.append("cost_multiplier = ?")
                params.append(update_data.cost_multiplier)
            if update_data.default_model is not None:
                update_fields.append("default_model = ?")
                params.append(update_data.default_model)
            if update_data.config_json is not None:
                update_fields.append("config_json = ?")
                params.append(update_data.config_json)

            if not update_fields:
                return False

            params.extend([datetime.now().isoformat(), provider_name])

            update_fields.append("updated_at = ?")

            query = f"UPDATE providers SET {', '.join(update_fields)} WHERE name = ?"
            cursor.execute(query, params)

            # Log update to audit logs
            cursor.execute("""
                INSERT INTO audit_logs (action_type, resource_type, resource_id, details)
                VALUES (?, ?, ?, ?)
            """, ("UPDATE_PROVIDER", "provider", str(provider_id), f"Updated provider: {provider_name}"))

            conn.commit()

            return cursor.rowcount > 0

    def _validate_config_update(self, update_data: ProviderUpdate):
        """Validate provider configuration updates."""
        # Validate tier
        if update_data.tier is not None and update_data.tier not in ['strong', 'medium', 'fast']:
            raise HTTPException(status_code=400, detail=f"Invalid tier: {update_data.tier}. Must be strong, medium, or fast")

        # Validate quota
        if update_data.quota is not None and update_data.quota < 1:
            raise HTTPException(status_code=400, detail=f"Invalid quota: {update_data.quota}. Must be >= 1")

        # Validate cooldown
        if update_data.cooldown_s is not None and update_data.cooldown_s < 0:
            raise HTTPException(status_code=400, detail=f"Invalid cooldown_s: {update_data.cooldown_s}. Must be >= 0")

        # Validate cost_multiplier
        if update_data.cost_multiplier is not None and update_data.cost_multiplier < 0:
            raise HTTPException(status_code=400, detail=f"Invalid cost_multiplier: {update_data.cost_multiplier}. Must be >= 0")

        # Validate base_url format if provided
        if update_data.base_url is not None:
            if not update_data.base_url.startswith(('http://', 'https://')):
                raise HTTPException(status_code=400, detail=f"Invalid base_url: {update_data.base_url}. Must start with http:// or https://")

    def rollback_provider_config(self, provider_name: str, config_version: int) -> bool:
        """Rollback provider configuration to a specific version."""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Get provider ID
            cursor.execute("SELECT id FROM providers WHERE name = ?", (provider_name,))
            provider_row = cursor.fetchone()
            if not provider_row:
                raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' not found")

            provider_id = provider_row["id"]

            # Get the target configuration from history
            cursor.execute("""
                SELECT config_data, rollback_data
                FROM config_history
                WHERE provider_id = ? AND config_version = ?
                ORDER BY config_version DESC
                LIMIT 1
            """, (provider_id, config_version))

            history_row = cursor.fetchone()
            if not history_row:
                raise HTTPException(status_code=404, detail=f"Config version {config_version} not found for provider '{provider_name}'")

            import json
            rollback_config = json.loads(history_row["rollback_data"])

            # Restore the configuration
            update_fields = []
            params = []

            # Reconstruct the provider from rollback data
            if "tier" in rollback_config:
                update_fields.append("tier = ?")
                params.append(rollback_config["tier"])
            if "base_url" in rollback_config and rollback_config["base_url"]:
                update_fields.append("base_url = ?")
                params.append(rollback_config["base_url"])
            if "quota" in rollback_config:
                update_fields.append("quota = ?")
                params.append(rollback_config["quota"])
            if "cooldown_s" in rollback_config:
                update_fields.append("cooldown_s = ?")
                params.append(rollback_config["cooldown_s"])
            if "cost_multiplier" in rollback_config:
                update_fields.append("cost_multiplier = ?")
                params.append(rollback_config["cost_multiplier"])
            if "default_model" in rollback_config and rollback_config["default_model"]:
                update_fields.append("default_model = ?")
                params.append(rollback_config["default_model"])
            if "config_json" in rollback_config and rollback_config["config_json"]:
                update_fields.append("config_json = ?")
                params.append(rollback_config["config_json"])

            if update_fields:
                params.extend([datetime.now().isoformat(), provider_name])
                update_fields.append("updated_at = ?")

                query = f"UPDATE providers SET {', '.join(update_fields)} WHERE name = ?"
                cursor.execute(query, params)

                # Log rollback to audit logs
                cursor.execute("""
                    INSERT INTO audit_logs (action_type, resource_type, resource_id, details)
                    VALUES (?, ?, ?, ?)
                """, ("ROLLBACK_PROVIDER", "provider", str(provider_id), f"Rolled back provider {provider_name} to version {config_version}"))

                conn.commit()
                return True

            return False

    def get_provider_config_history(self, provider_name: str) -> list[dict]:
        """Get configuration history for a provider."""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Get provider ID
            cursor.execute("SELECT id FROM providers WHERE name = ?", (provider_name,))
            provider_row = cursor.fetchone()
            if not provider_row:
                return []

            provider_id = provider_row["id"]

            # Get configuration history
            cursor.execute("""
                SELECT config_version, config_data, changed_by, changed_at
                FROM config_history
                WHERE provider_id = ?
                ORDER BY config_version DESC
                LIMIT 10
            """, (provider_id,))

            history = []
            for row in cursor.fetchall():
                history.append({
                    "config_version": row["config_version"],
                    "config_data": row["config_data"],
                    "changed_by": row["changed_by"],
                    "changed_at": row["changed_at"]
                })

            return history

    def delete_provider(self, provider_name: str) -> bool:
        """Soft delete provider by setting is_active = 0."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE providers SET is_active = 0, updated_at = ?
                WHERE name = ?
            """, (datetime.now().isoformat(), provider_name))
            conn.commit()
            return cursor.rowcount > 0

# Global provider manager instance
provider_manager = ProviderManager()