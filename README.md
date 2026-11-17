# LiveForge — editor autônomo de lives

Enquanto você joga, o LiveForge vigia a gravação do OBS e entrega **Shorts/Reels/TikToks prontos** (1080×1920, legenda amarela animada palavra por palavra, zoom, efeitos sonoros, cortes de pausa) e, se você quiser, **episódios longos** com capítulos. A live nunca é tocada: acesso ao OBS só leitura, prioridade baixa, e tudo pausa sozinho se o OBS começar a perder frames.

A escolha dos melhores momentos usa o **Claude da sua assinatura** (Pro/Max, via Claude Code) — sem chave de API e sem custo extra.

## Instalar (Windows 10/11)

```bash
git clone <url-deste-repositorio> LiveForge
cd LiveForge
setup.cmd
```
O `setup.cmd` avisa se faltar **uv**, **Node.js** ou **FFmpeg** (mostra o comando `winget` de cada um) e instala o resto dentro da pasta. Depois:

1. **`login.cmd`** — login do Claude Code na sua assinatura (abre o navegador, uma vez). Precisa do app Claude desktop instalado.
2. **`start.cmd`** — inicia; o painel abre sozinho em `http://127.0.0.1:8765/`.
3. Aba **Configuração** do painel: mostra, com ✓/✗, tudo o que precisa estar certo no OBS e no PC, e o passo a passo do que faltar.

Requisitos: OBS Studio 30+, app Claude desktop (assinatura Pro/Max), ~3 GB livres para o programa e modelos, espaço em disco para as gravações.

## Como funciona
1. **Agente 1 (monitor)** — a cada arquivo de gravação fechado (divisão de 3 min no OBS): transcreve a sua voz (Whisper `large-v3-turbo`), mede picos de volume, movimento na tela, picos do chat da Twitch (opcional) e os marcadores **F8**; o Claude dá nota 0–100 aos momentos.
2. **Agente 2 (shorts)** — corta o trecho, remove pausas mortas, **retranscreve o áudio final do clipe** e alinha cada palavra ao início real da voz, e renderiza com **HyperFrames** (fallback FFmpeg).
3. **Agente 3 (episodes)** — **F6/F7** marcam início/fim de episódio, **F9/F10** um intervalo a remover; exporta sem reencodar, com título, descrição e capítulos.

## Painel
- **Ao vivo** — saúde do PC/OBS, filas, clipes (de todas as sessões) com prévia, aprovar/priorizar/descartar/refazer/**excluir** (libera espaço), **editar legenda** (texto e tempos, com o vídeo ao lado; o clipe é refeito), copiar título+descrição.
- **Configuração** — jogo da live (automático pelo OBS ou digitado), vocabulário da transcrição (nomes de amigos, termos do jogo) e o checklist do OBS/áudio/Claude/programas.
- **Uso do Claude** — quanto da sua assinatura já foi usado (janela de 5 h e semanal), ritmo e projeção; o LiveForge para de chamar o Claude em 80 % / 90 % (editável) e segue com uma heurística local.
- **Modelos** — editor visual dos 3 layouts (rosto+jogo, rosto grande, só jogo).
- **Contas** — Claude e o canal da Twitch (chat como sinal, leitura anônima).

Teclas globais (não atrapalham o jogo): **F8** marcar clipe · **F6/F7** episódio · **F9/F10** intervalo · **F11** pausar a edição (nunca a live).

## Áudio: separe o microfone do jogo (importante)
Com tudo numa trilha só, a transcrição mistura sua fala com diálogos do jogo e a detecção de momentos erra. No OBS:
1. *Editar → Propriedades avançadas de áudio*: microfone nas trilhas **1 e 2**; todas as outras fontes com som (desktop, captura de jogo, placa de captura, alertas, Discord, microfone da webcam) **só na 1**.
2. *Configurações → Saída → Gravação → Trilha de áudio*: marque **1 e 2**.
3. Grave 10 s, e no painel (Configuração → Áudio) clique em **Usar trilhas detectadas**.
Os clipes continuam com o som completo; legenda e detecção usam só a sua voz.

## OBS (o checklist confere tudo isso)
WebSocket ativado (*Ferramentas → Configurações do servidor WebSocket*) · Saída no modo Avançado · gravação em **MKV** com **divisão automática a cada 3 min** · *Geral → Gravar automaticamente quando estiver transmitindo* · pasta de gravação num disco com espaço (1080p60 em CQP 18 ≈ 16 GB/h).

## Arquivos e privacidade
- `config.yaml` — padrões do projeto (versionado). **Seus ajustes** do painel ficam em `data/settings.json`.
- `.env` — segredos opcionais (senha do WebSocket do OBS se não estiver no mesmo PC; token do Claude Code). Modelo: `.env.example`.
- **Nunca vão para o Git** (`.gitignore`): `.env`, `data/` (banco, ajustes, modelos), `recordings/`, `output/`, `work/`, `logs/`, vídeos.
- Chat da Twitch: só contagens (mensagens/risadas), nunca nomes ou textos. Nada é publicado automaticamente.

```
main.py                  CLI e laço do worker        liveforge/panel.py/.html  painel local
liveforge/monitor.py     Agente 1                     liveforge/shorts.py       Agente 2 (+ legendas, edição, exclusão)
liveforge/episodes.py    Agente 3                     liveforge/render_hf.py    HyperFrames (0.9.59, Apache-2.0)
liveforge/render_ass.py  fallback FFmpeg              liveforge/guard.py        proteção da live + OBS (leitura)
liveforge/llm.py         Claude Code (assinatura)     liveforge/setup_checks.py checklist da aba Configuração
liveforge/settings.py    ajustes do painel            liveforge/chat.py, gpu.py, db.py, core.py
```
Teste rápido da lógica: `.venv\Scripts\python -m tests.test_logic`

## Comandos
`start.cmd` / `stop.cmd` · `.venv\Scripts\python main.py status | resume | doctor | process PASTA_OU_ARQUIVO` · `--agents monitor,shorts,episodes`

## Problemas comuns
- **Nada é processado**: confira a divisão automática de 3 min no OBS (o arquivo só “fecha” quando o próximo começa).
- **Legenda fora de sincronia / palavra errada**: aba Ao vivo → *Editar legenda* (ajuste pelo vídeo com “agora”) — e adicione nomes/termos no Vocabulário.
- **Claude parou de ser usado**: veja a aba Uso do Claude (limite da janela de 5 h atingido → volta sozinho quando reinicia).
- **Log**: `logs\liveforge.log` (ou o fim dele no painel).
