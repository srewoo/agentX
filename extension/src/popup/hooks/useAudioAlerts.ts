import { useEffect, useRef } from "react";
import type { Signal, AppSettings } from "../../shared/types";
import { getSettings } from "../../shared/storage";

/**
 * Plays a short bell tone via WebAudio when a new signal arrives whose
 * strength meets the user's threshold. Settings honoured live.
 */
export function useAudioAlerts(signals: Signal[]) {
  const seenIds = useRef<Set<string>>(new Set());
  const ctxRef = useRef<AudioContext | null>(null);
  const settingsRef = useRef<Partial<AppSettings>>({});

  useEffect(() => {
    getSettings().then((s) => { settingsRef.current = s; });
    const onChange = (changes: { [k: string]: chrome.storage.StorageChange }) => {
      if (changes.settings) settingsRef.current = (changes.settings.newValue ?? {}) as Partial<AppSettings>;
    };
    chrome.storage.onChanged.addListener(onChange);
    return () => chrome.storage.onChanged.removeListener(onChange);
  }, []);

  useEffect(() => {
    const s = settingsRef.current;
    if (!s.audio_alerts) {
      // still mark seen so we don't bing on first toggle on
      signals.forEach((sig) => seenIds.current.add(sig.id));
      return;
    }
    const threshold = s.audio_strength_threshold ?? 8;
    const newOnes = signals.filter((sig) => !seenIds.current.has(sig.id));
    newOnes.forEach((sig) => seenIds.current.add(sig.id));
    const ringWorthy = newOnes.find((sig) => !sig.read && sig.strength >= threshold);
    if (!ringWorthy) return;

    try {
      if (!ctxRef.current) {
        ctxRef.current = new (window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext)();
      }
      const ctx = ctxRef.current;
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.type = "sine";
      // Two-tone bell: 880 then 660
      const t = ctx.currentTime;
      osc.frequency.setValueAtTime(880, t);
      osc.frequency.setValueAtTime(660, t + 0.12);
      gain.gain.setValueAtTime(0.0001, t);
      gain.gain.exponentialRampToValueAtTime(0.18, t + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.4);
      osc.start(t);
      osc.stop(t + 0.45);
    } catch {
      /* ignore — popup may not have user gesture yet */
    }
  }, [signals]);
}
