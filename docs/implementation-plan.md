# Plano de implementação

## Fase 0 — Preparação
- [x] Consolidar descobertas e criar repositório privado
- [ ] Configurar ambiente virtual, lint e testes
- [ ] Criar modelos/schemas versionados

## Fase 1 — Exportador Stash (somente leitura)
- [ ] Cliente HTTP com timeout/retry/User-Agent
- [ ] Paginação dinâmica
- [ ] Exportar jogos e reviews
- [ ] Comparar texto de listagem e review individual
- [ ] Gerar games.json, reviews.json e export-report.json
- [ ] Começar com uma página e validar contagens

## Fase 2 — Já Zerei (leitura)
- [ ] Validar autenticação legítima sem expor credenciais
- [ ] Consultar reviews, Wishlist e perfil atuais
- [ ] Pesquisa e cache do catálogo
- [ ] Fixtures sanitizadas e testes

## Fase 3 — Normalização/correspondência
- [ ] Normalizar nomes, slugs, datas e notas
- [ ] Gerar candidatos, confiança e justificativas
- [ ] Decisões manuais; ambiguidades não são aprovadas automaticamente

## Fase 4 — Plano/aprovação
- [ ] Detectar reviews e Wishlist existentes
- [ ] Exibir conflitos e duplicatas
- [ ] Selecionar até 5 favoritos e 10 Playing Now
- [ ] Aprovar explicitamente cada grupo

## Fase 5 — Importação controlada
- [ ] Validar upsert
- [ ] Implementar reviews, Wishlist e perfil
- [ ] Rate limiting, checkpoints e retomada
- [ ] Exclusões desabilitadas por padrão

## Fase 6 — Verificação
- [ ] Reler reviews, Wishlist e perfil
- [ ] Comparar estado final com plano aprovado
- [ ] Relatório de sucessos, falhas e pendências
