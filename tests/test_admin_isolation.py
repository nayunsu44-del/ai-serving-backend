from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest

from app.db.models import APIKey, AuditLog, Organization


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _create_admin_isolation_data(sessionmaker) -> dict[str, str]:
    async with sessionmaker() as session:
        org_a = Organization(name="Isolation Org A")
        org_b = Organization(name="Isolation Org B")
        session.add_all([org_a, org_b])
        await session.flush()

        key_a = APIKey(
            org_id=org_a.id,
            prefix="org-a-admin"[:12],
            key_hash=_hash_token("org-a-admin"),
            name="Org A Admin",
            scopes="admin",
        )
        key_b = APIKey(
            org_id=org_b.id,
            prefix="org-b-admin"[:12],
            key_hash=_hash_token("org-b-admin"),
            name="Org B Admin",
            scopes="admin",
        )
        session.add_all([key_a, key_b])
        await session.flush()

        session.add_all(
            [
                AuditLog(
                    request_id="audit-org-a",
                    org_id=org_a.id,
                    api_key_id=key_a.id,
                    provider="fake",
                    model="gpt-a",
                    status_code=200,
                    prompt_tokens=3,
                    completion_tokens=4,
                    total_tokens=7,
                    cost_usd=Decimal("0.000007"),
                    latency_ms=11,
                    stream=False,
                ),
                AuditLog(
                    request_id="audit-org-b",
                    org_id=org_b.id,
                    api_key_id=key_b.id,
                    provider="fake",
                    model="gpt-b",
                    status_code=500,
                    error_type="server_error",
                    prompt_tokens=5,
                    completion_tokens=6,
                    total_tokens=11,
                    cost_usd=Decimal("0.000011"),
                    latency_ms=22,
                    stream=False,
                ),
            ]
        )
        await session.commit()

        return {
            "org_a_id": org_a.id,
            "org_b_id": org_b.id,
            "key_a_id": key_a.id,
            "key_b_id": key_b.id,
            "org_a_token": "org-a-admin",
            "org_b_token": "org-b-admin",
        }


@pytest.mark.asyncio
async def test_org_admin_is_scoped_to_own_org(
    client,
    db_sessionmaker,
) -> None:
    data = await _create_admin_isolation_data(db_sessionmaker)
    org_a_headers = {"Authorization": f"Bearer {data['org_a_token']}"}

    keys_response = await client.get(
        f"/admin/keys?org_id={data['org_b_id']}",
        headers=org_a_headers,
    )
    assert keys_response.status_code == 200
    listed = keys_response.json()["items"]
    assert {item["org_id"] for item in listed} == {data["org_a_id"]}
    assert data["key_b_id"] not in {item["id"] for item in listed}

    org_response = await client.post(
        "/admin/orgs",
        headers=org_a_headers,
        json={"name": "Should Not Create"},
    )
    assert org_response.status_code == 403

    revoke_response = await client.post(
        f"/admin/keys/{data['key_b_id']}/revoke",
        headers=org_a_headers,
    )
    assert revoke_response.status_code == 404

    create_response = await client.post(
        "/admin/keys",
        headers=org_a_headers,
        json={"name": "Wrong Org", "scopes": ["chat"], "org_id": data["org_b_id"]},
    )
    assert create_response.status_code == 403

    audit_response = await client.get("/admin/audit", headers=org_a_headers)
    assert audit_response.status_code == 200
    audit_items = audit_response.json()["items"]
    assert {item["org_id"] for item in audit_items} == {data["org_a_id"]}
    assert {item["request_id"] for item in audit_items} == {"audit-org-a"}

    filtered_audit_response = await client.get(
        f"/admin/audit?api_key_id={data['key_b_id']}",
        headers=org_a_headers,
    )
    assert filtered_audit_response.status_code == 404

    usage_response = await client.get(
        "/admin/usage?group_by=org",
        headers=org_a_headers,
    )
    assert usage_response.status_code == 200
    groups = usage_response.json()["groups"]
    assert len(groups) == 1
    assert groups[0]["group_key"] == data["org_a_id"]
    assert groups[0]["request_count"] == 1
    assert groups[0]["total_tokens"] == 7


@pytest.mark.asyncio
async def test_org_admin_cannot_grant_super_admin(
    client,
    db_sessionmaker,
) -> None:
    data = await _create_admin_isolation_data(db_sessionmaker)
    org_a_headers = {"Authorization": f"Bearer {data['org_a_token']}"}

    create_response = await client.post(
        "/admin/keys",
        headers=org_a_headers,
        json={
            "name": "Scoped Child Admin",
            "scopes": ["admin", "super_admin"],
            "org_id": data["org_a_id"],
        },
    )

    assert create_response.status_code == 200
    body = create_response.json()
    assert body["org_id"] == data["org_a_id"]
    assert body["scopes"] == ["admin"]


@pytest.mark.asyncio
async def test_env_super_admin_can_operate_across_orgs(
    client,
    db_sessionmaker,
    auth_headers,
) -> None:
    data = await _create_admin_isolation_data(db_sessionmaker)

    keys_response = await client.get("/admin/keys", headers=auth_headers)
    assert keys_response.status_code == 200
    listed = keys_response.json()["items"]
    assert {item["org_id"] for item in listed} == {data["org_a_id"], data["org_b_id"]}

    org_response = await client.post(
        "/admin/orgs",
        headers=auth_headers,
        json={"name": "Super Created Org"},
    )
    assert org_response.status_code == 200

    create_response = await client.post(
        "/admin/keys",
        headers=auth_headers,
        json={
            "name": "Super DB Admin",
            "scopes": ["admin", "super_admin"],
            "org_id": data["org_b_id"],
        },
    )
    assert create_response.status_code == 200
    assert create_response.json()["scopes"] == ["admin", "super_admin"]

    revoke_response = await client.post(
        f"/admin/keys/{data['key_b_id']}/revoke",
        headers=auth_headers,
    )
    assert revoke_response.status_code == 200
    assert revoke_response.json()["id"] == data["key_b_id"]
