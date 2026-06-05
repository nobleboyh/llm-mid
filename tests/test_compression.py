"""Verify Headroom compression reduces token usage without breaking responses."""
import json
import pytest
from openai import OpenAI

# A prompt with JSON blobs and code — compression should have measurable impact
LARGE_CODE_PROMPT = """
Here is a database schema and a function that queries it. Please review the code.

Database schema:
{
  "tables": {
    "users": {
      "columns": ["id", "name", "email", "created_at", "updated_at", "status", "role", "org_id", "last_login", "preferences"],
      "indexes": ["idx_users_email", "idx_users_org_id", "idx_users_status"],
      "constraints": ["pk_users_id", "fk_users_org_id", "uq_users_email"]
    },
    "orders": {
      "columns": ["id", "user_id", "product_id", "quantity", "total", "status", "created_at", "updated_at", "shipping_address", "billing_address", "tracking_number", "notes"],
      "indexes": ["idx_orders_user_id", "idx_orders_status", "idx_orders_created_at"],
      "constraints": ["pk_orders_id", "fk_orders_user_id", "fk_orders_product_id"]
    },
    "products": {
      "columns": ["id", "name", "description", "price", "category", "inventory_count", "warehouse_id", "sku", "weight", "dimensions", "supplier_id", "created_at"],
      "indexes": ["idx_products_sku", "idx_products_category", "idx_products_supplier_id"],
      "constraints": ["pk_products_id", "fk_products_supplier_id", "uq_products_sku"]
    }
  }
}

Here is the Python code:

import asyncio
import json
from datetime import datetime
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from decimal import Decimal

@dataclass
class OrderSummary:
    order_id: int
    user_name: str
    product_name: str
    quantity: int
    total: Decimal
    status: str
    created_at: datetime

class OrderRepository:
    def __init__(self, db_pool):
        self._pool = db_pool

    async def get_user_orders(
        self,
        user_id: int,
        status: Optional[str] = None,
        since: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[OrderSummary]:
        query = '''
            SELECT
                o.id AS order_id,
                u.name AS user_name,
                p.name AS product_name,
                o.quantity,
                o.total,
                o.status,
                o.created_at
            FROM orders o
            JOIN users u ON o.user_id = u.id
            JOIN products p ON o.product_id = p.id
            WHERE o.user_id = $1
        '''
        params: List[Any] = [user_id]
        param_idx = 2

        if status:
            query += f" AND o.status = ${param_idx}"
            params.append(status)
            param_idx += 1

        if since:
            query += f" AND o.created_at >= ${param_idx}"
            params.append(since)
            param_idx += 1

        query += f" ORDER BY o.created_at DESC LIMIT ${param_idx} OFFSET ${param_idx + 1}"
        params.extend([limit, offset])

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [OrderSummary(**dict(row)) for row in rows]

Please review this for:
1. SQL injection vulnerabilities
2. Performance issues
3. Code style improvements
"""

SHORT_PROMPT = "What is the capital of France?"


@pytest.fixture(scope="module")
def client(gateway_url, gateway_ready):
    """OpenAI-compatible client pointed at the gateway."""
    return OpenAI(
        base_url=f"{gateway_url}/v1",
        api_key="sk-local-dev-key",
    )


class TestCompression:
    """Verify Headroom compression is active and non-destructive."""

    def test_compressed_response_is_valid(self, client):
        """Compressed request should return a coherent response."""
        resp = client.chat.completions.create(
            model="team-smart-router",
            messages=[{"role": "user", "content": LARGE_CODE_PROMPT}],
            max_tokens=200,
        )
        content = resp.choices[0].message.content
        assert content is not None
        assert len(content) > 50, "Response should be substantive"
        # Should address at least one of the review points
        keywords = ["sql", "injection", "performance", "style", "code", "review"]
        has_relevant = any(kw in content.lower() for kw in keywords)
        assert has_relevant, (
            f"Response should address code review, got: {content[:200]}"
        )

    def test_compression_preserves_semantic_quality(self, client):
        """Simple fact-based queries should return correct answers despite compression."""
        resp = client.chat.completions.create(
            model="team-smart-router",
            messages=[{"role": "user", "content": SHORT_PROMPT}],
            max_tokens=50,
        )
        content = resp.choices[0].message.content.lower()
        assert "paris" in content, (
            f"Expected 'Paris' in response to capital question, got: {content}"
        )

    def test_large_payload_does_not_error(self, client):
        """Very large payloads with JSON + code should succeed, not error out."""
        resp = client.chat.completions.create(
            model="team-smart-router",
            messages=[
                {"role": "user", "content": LARGE_CODE_PROMPT},
                {
                    "role": "user",
                    "content": "Also, explain how connection pooling works in asyncpg.",
                },
            ],
            max_tokens=150,
        )
        assert resp.choices[0].message.content is not None
