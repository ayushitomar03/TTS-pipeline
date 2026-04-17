"use client";

import { useRef, useState, useCallback } from "react";

export type ChunkStatus = "pending" | "processing" | "retrying" | "done" | "failed";

export interface AudioChunk {
  id: string;
  text: string;
  status: ChunkStatus;
  audioUrl: string | null;
  cached: boolean;
  createdAt: number;
  error?: string;
  retryCount?: number;
}

const WS_BASE = process.env.NEXT_PUBLIC_WS_URL ?? "ws://localhost:8080/ws/stream";
const API_KEY = process.env.NEXT_PUBLIC_TTS_API_KEY ?? "";

// Build the WebSocket URL, appending the API key as a query param when set.
function buildWsUrl(): string {
  if (!API_KEY) return WS_BASE;
  const sep = WS_BASE.includes("?") ? "&" : "?";
  return `${WS_BASE}${sep}api_key=${encodeURIComponent(API_KEY)}`;
}

// Detects sentence boundaries in text typed so far.
// Returns [completedSentences, remainder].
//
// Strategy: split on sentence-ending punctuation (.!?) that is followed by
// whitespace + an uppercase letter. This avoids false splits on abbreviations
// like "Dr. Smith", "U.S.A.", and decimal numbers like "3.14", while still
// catching real sentence boundaries like "Hello world. The next sentence."
function extractSentences(text: string): [string[], string] {
  // Lookahead: only split where [.!?] is followed by whitespace + uppercase.
  // The split keeps the punctuation with the preceding sentence.
  const parts = text.split(/(?<=[.!?])\s+(?=[A-Z])/);

  if (parts.length <= 1) return [[], text.trim()];

  const sentences = parts.slice(0, -1).map((s) => s.trim()).filter((s) => s.length > 0);
  const remainder = parts[parts.length - 1].trim();
  return [sentences, remainder];
}

const MAX_RETRIES = 3;
const RETRY_BASE_MS = 2000;

export function useTTSStream(voice: string, speed: number) {
  const ws = useRef<WebSocket | null>(null);
  const [chunks, setChunks] = useState<AudioChunk[]>([]);
  const [connected, setConnected] = useState(false);
  const sentCount = useRef(0);
  const audioQueue = useRef<AudioChunk[]>([]);
  const isPlaying = useRef(false);
  const debounceTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Text of the last mid-sentence partial sent by the debounce timer.
  // Prevents double-sending when the user completes a sentence that was already
  // partially dispatched (e.g. debounce sent "The quick", user then finishes
  // "The quick brown fox." — without this we'd send both).
  const lastPartialText = useRef("");
  // Exponential backoff state: starts at 2s, doubles each failure, caps at 30s.
  const reconnectDelay = useRef(2000);
  // Retry tracking: chunk_id → { attempts, voice, speed, text }
  const retryDataRef = useRef<Map<string, { text: string; voice: string; speed: number; attempts: number }>>(new Map());
  const retryTimersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());

  // ── WebSocket connection ────────────────────────────────────────────────

  const connect = useCallback(() => {
    if (
      ws.current?.readyState === WebSocket.OPEN ||
      ws.current?.readyState === WebSocket.CONNECTING
    ) return;

    const socket = new WebSocket(buildWsUrl());

    socket.onopen = () => {
      setConnected(true);
      reconnectDelay.current = 2000; // reset backoff on successful connect
      if (reconnectTimer.current) {
        clearTimeout(reconnectTimer.current);
        reconnectTimer.current = null;
      }
    };

    socket.onclose = () => {
      setConnected(false);
      ws.current = null;
      // Exponential backoff: 2s → 4s → 8s … capped at 30s.
      // Avoids hammering a server that just restarted.
      const delay = reconnectDelay.current;
      reconnectDelay.current = Math.min(delay * 2, 30_000);
      reconnectTimer.current = setTimeout(() => connect(), delay);
    };

    socket.onerror = () => {
      setConnected(false);
    };

    socket.onmessage = (event) => {
      const data = JSON.parse(event.data);
      const { chunk_id, status, audio_b64, cached, retryable, error } = data;

      if (status === "done" && audio_b64) {
        retryDataRef.current.delete(chunk_id);

        const binary = atob(audio_b64);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
        const blob = new Blob([bytes], { type: "audio/mpeg" });
        const audioUrl = URL.createObjectURL(blob);

        setChunks((prev) =>
          prev.map((c) =>
            c.id === chunk_id ? { ...c, status: "done", audioUrl, cached: !!cached } : c
          )
        );

        audioQueue.current = audioQueue.current.map((c) =>
          c.id === chunk_id ? { ...c, status: "done", audioUrl, cached: !!cached } : c
        );
        playNext();
      } else if (status === "failed") {
        const entry = retryDataRef.current.get(chunk_id);
        const attempts = entry?.attempts ?? 0;

        if (retryable && attempts < MAX_RETRIES && entry) {
          const delay = RETRY_BASE_MS * Math.pow(2, attempts);
          retryDataRef.current.set(chunk_id, { ...entry, attempts: attempts + 1 });

          setChunks((prev) =>
            prev.map((c) =>
              c.id === chunk_id ? { ...c, status: "retrying", retryCount: attempts + 1 } : c
            )
          );

          const timer = setTimeout(() => {
            retryTimersRef.current.delete(chunk_id);
            const current = retryDataRef.current.get(chunk_id);
            if (!current || !ws.current || ws.current.readyState !== WebSocket.OPEN) return;

            setChunks((prev) =>
              prev.map((c) => c.id === chunk_id ? { ...c, status: "processing" } : c)
            );
            ws.current.send(JSON.stringify({ chunk_id, text: current.text, voice: current.voice, speed: current.speed }));
          }, delay);

          retryTimersRef.current.set(chunk_id, timer);
        } else {
          retryDataRef.current.delete(chunk_id);
          setChunks((prev) =>
            prev.map((c) => c.id === chunk_id ? { ...c, status: "failed", error } : c)
          );
          audioQueue.current = audioQueue.current.filter((c) => c.id !== chunk_id);
          playNext();
        }
      }
    };

    ws.current = socket;
  }, []);

  // ── Sequential audio playback ───────────────────────────────────────────

  const playNext = useCallback(() => {
    if (isPlaying.current) return;

    const next = audioQueue.current.find((c) => c.status === "done" && c.audioUrl);
    if (!next) return;

    const idx = audioQueue.current.findIndex((c) => c.id === next.id);
    const allPriorDone = audioQueue.current
      .slice(0, idx)
      .every((c) => c.status === "done" || c.status === "failed");

    if (!allPriorDone) return;

    isPlaying.current = true;
    const audio = new Audio(next.audioUrl!);
    audio.onended = () => {
      audioQueue.current = audioQueue.current.filter((c) => c.id !== next.id);
      isPlaying.current = false;
      playNext();
    };
    audio.play().catch(() => {
      isPlaying.current = false;
      audioQueue.current = audioQueue.current.filter((c) => c.id !== next.id);
      playNext();
    });
  }, []);

  // ── Send a sentence chunk to the backend ───────────────────────────────

  const sendChunk = useCallback(
    (text: string) => {
      if (!text.trim()) return;
      if (!ws.current || ws.current.readyState !== WebSocket.OPEN) return;

      const chunk_id = crypto.randomUUID();
      const chunk: AudioChunk = {
        id: chunk_id,
        text,
        status: "processing",
        audioUrl: null,
        cached: false,
        createdAt: Date.now(),
      };

      retryDataRef.current.set(chunk_id, { text, voice, speed, attempts: 0 });
      setChunks((prev) => [...prev, chunk]);
      audioQueue.current.push({ ...chunk });
      ws.current.send(JSON.stringify({ chunk_id, text, voice, speed }));
    },
    [voice, speed]
  );

  // ── Called on every keystroke ───────────────────────────────────────────

  const onTextChange = useCallback(
    (text: string) => {
      if (!ws.current || ws.current.readyState !== WebSocket.OPEN) return;

      const [sentences] = extractSentences(text);

      // User deleted text — walk counters back and clear partial tracking.
      if (sentences.length < sentCount.current) {
        sentCount.current = sentences.length;
        lastPartialText.current = "";
      }

      for (let i = sentCount.current; i < sentences.length; i++) {
        const sentence = sentences[i];

        if (lastPartialText.current && sentence.startsWith(lastPartialText.current)) {
          // This sentence was already partially sent by the debounce.
          // Send only the tail so the user doesn't hear the same opening twice.
          // e.g. debounce sent "The quick" → send only "brown fox." here.
          const delta = sentence.slice(lastPartialText.current.length).trim();
          if (delta.length > 3) sendChunk(delta);
          lastPartialText.current = "";
        } else {
          sendChunk(sentence);
        }
      }
      sentCount.current = sentences.length;

      if (debounceTimer.current) clearTimeout(debounceTimer.current);
      debounceTimer.current = setTimeout(() => {
        const [, remainder] = extractSentences(text);
        if (remainder.length > 10) {
          if (lastPartialText.current && remainder.startsWith(lastPartialText.current)) {
            // User paused again mid-sentence after a previous partial was sent.
            // Only send what's new since the last partial to avoid repeating.
            const delta = remainder.slice(lastPartialText.current.length).trim();
            if (delta.length > 3) {
              sendChunk(delta);
              lastPartialText.current = remainder; // update to the full remainder
            }
          } else {
            sendChunk(remainder);
            lastPartialText.current = remainder;
          }
        }
      }, 800);
    },
    [sendChunk]
  );

  const reset = useCallback(() => {
    setChunks([]);
    sentCount.current = 0;
    audioQueue.current = [];
    isPlaying.current = false;
    lastPartialText.current = "";
    if (debounceTimer.current) clearTimeout(debounceTimer.current);
    retryTimersRef.current.forEach((t) => clearTimeout(t));
    retryTimersRef.current.clear();
    retryDataRef.current.clear();
  }, []);

  return { chunks, connected, connect, onTextChange, reset };
}
