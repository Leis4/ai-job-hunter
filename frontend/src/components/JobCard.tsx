import { Link } from "react-router-dom";
import { useState } from "react";
import { Bookmark, BookmarkCheck, ExternalLink, Loader2, Send } from "lucide-react";
import { Job } from "@/lib/types";
import { apiPost } from "@/lib/api";
import { cn, money, MODE_LABELS, STATUS_LABELS } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

export function StatusBadge({ status }: { status: string }) {
  const variant = status === "invited" || status === "offer" ? "success" : status === "rejected" ? "danger" : "default";
  return <Badge variant={variant}>{STATUS_LABELS[status] || status}</Badge>;
}

type Verdict = { label: string; variant: "success" | "warning" | "danger"; canPrepare: boolean; hint: string };

export function jobVerdict(job: Pick<Job, "eligible" | "eligibility">): Verdict {
  if (job.eligibility === "rejected") {
    return { label: "Не подходит", variant: "danger", canPrepare: false,
      hint: "Есть обязательное несовпадение в требованиях" };
  }
  if (job.eligibility === "review" || job.eligibility === "weak") {
    return { label: "Нужно проверить", variant: "warning", canPrepare: true,
      hint: "Проверьте требования перед откликом" };
  }
  if (!job.eligible) {
    return { label: "Проверить вручную", variant: "warning", canPrepare: true,
      hint: "Вакансия не прошла автоматический порог" };
  }
  return { label: "Подходит", variant: "success", canPrepare: true, hint: "Можно готовить отклик" };
}

export function realismVariant(value: number): "success" | "warning" | "danger" {
  return value >= 60 ? "success" : value >= 40 ? "warning" : "danger";
}

export function EligibilityBadge({ job }: { job: Pick<Job, "eligible" | "eligibility"> }) {
  const verdict = jobVerdict(job);
  return <Badge variant={verdict.variant}>{verdict.label}</Badge>;
}

function matchLabel(score: number) {
  if (score >= 80) return "Высокое соответствие";
  if (score >= 65) return "Хорошее соответствие";
  return "Среднее соответствие";
}

export function JobCard({ job, onChanged, compact = false }: { job: Job; onChanged?: () => void; compact?: boolean }) {
  const [busy, setBusy] = useState(false);
  const [fav, setFav] = useState(job.favorite);
  const [err, setErr] = useState("");
  const verdict = jobVerdict(job);
  const salary = money(job.salary_min, job.salary_max, job.currency);
  const matched = job.skills.matched.slice(0, 2);
  const missing = job.skills.required_missing.slice(0, 1);

  async function favorite() {
    try {
      const r = await apiPost<{ favorite: boolean }>("/api/jobs/favorite", { id: job.id });
      setFav(r.favorite); setErr(""); onChanged?.();
    } catch (e) { setErr((e as Error).message); }
  }
  async function prepare() {
    setBusy(true); setErr("");
    try {
      if (!job.eligible && !window.confirm("Подготовить отклик вручную для этой вакансии?")) return;
      await apiPost("/api/jobs/prepare", { id: job.id, manual_review: !job.eligible });
      onChanged?.();
    } catch (e) { setErr((e as Error).message); }
    finally { setBusy(false); }
  }

  return (
    <div className={cn("group flex gap-4 border-b px-4 py-4 last:border-b-0 sm:px-5", compact && "py-3.5")}>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
          <Link to={`/vacancies/${job.id}`} className="text-[15px] font-semibold leading-snug tracking-[-0.01em] hover:text-primary">
            {job.title}
          </Link>
          {salary !== "—" && <span className="text-[12.5px] font-medium text-foreground/80">{salary}</span>}
        </div>
        <div className="mt-0.5 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-[12px] text-muted-foreground">
          <span className="font-medium text-foreground/70">{job.company || "Компания не указана"}</span>
          {job.location && <span>· {job.location}</span>}
          {job.work_mode && <span>· {MODE_LABELS[job.work_mode] || job.work_mode}</span>}
        </div>
        {!compact && job.description && <div className="mt-2 line-clamp-2 text-[12.5px] leading-relaxed text-muted-foreground">{job.description}</div>}
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <span className={cn("inline-flex items-center gap-1.5 text-[11.5px] font-medium",
            job.eligibility === "rejected" ? "text-rose-600 dark:text-rose-400" : "text-emerald-700 dark:text-emerald-300")}>
            <span className={cn("h-1.5 w-1.5 rounded-full", job.eligibility === "rejected" ? "bg-rose-500" : "bg-emerald-500")} />
            {matchLabel(job.score)}
          </span>
          {matched.map((s) => <Badge key={s} variant="primary">{s}</Badge>)}
          {missing.map((s) => <Badge key={s} variant="warning">нет {s}</Badge>)}
          {job.category === "opportunity" && <Badge variant="outline">перспективная</Badge>}
        </div>
        {err && <div className="mt-1.5 text-[11.5px] text-destructive">{err}</div>}
      </div>
      <div className="flex shrink-0 flex-col items-end justify-between gap-2">
        <div className="flex items-center gap-1">
          <Button variant="ghost" size="icon" onClick={favorite} title={fav ? "Убрать из сохранённых" : "Сохранить"} aria-label="Сохранить">
            {fav ? <BookmarkCheck className="h-4 w-4 text-primary" /> : <Bookmark className="h-4 w-4" />}
          </Button>
          <Button variant="ghost" size="icon" asChild title="Открыть оригинал" aria-label="Открыть оригинал">
            <a href={job.url} target="_blank" rel="noopener noreferrer"><ExternalLink className="h-4 w-4" /></a>
          </Button>
        </div>
        <div className="flex items-center gap-1.5">
          <Button variant="outline" size="sm" asChild><Link to={`/vacancies/${job.id}`}>Подробнее</Link></Button>
          <Button size="sm" onClick={prepare} disabled={busy || !verdict.canPrepare} title={verdict.hint}>
            {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}
            <span className="hidden sm:inline">Откликнуться</span>
          </Button>
        </div>
      </div>
    </div>
  );
}
