# Handoff do projeto Stash → Já Zerei

**Data:** 2026-11-26  
**Branch:** `main`  
**Commit de referência:** `898512a` (`docs: assess project state and restart options`)  
**Estado:** ciclo pausado; implementação atual preservada como legado de referência.  
**Gates:** `MIGRATION_ALLOW_WRITES` e `MIGRATION_ENABLE_REAL_WRITE_TRANSPORT` desligados; `.env` inexistente.

Este é o documento principal para retomar o projeto. Ele registra o que foi feito, o que é confiável, o que não pode ser reutilizado sem revisão e qual arquitetura deve ser construída no próximo ciclo.

Documento complementar de diagnóstico: [`docs/project-assessment-and-restart-options.md`](project-assessment-and-restart-options.md).  
Documentação histórica: os demais arquivos de `docs/` descrevem etapas do ciclo encerrado.

> **Regra central:** dados, decisões e execuções históricas são **evidências**, não autorizações para novas escritas. Nenhuma decisão antiga deve ser promovida automaticamente a decisão do novo fluxo.

---

## A. Estado de encerramento

### O que o projeto pretendia

Transferir as avaliações pessoais do Stash para o Já Zerei sem cadastrar jogo por jogo, preservando nota, texto e `liked`, com revisão humana, plano auditável, leitura prévia do destino, escrita controlada e verificação posterior.

### O que foi concluído

- Coleta completa do Stash: 1.114 jogos, 76 reviews escritas e 76 detalhes.
- Classificação das avaliações: 76 escritas, 622 somente com nota, 416 sem nota descartadas.
- Matching somente leitura de 629 alvos, com candidatos, scores, alternativas e estados explícitos.
- Snapshot autenticado do destino, versionado e com manifesto.
- Interface local de revisão de correspondências e `liked`.
- Journals append-only para decisões de correspondência e `liked`.
- Cadeia de plano, manifesto, aprovação, preflight e execução.
- 17 reviews realmente criadas no destino e confirmadas por releitura autenticada.
- Suíte local com 357 testes coletados; último resultado: 347 aprovados, 10 ignorados, 0 falhas.

### O que permanece incompleto

- A migração das 698 avaliações não foi concluída.
- Apenas quatro itens aparecem como elegíveis no motor, e um deles está errado.
- 47 decisões automáticas de `liked` continuam exigindo reconfirmação humana.
- Nenhuma avaliação somente nota foi escrita e verificada de ponta a ponta.
- O contrato de `platforms` no upsert não foi validado.
- O motor central de elegibilidade não é consumido pelo planejamento legado.
- Identidade de candidato não é validada por título/ano/edição no motor central.
- A interface mistura estados de correspondência, destino, `liked` e elegibilidade.

### Por que a arquitetura atual não será continuada como base principal

A implementação acumulou fontes de verdade concorrentes:

- `data/migration/matches.json` histórico;
- `data/migration/evaluations/<run>/matches.json`;
- `data/migration/stage9/match-decisions.jsonl`;
- `data/migration/evaluations/<run>/match-decisions.jsonl`;
- `data/migration/liked-decisions.jsonl`;
- snapshots do destino;
- planos, manifestos, aprovações, preflights e execuções.

O planejamento legado usa caminhos históricos e não consome o motor novo de elegibilidade. O resultado é que “aprovado”, “elegível” e “pronto para migrar” podem significar coisas diferentes conforme o arquivo consultado.

Além disso, o fluxo operacional exige muitas camadas para uma única operação de review: coletar, gerar snapshot, revisar correspondência, decidir `liked`, gerar plano, aprovar manifesto, preflight, preparar execução, confirmar ação, executar e verificar. A complexidade é maior que o requisito funcional.

### Decisão

O desenvolvimento do fluxo atual está **pausado**. O código existente não será apagado nem continuará recebendo funcionalidades. A próxima implementação será uma aplicação mínima, linear e com uma única fonte de verdade, reaproveitando apenas os componentes que já provaram valor.

---

## B. Estado técnico atual

### Repositório

- Branch: `main`.
- HEAD de referência: `898512a`.
- O handoff e os ajustes documentais desta etapa não foram commitados, por instrução explícita.
- O código-fonte não foi alterado nesta etapa.

### Estrutura

```text
stash-jazerei-migration/
├── src/
│   ├── cli.py                 comandos operacionais do fluxo legado
│   ├── config.py              ambiente e gates
│   ├── storage.py             JSON, hashes e manifestos
│   ├── transport.py           HTTP somente leitura
│   ├── stash/                 parsers e coletores do Stash
│   ├── browser/               Chromium persistente e política de rede
│   ├── jazerei/               clientes de leitura, catálogo, snapshots, identidade
│   ├── models/                modelos Pydantic de origem, destino e execução
│   ├── migration/             matching, decisões, elegibilidade, plano, preflight, execução
│   ├── review_ui.py           servidor local e serviço de revisão
│   └── cli.py
├── web/                       interface estática da revisão
├── tests/                     suíte local e fixtures
├── docs/                      conhecimento e histórico
├── data/                      dados operacionais, ignorados pelo Git
└── reports/                   relatórios operacionais, ignorados pelo Git
```

### Módulos e responsabilidades

| Área | Arquivos principais | Responsabilidade | Situação |
|---|---|---|---|
| Configuração | `src/config.py` | ambiente, gates, operador | funcional |
| Armazenamento | `src/storage.py` | JSON atômico, SHA-256, manifesto | funcional |
| Stash | `src/stash/*`, `src/browser/stash.py` | coleta, paginação, parsing | comprovado com dados reais |
| Navegador | `src/browser/session.py`, `policy.py` | Chromium persistente, política read-only | funcional |
| Já Zerei leitura | `src/jazerei/*` | catálogo, reviews, identidade, snapshot | parcialmente comprovado |
| Avaliações | `src/migration/evaluations.py` | 76/622/416, proveniência | funcional e testado |
| Matching | `matcher.py`, `evaluation_matching.py`, `evaluation_store.py` | candidatos, scores, cache, run | funcional |
| Decisões de correspondência | `match_decisions.py` | journal append-only por run | funcional |
| `liked` | `liked.py` | decisão, source-only, reconfirmação, deferimento | funcional |
| Elegibilidade | `eligibility.py` | regras de migração e lote | implementada, **desconectada do planner** |
| Planejamento legado | `planner.py`, `validator.py` | plano de reviews | funcional, usa caminho antigo |
| Aprovação/preflight/execução | `approval.py`, `preflight.py`, `execution.py` | cadeia de hashes e escrita controlada | funcional, porém complexo |
| Revisão/UI | `review_ui.py`, `wizard.py`, `orchestrator.py`, `setup.py` | estado local, jobs, wizard | funcional, alto acoplamento |
| Web | `web/review.*` | duas filas, filtros, lote | funcional, testado localmente |
| CLI | `src/cli.py` | operações do fluxo legado | funcional |

### Testes

- 357 testes coletados.
- 347 aprovados.
- 10 ignorados.
- 0 falhas.
- `ruff check` limpo.
- `node --check web/review.js` aprovado.

Os 10 ignorados dependem de artefatos históricos removidos pelo reset operacional; não são falhas.

A suíte é majoritariamente local. Ela cobre parsers, modelos, matching, decisões, segurança de escrita e a interface, mas não substitui a prova de uma escrita real completa em todos os cenários.

### Componentes funcionais comprovados

- Coleta real do Stash via navegador.
- Classificação 76/622/416.
- Catálogo público e matching real de 629 alvos.
- Snapshot autenticado de 36 reviews.
- Journals de decisões append-only.
- Geração de plano, manifesto, preflight e execução com hashes.
- 17 criações de review confirmadas por HTTP 200 e releitura autenticada.

### Componentes problemáticos, redundantes ou legados

- `ratification.py` antigo convive com a reconfirmação nova de `liked`.
- `eligibility.py` convive com regras duplicadas no planner e no validador.
- Dois arquivos de matching e dois históricos de correspondência.
- `wizard.py`, `orchestrator.py`, `setup.py` e `review_ui.py` repetem estados operacionais.
- `watchlist.py` e a leitura de watchlist em `browser/jazerei.py` não são usadas por nenhum fluxo atual.
- `importer.py` é apenas um resumo de bloqueios.
- O painel de plano legado pode gerar plano sem consumir a elegibilidade nova.

### Gates

- `MIGRATION_ALLOW_WRITES=false`.
- `MIGRATION_ENABLE_REAL_WRITE_TRANSPORT=false`.
- `.env` não existe.
- As variáveis de ambiente estavam ausentes na verificação.
- As 17 escritas históricas ocorreram em uma execução autorizada anterior; isso não autoriza novas escritas.

---

## C. Inventário de dados e evidências

### Números atuais

| Item | Quantidade | Fonte |
|---|---:|---|
| Jogos no Stash | 1.114 | `games.json` |
| Avaliações com nota | 698 | `evaluations.json` |
| Reviews escritas | 76 | `evaluations.json` |
| Avaliações somente nota | 622 | `evaluations.json` |
| Jogos sem nota | 416 | `excluded` |
| Alvos de matching | 629 | `matches.json` |
| Candidatos | 553 | `lookup_status=candidate` |
| Revisão manual | 49 | `lookup_status=manual_review` |
| Sem candidato | 26 | `lookup_status=no_candidate` |
| Inconclusivo | 1 | HTTP 503 |
| Reviews no destino agora | 36 | snapshot atual |
| Reviews no destino antes das 17 escritas | 19 | snapshot anterior |
| Avaliações de origem representadas | 35 | 36 − RE4 |
| Escritas verificadas | 17 | execução preservada |
| Reconciliadas por conteúdo | 18 | candidatos no destino |
| RE4 fora do escopo | 1 | `game_id=974` |
| Aprovações de correspondência | 51 | `matches.json` |
| Aprovações com escrita verificada | 17 | execução |
| Aprovações sem escrita no destino | 34 | `matches.json` |
| Decisões de `liked` | 53 | journal |
| Automáticas | 47 | journal |
| Humanas | 6 | journal |
| Ratificações em lote | 2 | 48 correspondências + 47 `liked` |
| Edições especiais | 123 | `evaluations.json` |
| Candidatos para lote | 498 | elegibilidade |
| Elegíveis no motor | 4 (3 corretos) | elegibilidade |
| `liked` automático a reconfirmar | 47 | estado da UI |
| Itens sem identidade de destino | 23 | 22 sem candidato + 1 inconclusivo |

### Como os números se relacionam

- **1.114 → 698:** 416 jogos não têm nota válida e foram descartados.
- **698 → 76 + 622:** 76 têm texto; 622 têm apenas nota.
- **698 → 629:** 69 avaliações escritas preservam decisões históricas e não foram reenviadas ao matching de avaliações.
- **629 → 553 + 49 + 26 + 1:** candidatos, revisão manual, sem candidato e uma falha HTTP.
- **36 no destino:** 19 antes da execução + 17 criadas e verificadas.
- **35 de origem:** 36 menos a review RE4, que não existe no Stash como avaliação.
- **35 = 17 + 18:** 17 escritas verificadas e 18 candidatos reconciliados por conteúdo, estes últimos **não aprovados**.
- **51 = 17 + 34:** 17 aprovações com escrita verificada e 34 aprovações históricas sem escrita correspondente no destino.
- **53 = 47 + 6:** decisões automáticas e humanas de `liked`.
- **4 elegíveis:** itens com correspondência aprovada, `liked` humano, edição resolvida, ausência no destino e hashes atuais.
- **498 para lote:** candidatos de correspondência elegíveis para aprovação em lote; **não** são migrações prontas.
- **629 não é 698:** o matching de avaliações excluiu avaliações escritas que já tinham decisão histórica.

### Caminhos preservados

Dados de origem:

- `data/stash/runs/20261126T183458Z-stash-browser/games.json`
- `data/stash/runs/20261126T183458Z-stash-browser/reviews.json`
- `data/stash/runs/20261126T183458Z-stash-browser/export-report.json`
- `data/stash/runs/20261126T183458Z-stash-browser/manifest.json`

Avaliações e matching:

- `data/migration/evaluations/20261126T201952Z-evaluations/evaluations.json`
- `data/migration/evaluations/20261126T201952Z-evaluations/matches.json`
- `data/migration/evaluations/20261126T201952Z-evaluations/match-report.json`
- `data/migration/evaluations/20261126T201952Z-evaluations/catalog-cache.jsonl`
- `data/migration/evaluations/20261126T201952Z-evaluations/manifest.json`

Destino:

- `data/jazerei/runs/20261126T183706Z-account-snapshot/review-state/account-reviews.json` — 19 reviews, antes das escritas.
- `data/jazerei/runs/20261126T-evaluation-account-snapshot/review-state/account-reviews.json` — 36 reviews, estado atual conhecido.
- `data/jazerei/runs/*/manifest.json`

Decisões:

- `data/migration/matches.json`
- `data/migration/stage9/match-decisions.jsonl`
- `data/migration/stage9/ratifications.jsonl`
- `data/migration/liked-decisions.jsonl`
- `data/migration/approvals.jsonl`

Plano, preflight e execução:

- `data/migration/plans/stage9-review-20261126T191627Z/` — plano executado.
- `data/migration/preflights/20261126T191630Z-preflight-stage9-review-20261126T191627Z/` — 51 snapshots de evidência.
- `data/migration/executions/20261126T191634Z-execution-stage9-review-20261126T191627Z/` — 51 ações; 17 verificadas, 34 canceladas.
- Demais planos em `data/migration/plans/` são prévias históricas.

Sessão e estado operacional:

- `data/browser/` — perfil persistente e identidade local; contém credenciais. **Nunca versionar, copiar ou anexar.**
- `data/migration/wizard-state.json` — seleção da interface; não autoriza escrita.
- `data/migration/prepare-runs/` — eventos de preparação.
- `reports/` — relatórios operacionais; não são fonte de verdade.

### O que é confiável

- Exports brutos e manifestos.
- As 698 avaliações classificadas.
- O snapshot de 36 reviews.
- Os 629 resultados de matching como candidatos.
- Os journals de decisões.
- O plano executado, seu preflight e a execução de 17 verificações.
- A presença de 35 avaliações de origem no destino.

### O que não é confiável sem revisão

- A aprovação de `left-4-dead-2` para o candidato 122.
- As três edições especiais sem confirmação.
- As 47 decisões automáticas de `liked`.
- As 34 aprovações históricas sem escrita.
- Os 18 candidatos que já estão no destino, mas nunca foram aprovados.
- Qualquer decisão histórica sem operador registrado.

---

## D. Pendências e riscos conhecidos

### Bloqueios comprovados

| Item | Estado | Impacto |
|---|---|---|
| `left-4-dead-2` | aprovado para `122` (Left 4 Dead, 2008); alternativa correta `124` (Left 4 Dead 2, 2009), confiança 0,90 | não migrar; corrigir ou revisar |
| `rango--2` | aprovado para 5114; `edition_confirmed=false` | bloqueado por edição |
| `ratatouille--1` | aprovado para 4081; `edition_confirmed=false`; `liked` automático | bloqueado por edição e reconfirmação |
| `spirit-of-the-north-enhanced-edition` | aprovado para 107141; `edition_confirmed=false`; `identity_conflict=true`; presente no destino | não reenviar; reavaliar identidade |
| 47 `liked` automáticos | `requires_reconfirmation` | bloqueiam os itens correspondentes |
| 35 avaliações no destino | 17 verificadas + 18 reconciliadas | não podem ser reenviadas |
| RE4 (`game_id=974`) | review vazia no destino, sem origem no Stash | fora do escopo |
| Elegibilidade × planner | `evaluate_eligibility` não é usado por `generate_plan`, `planner`, `preflight` ou `validator` | plano legado não é confiável |

### Riscos

- **Contrato de `platforms` desconhecido.** O frontend envia cinco campos; o gateway envia quatro. A primeira escrita pode falhar com 4xx.
- **Escrita somente nota não comprovada.** As 17 escritas eram reviews com texto. Nenhuma avaliação `rating_only` foi criada e verificada.
- **Identidade não validada centralmente.** O motor não compara título, ano, slug de edição ou `identity_conflict`.
- **Source-only não chega ao plano legado.** `eligible_liked` descarta decisões com `game_id=null`.
- **Histórico novo não chega ao plano legado.** O planejamento lê o histórico `stage9`, não as decisões do run de avaliações.
- **Contadores ambíguos.** “498 prontos para lote” significa apenas “elegível para aprovar correspondência”.
- **Lote parcial pouco visível.** A UI mostra falhas apenas em toast, não por item.
- **Sessão e perfil do navegador podem estar expirados.** O próximo ciclo exige login manual e nova verificação de identidade.
- **Artefatos de execuções antigas não existem mais.** Documentos que citam as execuções `20261125T221529Z` e `20261125T224312Z` descrevem evidência removida pelo reset; não procurar esses diretórios.

### Desconhecidos

- Se `platforms` é obrigatório ou opcional no backend.
- Se o backend aceita `[]`, omissão ou `null` em `platforms`.
- Se `liked` aceita omissão ou `null` fora do payload estrito usado pelo gateway.
- Se o catálogo mudou desde 2026-11-26.
- Se as três edições especiais ainda são as mesmas entradas.

### Não confundir

- 51 aprovações **não** são 51 migrações prontas.
- 498 candidatos para lote **não** são 498 migrações prontas.
- 629 resultados de matching **não** são aprovações.
- 35 avaliações no destino **não** são 35 autorizações para reescrever.
- 17 verificações **não** autorizam novas escritas.

---

## E. Arquitetura futura recomendada

A direção aprovada é uma aplicação mínima, linear e explícita. Ela não herda o wizard, o plano por bundle, o preflight separado nem a máquina de estados da UI legada.

```mermaid
flowchart LR
  A[Export Stash imutável] --> B[evaluations.json]
  B --> C[matches.json + evidências]
  C --> D[decisions.jsonl append-only]
  D --> E[destination snapshot]
  E --> F[eligibility]
  F --> G[plan.json reproduzível]
  G --> H[execução: leitura prévia]
  H --> I[um POST autorizado]
  I --> J[releitura autenticada]
  J --> K[verification.json + execution.jsonl]
```

### Componentes mínimos

| Componente | Responsabilidade |
|---|---|
| `collect` / import | validar e importar export bruto do Stash |
| `classify` | produzir 698 avaliações normalizadas |
| `match` | buscar catálogo, propor candidatos e evidências |
| `review` | registrar decisões humanas de correspondência e `liked` |
| `destination` | capturar snapshot autenticado do destino |
| `eligibility` | única função que decide o que pode ser migrado |
| `plan` | gerar lista reproduzível de ações |
| `execute` | leitura prévia, confirmação, POST único, releitura |
| `verify` | comparar o estado posterior e declarar o resultado |

### Princípios

- Dados brutos imutáveis.
- Um único modelo de avaliação.
- Uma única decisão por `source_ref`.
- Uma única função de elegibilidade.
- Uma única lista de ações.
- Um journal append-only de decisões e execuções.
- Leitura do destino antes de qualquer escrita.
- Gates explícitos e confirmação humana.
- Sem retry automático em resultado ambíguo.
- Nenhuma operação é concluída apenas por HTTP 2xx; a releitura é obrigatória.
- A presença no destino bloqueia nova escrita.
- Decisão histórica não é autorização nova.

### Fluxo operacional

1. Importar o export do Stash e validar manifesto.
2. Classificar 76/622/416.
3. Gerar candidatos de matching com evidências.
4. Revisar humanamente correspondência e `liked`.
5. Capturar snapshot atual do destino.
6. Avaliar elegibilidade com identidade, edição, destino, `liked` e versão.
7. Gerar plano reproduzível.
8. Autorizar uma ação explicitamente.
9. Ler o destino imediatamente antes.
10. Enviar um único POST.
11. Reler o destino autenticado.
12. Confirmar ou classificar como ambíguo; nunca reenviar automaticamente.

---

## F. Estratégia de reaproveitamento

### 1. Reaproveitar diretamente

- `src/stash/parser.py`, `games.py`, `reviews.py`, `pagination.py` — parsers e coletores comprovados.
- `src/browser/session.py`, `policy.py`, `stash.py` — Chromium persistente e política read-only.
- `src/jazerei/catalog.py` — busca de catálogo.
- `src/jazerei/identity.py`, `state.py`, `snapshots.py` — identidade, captura e manifesto.
- `src/models/source.py`, `src/models/snapshot.py` — modelos de origem e snapshot.
- `src/storage.py` — JSON, SHA-256 e manifestos.
- `src/config.py` — gates e configuração.
- `src/transport.py` — HTTP somente leitura.

### 2. Reaproveitar após adaptação

- `src/migration/evaluations.py` — classificação 76/622/416; remover dependências do fluxo antigo.
- `src/migration/matcher.py`, `evaluation_matching.py` — matching e evidências; retirar cache/estado legado.
- `src/migration/match_decisions.py`, `liked.py` — journals; unificar em um único journal ou manter dois com contrato explícito.
- `src/migration/eligibility.py` — base da função única; adicionar identidade, edição e `identity_conflict`.
- `src/models/normalized.py` — avaliar quais campos pertencem ao novo modelo mínimo.
- `src/migration/execution.py` — reutilizar `BrowserWriteTransport`, payload estrito e classificação de ambiguidade; descartar a cadeia de plano/preflight acoplada.
- `src/browser/jazerei.py` — busca e leitura; remover o que não for usado.

### 3. Consultar apenas como referência

- `src/review_ui.py` e `web/review.*` — UX de duas filas, filtros, lote e confirmação.
- `src/cli.py` — comandos e semântica de coleta, matching e execução.
- `src/migration/approval.py`, `preflight.py`, `planner.py`, `validator.py` — ideias de proveniência, hash, leitura prévia e verificação.
- `docs/` — conhecimento de API, DOM, segurança, execução e decisões.
- `tests/` — casos de borda e contratos que valem preservar.

### 4. Não utilizar na nova aplicação

- `wizard.py`, `orchestrator.py`, `setup.py` como camada de estado.
- `approval.py` + `preflight.py` + `executions.py` como fluxo principal.
- `ratification.py` antigo.
- `importer.py`.
- `watchlist.py` e leitura de perfil não usadas.
- `matches.json` histórico como fonte de decisão.
- Defaults `stage9`.
- Contadores e estados visuais que não derivam da elegibilidade.

Nenhum módulo deve ser considerado “pronto para reutilização” sem leitura do código e teste no novo fluxo.

---

## G. Primeiro marco da retomada

O primeiro marco é **uma única avaliação somente nota**, migrada de ponta a ponta por um fluxo mínimo, sem tocar nas 35 avaliações já representadas.

### Critérios de seleção

- `evaluation_kind=rating_only`;
- correspondência aprovada com identidade confiável (título, ano, slug);
- ausência de ambiguidade, duplicidade, edição especial ou conflito;
- `liked` humano válido e atual;
- `game_id` ausente do snapshot atual do destino;
- snapshot do destino atualizado e completo;
- payload sem sobrescrever review existente.

### Estado verificado

Há **498 candidatos** de avaliação somente nota que atendem aos critérios objetivos de matching e ausência no destino. Nenhum deles possui decisão humana de `liked` registrada até o encerramento. Portanto, nenhum item atende a todos os critérios neste momento.

A retomada começa escolhendo um item desse conjunto, revisando a correspondência e registrando a decisão humana de `liked`. Essa escolha é uma decisão nova, não uma aprovação automática de dado histórico.

### Fluxo do marco

1. Selecionar o item e revisar candidato, título, ano e alternativas.
2. Aprovar a correspondência com identidade validada.
3. Registrar `liked` humano.
4. Capturar snapshot atual do destino e confirmar ausência.
5. Gerar plano de uma única ação.
6. Autorizar explicitamente a execução.
7. Ler o destino imediatamente antes.
8. Enviar um único POST.
9. Reler o destino autenticado.
10. Confirmar nota, texto vazio, `liked`, `game_id` e dono.
11. Registrar o resultado; não reenviar em caso de ambiguidade.

Critério de conclusão: uma review somente nota criada, relida e confirmada; destino passa de 36 para 37 reviews; nenhuma das 35 anteriores é alterada.

**Este marco não foi executado nesta etapa.**

---

## H. Procedimento para retomar o projeto

1. Ler este documento.
2. Ler [`docs/project-assessment-and-restart-options.md`](project-assessment-and-restart-options.md).
3. Conferir branch, HEAD, working tree e os caminhos de dados listados na seção C.
4. Revalidar gates, `.env`, identidade do destino e validade dos manifestos.
5. Revisar a arquitetura mínima da seção E e decidir o menor recorte vertical.
6. Planejar a primeira implementação, sem herdar o wizard nem o fluxo de aprovação/preflight antigo.
7. Implementar e testar o marco da seção G sem tocar nas 35 avaliações existentes.
8. Só depois considerar expansão para as demais avaliações.

Lembretes:

- Dados históricos são evidências, não autorizações.
- Não usar `generate_plan` legado como referência de elegibilidade.
- Não reenviar as 35 avaliações já representadas.
- Não reenviar ação `write_ambiguous`.
- Não habilitar gates sem autorização humana explícita para uma ação específica.
- Não tratar 498 candidatos de lote como 498 migrações prontas.
- Não apagar o código legado antes de o novo fluxo provar paridade.

---

## Encerramento

O ciclo atual está formalmente pausado. O valor preservado está nos dados, nos manifestos, no conhecimento de API e DOM, na classificação, no matching, nos journals e nas 17 escritas verificadas. A próxima etapa deve começar pequena, com uma única avaliação e uma única fonte de verdade.
