# START OF FILE api/audit_log.py

"""
API Request Audit Logging Module.

Provides functions to log API requests for security monitoring and analytics.
Logs are stored in PostgreSQL for analysis and retention.
"""

import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import asyncpg

logger = logging.getLogger(__name__)


async def log_api_request(
    db_pool: asyncpg.Pool,
    api_key_id: int,
    endpoint: str,
    method: str,
    client_ip: str,
    status_code: int,
    response_time_ms: float,
    user_agent: Optional[str] = None,
    request_body_size: Optional[int] = None,
    response_body_size: Optional[int] = None,
    error_message: Optional[str] = None,
    extra_data: Optional[Dict[str, Any]] = None
):
    """
    Log an API request to the database.

    Args:
        db_pool: AsyncPG connection pool
        api_key_id: ID of the API key used
        endpoint: API endpoint path
        method: HTTP method (GET, POST, etc.)
        client_ip: Client IP address
        status_code: HTTP response status code
        response_time_ms: Response time in milliseconds
        user_agent: Client User-Agent header
        request_body_size: Size of request body in bytes
        response_body_size: Size of response body in bytes
        error_message: Error message if request failed
        extra_data: Additional data to log (JSON)
    """
    if db_pool is None:
        logger.debug(f"[AUDIT] No DB pool, skipping log: {method} {endpoint}")
        return

    query = """
        INSERT INTO api_request_logs 
        (api_key_id, endpoint, method, client_ip, status_code, response_time_ms,
         user_agent, request_body_size, response_body_size, error_message, extra_data, created_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
    """

    try:
        await db_pool.execute(
            query,
            api_key_id,
            endpoint[:255] if endpoint else '',  # Truncate to fit column
            method[:10] if method else 'UNKNOWN',
            client_ip[:45] if client_ip else 'unknown',  # IPv6 max length
            status_code,
            response_time_ms,
            user_agent[:500] if user_agent else None,  # Truncate user agent
            request_body_size,
            response_body_size,
            error_message[:1000] if error_message else None,
            extra_data if extra_data else None,
            datetime.now(timezone.utc)
        )
        logger.debug(f"[AUDIT] Logged: {method} {endpoint} -> {status_code} ({response_time_ms:.1f}ms)")
    except asyncpg.UndefinedTableError:
        # Table doesn't exist yet - log warning once and continue
        logger.warning("[AUDIT] api_request_logs table doesn't exist. Run migrations to enable audit logging.")
    except Exception as e:
        # Don't fail the request if logging fails
        logger.error(f"[AUDIT] Failed to log request: {e}")


async def cleanup_old_logs(db_pool: asyncpg.Pool, days_to_keep: int = 30) -> int:
    """
    Delete old audit logs beyond retention period.

    Args:
        db_pool: AsyncPG connection pool
        days_to_keep: Number of days to retain logs

    Returns:
        Number of deleted records
    """
    if db_pool is None:
        return 0

    query = """
        DELETE FROM api_request_logs
        WHERE created_at < NOW() - INTERVAL '%s days'
    """

    try:
        result = await db_pool.execute(query, days_to_keep)
        # Parse "DELETE X" result
        deleted = int(result.split()[-1]) if result else 0
        logger.info(f"[AUDIT] Cleaned up {deleted} old log entries (older than {days_to_keep} days)")
        return deleted
    except asyncpg.UndefinedTableError:
        return 0
    except Exception as e:
        logger.error(f"[AUDIT] Failed to cleanup logs: {e}")
        return 0


async def get_request_stats(
    db_pool: asyncpg.Pool,
    api_key_id: Optional[int] = None,
    hours: int = 24
) -> Dict[str, Any]:
    """
    Get request statistics for monitoring.

    Args:
        db_pool: AsyncPG connection pool
        api_key_id: Filter by specific API key (optional)
        hours: Time window in hours

    Returns:
        Dictionary with statistics
    """
    if db_pool is None:
        return {}

    try:
        base_query = """
            SELECT 
                COUNT(*) as total_requests,
                COUNT(*) FILTER (WHERE status_code >= 400) as error_requests,
                AVG(response_time_ms) as avg_response_time,
                MAX(response_time_ms) as max_response_time,
                COUNT(DISTINCT client_ip) as unique_ips
            FROM api_request_logs
            WHERE created_at > NOW() - INTERVAL '%s hours'
        """

        if api_key_id:
            query = base_query + " AND api_key_id = $2"
            result = await db_pool.fetchrow(query, hours, api_key_id)
        else:
            result = await db_pool.fetchrow(base_query, hours)

        if result:
            return {
                'total_requests': result['total_requests'] or 0,
                'error_requests': result['error_requests'] or 0,
                'error_rate': (result['error_requests'] or 0) / max(result['total_requests'] or 1, 1),
                'avg_response_time_ms': float(result['avg_response_time'] or 0),
                'max_response_time_ms': float(result['max_response_time'] or 0),
                'unique_ips': result['unique_ips'] or 0,
                'time_window_hours': hours,
            }
        return {}
    except asyncpg.UndefinedTableError:
        return {}
    except Exception as e:
        logger.error(f"[AUDIT] Failed to get stats: {e}")
        return {}


# SQL для создания таблицы (выполнить вручную или через миграции)
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS api_request_logs (
    id SERIAL PRIMARY KEY,
    api_key_id INTEGER REFERENCES api_keys(id) ON DELETE SET NULL,
    endpoint VARCHAR(255) NOT NULL,
    method VARCHAR(10) NOT NULL,
    client_ip VARCHAR(45) NOT NULL,
    status_code INTEGER NOT NULL,
    response_time_ms FLOAT,
    user_agent VARCHAR(500),
    request_body_size INTEGER,
    response_body_size INTEGER,
    error_message VARCHAR(1000),
    extra_data JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_api_logs_key ON api_request_logs(api_key_id);
CREATE INDEX IF NOT EXISTS idx_api_logs_created ON api_request_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_api_logs_endpoint ON api_request_logs(endpoint);
CREATE INDEX IF NOT EXISTS idx_api_logs_status ON api_request_logs(status_code);
CREATE INDEX IF NOT EXISTS idx_api_logs_ip ON api_request_logs(client_ip);

-- Partitioning by month (optional, for large deployments)
-- CREATE TABLE api_request_logs_y2026m01 PARTITION OF api_request_logs
--     FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');
"""
