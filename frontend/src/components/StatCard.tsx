import { ReactNode } from "react";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";

export function StatCard({ label, value, hint, tone = "default", icon }: {
  label: string; value: ReactNode; hint?: string; tone?: "default" | "success" | "warning" | "danger"; icon?: ReactNode;
}) {
  return (
    <Card className="p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-[12px] font-medium text-muted-foreground">{label}</div>
          <div className="mt-1 text-[26px] font-semibold leading-none tracking-[-0.03em]">{value}</div>
          {hint && <div className={cn("mt-1.5 text-[11.5px] text-muted-foreground", tone === "success" && "text-emerald-600 dark:text-emerald-400", tone === "warning" && "text-amber-600 dark:text-amber-400", tone === "danger" && "text-rose-600 dark:text-rose-400")}>{hint}</div>}
        </div>
        {icon && <div className="rounded-md bg-muted p-2 text-muted-foreground">{icon}</div>}
      </div>
    </Card>
  );
}
