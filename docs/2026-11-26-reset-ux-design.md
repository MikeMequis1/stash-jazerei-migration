# Etapa 7 — Reset operacional e UX em três etapas

Data: 2026-11-26

## Intenção

Reduzir a administração de procedimentos técnicos a três atividades de
usuário: preparar, revisar/executar e conferir. O backend, os gates e as
decisões humanas continuam sendo a fonte de verdade.

## Abordagens consideradas

1. **Reescrever o fluxo em três etapas com orquestrador de preparação**
   (escolhida). Reutiliza os serviços existentes, mantém aprovação,
   preflight e execução separados e esconde procedimentos internos atrás de
   um único CTA por etapa.
2. Manter o rail de oito passos e apenas agrupar visualmente. Reduz pouco o
   número de cliques e mantém a navegação acoplada ao backend.
3. Criar uma CLI nova por cima do fluxo atual. Não resolve a confusão visual
   e duplicaria caminhos de decisão.

## Decisões

- **Preparar** executa somente o que está ausente ou desatualizado:
  coleta válida, snapshot de destino completo e mais novo que a origem,
  correspondências faltantes, política automática (somente após confirmação
  explícita), plano novo quando o atual está inválido/stale e validação
  final.
- **Aprovação e preflight ficam na etapa 2**, depois da revisão humana.
  O preflight atual exige plano aprovado; inverter essa ordem reduziria a
  segurança. A etapa 2 oferece um CTA "Aprovar plano e preparar execução"
  que encadeia aprovação, preflight e criação da execução, sem escrever.
- **Executar** continua exigindo confirmação explícita, os dois gates de
  ambiente e o endpoint existente.
- **Conferir** usa a verificação autenticada já existente, somente leitura,
  e agrupa o relatório em uma única superfície.
- **Avançado** recebe histórico de planos/execuções/verificações, hashes,
  snapshots, identidade, gates, logs de preparação e reconciliação.
- O startup não seleciona plano nem execução histórica automaticamente.
  `wizard-state.json` é descartável e será recriado.
- Execuções antigas permanecem no disco e no histórico técnico. A presença
  de `write_ambiguous` não resolvida bloqueia nova preparação para execução
  até leitura autenticada de reconciliação.
- O orquestrador grava eventos append-only em
  `data/migration/prepare-runs/<run_id>/events.jsonl` e um manifesto por
  execução. Reinício reavalia artefatos e reutiliza o que já é válido.
- Nenhuma política automática roda silenciosamente; a prévia é mostrada e a
  aplicação exige um clique de confirmação.

## Não objetivos

- Não apagar planos, aprovações, execuções, verificações, preflights,
  decisões de match/liked ou snapshots.
- Não habilitar escrita, não mudar os gates e não iniciar migração.
- Não criar retry automático para escrita.

## Partes tocadas

- `src/migration/orchestrator.py` (novo): varredura, orquestração e
  persistência da preparação.
- `src/review_ui.py`: rotas de preparação, encadeamento
  aprovação+preflight+execução, fim da seleção automática de histórico.
- `web/review.html`, `web/review.js`, `web/review.css`: três etapas e seção
  avançada.
- `tests/test_prepare_orchestrator.py` (novo) e ajustes nos testes do wizard.
