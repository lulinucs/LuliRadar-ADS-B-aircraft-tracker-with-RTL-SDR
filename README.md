# SDR Aviation Monitor

Estação local de monitoramento aeronáutico usando **um único RTL-SDR Blog V4**,
com dois modos de operação **mutuamente exclusivos**:

- **ADS-B** — recepção via `dump1090-mutability`, mapa ao vivo, histórico de voos e trilhas.
- **Rádio aeronáutico (AM)** — recepção via `rtl_fm`, presets, medidor de nível e
  gravação automática de transmissões.

Um `SDRManager` central garante que **apenas um processo por vez** use o
dongle: ativar um modo sempre encerra o outro primeiro.

---

## 1. Dependências do sistema (apt)

```bash
sudo apt update
sudo apt install python3-venv python3-pip rtl-sdr dump1090-mutability alsa-utils
```

- `rtl-sdr` fornece `rtl_fm`, `rtl_test` etc.
- `dump1090-mutability` você já tem instalado e funcionando.
- `alsa-utils` fornece `aplay` (reprodução local do áudio do rádio).

### 1.1 IMPORTANTE: desative o serviço systemd do dump1090

O pacote `dump1090-mutability` registra um **serviço systemd que inicia
sozinho no boot** e tenta abrir o RTL-SDR. Ele vai brigar com esta aplicação
pelo dongle (erro "device busy"). Desative-o uma vez, permanentemente:

```bash
sudo systemctl disable --now dump1090-mutability
```

Você pode confirmar que está desligado com `systemctl status dump1090-mutability`.

> Esta aplicação **também mata automaticamente** qualquer processo
> `dump1090-mutability` ou `rtl_fm` que já esteja rodando toda vez que você
> inicia `run.py` (inclusive uma sessão manual como
> `dump1090-mutability --interactive` que você tenha aberto num terminal) —
> isso é proposital, para garantir posse exclusiva do dongle sem você
> precisar matar nada na mão. Ver seção 8 (Robustez).

---

## 2. Dependências Python

```bash
cd /home/luli/dev/sdrlab/luliradar
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

(Alternativa sem venv, já que `python3-flask` está disponível via apt:
`sudo apt install python3-flask` e rode com o `python3` do sistema.)

---

## 3. Configurar a posição do receptor (obrigatório)

```bash
cp .env.example .env
```

Edite `.env` e preencha, com a posição real da sua antena (não inventamos
esse valor por você):

```
RECEIVER_LAT=-23.5505
RECEIVER_LON=-46.6333
RECEIVER_LABEL=Minha estação
```

Sem isso, o mapa carrega centralizado no (0,0) e as distâncias das
aeronaves não são calculadas.

---

## 4. Presets de rádio (Torre, ATIS, ...)

`config/frequencies.json` já vem preenchido com as frequências reais do
Aeroporto Internacional Hercílio Luz / Florianópolis (SBFL), fonte
AISWEB/DECEA:

| Preset | Frequência | 
|---|---|
| Torre Florianópolis | 118.700 MHz |
| ATIS Florianópolis | 127.450 MHz |
| Solo Florianópolis | 121.700 MHz |
| Operações Florianópolis | 122.500 MHz |
| Emergência / Guard | 121.500 MHz |
| Torre MIL | 122.800 MHz |

Se for usar outro aeródromo, edite `frequency_mhz` (sempre em **MHz com
ponto decimal**, ex.: `118.700` — nunca `118700`) ou adicione novos presets
seguindo o mesmo formato. O backend valida a faixa (20–1800 MHz, a faixa do
RTL-SDR) e recusa valores fora dela com uma mensagem clara, então um erro de
dígito não passa mais despercebido.

---

## 5. Executar

```bash
source .venv/bin/activate   # se estiver usando venv
python3 run.py
```

Acesse no navegador: **http://127.0.0.1:5000**

Pressione `Ctrl+C` para encerrar — o servidor derruba de forma limpa
qualquer `dump1090-mutability`/`rtl_fm` em execução antes de sair.

---

## 6. Navegar entre abas x controlar o SDR

**Clicar nas abas "ADS-B" / "RÁDIO" no topo só troca o que é exibido - nunca
liga ou desliga nada no dongle.** Quem controla o hardware são os botões
dentro de cada painel:

- Painel ADS-B: **Iniciar ADS-B** / **Parar ADS-B** (ou **ASSUMIR SDR /
  INICIAR ADS-B** se o rádio estiver ativo no momento).
- Painel Rádio: **Iniciar Rádio** / **Parar** (ou **ASSUMIR SDR / INICIAR
  RÁDIO** se o ADS-B estiver ativo no momento).

O indicador **SDR ONLINE/OFFLINE/TROCANDO MODO/ERRO** no topo sempre reflete
o hardware de verdade, independente de qual aba você está olhando - se você
estiver na aba Rádio enquanto o ADS-B está ativo, um aviso aparece
explicitamente ("SDR atualmente em uso pelo ADS-B").

## 7. Validar o modo ADS-B

1. Na aba **ADS-B**, clique **Iniciar ADS-B**.
2. O indicador no topo muda para `TROCANDO MODO...` por ~1s e depois
   `SDR ONLINE (ADS-B)`. O painel de status deve mostrar `dump1090: rodando`
   e, em alguns segundos, `Aeronaves visíveis` > 0 se houver tráfego.
3. Clique numa aeronave no mapa ou na tabela para ver detalhes e trilha.
4. Abra **Histórico ADS-B** para ver sessões já encerradas.
5. Clique **Parar ADS-B** - o indicador deve voltar para `SDR OFFLINE / IDLE`.

Teste manual do dump1090 fora da aplicação (pare a aplicação antes, para
não haver disputa pelo dongle):

```bash
dump1090-mutability --interactive
```

Se aeronaves aparecem aí mas não na aplicação, veja a seção de Troubleshooting.

---

## 8. Validar o modo Rádio

1. Vá para a aba **RÁDIO** (isso só troca a exibição - o ADS-B, se estiver
   rodando, continua ativo até você assumir).
2. Escolha o preset **ATIS Florianópolis** (127.450 MHz - bom teste porque
   ATIS transmite quase continuamente) e clique **Iniciar Rádio** (ou
   **ASSUMIR SDR / INICIAR RÁDIO** se o ADS-B estava ativo).
3. O medidor de nível deve se mover; ao aparecer sinal acima do squelch, o
   estado muda para `RECEBENDO`/`GRAVANDO` e uma gravação `.wav` é criada em
   `recordings/AAAA-MM-DD/`.
4. A gravação aparece em **Últimas transmissões**, com o preset e a
   frequência corretos (**127.450 MHz**, não `127450.000`); clique **OUVIR**
   para reproduzir pelo navegador.
5. Clique **Parar** - o indicador volta para `SDR OFFLINE / IDLE`.

Teste manual do rtl_fm fora da aplicação:

```bash
rtl_fm -f 127450000 -M am -s 48000 -g 40 -l 0 - | aplay -q -r 48000 -f S16_LE -t raw -c 1
```

(`-f` já em Hz - 127.450 MHz × 1.000.000).

---

## 9. Rodar os testes automatizados (sem tocar no dongle)

```bash
python3 -m unittest discover -s tests -v   # backend: máquina de estados, presets, HTTP
node tests/test_frontend_format.mjs         # frontend: formatação de frequência
```

Todos usam `subprocess.Popen` simulado (`tests/fakes.py`) e diretórios
temporários - nunca abrem o RTL-SDR nem escrevem em `data/`/`recordings/`
reais.

---

## 10. Robustez / o que já foi tratado

- **Máquina de estados única**: `SDRManager` (`app/sdr_manager.py`) é a
  única autoridade sobre o dongle, com estados `IDLE / ADSB / RADIO /
  SWITCHING / ERROR`. Trocar de modo sempre encerra o processo anterior,
  confirma que ele morreu, só então inicia o novo, e confirma que o novo
  processo não caiu de imediato antes de declarar o modo ativo.
- **Navegação x hardware são independentes**: clicar nas abas nunca chama
  `/api/mode/*`/`/api/stop` - só os botões Iniciar/Parar/Assumir SDR fazem
  isso (ver seção 6).
- **Duas trocas simultâneas**: uma trava não-bloqueante rejeita a segunda
  solicitação com HTTP 409 em vez de deixá-las correr em paralelo.
- **Processos órfãos**: subprocessos são criados com `start_new_session=True`
  e encerrados com SIGTERM → aguarda → SIGKILL se necessário
  (`app/procutil.py`). No startup, `cleanup_stray_processes()` mata
  sobras de execuções anteriores antes de tocar no dongle.
- **Encerramento do servidor**: `run.py` registra handlers de `SIGINT`/
  `SIGTERM` e `atexit` que chamam `SDRManager.shutdown()` (para tudo) e
  fecham o banco antes de sair.
- **WAV nunca corrompido**: o arquivo é fechado (`wave.close()`) tanto ao
  detectar fim de transmissão quanto ao parar o rádio ou o servidor.
- **SQLite multi-thread**: uma única conexão, protegida por lock
  (`app/database.py`); todas as threads (poll do ADS-B, loop de áudio do
  rádio, requisições HTTP) passam por ela.
- **Sem chamadas bloqueantes no request thread**: leitura de `aircraft.json`
  e do PCM do `rtl_fm` rodam em threads dedicadas; as rotas HTTP só leem
  caches em memória ou fazem queries rápidas no SQLite.
- **dump1090/rtl_fm morrendo**: os loops de poll detectam
  `proc.poll() is not None` e reportam erro em `/api/status`, exibido como
  toast na interface.

---

## 11. Troubleshooting

**"usb_claim_interface error" / "device busy"**
Outro processo já tem o dongle aberto. Confira:
```bash
sudo systemctl status dump1090-mutability   # deve estar "inactive"/"disabled"
ps aux | grep -E "dump1090|rtl_fm|SDR"
```
Mate manualmente se necessário (`pkill -f dump1090-mutability`,
`pkill -f rtl_fm`) e reinicie `python3 run.py` (ele também tenta limpar
isso sozinho no início).

**"kernel driver attached" / "usb_open error"**
O driver `dvb_usb_rtl28xxu` do kernel pode capturar o dongle antes do
`librtlsdr`. Confirme que a regra em `/etc/udev/rules.d/20-rtlsdr.rules`
existe (normalmente já vem com o pacote `rtl-sdr`) e faça
replug do dongle. Em último caso: `sudo modprobe -r dvb_usb_rtl28xxu`.

**"Permission denied" ao abrir o dispositivo USB**
Confirme que seu usuário está no grupo `plugdev` (`groups $USER`) e que a
regra udev de `rtl-sdr` está instalada; depois, faça logout/login (ou
replug do dongle).

**"dump1090 já rodando" mesmo tendo fechado a aplicação**
Verifique `systemctl status dump1090-mutability` (seção 1.1) — o serviço
systemd pode ter reiniciado no boot. Desabilite-o permanentemente.

**SDR++ (ou outro programa) usando o dongle**
Só um programa pode ter o RTL-SDR aberto por vez. Feche o SDR++ (ou
qualquer outro SDR software) antes de usar esta aplicação, e vice-versa.

---

## 12. Estrutura do projeto

```
luliradar/
  run.py                    # ponto de entrada (signal handlers, cleanup, Flask.run)
  requirements.txt
  .env.example               # copie para .env e configure RECEIVER_LAT/LON
  app/
    config.py                 # Settings (env vars / .env)
    database.py                # SQLite thread-safe + schema
    distance.py                 # Haversine
    models.py                    # SDRState, normalização do aircraft.json
    state_classifier.py           # inferência simples de estado de voo
    procutil.py                    # terminate_process / cleanup_stray_processes
    sdr_manager.py                  # exclusividade mútua ADS-B <-> Rádio
    adsb_service.py                  # processo dump1090 + polling do aircraft.json + DB
    radio_service.py                  # processo rtl_fm + VAD + gravação WAV + DB
    routes.py                          # todas as rotas HTTP (páginas + API)
    __init__.py                         # application factory
  templates/index.html          # SPA única (troca de painel via JS)
  static/app.js, style.css
  config/frequencies.json        # presets de rádio (Torre/ATIS/Solo/... de SBFL)
  tests/                          # testes automatizados (sem tocar no dongle)
    fakes.py                        # FakeProcess/FakePopenFactory (substituem subprocess.Popen)
    test_sdr_manager.py              # máquina de estados: A-G, P, Q, órfãos, SIGKILL
    test_radio_params.py              # resolução de preset, unidades MHz->Hz
    test_http_smoke.py                 # rotas HTTP fim-a-fim
    test_frontend_format.mjs            # formatação de frequência no JS (node)
  data/aviation.db                # criado automaticamente
  data/dump1090_json/              # aircraft.json do dump1090 (criado automaticamente)
  recordings/AAAA-MM-DD/            # gravações WAV (criado automaticamente)
```

### Banco de dados (SQLite)

- `aircraft` — última informação conhecida de cada ICAO.
- `flight_sessions` — cada passagem de uma aeronave pela cobertura do receptor.
- `positions` — histórico de posições (trilha), com throttling para não
  gravar pontos redundantes.
- `adsb_messages_summary` — snapshot periódico de estatísticas globais.
- `radio_recordings` — cada gravação automática de rádio.

A tabela `radio_recordings.start_time` e `positions.timestamp`/
`flight_sessions` já ficam no mesmo formato de timestamp (ISO 8601 UTC),
propositalmente, para permitir cruzar transmissões de rádio com a posição
das aeronaves no momento — próxima etapa natural deste projeto.

---

## 13. Fora do escopo desta primeira versão

Deliberadamente **não** implementado (arquitetura deixada aberta para o
futuro): transcrição de áudio (Whisper), correlação automática áudio↔aeronave,
machine learning, APIs externas de rastreamento, scanner de frequências,
FFT/waterfall, Docker, React, WebSocket, Redis, PostgreSQL.
