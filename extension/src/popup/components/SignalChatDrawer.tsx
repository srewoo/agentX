import { useEffect, useRef, useState } from "react";
import { api } from "../../shared/api";

type Msg = { role: string; content: string };

/**
 * SignalChatDrawer — per-signal conversational follow-ups with SSE streaming.
 * Falls back to POST /chat if EventSource isn't available.
 */
export default function SignalChatDrawer({
  signalId,
  onClose,
}: {
  signalId: string;
  onClose: () => void;
}) {
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const sseUrlRef = useRef<string | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const r = await api.getSignalChat(signalId);
        if (alive) setMessages(r.messages);
        sseUrlRef.current = await api.signalChatStreamUrl(signalId);
      } catch (e) {
        if (alive) setErr(e instanceof Error ? e.message : "chat history failed");
      }
    })();
    return () => { alive = false; };
  }, [signalId]);

  async function send() {
    const text = input.trim();
    if (!text || streaming) return;
    setInput("");
    setMessages((prev) => [...prev, { role: "user", content: text }, { role: "assistant", content: "" }]);
    setStreaming(true);

    // Use POST with fetch (chrome extension limitation: EventSource doesn't
    // support POST). We parse SSE frames manually from a streaming fetch.
    try {
      const url = sseUrlRef.current!;
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
        body: JSON.stringify({ message: text, session_id: "default" }),
      });
      if (!res.ok || !res.body) {
        throw new Error(`stream HTTP ${res.status}`);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        // Parse complete SSE frames split by blank line.
        let idx;
        while ((idx = buf.indexOf("\n\n")) !== -1) {
          const frame = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const eventLine = frame.match(/^event: (.+)/m);
          const dataLines = [...frame.matchAll(/^data: (.*)$/gm)].map((m) => m[1]);
          const event = eventLine ? eventLine[1] : "message";
          const data = dataLines.join("\n");
          if (event === "token" && data) {
            try {
              const j = JSON.parse(data);
              setMessages((prev) => {
                const copy = [...prev];
                const last = copy[copy.length - 1];
                if (last && last.role === "assistant") {
                  copy[copy.length - 1] = { ...last, content: last.content + (j.text || "") };
                }
                return copy;
              });
            } catch {/* ignore */}
          } else if (event === "error") {
            setErr(data);
          }
        }
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : "stream error");
    } finally {
      setStreaming(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/50">
      <div className="w-full max-w-md bg-zinc-950 border-t border-border rounded-t-md p-2 max-h-[80vh] flex flex-col">
        <div className="flex justify-between items-center mb-1">
          <span className="text-[11px] text-zinc-400 uppercase tracking-wider">Ask agentX · {signalId.slice(0, 12)}</span>
          <button onClick={onClose} className="text-zinc-400 hover:text-zinc-200 text-xs">×</button>
        </div>
        <div className="flex-1 overflow-y-auto space-y-1 text-[11px]">
          {messages.length === 0 && (
            <div className="text-zinc-500">Ask anything about this signal. "Compare with peers", "what would change this thesis", "risk of regime flip"...</div>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`p-1.5 rounded ${m.role === "user" ? "bg-zinc-900 text-zinc-200" : "bg-zinc-900/40 text-zinc-300"}`}>
              <div className="text-[9px] uppercase tracking-wider text-zinc-500 mb-0.5">{m.role}</div>
              <div className="whitespace-pre-wrap">{m.content || (streaming && i === messages.length - 1 ? "…" : "")}</div>
            </div>
          ))}
        </div>
        {err && <div className="text-[11px] text-red-400 mt-1">{err}</div>}
        <div className="flex gap-1 mt-1">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }}
            placeholder="Ask a follow-up…"
            disabled={streaming}
            className="flex-1 bg-zinc-900 border border-border rounded px-2 py-1 text-[11px] text-zinc-200"
          />
          <button
            onClick={send}
            disabled={streaming || !input.trim()}
            className="bg-zinc-800 hover:bg-zinc-700 disabled:opacity-50 text-zinc-200 rounded px-2 text-[11px]"
          >
            {streaming ? "…" : "Send"}
          </button>
        </div>
      </div>
    </div>
  );
}
