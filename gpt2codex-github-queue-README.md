# gpt2codex GitHub Queue (opt-in prototype v1)

## Issue 20: approved target, implementation incomplete

The new target requires one registered persistent screen per project and one
Codex process per project, shared across Queue, manual start and resume. The
per-issue screen/TUI flow below is legacy behavior, not the new target.
`project-screen` is a reserved explicit backend choice; readiness is NO and
approval/automatic dispatch refuse it without fallback. It is not deployable.
The default and historical approvals are unchanged. An isolated durable
reservation candidate and offline tests are prepared, but Rust process/window
supervision, concurrent worker integration and Qt6 local overview are unfinished.
See [stage status, remaining gates and future E2E](.ai/QUEUE_PROJECT_DISPATCH.md).
Do not apply the older screen rollout recipe below as an issue 20 rollout.

This component is independent of the gpt2codex Rust/Qt GUI and existing GNU
screen/tmux session management. Nothing starts automatically when files are
installed. GitHub is used ONLY as a private message queue, not as an execution
host. Any authorized GitHub client or ChatGPT GitHub connector can create a
request from any computer. Only the single configured Linux workstation polls
and runs Codex. No incoming ports, reverse proxies or MCP tunnels are required.

## Security boundaries

- Use a NEW PRIVATE repository, e.g. `Pitriss/gpt2codex-queue`. Never use the
  public `Pitriss/gpt2codex` source repository as a task queue.
- Issue author must be on the local allowlist. An issue is NOT permission to
  execute. The operator must inspect the exact text and enter a digest-bound
  local approval in an interactive terminal. No remote approval or silent auto-
  acceptance is implemented in v1.
- GitHub issue edits after ingestion/approval invalidate the request. A job is
  claimed locally before starting Codex, so a crash will NOT replay it. Another
  local worker is locked out. The worker token stays ONLY on the execution PC.
- Only exact locally configured project IDs are allowed. No arbitrary shell,
  remote executable paths, arbitrary filesystem roots, `screen -X stuff`,
  session takeover, or caller-chosen command line options.
- A newly approved job runs as a SEPARATE noninteractive `codex exec` with
  `read-only` or `workspace-write` sandbox. It DOES NOT reuse/restart the
  existing persistent Codex TUI or change Attention Manager/P0-M.
- No prompt, complete output, credential, source code or chat transcript is
  automatically posted back to GitHub. New claims also publish a bounded,
  filtered job-specific `G2C-HANDOFF/1` comment under the locally approved
  automatic reporting policy described below. A human may separately publish
  a longer deliberately sanitized summary using `publish-summary`.
- This is a same-UID local trust model, NOT isolation against malicious code
  running as the user. Codex or project tools might read same-UID files if
  allowed by the actual sandbox. Use trusted authors and inspect every request.
  Start with read-only tests. Never put secrets in Issues.
- GitHub Issue edits, account compromise, and malicious task instructions are
  still threats. The local approval is essential. Repo access and company policy
  must permit task text to be stored on GitHub.

## One-time setup

1. Create a **private** empty GitHub repository `Pitriss/gpt2codex-queue` with
   Issues enabled. This requires a separate user action: the available ChatGPT
   GitHub connector does not expose repository creation.
2. Make the new private repository accessible to the GitHub connector used in
   ChatGPT so it can create Issues and read status comments.
3. In that new queue repo, add `github-queue-issue-form.yml` as
   `.github/ISSUE_TEMPLATE/codex-request.yml` and commit it there. This is a
   queue-repo change, not a change to the public gpt2codex source repository.
4. Create a fine-grained GitHub PAT scoped ONLY to the queue repository, with
   repository Metadata(read) and Issues(read/write). Store it in
   `~/.config/gpt2codex/github-queue.token`, owned by your user, mode 0600.
   Use `vim` to enter it, not a shell argument/history entry.
5. Apply the accompanying patch to the local gpt2codex source root. The patch
   only ADDS the queue worker, offline tests, issue form and this README.

The generated configuration is `~/.config/gpt2codex/github-queue/config.json`
inside a dedicated private directory (0700). The existing parent
`~/.config/gpt2codex` is not modified, even if it is mode 0755.
The token stays at `~/.config/gpt2codex/github-queue.token` (0600).

From the local gpt2codex source root (each command is one line):

```sh
python3 ./gpt2codex-github-queue.py init-config
vim ~/.config/gpt2codex/github-queue/config.json
install -m 600 /dev/null ~/.config/gpt2codex/github-queue.token
vim ~/.config/gpt2codex/github-queue.token
python3 ./tests/unit/gpt2codex-github-queue-test.py
python3 ./gpt2codex-github-queue.py check
```

The generated JSON pins the execution hostname, GitHub repo, allowed issue
creators, Codex binary and specific local project roots. Review/edit them
before using the worker. No globally installed services are required.

## Usage

Create an Issue using the repo's form from any GitHub-enabled device, or ask
ChatGPT to create an Issue via its connected GitHub tools. ChatGPT must format
its body EXACTLY as the form does:

```md
### Projekt

kencoder

### Rezim

read-only

### Zadani

Inspect the translation test failure; propose a fix. Do not modify files.
```

The issue title must start with `[g2c] ` and the author must be allowlisted.
The worker recognizes existing Issues but does not automatically execute them.

```sh
python3 ./gpt2codex-github-queue.py poll
python3 ./gpt2codex-github-queue.py list
python3 ./gpt2codex-github-queue.py show 1
python3 ./gpt2codex-github-queue.py approve 1 <first-12-or-more-sha256-characters>
python3 ./gpt2codex-github-queue.py run-approved
```

`approve` requires a physical interactive terminal with the precise text
`SCHVALUJI <issue number>` and rechecks the remote content before accepting.
The worker may also be kept running in the FOREGROUND (not installed as a
service by this patch):

```sh
python3 ./gpt2codex-github-queue.py watch --interval 60
```

It polls and executes only **previously locally approved** requests. It may
issue a local desktop notification if `notify-send` is already installed. Use a
separate terminal for `list/show/approve` while `watch` runs. If the worker
stops during an execution, the job remains `running` and MUST be investigated
manually, never replayed automatically.

Codex output logs stay under `.log/github-queue/`, and temporary queue files
under `.tmp/github-queue/` (the existing bridge-managed links). Durable
idempotency state is pinned to `~/.local/state/gpt2codex/github-queue/`
for both the systemd worker and the interactive CLI. The queue deliberately
ignores `XDG_STATE_HOME`, which Codex may point into temporary `/tmp` sandboxes.
The state must survive reboots and `/tmp` cleanup. No logs or state should be
committed or uploaded. This uses the installed Codex CLI
and its existing local authentication. The documented `codex exec` behavior and
flags must be checked against the installed version before real execution.

After reviewing/redacting an explicit summary file (mode 0600) inside the
project `.tmp/github-queue/` directory, you can send ONLY that exact text
back to the Issue:

```sh
python3 ./gpt2codex-github-queue.py publish-summary 1 .tmp/github-queue/safe-summary.md
```

## Operational limitations

- If the workstation is offline or shut down, Issues remain queued on GitHub;
  they are picked up when the workstation and worker come back.
- If another device uses a different GitHub account, add it to BOTH the repo
  collaborators and local `allowed_authors` list. Never copy the worker PAT.
- There is no GitHub-originated approval or interactive Codex TUI injection.
  Automatic short handoffs apply only to new claims made by the updated worker.
- The worker is a pilot. Do not enable unattended service/autostart until the
  first private-repo + local Codex read-only acceptance test succeeds.

## Optional user service, after manual acceptance testing only

The bundle includes `gpt2codex-github-queue.service` for the known workstation
path. This file is NOT installed or activated automatically. On Debian 13,
after confirming a real private-repo read-only run, you may install it as a
user unit (no sudo; adjust the source root if necessary):

```sh
install -Dm600 ./gpt2codex-github-queue.service ~/.config/systemd/user/gpt2codex-github-queue.service
systemctl --user daemon-reload
systemctl --user enable --now gpt2codex-github-queue.service
systemctl --user status gpt2codex-github-queue.service --no-pager
```

The service does not add login linger. Without linger it runs during the user
manager's lifetime, normally while you are logged in. Never enable lingering
without a separate decision. Service output goes to `.log/` as requested.

## Automatický stručný handoff (2026-11-10)

Výslovně schválené automatické publikování nevyžaduje další `PUBLIKUJI`.
Po novém claimu Issue #13+ aktualizovaný worker zachová `G2C-QUEUE/1`
(worker, schválený SHA, status, exit code) a přidá samostatný `G2C-HANDOFF/1`
do **stejné** Issue, i když už je uzavřená. Status DONE/FAILED pochází z workeru,
nikoli z Issue nebo modelového reportu. UNKNOWN sděluje jen známý stav a neuvádí
neověřený exit code. Automatický status není důkaz, že všechny testy prošly.

Změna se aktivuje při novém claimu, nikoli migrací stavu. Hotové #1–#12 se
nikdy nedoplňují. Právě běžící #13 spuštěná starým procesem se nezmění;
první praktické ověření musí být nová Issue po načtení aktualizovaného workeru.
SHA approval, jednorázový claim, stdin prompt, timeout a backend `codex exec`
zůstávají zachované. Issue formát ani globální AGENTS se nemění.

Exec dostane závazný reportovací kontrakt za původním zadáním a dvě volby,
ověřené v lokálním `codex exec --help`: `--output-schema` a
`--output-last-message`. Finální JSON ukládá hostitelský CLI do 0600 artefaktu
`.log/github-queue/handoff-N-NONCE.final.json`; schema je v existující
`.tmp/github-queue/`. Read-only model nepíše do projektu a nedostává další
zapisovatelné cesty. Finální JSON obsahuje jen práci tohoto turnu: `implemented`,
`files`, `tests` (name/status), `limitations`, `next_step` a přesnou vazbu
`issue`, `request_sha256`, `run_nonce`. Status komentáře se z JSON nikdy nebere.
[Oficiální popis strukturovaného finálního výstupu](https://learn.chatgpt.com/docs/non-interactive-mode)
je podpůrná dokumentace, nikoli hostitelský E2E důkaz.

Publisher nečte stdout/stderr, diff, prompty ani historický
`.ai/.zzz.handoff-result.md`. Povoluje nanejvýš 8 položek na sekci a 220 znaků
na položku; vstupní report má limit 8 KiB, komentář 6 KiB v UTF-8. Ověřuje
schema, vazbu na konkrétní turn, duplicity JSON klíčů, soubor/owner/mode,
relativní názvy souborů a rozpor read-only vs hlášené změny. Normalizuje NFKC,
escapuje Markdown a odmítá řídicí/hidden znaky, nové řádky uvnitř polí,
protokolové markery, URL, absolutní cesty, konfigurační/kódovou syntaxi,
citlivé názvy a běžné secret/token/key vzory i dlouhé neprůhledné řetězce.
Podezřelá jediná položka vyřadí **celý** detail, bez částečného salvagingu.
Prozaický whitelist zahrnuje také pomlčky U+2013 a U+2014. Právě U+2014
ve validním finálním JSON read-only #14 dříve chybně vyvolala fallback;
capture přes CLI i identita reportu byly lokálně ověřeny jako správné.
Komentář má rubriky Provedeno, Výsledek, Změněné soubory, Testy, Omezení
a Další krok. Výsledek je workerem známý stav/exit, nikoli odhad úspěchu testů.
Prázdný seznam souborů znamená žádné dle reportu; prázdné testy jsou neuvedené.
Fallback zachovává Issue, SHA, stav/exit a `Podrobný handoff: nedostupný`;
v rubrikách přiznává neověřitelné či neuvedené údaje, bez domyšlených souborů
nebo PASS. Staré uložené komentáře se nepřerenderují ani znovu nepublikují.

Tyto kontroly nejsou dokonalý DLP filtr ani důkaz pravdivosti modelu. Detail je
výslovně označen jako report Codexu bez nezávislého ověření. Krátký přirozený
text stále závisí na důvěryhodném autorovi a lokálním posouzení úlohy;
schema ani regex nemohou zaručit zachycení každého tajemství zakódovaného do
běžné řeči. Nepovolujte úlohy žádající přenos privátních dat. Model a nástroje
stejného UID mohou měnit lokální stav; tato změna nepřidává OS izolaci.
Opt-in screen backend ani jeho sloty se nemění: pro jeho nové claimy je
automatický handoff pouze bezdetailový, protože tento patch nesbírá jeho JSON
události a nemění jeho finální výstup.

Bezpečný již vyrenderovaný komentář a terminal result jsou uloženy společně
v existujícím durable queue state. Pole `handoff` obsahuje `phase`, `attempts`,
pevné `diagnostic` a `report_diagnostic` a bezpečné `body`. Marker váže Issue number + immutable ID +
celý schválený SHA. Publikace drží stávající state lock, kontroluje privátní
repo, immutable Issue ID a všechny stránky komentářů. Za potvrzení přijme
pouze přesný bezpečný komentář od identity současného tokenu (`GET /user`);
podvržený blok v zadání nebo komentář jiného účtu nestačí. Stávající token
se nemění a čtení `/user` vyžaduje dostupnou identitu uživatelského PAT.

Před POST se trvale uloží `posting`. Po ACK je `published`. Po explicitním
HTTP odmítnutí (400/401/403/404/405/410/422/429) zůstane `pending` pro další
publikaci. Po timeoutu, 5xx, pádu nebo nejasném doručení je `uncertain`:
další běh pouze dohledá přesný komentář. Jestli jej nenajde, **neposílá druhý
POST**, protože GitHub Comments nemají atomický idempotency klíč a první
požadavek může stále doběhnout. Totéž platí pro pád po uložení `posting`
před POST; upřednostněna je absence duplicity. Stav vyžaduje operátorské
vyšetření, nikoli změnu state naslepo nebo opakování modelového promptu.
Po změně účtu/token identity se starý komentář automaticky nepotvrzuje.
`watch`/`run-approved` opakují pouze bezpečnou publikaci/reconciliation outboxu;
`poll` nadále jen ingestuje požadavky. Smazání volatilních artefaktů nevadí
publikaci uloženého body a nikdy neopravňuje nový Codex turn.

Lokální kontrola (bez tokenu a bez síťového volání) a samostatný publikační
pokus pro konkrétní nové Issue N; nahraďte N jeho číslem:

```sh
python3 -B ./gpt2codex-github-queue.py handoff-status N
python3 -B ./gpt2codex-github-queue.py publish-handoff N
```

První příkaz zobrazí bezpečné body a diagnostiku. Druhý pouze reconciliuje
nebo odešle již uložený bezpečný handoff, nepotřebuje nový ruční souhlas a
nemá cestu ke spuštění Codexu. Vrací 0 pro potvrzenou publikaci, jinak 2.
Ruční `publish-summary N FILE.md` zůstává samostatnou cestou pro **delší,
vědomě zkontrolovaný** text s přesným interaktivním `PUBLIKUJI N`; automatický
publisher ho nevolá. Nemazat durable state ani claim pro opravu publikace.

Offline ověření bez skutečného GitHub POST, Codex turnu či relace:

```sh
PYTHONDONTWRITEBYTECODE=1 TMPDIR="$PWD/.tmp" python3 ./tests/unit/gpt2codex-queue-handoff-test.py > .log/queue-handoff-tests.log 2>&1
PYTHONDONTWRITEBYTECODE=1 TMPDIR="$PWD/.tmp" python3 ./tests/unit/gpt2codex-queue-screen-test.py > .log/queue-handoff-screen-regressions.log 2>&1
```

Aktuální source checkout potřebuje `gpt2codex-github-queue.py` i nový
`gpt2codex_queue_handoff.py` vedle sebe. Historické v1 patch/ZIP nejsou tímto
patchem přebalené. Neprovádí se žádná instalace ani změna služby.

### Načtení změny na 9h259 a jeden praktický read-only E2E

**Služba nebyla restartována.** Starý Python proces nový kód nenačte. Operátor
nejprve počká na dokončení #13 a ověří stav ve vlastním hostitelském terminálu:

```sh
python3 -B ./gpt2codex-github-queue.py list
systemctl --user status gpt2codex-github-queue.service --no-pager
```

Pokračovat pouze po potvrzení idle: žádné running/approved/approved-screen,
žádný aktivní Codex exec potomek služby, žádné souběžné nové schvalování.
Stale/unknown není důkaz idle; při nejasnosti STOP a ruční kontrola konkrétního
jobu. Nepřipojovat ani neukončovat jiné screen/tmux relace. Teprve po tomto
ověření operátor sám načte změnu (nevyžaduje daemon-reload, unit se nezměnila):

```sh
systemctl --user restart gpt2codex-github-queue.service
systemctl --user status gpt2codex-github-queue.service --no-pager
```

Pak vytvořit **jednu novou** Issue s titulkem `[g2c] read-only handoff smoke`,
projektem `gpt2codex`, režimem `read-only` a zadáním: „Bez zápisů ověř, zda
v kořeni projektu existuje gpt2codex-github-queue.py. Stručně uveď skutečné
zjištění; testy ani síťové operace nespouštěj.“ Použít dosavadní Issue formát.
Po poll/show schválit právě její SHA obvyklým místním postupem (N a SHA
nahradit zobrazenými hodnotami):

```sh
python3 -B ./gpt2codex-github-queue.py poll
python3 -B ./gpt2codex-github-queue.py show N
python3 -B ./gpt2codex-github-queue.py approve N SHA
```

Aktualizovaná služba provede právě jeden exec. Po dokončení přímo ve stejné
Issue očekávat původní `G2C-QUEUE/1` a právě jeden `G2C-HANDOFF/1` s konkrétním
zjištěním, bez změněných souborů, bez vymyšlených testů a se shodným SHA.
Při fallbacku se bezpečnost zachovala, ale praktický detailní E2E neprošel;
ověřit pouze lokální diagnostiku `handoff-status N`, prompt automaticky neopakovat.
Znovu `publish-handoff N` musí potvrdit existující komentář bez duplicity.
Neprovádět automatický re-run staré Issue #13, publikaci #1–#12 ani další smoke.
Tento skutečný hostitelský E2E je **NOT RUN**; offline test není jeho náhrada.

## Opt-in screen MVP (2026-11-09; host E2E NOT PROVEN)

Nový backend běží pod stejným Linux uživatelem a existujícím ChatGPT loginem.
Odděluje task slot, proces a screen socket; nepřidává nový OS sandbox.
Default `approve`, `run-approved` a `watch` dál používají původní `codex exec`.
Služba se nepřepíná ani nerestartuje. Rust ani systémová instalace se nemění.

Na hostu **9h259** vytvořte běžný read-only Issue, například s úlohou
„Odpověz pouze QUEUE_SCREEN_MVP_OK; nepoužívej nástroje a nic neměň.“
Po běžném `poll` / `show NUMBER` použijte místní SHA schválení s opt-in:

```sh
bash ./gpt2codex-queue-screen-once.sh NUMBER SHA256_PREFIX
```

Helper provede interaktivní `approve NUMBER SHA --backend screen` a pak
jednorázový worker `run-screen NUMBER`. Stav `approved-screen` rezervuje úlohu
pro tuto cestu: starý běžící worker ji ignoruje. Pokud helper skončí mezi
schválením a claimem, lze spustit `python3 gpt2codex-github-queue.py run-screen NUMBER`.
Po claimu (`running`, `done`, `failed` nebo `unknown`) není replay povolen.
Selhání neopravňují nový pokus se stejným promptem. U vlastního configu použijte
obě odpovídající CLI operace s existujícím `--config /absolute/config.json`.

Nový slot `qNUMBER-NONCE` vznikne v `.log/github-queue/screens/`, jeho vlastní
`SCREENDIR` je podadresář `s/`. Názvy ani registry projektových relací se nepoužijí.
Prompt má 0600 v `.tmp/github-queue/` a jde výhradně přes stdin do `exec --json -`.
Backend odmítne chybějící ChatGPT login; odstraní API/GitHub key env a nastaví
OpenAI provider a `approval_policy=never`. Neprovádí login/logout ani API fallback.
Předává schválený `read-only` / `workspace-write` také do resume.

Po přesně jednom `thread.started`, jednom `turn.started`, jednom `turn.completed`
a exit 0 worker zaznamená dokončení turnu. Teprve potom wrapper ve stejném screen
spustí `codex resume UUID`, bez promptu. Běžící screen sám neznamená úspěch.
TUI se během exec neslibuje. `done` popisuje první turn; TUI může dál čekat na vstup.
Soukromá `events.jsonl` měří první turn; počet dalších interaktivních turnů zatím
není automaticky měřen. Žádný surový výstup ani UUID nejde automaticky na GitHub.

Otevření konkrétní úlohy v nově sestaveném Qt6, bez autoattach jiných relací:

```sh
./target/release/gpt2codex-gui --bridge-cli "$PWD/target/release/gpt2codex" --queue-helper "$PWD/gpt2codex_queue_screen.py" --queue-only
```

Po dokončení exec vyberte Issue a **Otevřít vybranou úlohu**. Během exec je attach zakázán, aby čekající vstup nezaložil další turn. V běžném GUI stejné přepínače bez
`--queue-only` přidají nabídku **Relace → GitHub Queue úlohy…**; běžný režim zachová
stávající Bridge autoattach. Queue okno má vlastní terminál a nenahrazuje projektovou
záložku. Zavření terminálového okna odpojí klienta. Žádný force-detach nebo restart;
pokud je slot připojen jinde, `screen -r` připojení odmítne.

Diagnostika bez GitHub tokenu, queue konfigurace a starých mux registrů:

```sh
python3 gpt2codex_queue_screen.py list
python3 gpt2codex_queue_screen.py attach qNUMBER-NONCE
```

Listing rozlišuje `issue_accepted`, `exec_starting`, `turn_running`, `turn_finished`,
`tui_starting`, `tui_ready` (pouze vizuálně potvrzené), `tui_exited`, `tui_failed`, `launch_failed`
a `launch_unknown`; ztráta serveru během aktivní fáze je `interrupted_unknown`. `attachable=false` znamená chybějící nebo neověřenou identitu.
Attach revaliduje celý ID, PID/start ticks, boot ID a device/inode vlastního socketu.
`resume.json` zaznamenává přesný UUID a absenci promptu; `result.json` dokončení
prvního turnu. Samotný start procesu TUI je vždy **NOT PROVEN** pro readiness.
Po vizuálním ověření stejného thread UUID a vstupní řádky v Qt6 lze uložit důkaz:

```sh
python3 gpt2codex_queue_screen.py confirm-ready qNUMBER-NONCE
```

Potvrzení vyžaduje místní interaktivní terminál a zobrazený správný UUID; není to
mock ani automatická detekce. Pro skutečné pokračování napište další úlohu přímo
do otevřeného Codex TUI. Toto je nový, uživatelem zadaný turn stejné konverzace.
Po TUI exit není automatické znovuspuštění. Volatilní metadata nejsou recovery
mechanismus; při ztrátě `.log` trvá durable queue claim a nic se nereplayuje.

Cílené ověření (mock není E2E důkaz):

```sh
PYTHONDONTWRITEBYTECODE=1 TMPDIR="$PWD/.tmp" python3 tests/unit/gpt2codex-queue-screen-test.py
python3 gpt2codex-queue-screen-smoke.py
```

Druhý příkaz vytváří pouze nový syntetický screen a sám ukončí svůj fake TUI.
Volitelný **jeden skutečný read-only smoke** bez GitHub credentials/state:

```sh
python3 gpt2codex-queue-screen-smoke.py --real --approve-read-only-smoke
```

Tento skutečný smoke není náhrada testu přijatého Issue. Při selhání ho neopakujte
bez nového explicitního schválení. V sandboxu této implementace vlastní AF_UNIX
bind vrací EPERM a nový syntetický screen skončil před Codexem. Skutečný turn,
resume UUID a živý Qt6 attach jsou **NOT RUN / NOT PROVEN**; Definition of Done
zatím není splněna. Na hostu proveďte jednu přijatou read-only úlohu příkazem výše,
pak Qt6 attach a vizuální kontrolu. Nepřidávejte oprávnění do této Codex relace.

Návrat na původní backend: další Issue schvalujte bez `--backend screen` a použijte
běžný worker. Dosud neclaimnutý `approved-screen` lze místně `reject NUMBER`;
nezmění se potichu na `exec`. Již spuštěný prompt se znovu nespouští. Ponechání
nového slotu nemění primární projektovou relaci ani službu.

## Automatický screen backend v jediném workeru (Issue 16)

Tato implementace nahrazuje výše uvedený ruční screen MVP pro běžný provoz.
Produkční konfigurace ani běžící služba nebyly v tomto jobu změněny.
`execution_backend` v existujícím privátním queue `config.json` přijímá pouze
`exec` nebo `screen`. Chybějící volba znamená `exec`; neplatná hodnota je chyba.
CLI `approve NUMBER SHA12` načte tento default a při místním schválení závazně
uloží backend a worker host vedle identity Issue, SHA a projektu. Explicitní
`--backend exec` i `--backend screen` mají přednost. Změna defaultu nepřepisuje
schválené ani běžící joby.

Stejný `watch` nyní sekvenčně vybírá `approved` i `approved-screen`.
`run-approved`, `run-screen` a watch sdílejí neblokující execution lock;
jednorázový durable claim se ukládá před spuštěním. Nevyřešený `running` claim
blokuje další spuštění, včetně stavu po pádu workera. Restart nikdy neopakuje
Codex. Neexistuje automatická oprava nejasného výsledku ani automatická změna
backendu. Dříve ručně schválený `approved-screen` může nový worker převzít:
před restartem proto musí být queue skutečně idle.

Nový slot má vlastní screen server i socket directory. Před startem se do
queue state uloží jeho vazba na schválený job; runner při přijetí `thread.started`
uloží UUID do téhož durable state. Ověřuje Issue ID, SHA, projekt, host, backend
a slot. Výsledek prvního turnu je samostatný `result.json`; `done` vyžaduje
exit 0, UUID a právě jeden zahájený a dokončený turn. Následný `codex resume UUID`
neobsahuje prompt. Qt6 používá stávající GitHub Queue záložku, filtr a attach guard.
Projektové registry, session ani sockety se při tom neprohledávají a nemění.

Oba backendy dostávají stejné schéma a per-turn report identity. Host CLI ukládá
finální report mimo modelový tool sandbox i u read-only jobu. Používá se původní
sanitizér, fail-closed fallback a durable outbox pro automatický handoff.
Publication-only reconciliation nikdy nespouští Codex. Po nejistém POST se komentář
znovu neposílá; hledá se potvrzení již odeslaného obsahu.

Queue state je pevně pod uživatelským `.local/state/gpt2codex/github-queue`;
`XDG_STATE_HOME` jej nepřesměruje. Nový screen i jeho Codex potomci dostanou
standardní uživatelský XDG state a ignorují zděděné testovací XDG nastavení.
Zděděné `SCREENDIR`, STY a TMUX se odstraňují, screen dostane jen nový slotový
socket directory. Stávající relace se nikdy neadoptují. Logy a screen metadata
zůstávají v existující `.log`, scratch v `.tmp`; ztráta volatilních artefaktů
neodstraní durable claim a neopravňuje replay.

### Jednoduchý postup operátora a rollback

1. Po dokončení tohoto jobu zastavte souběžné schvalování. Ověřte idle pomocí
   `python3 -B gpt2codex-github-queue.py list`,
   `python3 -B gpt2codex-github-queue.py worker-ready` a
   `systemctl --user status gpt2codex-github-queue.service --no-pager`.
   Nesmí být running/approved/approved-screen ani nejasný/stale/unknown job
   nebo aktivní Codex exec potomek služby. Živá dokončená resume TUI sama
   neznamená běžící první turn. Nejasný stav řešte lokálně, nemažte claimy.
2. V existujícím queue configu lokálně nastavte `execution_backend` na `screen`.
   Například otevřete `vim ~/.config/gpt2codex/github-queue/config.json`.
   Spusťte znovu `python3 -B gpt2codex-github-queue.py worker-ready`.
   Vyžadujte `WORKER_READY=YES BACKEND=screen IDLE=YES`. Toto je offline preflight,
   nikoli důkaz funkčního screen, přihlášení nebo E2E. Nedumpujte celé prostředí
   služby; případně kontrolujte jen XDG_STATE_HOME a SCREENDIR jejího MainPID.
3. Operátor restartuje právě jednu idle službu příkazem
   `systemctl --user restart gpt2codex-github-queue.service`.
   Unit se neměnila; daemon-reload není potřeba. Nepoužívejte screen-once ani
   další watcher. Provedení tohoto restartu není součástí implementačního jobu.
4. Vytvořte jednu novou jednoduchou read-only Issue, nechte ji načíst pollingem,
   ověřte její SHA přes `show NUMBER` a schvalte ji běžným
   `python3 -B gpt2codex-github-queue.py approve NUMBER SHA12`.
   Bez dalšího runner příkazu vyčkejte na dokončení prvního turnu. Ověřte durable
   vazbu, právě jeden receipt a jeden věcný handoff bez fallbacku. V existující
   Qt6 záložce GitHub Queue otevřete nový slot a vizuálně ověřte stejné UUID
   a idle TUI. Volitelné `confirm-ready SLOT` uchová místní vizuální důkaz.

Rollback: při ověřeném idle nastavte stejnou volbu zpět na `exec` a restartujte
stejnou službu. Pro jednotlivé nové schválení lze vždy použít `--backend exec`.
Již schválený screen job se na exec nepřevádí: neclaimnutý lze `reject NUMBER`,
pak je nutná nová schválená Issue. Běžící/unknown job se nesmí znovu spouštět.
Dokončené screen TUI ponechte otevřené; rollback je neukončuje.

Pokud nový screen nejde vytvořit, slotové `launch-error.json` a `phase.json`
obsahují omezenou diagnostiku, queue zůstane failed/unknown podle známého výsledku
bez replaye. Zkontrolujte dostupnost screen, vlastní nový socket directory,
service environment a lokální log; nepoužívejte cizí sockety ani force-detach.

### Skutečně ověřeno a hranice důkazu

Offline testy používají fake GitHub, fake Codex a nové privátní fixture adresáře.
Ověřují default/fallback konfigurace, vazbu approval, watch dispatch, společný
zámek, restart bez replaye, výsledek a resume UUID, automatický věcný handoff
právě jednou i u read-only a očištění prostředí. Qt6 fixture testy používají
MemoryBackend; nepřipojují se k živým slotům.

V tomto sandboxu nebyl dostupný systemd user bus; environment skutečné služby
ani její efektivní runtime připravenost nelze doložit. Žádný restart, změna
produkčního configu, živý screen start, Codex turn nebo skutečný Qt6 attach
neproběhl. Operátorský E2E po přepnutí backendu je **NOT RUN / NOT PROVEN**.
