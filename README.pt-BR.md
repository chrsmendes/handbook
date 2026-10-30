# Acompanhamento do Manual Geral

[English](README.en.md) | [Português (Brasil)](README.pt-BR.md)

Este projeto acompanha as mudanças no Manual Geral de A Igreja de Jesus Cristo
dos Santos dos Últimos Dias usando arquivos Markdown e `git diff`. É um projeto
independente, iniciado por [Easton Potokar](https://github.com/contagon), sem vínculo
oficial com a Igreja.

## Idiomas e fontes

O conteúdo do manual vem exclusivamente da publicação oficial da Igreja no idioma
solicitado. Não traduzimos os capítulos do inglês. A tradução mantida pelo projeto
se limita à sua própria documentação.

| Idioma | Documentação | Edições |
| --- | --- | --- |
| Inglês | [README](README.en.md) | `editions/AAAA-MM/` |
| Português (Brasil) | Este documento | `editions/por/AAAA-MM/` |

A edição [2026-10](editions/por/2026-10) contém os 39 capítulos em português.
O [resumo oficial de atualizações](https://www.churchofjesuschrist.org/study/manual/general-handbook/summary-of-recent-updates?lang=por)
identifica a edição vigente como outubro de 2026. Os nomes técnicos dos arquivos
seguem os identificadores de URL da Igreja, mesmo quando estão em inglês.

As edições anteriores são buscadas nas capturas das páginas oficiais em português
preservadas pelo Internet Archive. O [relatório de downloads](editions/por/download-status.json)
registra o resultado por edição, incluindo buscas pendentes, incompletas ou
interrompidas por indisponibilidade do serviço. Uma busca interrompida não significa
que a edição não exista no arquivo.

## Instalação

O projeto requer Python 3.8 ou superior. Instale as dependências com `uv sync` ou
com as ferramentas do próprio Python:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

## Sincronizar as edições em português

Para tentar todas as edições configuradas, começando pela mais recente:

```sh
./run.sh por
```

O comando usa a mesma organização por data do inglês: `editions/por/AAAA-MM/`.
A edição mais recente configurada é obtida diretamente do site oficial; as
anteriores exigem capturas históricas dentro do intervalo correspondente. Não há
uma pasta permanente chamada `current`.

Para baixar apenas uma edição:

```sh
./run.sh por 2026-10
./run.sh por 2026-03
```

Os arquivos já baixados são preservados. Assim, executar o comando novamente retoma
os capítulos que faltaram. Para solicitar uma nova cópia dos arquivos existentes,
use `--refresh` na etapa de download:

```sh
.venv/bin/python execute.py --lang por download --date 2026-10 --refresh
.venv/bin/python execute.py --lang por sanitize --date 2026-10
```

Também é possível executar cada etapa separadamente:

```sh
.venv/bin/python execute.py --lang por download --date all
.venv/bin/python execute.py --lang por sanitize --date all
.venv/bin/python execute.py --lang por missing
```

Sem `--date`, os subcomandos usam a edição mais recente configurada, como no inglês.
As opções globais `--lang` e `--dir` devem vir antes do subcomando. `--dir` indica a
pasta-base; por exemplo, `--dir dados --lang por download` grava em
`dados/por/2026-10/` enquanto essa for a edição mais recente.

O script usa `uv` quando disponível; caso contrário, usa `.venv/bin/python` ou
`python3`. Se instalou com uv, substitua `.venv/bin/python` por `uv run python` nos
comandos individuais.

## Origem, lacunas e falhas de download

O sincronizador valida a URL, a identidade do capítulo e o idioma informado pelo
próprio conteúdo. Cada edição tem um `sources.json` com a edição solicitada, a URL
oficial, a URL consultada, o instante da coleta em UTC e o SHA-256 do HTML original.
As URLs do Wayback Machine também registram o instante exato da captura histórica.

As datas em `src/handbook/const.py` delimitam os intervalos de busca das edições
anteriores. Elas não comprovam que todos os capítulos em português tenham sido
atualizados ao mesmo tempo. Ao acrescentar uma edição, confira o resumo oficial no
idioma correspondente antes de direcionar o conteúdo vigente para a nova pasta.

Quando uma captura não é encontrada, o capítulo continua ausente. O comando
`--fill` não é permitido para português: capítulos em inglês ou de outras edições
não são usados para preencher as lacunas.

Se o arquivo histórico estiver indisponível ou limitar as consultas, a execução
interrompe as requisições ao arquivo, registra a falha em `download-status.json`
e deixa as próximas edições pendentes. Os arquivos existentes são preservados, e
o comando retorna um código diferente de zero. Execute `./run.sh por` novamente
quando o serviço estiver disponível.

Falhas de conexão, de tempo limite e erros transitórios do servidor (500/502/503/504)
têm até duas novas tentativas na mesma URL. O script respeita o intervalo entre
consultas ao Internet Archive; a busca de todas as edições pode demorar.

[missing.txt](editions/por/missing.txt) lista os capítulos ainda ausentes localmente
em todas as datas configuradas, incluindo as que ainda não puderam ser consultadas.
O arquivo é recriado a cada verificação, sem duplicatas. Consulte o relatório de
downloads para distinguir a indisponibilidade do serviço de uma busca concluída.

## Comparar alterações

```sh
# Alterações locais na edição de outubro de 2026:
git diff -- editions/por/2026-10

# Duas edições, quando ambas estiverem disponíveis:
git diff --no-index editions/por/2026-03 editions/por/2026-10

# Mudanças entre commits:
git diff <commit-anterior> <commit-novo> -- editions/por
```

As comparações por idioma são geradas automaticamente: [inglês](docs/comparisons-eng.md)
e [português](docs/comparisons-por.md). As tags novas usam `eng/AAAA-MM` e `por/AAAA-MM`;
as tags inglesas antigas são preservadas. Cada snapshot tem um commit separado por
edição e idioma, e nenhuma tag existente é movida.

## Preservação no Wayback Machine

Cada capítulo novo, baixado e validado, entra numa fila de capturas. O script
solicita a captura pelo Save Page Now e acompanha o job. O resultado só é associado
à edição depois de conferir o idioma, a identidade do capítulo e a correspondência
do conteúdo. O endereço definitivo e o timestamp ficam no `sources.json`.

Configure `IA_ACCESS_KEY` e `IA_SECRET_KEY` no ambiente do processo local.
Nunca coloque os valores em arquivos versionados. Sem credenciais, ou quando o
serviço estiver indisponível, os downloads e pedidos pendentes são preservados.
Para retomar sem baixar os capítulos novamente:

```sh
.venv/bin/python execute.py archive --all-languages --max-seconds 900
```

Toda a automação roda localmente. Veja [Automação e configuração](docs/automation.md)
para os comandos, limites, novas tentativas, commits e publicação das tags. A busca
de capturas antigas continua separada da preservação de novos downloads.

## Desenvolvimento

O inglês continua sendo o idioma padrão. `./run.sh` mantém o fluxo inglês
existente. O preenchimento de lacunas com versões próximas é uma característica
legada desse fluxo e não se aplica ao português.

Execute os testes com:

```sh
.venv/bin/python -m unittest discover -s tests
```

Contribuições podem ser enviadas ao [repositório original](https://github.com/contagon/handbook).
