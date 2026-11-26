# Stash — origem dos dados

Perfil: https://stash.games/users/MikeMequis1

Páginas:
- Jogos: /users/MikeMequis1/games/
- Reviews: /users/MikeMequis1/reviews/
- Review individual: /games/{slug}/reviews/MikeMequis1

As páginas do perfil são públicas; autenticação do app não implica autenticação para essas páginas.

## Paginação
Foram observadas 38 páginas de jogos e 13 de reviews. Descobrir dinamicamente via `nav.profile-pagination`; não fixar esses totais.

## Cards de jogos
Seletor: `.games-list__item-link`.
- slug/URL: `href`
- título: `.games-list__item-name` ou `data-text`
- status: `<use href="#...">`
- data: `.games-list__item-date`
- capa: `img[src]` ou `img[data-src]`

Preservar a data como `date_displayed`: seu significado exato não foi confirmado. Um jogo pode ter status e nota sem review textual (Dead to Rights: Retribution, playing, nota 8); portanto, reviews não representam a biblioteca inteira.

## Cards de reviews
Seletor: `.user__review-item`.
- link: `.user__review-comment[href]`
- texto: `.user__review-text`
- data: `time.user__review-date[datetime]`
- jogo: `.user__review-game`
- nota: `.user__review-rating`
- status/progresso: textos no bloco do jogo
- capa: imagem do bloco
- likes/comentários: `.user__review-likes`; validar pela estrutura/ícone.

Amostras: Sky: Children of the Light (nota 7, Beaten, Main story + extras) e B3313 (nota 8, Playing). A listagem mostra texto e “Full review”, mas não foi confirmado se o texto é integral. Visitar detalhes e comparar.

## Coleta
Requests + BeautifulSoup; paginação dinâmica; jogos e reviews em coletas separadas; visitar reviews individuais; preservar HTML opcionalmente; deduplicar sem descartar colisões; gerar relatório de páginas, contagens, campos ausentes e falhas. Usar timeout, retry limitado, User-Agent e intervalo respeitoso.
