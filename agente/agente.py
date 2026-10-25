"""Agente "Central de Salas": servidor A2A v1.0 (JSON-RPC) por fora, host MCP por dentro.

Sem LLM: o pedido chega em formato fixo e o agente so traduz protocolo.
Conflito, politica e alternativas sao decididos pelo servidor MCP.

A ponte esta em `_traduzir_resultado` (input_required -> TASK_STATE_INPUT_REQUIRED,
requestState guardado em PAUSAS[task_id]) e em `_continuar` (requestState
devolvido ao servidor MCP num tools/call com id novo).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from cliente_mcp import ClienteMCP, ErroMCP

HOST = os.environ.get("AGENTE_HOST", "127.0.0.1")
PORTA = int(os.environ.get("AGENTE_PORT", "7300"))
MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")
URL_PUBLICA = os.environ.get("AGENTE_URL_PUBLICA", f"http://{HOST}:{PORTA}/a2a")

TOOL_RESERVA = "reservar_sala"
RESOURCE_POLITICA = "politica://uso"

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
CANCELED = "TASK_STATE_CANCELED"
FAILED = "TASK_STATE_FAILED"
TERMINAIS = {COMPLETED, CANCELED, FAILED}

# Codigos JSON-RPC (A2A v1.0)
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
TASK_NOT_FOUND = -32001
UNSUPPORTED_OPERATION = -32004

AJUDA = "Pedido invalido: use reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"

AGENT_CARD = {
    "name": "Central de Salas",
    "description": "Reserva salas de reuniao da Hill Valley Tech.",
    "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
    "version": "1.0.0",
    "supportedInterfaces": [{"url": URL_PUBLICA, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
    "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {
            "id": "reservar-sala",
            "name": "Reservar sala",
            "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
            "tags": ["salas", "agenda"],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
            "examples": [
                "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
            ],
        }
    ],
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class ErroA2A(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ------------------------------------------------------------------ estado


@dataclass
class Pausa:
    """O que o agente guarda enquanto a Task espera o cliente A2A. Nunca sai pela API A2A."""

    tool: str
    argumentos: dict[str, Any]
    chave: str  # chave atribuida pelo servidor no inputRequests
    campo: str  # propriedade do requestedSchema a preencher
    opcoes: list[str]  # enum da elicitation, na ordem recebida
    request_state: str  # opaco: guardado e ecoado, nunca aberto
    politica: str


TASKS: dict[str, dict[str, Any]] = {}  # visao publica (o que GetTask devolve)
TRACES: dict[str, str] = {}  # task_id -> trace-id propagado ao MCP
PAUSAS: dict[str, Pausa] = {}  # task_id -> estado MRTR pendente
LOCKS: dict[str, asyncio.Lock] = {}

MCP: ClienteMCP


def _id(prefixo: str) -> str:
    return f"{prefixo}-{secrets.token_hex(6)}"


def _mensagem_agente(task: dict[str, Any], texto: str) -> dict[str, Any]:
    return {
        "messageId": _id("msg"),
        "role": "ROLE_AGENT",
        "parts": [{"text": texto}],
        "taskId": task["id"],
        "contextId": task["contextId"],
    }


def _status(task: dict[str, Any], estado: str, texto: str | None = None) -> None:
    status: dict[str, Any] = {"state": estado}
    if texto is not None:
        msg = _mensagem_agente(task, texto)
        task["history"].append(msg)
        status["message"] = msg
    task["status"] = status
    log(f"[a2a] task={task['id']} -> {estado}" + (f" ({texto})" if texto else ""))


def _texto(mensagem: dict[str, Any]) -> str:
    return " ".join(p.get("text", "") for p in mensagem.get("parts", []) if isinstance(p, dict)).strip()


def _trace_id(traceparent: str | None) -> str | None:
    m = re.fullmatch(r"[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}", (traceparent or "").strip().lower())
    return m.group(1) if m and m.group(1) != "0" * 32 else None


# ------------------------------------------------------------- traducao de texto


def _parse_pedido(texto: str) -> dict[str, str] | None:
    m = re.fullmatch(r"reservar\s+(.+)", texto.strip(), flags=re.S)
    if not m:
        return None
    pares = re.findall(r"(\w+)=(.*?)(?=\s+\w+=|\s*$)", m.group(1).strip(), flags=re.S)
    return {k: v.strip() for k, v in pares} or None


def _parse_escolha(texto: str) -> str | None:
    m = re.fullmatch(r"escolha=(\S+)", texto.strip())
    return m.group(1) if m else None


def _linha_alternativas(opcoes: list[str]) -> str:
    return "alternativas: " + ", ".join(opcoes)


# --------------------------------------------------------------------- a ponte


def _extrair_pergunta(resultado: dict[str, Any]) -> tuple[str, str, list[str]]:
    """Le a elicitation em form mode do input_required: (chave, campo, opcoes)."""
    pedidos = resultado.get("inputRequests") or {}
    if len(pedidos) != 1:
        raise ValueError("input_required com quantidade inesperada de inputRequests")
    chave, pedido = next(iter(pedidos.items()))
    params = pedido.get("params") or {}
    if pedido.get("method") != "elicitation/create" or params.get("mode", "form") != "form":
        raise ValueError("input_required sem elicitation em form mode")
    for campo, schema in ((params.get("requestedSchema") or {}).get("properties") or {}).items():
        opcoes = schema.get("enum") or ([schema["const"]] if "const" in schema else [])
        if opcoes:
            return chave, campo, [str(o) for o in opcoes]
    raise ValueError("elicitation sem lista de opcoes")


def _traduzir_resultado(
    task: dict[str, Any], resultado: dict[str, Any], tool: str, argumentos: dict[str, Any], politica: str
) -> None:
    """Traduz o resultado MCP para o estado da Task. AQUI o input_required vira pausa."""
    task_id = task["id"]

    if resultado.get("resultType") == "input_required":
        chave, campo, opcoes = _extrair_pergunta(resultado)
        PAUSAS[task_id] = Pausa(
            tool=tool,
            argumentos=argumentos,
            chave=chave,
            campo=campo,
            opcoes=opcoes,
            request_state=resultado["requestState"],
            politica=politica,
        )
        _status(task, INPUT_REQUIRED, _linha_alternativas(opcoes))
        return

    PAUSAS.pop(task_id, None)
    if resultado.get("isError"):
        texto = " ".join(c.get("text", "") for c in resultado.get("content", []) if c.get("type") == "text")
        _status(task, FAILED, texto.strip() or "Erro na execucao da tool")
        return

    dados = resultado.get("structuredContent") or {}
    if dados.get("reservado") is False:
        _status(task, CANCELED, f"Reserva nao realizada: {dados.get('motivo') or 'recusado'}.")
        return

    reserva = {
        "reserva": dados.get("reserva"),
        "sala": dados.get("sala"),
        "inicio": dados.get("inicio"),
        "fim": dados.get("fim"),
        "responsavel": dados.get("responsavel"),
        "politica": politica,
    }
    task["artifacts"].append({"artifactId": _id("art"), "name": "reserva", "parts": [{"text": json.dumps(reserva)}]})
    _status(task, COMPLETED, f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.")


async def _iniciar(task: dict[str, Any], pedido: dict[str, str]) -> None:
    trace = TRACES.get(task["id"])
    _status(task, WORKING)
    # Descoberta em runtime: nada de lista fixa de tools no codigo.
    tools = {t["name"]: t for t in await MCP.listar_tools(trace)}
    if TOOL_RESERVA not in tools:
        _status(task, FAILED, f"O servidor MCP nao oferece a tool {TOOL_RESERVA}")
        return
    texto_politica = await MCP.ler_resource(RESOURCE_POLITICA, trace)
    politica = texto_politica.splitlines()[0].split(":", 1)[1].strip() if texto_politica else ""

    schema = tools[TOOL_RESERVA].get("inputSchema") or {}
    faltando = [c for c in schema.get("required", []) if c not in pedido]
    if faltando:
        _status(task, FAILED, f"{AJUDA} (faltou: {', '.join(faltando)})")
        return
    propriedades = schema.get("properties") or {}
    argumentos = {k: v for k, v in pedido.items() if k in propriedades}

    resultado = await MCP.chamar_tool(TOOL_RESERVA, argumentos, trace)
    _traduzir_resultado(task, resultado, TOOL_RESERVA, argumentos, politica)


async def _continuar(task: dict[str, Any], texto: str) -> None:
    pausa = PAUSAS[task["id"]]
    escolha = _parse_escolha(texto)
    if escolha == "recusar":
        resposta: dict[str, Any] = {"action": "decline"}
    elif escolha in pausa.opcoes:
        resposta = {"action": "accept", "content": {pausa.campo: escolha}}
    else:
        # Fora do enum: continua pausada, repete a lista, nao fala com o MCP.
        _status(task, INPUT_REQUIRED, _linha_alternativas(pausa.opcoes))
        return

    _status(task, WORKING)
    # O retry: mesmo tools/call, id JSON-RPC novo, mesma chave, requestState ecoado intacto.
    resultado = await MCP.chamar_tool(
        pausa.tool,
        pausa.argumentos,
        TRACES.get(task["id"]),
        input_responses={pausa.chave: resposta},
        request_state=pausa.request_state,
    )
    _traduzir_resultado(task, resultado, pausa.tool, pausa.argumentos, pausa.politica)


async def _executar(task: dict[str, Any], acao) -> None:
    try:
        await acao
    except ErroMCP as e:
        PAUSAS.pop(task["id"], None)
        _status(task, FAILED, f"Erro do servidor MCP ({e.code}): {e.message}")
    except Exception as e:  # noqa: BLE001 - qualquer falha termina a Task, nunca a deixa em WORKING
        PAUSAS.pop(task["id"], None)
        log(f"[a2a] task={task['id']} falhou: {e!r}")
        _status(task, FAILED, f"Falha ao processar o pedido: {e}")


# ---------------------------------------------------------------- metodos A2A


def _visao(task: dict[str, Any], history_length: int | None = None) -> dict[str, Any]:
    visao = json.loads(json.dumps(task))
    if history_length is not None:
        visao["history"] = visao["history"][-history_length:] if history_length > 0 else []
    return visao


async def send_message(params: dict[str, Any], traceparent: str | None) -> dict[str, Any]:
    mensagem = params.get("message")
    if not isinstance(mensagem, dict) or not isinstance(mensagem.get("parts"), list):
        raise ErroA2A(INVALID_PARAMS, "params.message com parts e obrigatorio")
    mensagem = {**mensagem, "messageId": mensagem.get("messageId") or _id("msg")}
    texto = _texto(mensagem)
    task_id = mensagem.get("taskId")

    if task_id:
        task = TASKS.get(task_id)
        if task is None:
            raise ErroA2A(TASK_NOT_FOUND, f"Task nao encontrada: {task_id}")
        async with LOCKS[task_id]:
            estado = task["status"]["state"]
            if estado in TERMINAIS:
                raise ErroA2A(UNSUPPORTED_OPERATION, f"Task {task_id} esta em estado terminal ({estado})")
            if estado != INPUT_REQUIRED:
                raise ErroA2A(UNSUPPORTED_OPERATION, f"Task {task_id} nao esta aguardando entrada ({estado})")
            task["history"].append(mensagem)
            if task_id not in TRACES and (tid := _trace_id(traceparent)):
                TRACES[task_id] = tid
            await _executar(task, _continuar(task, texto))
            return {"task": _visao(task)}

    task = {
        "id": _id("task"),
        "contextId": mensagem.get("contextId") or _id("ctx"),
        "status": {"state": SUBMITTED},
        "history": [],
        "artifacts": [],
    }
    mensagem["taskId"], mensagem["contextId"] = task["id"], task["contextId"]
    task["history"].append(mensagem)
    TASKS[task["id"]] = task
    LOCKS[task["id"]] = asyncio.Lock()
    TRACES[task["id"]] = _trace_id(traceparent) or secrets.token_hex(16)
    log(f"[a2a] task={task['id']} -> {SUBMITTED} trace={TRACES[task['id']]}")

    async with LOCKS[task["id"]]:
        pedido = _parse_pedido(texto)
        if pedido is None:
            _status(task, FAILED, AJUDA)
        else:
            await _executar(task, _iniciar(task, pedido))
    return {"task": _visao(task)}


async def get_task(params: dict[str, Any], _traceparent: str | None) -> dict[str, Any]:
    task_id = params.get("id")
    task = TASKS.get(task_id) if isinstance(task_id, str) else None
    if task is None:
        raise ErroA2A(TASK_NOT_FOUND, f"Task nao encontrada: {task_id}")
    history_length = params.get("historyLength")
    return {"task": _visao(task, history_length if isinstance(history_length, int) else None)}


METODOS = {
    "SendMessage": send_message,
    "GetTask": get_task,
    # nomes da v0.3, aceitos por conveniencia
    "message/send": send_message,
    "tasks/get": get_task,
}


# ------------------------------------------------------------------------- http


def _erro(id_: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}})


async def rota_card(_: Request) -> JSONResponse:
    return JSONResponse(AGENT_CARD)


async def rota_a2a(request: Request) -> JSONResponse:
    try:
        corpo = await request.json()
    except ValueError:
        return _erro(None, PARSE_ERROR, "JSON invalido")
    if not isinstance(corpo, dict) or corpo.get("jsonrpc") != "2.0" or "method" not in corpo:
        return _erro(corpo.get("id") if isinstance(corpo, dict) else None, INVALID_REQUEST, "Request JSON-RPC invalido")
    id_, metodo, params = corpo.get("id"), corpo["method"], corpo.get("params") or {}
    log(f"[a2a] method={metodo} id={id_} traceparent={request.headers.get('traceparent', '-')}")
    handler = METODOS.get(metodo)
    if handler is None:
        return _erro(id_, METHOD_NOT_FOUND, f"Metodo nao suportado: {metodo}")
    try:
        resultado = await handler(params, request.headers.get("traceparent"))
    except ErroA2A as e:
        return _erro(id_, e.code, e.message)
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": resultado})


@asynccontextmanager
async def ciclo_de_vida(_app: Starlette):
    global MCP
    MCP = ClienteMCP(MCP_URL)
    log(f"agente A2A em http://{HOST}:{PORTA}/a2a, servidor MCP em {MCP_URL}")
    try:
        yield
    finally:
        await MCP.fechar()


app = Starlette(
    routes=[
        Route("/.well-known/agent-card.json", rota_card, methods=["GET"]),
        Route("/a2a", rota_a2a, methods=["POST"]),
    ],
    lifespan=ciclo_de_vida,
)


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORTA, log_level="warning")
