"""Cliente MCP minimo, stateless, falando Streamable HTTP com o servidor de salas.

Escrito sobre HTTP puro de proposito: o agente precisa enxergar o
`input_required` cru para pausar a Task A2A. Um cliente com callback de
elicitation responderia sozinho e a pausa nunca chegaria ao cliente A2A.

Cada request carrega, no corpo e nos headers espelhados, tudo que o servidor
precisa: nenhuma versao ou capability e herdada de request anterior.
"""

from __future__ import annotations

import itertools
import secrets
from typing import Any

import httpx

PROTOCOLO = "2026-07-28"
CAPABILITIES = {"elicitation": {"form": {}}}
CLIENT_INFO = {"name": "agente-central-de-salas", "version": "1.0.0"}


class ErroMCP(Exception):
    """Erro de protocolo (campo `error` do JSON-RPC)."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.data = data


def novo_traceparent(trace_id: str, flags: str = "01") -> str:
    """Mesmo trace-id da Task, span-id novo por request MCP."""
    return f"00-{trace_id}-{secrets.token_hex(8)}-{flags}"


class ClienteMCP:
    def __init__(self, url: str) -> None:
        self.url = url
        self._http = httpx.AsyncClient(timeout=30.0)
        # Ids monotonicos: o retry de MRTR sempre sai com id diferente do request inicial.
        self._ids = itertools.count(1)

    async def fechar(self) -> None:
        await self._http.aclose()

    async def request(self, metodo: str, params: dict[str, Any], *, trace_id: str | None, nome: str | None = None) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "io.modelcontextprotocol/protocolVersion": PROTOCOLO,
            "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
            "io.modelcontextprotocol/clientCapabilities": CAPABILITIES,
        }
        if trace_id:
            meta["traceparent"] = novo_traceparent(trace_id)
        corpo = {"jsonrpc": "2.0", "id": next(self._ids), "method": metodo, "params": {**params, "_meta": meta}}
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOLO,
            "Mcp-Method": metodo,
        }
        if nome is not None:
            headers["Mcp-Name"] = nome
        resp = await self._http.post(self.url, json=corpo, headers=headers)
        dados = resp.json()
        if "error" in dados:
            e = dados["error"]
            raise ErroMCP(e.get("code", 0), e.get("message", ""), e.get("data"))
        return dados["result"]

    async def listar_tools(self, trace_id: str | None) -> list[dict[str, Any]]:
        return (await self.request("tools/list", {}, trace_id=trace_id))["tools"]

    async def ler_resource(self, uri: str, trace_id: str | None) -> str:
        resultado = await self.request("resources/read", {"uri": uri}, trace_id=trace_id, nome=uri)
        return "".join(c.get("text", "") for c in resultado.get("contents", []))

    async def chamar_tool(
        self,
        nome: str,
        argumentos: dict[str, Any],
        trace_id: str | None,
        *,
        input_responses: dict[str, Any] | None = None,
        request_state: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"name": nome, "arguments": argumentos}
        if input_responses is not None:
            params["inputResponses"] = input_responses
        if request_state is not None:
            params["requestState"] = request_state  # ecoado sem modificacao
        return await self.request("tools/call", params, trace_id=trace_id, nome=nome)
