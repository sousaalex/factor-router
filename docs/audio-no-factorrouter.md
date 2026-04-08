# Audio no FactorRouter

Documentacao rapida para equipas de frontend integrarem transcricao de audio via FactorRouter.

## Endpoint

- Metodo: `POST`
- URL: `/v1/audio/transcriptions`
- Content-Type: `multipart/form-data`
- Auth: `Authorization: Bearer <FACTOR_ROUTER_KEY>`

O FactorRouter faz proxy para a Factor Whisper e mantem autenticacao, rastreio e centro de custos.

## Campos do request (multipart)

- `file` (obrigatorio): ficheiro de audio (`wav`, `mp3`, `m4a`, etc.)
- `model` (recomendado): usar sempre `auto`
- `response_format` (opcional): `json` (default) ou `text`
- `language` (opcional)
- `prompt` (opcional)
- `temperature` (opcional)

> Recomendacao de produto: enviar `model="auto"` para manter compatibilidade com clientes OpenAI-like.

## Headers obrigatorios (mesmos do chat)

Todos os requests de audio devem incluir os mesmos `X-*` do chat:

- `X-Turn-Id` (UUID v4)
- `X-Session-Id`
- `X-Conversation-Id` (ou `"null"`)
- `X-User-Message` (ate 300 chars)
- `X-User-Id` (ou `"null"`)
- `X-User-Name` (ou `"null"`)
- `X-User-Email` (ou `"null"`)
- `X-Company-Id` (ou `"null"`)
- `X-Company-Name` (ou `"null"`)

## Exemplo cURL

```bash
curl -X POST "http://localhost:8003/v1/audio/transcriptions" \
  -H "Authorization: Bearer sk-fai-..." \
  -H "X-Turn-Id: 11111111-1111-4111-8111-111111111111" \
  -H "X-Session-Id: sess-001" \
  -H "X-Conversation-Id: conv-001" \
  -H "X-User-Message: transcrever audio da reuniao" \
  -H "X-User-Id: user-001" \
  -H "X-User-Name: Alex" \
  -H "X-User-Email: alex@empresa.com" \
  -H "X-Company-Id: comp-001" \
  -H "X-Company-Name: Factor" \
  -F "file=@/caminho/audio.wav" \
  -F "model=auto" \
  -F "response_format=json"
```

## Exemplo frontend (JavaScript/TypeScript)

```ts
async function transcreverAudio(file: File, apiKey: string) {
  const form = new FormData();
  form.append("file", file);
  form.append("model", "auto");
  form.append("response_format", "json");

  const res = await fetch("http://localhost:8003/v1/audio/transcriptions", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "X-Turn-Id": crypto.randomUUID(),
      "X-Session-Id": "sess-001",
      "X-Conversation-Id": "conv-001",
      "X-User-Message": "transcrever audio do utilizador",
      "X-User-Id": "user-001",
      "X-User-Name": "Alex",
      "X-User-Email": "alex@empresa.com",
      "X-Company-Id": "comp-001",
      "X-Company-Name": "Factor",
    },
    body: form,
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err?.message || `Erro HTTP ${res.status}`);
  }

  return res.json(); // quando response_format=json
}
```

## Respostas esperadas

- `response_format=json`: objeto com `text`, `segments`, `usage` estimado, etc.
- `response_format=text`: texto simples (`text/plain`) com a transcricao.

## Erros comuns

- `400 missing_file`: campo `file` nao foi enviado.
- `400 missing_required_header`: faltou algum header `X-*`.
- `401 missing_authorization` / `401 invalid_api_key`: autenticacao invalida.
- `504 upstream_timeout`: timeout no upstream de transcricao.

