# Fluxo Imagens: backlog de melhorias

Origem: avaliação de 24/10/2026 do app local (todas as abas, somente leitura) e do portfólio na Adobe Stock.
Documento completo: Google Drive, pasta Fluxo Imagens, "Fluxo Imagens - Avaliação e plano".

Situação: 31 imagens no portfólio, 4 downloads e USD 3,11 desde o início, nenhum download nas últimas 12 semanas. Das 5 rejeições registradas, 3 foram por qualidade técnica (exposição, foco suave, filtros, artefatos ou ruído) e 2 por conteúdo semelhante.

Objetivo: aumentar a taxa de aprovação e o volume de imagens aprovadas por semana, e medir o ganho por hora para decidir, em 90 dias, se a frente vale a pena.

## Regras para quem for implementar

- Antes de mudar qualquer coisa, inspecione o projeto e descreva a estrutura: abas do Streamlit, onde ficam coleções, candidatos, metadados, configuração e o registro de moderação.
- Uma tarefa por branch e por PR.
- O envio para a Adobe continua manual. Nada no app deve enviar imagens ou fazer login em portais.
- Não apague imagens, coleções nem históricos sem perguntar.
- Respeite a serialização entre LM Studio e ComfyUI e a opção de liberar VRAM (a máquina é uma RTX 4060 com 8 GB).
- Limiares e parâmetros novos ficam na aba Configuração, não fixos no código.
- Toda tarefa termina com testes rodando e um resumo do que mudou.

## P0. Dados para decidir

### 1. Registro completo da moderação
O app conta 4 aprovadas e 5 rejeitadas, mas o portfólio já tem 11 imagens dos temas do Fluxo (7 de ingredientes e 4 de circuitos).
- [ ] Tela para marcar cada imagem enviada como aprovada, rejeitada (com motivo) ou pendente, por coleção.
- [ ] Permitir registrar imagens enviadas fora do fluxo, para o histórico ficar completo.
- [ ] O texto enviado ao planejador passa a usar esse registro completo.
- Aceite: as 11 imagens do portfólio aparecem como aprovadas, e as 5 rejeições continuam com os motivos.

### 2. Registro de horas por coleção
- [ ] Campo para lançar horas trabalhadas por coleção (manual) e, se for simples, um cronômetro de iniciar e parar.
- [ ] A aba Resultados mostra receita por hora, por coleção e no total, sem converter moedas nem projetar ganhos.

### 3. Lançamento de downloads e receita
- [ ] Lançamento mensal de downloads e receita por coleção, conferidos no painel da Adobe.
- [ ] Métricas na aba Resultados: taxa de aprovação, imagens aprovadas por semana, downloads por imagem aprovada por mês e receita por hora.

## P0. Qualidade antes do envio

### 4. Checagem técnica automática
- [x] Nitidez (por exemplo, variância do Laplaciano), estimativa de ruído e exposição (porcentagem de pixels estourados ou pretos) em cada imagem final.
- [x] Conferir resolução, megapixels e tamanho de arquivo contra os limites já salvos na Configuração.
- [x] Resultado por imagem: aprovada, alerta ou bloqueada, com o motivo. A exportação avisa antes de incluir uma imagem com alerta.
- [x] Limiares configuráveis. Calibrar usando as imagens já aprovadas e as rejeitadas por qualidade como referência.
- Aceite: as 3 imagens rejeitadas por qualidade recebem pelo menos um alerta, e as aprovadas passam.

Validação local em 24/10/2026: 3/3 rejeitadas por qualidade com alerta e 11/11 aprovadas locais sem alerta. Calibração na própria amostra; limitações e reprodução no README.

### 5. Detecção de imagens parecidas
- [x] Comparar candidatas entre si e com tudo o que já foi enviado (hash perceptual e, se couber na VRAM, embeddings como CLIP).
- [x] Na exportação, avisar quando duas imagens do pacote forem parecidas, ou quando uma for parecida com algo já enviado.
- [x] Regra padrão: uma imagem por conceito, configurável.
- Aceite: "The Filament Dandelion" e "The Transistor Mycelium" aparecem como parecidas com as imagens aprovadas do Jardim Eletrônico.

Validação local em 24/10/2026: os dois pares do aceite recebem alerta de paleta semelhante a The Capacitor Vine Canopy (91,4% e 83,6%). Hash perceptual e paleta em CPU; cobertura incompleta é informada, com anexação de referências locais para os envios externos. Limitações e reprodução no README.

## P1. Temas e produção

### 6. Calendário sazonal
- [x] Lista configurável de datas (Ano Novo, Carnaval, Páscoa, Dia das Mães, verão, Black Friday, Natal etc.) com antecedência de envio recomendada.
- [x] O Painel mostra as próximas datas-limite de envio.
- [x] O planejador recebe as datas próximas ao sugerir conceitos.

### 7. Briefings com foco comercial
- [x] Modelos de briefing prontos para temas que compradores procuram: ingredientes e comida, fundos com espaço para texto, conceitos de negócio e tecnologia com uso claro, ambientes.
- [x] O planejador passa a pedir espaço para texto e variação real de composição, cor e cenário entre conceitos.

### 8. GPU na nuvem para as finalistas
- [ ] Permitir uma URL de ComfyUI diferente por etapa (geração, ampliação), para usar uma GPU alugada só nas imagens finais.
- [ ] Registrar o custo informado por coleção, para entrar na conta da receita por hora.

## P2. Distribuição e nome

### 9. Perfis de exportação por agência
- [ ] Perfis de exportação configuráveis (dimensões, formato e arquivo de metadados) para outras agências além da Adobe.
- [ ] Antes de ativar um perfil, conferir as regras atuais daquela agência para imagens geradas por IA e registrar a fonte e a data, como já é feito para a Adobe.

### 10. Nome da ferramenta
- [ ] Aguardando decisão (opções: Safra, Viveiro, Prensa, Lote, Ateliê). Depois de decidido, trocar o título do app e as referências na interface.
