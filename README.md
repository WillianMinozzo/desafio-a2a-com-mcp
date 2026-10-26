# A Ponte: agente A2A com MCP por dentro

Entrega do desafio. Dois processos Python separados:

| Processo | Pasta | Porta | Endpoint |
|---|---|---|---|
| Servidor MCP `central-de-salas` (SDK oficial `mcp==2.3.0`, spec 2026-07-28, Streamable HTTP stateless) | `servidor-mcp/` | 7301 | `/mcp` |
| Agente `Central de Salas` (A2A v1.0, binding JSON-RPC; host MCP por dentro) | `agente/` | 7300 | `/a2a` e `/.well-known/agent-card.json` |

O agente fala com o servidor MCP só por HTTP. Não há LLM no caminho de execução nem dependência de SDK de provedor de LLM.

## Como rodar

Requisitos: Python 3.10+ e Git.

### Linux / macOS (bash)

```bash
git clone https://github.com/WillianMinozzo/desafio-a2a-com-mcp.git
cd desafio-a2a-com-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install ./servidor-mcp ./agente

# chave de integridade do requestState (>= 32 bytes aleatorios; nunca commite o valor)
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
```

Terminal 1, servidor MCP (o log de cada request vai para o stderr):

```bash
source .venv/bin/activate
export REQUEST_STATE_SECRET=<o mesmo valor gerado acima>
python servidor-mcp/servidor.py
```

Terminal 2, agente:

```bash
source .venv/bin/activate
python agente/agente.py
```

Terminal 3, validador (com os dois processos recém-iniciados):

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

### Windows (PowerShell)

```powershell
git clone https://github.com/WillianMinozzo/desafio-a2a-com-mcp.git
cd desafio-a2a-com-mcp
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install ./servidor-mcp ./agente

# Terminal 1
$env:REQUEST_STATE_SECRET = py -c "import secrets; print(secrets.token_hex(32))"
python servidor-mcp/servidor.py

# Terminal 2 (com o venv ativado)
python agente/agente.py

# Terminal 3
py validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Sem `REQUEST_STATE_SECRET` (ou com menos de 32 bytes) o servidor MCP não sobe e diz como gerar a chave. Para o teste de restart, reinicie o servidor MCP com **o mesmo** valor da variável.

Variáveis opcionais (os padrões são os do validador): `MCP_HOST`, `MCP_PORT` (7301), `AGENTE_HOST`, `AGENTE_PORT` (7300), `MCP_URL` (`http://127.0.0.1:7301/mcp`), `AGENTE_URL_PUBLICA` (URL anunciada no card).

## Onde a ponte acontece

Tudo em `agente/agente.py`. Em `_traduzir_resultado`, quando o `tools/call` volta com `resultType == "input_required"`, o agente extrai da elicitation em form mode a chave do `inputRequests` e o `enum` do `requestedSchema`, guarda num objeto `Pausa` em `PAUSAS[task_id]` (tool, argumentos, chave, opções e o `requestState` opaco) e põe a Task em `TASK_STATE_INPUT_REQUIRED` com a mensagem `alternativas: <ids>`. O caminho de volta está em `_continuar`: o `SendMessage` com `taskId` e `escolha=<id>` vira `{"action": "accept", "content": {...}}` (ou `escolha=recusar` vira `{"action": "decline"}`), e o agente repete o mesmo `tools/call` com a mesma chave no `inputResponses` e o `requestState` ecoado sem modificação. O id JSON-RPC novo vem do contador monotônico em `agente/cliente_mcp.py` (`ClienteMCP._ids`), então o retry nunca reaproveita o id do request inicial. O cliente MCP do agente é HTTP puro justamente para enxergar o `input_required` cru: um cliente com callback de elicitation responderia sozinho e a Task nunca pausaria.

No servidor (`servidor-mcp/servidor.py`), `reservar_sala` declara retorno `ReservaOut | InputRequiredResult`. Em conflito, `_perguntar` calcula as alternativas, confere a capability de elicitation em form mode do request (senão `-32021` com `requiredCapabilities`, HTTP 400) e devolve um `InputRequiredResult` com a elicitation na chave `escolha_de_sala` e, como `requestState`, a lista de alternativas oferecidas. No retry, a tool lê `inputResponses` e `requestState` de dentro de `params` (via `Context`) e valida a escolha contra as alternativas que foram seladas, não contra um recálculo.

## Decisões técnicas

**Proteção do `requestState`.** Uso o `RequestStateBoundary` do próprio SDK, configurado com `RequestStateSecurity(keys=[REQUEST_STATE_SECRET], ttl=600, bind_principal=None)`. O token é AES-256-GCM (chave derivada por HKDF-SHA256), ou seja, cifrado e autenticado: qualquer caractere trocado falha na tag e volta `-32602 Invalid or expired requestState`. O envelope selado carrega `iat`, `exp`, método, nome da tool, um digest dos argumentos e a audiência (`central-de-salas`). Retry com argumentos diferentes dos selados falha na verificação de binding e é rejeitado com `-32602` (o caminho "rejeitar o estado" aceito pelo enunciado). A chave só vem da variável de ambiente; não há segredo no código. `bind_principal=None` porque o desafio não tem autenticação.

**Validade.** 10 minutos (`TTL_REQUEST_STATE = 600`), dentro da faixa de 5 a 30.

**Stateless de verdade.** O servidor não guarda nada entre o `input_required` e o retry: as alternativas oferecidas viajam seladas no `requestState`, e a escolha é validada contra elas. Como a chave vem do ambiente e não do processo, um retry funciona depois de reiniciar o servidor MCP, mesmo que as reservas em memória tenham mudado. Se a sala escolhida tiver sido ocupada entre a pergunta e o retry, o servidor pergunta de novo em vez de gravar sobreposição. Cada request precisa trazer `protocolVersion` e `clientCapabilities` no `_meta` (`-32602` e HTTP 400 caso contrário); os headers `MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name` são conferidos contra o corpo (`-32020`).

**Estado das Tasks.** Em memória no processo do agente: `TASKS` (a visão pública que `GetTask` devolve), `PAUSAS` (o estado MRTR pendente, que nunca sai pela API A2A) e `TRACES` (o trace-id da Task). Cada Task tem um `asyncio.Lock`, então duas continuações da mesma Task não se cruzam e Tasks diferentes nunca compartilham `requestState`. Estados terminais (`COMPLETED`, `CANCELED`, `FAILED`) são definitivos: novo `SendMessage` na Task é recusado com erro JSON-RPC `-32004`. Task inexistente devolve `-32001`.

**Host MCP.** Para cada Task o agente faz `tools/list` (descoberta em runtime; os argumentos são montados a partir do `inputSchema` descoberto), lê `politica://uso` e extrai a versão da primeira linha (vai no campo `politica` do artifact), e só então chama `reservar_sala`. Todo request leva `_meta` completo com `{"elicitation": {"form": {}}}` e os headers espelhados. O agente não implementa regra de sala: conflito, política e alternativas são decisão do servidor MCP, e erros de execução (`isError: true`) terminam a Task em `TASK_STATE_FAILED` com o texto da tool.

**Trace.** O agente lê o `traceparent` do header A2A, guarda o trace-id na Task e envia em todo request MCP daquela Task um `traceparent` com o mesmo trace-id e span-id novo. O servidor MCP registra no stderr, para todo POST recebido (antes de qualquer validação), `method`, `id`, `name` (tool ou URI), a marca `retry` quando há `requestState` e o `traceparent`.

**Persistência.** Reservas em memória, recarregadas de `dados/reservas.json` a cada start, como o enunciado permite.

## Saída do validador

```
trace-id desta execucao: fe7167a1d48bef9f644cc6a2ceb1f65d
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
