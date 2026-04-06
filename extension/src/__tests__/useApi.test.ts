import { describe, it, expect, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useApi } from "../popup/hooks/useApi";

describe("useApi", () => {
  it("should start with null data, no loading, no error", () => {
    const fn = vi.fn(async () => "result");
    const { result } = renderHook(() => useApi(fn));

    expect(result.current.data).toBeNull();
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it("should set loading true during execution and false after", async () => {
    let resolve: (v: string) => void;
    const fn = vi.fn(() => new Promise<string>((r) => { resolve = r; }));
    const { result } = renderHook(() => useApi(fn));

    let promise: Promise<unknown>;
    act(() => {
      promise = result.current.execute();
    });

    expect(result.current.loading).toBe(true);

    await act(async () => {
      resolve!("done");
      await promise;
    });

    expect(result.current.loading).toBe(false);
    expect(result.current.data).toBe("done");
  });

  it("should set error on failure", async () => {
    const fn = vi.fn(async () => {
      throw new Error("Network failed");
    });
    const { result } = renderHook(() => useApi(fn));

    await act(async () => {
      await result.current.execute();
    });

    expect(result.current.error).toBe("Network failed");
    expect(result.current.data).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it("should handle non-Error throws", async () => {
    const fn = vi.fn(async () => {
      throw "string error";
    });
    const { result } = renderHook(() => useApi(fn));

    await act(async () => {
      await result.current.execute();
    });

    expect(result.current.error).toBe("string error");
  });

  it("should return the result from execute", async () => {
    const fn = vi.fn(async () => ({ items: [1, 2, 3] }));
    const { result } = renderHook(() => useApi(fn));

    let returned: unknown;
    await act(async () => {
      returned = await result.current.execute();
    });

    expect(returned).toEqual({ items: [1, 2, 3] });
  });

  it("should return null from execute on error", async () => {
    const fn = vi.fn(async () => { throw new Error("fail"); });
    const { result } = renderHook(() => useApi(fn));

    let returned: unknown;
    await act(async () => {
      returned = await result.current.execute();
    });

    expect(returned).toBeNull();
  });

  it("should reset data and error", async () => {
    const fn = vi.fn(async () => "data");
    const { result } = renderHook(() => useApi(fn));

    await act(async () => {
      await result.current.execute();
    });
    expect(result.current.data).toBe("data");

    act(() => {
      result.current.reset();
    });

    expect(result.current.data).toBeNull();
    expect(result.current.error).toBeNull();
  });

  it("should clear previous error on successful re-execute", async () => {
    let shouldFail = true;
    const fn = vi.fn(async () => {
      if (shouldFail) throw new Error("fail");
      return "success";
    });
    const { result } = renderHook(() => useApi(fn));

    await act(async () => {
      await result.current.execute();
    });
    expect(result.current.error).toBe("fail");

    shouldFail = false;
    await act(async () => {
      await result.current.execute();
    });
    expect(result.current.error).toBeNull();
    expect(result.current.data).toBe("success");
  });

  it("should pass arguments through to the function", async () => {
    const fn = vi.fn(async (a: unknown, b: unknown) => `${a}-${b}`);
    const { result } = renderHook(() => useApi(fn));

    await act(async () => {
      await result.current.execute("hello", 42);
    });

    expect(fn).toHaveBeenCalledWith("hello", 42);
  });
});
