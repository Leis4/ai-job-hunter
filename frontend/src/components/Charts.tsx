import { cn } from "@/lib/utils";

export function Funnel({ items }: { items: { label: string; count: number; share: number }[] }) {
  const max = Math.max(...items.map((i) => i.count), 1);
  return (
    <div className="space-y-2.5">
      {items.map((item) => (
        <div key={item.label}>
          <div className="mb-1 flex items-center justify-between text-[12px]">
            <span className="text-muted-foreground">{item.label}</span>
            <span className="font-semibold tabular-nums">{item.count}</span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-muted">
            <div className="h-full rounded-full bg-primary/80" style={{ width: item.count ? `${Math.max(2, (item.count / max) * 100)}%` : 0 }} />
          </div>
        </div>
      ))}
    </div>
  );
}

export function Sparkline({ points, height = 64, className }: { points: number[]; height?: number; className?: string }) {
  const width = 320;
  const max = Math.max(...points, 1);
  const min = Math.min(...points, 0);
  const range = Math.max(1, max - min);
  const coords = points.map((p, i) => {
    const x = (i / Math.max(1, points.length - 1)) * width;
    const y = height - 8 - ((p - min) / range) * (height - 16);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  return (
    <svg viewBox={`0 0 ${width} ${height}`} className={cn("w-full", className)} preserveAspectRatio="none" role="img" aria-label="Динамика">
      <polyline points={coords.join(" ")} fill="none" stroke="currentColor" className="text-primary" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}
