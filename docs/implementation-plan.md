# Plano de implementação

Acompanhamento real do desenvolvimento. Uma etapa só é marcada como concluída com evidência de teste.

## Fase 0 — Preparação
**Status: concluída.**
- [x] Consolidar descobertas e criar repositório privado.
- [x] Ambiente virtual, lint e testes (`pytest`, `ruff`).
- [x] Modelos/schemas versionados com Pydantic.
- Evidência: suíte inicial 31 testes; `ruff check` e `ruff format --check` limpos.

## Fase 1 — Exportador Stash (somente leitura)
**Status: coleta browser + dirigida concluídas; HTTP ainda bloqueado por Cloudflare.**
- [x] Cliente HTTP com timeout/retry/User-Agent/politeness.
- [x] Paginação dinâmica por `nav.profile-pagination`.
- [x] Exportar jogos e reviews.
- [x] Comparar texto de listagem e review individual; divergências registradas.
- [x] Gerar `games.json`, `reviews.json` e `export-report.json`.
- [x] Coleta via browser com os mesmos modelos/arquivos.
- [x] Coleta dirigida por página explícita e slug.
- [x] Runs versionados, manifesto SHA-256, exit codes e proteção contra sobrescrita.
- [ ] Validar contagens completas HTTP (bloqueado: HTTP 403).
- Evidência real: browser headful leu 30 jogos e 6 reviews (página 1), 6 detalhes, 2 divergências; 38/13 páginas detectadas.
- Evidência dirigida: `pilot-demolition-girl-20261124-v4` — página 9 de jogos e 4 de reviews, 1+1 registros, outcome `directed`, exit 0, manifesto íntegro, dados iguais aos artefatos antigos.

## Etapa 1 — Integridade e reprodutibilidade da coleta
**Status: concluída e validada com fixtures e dados reais.**
- [x] `export --slug/--game-page/--review-page`.
- [x] Páginas explícitas sem quebrar paginação dinâmica.
- [x] Falhas de HTTP, browser e parsing registradas.
- [x] Distinção `complete`/`directed`/`empty`/`partial`/`target_not_found`/`failed`.
- [x] Run dirs com schema, run ID, report, manifesto e SHA-256.
- [x] Sem sobrescrita por padrão; `--overwrite` explícito.
- [x] Staging evita substituir run válido por falha.
- [x] Exit codes 0/1/2.
- Evidência: 70 testes na Etapa 1; execução real v4; tentativa Cloudflare gerou `failed` sem dados e preservou artefatos anteriores.

## Etapa 2 — Estado autenticado do Já Zerei
**Status: concluída e validada com sessão real (somente leitura).**
- [x] `DestinationReview` com `user_id`, `liked`, `likes_count`, `comments_count` e campos extras.
- [x] Validar `user_id` na query e no corpo; rejeitar usuário inesperado.
- [x] Paginar todas as respostas com `Range`/`Content-Range`; não usar a primeira não vazia.
- [x] Snapshot vazio válido com `exists:false`, `query`, `captured_at`, `response_hash`, `complete`.
- [x] Comando `browser review-state` por `game_id`/slug.
- [x] Usar sessão Playwright; identidade via IndexedDB + `get_my_profile`; sem token em `.env`.
- [x] Testes de identidade, vazio, múltiplas páginas, duplicatas, erro e captura incompleta.
- [x] Sanitização de URLs/campos sensíveis antes de persistir (token de avatar removido).
- Evidência real:
  - 26570 (`demolition-girl`) → `exists:false`, 1 página, 1 review recebida, 0 duplicatas, hash registrado.
  - 974 (`resident-evil-4`) → `exists:true`, review completa, `liked:false`, texto vazio e campos extras preservados.
  - Ambos com `identity_verified:true` e manifesto íntegro.
- Limitação original: identidade esperada exigia `JAZEREI_USER_ID`; atualmente o login preenche `data/browser/session-identity.json` e o snapshot ainda não é consumido automaticamente pelo planner.

## Etapa 2 — proposta técnica (implementada)

### 4.1 Identidade

- Adicionar `user_id` a `DestinationReview` e aos snapshots de estado.
- Validar `user_id` na query observada e no corpo de cada review.
- Abortar com erro explícito se a resposta contiver usuário diferente do autenticado.
- Registrar o UUID consultado, sem nome, e-mail ou avatar.
- Nunca inferir identidade apenas por “a resposta veio da sessão”.

### 4.2 Paginação

- Capturar todas as respostas `/rest/v1/reviews` da página/consulta, não a primeira não vazia.
- Deduplicar por `id` de review.
- Registrar páginas/limites, contagens recebidas, critério de encerramento e se a coleta está completa.
- Uma resposta vazia só é estado válido quando a consulta terminou com sucesso e sem páginas pendentes.

### 4.3 Estado vazio e proveniência

Snapshot mínimo:

```json
{
  "schema_version": 1,
  "game_id": 26570,
  "game_slug": "demolition-girl",
  "user_id": "...",
  "exists": false,
  "query": "select=*&user_id=eq....&game_id=eq.26570",
  "source": "authenticated_browser_network",
  "captured_at": "...",
  "pages_captured": 1,
  "complete": true,
  "response_hash": "..."
}
```

Se `exists: true`, incluir a review completa, `liked`, contagens e hash. Ausência de arquivo não pode significar ausência de review.

### 4.4 CLI implementada

```powershell
python -m src.cli browser review-state --game-id 26570 --slug demolition-girl --page-size 50 --headless
```

Grava snapshot vazio válido ou com review, mostra contagens/hash e nunca realiza escrita no site.

### 4.5 Autenticação Playwright

- Identidade local lida do IndexedDB do Firebase (apenas `uid`).
- Identidade do servidor lida da resposta de `get_my_profile` (apenas `id`).
- Headers da própria aplicação são capturados em memória para o GET paginado via `context.request`; nenhum header é persistido.
- Evitar copiar tokens para `.env` ou logs; não exportar credenciais.
- Validar que a resposta é do usuário esperado antes de aceitar.
- Preservar login manual e perfil persistente.
- Testes com fixtures de respostas vazias, mistas, paginadas e 401.

### Critérios de conclusão da Etapa 2

1. `DestinationReview` preserva identidade, `liked` e contagens.
2. Usuário divergente é rejeitado.
3. Snapshot vazio é gerado via CLI com hash e completude.
4. Paginação completa é comprovada por contagens/fixtures.
5. Nenhuma escrita no Já Zerei.

## Etapa 3 — Contrato e política de `liked`
**Status: concluída (investigação + política + decisões), sem escrita.**
- [x] Significado documentado: recomendação do autor, não curtida.
- [x] Contrato do backend investigado; `true`/`false` usados pelo frontend, omissão/`null`/default desconhecidos.
- [x] Equivalência com Stash investigada: nenhum campo confirmado.
- [x] Estado `unresolved` explícito; sem default `false`.
- [x] Decisões humanas `true`/`false` append-only com `source_hash`, `game_id`, `decided_at`, `decision_id` e `supersedes`.
- [x] Decisão desatualizada (`stale`) e jogo divergente não são reutilizados.
- [x] Review existente no destino é preservada; nenhuma atualização de review é proposta.
- [x] Planner bloqueia `liked_unresolved`/`liked_decision_stale`; valida valor decidido.
- [x] CLI `migration liked decide/list/show`.
- Evidência: 105 testes; decisões testadas com fixtures; nenhuma escrita no Já Zerei.

## Etapa 4 — Aprovação auditável
**Status: concluída (autorização documental; nenhuma execução).**
- [x] Plano gravado em `data/migration/plans/<plan-id>/` com `plan.json` + `manifest.json`.
- [x] Hash canônico determinístico de proveniência + ações.
- [x] Manifesto com origem, destino, matching, decisões, ações e hashes.
- [x] Aprovação append-only vinculada a `plan_id`, `manifest_sha256`, `plan_sha256` e IDs das ações.
- [x] Confirmação humana explícita (`--confirm <plan-id>`).
- [x] Aprovação bloqueada por proveniência incompleta, manifesto inválido ou validação estrutural.
- [x] Status `approved`/`unapproved`/`stale`/`invalid`/`blocked`.
- [x] Plano legado `approved:true` não autoriza nada.
- [x] Cenário Demolition Girl: plano real bloqueado por `liked_unresolved`; aprovação recusada.
- Evidência: 113 testes; manifestos e aprovações com hashes; Ruff limpo; nenhuma escrita no destino.

## Etapa 5 — Preflight rigoroso
**Status: concluída (somente leitura e comparação).**
- [x] Pré-condições: plano/manifesto/proveniência, decisões, validação, aprovação atual e identidade.
- [x] Releitura autenticada via sessão Playwright, sem reutilizar snapshot antigo.
- [x] Uma captura de reviews reutilizada para todos os jogos do plano.
- [x] Comparação semântica `before` × atual (`rating`, `text`, `liked`, existência, IDs).
- [x] Detecção de review criada/removida/modificada e campos alterados.
- [x] Captura incompleta/identidade divergente nunca viram ausência.
- [x] Ações Wishlist/favoritos/Playing Now classificadas como `unsupported` e bloqueadas.
- [x] Resultado por ação e global (`compatible/conflict/blocked/inconclusive/invalid`).
- [x] Persistência em `data/migration/preflights/<preflight-id>/` com snapshots e manifesto.
- [x] CLI `preflight`, `preflight-show`, `verify-preflight`.
- Evidência: 123 testes; preflight real do plano bloqueado recusado sem abrir o navegador; fixtures cobrem compatível/conflito/inconclusivo; Ruff limpo.

## Etapa 6 — Gateway de escrita
**Status: infraestrutura concluída; execução real bloqueada (nenhuma escrita).**
- [x] Preparação de execução com plano/aprovação/preflight/identidade/payload.
- [x] Somente criação de review; update/delete/Wishlist/favoritos/Playing Now bloqueados.
- [x] Payload estrito `{game_id,rating,text,liked}`, `liked` booleano explícito.
- [x] Confirmação individual append-only vinculada a todos os hashes/IDs.
- [x] Leitura imediatamente anterior à escrita (simulada).
- [x] Transporte injetado e explicitamente simulado; transporte real recusado.
- [x] Idempotência local; ação verificada não repete; ambiguidade não reenvia.
- [x] Timeout pós-envio → `write_ambiguous`/`inconclusive`.
- [x] Verificação posterior por releitura; resposta HTTP não basta.
- [x] Persistência em `data/migration/executions/<id>/` com eventos/manifesto.
- [x] CLI `execution-prepare/show/validate/confirm/status/verify/reconcile/resolve`; sem comando de execução real.
- [x] Escrita interrompida é reconciliada como ambígua; resolução exige snapshot autenticado/conclusivo e nunca reenvia.
- [x] Verificação posterior valida identidade, dono, jogo e todos os campos do payload.
- [x] Timeout, conexão interrompida, HTTP 4xx/5xx, identidade divergente, captura incompleta e review ausente após envio → `write_ambiguous`; somente falha comprovadamente anterior ao envio é `failed`.
- [x] Nenhuma ação subsequente é executada enquanto houver ação ambígua.
- [x] Resolução de ambiguidade mantém ausência como ambígua e nunca reenvia.
- Evidência: 165 testes com transporte falso; Ruff limpo; nenhuma escrita no destino.

## Etapa 7 — Auditoria operacional e prontidão
**Status: auditoria concluída; escrita real `blocked` (nenhuma escrita).**
- [x] Auditoria do repositório, Git, dependências, CLI e artefatos sensíveis.
- [x] Cadeia de proveniência documentada com IDs/hashes e invalidação em cascata.
- [x] Piloto Demolition Girl avaliado sem alteração; `liked_unresolved` preservado.
- [x] Contrato `POST /api/reviews/upsert` classificado em fato/hipótese/desconhecido.
- [x] Gateway revisado; helpers legados de escrita identificados como risco fora do gateway.
- [x] Procedimento operacional e matriz de recuperação documentados.
- [x] Requisitos de um transporte real avaliados, sem implementação.
- [x] Relatório de prontidão com estados `ready/blocked/pending_human/unknown/not_applicable`.
- [x] Testes de superfície de escrita: helpers legados bloqueados, POST fora de retry, CLI sem comando de escrita.
- Evidência: `docs/readiness.md`, `docs/operations-first-write.md`; 169 testes; Ruff limpo; nenhuma escrita.

## Etapa 8 — Remoção da superfície legada de escrita
**Status: concluída; escrita real continua bloqueada.**
- [x] `upsert_review`, `update_profile`, `add_to_watchlist` e `remove_from_watchlist` removidos.
- [x] `JazereiClient` sem `write_json`/`require_writes`/método HTTP genérico.
- [x] `HttpClient` somente leitura: GET/HEAD + POST de consulta allowlisted; métodos mutáveis recusados.
- [x] `importer.execute` removido; não há segunda camada de execução.
- [x] `allow_writes=True` não reativa helpers nem concede transporte.
- [x] Testes arquiteturais: ausência de símbolos, endpoints mutáveis recusados, leituras preservadas.
- Evidência: 179 testes; Ruff limpo; nenhuma escrita.

## Etapa 9 — Encerramento da infraestrutura e transporte real
**Status: concluída; nenhuma escrita real.**
- [x] Coleta completa via navegador: 38 páginas de jogos (1.114), 13 de reviews (76), 76 detalhes.
- [x] Correspondências para 76 reviews: 66 candidatos, 10 para revisão manual, 4 sem candidato.
- [x] Relatório de decisões `liked` pendentes gerado; nenhuma decisão inventada.
- [x] `BrowserWriteTransport` exclusivo do gateway: endpoint fixo, POST, sessão persistente, sem redirect/retry.
- [x] Dois gates independentes: `MIGRATION_ALLOW_WRITES` + `MIGRATION_ENABLE_REAL_WRITE_TRANSPORT`, ambos falsos por padrão.
- [x] CLI `execution-run` implementada e bloqueada sem os dois gates e confirmação exata.
- [x] Testes com sessão simulada cobrem endpoints, payload, timeout, 4xx/5xx, redirect, 2xx sem review, divergência e inconclusivo.
- [x] Interface local `review-ui` para revisar os 76 jogos, candidatos e `liked`, com persistência e histórico.
- [x] Ratificação humana em lote append-only; decisões automáticas cobertas por conjunto fechado e hash.
- [x] `verify-plan`/`approval-status` distinguem individual, lote e automático não revisado.
- Evidência: 250 testes; Ruff limpo; coleta completa `stage9-full-stash-browser-v2-20261125`; destino com 1 review (RE4); nenhuma escrita.

## Etapa 10 — Expansão
**Status: não implementada; fora do escopo de reviews.**
- Wishlist, favoritos, Playing Now, limites e relatório de perdas.

## Fase 2 — Já Zerei (leitura)
**Status: leitura pública e autenticada por jogo validadas; snapshot por jogo concluído.**
- [x] Autenticação sem expor credenciais (código).
- [x] Busca pública do catálogo por HTTP e browser, resultados idênticos.
- [x] Detalhe público do jogo via DOM/JSON-LD (`/jogos/{slug}`).
- [x] Estrutura de leitura de reviews/Wishlist/perfil (API same-origin).
- [x] Política de rede que só permite leitura.
- [x] Validar login manual real e leitura autenticada.
- [x] Snapshot autenticado completo com identidade/paginação/proveniência.
- [ ] Wishlist e perfil autenticados pelo novo mecanismo.
- Evidência: Sonic Unleashed → ID 5169 em ambos os métodos; login Google confirmado com `logged_in: true` e IndexedDB; `browser reviews` capturou 1 review autenticada; `review-state` validou 26570 ausente e 974 existente.

## Fase 3 — Normalização/correspondência
**Status: concluída e testada offline.**
- [x] Normalizar nomes, slugs, datas, notas e tokens de edição.
- [x] Candidatos com confiança, critérios e alternativas.
- [x] Empates/ambiguidades → `manual_review`; nada aprovado automaticamente.
- Evidência: testes de matcher, incluindo ambiguidade e penalidade de remake/port.

## Fase 4 — Plano/aprovação
**Status: ensaio real executado; plano bloqueado por aprovação e `liked`.**
- [x] Detectar review existente e conflito (`before`).
- [x] Reviews, Wishlist, favoritos e Playing Now; limites 5/10.
- [x] Excedentes e duplicatas em `skipped`; não truncar silenciosamente.
- [x] Aprovação programática explícita, inclusive conflitos.
- [x] Ensaio Demolition Girl: Stash página 9, review página 4, destino 26570.
- [x] Sem review existente no destino; `before: null`.
- [x] Plano de 1 ação gerado e validado; nenhuma escrita.
- [ ] Comando CLI dedicado de aprovação (próxima ação).
- [ ] Resolver semântica de `liked` antes de qualquer aprovação.
- Evidência: artefatos em `data/migration/demolition-girl/`; validação com `plan_not_approved`, `action_not_approved`, `liked_semantics_pending`; preflight `allowed: false`.

## Fase 5 — Importação controlada
**Status: bloqueada por decisão de escopo.**
- [x] Preflight e validação do plano.
- [x] Escritas bloqueadas por `MIGRATION_ALLOW_WRITES`; helpers legados removidos na Etapa 8.
- [ ] Implementar reviews, Wishlist e perfil (não executar nesta tarefa).
- [ ] Checkpoints/retomada e remoção.
- Regra mantida: nenhuma escrita no Já Zerei nesta entrega.

## Fase 6 — Verificação
**Status: implementada para reviews; teste real pendente.**
- [x] Comparar plano aprovado com estado relido.
- [x] Relatório de confirmados, ausentes e divergentes.
- [ ] Verificação pós-escrita real (depende da Fase 5).

## Automação de navegador (tarefa atual)
**Status: implementada e validada em ambiente real.**
- [x] Playwright + Chromium; dependência opcional e install documentado.
- [x] Perfil persistente exclusivo, ignorado pelo Git.
- [x] Comandos `browser open/login/status/close/reviews/detail`.
- [x] Política read-only por método, host, endpoint e finalidade.
- [x] Coleta Stash via browser reutilizando parsers/coletores.
- [x] Busca/detalhe do catálogo e captura de respostas same-origin.
- [x] Testes de sessão/cookie/localStorage, políticas, parsing e compatibilidade.
- Evidência: suíte atual com 179 testes; sessão persistiu após fechar/reabrir; login Google real confirmado; `browser status --verify` mostrou `logged_in: true` sem requisições bloqueadas; `browser reviews` capturou 1 review.

## Testes e entrega
**Status: concluída para o escopo permitido.**
- [x] pytest, Ruff check e Ruff format.
- [x] Diagnóstico CLI (`status`, `browser status`, `--verify`).
- [x] Verificação de Git e dados sensíveis.
- [x] Ensaio de um jogo (Demolition Girl; plano + validação, sem escrita).
- [ ] Commits (não autorizados).

## Próximas ações
1. Definir e validar a semântica de `liked` por endpoint.
2. Implementar aprovação CLI explícita do plano/ação.
3. Revisar o plano `data/migration/demolition-girl/plan.json` com o usuário.
4. Futuro: Fase 5 de escrita real, somente com autorização expressa.
5. Repetir o fluxo com mais jogos depois de validar o piloto.
