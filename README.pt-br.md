# git-sync

Ferramenta CLI concorrente e ultrarrápida para auditoria de status e sincronização em lote de múltiplos repositórios Git em workspaces locais.

Projetada tanto para uso humano no terminal quanto por **agentes de IA** (como Antigravity), minimizando o consumo de tokens e tempo de resposta.

---

## Recursos Principais

- **Execução Concorrente:** Varre dezenas de repositórios em paralelo via `ThreadPoolExecutor` do Python (audita mais de 30 repositórios em ~2 segundos).
- **Zero Dependências Externas:** Construído exclusivamente com a biblioteca padrão do Python 3.12+ (sem necessidade de `pip install`).
- **Prevenção de Travamentos:** Suprime prompts interativos com `GIT_TERMINAL_PROMPT=0` e aplica timeouts automáticos.
- **Sincronização Segura:** Realiza `pull` com `--ff-only` por padrão e sincroniza atomicamente branches auxiliares de tracking sem trocar a working tree.
- **Token-Dense para Agentes de IA:**
  - `--json`: Retorna JSON estruturado e compacto com `pull_pending`, `push_pending`, `dirty`, `errors`.
  - `--pending`: Exibe apenas repositórios com pendências.
  - `--no-fetch`: Inspeção local instantânea (<0.5s).

---

## Instalação Global

Execute o comando instalador seguro e idempotente:

```bash
# Instalação automática em ~/.local/bin/git-sync
git-sync install

# Ou execute o script de bootstrap diretamente do repositório
./scripts/install.sh
```

Para habilitar o autocompletar no terminal (Bash e Zsh):

```bash
# Autocompletar no Bash:
git-sync completion bash --install
# ou carregar na sessão atual:
eval "$(git-sync completion bash)"

# Autocompletar no Zsh:
git-sync completion zsh --install
```

---

## Exemplos de Uso

```bash
# Status completo com fetch paralelo em todos os repositórios
git-sync status

# Status local instantâneo (sem requisições de rede)
git-sync status --no-fetch

# Exibir apenas repositórios com pendências (pull/push/dirty)
git-sync status --pending

# Formato JSON para consumo por agentes
git-sync status --pending --json

# Sincronizar todos os repositórios pendentes
git-sync pull --all

# Sincronizar repositórios específicos
git-sync pull taskctl template-agent

# Enviar commits de todos os repositórios com pendência de push (com simulação dry-run)
git-sync push --dry-run --all

# Enviar commits de todos os repositórios pendentes
git-sync push --all

# Enviar commits de repositórios específicos
git-sync push taskctl template-agent

# Listar arquivos modificados/não comitados em todos os repositórios
git-sync dirty
```

---

## Testes & CI/CD

Execução dos testes canônicos e checagem de sintaxe:

```bash
# Executar todos os testes unitários
python3 -m unittest discover -s tests

# Verificar compilação e sintaxe
python3 -m py_compile src/git_sync/*.py tests/*.py scripts/*.py
```

A integração contínua (CI) automatizada está configurada em `.github/workflows/ci.yml`, cobrindo matriz de testes em Python 3.10, 3.11, 3.12 e 3.13, verificação de higiene git, execução da CLI, testes do instalador e auditoria de metadados com Twine.

---

## Empacotamento de Release e Higiene de Distribuição

Gere e valide pacotes de distribuição fonte (`sdist`) e binários (`wheel`) com zero dependências externas em tempo de execução:

```bash
# Construir e auditar pacotes de distribuição
python3 scripts/build.py

# Auditar pacotes existentes em dist/
python3 scripts/build.py --check-dist

# Limpar artefatos de build transitórios
python3 scripts/build.py --clean

# Instalar o pacote wheel localmente
pip install dist/git_sync-*.whl
```
