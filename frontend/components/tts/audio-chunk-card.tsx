"use client";

import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { AudioChunk, ChunkStatus } from "@/hooks/use-tts-stream";
import { CheckCircle, Loader2, RefreshCw, XCircle, Zap } from "lucide-react";

const statusConfig: Record<ChunkStatus, {
  label: (chunk: AudioChunk) => string;
  variant: "default" | "secondary" | "destructive" | "outline";
  icon: React.ReactNode;
}> = {
  pending: {
    label: () => "Pending",
    variant: "outline",
    icon: <Loader2 className="h-3 w-3 animate-spin" />,
  },
  processing: {
    label: () => "Processing",
    variant: "secondary",
    icon: <Loader2 className="h-3 w-3 animate-spin" />,
  },
  retrying: {
    label: (c) => `Retrying${c.retryCount ? ` (${c.retryCount}/${3})` : ""}`,
    variant: "outline",
    icon: <RefreshCw className="h-3 w-3 animate-spin" />,
  },
  done: {
    label: () => "Ready",
    variant: "default",
    icon: <CheckCircle className="h-3 w-3" />,
  },
  failed: {
    label: () => "Failed",
    variant: "destructive",
    icon: <XCircle className="h-3 w-3" />,
  },
};

export function AudioChunkCard({ chunk }: { chunk: AudioChunk }) {
  const config = statusConfig[chunk.status];

  return (
    <Card className="p-4 flex flex-col gap-3 bg-card border-border">
      {/* Text */}
      <p className="text-sm text-foreground leading-relaxed">{chunk.text}</p>

      {/* Status row */}
      <div className="flex items-center gap-2 flex-wrap">
        <Badge variant={config.variant} className="flex items-center gap-1 text-xs">
          {config.icon}
          {config.label(chunk)}
        </Badge>
        {chunk.cached && (
          <Badge variant="outline" className="flex items-center gap-1 text-xs text-yellow-500 border-yellow-500">
            <Zap className="h-3 w-3" />
            Cached
          </Badge>
        )}
      </div>

      {/* Error message */}
      {chunk.status === "failed" && chunk.error && (
        <p className="text-xs text-destructive">{chunk.error}</p>
      )}

      {/* Audio player */}
      {chunk.status === "done" && chunk.audioUrl && (
        <audio
          controls
          src={chunk.audioUrl}
          className="w-full h-8"
          style={{ colorScheme: "dark" }}
        />
      )}
    </Card>
  );
}
