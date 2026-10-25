"""Servidor MCP "central-de-salas" (Streamable HTTP, stateless, spec 2026-07-28).

Tres tools (listar_salas, consultar_disponibilidade, reservar_sala) e um resource
(politica://uso). O conflito de horario em reservar_sala vira MRTR: o resolver
`escolha_de_sala` devolve um `Elicit`, o SDK responde `input_required` e sela o
`requestState` com AES-256-GCM (RequestStateBoundary) usando REQUEST_STATE_SECRET.
Nada fica em memoria entre o input_required e o retry.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

import uvicorn
from pydantic import BaseModel, Field, create_model

from mcp.server.mcpserver import (
    Elicit,
    ElicitationResult,
    AcceptedElicitation,
    MCPServer,
    RequestStateSecurity,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ToolError

RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / "dados"
HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORTA = int(os.environ.get("MCP_PORT", "7301"))
TTL_REQUEST_STATE = 600.0  # 10 minutos (o enunciado exige entre 5 e 30)

TZ_SP = timezone(timedelta(hours=-3))
JANELA_INICIO = time(8, 0)
JANELA_FIM = time(20, 0)
DURACAO_MAX = timedelta(hours=2)
MAX_ALTERNATIVAS = 3

ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- dados

SALAS: list[dict[str, Any]] = json.loads((DADOS / "salas.json").read_text(encoding="utf-8"))
SALAS_POR_ID = {s["id"]: s for s in SALAS}
RESERVAS: list[dict[str, Any]] = json.loads((DADOS / "reservas.json").read_text(encoding="utf-8"))
POLITICA_TEXTO = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")
POLITICA_VERSAO = POLITICA_TEXTO.splitlines()[0].split(":", 1)[1].strip()


def _proximo_id() -> str:
    maior = max((int(r["id"].split("-")[1]) for r in RESERVAS), default=0)
    return f"res-{maior + 1:04d}"


# ------------------------------------------------------------------------- regras


def _parse(valor: str) -> datetime:
    try:
        dt = datetime.fromisoformat(valor)
    except ValueError as exc:
        raise ToolError(f"Data invalida: {valor}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_SP)
    return dt


def validar(sala: str, inicio: str, fim: str) -> tuple[datetime, datetime]:
    """Validacoes de sala e de politica, comuns a consulta e a reserva."""
    if sala not in SALAS_POR_ID:
        raise ToolError(f"Sala inexistente: {sala}")
    ini, fi = _parse(inicio), _parse(fim)
    if fi <= ini:
        raise ToolError(ERRO_INTERVALO)
    ini_sp, fim_sp = ini.astimezone(TZ_SP), fi.astimezone(TZ_SP)
    dentro = (
        ini_sp.date() == fim_sp.date()
        and JANELA_INICIO <= ini_sp.time() <= JANELA_FIM
        and JANELA_INICIO <= fim_sp.time() <= JANELA_FIM
    )
    if not dentro:
        raise ToolError(ERRO_JANELA)
    if fi - ini > DURACAO_MAX:
        raise ToolError(ERRO_DURACAO)
    return ini, fi


def conflitos(sala: str, ini: datetime, fi: datetime) -> list[dict[str, Any]]:
    return [
        r
        for r in RESERVAS
        if r["sala"] == sala and _parse(r["inicio"]) < fi and ini < _parse(r["fim"])
    ]


def alternativas(sala: str, ini: datetime, fi: datetime) -> list[str]:
    capacidade = SALAS_POR_ID[sala]["capacidade"]
    livres = [
        s
        for s in SALAS
        if s["id"] != sala and s["capacidade"] >= capacidade and not conflitos(s["id"], ini, fi)
    ]
    livres.sort(key=lambda s: (s["capacidade"], s["id"]))
    return [s["id"] for s in livres[:MAX_ALTERNATIVAS]]


# ------------------------------------------------------------------------ schemas


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


class EscolhaBase(BaseModel):
    """Base do formulario da elicitation; o enum real e montado por pedido."""


def _modelo_escolha(opcoes: list[str]) -> type[EscolhaBase]:
    return create_model(
        "EscolhaDeSala",
        __base__=EscolhaBase,
        sala=(Literal[tuple(opcoes)], Field(title="Sala", description="Sala alternativa escolhida")),  # type: ignore[valid-type]
    )


# ------------------------------------------------------------------------- server


def _security() -> RequestStateSecurity:
    segredo = os.environ.get("REQUEST_STATE_SECRET", "")
    if len(segredo.encode()) < 32:
        raise SystemExit(
            "REQUEST_STATE_SECRET ausente ou com menos de 32 bytes. Gere com:\n"
            '  python3 -c "import secrets; print(secrets.token_hex(32))"'
        )
    # bind_principal=None: sem autenticacao neste desafio, o estado nao se liga a usuario.
    return RequestStateSecurity(keys=[segredo], ttl=TTL_REQUEST_STATE, bind_principal=None)


mcp = MCPServer(
    name="central-de-salas",
    version="1.0.0",
    request_state_security=_security(),
)


@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**s) for s in SALAS])


@mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    ini, fi = validar(sala, inicio, fim)
    em_conflito = [
        ConflitoOut(id=r["id"], inicio=r["inicio"], fim=r["fim"], responsavel=r["responsavel"])
        for r in conflitos(sala, ini, fi)
    ]
    return Disponibilidade(sala=sala, livre=not em_conflito, conflitos=em_conflito)


def escolha_de_sala(sala: str, inicio: str, fim: str) -> Elicit[EscolhaBase] | None:
    """Resolver do MRTR: so pergunta quando a sala pedida esta ocupada.

    Roda de novo em cada rodada; o SDK so aceita a resposta se a pergunta
    renderizada for identica a que foi selada no requestState.
    """
    ini, fi = validar(sala, inicio, fim)
    if not conflitos(sala, ini, fi):
        return None
    opcoes = alternativas(sala, ini, fi)
    if not opcoes:
        raise ToolError(ERRO_SEM_ALTERNATIVA)
    return Elicit(
        "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.",
        _modelo_escolha(opcoes),
    )


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    escolha: Annotated[ElicitationResult[EscolhaBase], Resolve(escolha_de_sala)],
) -> ReservaOut:
    if not isinstance(escolha, AcceptedElicitation):
        return ReservaOut(reservado=False, motivo="recusado")
    sala_final = escolha.data.sala if escolha.data is not None else sala
    ini, fi = validar(sala_final, inicio, fim)
    if conflitos(sala_final, ini, fi):  # defesa: nunca grava sobreposicao
        raise ToolError(ERRO_SEM_ALTERNATIVA)
    reserva = {"id": _proximo_id(), "sala": sala_final, "inicio": inicio, "fim": fim, "responsavel": responsavel}
    RESERVAS.append(reserva)
    log(f"reserva criada {reserva['id']} sala={sala_final} {inicio}..{fim} responsavel={responsavel}")
    return ReservaOut(
        reserva=reserva["id"],
        sala=sala_final,
        inicio=inicio,
        fim=fim,
        responsavel=responsavel,
        politica=POLITICA_VERSAO,
    )


@mcp.resource("politica://uso", name="politica-de-uso", description="Politica de uso das salas.", mime_type="text/markdown")
def politica_de_uso() -> str:
    return POLITICA_TEXTO


# --------------------------------------------------------------- log no stderr


def com_log(app):
    """ASGI wrapper: registra metodo, id e traceparent de todo POST, antes de qualquer validacao."""

    async def wrapper(scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await app(scope, receive, send)
        corpo = b""
        mais = True
        while mais:
            msg = await receive()
            corpo += msg.get("body", b"")
            mais = msg.get("more_body", False)
        try:
            dados = json.loads(corpo)
            params = dados.get("params") or {}
            meta = params.get("_meta") or {}
            alvo = params.get("name") or params.get("uri") or "-"
            retry = " retry" if "requestState" in params else ""
            log(
                f"[mcp] method={dados.get('method')} id={dados.get('id')} name={alvo}{retry} "
                f"traceparent={meta.get('traceparent', '-')}"
            )
        except (ValueError, AttributeError):
            log("[mcp] corpo nao-JSON")

        entregue = False

        async def receive_replay():
            nonlocal entregue
            if not entregue:
                entregue = True
                return {"type": "http.request", "body": corpo, "more_body": False}
            return await receive()

        return await app(scope, receive_replay, send)

    return wrapper


def main() -> None:
    app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True, host=HOST)
    log(f"servidor MCP em http://{HOST}:{PORTA}/mcp (politica {POLITICA_VERSAO})")
    uvicorn.run(com_log(app), host=HOST, port=PORTA, log_level="warning")


if __name__ == "__main__":
    main()
