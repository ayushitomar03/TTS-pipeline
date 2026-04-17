"use client";

import { useEffect, useRef, useState } from "react";
import { useTTSStream } from "@/hooks/use-tts-stream";
import { AudioChunkCard } from "@/components/tts/audio-chunk-card";
import { Textarea } from "@/components/ui/textarea";
import { Slider } from "@/components/ui/slider";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Button } from "@/components/ui/button";
import {
  SidebarProvider,
  Sidebar,
  SidebarContent,
  SidebarHeader,
  SidebarGroup,
  SidebarGroupLabel,
  SidebarGroupContent,
  SidebarInset,
  SidebarTrigger,
} from "@/components/ui/sidebar";
import { ArrowDown, Mic, RotateCcw, Wifi, WifiOff } from "lucide-react";

const VOICES = [
  { id: "en-US-Neural2-C", label: "US English · Female" },
  { id: "en-US-Neural2-D", label: "US English · Male" },
  { id: "en-US-Neural2-F", label: "US English · Female 2" },
  { id: "en-US-Neural2-J", label: "US English · Male 2" },
  { id: "en-GB-Neural2-A", label: "UK English · Female" },
  { id: "en-GB-Neural2-B", label: "UK English · Male" },
];

export default function Home() {
  const [voice, setVoice] = useState("en-US-Neural2-C");
  const [speed, setSpeed] = useState(1.0);
  const [text, setText] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);

  const { chunks, connected, connect, onTextChange, reset } = useTTSStream(voice, speed);
  const isAtBottom = useRef(true);
  // Controls the visible "jump to bottom" button. State (not ref) because
  // showing/hiding it requires a re-render.
  const [showJump, setShowJump] = useState(false);
  // How many chunks arrived while the user was scrolled up.
  const newChunksRef = useRef(0);
  const [newChunkCount, setNewChunkCount] = useState(0);

  useEffect(() => {
    connect();
  }, [connect]);

  const getScrollParent = (el: HTMLElement | null): HTMLElement | null => {
    if (!el || el === document.body) return null;
    const { overflow, overflowY } = window.getComputedStyle(el);
    if (/auto|scroll/.test(overflow + overflowY)) return el;
    return getScrollParent(el.parentElement as HTMLElement | null);
  };

  const scrollToBottom = () => {
    const viewport = getScrollParent(bottomRef.current);
    if (viewport) viewport.scrollTop = viewport.scrollHeight;
    isAtBottom.current = true;
    setShowJump(false);
    newChunksRef.current = 0;
    setNewChunkCount(0);
  };

  // Attach scroll listener once on mount.
  useEffect(() => {
    const viewport = getScrollParent(bottomRef.current);
    if (!viewport) return;

    const onScroll = () => {
      const { scrollTop, scrollHeight, clientHeight } = viewport;
      const atBottom = scrollHeight - scrollTop - clientHeight < 60;
      if (atBottom && !isAtBottom.current) {
        // User scrolled back to bottom — hide button, reset counter.
        isAtBottom.current = true;
        setShowJump(false);
        newChunksRef.current = 0;
        setNewChunkCount(0);
      } else if (!atBottom && isAtBottom.current) {
        isAtBottom.current = false;
      }
    };

    viewport.addEventListener("scroll", onScroll, { passive: true });
    return () => viewport.removeEventListener("scroll", onScroll);
  }, []);

  // On new chunk: auto-scroll if at bottom, otherwise increment counter + show button.
  useEffect(() => {
    if (chunks.length === 0) return;
    if (isAtBottom.current) {
      const viewport = getScrollParent(bottomRef.current);
      if (viewport) viewport.scrollTop = viewport.scrollHeight;
    } else {
      newChunksRef.current += 1;
      setNewChunkCount(newChunksRef.current);
      setShowJump(true);
    }
  }, [chunks.length]);

  const handleTextChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const val = e.target.value;
    setText(val);
    onTextChange(val);
  };

  const handleReset = () => {
    setText("");
    reset();
  };

  return (
    <SidebarProvider>
      <div className="flex h-screen w-full bg-background">

        {/* ── Sidebar ── */}
        <Sidebar className="border-r border-border">
          <SidebarHeader className="p-4">
            <div className="flex items-center gap-2">
              <Mic className="h-5 w-5 text-primary" />
              <span className="font-semibold text-lg">TTS Stream</span>
            </div>
            <div className="mt-2">
              {connected ? (
                <Badge className="flex items-center gap-1 text-xs bg-green-600 hover:bg-green-600 w-fit">
                  <Wifi className="h-3 w-3" /> Live
                </Badge>
              ) : (
                <Badge variant="destructive" className="flex items-center gap-1 text-xs w-fit">
                  <WifiOff className="h-3 w-3" /> Disconnected
                </Badge>
              )}
            </div>
          </SidebarHeader>

          <SidebarContent>
            {/* Voice */}
            <SidebarGroup>
              <SidebarGroupLabel>Voice</SidebarGroupLabel>
              <SidebarGroupContent className="px-2 space-y-1">
                {VOICES.map((v) => (
                  <button
                    key={v.id}
                    onClick={() => setVoice(v.id)}
                    className={`w-full text-left px-3 py-2 rounded-md text-sm transition-colors
                      ${voice === v.id
                        ? "bg-primary text-primary-foreground"
                        : "hover:bg-muted text-muted-foreground hover:text-foreground"
                      }`}
                  >
                    {v.label}
                  </button>
                ))}
              </SidebarGroupContent>
            </SidebarGroup>

            <Separator className="my-2" />

            {/* Speed */}
            <SidebarGroup>
              <SidebarGroupLabel>Speed — {speed.toFixed(2)}x</SidebarGroupLabel>
              <SidebarGroupContent className="px-4 pt-2">
                <Slider
                  min={0.25}
                  max={2.0}
                  step={0.25}
                  value={[speed]}
                  onValueChange={([val]) => setSpeed(val)}
                  className="w-full"
                />
                <div className="flex justify-between text-xs text-muted-foreground mt-1">
                  <span>0.25x</span>
                  <span>2x</span>
                </div>
              </SidebarGroupContent>
            </SidebarGroup>

            <Separator className="my-2" />

            {/* Stats */}
            <SidebarGroup>
              <SidebarGroupLabel>Session</SidebarGroupLabel>
              <SidebarGroupContent className="px-4 space-y-2">
                {[
                  { label: "Chunks", value: chunks.length, color: "" },
                  { label: "Ready", value: chunks.filter((c) => c.status === "done").length, color: "text-green-500" },
                  { label: "Cached", value: chunks.filter((c) => c.cached).length, color: "text-yellow-500" },
                ].map(({ label, value, color }) => (
                  <div key={label} className="flex justify-between text-sm">
                    <span className="text-muted-foreground">{label}</span>
                    <span className={`font-medium ${color}`}>{value}</span>
                  </div>
                ))}
              </SidebarGroupContent>
            </SidebarGroup>
          </SidebarContent>
        </Sidebar>

        {/* ── Main Content ── */}
        <SidebarInset className="flex flex-col flex-1 overflow-hidden">

          {/* Header */}
          <header className="flex items-center gap-3 px-6 py-4 border-b border-border shrink-0">
            <SidebarTrigger />
            <div>
              <h1 className="font-semibold text-base">Streaming TTS</h1>
              <p className="text-xs text-muted-foreground">
                Audio generates at each sentence boundary as you type
              </p>
            </div>
            <Button
              variant="outline"
              size="sm"
              onClick={handleReset}
              className="ml-auto flex items-center gap-2"
            >
              <RotateCcw className="h-4 w-4" />
              Reset
            </Button>
          </header>

          {/* Text Input */}
          <div className="px-6 py-4 border-b border-border shrink-0">
            <Textarea
              value={text}
              onChange={handleTextChange}
              placeholder='Start typing... Audio triggers at "." "!" "?" — or after 0.8s pause'
              className="min-h-[120px] resize-none text-sm bg-muted border-0 focus-visible:ring-1"
              disabled={!connected}
            />
            <p className="text-xs text-muted-foreground mt-2">
              Each sentence is queued and played in order. Cached sentences play instantly.
            </p>
          </div>

          {/* Audio Chunks */}
          <div className="relative flex-1 overflow-hidden">
            <ScrollArea className="h-full px-6 py-4">
              {chunks.length === 0 ? (
                <div className="flex flex-col items-center justify-center h-48 text-center">
                  <Mic className="h-10 w-10 text-muted-foreground/30 mb-3" />
                  <p className="text-sm text-muted-foreground">
                    Audio chunks appear here as you type
                  </p>
                  <p className="text-xs text-muted-foreground/60 mt-1">
                    Each sentence is converted and played separately
                  </p>
                </div>
              ) : (
                <div className="space-y-3 pb-4">
                  {chunks.map((chunk) => (
                    <AudioChunkCard key={chunk.id} chunk={chunk} />
                  ))}
                </div>
              )}
              <div ref={bottomRef} />
            </ScrollArea>

            {/* Jump-to-bottom button — visible when the user has scrolled up */}
            {showJump && (
              <button
                onClick={scrollToBottom}
                className="absolute bottom-4 left-1/2 -translate-x-1/2 flex items-center gap-2 px-4 py-2 rounded-full bg-primary text-primary-foreground text-sm font-medium shadow-lg hover:bg-primary/90 transition-all animate-in fade-in slide-in-from-bottom-2"
              >
                <ArrowDown className="h-4 w-4" />
                {newChunkCount > 0 ? `${newChunkCount} new` : "Jump to bottom"}
              </button>
            )}
          </div>

        </SidebarInset>
      </div>
    </SidebarProvider>
  );
}
