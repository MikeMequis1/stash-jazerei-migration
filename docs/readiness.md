# Etapa 7 - Auditoria operacional e prontidão para a primeira escrita

> **Documento histórico (2026-11-26).** Esta auditoria descreve uma etapa anterior do ciclo encerrado. Não é uma autorização e não reflete todo o estado atual: depois dela houve 17 escritas reais verificadas, e os gates continuam desligados. Para retomar o projeto, leia [`docs/project-handoff.md`](project-handoff.md).

**Data:** 2026-11-24  
**Escopo:** auditoria local, sem escrita real.  
**Resultado global:** **`blocked`** — não há autorização para escrita real e o piloto continua bloqueado por `liked_unresolved`.

> **Atualização Etapa 8 (2026-11-24):** os quatro helpers legados de escrita identificados nesta auditoria (`upsert_review`, `update_profile`, `add_to_watchlist`, `remove_from_watchlist`) foram **removidos do código**. O transporte HTTP restante é somente de leitura e aceita POST apenas em endpoints de consulta allowlisted. A configuração `allow_writes=True` não reativa escrita. O histórico abaixo é preservado; onde dizia “existe”, leia-se “existia antes da Etapa 8”.

> **Atualização Etapa 9 (2026-11-25):** `BrowserWriteTransport` foi implementado exclusivamente no gateway, com endpoint fixo, POST, sem redirect/retry e dois gates (`MIGRATION_ALLOW_WRITES` + `MIGRATION_ENABLE_REAL_WRITE_TRANSPORT`), ambos falsos por padrão. Coleta completa: 1.114 jogos, 76 reviews, 76 detalhes; 66 candidatos, 10 correspondências ambíguas, 4 sem candidato; destino com 1 review (RE4); 76 decisões `liked` pendentes. Nenhuma escrita real. A prontidão global continua `blocked`.

Este documento não habilita execução. Ele responde o que está comprovado, o que depende de decisão humana e o que ainda é desconhecido.

Classificação usada:

- **[FATO]** observado no código, nos artefatos ou em teste local;
- **[HIPÓTESE]** plausível, mas não demonstrado;
- **[DESCONHECIDO]** exige contrato interno, documentação confiável ou teste real que não foi feito.

## 1. Veredito por dimensão

| Dimensão | Estado | Motivo resumido |
|---|---|---|
| Prontidão técnica | `blocked` (real) | Transporte real implementado no gateway, mas desabilitado por dois gates e nunca executado contra o serviço real. |
| Prontidão dos dados | `blocked` | Coleta íntegra; política automática aplicada, mas 9 correspondências ambíguas/sem candidato e 15 `liked` unresolved ainda bloqueiam parte do lote. |
| Prontidão operacional | `ready` (documentação) / `blocked` (execução) | Procedimento e recuperação documentados; nenhum ensaio real autorizado. |
| Prontidão de autorização | `pending_human` | Nenhuma autorização explícita para escrita real; confirmação documental não é autorização. |
| **Global** | **`blocked`** | Correspondências, `liked`, aprovação/preflight e autorização ainda não satisfeitos. |

`ready` técnico não significa autorizado. A autorização é uma decisão humana separada e posterior.

## 2. Estado do repositório

**[FATO]**

- Branch `main`; último commit `4746280`.
- Nenhum commit/push desta etapa.
- Há trabalho local não commitado e não rastreado que precisa ser preservado:
  - modificados: `.env.example`, `.gitignore`, `README.md`, `docs/{architecture,implementation-plan,jazerei-api,security,stash}.md`;
  - não rastreados: `src/`, `tests/`, `data/`, `reports/`, `pyproject.toml`, `requirements-browser.txt`, `docs/execution-log.md`.
- `.gitignore` cobre `.env`, `.venv`, caches, `data/stash/*`, `data/jazerei/*`, `data/migration/*`, `data/browser/`, `reports/*`.
- `.env` não existe no workspace auditado.
- Perfil de navegador existe em `data/browser/profile` e está ignorado pelo Git. Não foi inspecionado.
- Nenhuma chave sensível foi encontrada nos JSON/JSONL de `data/migration`, `data/stash` e `data/jazerei`.
- Dependências: `requests`, `beautifulsoup4`, `python-dotenv`, `pydantic`; opcionais `pytest`, `ruff`, `playwright`.
- Nenhuma limpeza, reset, checkout destrutivo ou alteração de artefato real do piloto foi feita.

## 3. Cadeia de proveniência do piloto

Todos os elos abaixo foram verificados localmente. Hashes conferem.

| Elo | Identificador | Origem / depende de | Hash / evidência | Validação |
|---|---|---|---|---|
| Coleta Stash | `pilot-demolition-girl-20261124-v4` | navegador Stash, usuário `MikeMequis1`, escopo direcionado | manifesto `fab23d47…2e81a`; `games.json` `7b3e7528…66112`; `reviews.json` `834bedc8…54b17` | `verify_manifest` + hash dos arquivos no manifesto do plano |
| Snapshot destino | `review-state-demolition-girl-20261124-v4` | sessão autenticada Já Zerei, `game_id` 26570, UID `fKJq…TF2` | manifesto `a7415a4d…889a`; snapshot `39d4be77…5582`; `response_hash` `f7b3945a…7974` | `verify_manifest` + hash do snapshot |
| Correspondência | `data/migration/demolition-girl/matches.json` | jogo de origem + catálogo Já Zerei + decisão humana | `08bdfffc…6bd` | hash em `manifest.matching.matches_sha256` |
| Decisão `liked` | ausente | review de origem + decisão humana | `data/migration/liked-decisions.jsonl` não existe; ação `unresolved`; versão de origem `83fc4024…21d3` | decisão obrigatória; validador bloqueia |
| Plano | `pilot-demolition-girl-20261124-plan` | origem + snapshot + matches + decisões | `plan_sha256` `9c547475…8abe`; arquivo `dbd1be34…e1e`; ação `c4a35763…3510` | `migration verify-plan` |
| Manifesto do plano | `manifest.json` | plano + proveniência declarada | `manifest_sha256` `7b423079…8023` | hash canônico e hash do arquivo |
| Aprovação | ausente | plano/manifesto + confirmação humana | `data/migration/approvals.jsonl` não existe; status `blocked` | `migration approval-status` |
| Preflight | `pilot-demolition-girl-20261124-preflight-blocked-v2` | plano + manifesto + aprovação + releitura autenticada | `result_sha256` `7a60fa36…77d4`; manifesto válido | `verify-preflight` |
| Execução | ausente | plano + preflight + confirmação + autorização | `data/migration/executions/` não existe | nada a validar |
| Confirmação | ausente | execução + payload + humano | depende de execução | nada a validar |
| Evidência de escrita | ausente | execução + transporte real | nenhuma escrita permitida | não aplicável |
| Verificação posterior | ausente | escrita + releitura autenticada | depende de escrita real | não aplicável |

### Como a cadeia invalida elos seguintes

- Alterar `games.json`/`reviews.json` quebra `manifest.source.files` → `verify_bundle` acusa `stale` → plano/aprovação/preflight/execução deixam de ser aceitos.
- Alterar o snapshot quebra `destination.snapshot_sha256`/`response_hash` → preflight e execução recusam.
- Alterar `matches.json` quebra `matching.matches_sha256` → plano inválido.
- Alterar a decisão `liked` muda `source_hash`/`decisions_sha256` → ação fica `stale` e o plano bloqueia.
- Alterar o plano muda `plan_file_sha256`, `plan_sha256` e `manifest_sha256` → aprovação antiga vira `stale`.
- Reaproveitar aprovação antiga: `approval_status` compara `manifest_sha256`/`plan_sha256`/ações e retorna `stale`.
- Alterar o preflight quebra `result_sha256`/manifesto → `verify-preflight` acusa.
- A execução exige preflight `compatible`, posterior à aprovação, do mesmo plano/manifesto/aprovação/identidade.
- O payload da execução tem hash próprio; confirmação vinculada a esse hash; verificação posterior compara conteúdo real.

**[FATO]** Não foi encontrada reutilização aceitável de aprovação/preflight incompatível nos testes: aprovação obsoleta, preflight de outro plano, preflight anterior à aprovação e preflight não compatível são recusados.

## 4. Situação do piloto Demolition Girl

| Item | Estado | Evidência |
|---|---|---|
| Run Stash | `ready` | v4, escopo direcionado, 1 jogo e 1 review, hashes válidos |
| Escopo direcionado | `ready` | `complete:false` global, `requested_scope_complete:true`; suficiente para 1 alvo, não para migração total |
| Jogo de origem | `ready` | `demolition-girl`, 2004, nota 2.0, review com texto |
| Snapshot destino | `ready` | `game_id` 26570, identidade `fKJq…TF2`, `exists:false`, captura completa |
| Correspondência | `ready` | decisão humana `approved`, confiança 0.9, hash válido |
| Plano | `blocked` | plano íntegro, mas 1 ação `unapproved` e `liked:null` |
| Decisão `liked` | `automatic: false` (nota 2.0) | política por nota; plano antigo não reavaliado |
| Aprovação | `blocked` | nenhuma aprovação registrada; `approval-status` = `blocked` |
| Preflight | `blocked` | motivo `liked_unresolved`; nenhuma captura de destino executada |
| Execução | `not_applicable` | nenhuma execução preparada |
| Confirmação | `not_applicable` | nenhuma ação confirmada |
| Escrita | `not_applicable` | proibida nesta etapa |

O preflight existente é evidência válida de bloqueio, não de compatibilidade. Ele **não** deve ser promovido a `compatible` sem decisão humana e nova captura.

Artefatos legados em `data/migration/demolition-girl/` (`plan.json`, `preflight.json`, `summary.json`) são de formato anterior e **não** são a fonte autoritativa. A fonte atual é o bundle em `data/migration/plans/pilot-demolition-girl-20261124-plan/`.

### Coleta completa da Etapa 9

| Item | Resultado |
|---|---|
| Run | `stage9-full-stash-browser-v2-20261125` |
| Jogos | 1.114 (38/38 páginas) |
| Reviews | 76 (13/13 páginas), 76 detalhes, 76 com texto |
| Divergências listagem/detalhe | 11 |
| `progress` ausente | 61 |
| Jogos sem plataforma / sem rating | 1.114 / 416 |
| Correspondências | 76: 66 `candidate`, 10 `manual_review`, 0 aprovadas |
| Sem candidato | 4 |
| Reviews no destino | 1 (RE4, `game_id` 974), nenhuma sobreposta à origem |
| Elegíveis estrito | 0 (correspondências e `liked` pendentes) |
| Potencialmente elegíveis após revisão | 76 |
| Decisões `liked` | 76 pendentes, 0 registradas |
| Planos/aprovações/execuções reais | 0 / 0 / 0 |

Tentativas anteriores preservadas: `stage9-full-stash-20261125` (HTTP 403) e `stage9-full-stash-browser-20261125` (Cloudflare parcial). A coleta só foi aceita após abrir a sessão do Stash e repetir; nenhum dado anterior foi sobrescrito.

## 5. Contrato `POST /api/reviews/upsert`

### Fatos confirmados

- **[FATO]** O frontend observado envia `POST https://jazerei.com/api/reviews/upsert` com `{game_id, rating, text, liked}`.
- **[FATO]** `src/jazerei/reviews.py` exige localmente `game_id`, `rating`, `text`, `liked`; ausência gera `ValueError`.
- **[FATO, antes da Etapa 8]** O cliente usava `client.write_json("POST", "/api/reviews/upsert", json=payload)`, que exigia `MIGRATION_ALLOW_WRITES=true`. Esse caminho foi removido.
- **[FATO]** O transporte HTTP usa timeout configurável e não inclui POST na política de retry (`allowed_methods={GET, HEAD}`).
- **[FATO]** HTTP >= 400 vira `HttpError` no transporte; não há tratamento de corpo de erro por endpoint.
- **[FATO]** A política do navegador bloqueia `/upsert`, `/insert`, `/update`, `/delete`, watchlist/favoritos/Playing Now.
- **[FATO]** O gateway da Etapa 6 não usava esse helper; ele aceita somente transporte `simulated=True`.
- **[FATO]** `liked` é recomendação do autor, não curtida da review.
- **[FATO]** A review do piloto no destino está ausente segundo snapshot completo.

### Hipóteses

- **[HIPÓTESE]** O endpoint cria a review quando ela não existe e atualiza quando existe.
- **[HIPÓTESE]** `rating` usa a mesma escala 0–10 observada na UI.
- **[HIPÓTESE]** `liked` é obrigatório e aceita apenas booleano.
- **[HIPÓTESE]** Uma resposta 2xx indica persistência, mas isso não foi verificado por releitura real após POST.
- **[HIPÓTESE]** O endpoint pode ser idempotente por `(user_id, game_id)`, mas isso não foi provado.

### Desconhecidos

- **[DESCONHECIDO]** Regras reais de validação do backend para `rating`, `text`, `liked`, campos extras e tipos.
- **[DESCONHECIDO]** Se `liked:null`/omissão é aceito e qual o default.
- **[DESCONHECIDO]** Formato exato de sucesso/erro e se HTTP 4xx/5xx pode ocorrer após persistência parcial.
- **[DESCONHECIDO]** Se o endpoint é idempotente em caso de repetição após timeout.
- **[DESCONHECIDO]** Rate limits, proteção CSRF, exigência de cookies/headers adicionais e comportamento de redirect.
- **[DESCONHECIDO]** Efeitos colaterais em perfil, contadores, moderação ou filas assíncronas.

Nenhuma chamada mutável foi feita para resolver esses pontos.

## 6. Auditoria do gateway e superfícies alternativas

### Comprovado

- **[FATO]** `execute_action` aceita transporte simulado ou, no caminho real, apenas `BrowserWriteTransport` com os dois gates verdadeiros. Antes da Etapa 9 recusava qualquer transporte não simulado.
- **[FATO histórico]** Antes da Etapa 8, nenhum comando CLI chamava `execute_action`, `upsert_review`, `update_profile` ou funções de watchlist. Os símbolos foram removidos.
- **[FATO]** CLI não possui `import`, `migrate`, `write`, `execution-run` ou equivalente.
- **[FATO]** Confirmação exige `--confirm <execution-id>` e é vinculada a plano/manifesto/preflight/ação/payload/identidade.
- **[FATO]** Leitura imediatamente anterior é feita por ação; não há leitura única reutilizada.
- **[FATO]** Timeout, conexão interrompida, HTTP 4xx/5xx, identidade divergente, captura incompleta e review ausente após envio são `write_ambiguous`.
- **[FATO]** `WriteNotSent` é a única falha pré-envio considerada definitiva.
- **[FATO]** Ação verificada não repete; ação ambígua bloqueia a execução e qualquer ação subsequente.
- **[FATO]** `execution-reconcile` classifica interrupções persistidas como ambíguas; `execution-resolve` não escreve e mantém ausência como ambígua.
- **[FATO]** `execution.json` é gravado com arquivo temporário + `os.replace`; eventos/confirmações são append-only.
- **[FATO]** Nenhuma credencial é persistida nos artefatos de execução; teste cobre `authorization`, `cookie`, `id_token`, `refresh_token`, `bearer`.

### Risco encontrado na Etapa 7 e resolvido na Etapa 8

**[FATO histórico]** Existiam funções de escrita reais em `src/jazerei/reviews.py`, `profile.py` e `watchlist.py`, protegidas apenas por `MIGRATION_ALLOW_WRITES` e fora do gateway.

**Resolução da Etapa 8:**

- os quatro helpers foram removidos;
- `JazereiClient` não possui `write_json`, `require_writes`, `post_json` ou `public_post_json`;
- `HttpClient` não aceita métodos mutáveis; `POST` só existe como `post_read_json` com allowlist de consulta;
- `importer.execute` (segunda camada de execução) foi removido;
- `allow_writes=True` não recria ou reativa nenhum helper;
- testes arquiteturais cobrem a ausência dos símbolos e a rejeição de endpoints mutáveis.

**[FATO]** A superfície pública de escrita fora do gateway deixou de existir no código.

| Superfície | Antes da Etapa 8 | Depois da Etapa 8 |
|---|---|---|
| `reviews.py` | `upsert_review` | removido; apenas leitura |
| `profile.py` | `update_profile`, `build_partial_update` | removidos; apenas leitura |
| `watchlist.py` | `add_to_watchlist`, `remove_from_watchlist` | removidos; apenas leitura |
| `JazereiClient` | `write_json`, `require_writes`, `post_json` | removidos; `post_read_json` allowlisted |
| `HttpClient` | `request`/`post_json` genéricos | GET/HEAD + `post_read_json` allowlisted |
| `importer.py` | `execute`/`ImportRefused` | removidos; `preflight` nunca autoriza |
| CLI | sem comando de escrita | sem comando de escrita |
| Gateway | `simulated=True` | `simulated=True` mantido |

**Limitação de Python:** um módulo ainda pode acessar atributos privados (`_session`) ou importar `requests` diretamente. A proteção é arquitetural e por testes, não um sandbox. Ver `docs/security.md`.

## 7. Transporte real implementado na Etapa 9

`BrowserWriteTransport` foi implementado dentro do gateway. O uso real continua desabilitado e nunca foi exercitado.

| Requisito | Estado |
|---|---|
| Sessão autenticada via `context.request` | `ready` (código) / `unknown` (contrato real) |
| Endpoint fixo `/api/reviews/upsert`, método POST | `ready` |
| Sem redirect (`max_redirects=0`) | `ready` |
| Payload exato `{game_id,rating,text,liked}` | `ready` |
| Timeout explícito | `ready` |
| Sem retry de POST | `ready` |
| Sem tokens/cookies em artefatos | `ready` |
| HTTP apenas sinal auxiliar | `ready` |
| Dois gates + confirmação + leitura prévia | `ready` |
| Teste com servidor falso | `ready` |
| Uso real autorizado | `pending_human` |
| Contrato real do backend | `unknown` |

Critérios mínimos originais, agora atendidos no código:

1. Reutilizar a sessão autenticada validada, mantendo cookies/tokens apenas em memória.
2. Permitir somente `https://jazerei.com/api/reviews/upsert`, método `POST`.
3. Rejeitar redirects, hosts alternativos e paths desconhecidos.
4. Enviar exatamente `{game_id, rating, text, liked}` com `liked` booleano explícito.
5. Aplicar timeout e classificar timeout/conexão como resultado desconhecido.
6. Não usar retry automático para POST.
7. Não registrar headers, cookies, tokens ou corpos sensíveis.
8. Validar resposta apenas como sinal auxiliar; persistência exige releitura posterior.
9. Exigir `MIGRATION_ALLOW_WRITES=true` **e** `MIGRATION_ENABLE_REAL_WRITE_TRANSPORT=true`, além de autorização por execução.
10. Rotear toda escrita pelo gateway.
11. Ser injetável apenas pelo gateway/testes.
12. Ter testes locais com sessão falsa para timeout, 4xx/5xx, redirect, conexão e resposta sem corpo.

Riscos principais: regras desconhecidas do backend, idempotência não comprovada, sessão expirada, CSRF/redirect, rate limit, escrita parcial e vazamento de credenciais.

## 8. Matriz de riscos

| Risco | Sev. | Estado | Mitigação |
|---|---|---|---|
| Duplicar review por timeout/retry | Alta | `blocked` | Nenhum retry; ambiguidade exige resolução humana; releitura |
| POST real via helper legado | Alta | `resolved` | Helpers removidos; `allow_writes=True` não reativa; testes arquiteturais |
| Aprovação/preflight obsoleto | Alta | `ready` | Hashes e vínculo exato; execução recusa |
| `liked` automático por nota | Alta | `ready` | Política explícita: ≥7 true, ≤5 false, intermediário unresolved; decisão automática é `source: automatic` e pode ser revista |
| Decisão `liked` humana sobrescrita | Alta | `ready` | Política automática não sobrescreve `source: human` |
| Backend rejeitar payload | Média | `unknown` | Validar localmente; classificar resposta como ambígua; releitura |
| Resposta 2xx sem persistência | Média | `unknown` | Verificação posterior obrigatória |
| Sessão expirada/identidade errada | Média | `ready` | Revalidar identidade antes/depois; bloquear |
| Interrupção no processo | Média | `ready` | `writing`/`verification_pending` → reconcile |
| Corrupção local/adulteração | Média | `blocked` | Manifestos/hashes; append-only lógico; não é imutabilidade física |
| Vazamento de credenciais | Alta | `ready` | Sem cookies/tokens em artefatos; `.env` e perfil ignorados |
| Escopo Stash | Baixa | `ready` | Coleta completa 1.114 jogos/76 reviews; falhas anteriores documentadas |
| `upsert` atualiza review existente | Média | `ready` | Gateway só aceita `before:null`; validação bloqueia existente |

## 9. Recomendações antes da primeira escrita

1. Revisar as 9 correspondências pendentes (6 ambíguas + 4 sem candidato, descontando a já aprovada manualmente) e as 15 decisões `liked` unresolved.
2. Revisar decisões automáticas de `liked` e ajustar as que o usuário discordar, registrando `source: human`.
3. Gerar plano, manifesto e aprovação após correspondências/`liked`; não reutilizar plano antigo.
4. Gerar preflight autenticado e obter `compatible`.
5. Preparar execução e confirmar cada ação individualmente.
6. Habilitar os dois gates somente após revisão e autorização explícita.
7. Executar uma única ação real e verificar por releitura.
8. Revisar o resultado antes de qualquer segunda ação.

## 10. Ferramentas e evidências executadas

- `pytest`: **250 passed** após progresso/cancelamento/retomada (244 antes; 205 na Etapa 9 sem UI).
- `ruff check`: **All checks passed!**
- `ruff format --check`: **75 files already formatted**.
- Coleta completa: 1.114 jogos, 76 reviews, 76 detalhes.
- Correspondências: 66 candidatas, 10 ambíguas, 4 sem candidato, 0 aprovadas.
- Destino: 1 review (RE4), nenhuma sobreposta à origem.
- `migration verify-plan`: `invalid: []`, `stale: []`, `blocked: [liked_unresolved]` no piloto antigo.
- `migration approval-status`: `status: blocked`.
- `migration verify-preflight`: `valid: true`.
- `status`: `allow_writes: false`; transporte real desligado.
- Nenhum arquivo em `data/migration/executions/`; nenhum `approvals.jsonl`; nenhum `liked-decisions.jsonl`.

## 11. Conclusão

**Não pronto para escrita real.** A infraestrutura está encerrada: transporte real implementado e testado apenas com sessão simulada, porém desligado por dois gates. Faltam decisões humanas de correspondência e `liked`, plano/aprovação/preflight finais e autorização explícita.

O bloqueio atual é intencional e deve permanecer até decisão humana.
