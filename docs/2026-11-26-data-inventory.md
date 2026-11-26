# Inventário de dados — reset operacional 2026-11-26

Levantamento feito antes de qualquer remoção. Nenhuma escrita no Já Zerei
foi executada nesta etapa.

| Caminho | Conteúdo | Classificação | Impacto da remoção | Backup | Ação |
|---|---|---|---|---|---|
| `data/browser/profile/` | Perfil Chromium persistente: cookies, IndexedDB, local/session storage, cache, sessões e shaders | 1. Sessão/autenticação descartável | Exige login manual no Já Zerei e nova passagem pelo Cloudflare no Stash | Não (contém credenciais) | Remover |
| `data/browser/session-identity.json` | UID descoberto da sessão do Já Zerei | 1. Sessão/autenticação descartável | Regenerado no próximo login/verificação | Não | Remover |
| `data/browser/profile-meta.json` | Metadados do perfil do navegador | 2. Estado operacional temporário | Recriado pelo navegador | Não | Remover |
| `data/browser/probe-*` | Perfis de sondagem HTTP/Stash | 1. Sessão/autenticação descartável | Nenhum artefato de auditoria depende deles | Não | Remover |
| `data/migration/wizard-state.json` | Seleção visual, identidade e último id de verificação | 2. Estado operacional temporário | Startup volta a estado limpo; recriado pela UI | Não | Remover |
| `data/migration/matches.json` | Correspondências e decisões de matches | 4. Decisões humanas; 5. Dados reutilizáveis | Perda de decisões humanas e insumo do plano | Sim, se houver suspeita | Preservar |
| `data/migration/liked-decisions.jsonl` | Decisões de `liked`, humanas e automáticas | 4. Decisões humanas; 5. Reutilizável | Perda de decisões e histórico | Sim | Preservar |
| `data/migration/stage9/match-decisions.jsonl` | Histórico append-only de decisões de match | 3. Auditoria; 4. Decisões humanas | Perda de rastreabilidade | Sim | Preservar |
| `data/migration/stage9/ratifications.jsonl` | Ratificações de revisão em lote | 3. Auditoria; 4. Decisões humanas | Perda de evidência de revisão | Sim | Preservar |
| `data/migration/stage9/*.json|md` | Filas, resumos, categorias e aceitações editoriais | 3. Auditoria; 5. Reutilizável | Perda de análise histórica | Sim | Preservar |
| `data/migration/approvals.jsonl` | Aprovações vinculadas a planos e hashes | 3. Auditoria; 4. Aprovações | Perda de prova de autorização | Sim | Preservar |
| `data/migration/plans/` | Planos e manifestos, inclusive aprovados | 3. Auditoria; 4. Aprovações | Perda de planos e hashes | Sim | Preservar |
| `data/migration/preflights/` | Preflights, snapshots de evidência e manifestos | 3. Auditoria; 6. Recoletável se necessário | Perda de evidência pré-escrita | Sim | Preservar |
| `data/migration/executions/` | Execuções, confirmações, eventos e resultados de escrita | 3. Auditoria; 6. Não recoletável | Perda de prova do que ocorreu no destino | Sim | Preservar |
| `data/migration/verifications/` (quando existir) | Relatórios de verificação pós-escrita | 3. Auditoria | Perda de confirmações causais | Sim | Preservar |
| `data/migration/demolition-girl/` | Artefatos do piloto Demolition Girl | 3. Auditoria | Perda de histórico do piloto | Sim | Preservar |
| `data/stash/runs/` | Coletas do Stash com manifestos e hashes | 5. Dados reutilizáveis | Nova coleta completa seria necessária | Sim | Preservar |
| `data/jazerei/runs/` | Snapshots autenticados e runs de review-state | 3. Auditoria; 5. Reutilizável | Perda de estados anteriores do destino | Sim | Preservar |
| `data/jazerei/reviews.json`, `data/jazerei/reviews-stage9.json` | Reviews legadas de apoio | 3. Auditoria | Perda de referência histórica | Sim | Preservar |
| `reports/` | Reservado para relatórios; contém apenas `.gitkeep` | 2. Temporário | Nenhum | Não | Manter diretório |
| `.pytest_cache/`, `.ruff_cache/`, `src/**/__pycache__`, `tests/__pycache__` | Caches de ferramentas | 2. Temporário | Nenhum | Não | Remover |

## Reutilização

- A coleta `stage9-full-stash-browser-v2-20261125` continua válida e será
  reutilizada enquanto o manifesto estiver íntegro.
- O snapshot `stage9-account-snapshot-20261125` continua válido e é mais novo
  que a coleta; será reutilizado. A sessão autenticada em si será descartada.
- Correspondências e decisões humanas permanecem como insumo; a política
  automática não as sobrescreve.
- Planos, aprovações, preflights e execuções antigos não serão promovidos a
  estado atual no startup.

## Observação de segurança

A execução `20261125T224312Z-execution-stage9-review-20261125T201911Z` está
`inconclusive` e contém uma ação `write_ambiguous` (`pseudoregalia`). Ela é
preservada. Enquanto não for reconciliada com leitura autenticada, a nova
preparação não poderá preparar execução, evitando reenvio cego.
