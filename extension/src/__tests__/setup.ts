import "@testing-library/jest-dom/vitest";
import { vi } from "vitest";

// Mock chrome.storage API
const storageData: Record<string, Record<string, unknown>> = {
  local: {},
  sync: {},
};

const createStorageArea = (area: "local" | "sync") => ({
  get: vi.fn(async (keys?: string | string[] | Record<string, unknown>) => {
    if (!keys) return { ...storageData[area] };
    if (typeof keys === "string") {
      return { [keys]: storageData[area][keys] };
    }
    if (Array.isArray(keys)) {
      const result: Record<string, unknown> = {};
      for (const k of keys) result[k] = storageData[area][k];
      return result;
    }
    // keys is default-value object
    const result: Record<string, unknown> = {};
    for (const [k, def] of Object.entries(keys)) {
      result[k] = storageData[area][k] ?? def;
    }
    return result;
  }),
  set: vi.fn(async (items: Record<string, unknown>) => {
    Object.assign(storageData[area], items);
  }),
  remove: vi.fn(async (keys: string | string[]) => {
    const arr = Array.isArray(keys) ? keys : [keys];
    for (const k of arr) delete storageData[area][k];
  }),
  clear: vi.fn(async () => {
    storageData[area] = {};
  }),
});

const changeListeners: Array<
  (changes: Record<string, chrome.storage.StorageChange>, area: string) => void
> = [];

const mockChrome = {
  storage: {
    local: createStorageArea("local"),
    sync: createStorageArea("sync"),
    onChanged: {
      addListener: vi.fn((cb: typeof changeListeners[0]) => {
        changeListeners.push(cb);
      }),
      removeListener: vi.fn((cb: typeof changeListeners[0]) => {
        const idx = changeListeners.indexOf(cb);
        if (idx >= 0) changeListeners.splice(idx, 1);
      }),
    },
  },
  runtime: {
    sendMessage: vi.fn(async () => ({})),
    onMessage: {
      addListener: vi.fn(),
      removeListener: vi.fn(),
    },
  },
  alarms: {
    create: vi.fn(),
    clear: vi.fn(),
    onAlarm: { addListener: vi.fn(), removeListener: vi.fn() },
  },
};

// Expose on global
Object.defineProperty(globalThis, "chrome", { value: mockChrome, writable: true });

// Helper to reset storage between tests
export function resetChromeStorage() {
  storageData.local = {};
  storageData.sync = {};
}
