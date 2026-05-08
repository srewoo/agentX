import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, apiClient, request, uuidv4 } from "./api";

// chrome.storage is mocked globally via __tests__/setup.ts.

describe("uuidv4", () => {
  it("returns RFC4122-shaped string", () => {
    const v = uuidv4();
    expect(v).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$/i);
  });

  it("returns different values across calls", () => {
    expect(uuidv4()).not.toBe(uuidv4());
  });
});

describe("api client", () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, "fetch" as never);
  });

  afterEach(() => {
    fetchSpy.mockRestore();
  });

  function mockJson(payload: unknown, status = 200, ok = status < 400) {
    fetchSpy.mockResolvedValueOnce({
      ok,
      status,
      text: async () => JSON.stringify(payload),
    } as unknown as Response);
  }

  it("attaches X-Request-Id header on every call", async () => {
    mockJson({ data: [] });
    await apiClient.getRecommendations();
    const init = fetchSpy.mock.calls[0][1] as RequestInit;
    const headers = init.headers as Record<string, string>;
    expect(headers["X-Request-Id"]).toMatch(/^[0-9a-f-]{36}$/i);
    expect(headers["Content-Type"]).toBe("application/json");
  });

  it("unwraps the data envelope", async () => {
    mockJson({ data: [{ id: "1", symbol: "RELIANCE" }] });
    const res = await apiClient.getRecommendations();
    expect(res).toEqual([{ id: "1", symbol: "RELIANCE" }]);
  });

  it("tolerates a bare-data (non-envelope) response", async () => {
    mockJson([{ id: "1" }]);
    const res = await apiClient.getRecommendations();
    expect(res).toEqual([{ id: "1" }]);
  });

  it("surfaces envelope errors as ApiError", async () => {
    mockJson({ errors: [{ code: "BAD", message: "no good" }] }, 200);
    await expect(apiClient.getRecommendations()).rejects.toMatchObject({
      name: "ApiError",
      code: "BAD",
      message: "no good",
    });
  });

  it("maps non-2xx status to ApiError with code", async () => {
    mockJson({ errors: [{ code: "FORBIDDEN", message: "nope" }] }, 403, false);
    await expect(apiClient.getRecommendations()).rejects.toBeInstanceOf(ApiError);
  });

  it("surfaces malformed JSON as ApiError with BAD_JSON code", async () => {
    fetchSpy.mockResolvedValueOnce({
      ok: true,
      status: 200,
      text: async () => "<<not json>>",
    } as unknown as Response);
    await expect(request<unknown>("/api/v1/recommendations")).rejects.toMatchObject({
      name: "ApiError",
      code: "BAD_JSON",
    });
  });

  it("passes filters as query string", async () => {
    mockJson({ data: [] });
    await apiClient.getRecommendations({ horizon: "swing", minConviction: 0.7 });
    const url = fetchSpy.mock.calls[0][0] as string;
    expect(url).toContain("horizon=swing");
    expect(url).toContain("minConviction=0.7");
  });

  it("times out and throws ApiError(TIMEOUT)", async () => {
    fetchSpy.mockImplementationOnce(((..._args: unknown[]) => {
      const init = _args[1] as RequestInit | undefined;
      return new Promise((_, reject) => {
        init?.signal?.addEventListener("abort", () =>
          reject(new DOMException("aborted", "AbortError")),
        );
      }) as unknown as Promise<Response>;
    }) as unknown as Parameters<typeof fetchSpy.mockImplementationOnce>[0]);
    await expect(
      request("/slow", { timeoutMs: 5 }),
    ).rejects.toMatchObject({ name: "ApiError", code: "TIMEOUT" });
  });

  it("creates an alert via POST", async () => {
    mockJson({ data: { id: "a1", symbol: "TCS", condition: "above", targetPrice: 4000, note: null, active: true, createdAt: "x", triggeredAt: null, triggeredPrice: null } });
    const res = await apiClient.createAlert({ symbol: "TCS", condition: "above", targetPrice: 4000 });
    const init = fetchSpy.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ symbol: "TCS", condition: "above", targetPrice: 4000 });
    expect(res.id).toBe("a1");
  });
});
