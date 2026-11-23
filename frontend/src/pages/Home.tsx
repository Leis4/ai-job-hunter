import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, Bell, BriefcaseBusiness, CheckCircle2, Clock3, MessageSquare, Sparkles, AlertCircle } from "lucide-react";
import { apiGet } from "@/lib/api";
import { Dashboard, Job } from "@/lib/types";
import { dt } from "@/lib/utils";
import { PageHeader, EmptyState, ErrorState, LoadingState } from "@/components/PageChrome";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

type Decision = { id: number; kind: string; employer: string; title: string; question: string; urgency: string };

function attentionLabel(n: number) {
  const mod10 = n % 10, mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return `${n} задача ждёт решения`;
  if (mod10 >= 2 && mod10 <= 4 && !(mod100 >= 12 && mod100 <= 14)) return `${n} задачи ждут решения`;
  return `${n} задач ждут решения`;
}

function Stat({ label, value, hint, icon }: { label: string; value: number | string; hint?: string; icon: React.ReactNode }) {
  return (
    <Card className="p-4">
      <div className="flex items-center justify-between text-muted-foreground"><span className="text-[12px] font-medium">{label}</span>{icon}</div>
      <div className="mt-2 text-[26px] font-semibold leading-none tracking-[-0.03em]">{value}</div>
      {hint && <div className="mt-1.5 text-[11.5px] text-muted-foreground">{hint}</div>}
    </Card>
  );
}

function JobLine({ job }: { job: Job }) {
  return (
    <Link to={`/vacancies/${job.id}`} className="flex items-center gap-3 rounded-lg px-2 py-3 transition-colors hover:bg-accent/60">
      <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-[12px] font-semibold text-primary">
        {(job.company || job.title).slice(0, 1).toUpperCase()}
      </div>
      <div className="min-w-0 flex-1">
        <div className="truncate text-[13.5px] font-medium">{job.title}</div>
        <div className="truncate text-[11.5px] text-muted-foreground">
          {job.company || "Компания не указана"}{job.location ? ` · ${job.location}` : ""}{job.work_mode ? ` · ${job.work_mode === "remote" ? "удалённо" : job.work_mode === "hybrid" ? "гибрид" : "офис"}` : ""}
        </div>
      </div>
      <span className={`h-2 w-2 shrink-0 rounded-full ${job.score >= 65 ? "bg-emerald-500" : "bg-amber-500"}`} title={`${Math.round(job.score)}/100`} />
    </Link>
  );
}

export default function HomePage() {
  const [data, setData] = useState<Dashboard | null>(null);
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [apps, setApps] = useState<any[]>([]);
  const [error, setError] = useState("");

  async function load() {
    try {
      const [d, decisionsData, appsData] = await Promise.all([
        apiGet<Dashboard>("/api/dashboard"),
        apiGet<any>("/api/autopilot/decisions").catch(() => ({ items: [] })),
        apiGet<any>("/api/applications").catch(() => ({ items: [] })),
      ]);
      setData(d); setDecisions(decisionsData.items || []); setApps(appsData.items || []); setError("");
    } catch (e) { setError((e as Error).message); }
  }
  useEffect(() => { load(); }, []);

  if (error && !data) return <ErrorState error={error} onRetry={load} />;
  if (!data) return <LoadingState rows={5} />;
  const s = data.summary;
  const automation = data.automation || {};
  const activeApps = (apps || []).filter((a: any) => ["queued", "manual_review", "requires_user_action", "approved", "form_filling"].includes(a.state));
  const contactGap = activeApps.filter((a: any) => a.readiness === "needs_contact_info").length;
  const attention = [
    ...decisions.slice(0, 3).map((d) => ({
      key: `d-${d.id}`, title: d.employer || "Работодатель", text: d.question || d.title,
      hint: d.kind === "interview_invitation" || d.kind === "scheduling_request" ? "Нужно согласовать время" : "Нужен ваш ответ",
      href: "/autopilot", urgency: d.urgency,
    })),
    ...(contactGap ? [{
      key: "contacts", title: "Заполните контактные данные", text: "Укажите имя, фамилию и email — без них часть откликов не отправить.",
      hint: "Имя, фамилия и email в профиле", href: "/applications", urgency: "high",
    }] : []),
    ...(activeApps.filter((a: any) => a.state === "requires_user_action").length ? [{
      key: "form", title: "Нужно заполнить форму", text: "Откройте отклики и завершите подготовленные формы.",
      hint: "Внешняя форма или Telegram-контакт", href: "/applications", urgency: "normal",
    }] : []),
  ];
  const events = (data.events || []).filter((e) => !e.type.startsWith("letter_") && !e.type.includes("form_filling")).slice(0, 6);
  const topJobs = (data.top_jobs || []).slice(0, 4);

  return (
    <div className="animate-rise">
      <PageHeader eyebrow="Сегодня" title="Поиск работы"
        description="Главное на одном экране: что нашлось, что происходит с откликами и что требует вашего решения."
        actions={<div className="flex items-center gap-2">
          <Badge variant={automation.enabled ? "success" : "warning"} className="gap-1.5">
            <span className={`h-1.5 w-1.5 rounded-full ${automation.enabled ? "bg-emerald-500" : "bg-amber-500"}`} />
            {automation.enabled ? "Поиск работает" : "Поиск приостановлен"}
          </Badge>
          <Button size="sm" variant="outline" asChild><Link to="/statistics">Аналитика <ArrowRight className="h-3.5 w-3.5" /></Link></Button>
        </div>} />

      <Card className="mb-4 overflow-hidden border-primary/20 bg-gradient-to-br from-primary/[0.06] to-transparent">
        <CardContent className="p-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-start gap-3">
              <div className="mt-0.5 flex h-9 w-9 items-center justify-center rounded-full bg-primary/10 text-primary"><Bell className="h-4 w-4" /></div>
              <div>
                <h2 className="text-[15px] font-semibold">Нужно ваше внимание</h2>
                {attention.length ? <div className="mt-0.5 text-[12px] text-muted-foreground">{attentionLabel(attention.length)}</div> : <div className="mt-0.5 text-[12px] text-muted-foreground">Всё в порядке. Новых вопросов нет.</div>}
              </div>
            </div>
            {attention.length ? <Button size="sm" asChild><Link to={attention[0].href}>Открыть</Link></Button> : <Badge variant="success" className="gap-1"><CheckCircle2 className="h-3 w-3" />Всё под контролем</Badge>}
          </div>
          {attention.length > 0 && (
            <div className="mt-4 grid gap-2 sm:grid-cols-2">
              {attention.slice(0, 4).map((a) => (
                <Link key={a.key} to={a.href} className="rounded-lg border bg-card/80 p-3 transition-colors hover:border-primary/30">
                  <div className="flex items-center gap-2"><AlertCircle className={`h-3.5 w-3.5 ${a.urgency === "high" || a.urgency === "critical" ? "text-amber-500" : "text-muted-foreground"}`} /><b className="text-[12.5px]">{a.title}</b></div>
                  <div className="mt-1 line-clamp-2 text-[11.5px] text-muted-foreground">{a.text}</div>
                  <div className="mt-1 text-[10.5px] text-muted-foreground/80">{a.hint}</div>
                </Link>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Подходящие вакансии" value={s.eligible || 0} hint="прошли ваши требования" icon={<BriefcaseBusiness className="h-4 w-4" />} />
        <Stat label="Отправленные отклики" value={s.sent || 0} hint="подтверждённые площадкой" icon={<Sparkles className="h-4 w-4" />} />
        <Stat label="Новые ответы" value={s.replied || 0} hint="сообщения работодателей" icon={<MessageSquare className="h-4 w-4" />} />
        <Stat label="Приглашения" value={s.invited || 0} hint="интервью и офферы" icon={<CheckCircle2 className="h-4 w-4" />} />
      </div>

      <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_340px]">
        <Card>
          <div className="flex items-center justify-between px-4 pb-1 pt-4 sm:px-5">
            <div><h2 className="text-[14px] font-semibold">Лучшие вакансии</h2><p className="text-[11.5px] text-muted-foreground">Наиболее близкие к вашему профилю</p></div>
            <Button size="sm" variant="ghost" asChild><Link to="/vacancies">Все <ArrowRight className="h-3.5 w-3.5" /></Link></Button>
          </div>
          <CardContent className="p-2 sm:p-3">
            {topJobs.length ? topJobs.map((job) => <JobLine key={job.id} job={job} />) : <EmptyState icon={<BriefcaseBusiness className="h-5 w-5" />} title="Пока ничего не найдено" description="Поиск продолжится автоматически. Проверьте подключение источников." />}
          </CardContent>
        </Card>

        <div className="space-y-4">
          <Card>
            <div className="flex items-center justify-between px-4 pb-1 pt-4"><h2 className="text-[14px] font-semibold">Последние события</h2><Link to="/applications" className="text-[11.5px] text-primary hover:underline">Отклики</Link></div>
            <CardContent className="space-y-0.5 p-2">
              {events.length ? events.map((e) => (
                <div key={e.id} className="flex gap-2 rounded-lg px-2 py-2 text-[11.5px]">
                  <Clock3 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground/60" />
                  <div className="min-w-0"><div className="truncate text-foreground/80">{e.job_title || e.type.replaceAll("_", " ")}</div><div className="text-muted-foreground">{dt(e.created_at)}</div></div>
                </div>
              )) : <div className="px-2 py-6 text-center text-[12px] text-muted-foreground">Событий пока нет.</div>}
            </CardContent>
          </Card>

          {data.startups?.top?.length ? (
            <Card>
              <div className="flex items-center justify-between px-4 pb-1 pt-4"><h2 className="text-[14px] font-semibold">Стартапы</h2><Link to="/startups" className="text-[11.5px] text-primary hover:underline">Все</Link></div>
              <CardContent className="p-2">
                {data.startups.top.slice(0, 3).map((lead) => (
                  <Link key={lead.id} to="/startups" className="flex items-center justify-between rounded-lg px-2 py-2.5 hover:bg-accent/60">
                    <div className="min-w-0"><div className="truncate text-[12.5px] font-medium">{lead.company}</div><div className="truncate text-[11px] text-muted-foreground">{lead.kind_label}</div></div>
                    <span className={`h-2 w-2 rounded-full ${lead.is_paid ? "bg-emerald-500" : lead.pay_status === "unpaid" ? "bg-slate-300" : "bg-amber-400"}`} />
                  </Link>
                ))}
              </CardContent>
            </Card>
          ) : null}
        </div>
      </div>
    </div>
  );
}
