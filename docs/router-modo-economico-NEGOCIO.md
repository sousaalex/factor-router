# Router em modo económico — documentação de negócio

## O problema

Os modelos de linguagem têm **custos muito diferentes** por milhão de tokens. Sem disciplina, o agente tende a escolher modelos “mais capazes” mesmo quando uma versão mais simples chega para a tarefa — o que **gasta o saldo pré-pago** (OpenRouter) de forma desnecessária.

## O que o FactorRouter faz por nós

O nosso gateway não só **encaminha** o tráfego para o OpenRouter como **decide qual modelo usar** em cada turno, com base num classificador interno e em regras de negócio.

Para **proteger o orçamento** quando o saldo está baixo, activámos uma **inteligência de poupança** em duas camadas:

### 1. Orientação ao “decisor” (classificador)

O sistema envia **instruções adicionais** ao componente que escolhe o modelo (em linguagem natural, em inglês, para o motor local de classificação). Em resumo:

- Preferir o modelo **mais económico** que ainda consiga fazer o trabalho com segurança.
- Reservar modelos **intermédios** só quando a tarefa o exige (vários passos, dados ligados entre si, etc.).
- **Evitar** os modelos mais caros **salvo** se o utilizador pedir explicitamente esse nível ou se o risco de erro for inaceitável.

Isto **não bloqueia** o utilizador: muda a **prioridade de custo** na decisão automática.

### 2. Limite de segurança (teto)

Mesmo que a escolha automática ainda aponte para uma categoria **cara**, o gateway **corrige** para um modelo **intermédio** (Kimi K2.5 — tier “reasoning+”) quando o saldo está abaixo do limiar definido. Assim garantimos um **teto de despesa** por decisão nesses cenários, sem depender apenas da “opinião” do classificador.

## Quando é que isto se activa?

- O gateway consulta o **último saldo registado** na nossa base de dados (actualizado quando um administrador consulta o ecrã/API de créditos OpenRouter).
- Se esse saldo estiver **igual ou abaixo** do limiar de “modo poupança” **configurável**, entra o modo económico.
- Podemos ter **dois limiares**:
  - Um mais **alto** só para o router: começamos a **poupar mais cedo** (ex.: 25 USD).
  - Outro mais **baixo** para **alertas** ao utilizador ou equipa (ex.: 10 USD), alinhado com o aviso de saldo baixo na interface.

Se a funcionalidade estiver **desligada** na configuração (`OPENROUTER_ROUTER_BUDGET_ENABLED=false`), **nenhuma** destas regras extra é aplicada — o router comporta-se como antes.

## O que a equipa precisa de fazer

1. **Manter o saldo actualizado na base** — por exemplo, abrir periodicamente a ferramenta de administração que refresca os créditos OpenRouter (o mesmo fluxo que alimenta o alerta de saldo).
2. **Ajustar os limiares** em função do risco e do orçamento: começar a poupar “mais cedo” ou “mais tarde”.
3. **Comunicar** a utilizadores avançados que, com saldo muito baixo, o sistema pode **preferir modelos mais baratos** mesmo para tarefas que, em condições normais, usariam um modelo superior — sempre com o objectivo de **manter o serviço disponível** até carregar novo saldo.

## Benefícios para o negócio

- **Menos surpresas** no consumo quando o saldo aproxima-se de zero.
- **Comportamento previsível** e configurável (ligar/desligar, limiares distintos para alerta vs. poupança).
- **Continuidade**: em vez de parar o serviço, **reduz-se o custo médio** por interação.
- **Transparência operacional**: administradores sabem quando o modo poupança está activo através dos logs e da política de créditos.

## Resumo numa frase

**Com saldo baixo, o FactorRouter pensa duas vezes antes de gastar:** primeiro **orienta** a escolha para modelos mais baratos; depois **impõe um teto** se a escolha ainda for demasiado cara — sempre com base no último saldo conhecido e nas regras que definimos em configuração.
