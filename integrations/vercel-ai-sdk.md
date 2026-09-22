# Vercel AI SDK integration

The AI SDK runs on the server and on the client. Memory belongs on the **server**:
the API key must never reach the browser, and the memory store should not depend on
a client staying open.

## 1. Server-side route with memory

`app/api/chat/route.ts`:

```ts
import { NextRequest } from "next/server";
import { streamText } from "ai";
import { openai } from "@ai-sdk/openai";

const MEMORY_URL = process.env.MNEMOSYNE_URL ?? "http://127.0.0.1:8788";
const MEMORY_KEY = process.env.MNEMOSYNE_API_KEY!;

async function recall(query: string, userId: string, topK = 5) {
  const res = await fetch(`${MEMORY_URL}/v3/memories/search/`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${MEMORY_KEY}`,
    },
    body: JSON.stringify({ query, filters: { user_id: userId }, top_k: topK }),
  });
  if (!res.ok) return [];                     // never fail the chat for memory
  const { results } = await res.json();
  return results as Array<{ memory: string; score: number; id: string }>;
}

async function remember(messages: unknown[], userId: string) {
  // Fire-and-forget: extraction may call a model, and the user should not wait.
  void fetch(`${MEMORY_URL}/v3/memories/add/`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${MEMORY_KEY}`,
    },
    body: JSON.stringify({ messages, user_id: userId }),
  }).catch(() => {});
}

export async function POST(req: NextRequest) {
  const { messages, userId } = await req.json();

  const lastUser = [...messages].reverse().find((m) => m.role === "user");
  const hits = await recall(String(lastUser?.content ?? ""), userId);

  const memoryBlock = hits.length
    ? "\n\nWhat you already know about this user:\n" +
      hits.map((h) => `- ${h.memory}`).join("\n")
    : "";

  const result = streamText({
    model: openai("gpt-4o-mini"),
    system:
      "You are a helpful assistant with long-term memory of this user." +
      memoryBlock,
    messages,
    onFinish: ({ text }) => {
      remember(
        [...messages.slice(-4), { role: "assistant", content: text }],
        userId,
      );
    },
  });

  return result.toDataStreamResponse();
}
```

Three choices in that file are load-bearing:

- **`recall` swallows failures.** A memory outage degrades the answer; it does not
  break the chat. `if (!res.ok) return []` is what makes that true.
- **`remember` is not awaited.** `add()` may call a model for extraction. Awaiting
  it inside `onFinish` delays the stream's completion for bookkeeping.
- **`messages.slice(-4)`** sends only the recent turns. Sending the whole
  transcript every turn is how a memory store ends up with forty near-identical
  copies of the same fact.

## 2. Client-side hook

```ts
// app/useMemoryChat.ts
import { useChat } from "ai/react";

export function useMemoryChat(userId: string) {
  return useChat({
    api: "/api/chat",
    body: { userId },          // stable for as long as memory should persist
  });
}
```

## 3. Using the TypeScript-free path

If you would rather not run an HTTP service, call Mnemosyne from a Node server
with `child_process` — the CLI's agent mode is designed for exactly this:

```ts
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const run = promisify(execFile);

async function recall(query: string, userId: string) {
  const { stdout } = await run("mnemosyne", [
    "--agent", "search", query, "--user-id", userId,
  ]);
  return JSON.parse(stdout).data as Array<{ memory: string; score: number }>;
}

async function remember(text: string, userId: string) {
  await run("mnemosyne", ["--agent", "add", text, "--user-id", userId]);
}
```

Agent mode guarantees three things this relies on: the output is a single JSON
object on stdout, it carries no colour codes or banners to strip, and an error
arrives as JSON with a non-zero exit code rather than on stderr as prose.

## 4. Deployment notes

| Concern | Guidance |
| --- | --- |
| Key handling | `MNEMOSYNE_API_KEY` as a server-only env var. Never `NEXT_PUBLIC_*`. |
| Serverless | Memory must reach a persistent server. On Vercel, point `MNEMOSYNE_URL` at a host with a volume — the default filesystem is ephemeral, and a memory that resets on every cold start is worse than no memory. |
| Scope | `userId` must be the application's real account id. A value that varies per request creates a new empty store each time. |
| Retention | Bind `userId` to the account so deleting the account can delete its memory: `DELETE /v2/entities/` with `{"user_id": "..."}`. |

## 5. Switching models without touching the memory code

Provider selection is configuration, not code — so the memory route above stays
the same whatever model you use:

```json
{
  "llm":          { "provider": "openai",   "config": { "model": "gpt-4o-mini" } },
  "embedder":     { "provider": "openai",   "config": { "model": "text-embedding-3-small" } },
  "vector_store": { "provider": "qdrant",   "config": { "url": "http://qdrant:6333" } }
}
```

Write that to `<MNEMOSYNE_DIR>/api.config.json` on the memory host. Then check
`GET /v1/status/` — it reports what is actually running and anything that was
substituted, which is the difference between "I configured a better embedder" and
"I configured a better embedder and it is live".
