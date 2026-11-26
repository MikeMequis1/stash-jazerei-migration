# Já Zerei — API observada

Endpoints observados no DevTools durante uso normal de https://jazerei.com. Não são documentação oficial. Separar leitura e escrita; escrita desabilitada até aprovação.

## Autenticação
Foi observado Firebase Secure Token em `https://securetoken.googleapis.com/v1/token?key=REDACTED`. Resposta inclui access_token, refresh_token, id_token, expires_in e user_id. Cabeçalho/token exigido por endpoint ainda não foi confirmado. Nunca versionar/logar tokens; usar sessão legítima e armazenamento local seguro.

## Reviews
Leitura: `GET /rest/v1/reviews?select=*&user_id=eq.{USER_ID}&order=created_at.desc`.
Campos observados incluem id, game_id, game_slug, game_name, game_cover, game_year, developer, user_id, username, rating, text, created_at, likes_count, comments_count, short_id, genres e moderation_status. Review com texto vazio é válida (rating 10, text "").

Upsert: `POST /api/reviews/upsert`, JSON observado:
```json
{"game_id":26570,"rating":0.5,"text":"ruim","liked":false}
```
Resposta: `{"ok":true,"game_id":26570}`. A mesma rota foi usada para editar; id e created_at permaneceram no teste. `text:""` também foi aceito com nota. Validar antes de lote.

Remoção observada: `DELETE /rest/v1/reviews?id=eq.{USER_ID}_{GAME_ID}`. Destrutiva; desabilitada por padrão.

## Catálogo
`POST /api/search/games`, exemplo `{"q":"Sonic Unleashed","limit":8}`. Retorna candidatos com id, name, slug, cover, lançamento, gêneros, empresas, plataformas e ratings. Busca Sonic Unleashed retornou ID 5169 e variantes/projetos relacionados: não escolher automaticamente o primeiro resultado.

## Wishlist
Adicionar: `POST /rest/v1/user_watchlist_games?on_conflict=user_id%2Cgame_id`.
```json
{"user_id":"{USER_ID}","game_id":"26570","game_slug":"demolition-girl","game_title":"Demolition Girl","cover_url":"https://images.igdb.com/igdb/image/upload/t_cover_big_2x/co6x7n.jpg","release_year":2004,"primary_platform":"PlayStation 2"}
```
Remover: `DELETE /rest/v1/user_watchlist_games?user_id=eq.{USER_ID}&game_id=eq.{GAME_ID}`.
RPCs observadas: get_profile_watchlist e get_game_watchlist_count (p_game_id, p_game_slug).

## Perfil
Leitura: `POST /rest/v1/rpc/get_my_profile`. Atualização: `POST /rest/v1/rpc/update_my_profile`, payload amplo com perfil, favoritos, playing_now, avatar, tema/capa, privacidade, links e notificações.

favorite_games contém id/slug/title/coverUrl; limite visual 5. playing_now também contém platform; limite observado no Já Zerei 10. Stash não aparenta impor limite equivalente. Preservar todos os jogos e solicitar seleção dos excedentes. Ao atualizar, ler perfil atual, modificar somente campos desejados e preservar os demais.

## Outras descobertas
get_public_user_by_username; get_user_external_accounts_public; get_user_activity_heatmap; check_veterano_badge; list_user_external_library; count_user_external_library; get_profile_watchlist_facets; tabelas de listas, comentários, badges, seguidores e notificações.

A biblioteca externa é de serviços como Steam, não a biblioteca principal. Jogos do perfil vêm de reviews e Wishlist; favoritos e Playing Now são configurações do perfil.

## Pendências
Token/cabeçalho por endpoint; semântica de liked; headers Prefer e conflitos da Wishlist; limites validados no servidor; remoção de favoritos/playing_now; rate limits e paginação.
