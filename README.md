# Fluxo Imagens

Aplicativo local em Python para planejar colecoes, preparar prompts, gerar candidatos por workflows API importados no ComfyUI, selecionar e revisar imagens, ampliar selecionados, sugerir metadados e preparar pacotes para envio manual ao Adobe Stock.

## Requisitos

- Windows e Python 3.11 ou superior.
- ComfyUI local, quando for usar geração/upscale. O app consulta `/system_stats`, `/queue`, `/history`, `/object_info`, `/prompt`, `/upload/image` e `/view` da API local.
- LM Studio local, quando for usar agentes. Modelos e capacidade visual são descobertos por `GET /api/v1/models`; o agente usa `POST /v1/chat/completions`. Uma capacidade `vision: true` é necessária para as funções que enviam imagem.

O diagnóstico feito durante a implementação encontrou Python 3.13.3 no app, Intel UHD Graphics e NVIDIA GeForce RTX 4060 Laptop GPU com 8 GiB de VRAM. ComfyUI 0.38.0 estava disponível em `127.0.0.1:8188`; LM Studio estava disponível em `127.0.0.1:1234` com seis modelos descobertos. Os modelos declarando visão eram `gemma-4-e4b-it`, `qwen/qwen3.5-9b` e `google/gemma-3-12b`; nenhum estava carregado no momento da inspeção. O painel consulta o estado atual em vez de tratar esse snapshot como permanente.

## Instalação e início

O projeto usa o pacote compartilhado [`agentkit`](../agentkit/README.md) (clientes LM Studio/ComfyUI, pipeline de agentes, trava de GPU entre processos e painel central). Ele precisa estar em `D:\Python Apps\agentkit`, ao lado desta pasta; o `requirements.txt` o instala em modo editável (`-e ../agentkit`).

Execute `iniciar.bat`. O script verifica Python, cria `.venv` se necessário, instala as versões fixadas em `requirements.txt`, roda `pip check` e inicia a interface em `http://127.0.0.1:8501`. Se a porta já estiver ocupada, feche o outro processo Streamlit ou altere `--server.port` no script.

Para iniciar manualmente:

```powershell
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501
```

Crie sua configuração copiando `config.example.json` para `config.json` ou salve os campos pela aba **Configuração**. Modelos podem ser escolhidos entre os IDs descobertos no servidor. O agente consulta `/api/v1/models` e carrega um modelo descarregado pelo endpoint oficial `/api/v1/models/load` somente quando um agente é acionado; carga (padrão 600 s) e inferência (padrão 300 s) têm timeouts independentes. O planejamento roda em segundo plano para manter a interface responsiva. Enquanto executa, o painel atualiza automaticamente a fase atual e o tempo decorrido; durante a inferência sem streaming, mostra que aguarda resposta e o timeout configurado, não tokens gerados. URLs, nomes de modelo e limites podem ser sobrescritos pelas variáveis `FLOW_COMFY_BASE_URL`, `FLOW_LMSTUDIO_BASE_URL`, `FLOW_PLANNER_MODEL` e `FLOW_VISION_MODEL`. Se a autenticação estiver habilitada no LM Studio, defina `LM_STUDIO_API_KEY` no ambiente do processo; não grave chaves em `config.json`.

## Coleção automática com agentes

1. Importe uma vez o workflow de geração em **Configuração → Workflows ComfyUI** marcando **Usar como padrão para todas as coleções** (veja abaixo). Workflows padrão ficam em `data/workflows/` e valem para qualquer coleção que não tenha o seu próprio.
2. Em **Painel → Nova coleção**, informe nome, tema, objetivo, público, orientação, aplicação comercial e quantidade de imagens (1 a 20), escolha os modelos e clique em **Criar coleção e acionar agentes**.
3. Os agentes rodam em segundo plano, um de cada vez:
   - **Planejador (LM Studio)** analisa o briefing e devolve um resumo da estratégia, um guia de estilo comum e, para cada imagem, conceito, assunto, composição/espaço para texto, orientação, prompt e negative prompt. Pedidos acima de 5 imagens são divididos em lotes que recebem os conceitos anteriores para evitar repetição.
   - **Gerador (ComfyUI)** cria as imagens com seed aleatória nas dimensões configuradas para cada orientação.
   - **Revisor visual (LM Studio com visão)** dá um parecer para cada imagem (aprovar, revisar ou recriar).
   - **Refinador de prompt** reescreve o prompt quando o revisor reprova todas as imagens de um conceito, e o gerador recria. O número de rodadas automáticas é configurável (padrão 1).
4. Acompanhe na aba **Agentes** (e no resumo da barra lateral) o estado de cada agente, o progresso, a fase atual, o tempo decorrido e o log. É possível cancelar; a tarefa em andamento termina antes de parar. Se o app for reiniciado durante a execução, ela aparece como *interrompida* e **Retomar agentes** continua sem replanejar o que já existe. As mesmas execuções aparecem no painel central (`..\agentkit\iniciar_painel.bat`, http://127.0.0.1:8700), junto com as dos outros projetos e com quem está usando a GPU.
5. Avalie a coleção na aba **Criação**: **Selecionar** a melhor imagem de cada conceito, **Descartar**, **Recriar** (com feedback opcional que o refinador usa para reescrever o prompt), **Gerar outra variação**, **Remover da coleção** ou pedir uma **Nova imagem** para a coleção (com uma ideia opcional). Novas solicitações entram na fila.
6. As imagens selecionadas seguem o fluxo da aba **Ampliação** (upscale, revisão final, metadados) e **Exportação**.

Ao trocar de motor, a trava de GPU do agentkit pode liberar VRAM, inclusive quando a troca vem de outro projeto (descarrega modelos do LM Studio antes do ComfyUI e chama `/free` do ComfyUI antes do LM Studio). Isso evita disputa por memória em GPUs de 8 GiB, mas o modelo precisa ser recarregado a cada fase; desative em **Configuração** se houver VRAM suficiente.

## Fluxo manual

1. Verifique os serviços na aba **Painel**. A verificação só consulta endpoints e não carrega modelos.
2. Em **Painel → Modo manual**, preencha o briefing, o conceito e o prompt em inglês, revise-os e marque a aprovação humana. A coleção já fica pronta para ir direto à geração; esse caminho não chama o planejador LLM.
3. No ComfyUI, use **File → Export Workflow (API)**. Importe e mapeie os workflows de geração e upscale separadamente. Se quiser correções assistidas, importe também um workflow de inpainting com entradas de imagem e prompt e um nó de saída de imagem. Workflows visuais do editor são rejeitados.
4. Clique em **Gerar imagens** para enviar o lote ao ComfyUI. A execução é sequencial e grava parâmetros, workflow, seed, `prompt_id`, estados, duração e hashes. Timeout preserva o mesmo identificador para reconciliação.
5. Inspecione a galeria. Revisão por agente aparece somente para modelos que o LM Studio declara com visão; sem modelo compatível, a análise automática fica indisponível. O parecer legível é auxiliar e não substitui inspeção humana.
6. Se necessário, escolha um candidato, edite o feedback sugerido e gere a correção pelo workflow de inpainting. Cada imagem de origem permite até duas correções. Cada resultado é salvo separadamente em `corrections/` e volta à galeria para revisão; originais nunca são sobrescritos.
7. Selecione manualmente um candidato e faça o upscale. Compare original e resultado, revise a 100% e registre defeitos ou aprovação.
8. Confirme uma descrição visual e solicite metadados. Sem visão, a origem fica marcada como descrição e a conferência humana continua pendente. Edite e aprove título e palavras-chave.
9. Aprove localmente a preparação. O app salva JPEG sRGB, JSON auxiliar e manifesto. Ele não chama API de upload, não automatiza login e não atualiza submissão ou moderação sem confirmação humana.
10. Abra a pasta ou o portal somente pelos botões da aba **Exportação**. Registre transferência manual, submissão e resultado da Adobe em ações distintas.

Na aba **Registro**, o **Registro da moderação** guarda o resultado da Adobe (pendente, aprovada ou rejeitada, com motivo e ID) de cada imagem. Os conceitos da coleção ativa já preparados para envio podem ser marcados em uma tabela editável (o estado avança até a decisão, com histórico, e a rejeição exige motivo), e imagens enviadas fora do fluxo podem ser registradas à parte. Os botões da aba **Exportação** alimentam o mesmo registro, que é a fonte dos números e dos motivos enviados aos agentes. Ao remover uma coleção, seus registros de moderação são mantidos, sem vínculo, para preservar o aprendizado.

As imagens são guardadas em `data/collections/<id>/` e organizadas em `originals/`, `selected/`, `upscaled/`, `corrections/`, `exports/`, `metadata/` e `workflows/`. Arquivos de origem nunca são sobrescritos. O exportador não redimensiona nem distorce a proporção; limites de MP/tamanho são configuráveis. Não há CSV Adobe porque um esquema oficial aplicável não foi confirmado.

No overview do **Painel**, cada coleção pode ser removida permanentemente depois de digitar seu nome exato. A remoção apaga os registros relacionados no SQLite e apenas `data/collections/<id>/`; é bloqueada enquanto houver jobs ComfyUI em fila ou execução.

## Instruções dos workflows

Um workflow de geração precisa mapear `prompt`, uma entrada `seed`/`noise_seed` e pelo menos um nó que a API reconheça como saída de imagem. Cada candidato recebe uma seed aleatória nova; sem esse mapeamento, a geração é bloqueada em vez de reutilizar silenciosamente a seed fixa do workflow. Negative prompt, dimensões, sampler e prefixo de arquivo são mapeamentos opcionais, definidos usando os nós/entradas presentes no seu workflow. O workflow de upscale deve mapear `image` e seu nó de saída. O workflow opcional de inpainting precisa mapear `image`, `prompt` e o nó de saída. A validação consulta `/object_info`; nenhum checkpoint ou ID de nó é presumido.

Perfis iniciais de 1536×864 e 1344×768 são exemplos selecionáveis, não resoluções garantidas para qualquer checkpoint. Os valores são editáveis. Não há upscale adicional automático para alcançar uma dimensão alvo e nenhum recorte automático. Chamadas de inferência LM Studio e execução ComfyUI compartilham a trava de GPU do agentkit, que vale entre processos: este app e outros projetos que usam o agentkit nunca usam a GPU ao mesmo tempo. Com a liberação de VRAM desligada, os modelos podem continuar carregados após a chamada.

## Adobe Stock e limites

As diretrizes oficiais de conteúdo gerado por IA exigem marcar essa origem no portal. Quando aplicável, também é necessário confirmar pessoas/propriedades fictícias e fornecer liberações. Evite IP de terceiros, nomes de artistas, pessoas e personagens. A aprovação técnica, editorial e legal é decidida pela Adobe; a ferramenta não garante aceitação.

As regras numéricas de dimensão, megapixels e bytes ficam vazias até conferência na fonte atual. A página de requisitos técnicos não pôde ser recuperada durante esta implementação; a interface avisa e permite registrar fonte/data após verificação. MB, MP e DPI são tratados como medidas distintas. O manifesto informa que a identificação de IA precisa ser marcada manualmente no portal.

## Persistência, testes e segurança

SQLite, histórico de transições, execuções de agente, trabalhos e registros manuais são armazenados localmente em `data/flow.sqlite3`. Chaves idempotentes e o `prompt_id` do ComfyUI evitam duplicar um trabalho ao retomar. Agentes não executam shell nem escolhem caminhos, credenciais ou ações de upload. O pipeline automático só roda quando você cria a coleção ou pede uma ação na avaliação, tem número limitado de recriações, é interrompível entre tarefas e nunca seleciona imagens: a escolha final é sempre humana. Execuções, estado de cada agente e eventos ficam no banco central do agentkit (`%LOCALAPPDATA%\agentkit\hub.sqlite3`); as tabelas `pipeline_*` de versões anteriores em `flow.sqlite3` não são mais usadas. Não habilite endereços remotos sem proteger o serviço local.

Execute a suíte sem inferência ou geração real:

```powershell
& .\.venv\Scripts\python.exe -m pytest -q
```

## Limitações atuais

- Nenhum workflow API foi importado neste projeto; geração e upscale reais exigem importar e validar os seus.
- Nenhum modelo LM Studio foi carregado ou chamado durante o desenvolvimento. O app não baixa nem altera modelos.
- Não existe integração oficial confirmada para upload Adobe nesta implementação; envio é manual.
- Importação de relatórios oficiais, detecção de similaridade e correções automáticas ficam para etapas futuras; o inpainting disponível é sempre disparado e revisado por uma pessoa.
- Requisitos técnicos numéricos Adobe e esquema CSV não foram confirmados; não são presumidos nem exportados como regra oficial.

## Referências oficiais consultadas

- [Formato API de workflows](https://docs.comfy.org/development/api-development/workflow-api-format), [rotas do servidor ComfyUI](https://docs.comfy.org/development/comfyui-server/comms_routes) e [exemplos HTTP/WebSocket](https://docs.comfy.org/development/comfyui-server/api-examples).
- [LM Studio: OpenAI-compatible API](https://lmstudio.ai/docs/developer/openai-compat), [chat completions](https://lmstudio.ai/docs/developer/openai-compat/chat-completions) e [descoberta de modelos e capacidade visual](https://lmstudio.ai/docs/developer/rest/list).
- [Adobe Stock: diretrizes para conteúdo generativo](https://helpx.adobe.com/stock/contributor/help/generative-ai-content.html).

Ainda na aba **Resultados**, o bloco **Horas trabalhadas** registra o tempo por coleção, por cronômetro (um único aberto em todo o app, também exibido na barra lateral) ou por lançamento manual; lançamentos só são excluídos a pedido, com confirmação. A **receita/hora** divide a receita de cada moeda (sem conversão) pelas horas totais, que somam os lançamentos de horas e o "Tempo manual" informado nos relatórios; sem horas aparece "-". Ao remover uma coleção, suas sessões de trabalho são apagadas junto. O tempo que atravessa a meia-noite fica inteiro na data de início, sem divisão. Um cronômetro acima do limite configurado (`timer_max_hours`, padrão 12 h, na aba Configuração) é tratado como esquecido: ao parar, informe os minutos reais; com menos de 1 minuto a sessão é descartada. Uma coleção com cronômetro aberto não pode ser removida.

No topo da aba **Resultados**, os **Indicadores** mostram taxa de aprovação (aprovadas / aprovadas + rejeitadas, pendentes fora), aprovadas por semana (média das últimas 12, com gráfico), downloads por imagem aprovada no último mês lançado e receita/hora, no total e por coleção. Eles vêm do registro de moderação e do bloco **Downloads e receita mensal**, onde você lança, por mês, coleção (ou "sem coleção") e moeda, os números conferidos no painel da Adobe; há um lançamento por coleção e mês (a moeda é um campo dele, USD por padrão), e lançar de novo o mesmo mês substitui o valor, inclusive a moeda. Semanas e meses usam o fuso Europe/Madrid. Não há conversão de moedas nem projeção. Os lançamentos mensais e o registro de moderação são histórico: ao remover uma coleção eles são mantidos, sem vínculo, com o nome dela (aparece como "<nome> (removida)"), e as horas e a receita avulsa dela são arquivadas para que a receita/hora total não mude. A média semanal de aprovadas considera só semanas completas desde a primeira aprovação; aprovada sem data de decisão usa a data de envio ou da última atualização. O formulário antigo ficou em "Registro avulso (legado)"; a receita lançada ali também entra na receita/hora, então não lance o mesmo valor nos dois lugares.

Para trazer os ganhos reais, use **Importar ganhos da Adobe** na aba Resultados: no portal, Insights → Ganancias → selecione a imagem → baixe o CSV (um arquivo por imagem, sem cabeçalho nem ID, uma venda por linha `AAAA-MM-DD,valor` em USD). Envie um ou vários arquivos, escolha de qual imagem é cada um (ou cadastre uma nova imagem fora do fluxo) e clique em Importar. Reimportar o mesmo arquivo não duplica vendas; a imagem é marcada como aprovada (decisão na data da primeira venda); e os lançamentos mensais da coleção dela são recalculados (downloads = nº de vendas, receita = soma) e aparecem como "importado". Se já houver lançamento manual no mesmo mês, a prévia mostra o conflito (uma escolha por coleção e mês para o lote todo) e você decide manter o manual ou substituí-lo (o valor manual anterior fica na nota); editar à mão um mês importado pede confirmação. Os arquivos não são gravados em disco. Para desfazer uma importação, use "Remover vendas importadas de uma imagem" (a moderação não é alterada).
