export type KnowledgeBase = { id: number; name: string; description: string; created_at: string };
export type DocumentItem = { id: number; knowledge_base_id: number; filename: string; status: string; error_message: string; created_at: string };
export type Citation = { document_id: number; filename: string; chunk_id: number; page_number?: number; score: number; excerpt: string };
export type ChatMessage = { id: number; role: "user" | "assistant"; content: string; created_at: string };
export type ChatSession = { id: number; knowledge_base_id: number; title: string; created_at: string; message_count: number; last_message_at?: string };
export type ChatSessionDetail = ChatSession & { messages: ChatMessage[] };
export type ChatStreamHandlers = {
  onStatus?: (message: string) => void;
  onToken: (content: string) => void;
  onCitations: (citations: Citation[]) => void;
  onDone: (sessionId: number) => void;
};

const request = async <T>(path: string, options: RequestInit = {}): Promise<T> => {
  const token = localStorage.getItem("token");
  const headers = new Headers(options.headers);
  if (!(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(path, { ...options, headers });
  if (response.status === 204) return undefined as T;
  const contentType = response.headers.get("content-type") ?? "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();
  if (!response.ok) {
    const detail = typeof payload === "object" && payload ? payload.detail : payload;
    throw new Error(detail || `请求失败（${response.status}）`);
  }
  return payload as T;
};

const askStream = async (knowledgeBaseId: number, question: string, sessionId: number | undefined, handlers: ChatStreamHandlers) => {
  const token = localStorage.getItem("token");
  const response = await fetch("/api/chat/ask/stream", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {})
    },
    body: JSON.stringify({ knowledge_base_id: knowledgeBaseId, question, session_id: sessionId })
  });
  if (!response.ok || !response.body) {
    const payload = await response.text();
    throw new Error(payload || `Stream request failed (${response.status})`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const events = buffer.split("\n\n");
    buffer = events.pop() ?? "";
    for (const block of events) {
      const event = block.split("\n").find((line) => line.startsWith("event: "))?.slice(7);
      const dataLine = block.split("\n").find((line) => line.startsWith("data: "))?.slice(6);
      if (!event || !dataLine) continue;
      const data = JSON.parse(dataLine);
      if (event === "status") handlers.onStatus?.(data.message);
      if (event === "token") {
        handlers.onToken(data.content);
        await new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()));
      }
      if (event === "citations") handlers.onCitations(data.items);
      if (event === "done") handlers.onDone(data.session_id);
      if (event === "error") throw new Error(data.message || "Stream generation failed");
    }
    if (done) break;
  }
};

export const api = {
  login: (email: string, password: string, register = false) => request<{ access_token: string }>(`/api/auth/${register ? "register" : "login"}`, { method: "POST", body: JSON.stringify({ email, password }) }),
  listKnowledgeBases: () => request<KnowledgeBase[]>("/api/knowledge-bases"),
  createKnowledgeBase: (name: string, description: string) => request<KnowledgeBase>("/api/knowledge-bases", { method: "POST", body: JSON.stringify({ name, description }) }),
  updateKnowledgeBase: (id: number, name: string, description: string) => request<KnowledgeBase>(`/api/knowledge-bases/${id}`, { method: "PUT", body: JSON.stringify({ name, description }) }),
  deleteKnowledgeBase: (id: number) => request<void>(`/api/knowledge-bases/${id}`, { method: "DELETE" }),
  listDocuments: (knowledgeBaseId: number) => request<DocumentItem[]>(`/api/documents?knowledge_base_id=${knowledgeBaseId}`),
  uploadDocument: (knowledgeBaseId: number, file: File) => {
    const body = new FormData();
    body.append("file", file);
    return request<DocumentItem>(`/api/documents/upload?knowledge_base_id=${knowledgeBaseId}`, { method: "POST", body });
  },
  reprocessDocument: (id: number) => request<DocumentItem>(`/api/documents/${id}/reprocess`, { method: "POST" }),
  deleteDocument: (id: number) => request<void>(`/api/documents/${id}`, { method: "DELETE" }),
  listChatSessions: (knowledgeBaseId?: number) => request<ChatSession[]>(`/api/chat/sessions${knowledgeBaseId ? `?knowledge_base_id=${knowledgeBaseId}` : ""}`),
  getChatSession: (id: number) => request<ChatSessionDetail>(`/api/chat/sessions/${id}`),
  deleteChatSession: (id: number) => request<void>(`/api/chat/sessions/${id}`, { method: "DELETE" }),
  askStream,
  ask: (knowledgeBaseId: number, question: string, sessionId?: number) => request<{ session_id: number; answer: string; citations: Citation[] }>("/api/chat/ask", { method: "POST", body: JSON.stringify({ knowledge_base_id: knowledgeBaseId, question, session_id: sessionId }) })
};
