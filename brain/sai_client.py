"""Minimal async client for the Sai (Simular) API: start a task, follow it, answer approvals, stop it."""
import logging
import os
from urllib.parse import quote

import httpx

log = logging.getLogger("sai")


class SaiError(RuntimeError):
    pass


class SaiClient:
    def __init__(self, api_key: str, base_url: str, machine_id: str | None):
        self.machine_id = machine_id
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(20.0, read=75.0),
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        response = await self._http.request(method, path, **kwargs)
        try:
            body = response.json()
        except ValueError:
            body = {"error": response.text[:200]}
        if response.status_code >= 400:
            raise SaiError(f"{response.status_code}: {body.get('error') or body}")
        return body

    async def check(self) -> dict:
        return await self._request("GET", "/v1/agents/auth")

    async def machines(self) -> list[dict]:
        return (await self._request("GET", "/v1/agents/machines")).get("machines", [])

    async def upload(self, path: str) -> dict:
        """Upload one file; returns the attachment object to pass unchanged in send(attachments=[…])."""
        name = os.path.basename(path)
        with open(path, "rb") as f:
            data = f.read()
        return await self._request("POST", "/v1/agents/upload", content=data,
                                   headers={"x-filename": quote(name)})

    async def send(self, message: str, attachments: list[dict] | None = None) -> dict:
        """Start a task without waiting. Returns {sessionId, machineId, queued, turnId}."""
        body = {"message": message, "wait": False}
        if self.machine_id:
            body["machineId"] = self.machine_id
        if attachments:
            body["attachments"] = attachments
        result = await self._request("POST", "/v1/agents/message", json=body)
        if "sessionId" not in result:
            # Handled without starting a task (e.g. a plain acknowledgement): the reply is in "error".
            raise SaiError(result.get("error") or "the message did not start a task")
        return result

    async def events(self, session_id: str, cursor: str | None, wait_s: int = 30) -> dict:
        params = {"sessionId": session_id, "wait": wait_s}
        if cursor:
            params["since"] = cursor
        return await self._request("GET", "/v1/agents/events", params=params)

    async def approve(self, approval_id: str, response: str, selections: list[list[str]] | None = None) -> dict:
        body = {"approvalId": approval_id, "response": response}
        if selections is not None:
            body["selections"] = selections
        return await self._request("POST", "/v1/agents/approve", json=body)

    async def abort(self, machine_id: str) -> dict:
        return await self._request("POST", "/v1/agents/abort", json={"machineId": machine_id})
