# Procedimento operacional - primeira escrita real (futura)

> **Referência histórica (ciclo encerrado em 2026-11-26).** Este runbook não deve ser executado no próximo ciclo. Ele descreve a preparação da primeira escrita do fluxo legado; o primeiro marco da retomada está em [`docs/project-handoff.md`](project-handoff.md). Os gates permanecem desligados.

**Status:** documentação de preparação. Nenhum comando aqui foi executado para escrita real.  
**Pré-requisito:** ler `docs/readiness.md`. O resultado global atual é `blocked`.

Este runbook descreve como seria a primeira execução após uma autorização humana explícita. Ele **não** implementa autorização automática e **não** deve ser usado para contornar `MIGRATION_ALLOW_WRITES=false` nem `MIGRATION_ENABLE_REAL_WRITE_TRANSPORT=false`.

**Estado dos dados em 2026-11-25:** coleta completa em `stage9-full-stash-browser-v2-20261125` (1.114 jogos, 76 reviews). Correspondências: 66 candidatas, 10 ambíguas, 4 sem candidato; 0 aprovadas. Destino: 1 review (RE4), nenhuma sobreposta. Decisões `liked`: 76 pendentes. Relatório: `data/migration/stage9/liked-pending.md`.

## 0. Conceitos que não podem ser confundidos

| Evidência | O que prova | O que não prova |
|---|---|---|
| Aprovação por hash | O plano/manifesto exatos foram aprovados | Autorização para escrever agora |
| Preflight `compatible` | O destino não mudou desde a aprovação | Que a escrita será aplicada |
| Confirmação de ação | O humano viu o payload exato | Autorização para o transporte real |
| HTTP 2xx | O servidor respondeu | Que a review foi persistida |
| Releitura pós-escrita | O estado observado no destino | Que não houve efeito colateral |

A autorização de escrita real é uma decisão separada e registrada. Ela não pode ser inferida de `--confirm`.

## 1. Pré-condições de entrada

- [ ] `MIGRATION_ALLOW_WRITES=false` durante toda a preparação.
- [ ] Transporte real indisponível/bloqueado.
- [ ] Branch e commit revisados; nenhum commit automático.
- [ ] Coleção de origem íntegra e escopo compreendido.
- [ ] Snapshot de destino íntegro, completo e da identidade correta.
- [ ] Correspondência do jogo aprovada por humano.
- [ ] Decisão `liked` registrada para a review.
- [ ] Plano novo, íntegro, estruturalmente válido.
- [ ] Aprovação vinculada ao plano/manifesto exatos.
- [ ] Preflight novo, autenticado, íntegro e `compatible`.
- [ ] Revisão humana da implementação do transporte real.
- [ ] Autorização explícita e separada para a escrita real.

Se qualquer item faltar: **parar**.

## 2. Sequência proposta

### Etapa 1 — Validar a coleta de origem

Verificar:

- `data/stash/runs/<run-id>/manifest.json`;
- `games.json` e `reviews.json`;
- `complete` e `requested_scope_complete`;
- slug, título, ano, review, nota e texto.

O piloto usa coleta direcionada. Isso é suficiente para um alvo, mas **não** para uma migração completa.

### Etapa 2 — Validar o snapshot do destino

Verificar:

- manifesto e hash do snapshot;
- `identity.verified`, `user_id`;
- `capture.complete`, `pagination.complete`;
- `exists` e conteúdo da review.

Captura incompleta nunca significa ausência.

### Etapa 3 - Confirmar correspondência

Correspondências `strong_candidate` são aprovadas automaticamente pela política da interface. Revisar as ambíguas/sem candidato em `matches.json` ou na UI; não aceitar candidato ambíguo sem evidência.

### Etapa 4 - Revisar decisões de `liked`

A política automática preenche `true` para nota ≥ 7 e `false` para nota ≤ 5; notas entre 5 e 7 permanecem `unresolved`. O usuário deve revisar e pode substituir qualquer decisão por `source: human`.

Exemplo de comando existente:

```powershell
python -m src.cli migration liked decide --source-ref demolition-girl --game-id 26570 --value false --review-file data/stash/runs/stage9-full-stash-browser-v2-20261125/reviews.json
```

O valor acima é apenas sintaxe; a decisão final é do usuário.

### Etapa 5 - Gerar novo plano

Revisar as correspondências ambíguas/sem candidato em `data/migration/stage9/matches-review.md` ou na interface e decidir cada uma. Correspondências fortes já estão aprovadas. Depois gerar um plano novo; não editar o plano bloqueado para fingir que está pronto.

### Etapa 6 — Validar o plano

```powershell
python -m src.cli migration verify-plan --plan-id <novo-plan-id>
python -m src.cli validate --plan <plan.json>
```

Exigir:

- `invalid: []`, `stale: []`, `blocked: []`;
- ação `review`, `before:null`, `liked` decidido;
- payload com `game_id`, `rating`, `text`, `liked` booleano.

### Etapa 7 — Aprovar hashes

```powershell
python -m src.cli migration approve --plan-id <novo-plan-id> --confirm <novo-plan-id>
python -m src.cli migration approval-status --plan-id <novo-plan-id>
```

A aprovação deve apontar para o manifesto exato. Aprovação antiga não serve.

### Etapa 8 — Gerar preflight atualizado

```powershell
python -m src.cli migration preflight --plan-id <novo-plan-id> --preflight-id <novo-preflight-id> --headless
python -m src.cli migration verify-preflight --preflight-id <novo-preflight-id>
```

Exigir `status: compatible`, identidade correta, ações `compatible`, paginação completa e nenhum `unsupported`.

### Etapa 9 — Preparar execução

```powershell
python -m src.cli migration execution-prepare --plan-id <novo-plan-id> --preflight-id <novo-preflight-id> --execution-id <execution-id>
python -m src.cli migration execution-validate --execution-id <execution-id>
```

A preparação **não** escreve e **não** autoriza escrita.

### Etapa 10 — Revisar payload

```powershell
python -m src.cli migration execution-show --execution-id <execution-id>
```

Conferir jogo, `game_id`, nota, texto, `liked`, `before:null`, hashes e identidade.

### Etapa 11 — Confirmar individualmente

```powershell
python -m src.cli migration execution-confirm --execution-id <execution-id> --action-id <action-id> --confirm <execution-id>
```

Isso registra apenas confirmação documental. Nenhuma escrita ocorre.

### Etapa 12 — Autorização explícita para escrita real

Depois da revisão do plano/execução, habilitar **somente no ambiente local**:

```powershell
$env:MIGRATION_ALLOW_WRITES = "true"
$env:MIGRATION_ENABLE_REAL_WRITE_TRANSPORT = "true"
```

Os dois gates são necessários; nenhum é suficiente. A autorização deve ser uma decisão humana separada, registrada com:

- `execution_id`, `action_id`, hashes de plano/manifesto/preflight/payload;
- identidade do destino;
- escopo: uma única ação;
- validade: até a primeira tentativa ou cancelamento;
- ciência de que timeout/erro HTTP não autoriza retry.

Não criar essa autorização automaticamente e não reutilizá-la para outra ação. Após a execução, desligar os gates.

### Etapa 13 — Executar somente uma ação

```powershell
python -m src.cli migration execution-run --execution-id <execution-id> --action-id <action-id> --confirm <execution-id>
```

O comando:

- exige `--confirm` exatamente igual ao `execution_id`;
- valida execução, aprovação, preflight, confirmação e payload;
- faz leitura imediatamente anterior;
- envia no máximo um POST;
- nunca continua para outra ação;
- classifica timeout/erro/conexão como ambíguo.

**Não executar nesta etapa.** Nenhum ensaio real foi feito.

### Etapa 14 — Verificar o resultado

Após a escrita real, o gateway:

- relê o destino com captura completa;
- valida identidade;
- compara `game_id`, dono, nota, texto e `liked`;
- registra evidência em `data/migration/executions/<id>/`.

HTTP 2xx isolado não basta. Se a releitura falhar ou não confirmar, usar `execution-status`/`execution-reconcile`/`execution-resolve`.

### Etapa 15 — Parar e revisar

Parar após a primeira ação. Revisar:

- `execution-show`, `execution-status`, `execution-verify`;
- review no destino;
- eventos e verificação;
- qualquer divergência.

Somente depois de revisão humana considerar uma segunda ação.

## 3. Matriz de recuperação

Comandos de diagnóstico existentes: `execution-show`, `execution-status`, `execution-validate`, `execution-verify`, `execution-reconcile`, `execution-resolve`.

| # | Situação | Estado resultante | Diagnóstico | Evidência necessária | Novo POST? | Decisão humana |
|---|---|---|---|---|---|---|
| 1 | Falha antes do envio | ação `failed`; execução `completed_with_errors` | `execution-show/status` | erro `WriteNotSent`, sem `write_result` | Proibido automático | Criar nova execução/ação se decidir tentar |
| 2 | Timeout após envio | `write_ambiguous`; execução `inconclusive` | `execution-status` | `write_result` ausente/erro de timeout | Proibido | Nova leitura autenticada; `execution-resolve` |
| 3 | Conexão interrompida após envio | `write_ambiguous`; `write_outcome_unknown` | `execution-show` | erro do transporte, sem status confiável | Proibido | Releitura + resolução |
| 4 | HTTP 4xx/5xx | `write_ambiguous`; `http_<code>_unconfirmed` | `execution-show` | status HTTP registrado | Proibido | Releitura; não presumir falha |
| 5 | Review ausente na 1ª releitura | `write_ambiguous`; `review_not_found_after_write` | `execution-status` | snapshot completo sem review | Proibido | Repetir leitura; ausência não libera POST |
| 6 | Review encontrada divergente | `conflict`; `completed_with_errors` | `execution-show` | snapshot com campos divergentes | Proibido | Revisar manualmente; não corrigir automaticamente |
| 7 | Captura posterior incompleta | `write_ambiguous` | `execution-status` | `capture.complete:false` | Proibido | Repetir captura; resolver só com evidência conclusiva |
| 8 | Processo interrompido durante a ação | persistido `writing`/`running` | `execution-status` | ausência de resultado final | Proibido | `execution-reconcile` → `execution-resolve` |
| 9 | Interrupção após verificação | `verified` se persistido; senão `verification_pending` | `execution-show/status` | evento + snapshot de verificação | Proibido se ambíguo | Se `verification_pending`, reconcile e reler |
| 10 | Falha de persistência local | exceção; ação possivelmente `writing` | logs + `execution-status` | arquivo ausente/corrompido | Proibido | `execution-verify`; reconcile; revisar disco |

Regra universal: **um resultado ambíguo nunca é convertido em sucesso ou falha sem evidência autenticada e decisão humana**.

## 4. Critérios de parada imediata

Parar toda a operação se:

- identidade do destino divergir;
- preflight não for `compatible`;
- payload exibido não corresponder ao plano aprovado;
- confirmação não corresponder à execução/ação;
- qualquer ação ficar `write_ambiguous`/`conflict`/`inconclusive`;
- a leitura prévia encontrar review existente;
- houver falha de persistência local;
- houver dúvida sobre o contrato do backend.

## 5. Requisitos do transporte real (implementado, não executado)

1. Autenticação pela sessão existente, tokens/cookies só em memória.
2. Host/path/método fixos: `https://jazerei.com/api/reviews/upsert`, `POST`.
3. Sem redirects; sem hosts alternativos.
4. Payload exato `{game_id, rating, text, liked}`; `liked` booleano.
5. Timeout explícito; sem retry de POST.
6. Sem log de headers/cookies/token.
7. Resposta usada só como sinal; verificação posterior obrigatória.
8. Injeção explícita; somente `execution-run` constrói o transporte.
9. Gate duplo: `MIGRATION_ALLOW_WRITES=true` + `MIGRATION_ENABLE_REAL_WRITE_TRANSPORT=true`, além de autorização por ação.
10. Testes com sessão falsa cobrindo timeout, 4xx, 5xx, redirect, conexão interrompida e resposta vazia/divergente.

## 6. Registro da execução

Preservar em `data/migration/executions/<execution-id>/`:

- `execution.json`, `manifest.json`;
- `events.jsonl`, `confirmations.jsonl`;
- `write_result`, leitura prévia, verificação posterior;
- decisão de resolução/reconciliação, se houver.

Nunca sobrescrever execução anterior. Nunca apagar evidência ambígua para “limpar” o estado.

## 7. Condição de encerramento

A primeira execução real só termina quando:

- a ação estiver `verified` com releitura autenticada; ou
- houver ambiguidade/erro explicitamente registrado e revisado; e
- o operador decidir parar.

Aguarde revisão humana antes de qualquer nova ação.
