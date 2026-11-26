# Interface local de revisão de correspondências

**Status:** fluxo em três etapas e orquestrador de preparação implementados em 2026-11-26 (Etapa 7). Revisão, ratificação em lote, wizard anterior e endurecimento causal da Etapa 5 preservados no backend.  
**Dados reutilizáveis:** `stage9-full-stash-browser-v2-20261125` + `stage9-account-snapshot-20261125`; seleção antiga não é retomada automaticamente.

## Fluxo atual em três etapas

A navegação principal tem **Preparar**, **Revisar e executar**, **Conferir resultado** e **Avançado**. Procedimentos internos aparecem como atividades dentro da etapa, não como passos de navegação.

- **Preparar:** um CTA executa somente o que falta — sessão do Já Zerei, coleta válida, snapshot completo e mais novo, correspondências ausentes, política automática (após confirmação explícita), plano novo quando o atual está inválido/stale e validação final. O job é `prepare_all`; eventos ficam em `data/migration/prepare-runs/<run_id>/events.jsonl`.
- **Revisar e executar:** uma única superfície reúne revisão, decisões de `liked`, conflitos, ratificação em lote das decisões automáticas, aprovação do plano, preflight e criação da execução. O CTA `approve-and-prepare` encadeia aprovação + preflight + preparação, sem escrever. A escrita continua em um segundo CTA com confirmação por `plan_id`.
- **Conferir resultado:** um CTA `verify_destination` captura o destino e mostra confirmações, divergências, ambiguidades e conflitos em uma única superfície. A captura é somente leitura.
- **Avançado:** gates, identidade/perfil, logs da preparação, planos, execuções, verificações, snapshots, hashes e reconciliação manual. Nada é promovido a estado atual por abrir a tela.

O startup não seleciona plano nem execução histórica. `wizard-state.json` é descartável e não autoriza escrita. Execuções antigas continuam no disco; qualquer `write_ambiguous` não reconciliada bloqueia nova preparação de execução.

## Arquitetura

Servidor HTTP local em Python stdlib (`http.server`), sem framework e sem dependência nova. A UI é HTML/CSS/JS estático; a lógica de decisão fica em `src/review_ui.py` e reutiliza:

- `matches.json` como fonte canônica do planner;
- `src.migration.liked.append_decision`/`resolve_liked` para `liked`;
- `build_plan`/`build_bundle` para gerar a prévia de plano;
- `src.jazerei.catalog.search_games` para pesquisa somente-leitura no catálogo.

A interface não cria transporte próprio: execução usa o gateway existente e `BrowserWriteTransport`, condicionado aos dois gates de ambiente.

## Iniciar

```powershell
python -m src.cli review-ui --port 8765 --open
```

Sem `--open`, abra `http://127.0.0.1:8765/`.

## Navegação

- Cabeçalho compacto: identidade, plano/execução ativos e selo de escrita bloqueada/autorizada.
- Etapa 1: atividades agrupadas com status, um CTA principal e pausa explícita para login ou política.
- Etapa 2: painel do plano + workspace de revisão. Filtros principais: pendências, conflitos, ambíguas, sem candidato, aprovadas, sem correspondência, já existente e decisões automáticas a ratificar.
- Etapa 3: resumo do relatório, tabela por ação com filtros e detalhes recolhíveis.
- Avançado: histórico e diagnóstico, sem controles duplicados na navegação principal.
- Lista na ordem estável `S9-001` a `S9-076`.
- Tela individual: origem, texto integral, divergência listagem/detalhe, candidatos, pesquisa manual, `liked`, histórico e navegação anterior/próximo.

## Política automática

Quando o usuário confirma a aplicação explícita (prévia em `GET /api/policy/preview`, aplicação em `POST /api/policy`):

- `strong_candidate` sem histórico humano é aprovado automaticamente;
- `liked` é preenchido pela nota da review, somente quando há destino definido:
  - nota **≥ 7**: `true`;
  - nota **≤ 5**: `false`;
  - **5 < nota < 7**: permanece `unresolved` (inclui 5.5, 6.0 e 6.5).
- decisões humanas existentes não são sobrescritas;
- itens com histórico (aprovação, rejeição, ausência ou pendência) não são re-aprovados automaticamente;
- `already_exists` não recebe `liked` automático.

A prévia não grava nada; `matches.json` e `liked-decisions.jsonl` só mudam depois da confirmação. O startup da interface é somente leitura, para não invalidar um plano aprovado.

Decisões automáticas de `liked` usam `source: automatic` no arquivo append-only; decisões feitas na interface usam `source: human`.

Aplicação em 2026-11-25: 64 aprovações automáticas adicionais (66 `strong_candidate` no total), 59 `liked` automáticos, 2 decisões humanas preservadas, 15 `unresolved` (nota entre 5 e 7 ou sem candidato).

## Categorias especiais

O parser agora lê `.games-list__category` do card Stash (`Remaster`, `Mod`, `Expanded game`, `Port`, `Bundle`, etc.). O dado é preservado em `GameRecord.category` e em `source-categories.json` para o conjunto atual.

Regra da interface:

- categoria preenchida → `special_category`;
- sem candidato → sugestão **marcar sem correspondência/não migrar** (decisão humana);
- com candidato → revisão manual, sem autoaprovação;
- a categoria nunca é inferida pelo título;
- “remover” significa não migrar, nunca apagar a review ou o jogo de origem.

## Edição de review

A interface permite editar, por review:

- nota;
- texto;
- status;
- progresso.

O original continua visível. As edições são append-only em `data/migration/stage9/review-overrides.jsonl`, com hash da review de origem, valores anteriores e novos. O arquivo original do Stash nunca é alterado.

A prévia de plano usa os valores efetivos e vincula o hash de `review-overrides.jsonl` e `source-categories.json` ao manifesto do plano.

## Decisões

- **Aprovar**: grava `decision=approved` e o candidato em `matches.json`.
- **Rejeitar**: registra o candidato específico no histórico; não rejeita os demais.
- **Manual**: valida a seleção pelo catálogo (somente leitura), grava `approved` com consulta/`game_id` e evidência.
- **Sem correspondência**: `manual_review` + histórico `no_match`; distinto de rejeição.
- **Pendente**: mantém o item para retomada.
- **Liked**: `true`/`false` via `append_decision`, com `replace=True`; nunca inferido.

Persistência:

- `data/migration/matches.json` — fonte usada por `build_plan`;
- `data/migration/liked-decisions.jsonl` — decisões `liked` append-only;
- `data/migration/stage9/match-decisions.jsonl` - histórico append-only das decisões da UI, com hash da origem, manifesto de origem/destino, hashes de `matches.json` antes/depois e timestamp;
- `data/migration/stage9/review-overrides.jsonl` - edições append-only de nota/texto/status/progresso;
- `data/migration/stage9/source-categories.json` - categorias Stash coletadas para o conjunto atual.

Se a origem ou o manifesto do destino mudar, a decisão é marcada como `stale` na interface e deve ser refeita. `liked` vinculado a outro `game_id` também aparece como stale.

## Ratificação humana em lote

Decisões automáticas não viram humanas: elas permanecem registradas como automáticas. A ratificação é um evento append-only separado em `data/migration/stage9/ratifications.jsonl`, com:

- `ratification_id`;
- dimensão `matches` ou `liked`;
- conjunto fechado de itens;
- `items_sha256`;
- plano/manifestos e hashes de origem/destino;
- `reviewer: human`, `review_type: human_batch_review`;
- resultado `confirmed_existing`.

A interface mostra `human_individual`, `human_batch_review` e `automatic_unreviewed`. O gerador de plano inclui correspondências humanas individuais ou ratificadas e `liked` humano ou ratificado; decisões automáticas sem ratificação ficam fora.

Na Etapa 2, a UI mostra quantos itens automáticos faltam ratificar e oferece **Revisar e ratificar N decisão(ões)**. O endpoint `POST /api/migration/ratify` exige `plan_id` e `confirm` iguais, grava os eventos append-only e regenera o plano. A ratificação não aprova o plano, não altera `matches.json`/`liked-decisions.jsonl` e não escreve no destino.

O `verify-plan` e `approval-status` exibem contagem por modalidade:

```json
{"matches":{"human_individual":9,"human_batch_review":61,"automatic_unreviewed":0},
 "liked":{"human_individual":14,"human_batch_review":56,"automatic_unreviewed":0}}
```

Uma ratificação não é aprovação do plano nem autorização de escrita.

## Aceitação editorial

Alertas editoriais objetivos — como divergência de título/ano entre edição de origem e jogo-base — podem ser aceitos explicitamente em `data/migration/stage9/editorial-acceptances.jsonl`. O evento é append-only, `event_type: editorial_acceptance`, decisão `keep_association`, com plano, ação, origem/destino, motivo, hashes e identidade `human`.

Aceitação editorial não é aprovação de correspondência nem aprovação formal do plano.

## Painel de migração

A orquestração substitui o rail de oito passos. As operações técnicas continuam as mesmas:

1. **Sessão do Já Zerei** — verificação headless; se necessário, login manual com o navegador aberto. Pausa o job até confirmação.
2. **Coleta do Stash** — reaproveita run válido; coleta nova só quando ausente/inválida.
3. **Snapshot do destino** — reaproveita snapshot completo e mais novo que a coleta; captura nova quando necessário.
4. **Correspondências** — adiciona apenas slugs ausentes; decisões existentes nunca são sobrescritas.
5. **Política automática** — prévia e aplicação somente após confirmação explícita.
6. **Plano** — gera novo plano quando não há plano válido; planos válidos são reutilizados.
7. **Validação final** — exige plano existente, não inválido/stale e com proveniência completa.

Aprovação, preflight e criação da execução ficam na Etapa 2 porque o preflight atual exige plano aprovado. A execução real continua condicionada aos dois gates e à confirmação separada por `plan_id`.

O login manual continua sendo do usuário. A UI abre o Chromium, descobre o UID e grava apenas `data/browser/session-identity.json`. `JAZEREI_USER_ID` continua tendo precedência. O arquivo não contém credenciais.

Os botões usam os serviços existentes:

- `approve_plan`;
- `cmd_migration_preflight`;
- `prepare_execution`/`confirm_action`;
- `execute_action` com `BrowserWriteTransport`;
- `capture_review_state` para leitura anterior/posterior.

A execução real continua bloqueada se `MIGRATION_ALLOW_WRITES` ou `MIGRATION_ENABLE_REAL_WRITE_TRANSPORT` estiverem falsos. A interface não cria um segundo transporte. Ambiguidade interrompe o lote e exige reconciliação.

Durante a execução, a UI consulta `/api/migration/progress` a cada segundo e mostra:

- ação atual, `game_id`, status e motivo;
- barra de progresso e contagem de verificadas/falhas/conflitos/ambíguas/restantes;
- botão **Cancelar execução**, que para após a ação atual e cancela as ações ainda não iniciadas;
- painel de erro com código, ação, execução, orientação e detalhes técnicos.

Execuções interrompidas por falha transitória anterior ao envio (por exemplo, navegador fechado durante a leitura) podem ser retomadas pelo botão **Retomar execução**. A retomada não reenvia ações `verified` nem ações `write_ambiguous`; apenas falhas seguras de pré-escrita voltam para `ready`. Ações ambíguas ou em conflito exigem `execution-reconcile`/`execution-resolve` antes.

O fechamento manual do navegador é tratado como erro estruturado: a execução é reconciliada e a UI mostra `browser_closed` com instrução para revisar antes de retomar ou criar nova execução.

## Plano

O botão **Gerar plano consolidado (prévia)** usa `build_plan`/`build_bundle` com os valores efetivos. Ele:

- não aprova nada;
- não cria aprovação;
- não executa preflight;
- não escreve no Já Zerei;
- usa o snapshot completo da conta quando existir (`AccountReviewsSnapshot`), tornando a proveniência do destino completa;
- sem snapshot completo, usa a proveniência legada e marca o plano como incompleto.

Snapshot completo pode ser criado com:

```powershell
python -m src.cli browser account-reviews --run-id <run-id>
```

O snapshot referencia todas as reviews autenticadas da conta em uma única execução e permite verificar ausência/presença por `game_id`. A aprovação/preflight continuam obrigatórios.

## Preparação orquestrada e operações longas

A interface abre em modo somente leitura. `run_review_ui` não aplica política no startup, não captura destino e não cria planos. A varredura `scan()` é somente leitura e decide o que falta.

Seleções visuais ficam em `data/migration/wizard-state.json` (atômico) e guardam apenas IDs de plano, execução, coleta, snapshot, a última verificação e a última checagem de identidade. O arquivo não contém cookies, tokens ou senhas. Nenhuma seleção cria plano, preflight ou execução. O startup não promove IDs históricos a estado atual.

O fluxo principal tem três etapas:

- **Preparar:** orquestra sessão, coleta, snapshot, correspondências, política confirmada, plano e validação.
- **Revisar e executar:** decisões humanas, ratificação em lote, aprovação, preflight, preparação e execução confirmada.
- **Conferir resultado:** captura autenticada, relatório por ação, histórico de verificações e distinção entre presença, conteúdo e atribuição causal.

### Jobs

`JobRunner` executa uma operação por vez e mantém um lock global no perfil Chromium. Jobs disponíveis:

- `prepare_all`
- `approve_and_prepare`
- `stash_access`
- `login_jazerei`
- `verify_jazerei`
- `collect_stash`
- `snapshot_destination`
- `matches_ensure`
- `preflight`
- `resolve_ambiguity`
- `verify_destination`

Endpoints principais: `GET /api/wizard`, `GET /api/prepare`, `POST /api/prepare/start`, `POST /api/migration/ratify`, `POST /api/migration/approve-and-prepare`, `POST /api/wizard/select`, `GET /api/policy/preview`, `POST /api/policy`, `POST /api/jobs`, `GET /api/jobs/current`, `POST /api/jobs/cancel`, `GET /api/executions`, `GET /api/executions/{id}` e `POST /api/executions/{id}/reconcile`.

### Fase B operacional

`execution_detail` reutiliza `load_execution`, `load_events`, `load_confirmations` e `verify_execution`. A resposta separa:

- resumo: estado, datas, contagens, gates, último bloqueio e controles derivados pelo backend;
- ações: jogo, `game_id`, estado, nota, `liked`, payload, conteúdo planejado, `write_result`, verificação, motivo e erro;
- situações especiais: falhas transitórias, ambiguidades, conflitos e escritas interrompidas;
- histórico: confirmações, eventos, hashes, proveniência e erros de integridade.

`GET /api/executions` lista todas as execuções, de todos os planos. Selecionar uma execução antiga apenas muda a visualização e o plano associado; os controles continuam derivados do estado persistido. Execuções `inconclusive` não retomáveis permanecem sem botão de retomada.

### Reconciliação e resolução

- `POST /api/executions/{id}/reconcile` chama `reconcile_execution`; é local, sem navegador e sem POST. Escritas em `writing`/`verification_pending` viram `write_ambiguous`.
- O job `resolve_ambiguity` captura `capture_review_state` com a sessão autenticada, grava um snapshot versionado em `data/jazerei/runs/` e chama `resolve_ambiguous_action`.
- A resolução nunca cria review. Se a review existe e os campos conferem, a ação pode virar `verified`; se existe e diverge, vira `conflict`; se está ausente ou a evidência é inconclusiva, permanece `write_ambiguous` e o job explica o motivo.
- O `write_result` original (por exemplo, HTTP 401) é preservado como evidência.
- Nenhum retry automático é executado.

### Cancelamento e preservação

- Coleta e snapshot usam staging e só publicam no fim.
- Cancelar coleta/snapshot remove apenas o staging; o run anterior não é alterado.
- Cancelar login/Stash fecha o Chromium e preserva o perfil.
- Cancelar preflight pode gravar evidência inconclusiva, mas não prepara execução.
- A navegação entre passos não cancela operações; o botão de cancelamento é separado.
- Mudar coleta/snapshot é bloqueado quando existe execução preparada, em andamento ou retomável.

### Correspondências novas

`matches_ensure` adiciona entradas apenas para slugs ausentes em `matches.json`. Entradas e decisões existentes são preservadas. A consulta ao catálogo pode falhar item a item; nesse caso o item fica em revisão manual. `cmd_match` não é exposto pela interface porque sobrescreve o arquivo inteiro.

### Fase C — verificação final

O job `verify_destination` faz uma nova captura autenticada da conta, grava um snapshot versionado e persiste um relatório append-only em `data/migration/verifications/<verification_id>/`.

O relatório correlaciona plano, execução e snapshot e mantém três dimensões separadas:

- **execução:** `verified`, `ready`, `failed`, `write_ambiguous`, `conflict`, `cancelled`, entre outros;
- **presença no destino:** encontrada, ausente, duplicada, divergente ou inconclusiva;
- **conteúdo:** rating, texto, `liked`, slug e usuário, com resultado integral, parcial, divergente, não verificável ou não comparado.

Uma ação só é **confirmada** quando:

1. a captura está completa;
2. a identidade do snapshot confere com a execução;
3. a review existe uma única vez no destino;
4. rating, texto, `liked` (quando planejado) e identificação do jogo/usuário conferem;
5. o snapshot contém a mesma review observada pela verificação da execução;
6. a leitura anterior ou o snapshot pré-execução mostram ausência da review.

Presença no destino não é tratada como prova de autoria. Uma review encontrada sem esse vínculo aparece como “encontrada sem atribuição”. Se `liked` não estiver disponível no snapshot, o relatório marca “não verificável” e não infere valor.

Relatórios e snapshots anteriores não são sobrescritos. A Fase C reconstrói a lista a partir dos artefatos persistidos; abrir a interface não captura nem gera relatórios.

Endpoints:

- `POST /api/jobs` com `kind=verify_destination`;
- `GET /api/verifications`;
- `GET /api/verifications/{id}`;
- `GET /api/report?verification_id=...` ou `?plan_id=...&execution_id=...`.

### Endurecimento da causalidade e integridade

A Fase C agora valida cada evidência antes de aceitar ausência anterior:

- `before_read` precisa ser um `ReviewStateSnapshot` válido;
- `capture.complete` e `pagination.complete` precisam ser verdadeiros;
- identidade verificada e igual ao UID esperado;
- `query.game_id` e `query.slug` precisam coincidir com a ação;
- `result.exists` precisa ser compatível com `result.review`;
- `response_hash` precisa ser recalculado e conferir.

Campo ausente, `null`, objeto vazio, estrutura inválida ou hash divergente não provam ausência. Nesses casos a presença anterior é `unverifiable` e a causalidade fica inconclusiva.

Snapshots de conta também passam por validação semântica independente. O hash é recalculado com o mesmo algoritmo usado na captura. Integridade do arquivo, hash do manifesto e validade semântica são dimensões distintas.

Se `verify_bundle` indicar plano inválido, stale ou proveniência incompleta, nenhuma ação pode ser `confirmed`, mesmo que presença e conteúdo estejam corretos. O relatório mantém as evidências e marca `confirmation_blocked_by_plan`.

### Estados independentes

O relatório separa:

- `capture_status` — completude/integridade da captura;
- `report_status` — processamento do relatório;
- `migration_status` — execução da migração (`completed`, `incomplete`, `inconclusive`, `failed`, `cancelled`);
- `causal_confirmation_status` — nenhuma, parcial ou todas as confirmações causais.

`status` legado é mantido para compatibilidade, mas a interface não o usa como selo de migração concluída.

### Operações somente leitura

`can_resolve` não depende dos gates de escrita. A resolução de ambiguidade continua disponível com gates desligados; `can_resume` e `can_execute` continuam bloqueados. Os motivos de bloqueio são específicos por controle em `controls.reasons`.

### Limitações da atribuição causal

O vínculo forte usa o ID da review observada na verificação pós-escrita da execução e a ausência anterior (leitura prévia ou snapshot do preflight). Se esses dados não existirem, a review pode estar correta e ainda assim não ser atribuída à execução. O relatório nunca converte essa dúvida em confirmação.

## Limitações

- A interface é local e sem autenticação; use apenas em `127.0.0.1`.
- O snapshot de conta representa o estado completo da conta no momento da captura; preflight ainda deve reler o destino antes de qualquer execução.
- `liked` só pode ser registrado quando há um candidato/destino definido, pois o modelo existente exige `game_id`.
- Python não é sandbox; a UI não adiciona outro transporte além do gateway, mas o código é local e revisável.
