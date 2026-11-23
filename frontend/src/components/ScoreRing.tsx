import { cn } from "@/lib/utils";

export function ScoreRing({ score, size = 54, label }: { score: number; size?: number; label?: string }) {
  const value = Math.max(0, Math.min(100, Math.round(score || 0)));
  const color = value >= 80 ? "#0f9f6e" : value >= 65 ? "#3157d5" : value >= 45 ? "#b7791f" : "#be3a4b";
  const radius = (size - 6) / 2;
  const circumference = 2 * Math.PI * radius;
  return (
    <div className="flex flex-col items-center gap-0.5" title={label || `Соответствие ${value} из 100`}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="-rotate-90">
        <circle cx={size / 2} cy={size / 2} r={radius} fill="none" stroke="currentColor" className="text-muted" strokeWidth={4} />
        <circle
          cx={size / 2} cy={size / 2} r={radius} fill="none" stroke={color} strokeWidth={4}
          strokeDasharray={`${(circumference * value) / 100} ${circumference}`} strokeLinecap="round"
        />
      </svg>
      <div className={cn("text-[13px] font-bold leading-none", value === 0 && "text-muted-foreground")} style={{ color }}>
        {value}
      </div>
    </div>
  );
}
